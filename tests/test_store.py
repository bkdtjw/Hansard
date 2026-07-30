"""存储层验收测试（spec §4）。

覆盖任务卡条目 (c)：
  - §4.3 DDL：建表齐全（events/dispatches/sessions/jobs/thread_meta/metrics）。
  - §4.4(1) append_event：id 自增；为每个 to 目标生成 pending 派发行；
    to 空 → 生成 target='moderator' 派发行；terminate 型**不生成**派发行。
  - §4.4(5) reply_and_done：回复落盘 + 标 done + 会话 upsert 单事务。
  - §4.6 黑板 apply/rebuild：增量维护与重放重建**逐字段一致**。

硬约束：顶层只 import orch.store；符号在函数体内引用。断言仅依赖契约 §2 公开签名。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import orch.store  # 包级导入


# ——————————————————————————————————————————————————————————————
# §4.1 目录布局 + §4.3 DDL
# ——————————————————————————————————————————————————————————————

def _new_store(thread_dir: Path):
    return orch.store.Store(thread_dir)


def test_store_creates_thread_layout(thread_dir):
    _new_store(thread_dir)
    assert (thread_dir / "events.db").exists()
    assert (thread_dir / "blackboard").is_dir()
    assert (thread_dir / "logs").is_dir()


def test_ddl_creates_all_tables(thread_dir):
    _new_store(thread_dir)
    con = sqlite3.connect(str(thread_dir / "events.db"))
    try:
        names = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        con.close()
    for t in ["events", "dispatches", "sessions", "jobs", "thread_meta", "metrics"]:
        assert t in names, f"缺表 {t}"


def test_events_table_uses_sender_column_not_from(thread_dir):
    # §4.3：from 是关键字，列名用 sender。
    _new_store(thread_dir)
    con = sqlite3.connect(str(thread_dir / "events.db"))
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(events)").fetchall()}
    finally:
        con.close()
    assert "sender" in cols
    assert "from" not in cols
    assert "id" in cols and "to_json" in cols and "re_json" in cols and "bb_ops_json" in cols


def test_dispatches_status_check_constraint(thread_dir):
    # §4.3：status CHECK IN (pending,dispatching,done,gate_wait,failed)。
    st = _new_store(thread_dir)
    con = sqlite3.connect(str(thread_dir / "events.db"))
    try:
        # 直接插入非法 status 应被 CHECK 拒绝。
        raised = False
        try:
            con.execute(
                "INSERT INTO dispatches(event_id,target,status) VALUES (1,'x','bogus')"
            )
            con.commit()
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "dispatches.status 的 CHECK 约束缺失"
    finally:
        con.close()


# ——————————————————————————————————————————————————————————————
# §4.4(1) append_event：id 自增 + 派发行生成
# ——————————————————————————————————————————————————————————————

def test_append_event_returns_autoincrement_id(thread_dir):
    st = _new_store(thread_dir)
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["moderator"])
    e2 = st.append_event(sender="moderator", type="assign", body="派给 pm", to=["pm"])
    assert isinstance(e1, int) and isinstance(e2, int)
    assert e2 == e1 + 1


def test_append_event_creates_pending_dispatch_per_target(thread_dir):
    st = _new_store(thread_dir)
    eid = st.append_event(sender="pm", type="review", body="评审", to=["backend", "frontend"])
    pend = st.pending_dispatches()
    targets = sorted(d["target"] for d in pend if d["event_id"] == eid)
    assert targets == ["backend", "frontend"]
    for d in pend:
        if d["event_id"] == eid:
            assert d["status"] == "pending"


def test_append_event_empty_to_falls_back_to_moderator(thread_dir):
    # §4.4(1) + §5.2 兜底：to 为空 → 生成 target='moderator' 派发行。
    st = _new_store(thread_dir)
    eid = st.append_event(sender="human", type="assign", body="帖子支持点赞", to=[])
    pend = st.pending_dispatches()
    targets = [d["target"] for d in pend if d["event_id"] == eid]
    assert targets == ["moderator"]


def test_append_event_to_none_falls_back_to_moderator(thread_dir):
    st = _new_store(thread_dir)
    eid = st.append_event(sender="human", type="assign", body="帖子支持点赞")  # to 省略
    pend = st.pending_dispatches()
    targets = [d["target"] for d in pend if d["event_id"] == eid]
    assert targets == ["moderator"]


def test_terminate_event_creates_no_dispatch(thread_dir):
    # §5.4：terminate 是信号非待办，不生成派发行。
    st = _new_store(thread_dir)
    eid = st.append_event(sender="moderator", type="terminate", body="终止", to=[])
    pend = st.pending_dispatches()
    assert all(d["event_id"] != eid for d in pend), "terminate 不应生成任何派发行"


def test_pending_dispatches_sorted_by_event_id(thread_dir):
    # §5.1：pending 按 event_id 升序。
    st = _new_store(thread_dir)
    a = st.append_event(sender="human", type="assign", body="a", to=["backend"])
    b = st.append_event(sender="human", type="assign", body="b", to=["frontend"])
    c = st.append_event(sender="human", type="assign", body="c", to=["tester"])
    ids = [d["event_id"] for d in st.pending_dispatches()]
    assert ids == sorted(ids)
    assert set(ids) >= {a, b, c}


def test_events_roundtrip_preserves_author_fields(thread_dir):
    st = _new_store(thread_dir)
    eid = st.append_event(
        sender="pm", type="review", body="PRD",
        to=["backend"], artifacts=["docs/prd.md"], corr="c1",
    )
    evs = {e["id"]: e for e in st.events()}
    e = evs[eid]
    # 契约 §0.1：dict 键名用协议名，from ↔ sender 由存储层映射，dict 里键保持 "from"。
    assert e["from"] == "pm"
    assert e["type"] == "review"
    assert e["body"] == "PRD"
    assert e["to"] == ["backend"]
    assert e["artifacts"] == ["docs/prd.md"]
    assert e["corr"] == "c1"


# ——————————————————————————————————————————————————————————————
# §4.4 事务(2)：mark_dispatching
# ——————————————————————————————————————————————————————————————

def test_mark_dispatching_sets_status_and_deadline(thread_dir):
    st = _new_store(thread_dir)
    eid = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    st.mark_dispatching(eid, "backend", deadline_ts=12345.0)
    # 转 dispatching 后不再出现在 pending 列表。
    assert all(d["event_id"] != eid for d in st.pending_dispatches())


# ——————————————————————————————————————————————————————————————
# §4.4(5) reply_and_done 单事务
# ——————————————————————————————————————————————————————————————

def test_reply_and_done_single_transaction(thread_dir):
    st = _new_store(thread_dir)
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["moderator"])
    st.mark_dispatching(e1, "moderator", deadline_ts=99.0)
    reply = {
        "from": "moderator", "re": [e1], "to": ["pm"],
        "type": "assign", "body": "派给 pm",
    }
    rid = st.reply_and_done(
        done_event_id=e1, done_target="moderator", reply=reply,
        session={"role": "moderator", "backend": "mock", "sid": "s1",
                 "last_evt": e1, "gen": 1},
    )
    # ① 回复事件已落盘（id 自增，返回其 id）。
    assert isinstance(rid, int) and rid > e1
    evs = {e["id"]: e for e in st.events()}
    assert rid in evs
    assert evs[rid]["from"] == "moderator"
    assert evs[rid]["re"] == [e1]
    assert evs[rid]["to"] == ["pm"]
    # ② (e1, moderator) 标 done：不再 pending。
    assert all(d["event_id"] != e1 for d in st.pending_dispatches())
    # ③ 回复事件为其目标 pm 生成 pending 派发行（§4.4(5) 落盘顺序）。
    assert any(d["event_id"] == rid and d["target"] == "pm"
               for d in st.pending_dispatches())


def test_reply_and_done_without_session(thread_dir):
    # session=None 也应成功（API/mock 无会话时）。
    st = _new_store(thread_dir)
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    st.mark_dispatching(e1, "backend", deadline_ts=1.0)
    reply = {"from": "backend", "re": [e1], "to": ["tester"], "type": "handoff", "body": "交接"}
    rid = st.reply_and_done(done_event_id=e1, done_target="backend", reply=reply, session=None)
    assert isinstance(rid, int)


# ——————————————————————————————————————————————————————————————
# R-T5 发现2：upsert_session 会话簿记直写（§7.5，不经事件日志 §4.2）
# ——————————————————————————————————————————————————————————————

def _read_session(thread_dir: Path, role: str) -> dict | None:
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        con.row_factory = sqlite3.Row
        r = con.execute(
            "SELECT role, backend, sid, last_evt, gen FROM sessions WHERE role=?",
            (role,),
        ).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _count_events(thread_dir: Path) -> int:
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        return int(con.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    finally:
        con.close()


def test_upsert_session_direct_write_no_events(thread_dir):
    """upsert_session 单事务直写 sessions 表：既有行 sid 作废 + gen 递增，事件日志零新增。

    语义须与 reply_and_done 内的会话 upsert 一致（复用同一内部实现）；作废 sid 属会话簿记
    （§7.5 工作状态），不该向事件日志（§4.2 发生过什么）注入任何事件——events 行数不变。
    """
    st = _new_store(thread_dir)
    # 先经 reply_and_done 建一条 backend 会话行（sid 非空、gen=1、last_evt=e1、backend=cli）。
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["backend"])
    st.mark_dispatching(e1, "backend", deadline_ts=1.0)
    st.reply_and_done(
        done_event_id=e1, done_target="backend",
        reply={"from": "backend", "re": [e1], "to": ["human"],
               "type": "report", "body": "r1"},
        session={"role": "backend", "backend": "cli", "sid": "sid-A",
                 "last_evt": e1, "gen": 1},
    )
    before = _read_session(thread_dir, "backend")
    assert before["sid"] == "sid-A" and int(before["gen"]) == 1
    events_n0 = _count_events(thread_dir)

    # —— 作废 sid：sid=None、gen 递增；backend/last_evt 缺省保留既有行 ——
    st.upsert_session(role="backend", sid=None, gen=int(before["gen"]) + 1)

    after = _read_session(thread_dir, "backend")
    assert after["sid"] is None, "sid 应被置空（作废）"
    assert int(after["gen"]) == int(before["gen"]) + 1, "gen 应递增"
    # 未传的列保留既有值（作废只改 sid+gen，不动 backend/last_evt）。
    assert after["backend"] == before["backend"], "backend 缺省保留既有行"
    assert int(after["last_evt"]) == int(before["last_evt"]), "last_evt 缺省保留既有行"
    # 会话簿记不经事件日志：events 行数不变。
    assert _count_events(thread_dir) == events_n0, "upsert_session 不得新增任何事件"


def test_upsert_session_inserts_new_role_row(thread_dir):
    """无既有行时 upsert_session 插入新 sessions 行（backend 兜底空串、last_evt 兜底 0）。"""
    st = _new_store(thread_dir)
    n0 = _count_events(thread_dir)
    st.upsert_session(role="tester", sid="sid-T", gen=0, backend="cli", last_evt=5)
    row = _read_session(thread_dir, "tester")
    assert row is not None
    assert row["sid"] == "sid-T"
    assert row["backend"] == "cli"
    assert int(row["last_evt"]) == 5
    assert int(row["gen"]) == 0
    assert _count_events(thread_dir) == n0, "插入会话行同样不经事件日志"


# ——————————————————————————————————————————————————————————————
# §4.4 派发行状态迁移辅助
# ——————————————————————————————————————————————————————————————

def test_set_pending_requeues(thread_dir):
    st = _new_store(thread_dir)
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    st.mark_dispatching(e1, "backend", deadline_ts=1.0)
    assert all(d["event_id"] != e1 for d in st.pending_dispatches())
    st.set_pending(e1, "backend")
    assert any(d["event_id"] == e1 and d["target"] == "backend"
               for d in st.pending_dispatches())


def test_bump_attempt_increments(thread_dir):
    st = _new_store(thread_dir)
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    st.mark_dispatching(e1, "backend", deadline_ts=1.0)
    n1 = st.bump_attempt(e1, "backend")
    n2 = st.bump_attempt(e1, "backend")
    assert n2 == n1 + 1


def test_mark_gate_wait_removes_from_pending(thread_dir):
    st = _new_store(thread_dir)
    e1 = st.append_event(sender="moderator", type="gate_request", body="批准?", to=["human"])
    st.mark_gate_wait(e1, "human")
    assert all(d["event_id"] != e1 for d in st.pending_dispatches())


# ——————————————————————————————————————————————————————————————
# thread_meta / jobs / metrics / invoke 审计落点
# ——————————————————————————————————————————————————————————————

def test_thread_meta_get_set(thread_dir):
    st = _new_store(thread_dir)
    assert st.get_meta("status") in (None, "running")
    st.set_meta("status", "suspended")
    assert st.get_meta("status") == "suspended"


def test_register_job_and_status(thread_dir):
    st = _new_store(thread_dir)
    e1 = st.append_event(sender="human", type="assign", body="ci", to=["moderator"])
    st.register_job(corr="job-01", kind="ci", cmd="pytest -q",
                    callback_to="moderator", started_evt=e1)
    st.set_job_status("job-01", "done")  # 不应抛异常


def test_record_metric_writes_row(thread_dir):
    st = _new_store(thread_dir)
    st.record_metric("batch_size", 2.0, extra="E4,E5")
    con = sqlite3.connect(str(thread_dir / "events.db"))
    try:
        n = con.execute("SELECT COUNT(*) FROM metrics WHERE key='batch_size'").fetchone()[0]
    finally:
        con.close()
    assert n >= 1


def test_write_invoke_log_creates_file_with_event_and_role(thread_dir):
    # §14：invoke 原文审计一等公民，文件名含事件号与角色。
    st = _new_store(thread_dir)
    st.write_invoke_log(event_ids=[4, 5], role="pm",
                        view_text="VIEW", output_text="OUT")
    logs = list((thread_dir / "logs").iterdir())
    assert logs, "logs/ 下应有审计文件"
    joined = " ".join(p.name for p in logs)
    assert "pm" in joined


# ——————————————————————————————————————————————————————————————
# §4.6 黑板：apply / rebuild / board_state
# ——————————————————————————————————————————————————————————————

def _freeze(name, path, version):
    return {"op": "freeze_contract", "name": name, "path": path, "version": version}


def test_apply_blackboard_ops_freeze_contract(thread_dir):
    st = _new_store(thread_dir)
    e = st.append_event(sender="pm", type="decision", body="冻结", to=["moderator"],
                        blackboard_ops=[_freeze("like-api", "docs/like-api.md", 1)])
    orch.store.apply_blackboard_ops(st, [_freeze("like-api", "docs/like-api.md", 1)], e)
    state = orch.store.board_state(st)
    assert state["contracts"]["like-api"]["version"] == 1
    assert state["contracts"]["like-api"]["path"] == "docs/like-api.md"


def test_apply_blackboard_ops_set_decision_and_task(thread_dir):
    st = _new_store(thread_dir)
    e = st.append_event(sender="pm", type="decision", body="d", to=["moderator"],
                        blackboard_ops=[
                            {"op": "set_decision", "text": "幂等语义"},
                            {"op": "set_task", "key": "backend.impl", "status": "done"},
                        ])
    orch.store.apply_blackboard_ops(st, [
        {"op": "set_decision", "text": "幂等语义"},
        {"op": "set_task", "key": "backend.impl", "status": "done"},
    ], e)
    state = orch.store.board_state(st)
    assert any(d["text"] == "幂等语义" for d in state["decisions"])
    assert state["tasks"]["backend.impl"] == "done"


def test_freeze_contract_version_upgrade_overwrites(thread_dir):
    st = _new_store(thread_dir)
    e1 = st.append_event(sender="pm", type="decision", body="v1", to=["moderator"],
                         blackboard_ops=[_freeze("like-api", "docs/like-api.md", 1)])
    orch.store.apply_blackboard_ops(st, [_freeze("like-api", "docs/like-api.md", 1)], e1)
    e2 = st.append_event(sender="pm", type="decision", body="v2", to=["moderator"],
                         blackboard_ops=[_freeze("like-api", "docs/like-api.md", 2)])
    orch.store.apply_blackboard_ops(st, [_freeze("like-api", "docs/like-api.md", 2)], e2)
    state = orch.store.board_state(st)
    assert state["contracts"]["like-api"]["version"] == 2


def test_rebuild_blackboard_matches_incremental_field_by_field(thread_dir):
    """§4.6 核心不变量：增量维护结果 == 重放重建结果，逐字段一致。

    落一串 A 类决策事件并增量 apply，快照 state；再 rebuild，比对整份 state。
    """
    st = _new_store(thread_dir)

    ops_seq = [
        [_freeze("like-api", "docs/like-api.md", 1)],
        [{"op": "set_decision", "text": "重复点赞=取消赞（幂等）"}],
        [_freeze("like-api", "docs/like-api.md", 2),
         {"op": "set_task", "key": "backend.impl", "status": "done"}],
        [{"op": "set_task", "key": "frontend.impl", "status": "done"}],
    ]
    for ops in ops_seq:
        eid = st.append_event(sender="pm", type="decision", body="A类", to=["moderator"],
                              blackboard_ops=ops)
        orch.store.apply_blackboard_ops(st, ops, eid)

    incremental = orch.store.board_state(st)

    orch.store.rebuild_blackboard(st)
    rebuilt = orch.store.board_state(st)

    assert rebuilt == incremental, "rebuild 结果必须与增量维护逐字段一致（§4.6）"
    # 附加：终态字段核对（附录B 终态锚点）。
    assert rebuilt["contracts"]["like-api"]["version"] == 2
    assert rebuilt["tasks"]["backend.impl"] == "done"
    assert rebuilt["tasks"]["frontend.impl"] == "done"


def test_rebuild_only_replays_qualified_A_class(thread_dir):
    """§3.3 门槛：rebuild 只重放"满足门槛的 A 类事件"的 bb_ops。

    非决策类事件（如 report）即便携带 bb_ops，rebuild 也不得投影进黑板。
    """
    st = _new_store(thread_dir)
    # 合法 A 类：decision 冻结 v1。
    e1 = st.append_event(sender="pm", type="decision", body="ok", to=["moderator"],
                         blackboard_ops=[_freeze("like-api", "docs/like-api.md", 1)])
    orch.store.apply_blackboard_ops(st, [_freeze("like-api", "docs/like-api.md", 1)], e1)
    # 非 A 类：report 携带 bb_ops（不该被门槛放行 → rebuild 不得采纳）。
    st.append_event(sender="backend", type="report", body="偷改", to=["moderator"],
                    blackboard_ops=[_freeze("like-api", "docs/like-api.md", 99)])

    orch.store.rebuild_blackboard(st)
    state = orch.store.board_state(st)
    assert state["contracts"]["like-api"]["version"] == 1, \
        "report 型的 bb_ops 不满足 §3.3 门槛，rebuild 不得采纳"


# ——————————————————————————————————————————————————————————————
# board_state_checked（M5 后追加的只读原语）：区分"没有"与"坏了"
#
# 存在理由：宽松读取（board_state / Store._read_state）把损坏降级成空结构 ——
# 恢复路径要的正是这份宽松（§9.1 缺失或损坏同解为 rebuild），但**展示**路径不
# 重建、只渲染，拿空结构直出就等于替 store 编一个"黑板本来是空的"。
# 本节同时钉住"宽松那份没被改坏"。
# ——————————————————————————————————————————————————————————————

def _state_file(thread_dir: Path) -> Path:
    return thread_dir / "blackboard" / "state.json"


def test_board_state_checked_missing_file_is_not_an_error(thread_dir):
    """文件不存在 = 还没写过黑板：空结构 + 无错误（与 board_state 逐字一致）。"""
    st = _new_store(thread_dir)
    assert not _state_file(thread_dir).exists()

    state, err = orch.store.board_state_checked(st)

    assert err is None
    assert state == {"contracts": {}, "decisions": [], "tasks": {}}
    assert state == orch.store.board_state(st)


def test_board_state_checked_valid_file_matches_lenient_reader(thread_dir):
    """合法状态：第一个返回值与 board_state 逐字段一致，第二个是 None。"""
    st = _new_store(thread_dir)
    e = st.append_event(sender="pm", type="decision", body="d", to=["moderator"],
                        blackboard_ops=[_freeze("like-api", "docs/like-api.md", 3)])
    orch.store.apply_blackboard_ops(st, [_freeze("like-api", "docs/like-api.md", 3)], e)

    state, err = orch.store.board_state_checked(st)

    assert err is None
    assert state == orch.store.board_state(st)
    assert state["contracts"]["like-api"]["version"] == 3


def test_board_state_checked_reports_truncated_file_while_lenient_stays_lenient(thread_dir):
    """截半的 state.json：checked 版给人话；board_state **仍**降级成空结构不抛。

    后半句是本卡的硬约束（调度/恢复路径依赖宽松语义，见 tests/test_scheduler.py
    的"写坏 state.json 后仍要能读"），一旦有人把宽松读取改严，这条当场红。
    """
    st = _new_store(thread_dir)
    e = st.append_event(sender="pm", type="decision", body="d", to=["moderator"],
                        blackboard_ops=[_freeze("like-api", "docs/like-api.md", 1)])
    orch.store.apply_blackboard_ops(st, [_freeze("like-api", "docs/like-api.md", 1)], e)
    p = _state_file(thread_dir)
    good = p.read_text(encoding="utf-8")
    p.write_text(good[: len(good) // 2], encoding="utf-8")

    state, err = orch.store.board_state_checked(st)

    assert isinstance(err, str) and err.strip(), "损坏必须说出来"
    assert "state.json" in err and "orch run" in err, err
    assert "\n" not in err, f"人话须压成一行：{err!r}"
    assert state == {"contracts": {}, "decisions": [], "tasks": {}}, state
    # 宽松那份行为不变：同一份坏文件照旧空结构、不抛、不报错。
    assert orch.store.board_state(st) == {"contracts": {}, "decisions": [], "tasks": {}}


def test_board_state_checked_reports_non_object_top_level(thread_dir):
    """顶层写成数组：解析得动但不是状态 —— 同样报错（归一化拿它会当场炸）。"""
    st = _new_store(thread_dir)
    _state_file(thread_dir).write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    state, err = orch.store.board_state_checked(st)

    assert isinstance(err, str) and err.strip()
    assert "state.json" in err, err
    assert state == {"contracts": {}, "decisions": [], "tasks": {}}
