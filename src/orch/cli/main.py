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
  orch metrics [--thread t-xxx]         §13 指标汇总表（M4-T4）
  orch replay --thread t-xxx            按事件号升序渲染第三人称群聊 markdown（M4-T4）

【M2 骨架边界】
  - worktrees / 权限 三件套由 T3 落地；本层新建 workspace/t-xxx/{events.db, blackboard, logs}。
  - --follow 用轮询而非 tail -f（本层为 M2 骨架，M3 完善）。
  - `orch run` 骨架：默认装配 Fake adapters（陪跑接入真实 CLI/API 属 §17 开放决策），
    只做常驻循环骨架 + orch.stop 消费。真实 CLI/API 由 config 覆盖（M3 完善）。
  - 各命令的具体 flag 措辞取自 spec §12 表 + M2 契约 §5；与真实 CLI 的 flag 联跑差异属 §17
    开放决策（升级 QUESTIONS.md）。

【M4-T4 边界（本卡新增）】
  - `orch metrics`：从 metrics 表 + events 表汇总 §13 全表指标，纯文本表格输出。
    空 workspace / 无采集数据时对应字段显示 "N/A"（不采信具体数值，只保证字段名齐全）。
  - `orch replay`：按事件 id 升序，把某线程的事件日志渲染成"第三人称"markdown 群聊，
    每条形如 `#{id} [{from}->@{to1},@{to2}] ({type}): {body}`（§16.1：路由只认 to 字段，
    不从 body 解析 @）。

【M3-T4 边界】
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


def _force_utf8_stdio() -> None:
    """审视快赢②（P1 乱码根治）：Windows GBK 控制台/管道下中文输出全乱码。

    stdout/stderr 统一重配 UTF-8；不支持 reconfigure 的流（StringIO/测试替身）
    静默跳过。模块导入即生效，覆盖全部子命令（含 serve 的启动横幅）。
    """
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_stdio()


def _attach_run_log_handler() -> None:
    """审视快赢①：把核心环 orch.run 过程日志接到 stderr（[run] 前缀）。

    幂等：已挂过则只重绑当前 stderr（CliRunner 每次调用替换 sys.stderr）。
    """
    import logging
    import sys
    lg = logging.getLogger("orch.run")
    for h in lg.handlers:
        if getattr(h, "_orch_run_cli", False):
            h.stream = sys.stderr
            lg.setLevel(logging.INFO)
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[run] %(message)s"))
    handler._orch_run_cli = True
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)


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


