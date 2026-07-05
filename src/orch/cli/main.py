"""用户界面 CLI（spec §12 子集，M2-T4）。

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

【M2 骨架边界】
  - worktrees / 权限 三件套由 T3 落地；本层新建 workspace/t-xxx/{events.db, blackboard, logs}。
  - --follow 用轮询而非 tail -f（本层为 M2 骨架，M3 完善）。
  - `orch run` 骨架：默认装配 Fake adapters（陪跑接入真实 CLI/API 属 §17 开放决策），
    只做常驻循环骨架 + orch.stop 消费。真实 CLI/API 由 config 覆盖（M3 完善）。
  - 各命令的具体 flag 措辞取自 spec §12 表 + M2 契约 §5；与真实 CLI 的 flag 联跑差异属 §17
    开放决策（升级 QUESTIONS.md）。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import typer

import orch.store
import orch.scheduler


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="orch —— 异构多智能体编排系统 CLI（spec §12 子集）。",
)


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
