"""状态层：事件日志 / 派发表 / 黑板 / 会话表等持久化（spec §4）。

本模块直接使用 sqlite3（spec §14 禁止 ORM），单文件一线程一 db（§4.1），WAL 模式。
落盘顺序与事务边界严格对应 spec §4.4：
  - append_event  = 事务(1)  事件追加 + 每个 to 目标插 pending 派发行
  - mark_dispatching = 事务(2)  status→dispatching + 写绝对截止时间戳
  - reply_and_done = 事务(5)  回复落盘 + 标 done + 会话 upsert（单事务）

对外符号（docs/m0-contract.md §2 + §8 冻结）：
  类 Store（公开属性 thread_dir）及其方法；
  模块级 apply_blackboard_ops / rebuild_blackboard / board_state。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

# ——————————————————————————————————————————————————————————————
# 故障注入（M4 契约 §1；生产路径零开销：仅当 _CURRENT_FAULT 非 None 时激活）
# ——————————————————————————————————————————————————————————————

class FaultInjector:
    """在指定 site 的第 N 次调用时抛 SystemExit(137)（模拟 SIGKILL）。

    fault_map: {site_name: count}
      - site_name 与 store 关键落盘点的检查点参数对应。
      - count=N 表示"第 N 次 check(site) 命中即抛"；前 N-1 次静默通过。
      - count=0 视为立即触发（首次 check(site) 命中即抛）。
    未在 fault_map 中出现的 site 一律直接返回，不影响正常路径。
    """

    def __init__(self, fault_map: dict[str, int]) -> None:
        # 保存目标次数（拷贝防止外部改动）。
        self._targets: dict[str, int] = dict(fault_map or {})
        # 每个 site 独立计数（当前已 check 的次数）。
        self._counters: dict[str, int] = {}

    def check(self, site: str) -> None:
        if site not in self._targets:
            return
        # 命中语义：
        #   target=0 → 首次 check 即抛（"立即触发"）
        #   target=N (N>=1) → 第 N 次 check 抛
        target = self._targets[site]
        cur = self._counters.get(site, 0) + 1
        self._counters[site] = cur
        if target == 0 or cur == target:
            raise SystemExit(137)


# 全局单例：None 表示未装载注入器；生产路径 _fault_check 直接短路返回。
_CURRENT_FAULT: FaultInjector | None = None


def set_fault_injector(fj: FaultInjector | None) -> None:
    """装载 / 清除全局故障注入器。fj=None 清除。"""
    global _CURRENT_FAULT
    _CURRENT_FAULT = fj


def clear_fault_injector() -> None:
    """显式清除（等价 set_fault_injector(None)）。"""
    global _CURRENT_FAULT
    _CURRENT_FAULT = None


def _fault_check(site: str) -> None:
    """关键落盘点内嵌调用；未注入时零开销直接返回。"""
    if _CURRENT_FAULT is None:
        return
    _CURRENT_FAULT.check(site)


def fault_check(site: str) -> None:
    """公共故障注入检查点（R-T1：§4.4 间隙(3)invoke_post/(4)autocommit_post 复用）。

    store 内嵌的 3 个落盘边界 site（append_event_post / mark_dispatching_post /
    reply_and_done_post）调用私有 `_fault_check`；调度层（core/async_core）的两个
    **控制流位置** site（invoke_post / autocommit_post）经此公共入口触发，共享同一
    全局 FaultInjector 单例与计数语义。未注入时零开销直接返回。

    Lead 裁决（§17）：invoke_post/autocommit_post 按**控制流位置**触发——mock 语境
    无 worktree、autocommit 为 no-op 时位置依然存在，照样触发；不依赖是否真的产生 commit。
    """
    _fault_check(site)


# ——————————————————————————————————————————————————————————————
# 常量：DDL 与门槛
# ——————————————————————————————————————————————————————————————

# spec §4.3：六表 DDL 逐字段照抄，禁止"优化"字段。
_DDL = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  sender TEXT NOT NULL,                  -- from 是关键字, 列名用 sender
  to_json TEXT NOT NULL DEFAULT '[]',
  type TEXT NOT NULL,
  body TEXT NOT NULL,
  re_json TEXT NOT NULL DEFAULT '[]',
  corr TEXT,
  artifacts_json TEXT NOT NULL DEFAULT '[]',
  bb_ops_json TEXT,
  meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS dispatches(
  event_id INTEGER NOT NULL,
  target TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('pending','dispatching','done','gate_wait','failed')),
  deadline_ts REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(event_id, target)
);
CREATE TABLE IF NOT EXISTS sessions(
  role TEXT PRIMARY KEY,
  backend TEXT NOT NULL,
  sid TEXT,
  last_evt INTEGER NOT NULL DEFAULT 0,
  gen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs(
  corr TEXT PRIMARY KEY,
  kind TEXT, cmd TEXT,
  callback_to TEXT NOT NULL,
  started_evt INTEGER,
  status TEXT
);
CREATE TABLE IF NOT EXISTS thread_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS metrics(ts REAL, key TEXT, value REAL, extra TEXT);
"""