def _build_adapters_from_config(roles: list[str], config: dict, thread_dir: Path) -> dict:
    """据 config（§11.1）为每角色装配**真实** CLI 适配器（Q1/Q2 陪跑接入）。

    role → config.roles[role].adapter → config.adapters[name].kind：
      - cli → CliAdapter（cwd=thread_dir；write_scope 空的角色无需 git worktree）。
    role 层字段（can_decide/write_scope/tools/supports_resume）覆盖 adapter 层。
    暂只支持 kind=cli（真实联跑）；其它 kind 显式报错，不臆造后端（诚实边界）。
    """
    from orch.adapters import CliAdapter
    from orch.scheduler.permissions import ensure_worktrees

    adapters_conf = config.get("adapters", {}) or {}
    roles_conf = config.get("roles", {}) or {}
    # §8.1 落代码隔离：config 有 target_repo → 为 write_scope 非空的角色建 git worktree。
    # worktree 路径写回 config['worktrees'][role]，供 core.py §8.2 越权审计（_role_worktree 读它）。
    # 无 target_repo（纯对话联跑）→ worktrees 为空，CliAdapter cwd 回退 thread_dir（不破坏既有）。
    worktrees: dict = {}
    target_repo = config.get("target_repo")
    if target_repo:
        wt_root = Path(thread_dir) / "worktrees"
        worktrees = ensure_worktrees(
            config, Path(target_repo), wt_root, thread_id=Path(thread_dir).name,
        )
        wt_map = config.setdefault("worktrees", {})
        for r, wt in worktrees.items():
            wt_map[r] = str(wt)
    out: dict = {}
    for role in roles:
        rc = dict(roles_conf.get(role, {}) or {})
        aname = rc.get("adapter")
        ac = dict(adapters_conf.get(aname, {}) or {})
        merged = {**ac, **rc}
        kind = str(merged.get("kind", ac.get("kind", "")))
        if kind == "cli":
            # 有 write_scope 的角色 cwd=其 git worktree（kimi 在隔离沙箱写代码）；否则 thread_dir。
            wt = worktrees.get(role, Path(thread_dir))
            out[role] = CliAdapter(role=role, config=merged, worktree=wt)
        else:
            raise ValueError(
                f"真实装配暂只支持 kind=cli（角色 {role!r} 解析到 kind={kind!r}）；"
                "混合/API 后端属后续陪跑项。"
            )
    return out


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
    _attach_run_log_handler()   # 快赢①：核心环过程日志 → stderr
    warned_fake = False
    announced_resident = False

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
            cfg = _load_config(ws)
            if cfg.get("adapters") and cfg.get("roles"):
                adapters = _build_adapters_from_config(roles, cfg, tdir)
            else:
                # 快赢③（P1 防"假跑"误导）：无 adapters/roles 配置必须显式告知。
                if not warned_fake:
                    _echo("⚠ [run] workspace 无 adapters/roles 配置——使用 Fake 演示适配器"
                          "（仅验证控制流，非真实模型输出）。真实联跑请在 config.yaml "
                          "配置 adapters + roles（见 docs/USAGE.md）。")
                    warned_fake = True
                adapters = _build_default_adapters(roles)
            try:
                orch.scheduler.run_thread(store, cfg, adapters)
            except Exception as exc:  # noqa: BLE001 骨架层兜底：不因单个线程崩溃拖垮 run
                _echo(f"[run] thread {tdir.name} error: {exc!r}")

        # 步骤 3：--once 结束；常驻模式睡一觉再回步骤 1（同时检查 stop 标志）。
        if once:
            return
        if not announced_resident:  # 快赢①：长驻不再无声（一次性横幅）
            _echo(f"[run] 常驻监听中（每 {interval}s 巡一轮；Ctrl-C 或 orch stop 退出）")
            announced_resident = True
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

def _resolve_gate_thread(ws: Path, corr: str) -> str:
    """spec §12 `orch approve|reject <corr>`：缺省 --thread 时按 corr 扫描唯一定位。

    只查表（§16.10 禁猜测）：逐线程用 systemexec 的门禁定位（正式 gate_request
    或 §10 生成形 gate-{事件号} 反解）判定命中；0 命中/多命中都拒绝并给人话。
    """
    from orch.scheduler.systemexec import _find_gate_request, _find_informal_gate
    hits: list[str] = []
    for tdir in _find_thread_dirs(ws):
        store = orch.store.Store(tdir)
        if _find_gate_request(store, corr) or _find_informal_gate(store, corr):
            hits.append(tdir.name)
    if not hits:
        raise KeyError(f"未找到 corr={corr} 的门禁信封（已扫描 workspace 全部线程）")
    if len(hits) > 1:
        raise KeyError(
            f"corr={corr} 命中多个线程（{', '.join(sorted(hits))}），请用 --thread 指定"
        )
    return hits[0]


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
    corr: str = typer.Argument(..., help="门禁 corr（gate-01 或生成形 gate-{事件号}）。"),
    thread: str | None = typer.Option(
        None, "--thread", help="线程 id；缺省按 corr 扫描唯一定位（撞车时才必填）。",
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录。",
    ),
) -> None:
    """门禁裁决 approve（spec §10/§12 `orch approve <corr>`）：gate_decision + resume。"""
    ws = _resolve_workspace(workspace)
    try:
        tid = thread or _resolve_gate_thread(ws, corr)
        _apply_gate(ws, tid, corr, approve=True)
    except KeyError as exc:   # 快赢④：一行人话，不向用户喷 Traceback
        _echo(f"[错误] {exc.args[0] if exc.args else exc}")
        raise typer.Exit(code=1)
    _echo(f"[approve] thread={tid} corr={corr}")


