"""用户界面 CLI（spec §12 子集，M2-T4 + M3-T4）。

typer 单文件实现，命令与 spec §12 表一致：
  orch run [--once]                     启动调度进程（常驻；--once 一次循环退出）
  orch new "任务" --roles r1,r2,...     建线程：目录 / db + E1 入队
  orch send t-xxx "..." [--to role]     人类发言入队
  orch chat t-xxx [--follow]            事件日志渲染成群聊
  orch status t-xxx                     线程 status + 派发行摘要
  orch approve|reject <corr>            门禁裁决（调 scheduler.apply_gate_decision）
  orch stop                             优雅停机（写 workspace 级 orch.stop 标志）
  orch reopen t-xxx                     线程 status → running
  orch attach t-xxx role                打印该角色原生会话接入命令（sid=None 兜底）
  orch threads                          列 workspace 下所有 t-xxx 线程
  orch bench resume --fixture X --runs N   开/关 resume 的 tokens_in 对比实验（§12/§13，M3-T4）

【M2 骨架边界】
  - worktrees / 权限 三件套由 T3 落地；本层新建 workspace/t-xxx/{events.db, blackboard, logs}。
  - --follow 用轮询而非 tail -f（本层为 M2 骨架，M3 完善）。
  - `orch run` 骨架：默认装配 Fake adapters（陪跑接入真实 CLI/API 属 §17 开放决策），
    只做常驻循环骨架 + orch.stop 消费。真实 CLI/API 由 config 覆盖（M3 完善）。
  - 各命令的具体 flag 措辞取自 spec §12 表 + M2 契约 §5；与真实 CLI 的 flag 联跑差异属 §17
    开放决策（升级 QUESTIONS.md）。

【M3-T4 边界（本卡新增）】
  - `orch bench resume`：不启真子进程，只跑内部 render_view + estimate_tokens 估算
    （M3 契约 §5：bench resume 用 pytest fixture 生成简化任务而非附录B）。
  - "关 resume"（冷启动）路径：每轮都对完整事件流跑一次 orch.render.render_view
    （cold_start=True），tokens_in = 该轮视图的 token_est。
  - "开 resume"（热续）路径：本卡只写 CLI 层，不改 orch.render（其 render_delta 由
    T2 另卡实现，不在本卡可写路径内）。本层用 render_view 已冻结的公开产物
    自行近似热续增量：首轮仍是全量冷启动，之后各轮只对"上一轮之后新增的事件"
    重新走 render_view 并只统计新增焦点段 + 指令尾（黑板层固定段沿用首轮估算，
    不重复计入），从而得到一个不依赖未落地 render_delta 符号、且与其真实契约方向一致
    （新事件全文 + 黑板 diff + 指令尾必发）的 tokens_in 近似值，供 bench 相对对比使用。
    真实精确的 render_delta 落地后，bench 可直接切换调用（记 IMPLEMENTATION_NOTES.md）。
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from pathlib import Path

import typer

import orch.store
import orch.scheduler
import orch.render


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="orch —— 异构多智能体编排系统 CLI（spec §12 子集）。",
)

bench_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="orch bench —— 基准对比实验（spec §12/§13）。",
)
app.add_typer(bench_app, name="bench")


# ——————————————————————————————————————————————————————————————
# 内部工具
# ——————————————————————————————————————————————————————————————

def _load_config(workspace: Path) -> dict:
    """读取 workspace 下 config.yaml（若无则返回空 dict，M2 骨架宽松）。"""
    cfg_path = workspace / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml  # pyyaml 已在 spec §14 白名单
        with cfg_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return dict(data) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _resolve_workspace(workspace: str | None) -> Path:
    """把 --workspace 转为 Path；未提供则用当前目录。"""
    if workspace:
        return Path(workspace).resolve()
    return Path.cwd().resolve()


def _open_thread_store(workspace: Path, thread_id: str) -> "orch.store.Store":
    """打开某 workspace 下的一个线程 store。目录不存在则由 Store 建。"""
    tdir = workspace / thread_id
    return orch.store.Store(tdir)


def _new_thread_id() -> str:
    """生成一个 `t-xxxxxxxx` 形态的线程 id（uuid 前 8 位）。"""
    return "t-" + uuid.uuid4().hex[:8]


def _find_thread_dirs(workspace: Path) -> list[Path]:
    """workspace 下所有 `t-xxx` 子目录。"""
    if not workspace.exists():
        return []
    return sorted(
        p for p in workspace.iterdir()
        if p.is_dir() and p.name.startswith("t-")
    )


def _echo(msg: str) -> None:
    typer.echo(msg)


# ——————————————————————————————————————————————————————————————
# orch run（spec §12：启动调度进程；C-2 修复）
# ——————————————————————————————————————————————————————————————

def _thread_roles(store: "orch.store.Store") -> list[str]:
    """从 thread_meta.roles（JSON 字符串，由 orch new 写入）读回角色列表。"""
    raw = store.get_meta("roles")
    if not raw:
        return []
    try:
        rs = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(r) for r in rs if isinstance(r, str)]


def _build_default_adapters(roles: list[str]) -> dict:
    """M2 骨架默认适配器装配（陪跑事项属 §17；此处为 orch run 命令的默认 Fake 骨架）。

    策略（有意保守，仅用于 --once 与冒烟）：
      · 非 moderator 角色 → FakeApiAdapter 回一条 chat/report/handoff 型，目的地 moderator，
        让编排环有推进但不循环无限；
      · moderator → FakeApiAdapter 首次回 terminate（moderator 允许 terminate，§3.2），
        使 run_thread 在一轮内自然结束。

    真实 CLI/API 装配（read start_cmd/api_key/tools 等）属 M3 完善 + 陪跑联跑，
    本函数只提供最小可运行骨架，测试与冒烟走 Fake（M2 契约 §6 明列）。
    """
    from orch.adapters import FakeApiAdapter

    adapters: dict = {}
    for role in roles:
        if role == "moderator":
            adapters[role] = FakeApiAdapter(
                role=role,
                config={"kind": "api"},
                scripted_reply={
                    "type": "terminate", "to": [], "body": "run --once done",
                },
            )
        else:
            adapters[role] = FakeApiAdapter(
                role=role,
                config={"kind": "api"},
                scripted_reply={
                    "type": "chat", "to": ["moderator"],
                    "body": f"run-once ack from {role}",
                },
            )
    # 兜底：moderator 若不在角色列表里，也补一个（append_event to 为空默认落 moderator）。
    if "moderator" not in adapters:
        adapters["moderator"] = FakeApiAdapter(
            role="moderator",
            config={"kind": "api"},
            scripted_reply={
                "type": "terminate", "to": [], "body": "run --once done",
            },
        )
    return adapters


def _stop_marker_path(workspace: Path) -> Path:
    return workspace / "orch.stop"


def _consume_stop_marker(workspace: Path) -> bool:
    """若 workspace/orch.stop 存在则删除并返回 True；否则返回 False。

    §12 语义："优雅停机" —— stop 是一次性信号，run 见到即退，并**消费**该标志
    避免下次 run 被历史标志误触发（C-2 修复：现在有人读了）。
    """
    marker = _stop_marker_path(workspace)
    if marker.exists():
        try:
            marker.unlink()
        except OSError:
            # 极端并发下若删不掉：也算成功检测到 stop 信号，run 仍立即退出。
            pass
        return True
    return False


@app.command("run")
def cmd_run(
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录；缺省为当前目录。",
    ),
    once: bool = typer.Option(
        False, "--once", help="只跑一轮（用于测试/冒烟），不进入常驻循环。",
    ),
    interval: float = typer.Option(
        1.0, "--interval",
        help="常驻模式下每轮之间的休眠秒数（--once 时忽略）。",
    ),
) -> None:
    """§12 orch run：启动调度进程（常驻，可随时 kill）。

    骨架循环（--once 只跑一轮）：
      1) 检测 workspace/orch.stop 标志：存在则**消费**（删除）并立即退出（C-2）。
      2) 遍历 workspace 下所有 `t-*` 线程：
         · status ∈ {suspended, terminated} → 跳过；
         · 其余：装配默认 Fake adapters（M2 骨架）→ run_thread 一次。
      3) --once：一轮结束返回；否则休眠 interval 秒后回步骤 1。
    """
    ws = _resolve_workspace(workspace)
    ws.mkdir(parents=True, exist_ok=True)

    while True:
        # 步骤 1：orch.stop 标志优先 —— 消费并立即退出（C-2）。
        if _consume_stop_marker(ws):
            _echo(f"[run] detected orch.stop, exiting (consumed marker)")
            return

        # 步骤 2：遍历线程逐一推进一次 run_thread。
        thread_dirs = _find_thread_dirs(ws)
        for tdir in thread_dirs:
            store = orch.store.Store(tdir)
            status = store.get_meta("status")
            if status in ("suspended", "terminated"):
                continue
            roles = _thread_roles(store)
            if not roles:
                # 无角色配置 → 无从装配 adapters；跳过（run 骨架不臆造角色）。
                continue
            adapters = _build_default_adapters(roles)
            try:
                orch.scheduler.run_thread(store, _load_config(ws), adapters)
            except Exception as exc:  # noqa: BLE001 骨架层兜底：不因单个线程崩溃拖垮 run
                _echo(f"[run] thread {tdir.name} error: {exc!r}")

        # 步骤 3：--once 结束；常驻模式睡一觉再回步骤 1（同时检查 stop 标志）。
        if once:
            return
        try:  # pragma: no cover - 常驻模式手动 Ctrl-C 走出，测试不覆盖
            time.sleep(interval)
        except KeyboardInterrupt:
            return


# ——————————————————————————————————————————————————————————————
# orch new
# ——————————————————————————————————————————————————————————————

@app.command("new")
def cmd_new(
    task: str = typer.Argument(..., help="任务描述（将作为 E1 body）。"),
    roles: str = typer.Option(
        "pm,moderator",
        "--roles",
        help="逗号分隔角色列表。E1 派发目标默认为第一个角色。",
    ),
    thread: str | None = typer.Option(
        None, "--thread", help="可选指定线程 id；缺省自动生成 t-xxxx。",
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录；缺省为当前目录。",
    ),
) -> None:
    """建线程：目录 / events.db，E1（human assign）入队（spec §12）。

    M2 骨架简化：worktrees 由 T3 permissions 模块接管；本命令只建线程目录 + 播 E1。
    """
    ws = _resolve_workspace(workspace)
    ws.mkdir(parents=True, exist_ok=True)

    tid = thread or _new_thread_id()
    role_list = [r.strip() for r in roles.split(",") if r.strip()]
    if not role_list:
        raise typer.BadParameter("--roles 至少需要一个非空角色。")

    store = _open_thread_store(ws, tid)

    # E1 = human → 首个角色（若含 pm 则用 pm，否则第一个），承载任务描述。
    # 默认派发给 pm；无 pm 时派发给第一个角色（spec §11.1 pm 为常见入口）。
    first_target = "pm" if "pm" in role_list else role_list[0]

    e1_id = store.append_event(
        sender="human",
        type="assign",
        body=task,
        to=[first_target],
        meta={"roles": role_list},
    )

    # 线程 meta：status=running，记录角色列表。
    store.set_meta("status", "running")
    store.set_meta("roles", json.dumps(role_list, ensure_ascii=False))

    _echo(f"[new] thread={tid} workspace={ws} roles={role_list} E1={e1_id}")


# ——————————————————————————————————————————————————————————————
# orch send
# ——————————————————————————————————————————————————————————————

@app.command("send")
def cmd_send(
    thread: str = typer.Argument(..., help="线程 id（如 t-abc123）。"),
    body: str = typer.Argument(..., help="发言正文。"),
    to: str | None = typer.Option(
        None, "--to", help="接收角色；缺省为 moderator。",
    ),
    type_: str = typer.Option(
        "assign", "--type", help="事件 type（缺省 assign；answer 用于回门禁问询）。",
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录。",
    ),
) -> None:
    """人类发言入队（spec §12：`orch send t-xxx "..." [--to r]`）。

    产生 from=human 的信封，走普通队列（§10：人类是一等参与者，无特殊通道）。
    """
    ws = _resolve_workspace(workspace)
    store = _open_thread_store(ws, thread)
    target = to or "moderator"
    eid = store.append_event(
        sender="human", type=type_, body=body, to=[target],
    )
    _echo(f"[send] thread={thread} human -> {target} E{eid} body={body!r}")


# ——————————————————————————————————————————————————————————————
# orch chat（把事件日志渲染成群聊）
# ——————————————————————————————————————————————————————————————

def _render_event_bubble(ev: dict) -> str:
    """一条事件 → 群聊气泡（§12：气泡 = 信封投影；@ 由 to 渲染，§16.1）。

    骨架格式（不锁死措辞）：
      [E{id} type=... from={sender}] @{r1} @{r2} <body>
    """
    sender = ev.get("from", "?")
    to_list = ev.get("to") or []
    ev_id = ev.get("id")
    etype = ev.get("type")
    body = ev.get("body", "")
    at = " ".join(f"@{r}" for r in to_list) if to_list else ""
    return f"[E{ev_id} type={etype} from={sender}] {at} {body}".rstrip()


@app.command("chat")
def cmd_chat(
    thread: str = typer.Argument(..., help="线程 id。"),
    follow: bool = typer.Option(
        False, "--follow", "-f",
        help="轮询新事件（M2 骨架简化：不是 tail -f）。",
    ),
    interval: float = typer.Option(
        1.0, "--interval", help="--follow 轮询间隔秒。",
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录。",
    ),
) -> None:
    """把事件日志渲染成群聊（spec §12/§16.1）。"""
    ws = _resolve_workspace(workspace)
    store = _open_thread_store(ws, thread)

    events = store.events()
    for ev in events:
        _echo(_render_event_bubble(ev))

    if not follow:
        return

    # --follow：轮询新事件。M2 骨架：以最大 id 为水位，简单 sleep 循环。
    last_id = events[-1]["id"] if events else 0
    try:
        while True:
            time.sleep(interval)
            latest = store.events()
            new_evs = [e for e in latest if e["id"] > last_id]
            for ev in new_evs:
                _echo(_render_event_bubble(ev))
                last_id = ev["id"]
    except KeyboardInterrupt:  # pragma: no cover - 手动 Ctrl-C
        return


# ——————————————————————————————————————————————————————————————
# orch status
# ——————————————————————————————————————————————————————————————

@app.command("status")
def cmd_status(
    thread: str = typer.Argument(..., help="线程 id。"),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录。",
    ),
) -> None:
    """线程 status + 派发行摘要（spec §12）。"""
    ws = _resolve_workspace(workspace)
    store = _open_thread_store(ws, thread)

    status = store.get_meta("status") or "unknown"
    events = store.events()
    pending = store.pending_dispatches()

    _echo(f"thread={thread}")
    _echo(f"status={status}")
    _echo(f"events={len(events)}")
    _echo(f"pending_dispatches={len(pending)}")
    for row in pending:
        _echo(
            f"  - E{row['event_id']} -> {row['target']} status={row['status']}"
            f" attempts={row['attempts']}"
        )


# ——————————————————————————————————————————————————————————————
# orch approve / reject
# ——————————————————————————————————————————————————————————————

def _apply_gate(workspace: Path, thread: str, corr: str, approve: bool) -> None:
    """公共门禁裁决路径（走 scheduler.apply_gate_decision）。"""
    store = _open_thread_store(workspace, thread)
    config = _load_config(workspace)
    # adapters 在 M2 骨架中不由 CLI 装配；apply_gate_decision 内部只在触发特权操作时用到，
    # M2 骨架的门禁裁决路径允许 adapters 为空 dict（§10 只入 gate_decision + resume）。
    orch.scheduler.apply_gate_decision(
        store, config, {}, corr=corr, approve=approve, sender="human",
    )


@app.command("approve")
def cmd_approve(
    corr: str = typer.Argument(..., help="gate_request 的 corr。"),
    thread: str = typer.Option(..., "--thread", help="线程 id。"),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录。",
    ),
) -> None:
    """门禁裁决 approve（spec §10）：产生 gate_decision(approve) + resume。"""
    ws = _resolve_workspace(workspace)
    _apply_gate(ws, thread, corr, approve=True)
    _echo(f"[approve] thread={thread} corr={corr}")


@app.command("reject")
def cmd_reject(
    corr: str = typer.Argument(..., help="gate_request 的 corr。"),
    thread: str = typer.Option(..., "--thread", help="线程 id。"),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录。",
    ),
) -> None:
    """门禁裁决 reject（spec §10）：产生 gate_decision(reject) + resume（不执行特权）。"""
    ws = _resolve_workspace(workspace)
    _apply_gate(ws, thread, corr, approve=False)
    _echo(f"[reject] thread={thread} corr={corr}")


# ——————————————————————————————————————————————————————————————
# orch stop / reopen
# ——————————————————————————————————————————————————————————————

@app.command("stop")
def cmd_stop(
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录。",
    ),
) -> None:
    """优雅停机（spec §12）：写 workspace 级 `orch.stop` 标志文件。

    M2 骨架：调度器不常驻，此命令的语义为"告诉后续 `orch run` 进程尽快退出"。
    """
    ws = _resolve_workspace(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    marker = ws / "orch.stop"
    marker.write_text(f"stopped_at={time.time()}\n", encoding="utf-8")
    _echo(f"[stop] wrote {marker}")


@app.command("reopen")
def cmd_reopen(
    thread: str = typer.Argument(..., help="线程 id。"),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录。",
    ),
) -> None:
    """重开已终止线程（spec §12）：thread_meta.status = running。"""
    ws = _resolve_workspace(workspace)
    store = _open_thread_store(ws, thread)
    store.set_meta("status", "running")
    _echo(f"[reopen] thread={thread} status=running")


# ——————————————————————————————————————————————————————————————
# orch attach
# ——————————————————————————————————————————————————————————————

def _lookup_session(store: "orch.store.Store", role: str) -> dict | None:
    """从 sessions 表读某角色的 sid/backend。返回 None 表示无记录。"""
    # 直接查 sqlite3 连接（Store 未暴露读 sessions 的公开 API，M2 骨架许可）。
    row = store._con.execute(
        "SELECT role, backend, sid, last_evt, gen FROM sessions WHERE role=?",
        (role,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


@app.command("attach")
def cmd_attach(
    thread: str = typer.Argument(..., help="线程 id。"),
    role: str = typer.Argument(..., help="角色名（如 backend / pm）。"),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录。",
    ),
) -> None:
    """打印该角色原生会话接入命令（spec §12：`claude --resume <sid>` 等）。

    有 sid → 打印真实 resume 命令（默认按 claude 形态）。
    无 sid → 兜底提示：暂无 sid + 冷启动示例（契约 §5：即使 sid=None 也要有兜底提示）。
    """
    ws = _resolve_workspace(workspace)
    store = _open_thread_store(ws, thread)
    sess = _lookup_session(store, role)

    if sess is None or not sess.get("sid"):
        # 兜底：无 sid（尚未冷启动或 API 型角色永远没有 sid）。
        _echo(f"[attach] thread={thread} role={role}: no sid (暂无活会话)")
        _echo(
            "  冷启动示例（真实 flag 以各 CLI --help 为准，QUESTIONS.md 陪跑项）:\n"
            f"    cd <workspace>/{thread}\n"
            "    claude --print --output-format json < view.txt   # CLI 型（Claude Code）\n"
            "    codex --print --output-format json < view.txt    # CLI 型（Codex）\n"
            "    kimi  --print --output-format json < view.txt    # CLI 型（Kimi CLI）"
        )
        return

    backend = sess.get("backend") or "claude"
    sid = sess["sid"]
    # 按 backend 分派 resume 命令（M2 骨架默认按 claude；真实 flag 待陪跑实测）。
    if backend == "claude":
        cmd = f"claude --resume {sid}"
    elif backend == "codex":
        cmd = f"codex --resume {sid}"
    elif backend == "kimi":
        cmd = f"kimi --resume {sid}"
    else:
        cmd = f"{backend} --resume {sid}"
    _echo(f"[attach] thread={thread} role={role} backend={backend} sid={sid}")
    _echo(f"  {cmd}")


# ——————————————————————————————————————————————————————————————
# orch bench resume（spec §12/§13，M3-T4）
# ——————————————————————————————————————————————————————————————

def _bench_config(context_window: int = 100_000) -> dict:
    """bench 用的最小 config（结构同 docs/m1-contract.md §5；prompt 缺省为占位文本）。

    只声明一个 backend 角色（bench 只关心"某角色多轮 invoke 的 tokens_in"，
    不需要多角色编排）；adapter=mock，context_window 足够大以避免触发 §6.3 压缩
    干扰 bench 对比（压缩会让 cold/resume 差异失真）。
    """
    return {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {},
        "adapters": {"mock": {"kind": "mock", "context_window": context_window}},
        "roles": {
            "backend": {
                "adapter": "mock",
                "can_decide": False,
                "write_scope": ["server/"],
                "tools": ["Edit", "Write"],
                "prompt": None,
            },
        },
    }


def _bench_fixture_events(fixture: str) -> list[dict]:
    """bench 用简化任务事件序列（M3 契约 §5：用 pytest fixture 而非附录B）。

    fixture 名目前只需一个稳定别名 "like"（对齐 tests/test_bench_cli.py）；
    未识别的 fixture 名也退化到同一套简化序列（bench 不因陌生 fixture 名报错，
    只是对比实验，容错优先）。序列刻意含多轮 pm/tester → backend 的 B 类事件 +
    一条 A 类 decision，用以体现"新事件全文 + 黑板 diff"的热续增量方向。
    """
    return [
        {"sender": "pm", "type": "review", "to": ["backend"],
         "body": f"[{fixture}] PRD v1 发起评审，请 backend 确认字段。"},
        {"sender": "pm", "type": "decision", "to": ["moderator"],
         "body": f"[{fixture}] 契约 like-api v1 冻结",
         "blackboard_ops": [
             {"op": "freeze_contract", "name": "like-api",
              "path": "docs/like-api.md", "version": 1},
         ]},
        {"sender": "backend", "type": "answer", "to": ["pm"],
         "body": f"[{fixture}] 已确认字段，开始实现。"},
        {"sender": "tester", "type": "defect", "to": ["backend"],
         "body": f"[{fixture}] 已删资源二次操作返回 500，请修复。"},
        {"sender": "pm", "type": "decision", "to": ["moderator"],
         "body": f"[{fixture}] 契约 like-api v2 冻结（幂等语义）",
         "blackboard_ops": [
             {"op": "freeze_contract", "name": "like-api",
              "path": "docs/like-api.md", "version": 2},
             {"op": "set_decision", "text": "二次操作按幂等处理，不报错"},
         ]},
        {"sender": "frontend", "type": "review", "to": ["backend"],
         "body": f"[{fixture}] 前端联调发现返回体缺 updated_at 字段。"},
    ]


def _bench_seed(store: "orch.store.Store", fixture: str) -> list[int]:
    ids: list[int] = []
    for spec in _bench_fixture_events(fixture):
        ids.append(store.append_event(**spec))
    return ids


def _bench_cold_tokens(store: "orch.store.Store", config: dict, event_ids: list[int]) -> int:
    """关 resume（冷启动）：每轮对完整事件流重跑 render_view，tokens_in = token_est。"""
    view = orch.render.render_view(
        store, config,
        role="backend", event_ids=event_ids, cold_start=True,
        instruction="请处理以上事件。",
    )
    return int(view["meta"]["token_est"])


def _bench_resume_tokens(
    store: "orch.store.Store", config: dict,
    event_ids: list[int], last_evt: int | None,
) -> int:
    """开 resume（热续近似）：首轮无差别走冷启动；此后只对"新增事件切片"
    重新 render_view 并只计入其焦点段 + 指令尾（不重复计入系统层/黑板层——
    热续时二者已在会话上下文中，§6.5 语义为"黑板 diff + 新事件 + 指令尾"，
    本近似省略黑板 diff 的额外 token（首轮已含黑板全文），故为保守下界估算）。

    本函数只使用 orch.render 已冻结导出的 render_view/estimate_tokens，
    不依赖尚未落地的 render_delta（T2 另卡 owner，不在本卡可写路径）。
    """
    if last_evt is None:
        return _bench_cold_tokens(store, config, event_ids)

    new_ids = [eid for eid in event_ids if eid > last_evt]
    if not new_ids:
        # 无新事件：热续只需重发指令尾（近似为极小常量 token）。
        instruction_text = f"你是 backend。现在只针对 回应：请处理以上事件。"
        return orch.render.estimate_tokens(instruction_text)

    # 只对新增切片跑 render_view（cold_start=False 语义上是热续视图），
    # 其 token_est 近似 "新事件全文 + 指令尾"（该切片自身黑板层为空或极小，
    # 因为 render_view 的黑板层取自当前 board.md 全文——为避免重复计入黑板全文，
    # 这里改用 sections 拆分只累加 background+focus+instruction 三段）。
    view = orch.render.render_view(
        store, config,
        role="backend", event_ids=new_ids, cold_start=False,
        instruction="请处理以上事件。",
    )
    sections = view["sections"]
    partial_text = "\n\n".join(
        sections[name] for name in ("background", "focus", "instruction")
        if sections.get(name, "").strip()
    )
    return orch.render.estimate_tokens(partial_text)


def _run_bench_series(
    ws: Path, fixture: str, runs: int, *, use_resume: bool,
) -> list[int]:
    """跑 runs 轮，每轮新建独立线程目录（互不干扰），返回每轮 tokens_in 列表。"""
    config = _bench_config()
    samples: list[int] = []
    for i in range(runs):
        tdir = ws / f"bench-{'resume' if use_resume else 'cold'}-{i}-{uuid.uuid4().hex[:6]}"
        store = orch.store.Store(tdir)
        ids = _bench_seed(store, fixture)
        if not use_resume:
            samples.append(_bench_cold_tokens(store, config, ids))
        else:
            # 首轮（前半段事件）冷启动建会话，后半段作为"新事件"走热续近似。
            mid = max(1, len(ids) // 2)
            first_ids, rest_ids = ids[:mid], ids
            _ = _bench_cold_tokens(store, config, first_ids)  # 建立会话上下文（不计入 tokens_in 对比本身）
            last_evt = first_ids[-1]
            samples.append(_bench_resume_tokens(store, config, rest_ids, last_evt))
    return samples


@bench_app.command("resume")
def cmd_bench_resume(
    fixture: str = typer.Option(
        "like", "--fixture", help="简化任务 fixture 别名（M3 契约 §5：非附录B）。",
    ),
    runs: int = typer.Option(
        3, "--runs", help="开/关 resume 各跑的轮数（≥3 才有相对意义，§13）。",
    ),
    with_resume: bool = typer.Option(
        False, "--with-resume", help="只跑「开 resume」一条路径。",
    ),
    no_resume: bool = typer.Option(
        False, "--no-resume", help="只跑「关 resume」一条路径。",
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录；缺省为当前目录。",
    ),
) -> None:
    """§12/§13 `orch bench resume`：同 fixture 开/关 resume 各跑 N 次的 token 对比。

    不启真子进程（M3 契约 §5）：只在内部对 orch.render 反复 invoke 并用
    estimate_tokens 累计 tokens_in，比较冷启动全量 vs 热续增量近似的均值差与百分比。

    --with-resume / --no-resume 均缺省时（默认）→ 两条路径都跑并给出对比报告；
    只给其一 → 只跑该路径（用于单独抽查某侧, e-3 测试覆盖）。
    """
    ws = _resolve_workspace(workspace)
    ws.mkdir(parents=True, exist_ok=True)

    # 默认（既不给 --with-resume 也不给 --no-resume）→ 两条路径都跑，产出完整对比报告。
    # 只给其一 → 只跑该路径（e-3 测试覆盖：两开关各自独立可用）。
    only_warm = with_resume and not no_resume
    only_cold = no_resume and not with_resume
    run_cold = not only_warm
    run_warm = not only_cold

    _echo(f"[bench resume] fixture={fixture} runs={runs} workspace={ws}")

    cold_samples: list[int] = []
    warm_samples: list[int] = []

    if run_cold:
        cold_samples = _run_bench_series(ws, fixture, runs, use_resume=False)
        _echo(f"  no-resume tokens_in per run: {cold_samples}")

    if run_warm:
        warm_samples = _run_bench_series(ws, fixture, runs, use_resume=True)
        _echo(f"  with-resume tokens_in per run: {warm_samples}")

    if cold_samples:
        cold_mean = statistics.mean(cold_samples)
        _echo(f"  no-resume tokens_in mean: {cold_mean:.1f}")
    if warm_samples:
        warm_mean = statistics.mean(warm_samples)
        _echo(f"  with-resume tokens_in mean: {warm_mean:.1f}")

    if cold_samples and warm_samples:
        cold_mean = statistics.mean(cold_samples)
        warm_mean = statistics.mean(warm_samples)
        diff = cold_mean - warm_mean
        pct = (diff / cold_mean * 100.0) if cold_mean > 0 else 0.0
        _echo(f"  tokens_in mean diff (no-resume - with-resume): {diff:.1f}")
        _echo(f"  tokens saved %: {pct:.1f}%")
    elif cold_samples or warm_samples:
        _echo(
            "  (single path run; pass without --with-resume/--no-resume, or run"
            " the other side too, for a tokens saved % comparison)"
        )


# ——————————————————————————————————————————————————————————————
# orch threads
# ——————————————————————————————————————————————————————————————

@app.command("threads")
def cmd_threads(
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录。",
    ),
) -> None:
    """列 workspace 下所有 `t-xxx` 线程（spec §12）。"""
    ws = _resolve_workspace(workspace)
    dirs = _find_thread_dirs(ws)
    if not dirs:
        _echo(f"(no threads under {ws})")
        return
    for d in dirs:
        # 每个线程读一下 meta.status 便于一览。
        try:
            store = orch.store.Store(d)
            status = store.get_meta("status") or "unknown"
        except (OSError, ValueError):
            status = "?"
        _echo(f"{d.name}\tstatus={status}")


# ——————————————————————————————————————————————————————————————
# 入口
# ——————————————————————————————————————————————————————————————

def main() -> None:  # pragma: no cover - typer 直接管
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
