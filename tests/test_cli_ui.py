"""M2 T1 · CLI 用户界面骨架验收测试（spec §12；M2 契约 §5）。

覆盖任务卡条目 (e)：
  用 typer.testing.CliRunner 验证 §12 子集命令骨架的存在与参数：
    - orch new "任务" [--roles ...]       建线程（目录/db/worktrees + E1 入队）
    - orch send t-xxx "..." [--to r]      人类发言入队
    - orch chat t-xxx [--follow]          事件日志渲染为群聊
    - orch status t-xxx                   派发表 + 状态
    - orch approve|reject <corr>          门禁裁决
    - orch stop                           优雅停机（写元数据）
    - orch attach t-xxx role              打印该角色原生会话接入命令（sid=None 有兜底）
    - orch threads                        列线程

M2 边界（任务卡红线）：
  - 不启真实调度进程；不发真实 HTTP；只验证 CLI 参数解析 + 落盘副作用（§4.4）。
  - `orch run` 是常驻进程（§12），M2 骨架允许有 --once 类退出参数，本卡不做强约定，
    也不测 run。

硬约束（契约 §1/§7）：
  - 顶层只 `import orch.cli`；具体符号在函数体内引用（未实现 → 运行时红）。
  - CliRunner 隔离，不打真实网络；用 tmp_dir 做工作根。
  - typer 已在 optional-deps.cli 中声明；本机可用（pyproject.toml）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import orch.cli  # 包级导入
import orch.store  # 用于校验命令的落盘副作用


def _runner():
    """独立函数，避免顶层依赖 typer 破坏 collection。"""
    from typer.testing import CliRunner
    return CliRunner()


def _get_app():
    """typer app 应作为 orch.cli.app 暴露（§12 骨架，M2 契约 §5）。"""
    return orch.cli.app


# ——————————————————————————————————————————————————————————————
# (e-0) 命令入口存在（未实现符号触发 AttributeError → 红）
# ——————————————————————————————————————————————————————————————

def test_cli_app_exists_and_help_lists_subcommands():
    """§12 骨架命令必须在 typer app 中注册（--help 列出）。"""
    app = _get_app()
    r = _runner().invoke(app, ["--help"])
    assert r.exit_code == 0, r.output
    for sub in ("new", "send", "chat", "status", "approve", "reject",
                "stop", "attach", "threads"):
        assert sub in r.output, f"§12：命令 {sub!r} 应出现在 --help 中"


# ——————————————————————————————————————————————————————————————
# (e-1) orch new：建线程目录 + E1 入队
# ——————————————————————————————————————————————————————————————

def test_cli_new_creates_thread_dir_and_seeds_e1(tmp_dir):
    """§12 new：目录 / db / worktrees + E1 入队（human assign）。
    骨架允许简化 worktrees（M2 契约 §5：worktrees 落地由 T3 接管）。"""
    app = _get_app()
    # 骨架允许 --workspace / --root 指定线程根；采用通用 --workspace（若骨架换名，命令会红）。
    r = _runner().invoke(app, [
        "new", "点赞功能",
        "--roles", "pm,backend,frontend,tester,moderator",
        "--workspace", str(tmp_dir),
    ])
    assert r.exit_code == 0, r.output

    # 骨架应在 workspace 下建 t-xxx 目录并有 events.db。
    threads = [p for p in tmp_dir.iterdir() if p.is_dir() and p.name.startswith("t-")]
    assert threads, "orch new 应建 threads/t-xxx 目录"
    tdir = threads[0]
    assert (tdir / "events.db").exists()

    # E1 = human assign 入队。
    store = orch.store.Store(tdir)
    events = store.events()
    assert events, "orch new 应种入 E1"
    e1 = events[0]
    assert e1.get("from") == "human"
    assert e1.get("type") == "assign"


# ——————————————————————————————————————————————————————————————
# (e-2) orch send：人类发言入队
# ——————————————————————————————————————————————————————————————

def test_cli_send_inserts_human_event(tmp_dir):
    """§12 send：从 human 发一条事件到指定 to（或兜底 moderator）。"""
    app = _get_app()
    # 先 new
    _runner().invoke(app, [
        "new", "点赞", "--roles", "pm,moderator",
        "--workspace", str(tmp_dir),
    ])
    tid = next(p.name for p in tmp_dir.iterdir()
               if p.is_dir() and p.name.startswith("t-"))

    r = _runner().invoke(app, [
        "send", tid, "请启动",
        "--to", "pm",
        "--workspace", str(tmp_dir),
    ])
    assert r.exit_code == 0, r.output

    store = orch.store.Store(tmp_dir / tid)
    events = store.events()
    # 应存在一条 human → pm 的事件。
    assert any(
        ev.get("from") == "human" and "pm" in (ev.get("to") or [])
        and ev.get("body") == "请启动"
        for ev in events
    ), "orch send 应插入 human → pm 事件"


# ——————————————————————————————————————————————————————————————
# (e-3) orch status：派发表 + 状态
# ——————————————————————————————————————————————————————————————

def test_cli_status_prints_dispatch_table_and_thread_meta(tmp_dir):
    """§12 status：应输出线程 status（running/suspended/terminated）与派发行摘要。"""
    app = _get_app()
    _runner().invoke(app, [
        "new", "点赞", "--roles", "pm,moderator",
        "--workspace", str(tmp_dir),
    ])
    tid = next(p.name for p in tmp_dir.iterdir()
               if p.is_dir() and p.name.startswith("t-"))

    r = _runner().invoke(app, [
        "status", tid, "--workspace", str(tmp_dir),
    ])
    assert r.exit_code == 0, r.output
    # 输出至少包含 status 字段与线程 id / 事件条数字样（骨架，宽松断言）。
    assert tid in r.output
    lowered = r.output.lower()
    assert "status" in lowered or "状态" in r.output


# ——————————————————————————————————————————————————————————————
# (e-4) orch approve / reject：门禁裁决
# ——————————————————————————————————————————————————————————————

def _seed_gate_request(store, corr: str, sender: str = "moderator") -> int:
    """种入一条 gate_request(to=[human], corr=corr) + 手工置 gate_wait + suspended。"""
    eid = store.append_event(
        sender=sender, type="gate_request", body="need approval",
        to=["human"], corr=corr,
    )
    store.mark_gate_wait(eid, "human")
    store.set_meta("status", "suspended")
    return eid


def test_cli_approve_invokes_apply_gate_decision_and_resumes(tmp_dir):
    """§12 approve：走 orch.scheduler.apply_gate_decision → gate_decision 入队 + resume。"""
    app = _get_app()
    _runner().invoke(app, [
        "new", "点赞", "--roles", "pm,moderator",
        "--workspace", str(tmp_dir),
    ])
    tid = next(p.name for p in tmp_dir.iterdir()
               if p.is_dir() and p.name.startswith("t-"))

    tdir = tmp_dir / tid
    store = orch.store.Store(tdir)
    _seed_gate_request(store, corr="gate-42")

    r = _runner().invoke(app, [
        "approve", "gate-42",
        "--thread", tid,
        "--workspace", str(tmp_dir),
    ])
    assert r.exit_code == 0, r.output

    # 复读一次 store：应产生 gate_decision 事件、线程 resume。
    store2 = orch.store.Store(tdir)
    assert any(
        ev.get("type") == "gate_decision" and ev.get("corr") == "gate-42"
        and ev.get("body") == "approve"
        for ev in store2.events()
    ), "approve 应产生 gate_decision(approve) 事件"
    assert store2.get_meta("status") == "running"


def test_cli_reject_produces_gate_decision_reject(tmp_dir):
    """§12 reject：gate_decision(reject) + resume；不执行特权操作。"""
    app = _get_app()
    _runner().invoke(app, [
        "new", "点赞", "--roles", "pm,moderator",
        "--workspace", str(tmp_dir),
    ])
    tid = next(p.name for p in tmp_dir.iterdir()
               if p.is_dir() and p.name.startswith("t-"))

    tdir = tmp_dir / tid
    store = orch.store.Store(tdir)
    _seed_gate_request(store, corr="gate-9")

    r = _runner().invoke(app, [
        "reject", "gate-9",
        "--thread", tid,
        "--workspace", str(tmp_dir),
    ])
    assert r.exit_code == 0, r.output

    store2 = orch.store.Store(tdir)
    assert any(
        ev.get("type") == "gate_decision" and ev.get("corr") == "gate-9"
        and ev.get("body") == "reject"
        for ev in store2.events()
    )
    assert store2.get_meta("status") == "running"


# ——————————————————————————————————————————————————————————————
# (e-5) orch stop：优雅停机（写元数据）
# ——————————————————————————————————————————————————————————————

def test_cli_stop_marks_daemon_stop_flag(tmp_dir):
    """§12 stop：优雅停机。骨架允许写 workspace 级停机标志或每线程 meta，
    实现细节由 T3/T4 决定；本卡只断言"exit 0 + 有持久化痕迹"。"""
    app = _get_app()
    _runner().invoke(app, [
        "new", "点赞", "--roles", "pm,moderator",
        "--workspace", str(tmp_dir),
    ])
    r = _runner().invoke(app, ["stop", "--workspace", str(tmp_dir)])
    assert r.exit_code == 0, r.output
    # 骨架允许多种落盘方式；宽松断言：至少输出提示或写了 workspace 的 stop 标志文件。
    marker = tmp_dir / "orch.stop"
    stopped_via_marker = marker.exists()
    stopped_via_stdout = ("stop" in r.output.lower()) or ("停机" in r.output)
    assert stopped_via_marker or stopped_via_stdout


# ——————————————————————————————————————————————————————————————
# (e-6) orch attach：打印会话接入命令（sid=None 兜底）
# ——————————————————————————————————————————————————————————————

def test_cli_attach_prints_resume_command_when_sid_present(tmp_dir):
    """§12 attach：打印该角色原生会话接入命令（如 `claude --resume <sid>`）。
    有 sid → 输出中含 sid；无 sid → 输出兜底提示（"暂无 sid" / 冷启动示例）。"""
    app = _get_app()
    _runner().invoke(app, [
        "new", "点赞", "--roles", "pm,moderator",
        "--workspace", str(tmp_dir),
    ])
    tid = next(p.name for p in tmp_dir.iterdir()
               if p.is_dir() and p.name.startswith("t-"))

    tdir = tmp_dir / tid
    store = orch.store.Store(tdir)
    # 用 reply_and_done 顺手 upsert 一行 sessions（契约 §2）。
    # 先种一条事件用于 reply_and_done 的 done_event_id 挂钩：
    eid = store.append_event(
        sender="human", type="assign", body="prep", to=["backend"],
    )
    store.mark_dispatching(eid, "backend", 0.0)
    store.reply_and_done(
        done_event_id=eid, done_target="backend",
        reply={"from": "backend", "to": ["pm"], "type": "report", "body": "x",
               "re": [eid]},
        session={"role": "backend", "backend": "claude", "sid": "abc-xyz",
                 "last_evt": eid, "gen": 1},
    )

    r = _runner().invoke(app, [
        "attach", tid, "backend",
        "--workspace", str(tmp_dir),
    ])
    assert r.exit_code == 0, r.output
    assert "abc-xyz" in r.output, "attach 应打印 sid"


def test_cli_attach_prints_fallback_when_sid_none(tmp_dir):
    """§12 attach 兜底：无 sid 时也必须打印 something useful（冷启动示例或明确提示）。
    契约 §5：`attach 打印真实会话接入命令(即使 sid=None 也要有兜底提示)`。"""
    app = _get_app()
    _runner().invoke(app, [
        "new", "点赞", "--roles", "backend,pm,moderator",
        "--workspace", str(tmp_dir),
    ])
    tid = next(p.name for p in tmp_dir.iterdir()
               if p.is_dir() and p.name.startswith("t-"))
    # 没有 sessions 行：sid=None。
    r = _runner().invoke(app, [
        "attach", tid, "backend",
        "--workspace", str(tmp_dir),
    ])
    assert r.exit_code == 0, r.output
    # 兜底文案任意一种命中即可（不锁死措辞）：说明无 sid + 或给冷启动提示。
    out = r.output.lower()
    assert (
        "no sid" in out or "sid=none" in out or "暂无" in r.output
        or "冷启动" in r.output or "cold" in out
        or "start_cmd" in out or "no session" in out
    ), f"attach 无 sid 时应有兜底提示，实际输出: {r.output}"


# ——————————————————————————————————————————————————————————————
# (e-7) orch threads：列线程
# ——————————————————————————————————————————————————————————————

def test_cli_threads_lists_existing(tmp_dir):
    """§12 threads：列出 workspace 下所有线程（t-xxx 目录）。"""
    app = _get_app()
    _runner().invoke(app, [
        "new", "任务1", "--roles", "pm", "--workspace", str(tmp_dir),
    ])
    _runner().invoke(app, [
        "new", "任务2", "--roles", "pm", "--workspace", str(tmp_dir),
    ])
    ids = sorted(p.name for p in tmp_dir.iterdir()
                 if p.is_dir() and p.name.startswith("t-"))
    assert len(ids) >= 2

    r = _runner().invoke(app, ["threads", "--workspace", str(tmp_dir)])
    assert r.exit_code == 0, r.output
    for tid in ids:
        assert tid in r.output, f"threads 应列出 {tid}"


# ——————————————————————————————————————————————————————————————
# (e-8) orch chat：事件日志 → 群聊渲染
# ——————————————————————————————————————————————————————————————

def test_cli_chat_renders_events_as_chat(tmp_dir):
    """§12 chat：把事件日志渲染为群聊（气泡=信封投影；@ 由 to 渲染，§16.1）。"""
    app = _get_app()
    _runner().invoke(app, [
        "new", "点赞", "--roles", "pm,moderator",
        "--workspace", str(tmp_dir),
    ])
    tid = next(p.name for p in tmp_dir.iterdir()
               if p.is_dir() and p.name.startswith("t-"))

    # 手工插一条 human → pm 供渲染。
    store = orch.store.Store(tmp_dir / tid)
    store.append_event(
        sender="human", type="assign", body="启动一下", to=["pm"],
    )

    r = _runner().invoke(app, [
        "chat", tid, "--workspace", str(tmp_dir),
    ])
    assert r.exit_code == 0, r.output
    # 输出至少含 human/pm 标签（§16.1：@ 来源 to 字段渲染）与消息正文。
    assert ("human" in r.output.lower() or "[human" in r.output.lower())
    assert "pm" in r.output.lower()
    assert "启动一下" in r.output


# ——————————————————————————————————————————————————————————————
# (C-2) orch run：常驻调度进程（spec §12 明列）
# ——————————————————————————————————————————————————————————————
#
# 三维评审 C-2：spec §12 + M2 契约 §5 明列 `orch run` 常驻命令；`orch stop`
# 写 orch.stop 标志文件但**无人读**（无 run 进程消费）。修复：新增 `orch run
# --workspace <ws> [--once]` 命令，装配默认 Fake adapters 跑一轮 run_thread；
# 若 orch.stop 标志存在则立即 return，且**消费**掉该标志（避免下次启动误触发）。
# ——————————————————————————————————————————————————————————————


def _seed_thread_with_pending(workspace: Path) -> tuple[str, "orch.store.Store"]:
    """辅助：workspace 下建一条线程 + 一条 pending 事件供 orch run 消费。"""
    app = _get_app()
    _runner().invoke(app, [
        "new", "run-once test", "--roles", "pm,moderator",
        "--workspace", str(workspace),
    ])
    tid = next(p.name for p in workspace.iterdir()
               if p.is_dir() and p.name.startswith("t-"))
    store = orch.store.Store(workspace / tid)
    return tid, store


def test_cli_run_once_processes_pending_and_updates_status(tmp_dir):
    """§12 orch run：`orch run --workspace <ws> --once` 应装配默认 Fake adapters
    跑一轮 run_thread → 消费 pending 派发行 → 线程 status 从 running 变化
    （或至少 pending 数量下降，或线程被显式挂起/终止）。

    骨架允许两种落地方式：
      · 默认 fake：`orch new` 后 pending 会被处理（Fake* 恒回 chat 型信封），
        因此断言：exit 0 + 有至少一次 pending 消费的可观察副作用（events 数增加
        或 pending 数下降或状态变更）。
    """
    app = _get_app()
    tid, store_before = _seed_thread_with_pending(tmp_dir)
    events_before = len(store_before.events())
    pending_before = len(store_before.pending_dispatches())
    assert pending_before >= 1, "预置：应至少有 1 条 pending 供 run 消费"

    r = _runner().invoke(app, [
        "run", "--workspace", str(tmp_dir), "--once",
    ])
    assert r.exit_code == 0, r.output

    # run --once 应至少：跑一轮 run_thread → 有可观察副作用（events 增或 pending 减
    # 或 status 已流转到 suspended/terminated）。
    store_after = orch.store.Store(tmp_dir / tid)
    events_after = len(store_after.events())
    pending_after = len(store_after.pending_dispatches())
    status_after = store_after.get_meta("status")
    assert (
        events_after > events_before
        or pending_after < pending_before
        or status_after in ("suspended", "terminated")
    ), (
        f"orch run --once 应消费至少一条 pending：events {events_before}→{events_after}, "
        f"pending {pending_before}→{pending_after}, status={status_after}"
    )


def test_cli_run_respects_stop_flag_and_consumes_it(tmp_dir):
    """§12 orch stop + orch run：orch.stop 标志存在时 `orch run` 立即退出，
    且**消费**该标志（删除/移动/带时间戳后缀等）——避免下次启动被历史标志误停。

    契约：
      · run 检测到 orch.stop 存在 → 立即 return 0，不做任何 pending 消费。
      · 退出后 orch.stop 标志应被清理（不再存在于原路径），保证 stop 语义为
        "一次性触发"，而非"永久停机"。
    """
    app = _get_app()
    tid, store_before = _seed_thread_with_pending(tmp_dir)
    pending_before = len(store_before.pending_dispatches())
    assert pending_before >= 1

    # 先 orch stop 写标志。
    r_stop = _runner().invoke(app, ["stop", "--workspace", str(tmp_dir)])
    assert r_stop.exit_code == 0, r_stop.output
    marker = tmp_dir / "orch.stop"
    assert marker.exists(), "orch stop 应先写 orch.stop 标志"

    # 再 orch run：应立即退出，不消费 pending。
    r_run = _runner().invoke(app, [
        "run", "--workspace", str(tmp_dir), "--once",
    ])
    assert r_run.exit_code == 0, r_run.output

    store_after = orch.store.Store(tmp_dir / tid)
    pending_after = len(store_after.pending_dispatches())
    assert pending_after == pending_before, (
        f"orch.stop 标志存在时 orch run 应立即退出，不消费 pending；"
        f"实际 pending {pending_before}→{pending_after}"
    )
    # 标志已被消费（不再存在），下次 run 不会被历史 stop 误触发。
    assert not marker.exists(), (
        "orch run 检测到 orch.stop 后应消费该标志（删除），"
        "避免下次 run 被历史 stop 误触发。"
    )