@app.command("reject")
def cmd_reject(
    corr: str = typer.Argument(..., help="门禁 corr（gate-01 或生成形 gate-{事件号}）。"),
    thread: str | None = typer.Option(
        None, "--thread", help="线程 id；缺省按 corr 扫描唯一定位（撞车时才必填）。",
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录。",
    ),
) -> None:
    """门禁裁决 reject（spec §10/§12）：gate_decision(reject) + resume（不执行特权）。"""
    ws = _resolve_workspace(workspace)
    try:
        tid = thread or _resolve_gate_thread(ws, corr)
        _apply_gate(ws, tid, corr, approve=False)
    except KeyError as exc:   # 快赢④：一行人话，不向用户喷 Traceback
        _echo(f"[错误] {exc.args[0] if exc.args else exc}")
        raise typer.Exit(code=1)
    _echo(f"[reject] thread={tid} corr={corr}")


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
# orch metrics（spec §13 全表汇总，M4-T4）
# ——————————————————————————————————————————————————————————————

def _thread_dirs_for_metrics(workspace: Path, thread: str | None) -> list[Path]:
    """--thread 指定单线程；否则 workspace 下全部 t-xxx（无则空列表）。"""
    if thread:
        d = workspace / thread
        return [d] if d.exists() else []
    return _find_thread_dirs(workspace)


def _collect_metric_values(store: "orch.store.Store", key: str) -> list[float]:
    """从 metrics 表读某 key 的全部 value（无表/无行 → 空列表，宽松兜底）。"""
    try:
        rows = store._con.execute(
            "SELECT value FROM metrics WHERE key=?", (key,)
        ).fetchall()
    except Exception:  # noqa: BLE001 - metrics 表缺失等极端情况兜底为空
        return []
    return [float(r["value"]) for r in rows]


def _fmt_num(x: float | None, suffix: str = "") -> str:
    if x is None:
        return "N/A"
    return f"{x:.2f}{suffix}"


def _fmt_pct(x: float | None) -> str:
    return _fmt_num(x, suffix="%")


