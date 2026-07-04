"""调度层 · 恢复对账验收测试（spec §9.1、§9.4；并含 §5.1 核心环骨架断言）。

覆盖任务卡条目 (d)：§9.1 恢复对账**全情形**——
  - suspended 线程：保持挂起，gate_wait 行不动（不落入 dispatching 循环）。
  - a) 存在 sender=T 且 n ∈ re 的回复 → 补标 done（纵深防御）。
  - b) now > deadline_ts → 看门狗路径（attempt+1）。
  - c) 其余 → status → pending，重派发。
  - 黑板缺失/损坏 → rebuild_blackboard。

硬约束：顶层只 import orch.scheduler / orch.store；符号在函数体内引用。
恢复"禁止猜测"（§16.10）——测试只以查表结果为准。派发行状态直接读 dispatches 表
（观察落盘真相，非私有实现）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import orch.scheduler  # 包级导入
import orch.store


# —— 直接读 dispatches 真相表（契约未暴露"按 id 查 status"，读盘合法）——
def _dispatch_row(thread_dir: Path, event_id: int, target: str) -> dict | None:
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        con.row_factory = sqlite3.Row
        r = con.execute(
            "SELECT event_id,target,status,deadline_ts,attempts "
            "FROM dispatches WHERE event_id=? AND target=?",
            (event_id, target),
        ).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _force_dispatching(store, thread_dir, event_id, target, deadline_ts):
    """把某派发行强制置为 dispatching + 指定 deadline（模拟崩溃在 invoke 中）。"""
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        con.execute(
            "UPDATE dispatches SET status='dispatching', deadline_ts=? "
            "WHERE event_id=? AND target=?",
            (deadline_ts, event_id, target),
        )
        con.commit()
    finally:
        con.close()


def _config():
    # M0 恢复只需线程默认；gate_ops 用跨平台无害命令（契约 §6.5）。
    return {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {},
    }


# ——————————————————————————————————————————————————————————————
# §9.1 恢复情形 c)：dispatching 且未超时、无回复 → 转 pending
# ——————————————————————————————————————————————————————————————

def test_recover_case_c_requeues_to_pending(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    # 未来 deadline（不超时）、无回复 → 情形 c。
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() + 10_000)

    orch.scheduler.recover(st, _config())

    row = _dispatch_row(thread_dir, e1, "backend")
    assert row["status"] == "pending", "情形 c：应重置为 pending"
    # 且回到 pending 列表，主循环可接手。
    assert any(d["event_id"] == e1 and d["target"] == "backend"
               for d in st.pending_dispatches())


# ——————————————————————————————————————————————————————————————
# §9.1 恢复情形 a)：存在 sender=T 且 n∈re 的回复 → 补标 done
# ——————————————————————————————————————————————————————————————

def test_recover_case_a_backfills_done_when_reply_exists(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() + 10_000)
    # 回复已落盘：sender=backend 且 re 含 e1（崩溃发生在"标 done"之前的旧模型；
    # 合并事务后理论不出现，作纵深防御，§9.1 a）。
    st.append_event(sender="backend", type="handoff", body="done", to=["tester"], re=[e1])

    orch.scheduler.recover(st, _config())

    row = _dispatch_row(thread_dir, e1, "backend")
    assert row["status"] == "done", "情形 a：有对应回复应补标 done"


def test_recover_case_a_ignores_unrelated_reply(thread_dir):
    # 回复 sender 对但 re 不含 n，或 re 含 n 但 sender 不对 → 不算 a，落 c。
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() + 10_000)
    # sender 对但 re 不含 e1。
    st.append_event(sender="backend", type="report", body="别的", to=["moderator"], re=[999])
    # re 含 e1 但 sender 非目标 backend。
    st.append_event(sender="frontend", type="report", body="别的2", to=["moderator"], re=[e1])

    orch.scheduler.recover(st, _config())

    row = _dispatch_row(thread_dir, e1, "backend")
    assert row["status"] == "pending", "无匹配回复（sender=T 且 n 在 re 内）时不补 done，应转 pending"


# ——————————————————————————————————————————————————————————————
# §9.1 恢复情形 b)：now > deadline_ts → 看门狗路径（attempt+1）
# ——————————————————————————————————————————————————————————————

def test_recover_case_b_watchdog_bumps_attempt(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    before = _dispatch_row(thread_dir, e1, "backend")["attempts"]
    # 过去 deadline（已超时）、无回复 → 情形 b：看门狗计一次 attempt。
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() - 10_000)

    orch.scheduler.recover(st, _config())

    after = _dispatch_row(thread_dir, e1, "backend")
    assert after["attempts"] == before + 1, "情形 b：看门狗路径应 attempts+1"


def test_recover_case_b_precedence_when_deadline_passed_and_no_reply(thread_dir):
    # 超时且无回复：必须走 b（看门狗），不得直接当 c 简单 requeue 而漏计 attempt。
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() - 5.0)
    before = _dispatch_row(thread_dir, e1, "backend")["attempts"]

    orch.scheduler.recover(st, _config())

    after = _dispatch_row(thread_dir, e1, "backend")["attempts"]
    assert after == before + 1


# ——————————————————————————————————————————————————————————————
# §9.1 suspended：保持挂起，gate_wait 行不动
# ——————————————————————————————————————————————————————————————

def test_recover_suspended_keeps_gate_wait_untouched(thread_dir):
    st = orch.store.Store(thread_dir)
    e1 = st.append_event(sender="moderator", type="gate_request", body="批准?", to=["human"])
    st.mark_gate_wait(e1, "human")
    st.set_meta("status", "suspended")

    orch.scheduler.recover(st, _config())

    row = _dispatch_row(thread_dir, e1, "human")
    assert row["status"] == "gate_wait", "suspended 恢复：gate_wait 行必须保持不动"
    assert st.get_meta("status") == "suspended", "suspended 线程恢复后仍挂起"


def test_recover_suspended_does_not_touch_other_dispatching(thread_dir):
    # 挂起线程：恢复直接返回，连同期的 dispatching 行也不对账（§9.1：suspended → 保持挂起直接返回）。
    st = orch.store.Store(thread_dir)
    g = st.append_event(sender="moderator", type="gate_request", body="批?", to=["human"])
    st.mark_gate_wait(g, "human")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() + 10_000)
    st.set_meta("status", "suspended")

    orch.scheduler.recover(st, _config())

    # gate_wait 不动；dispatching 行保持（因挂起时恢复不处理派发循环）。
    assert _dispatch_row(thread_dir, g, "human")["status"] == "gate_wait"
    assert _dispatch_row(thread_dir, e1, "backend")["status"] == "dispatching"


# ——————————————————————————————————————————————————————————————
# §9.1 黑板缺失/损坏 → rebuild_blackboard
# ——————————————————————————————————————————————————————————————

def test_recover_rebuilds_blackboard_when_state_missing(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 落一条 A 类决策事件（冻结 v1）。
    e1 = st.append_event(sender="pm", type="decision", body="冻结", to=["moderator"],
                         blackboard_ops=[{"op": "freeze_contract", "name": "like-api",
                                          "path": "docs/like-api.md", "version": 1}])
    orch.store.apply_blackboard_ops(
        st, [{"op": "freeze_contract", "name": "like-api",
              "path": "docs/like-api.md", "version": 1}], e1)
    # 删除 state.json 模拟黑板缺失。
    (thread_dir / "blackboard" / "state.json").unlink()

    orch.scheduler.recover(st, _config())

    state = orch.store.board_state(st)
    assert state["contracts"]["like-api"]["version"] == 1, \
        "黑板缺失恢复后应由日志重放重建"


def test_recover_rebuilds_blackboard_when_state_corrupt(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="pm", type="decision", body="冻结", to=["moderator"],
                         blackboard_ops=[{"op": "freeze_contract", "name": "like-api",
                                          "path": "docs/like-api.md", "version": 2}])
    orch.store.apply_blackboard_ops(
        st, [{"op": "freeze_contract", "name": "like-api",
              "path": "docs/like-api.md", "version": 2}], e1)
    # 写入损坏 JSON。
    (thread_dir / "blackboard" / "state.json").write_text("{ this is not json",
                                                          encoding="utf-8")

    orch.scheduler.recover(st, _config())

    state = orch.store.board_state(st)
    assert state["contracts"]["like-api"]["version"] == 2


# ——————————————————————————————————————————————————————————————
# §9.1 pending 行不处理（主循环接手）
# ——————————————————————————————————————————————————————————————

def test_recover_leaves_pending_untouched(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    # 保持 pending，不转 dispatching。
    orch.scheduler.recover(st, _config())
    row = _dispatch_row(thread_dir, e1, "backend")
    assert row["status"] == "pending", "pending 行恢复时不处理"
