"""M3-T1 · §9.3 多线程并发互不干扰验收测试——测试先行，见红。

覆盖任务卡 (d)：
  两个 Store 各自独立 thread_dir，run_thread_async 各跑一个 fixture，
  断言：事件/黑板互不污染（一线程一目录一 db，§4.1/§9.3）。

硬约束（CLAUDE.md / M3 契约 §3）：
  - 顶层只 `import orch.scheduler / orch.store / orch.adapters`；M3 符号
    `run_thread_async` 在**函数体内**引用；未实现表现为运行时红。
  - 并发用 asyncio.gather 两条协程；不 sleep 假证；直接观察落盘真相。
  - 每线程独立 thread_dir → 天然隔离（§4.1）；测试只验证隔离**未破**。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orch.adapters
import orch.scheduler
import orch.store


def _config_with_role(role: str, write_scope: list[str]) -> dict:
    return {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {},
        "roles": {
            role: {"can_decide": True, "write_scope": write_scope},
            "moderator": {"can_decide": True, "write_scope": []},
        },
    }


def _seed_single_role_thread(store, target_role: str, body: str) -> int:
    """铺一条 human→target_role 的 assign 事件；返回 event_id。"""
    return store.append_event(
        sender="human", type="assign", body=body, to=[target_role],
    )


def _adapters(role: str, decision_text: str) -> dict:
    """构造：target_role 回 decision（带 bb_ops set_decision）+ moderator 收尾 terminate。"""
    ad_role = orch.adapters.FakeApiAdapter(
        role=role,
        config={"kind": "api"},
        scripted_reply={
            "to": ["moderator"],
            "type": "decision",
            "body": f"{role} decision",
            "blackboard_ops": [
                {"op": "set_decision", "text": decision_text},
            ],
        },
    )
    ad_mod = orch.adapters.FakeApiAdapter(
        role="moderator",
        config={"kind": "api"},
        scripted_reply={"to": [], "type": "terminate", "body": "done"},
    )
    return {role: ad_role, "moderator": ad_mod}


# ==================================================================
# (d) §9.3 双线程并发互不干扰
# ==================================================================

def test_two_threads_events_isolated(tmp_dir):
    """§9.3：两个 Store 各自 thread_dir，事件流互不污染。"""
    dir_a = tmp_dir / "t-A"
    dir_b = tmp_dir / "t-B"

    store_a = orch.store.Store(dir_a)
    store_b = orch.store.Store(dir_b)
    store_a.set_meta("status", "running")
    store_b.set_meta("status", "running")

    _seed_single_role_thread(store_a, "pm", "task A")
    _seed_single_role_thread(store_b, "backend", "task B")

    cfg_a = _config_with_role("pm", ["docs/"])
    cfg_b = _config_with_role("backend", ["server/"])

    async def _run_both():
        await asyncio.gather(
            orch.scheduler.run_thread_async(store_a, cfg_a, _adapters("pm", "A-decided")),
            orch.scheduler.run_thread_async(store_b, cfg_b, _adapters("backend", "B-decided")),
        )

    asyncio.run(_run_both())

    events_a = store_a.events()
    events_b = store_b.events()

    # 每线程应只含自己的事件，body 不互穿。
    bodies_a = [ev.get("body", "") for ev in events_a]
    bodies_b = [ev.get("body", "") for ev in events_b]
    assert any("task A" in b for b in bodies_a)
    assert not any("task A" in b for b in bodies_b), "§9.3：线程 B 不应含 A 的事件"
    assert any("task B" in b for b in bodies_b)
    assert not any("task B" in b for b in bodies_a), "§9.3：线程 A 不应含 B 的事件"

    # 每线程 events 表主键独立（都从 1 起自增，无跨线程污染）。
    ids_a = [ev["id"] for ev in events_a]
    ids_b = [ev["id"] for ev in events_b]
    assert ids_a[0] == 1 and ids_b[0] == 1, \
        "§4.1/§9.3：一线程一 db，event id 独立自增"


def test_two_threads_blackboard_isolated(tmp_dir):
    """§9.3：两线程黑板 state.json 互不污染。"""
    dir_a = tmp_dir / "t-A"
    dir_b = tmp_dir / "t-B"

    store_a = orch.store.Store(dir_a)
    store_b = orch.store.Store(dir_b)
    store_a.set_meta("status", "running")
    store_b.set_meta("status", "running")

    _seed_single_role_thread(store_a, "pm", "task A")
    _seed_single_role_thread(store_b, "backend", "task B")

    cfg_a = _config_with_role("pm", ["docs/"])
    cfg_b = _config_with_role("backend", ["server/"])

    async def _run_both():
        await asyncio.gather(
            orch.scheduler.run_thread_async(store_a, cfg_a, _adapters("pm", "A-decided")),
            orch.scheduler.run_thread_async(store_b, cfg_b, _adapters("backend", "B-decided")),
        )

    asyncio.run(_run_both())

    state_a = orch.store.board_state(store_a)
    state_b = orch.store.board_state(store_b)

    decisions_a = [d.get("text") for d in (state_a.get("decisions") or [])]
    decisions_b = [d.get("text") for d in (state_b.get("decisions") or [])]

    assert "A-decided" in decisions_a
    assert "B-decided" in decisions_b
    assert "B-decided" not in decisions_a, "§9.3：黑板互不污染（A 不含 B 的决策）"
    assert "A-decided" not in decisions_b, "§9.3：黑板互不污染（B 不含 A 的决策）"

    # 黑板文件路径也应各自独立。
    board_a = (Path(dir_a) / "blackboard" / "board.md").read_text(encoding="utf-8")
    board_b = (Path(dir_b) / "blackboard" / "board.md").read_text(encoding="utf-8")
    assert "A-decided" in board_a and "B-decided" not in board_a
    assert "B-decided" in board_b and "A-decided" not in board_b