@app.command("metrics")
def cmd_metrics(
    thread_arg: str | None = typer.Argument(
        None, metavar="[THREAD]",
        help="只统计单个线程（spec §12 写法：orch metrics t-001）；缺省汇总全部。",
    ),
    thread_opt: str | None = typer.Option(
        None, "--thread", help="同上（旧写法兼容别名）。",
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录；缺省为当前目录。",
    ),
) -> None:
    """§13 指标汇总表（M4-T4）：从 metrics 表 + events 表聚合，空数据显示 N/A。

    覆盖 §13 全部七类指标（数值为相对/近似估算，非精确计费）：
      1) 端到端任务数 / 平均轮数 / 成本
      2) 聚合节省 %（Σ(batch_size-1)/总调用数）
      3) 首次合法率 %（1 - schema_retry/total）
      4) 背景层压缩比（summarized/orig token 均值）
      5) resume 输入 token 节省 %（bench resume 结果，见 orch bench resume）
      6) 混沌轮数与两层结果（mock 100% / 真实 %）
      7) 新增供应商 adapter 行数（cloc，从第 3 家起算）
    """
    thread = thread_arg or thread_opt
    ws = _resolve_workspace(workspace)
    dirs = _thread_dirs_for_metrics(ws, thread)

    # —— 1) 任务数 / 平均轮数 / 成本 ——
    task_count = len(dirs)
    round_counts: list[int] = []
    cost_values: list[float] = []
    batch_sizes: list[float] = []
    token_row_count = 0            # `tokens` 行数 = invoke 计数（§13 首次合法率分母/"总调用数"）
    schema_retry_vals: list[float] = []
    bg_orig_vals: list[float] = []
    bg_summarized_vals: list[float] = []

    for d in dirs:
        store = orch.store.Store(d)
        events = store.events()
        round_counts.append(len(events))
        cost_values.extend(_collect_metric_values(store, "cost"))
        batch_sizes.extend(_collect_metric_values(store, "batch_size"))
        # §13：每次 invoke 一条 tokens 行；行数即 invoke 计数（首次合法率的"总调用数"）。
        token_row_count += len(_collect_metric_values(store, "tokens"))
        schema_retry_vals.extend(_collect_metric_values(store, "schema_retry"))
        bg_orig_vals.extend(_collect_metric_values(store, "bg_orig_tokens"))
        bg_summarized_vals.extend(_collect_metric_values(store, "bg_summarized_tokens"))

    avg_rounds = statistics.mean(round_counts) if round_counts else None
    # §13 成本：仅当有真实 cost 行（adapter 暴露 last_usage）才求和；否则 None → N/A
    # （诚实边界，禁止编造 cost=0；Mock/Fake 无用量 → 恒 N/A，真实后端 Q1/Q2 陪跑充值）。
    total_cost = sum(cost_values) if cost_values else None

    # —— 2) 聚合节省 %：Σ(batch_size-1)/总调用数 ——
    if batch_sizes:
        saved = sum(max(0.0, b - 1.0) for b in batch_sizes)
        agg_save_pct = (saved / sum(batch_sizes) * 100.0) if sum(batch_sizes) > 0 else None
    else:
        agg_save_pct = None

    # —— 3) 首次合法率 %：1 - schema_retry 行数 ÷ invoke(tokens) 行数（§13，R-T4 复算口径）——
    # 分母 = tokens 行数（每次 invoke 一条），分子 = schema_retry 行数（每次校验失败一条）。
    # 有 invoke 记录（tokens 行≥1）时即可复算：无 retry 行 → retry=0 → 首次合法率 100%（真实
    # 反映"全部一次合法"，非 N/A）；无任何 invoke 记录 → N/A（不臆造）。
    retry_calls = sum(schema_retry_vals)  # 无行时为 0.0
    if token_row_count > 0:
        first_legal_pct = (1.0 - retry_calls / token_row_count) * 100.0
    else:
        first_legal_pct = None

    # —— 4) 背景层压缩比：summarized/orig 均值 ——
    if bg_orig_vals and bg_summarized_vals:
        orig_sum = sum(bg_orig_vals)
        comp_ratio = (sum(bg_summarized_vals) / orig_sum) if orig_sum > 0 else None
    else:
        comp_ratio = None

    # —— 5) resume 输入 token 节省 %：走 orch.render 估算（依赖 orch bench resume 采集，
    #        本命令不重跑 bench；若 metrics 表已有该采集点则汇总，否则 N/A）——
    resume_save_vals = []
    for d in dirs:
        store = orch.store.Store(d)
        resume_save_vals.extend(_collect_metric_values(store, "resume_token_save_pct"))
    resume_save_pct = statistics.mean(resume_save_vals) if resume_save_vals else None

    # —— 6) 混沌轮数与两层结果 ——
    chaos_rounds_vals: list[float] = []
    chaos_mock_pass_vals: list[float] = []
    chaos_real_pass_vals: list[float] = []
    for d in dirs:
        store = orch.store.Store(d)
        chaos_rounds_vals.extend(_collect_metric_values(store, "chaos_rounds"))
        chaos_mock_pass_vals.extend(_collect_metric_values(store, "chaos_mock_pass_pct"))
        chaos_real_pass_vals.extend(_collect_metric_values(store, "chaos_real_pass_pct"))
    chaos_rounds = sum(chaos_rounds_vals) if chaos_rounds_vals else None
    chaos_mock_pct = statistics.mean(chaos_mock_pass_vals) if chaos_mock_pass_vals else None
    chaos_real_pct = statistics.mean(chaos_real_pass_vals) if chaos_real_pass_vals else None

    # —— 7) 新增供应商 adapter 行数（cloc，从第 3 家起算）——
    adapter_loc = _count_adapter_loc_from_third()

    # —— 输出：保守纯文本表格（不锁死具体措辞，字段名齐全即可）——
    _echo(f"orch metrics —— workspace={ws} thread={thread or '(all)'}")
    _echo("=" * 60)
    _echo(f"[1] 任务数(tasks)              : {task_count}")
    _echo(f"    平均轮数(avg rounds)        : {_fmt_num(avg_rounds)}")
    _echo(f"    成本(cost)                  : {_fmt_num(total_cost)}")
    _echo(f"[2] 聚合节省 %(aggregate save)  : {_fmt_pct(agg_save_pct)}")
    _echo(f"[3] 首次合法率 %(first-legal)   : {_fmt_pct(first_legal_pct)}")
    _echo(f"[4] 背景压缩比(background compression ratio): {_fmt_num(comp_ratio)}")
    _echo(f"[5] resume 输入 token 节省 %     : {_fmt_pct(resume_save_pct)}")
    _echo(f"    (via `orch bench resume` 采集；未采集显示 N/A)")
    _echo(f"[6] 混沌(chaos)轮数              : {_fmt_num(chaos_rounds)}")
    _echo(f"    mock 层通过率 %              : {_fmt_pct(chaos_mock_pct)}")
    _echo(f"    真实层通过率 %               : {_fmt_pct(chaos_real_pct)}")
    _echo(f"[7] 新增供应商 adapter 行数(adapter LoC, cloc, 从第3家起算): {adapter_loc}")