# spec §3.3：仅当 type ∈ {decision, acceptance, gate_decision} 时 bb_ops 才可能被应用。
# 该 type 门槛属存储层可判部分；can_decide 门槛由调度层持有角色配置后判定（契约 §2：
# apply_blackboard_ops 不判权限，调用方须已判过；rebuild 只重放此 type 集合以满足 §3.3
# 中"A 类事件"的 type 层门槛，防止 report 等非 A 类的 bb_ops 被投影）。
_BB_OP_TYPES = frozenset({"decision", "acceptance", "gate_decision"})

# terminate 是信号非待办，落盘时不生成派发行（§5.4）。
_NO_DISPATCH_TYPES = frozenset({"terminate"})


# ——————————————————————————————————————————————————————————————
# Store
# ——————————————————————————————————————————————————————————————

class Store:
    """绑定单个线程目录（§4.1：一线程一目录一 db）。"""

    def __init__(self, thread_dir: str | Path) -> None:
        self.thread_dir: Path = Path(thread_dir)
        self._bb_dir: Path = self.thread_dir / "blackboard"
        self._logs_dir: Path = self.thread_dir / "logs"
        self._db_path: Path = self.thread_dir / "events.db"
        self._state_path: Path = self._bb_dir / "state.json"
        self._board_path: Path = self._bb_dir / "board.md"

        # 建目录：<thread_dir>/{events.db, blackboard/, logs/}。
        self.thread_dir.mkdir(parents=True, exist_ok=True)
        self._bb_dir.mkdir(parents=True, exist_ok=True)
        self._logs_dir.mkdir(parents=True, exist_ok=True)

        self._con = sqlite3.connect(str(self._db_path))
        self._con.row_factory = sqlite3.Row
        # WAL 模式（§4.3/§14）。
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA foreign_keys=ON")
        # 建齐全部 DDL（§4.3）。executescript 自带隐式提交。
        self._con.executescript(_DDL)
        self._con.commit()

    # —— 内部事务辅助 ——
    def _begin(self) -> None:
        # 显式事务（§4.4）：凡多写操作显式 BEGIN/COMMIT。
        self._con.execute("BEGIN")

    # ——————————————————————————————————————————————————————
    # §4.4 事务(1)：事件追加 + 派发行生成（单事务）
    # ——————————————————————————————————————————————————————
    def append_event(
        self,
        *,
        sender: str,
        type: str,
        body: str,
        to: list[str] | None = None,
        re: list[int] | None = None,
        corr: str | None = None,
        artifacts: list[str] | None = None,
        blackboard_ops: list[dict] | None = None,
        meta: dict | None = None,
        ts: float | None = None,
        make_dispatches: bool = True,
    ) -> int:
        """插入 events（id 自增）+ 为每个 to 目标插 dispatches(pending)。

        to 为空 → 生成 target='moderator' 的派发行（§4.4(1) 兜底落盘）。
        terminate 型不生成派发行（§5.4）。返回 event_id。

        make_dispatches=False（M5 §5.6.2 通告类事件）：**只落事件、不生成任何派发行**
        ——"比照 terminate（§5.4）：落盘但不生成派发行"，是通告不是待办。缺省 True 时
        本方法行为与既有逐字一致（既有全部调用点均不传该参数）。
        """
        ts_val = time.time() if ts is None else ts
        to_list = list(to) if to else []
        re_list = list(re) if re else []
        artifacts_list = list(artifacts) if artifacts else []
        meta_val = dict(meta) if meta else {}

        self._begin()
        try:
            cur = self._con.execute(
                "INSERT INTO events"
                "(ts, sender, to_json, type, body, re_json, corr,"
                " artifacts_json, bb_ops_json, meta_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ts_val,
                    sender,
                    json.dumps(to_list, ensure_ascii=False),
                    type,
                    body,
                    json.dumps(re_list, ensure_ascii=False),
                    corr,
                    json.dumps(artifacts_list, ensure_ascii=False),
                    None if blackboard_ops is None
                    else json.dumps(blackboard_ops, ensure_ascii=False),
                    json.dumps(meta_val, ensure_ascii=False),
                ),
            )
            event_id = int(cur.lastrowid)

            # 派发行生成（§4.4(1)）；make_dispatches=False → 整段跳过（§5.6.2 通告类）。
            if make_dispatches and type not in _NO_DISPATCH_TYPES:
                targets = to_list if to_list else ["moderator"]
                for tgt in targets:
                    self._con.execute(
                        "INSERT INTO dispatches(event_id, target, status)"
                        " VALUES (?,?, 'pending')",
                        (event_id, tgt),
                    )
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise
        # §4.4 事务(1) 提交后：故障注入点（模拟 kill -9）。
        _fault_check("append_event_post")
        return event_id

    def pending_dispatches(self) -> list[dict]:
        """全部 status='pending' 的派发行，按 event_id 升序（§5.1）。"""
        rows = self._con.execute(
            "SELECT event_id, target, status, deadline_ts, attempts"
            " FROM dispatches WHERE status='pending'"
            " ORDER BY event_id ASC, target ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_dispatching(self, event_id: int, target: str, deadline_ts: float) -> None:
        """§4.4 事务(2)：status→dispatching + 写绝对截止时间戳。"""
        self._begin()
        try:
            self._con.execute(
                "UPDATE dispatches SET status='dispatching', deadline_ts=?"
                " WHERE event_id=? AND target=?",
                (deadline_ts, event_id, target),
            )
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise
        # §4.4 事务(2) 提交后：故障注入点。
        _fault_check("mark_dispatching_post")

    # ——————————————————————————————————————————————————————
    # §4.4 事务(5)：回复落盘 + 标 done + 会话表（单事务）
    # ——————————————————————————————————————————————————————
    def reply_and_done(
        self,
        *,
        done_event_id: int,
        done_target: str,
        reply: dict,
        session: dict | None = None,
    ) -> int:
        """单事务内：① 回复信封落盘（reply 含系统字段 from/re 已由调度层赋好）
        ② (done_event_id, done_target) status→done
        ③ 若 session 非空则 upsert sessions(role,backend,sid,last_evt,gen)。
        返回回复事件 id。
        """
        ts_val = reply.get("ts")
        ts_val = time.time() if ts_val is None else ts_val
        sender = reply.get("from")
        if sender is None:
            raise ValueError("reply 缺少系统字段 'from'（调度层须先赋值）")
        to_list = list(reply.get("to") or [])
        re_list = list(reply.get("re") or [])
        artifacts_list = list(reply.get("artifacts") or [])
        bb_ops = reply.get("blackboard_ops")
        meta_val = dict(reply.get("meta") or {})
        rtype = reply.get("type")
        if rtype is None:
            raise ValueError("reply 缺少作者字段 'type'")
        body = reply.get("body")
        if body is None:
            raise ValueError("reply 缺少作者字段 'body'")

        self._begin()
        try:
            # ① 回复落盘（+ 为其目标生成 pending 派发行，§4.4(5)）。
            cur = self._con.execute(
                "INSERT INTO events"
                "(ts, sender, to_json, type, body, re_json, corr,"
                " artifacts_json, bb_ops_json, meta_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ts_val,
                    sender,
                    json.dumps(to_list, ensure_ascii=False),
                    rtype,
                    body,
                    json.dumps(re_list, ensure_ascii=False),
                    reply.get("corr"),
                    json.dumps(artifacts_list, ensure_ascii=False),
                    None if bb_ops is None
                    else json.dumps(bb_ops, ensure_ascii=False),
                    json.dumps(meta_val, ensure_ascii=False),
                ),
            )
            reply_id = int(cur.lastrowid)

            if rtype not in _NO_DISPATCH_TYPES:
                targets = to_list if to_list else ["moderator"]
                for tgt in targets:
                    self._con.execute(
                        "INSERT INTO dispatches(event_id, target, status)"
                        " VALUES (?,?, 'pending')",
                        (reply_id, tgt),
                    )

            # ② 标 done。
            self._con.execute(
                "UPDATE dispatches SET status='done'"
                " WHERE event_id=? AND target=?",
                (done_event_id, done_target),
            )

            # ③ 会话 upsert（§7.5，更新时机在本事务内）。
            if session is not None:
                self._upsert_session_row(session)
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise
        # §4.4 事务(5) 提交后：故障注入点。
        _fault_check("reply_and_done_post")
        return reply_id

    def _upsert_session_row(self, session: dict) -> None:
        """§7.5 sessions upsert 的唯一内部实现（在调用方已开启的事务内执行，不自 commit）。

        reply_and_done 的会话 upsert 与公开 upsert_session 复用此段——同一条 SQL、同一语义，
        禁止两处各写一份（§4.3 DDL 冻结，列固定 role/backend/sid/last_evt/gen）。
        """
        self._con.execute(
            "INSERT INTO sessions(role, backend, sid, last_evt, gen)"
            " VALUES (:role, :backend, :sid, :last_evt, :gen)"
            " ON CONFLICT(role) DO UPDATE SET"
            "   backend=excluded.backend, sid=excluded.sid,"
            "   last_evt=excluded.last_evt, gen=excluded.gen",
            {
                "role": session["role"],
                "backend": session["backend"],
                "sid": session.get("sid"),
                "last_evt": int(session.get("last_evt", 0)),
                "gen": int(session.get("gen", 0)),
            },
        )

    def upsert_session(self, role: str, sid, gen: int, *,
                       backend: str | None = None,
                       last_evt: int | None = None) -> None:
        """会话簿记：单事务直写 sessions 表（§7.5），不经事件日志（§4.2）。

        sessions 表是**工作状态**而非事件真相：sid 作废、代际递增属会话簿记，不该借合成
        事件污染事件日志（§4.2 事件日志=发生过什么）。语义与 reply_and_done 内的会话 upsert
        完全一致、复用同一内部实现（_upsert_session_row），仅事务边界不同（此处自开自 commit）。

        缺省保留既有行的 backend/last_evt（作废 sid 只改 sid+gen，不该动其余列）；无既有行时
        backend 兜底空串、last_evt 兜底 0。sid 传 None = 作废。
        """
        row = self._con.execute(
            "SELECT role, backend, sid, last_evt, gen FROM sessions WHERE role=?",
            (role,),
        ).fetchone()
        prev = dict(row) if row is not None else {}
        eff_backend = backend if backend is not None else (prev.get("backend") or "")
        eff_last_evt = (last_evt if last_evt is not None
                        else int(prev.get("last_evt") or 0))
        self._begin()
        try:
            self._upsert_session_row({
                "role": role,
                "backend": eff_backend,
                "sid": sid,
                "last_evt": int(eff_last_evt),
                "gen": int(gen),
            })
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise

    # ——————————————————————————————————————————————————————
    # 派发行状态迁移辅助
    # ——————————————————————————————————————————————————————
    def mark_failed(self, event_id: int, target: str) -> None:
        self._set_status(event_id, target, "failed")

    def mark_gate_wait(self, event_id: int, target: str) -> None:  # §10
        self._set_status(event_id, target, "gate_wait")

    def mark_done(self, event_id: int, target: str) -> None:
        """通用标 done（契约 §8 缺口⑤）：把任一派发行状态置 done。"""
        self._set_status(event_id, target, "done")

    def set_pending(self, event_id: int, target: str) -> None:  # §9.1(c)
        self._set_status(event_id, target, "pending")

    def _set_status(self, event_id: int, target: str, status: str) -> None:
        self._begin()
        try:
            self._con.execute(
                "UPDATE dispatches SET status=? WHERE event_id=? AND target=?",
                (status, event_id, target),
            )
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise

    def reset_attempts(self, event_id: int, target: str) -> None:
        """§5.6.2 换绑重派：该派发行 attempts 归零（新后端享有完整重试预算）。

        M5 契约 §5 新增原语。既有 bump_attempt 只增不减、set_pending 只改 status，
        故"归零"无等价原语可复用。单事务直写，不触碰 status/deadline_ts。
        """
        self._begin()
        try:
            self._con.execute(
                "UPDATE dispatches SET attempts = 0"
                " WHERE event_id=? AND target=?",
                (event_id, target),
            )
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise

    def bump_attempt(self, event_id: int, target: str) -> int:
        """看门狗 attempts+1，返回新值（§5.3/§9.1 b）。"""
        self._begin()
        try:
            self._con.execute(
                "UPDATE dispatches SET attempts = attempts + 1"
                " WHERE event_id=? AND target=?",
                (event_id, target),
            )
            row = self._con.execute(
                "SELECT attempts FROM dispatches WHERE event_id=? AND target=?",
                (event_id, target),
            ).fetchone()
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise
        if row is None:
            raise KeyError(f"dispatch ({event_id},{target}) 不存在")
        return int(row["attempts"])

    # ——————————————————————————————————————————————————————
    # 读取：events
    # ——————————————————————————————————————————————————————
    def events(self) -> list[dict]:
        """全部事件，id 升序，dict 形态（键用协议名，含 "from" 由 sender 映射，§0.1）。"""
        rows = self._con.execute(
            "SELECT id, ts, sender, to_json, type, body, re_json, corr,"
            " artifacts_json, bb_ops_json, meta_json"
            " FROM events ORDER BY id ASC"
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> dict:
        bb_raw = r["bb_ops_json"]
        return {
            "id": int(r["id"]),
            "ts": r["ts"],
            "from": r["sender"],           # sender 列 → 协议名 "from"
            "to": json.loads(r["to_json"]),
            "type": r["type"],
            "body": r["body"],
            "re": json.loads(r["re_json"]),
            "corr": r["corr"],
            "artifacts": json.loads(r["artifacts_json"]),
            "blackboard_ops": None if bb_raw is None else json.loads(bb_raw),
            "meta": json.loads(r["meta_json"]),
        }

    # ——————————————————————————————————————————————————————
    # thread_meta
    # ——————————————————————————————————————————————————————
    def get_meta(self, key: str) -> str | None:
        row = self._con.execute(
            "SELECT value FROM thread_meta WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self._begin()
        try:
            self._con.execute(
                "INSERT INTO thread_meta(key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise

    # ——————————————————————————————————————————————————————
    # jobs（§5.2 长作业登记）
    # ——————————————————————————————————————————————————————
    def register_job(
        self,
        *,
        corr: str,
        kind: str,
        cmd: str,
        callback_to: str,
        started_evt: int,
    ) -> None:
        self._begin()
        try:
            self._con.execute(
                "INSERT INTO jobs(corr, kind, cmd, callback_to, started_evt, status)"
                " VALUES (?,?,?,?,?, 'running')"
                " ON CONFLICT(corr) DO UPDATE SET"
                "   kind=excluded.kind, cmd=excluded.cmd,"
                "   callback_to=excluded.callback_to,"
                "   started_evt=excluded.started_evt",
                (corr, kind, cmd, callback_to, started_evt),
            )
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise

    def set_job_status(self, corr: str, status: str) -> None:
        self._begin()
        try:
            self._con.execute(
                "UPDATE jobs SET status=? WHERE corr=?", (status, corr)
            )
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise

    # ——————————————————————————————————————————————————————
    # metrics（§13 采集点）
    # ——————————————————————————————————————————————————————
    def record_metric(self, key: str, value: float, extra: str = "") -> None:
        self._begin()
        try:
            self._con.execute(
                "INSERT INTO metrics(ts, key, value, extra) VALUES (?,?,?,?)",
                (time.time(), key, float(value), extra),
            )
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise

    # ——————————————————————————————————————————————————————
    # invoke 原文审计（§14：一等公民）
    # ——————————————————————————————————————————————————————
    def write_invoke_log(
        self,
        *,
        event_ids: list[int],
        role: str,
        view_text: str,
        output_text: str,
    ) -> None:
        """落 <thread_dir>/logs/ 一个文件，文件名含事件号与角色（§14）。"""
        ids_part = "-".join(str(i) for i in event_ids) if event_ids else "none"
        # 加入单调时间戳前缀，避免同一 (ids,role) 覆盖历史审计。
        fname = f"{time.time():.6f}_E{ids_part}_{role}.log"
        path = self._logs_dir / fname
        path.write_text(
            f"=== VIEW (role={role}, events={event_ids}) ===\n"
            f"{view_text}\n"
            f"=== OUTPUT ===\n"
            f"{output_text}\n",
            encoding="utf-8",
        )

    # ——————————————————————————————————————————————————————
    # 内部：黑板 state.json 读写（供模块级函数复用）
    # ——————————————————————————————————————————————————————
    def _read_state(self) -> dict:
        if not self._state_path.exists():
            return _empty_state()
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 损坏视为缺失（§9.1：缺失或损坏 → rebuild 由调用方触发）。
            return _empty_state()
        return _normalize_state(data)

    def _write_state(self, state: dict) -> None:
        self._state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _write_board(self, text: str) -> None:
        self._board_path.write_text(text, encoding="utf-8")


# ——————————————————————————————————————————————————————————————
# 黑板：决策类事件的投影（§4.6）
# ——————————————————————————————————————————————————————————————

def _empty_state() -> dict:
    return {"contracts": {}, "decisions": [], "tasks": {}}


def _normalize_state(data: dict) -> dict:
    """把任意来源 state 收敛到规范结构，容忍缺键。"""
    return {
        "contracts": dict(data.get("contracts") or {}),
        "decisions": list(data.get("decisions") or []),
        "tasks": dict(data.get("tasks") or {}),
    }


def _apply_ops_into(state: dict, ops: list[dict], source_event_id: int) -> None:
    """把一批 ops 就地作用到 state（§3.3 三种 op）。"""
    for op in ops or []:
        kind = op.get("op")
        if kind == "freeze_contract":
            state["contracts"][op["name"]] = {
                "version": op.get("version"),
                "path": op.get("path"),
                "frozen_at": source_event_id,
            }
        elif kind == "set_decision":
            state["decisions"].append({"evt": source_event_id, "text": op.get("text")})
        elif kind == "set_task":
            state["tasks"][op["key"]] = op.get("status")
        # 未知 op：忽略（schema 已在协议层限定 op 枚举，此处防御）。


def _render_board(state: dict) -> str:
    """board.md 渲染（格式属 spec §17，本实现自定，见 IMPLEMENTATION_NOTES / 最终回复）。

    结构：三段固定标题（契约 / 决策 / 任务），确定性排序，便于人读与快照。
    """
    lines: list[str] = ["# 黑板 (board.md)", ""]

    lines.append("## 冻结契约")
    contracts = state.get("contracts") or {}
    if contracts:
        for name in sorted(contracts):
            c = contracts[name] or {}
            lines.append(
                f"- {name} v{c.get('version')} @ {c.get('path')}"
                f" (frozen_at E{c.get('frozen_at')})"
            )
    else:
        lines.append("- （无）")
    lines.append("")

    lines.append("## 已定决策")
    decisions = state.get("decisions") or []
    if decisions:
        for d in decisions:
            lines.append(f"- [E{d.get('evt')}] {d.get('text')}")
    else:
        lines.append("- （无）")
    lines.append("")

    lines.append("## 任务状态")
    tasks = state.get("tasks") or {}
    if tasks:
        for key in sorted(tasks):
            lines.append(f"- {key}: {tasks[key]}")
    else:
        lines.append("- （无）")
    lines.append("")

    return "\n".join(lines)


def apply_blackboard_ops(store: Store, ops: list[dict], source_event_id: int) -> None:
    """按 §3.3 三种 op 更新 blackboard/state.json，随后重渲染 board.md。

    调用方须已判过 can_apply_blackboard_ops 门槛（本函数不再判权限，契约 §2）。
    """
    state = store._read_state()
    _apply_ops_into(state, ops, source_event_id)
    store._write_state(state)
    store._write_board(_render_board(state))


def rebuild_blackboard(store: Store) -> None:
    """清空 state 后按 id 升序重放全部满足 §3.3 门槛的 A 类事件的 bb_ops。

    结果必须与增量维护逐字段一致（§4.6）。恢复时黑板文件缺失/损坏即调用它（§9.1）。

    门槛（type 层）：仅 type ∈ {decision, acceptance, gate_decision} 的事件的 bb_ops
    才被重放；report 等非 A 类即便携带 bb_ops 也不采纳（见 test_rebuild_only_replays…）。
    can_decide 门槛：写入黑板发生在调度层已判权限之后（apply_blackboard_ops 不判权限），
    落盘的这些 A 类事件即视为"已通过门槛"，故此处按 type 重放即与增量一致。
    """
    state = _empty_state()
    for ev in store.events():
        if ev["type"] not in _BB_OP_TYPES:
            continue
        ops = ev.get("blackboard_ops")
        if not ops:
            continue
        _apply_ops_into(state, ops, ev["id"])
    store._write_state(state)
    store._write_board(_render_board(state))


def board_state(store: Store) -> dict:
    """读 state.json 供断言（§4.6）。"""
    return store._read_state()
