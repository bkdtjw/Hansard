"""M2 T1 · 停机-续跑 端到端验收（M2 契约 §1(e)/§6；spec §10 停机三小时 + §5.5）。

覆盖任务卡条目 (f)：
  FakeCliAdapter + FakeApiAdapter 组合，简化 fixture 端到端跑通控制流：
    assign → review → handoff → acceptance → gate_request → (挂起：模拟停机=重开 Store)
      → apply_gate_decision → 续跑 → terminate。
  ledger/黑板终态一致，事件类型序列可断言。

M2 边界（任务卡红线）：
  - 不启真实 CLI 子进程；FakeCliAdapter 全程模拟。
  - "停机" 用 store 重开（新 Store 实例）模拟；进程侧完全无状态（§0/§16.9）。

硬约束（契约 §1）：
  - 顶层只 `import orch.scheduler / orch.adapters / orch.store`；具体符号函数体内引用。
  - 断言只观察落盘真相（events / 派发表 / thread_meta），不依赖内部实现细节。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import orch.adapters
import orch.scheduler
import orch.store

# 测试层适配器桩：多回复脚本 + 越权注入（M2 契约 §2）。M2 T5 任务卡仅可写 tests/，
# src Fake* 目前只支持单次 scripted_output/scripted_reply → 测试层薄包装桩承载
# call_no-envelope 分发（不弱化断言：调度层路由/落盘/审计仍走 src 权威路径）。
from tests.adapters_helpers import (
    MultiReplyFakeApiAdapter,
    MultiReplyFakeCliAdapter,
)


def _git(cwd, *args) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )
    return proc.stdout


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for d in ("server", "tests", "docs"):
        (root / d).mkdir()
        (root / d / ".gitkeep").write_text("", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    return root


def _make_wt(target: Path, name: str) -> Path:
    wt = target.parent / name
    _git(target, "worktree", "add", "-b", f"feat/{name}", str(wt), "main")
    _git(wt, "config", "user.email", "t@example.com")
    _git(wt, "config", "user.name", "t")
    return wt


def _cfg(target_repo: Path) -> dict:
    return {
        "target_repo": str(target_repo),
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        # gate_ops：CI 类无害命令。用于 approve 后系统执行器回调（§5.5）。
        "gate_ops": {
            "run_ci": {"cmd": "python -c \"print('ci ok')\"", "cwd": ".",
                       "async": True},
        },
        "roles": {
            "moderator": {"can_decide": True, "write_scope": [], "tools": [],
                          "adapter": "api"},
            "pm": {"can_decide": True, "write_scope": ["docs/"],
                   "tools": ["Edit", "Write"], "adapter": "cli"},
            "backend": {"can_decide": False, "write_scope": ["server/"],
                        "tools": ["Edit", "Write"], "adapter": "cli"},
            "tester": {"can_decide": False, "write_scope": ["tests/", "reports/"],
                       "tools": ["Edit", "Write"], "adapter": "cli"},
        },
    }


def _fake_cli(role: str, wt: Path, scripted: dict) -> MultiReplyFakeCliAdapter:
    """构造 CLI 型测试双：scripted 为逐次调用返回的信封字典表 {call_no: env}。

    行为等价 M2 契约 §2 描述的 FakeCliAdapter（cwd=worktree，超时可模拟，
    session_id 可提取）；本包装承载多回复语义（src Fake* 只支持单次输出）。
    """
    return MultiReplyFakeCliAdapter(
        role=role,
        config={"kind": "cli", "start_cmd": "fake", "timeout_s": 10},
        worktree=wt,
        scripted_replies=scripted,
    )


def _fake_api(role: str, scripted: dict) -> MultiReplyFakeApiAdapter:
    """构造 API 型测试双（§7.3 单步，无会话，supports_resume=False）。"""
    return MultiReplyFakeApiAdapter(
        role=role, config={"kind": "api"},
        scripted_replies=scripted,
    )


# ——————————————————————————————————————————————————————————————
# 端到端：assign→review→handoff→acceptance→gate_request→挂起→approve→terminate
# ——————————————————————————————————————————————————————————————

def test_e2e_suspend_reopen_resume_terminate(tmp_dir):
    target = _init_repo(tmp_dir / "target")

    # 三个 CLI 型角色各一 worktree。
    wt_pm = _make_wt(target, "t001-pm")
    wt_be = _make_wt(target, "t001-backend")
    wt_te = _make_wt(target, "t001-tester")

    # 事件驱动脚本（按 (role, call_no) 返回信封；FakeCliAdapter 按调用顺序发返）。
    # 简化流程（M2 契约 §1(e)）：
    #   E1  human → moderator     assign  "点赞功能"（兜底路由）
    #   E2  moderator → pm        assign
    #   E3  pm → backend          review  "契约 v1"
    #   E4  backend → tester      handoff "已实现"
    #   E5  tester → moderator    acceptance
    #   E6  moderator → human     gate_request corr=gate-01 → suspend
    #   ── 模拟停机：重开 store ──
    #   apply_gate_decision(corr=gate-01, approve=True) → E7 gate_decision + E8 system(CI 回调)
    #   续跑：moderator 收到 E7 或 E8 后回 terminate（E9） → 触发终止清单（system 汇总 = E10）。
    scripts = {
        "moderator": {
            1: {"to": ["pm"], "type": "assign", "body": "start"},                # 回 E1
            2: {"to": ["human"], "type": "gate_request",                          # 回 E5
                "body": "请裁决是否合入", "corr": "gate-01"},
            3: {"to": [], "type": "terminate", "body": "完成"},                   # 续跑后
        },
        "pm": {
            1: {"to": ["backend"], "type": "review", "body": "契约 v1"},          # 回 E2
        },
        "backend": {
            1: {"to": ["tester"], "type": "handoff", "body": "已实现"},          # 回 E3
        },
        "tester": {
            1: {"to": ["moderator"], "type": "acceptance", "body": "通过"},      # 回 E4
        },
    }

    adapters = {
        "moderator": _fake_api("moderator", scripts["moderator"]),
        "pm": _fake_cli("pm", wt_pm, scripts["pm"]),
        "backend": _fake_cli("backend", wt_be, scripts["backend"]),
        "tester": _fake_cli("tester", wt_te, scripts["tester"]),
    }

    thread_dir = tmp_dir / "t-001"
    store = orch.store.Store(thread_dir)
    store.set_meta("status", "running")

    # E1：human → ∅（兜底路由 → moderator）。
    store.append_event(
        sender="human", type="assign", body="帖子支持点赞/取消赞", to=[],
    )

    # 跑到 suspended（触发 gate_request）。
    orch.scheduler.run_thread(store, _cfg(target), adapters)

    # —— 断言"挂起点"：thread status='suspended'；events 含 gate_request(corr=gate-01)。
    assert store.get_meta("status") == "suspended"
    events_at_suspend = store.events()
    types_seen = [ev.get("type") for ev in events_at_suspend]
    assert "assign" in types_seen
    assert "review" in types_seen
    assert "handoff" in types_seen
    assert "acceptance" in types_seen
    assert "gate_request" in types_seen
    gate_evs = [ev for ev in events_at_suspend
                if ev.get("type") == "gate_request" and ev.get("corr") == "gate-01"]
    assert gate_evs, "应有 gate_request(corr=gate-01)"

    # —— 模拟停机：重开 Store（新实例）；进程侧无状态（§0/§16.9）——
    del store
    store2 = orch.store.Store(thread_dir)
    # 应仍是 suspended，恢复算法应保持挂起（§9.1）。
    assert store2.get_meta("status") == "suspended"

    # 恢复/续跑：apply_gate_decision approve → 产生 gate_decision + 系统执行器 CI 回调。
    orch.scheduler.apply_gate_decision(
        store2, _cfg(target), adapters,
        corr="gate-01", approve=True, sender="human",
    )
    # resume 后续跑到 terminate。
    orch.scheduler.run_thread(store2, _cfg(target), adapters)

    # —— 终态断言 —— #
    all_events = store2.events()
    all_types = [ev.get("type") for ev in all_events]

    # 事件类型序列应包含（顺序判断而非逐字节）：
    # assign(E1 兜底 moderator) → assign(E2 mod→pm) → review(E3) → handoff(E4)
    # → acceptance(E5) → gate_request(E6) → gate_decision(E7) → system(CI 回调 E8)
    # → terminate(E9) → system(终止清单 E10)
    def _order_indices(types: list[str], seq: list[str]) -> bool:
        idx = 0
        for t in types:
            if idx < len(seq) and t == seq[idx]:
                idx += 1
        return idx == len(seq)

    expected_order = [
        "assign", "review", "handoff", "acceptance",
        "gate_request", "gate_decision", "system", "terminate", "system",
    ]
    assert _order_indices(all_types, expected_order), (
        f"事件类型序列不满足预期递进：actual={all_types!r}, expected={expected_order!r}"
    )

    # 终态：线程 status='terminated'
    assert store2.get_meta("status") == "terminated"

    # 终态：无 pending 派发行（§5.4：终止后拒绝新派发 + 清理）。
    assert store2.pending_dispatches() == []

    # gate_decision(approve) 存在
    assert any(
        ev.get("type") == "gate_decision" and ev.get("corr") == "gate-01"
        and ev.get("body") == "approve"
        for ev in all_events
    )

    # 系统执行器 CI 回调（system 事件 to=[moderator]）存在
    assert any(
        ev.get("type") == "system" and ev.get("from") == "system"
        and "moderator" in (ev.get("to") or [])
        and ("CI" in ev.get("body", "") or "ci" in ev.get("body", "").lower())
        for ev in all_events
    )

    # 终止清单 system 事件存在（§5.4）
    assert any(
        ev.get("type") == "system" and ("终止清单" in ev.get("body", "")
                                         or "terminate" in ev.get("body", "").lower())
        for ev in all_events
    )