def _count_adapter_loc_from_third() -> str:
    """粗略 cloc：src/orch/adapters/ 下按文件名排序，从第 3 个 adapter 文件起累加行数。

    §13：只关心"新增供应商 adapter"的行数（前两家视为基线 CLI/API 骨架，不计入）；
    未找到 adapters 目录或不足 3 个文件 → "N/A"（不臆造数值）。
    """
    adapters_dir = Path(__file__).resolve().parents[1] / "adapters"
    if not adapters_dir.exists():
        return "N/A"
    files = sorted(
        p for p in adapters_dir.iterdir()
        if p.is_file() and p.suffix == ".py" and p.name != "__init__.py"
    )
    if len(files) < 3:
        return "N/A (< 3 adapters)"
    extra_files = files[2:]
    total = 0
    for f in extra_files:
        try:
            total += sum(1 for _ in f.open("r", encoding="utf-8"))
        except OSError:
            continue
    return str(total)


# ——————————————————————————————————————————————————————————————
# orch replay（spec §12：按事件号升序渲染第三人称群聊 markdown，M4-T4）
# ——————————————————————————————————————————————————————————————

def _render_replay_line(ev: dict) -> str:
    """一条事件 → 第三人称群聊 markdown 行（§16.1：路由只认 to 字段）。

    形状：`#{id} [{from}->@{to1},@{to2}...] ({type}): {body}`
    """
    ev_id = ev.get("id")
    sender = ev.get("from", "?")
    to_list = ev.get("to") or []
    etype = ev.get("type")
    body = ev.get("body", "")
    to_part = ",".join(f"@{r}" for r in to_list) if to_list else "@(none)"
    return f"#{ev_id} [{sender}->{to_part}] ({etype}): {body}"


def _late_after_id(events: list[dict]) -> int | None:
    """③迟到标记（P3，展示层）：最后一条 terminate 的事件号。

    其后落盘的非 system 事件 = 终止前已在飞行中的在途回复（"日志=真相"，
    §5.4 只拒新派发不拒落账）——如实入账但加标记免读者困惑。
    """
    ids = [int(e["id"]) for e in events if e.get("type") == "terminate"]
    return max(ids) if ids else None


def _render_replay_lines(events: list[dict]) -> list[str]:
    """整流渲染：逐行 _render_replay_line + 终止后到达标记（CLI 与 web 共用）。"""
    late_after = _late_after_id(events)
    out: list[str] = []
    for ev in events:
        line = _render_replay_line(ev)
        if (late_after is not None and int(ev.get("id") or 0) > late_after
                and ev.get("from") != "system"):
            line += "　⏱（终止后到达：在途回复，如实入账）"
        out.append(line)
    return out


@app.command("replay")
def cmd_replay(
    thread_arg: str | None = typer.Argument(
        None, metavar="[THREAD]",
        help="线程 id（spec §12 写法：orch replay t-001）。",
    ),
    thread_opt: str | None = typer.Option(
        None, "--thread", help="线程 id（旧写法兼容别名，等价位置参数）。",
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", help="workspace 根目录；缺省为当前目录。",
    ),
) -> None:
    """§12 orch replay：按事件 id 升序渲染第三人称群聊 markdown（M4-T4）。

    第三人称标签 `[from->@to1,@to2...]` 只取自事件 to 字段（§16.1 硬约束：
    不从 body 正文解析 @ 提及进入路由标签）。
    语法统一（spec §12 回归）：thread 为位置参数；--thread 保留兼容。
    """
    thread = thread_arg or thread_opt
    if not thread:
        _echo("[错误] 缺少线程 id：orch replay t-001（或旧写法 --thread t-001）")
        raise typer.Exit(code=1)
    ws = _resolve_workspace(workspace)
    store = _open_thread_store(ws, thread)
    events = store.events()

    _echo(f"# orch replay —— thread={thread} workspace={ws}")
    if not events:
        _echo(f"(thread {thread} 暂无事件)")
        return

    for line in _render_replay_lines(events):
        _echo(line)


# ——————————————————————————————————————————————————————————————
# orch serve（spec 之外的补充工具：玻璃感 Web 控制台入口，W1）
# ——————————————————————————————————————————————————————————————

@app.command("serve")
def cmd_serve(
    workspace: list[str] | None = typer.Option(
        None, "--workspace", "-w",
        help="workspace 根目录，可重复传多个（②多工作区单控制台，顶栏下拉切换）；缺省当前目录。",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址。"),
    port: int = typer.Option(8787, "--port", help="监听端口。"),
) -> None:
    """启动玻璃感 Web 控制台（spec 之外的补充工具）。

    命令体尽量薄：真正逻辑在 orch.web.server.make_server；此处只解析 workspace、
    起 ThreadingHTTPServer 常驻、打印访问地址、Ctrl-C 优雅退出。
    """
    from orch.web.server import make_server

    ws_list = [_resolve_workspace(w) for w in (workspace or [None])]
    for w in ws_list:
        w.mkdir(parents=True, exist_ok=True)
    srv = make_server(ws_list if len(ws_list) > 1 else ws_list[0], host, port)
    actual_port = srv.server_address[1]
    _echo(f"[serve] http://{host}:{actual_port}")
    for w in ws_list:
        _echo(f"[serve]   workspace: {w}")
    _echo("[serve] Ctrl-C 停止。")
    try:  # pragma: no cover - 常驻循环，测试走 make_server 直接起停
        srv.serve_forever()
    except KeyboardInterrupt:
        _echo("[serve] shutting down")
        srv.shutdown()
        srv.server_close()


# ——————————————————————————————————————————————————————————————
# 入口
# ——————————————————————————————————————————————————————————————

def main() -> None:  # pragma: no cover - typer 直接管
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
