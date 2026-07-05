"""M1 · §5.3 看门狗三级 验收测试 —— 测试先行，见红。

覆盖任务卡条目 (e)：check_watchdogs(store, config, *, now=None) -> list[dict]
  级别1 单次调用超时：注入 now 假时钟，now > deadline_ts -> attempt+1
                     （**禁止**内存倒计时/sleep：时间只从 now 参数来，§16.2）。
  级别2 互@环路：同一有序对 (A->B) 的 defect 事件数 >= loop_limit(默认3)
                -> 自动 gate_request(to=[human]) + 线程 suspended。
  级别3 全局轮数：线程事件总数 >= max_rounds(默认100) -> 自动 gate_request。

硬约束（契约 §2 / CLAUDE.md / §16）：
  - 顶层只 import orch.scheduler / orch.store（包级）；check_watchdogs 在函数体内引用，
    未实现 -> 运行时红（AttributeError）而非 collection 中断。
  - §16.2 看门狗用**落盘绝对时间戳**（dispatches.deadline_ts），本测试注入假时钟 now，
    绝不 sleep、绝不依赖内存倒计时。
  - 环路/轮数每次从日志现数、不落盘（§5.3）。
  - gate_request 复用 M0 门禁机制 -> 线程 suspended（观察落盘真相：事件表 + thread_meta.status
    + dispatches.gate_wait），不绑定 check_watchdogs 返回值的内部结构细节。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import orch.scheduler  # 包级导入（check_watchdogs 在函数体内引用）
import orch.store

from tests.fixtures.m1_helpers import m1_config, seed_events


# ——————————————————————————————————————————————————————————————
# 只读观察落盘真相（契约 §7：读盘观察合法；写一律走 store 原语）
# ——————————————————————————————————————————————————————————————

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


def _force_dispatching(thread_dir, event_id, target, deadline_ts):
    """把某派发行强制置 dispatching + 指定 deadline（模拟崩溃在 invoke 中）。

    仅测试脚手架直接写盘构造前置状态；被测的 check_watchdogs 只读盘 + 走 store 原语改状态。
    """
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


def _gate_requests_to_human(store) -> list[dict]:
    return [
        e for e in store.events()
        if e["type"] == "gate_request" and "human" in (e.get("to") or [])
    ]


# ==================================================================
# 级别1：单次调用超时 —— 注入假时钟 now > deadline_ts -> attempt+1
# ==================================================================

def test_watchdog_level1_timeout_bumps_attempt_with_injected_now(thread_dir):
    """§5.3 级别1：注入 now，now > deadline_ts -> 看门狗 kill+attempt+1。

    关键：deadline 落在**未来**（相对真实 wall-clock），但注入的 now 更晚 -> 触发；
    证明判定只依赖 now 参数（假时钟），不依赖内存倒计时/sleep（§16.2）。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["backend"])
    before = _dispatch_row(thread_dir, e1, "backend")["attempts"]

    # deadline 在真实时钟的未来（+1000s）；若看门狗错误地用 wall-clock 就不会触发。
    real_future_deadline = time.time() + 1000.0
    _force_dispatching(thread_dir, e1, "backend", real_future_deadline)

    # 注入的假时钟 now 比该 deadline 还要晚 -> 应判超时。
    injected_now = real_future_deadline + 1.0
    orch.scheduler.check_watchdogs(st, m1_config(), now=injected_now)

    after = _dispatch_row(thread_dir, e1, "backend")["attempts"]
    assert after == before + 1, (
        "级别1 注入 now 超时应 attempts+1（时间取自 now 参数，非内存倒计时，§16.2/§5.3）"
    )


def test_watchdog_level1_not_timed_out_when_now_before_deadline(thread_dir):
    """反向对照：注入 now < deadline_ts -> 未超时，attempts 不变。

    证明看门狗按 now 与 deadline 的**大小关系**判定，而非无条件计一次。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["backend"])
    deadline = time.time() + 500.0
    _force_dispatching(thread_dir, e1, "backend", deadline)
    before = _dispatch_row(thread_dir, e1, "backend")["attempts"]

    # 注入 now 在 deadline 之前。
    orch.scheduler.check_watchdogs(st, m1_config(), now=deadline - 100.0)

    after = _dispatch_row(thread_dir, e1, "backend")["attempts"]
    assert after == before, "now < deadline 未超时，attempts 不应变化（§5.3 级别1）"


def test_watchdog_level1_no_sleep_no_wallclock_dependency(thread_dir):
    """§16.2：看门狗判定**即时**返回（不 sleep 倒计时）。

    以"整个 check_watchdogs 调用耗时远小于 deadline 跨度"为可观察证据：deadline 距今 300s，
    但调用应瞬时完成（<5s），证明未按倒计时阻塞等待。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["backend"])
    _force_dispatching(thread_dir, e1, "backend", time.time() + 300.0)

    t0 = time.time()
    orch.scheduler.check_watchdogs(st, m1_config(), now=time.time())
    elapsed = time.time() - t0
    assert elapsed < 5.0, "check_watchdogs 必须即时返回，禁止内存倒计时/sleep（§16.2）"


# ==================================================================
# 级别2：互@环路 —— 同一有序对 (A->B) defect x3 -> gate_request + suspended
# ==================================================================

def test_watchdog_level2_loop_triggers_gate_and_suspend(thread_dir):
    """§5.3 级别2：同一有序对 (tester->backend) 的 defect 数 >= loop_limit(3)
    -> 自动 gate_request(to=[human]) 升级人类 + 线程 suspended。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 三条 tester->backend 的 defect（同一有序对，计数达阈值 3）。
    seed_events(st, [
        {"sender": "tester", "type": "defect", "to": ["backend"], "body": f"缺陷{i}"}
        for i in range(3)
    ])

    orch.scheduler.check_watchdogs(st, m1_config(loop_limit=3))

    # 观察落盘真相：产生了一条 gate_request(to=[human])。
    gates = _gate_requests_to_human(st)
    assert gates, "级别2 环路达阈值应自动产生 gate_request(to=[human])（§5.3）"
    # 复用 M0 门禁机制 -> 线程 suspended。
    assert st.get_meta("status") == "suspended", (
        "级别2 gate_request 复用门禁机制 -> 线程 suspended（契约 §2/§10）"
    )


def test_watchdog_level2_below_limit_no_gate(thread_dir):
    """反向对照：同一有序对 defect 数 < loop_limit 时不触发（避免误升级）。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 仅两条 tester->backend defect（< 3）。
    seed_events(st, [
        {"sender": "tester", "type": "defect", "to": ["backend"], "body": f"缺陷{i}"}
        for i in range(2)
    ])

    orch.scheduler.check_watchdogs(st, m1_config(loop_limit=3))

    assert not _gate_requests_to_human(st), "环路未达阈值不得触发 gate_request（§5.3）"
    assert st.get_meta("status") == "running", "未触发时线程保持 running"


def test_watchdog_level2_counts_per_ordered_pair_not_crosspair(thread_dir):
    """§5.3 级别2 计数按**有序对**：不同对的 defect 不相互累加。

    构造 tester->backend x2 与 tester->frontend x2（各对 < 3），总 defect=4 但无单对达 3
    -> 不触发。防"跨对累加"实现错误。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    seed_events(st, [
        {"sender": "tester", "type": "defect", "to": ["backend"], "body": "b1"},
        {"sender": "tester", "type": "defect", "to": ["frontend"], "body": "f1"},
        {"sender": "tester", "type": "defect", "to": ["backend"], "body": "b2"},
        {"sender": "tester", "type": "defect", "to": ["frontend"], "body": "f2"},
    ])

    orch.scheduler.check_watchdogs(st, m1_config(loop_limit=3))

    assert not _gate_requests_to_human(st), (
        "有序对计数：无单对达阈值时不得触发（跨对不累加，§5.3）"
    )


def test_watchdog_level2_only_defect_counts_toward_loop(thread_dir):
    """§3.2/§5.3：只有 **defect** 计入环路计数；同对的其它 type 不计。

    tester->backend 有 2 条 defect + 1 条 handoff：defect 仅 2（<3）-> 不触发。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    seed_events(st, [
        {"sender": "tester", "type": "defect", "to": ["backend"], "body": "d1"},
        {"sender": "tester", "type": "handoff", "to": ["backend"], "body": "h1"},
        {"sender": "tester", "type": "defect", "to": ["backend"], "body": "d2"},
    ])

    orch.scheduler.check_watchdogs(st, m1_config(loop_limit=3))

    assert not _gate_requests_to_human(st), "非 defect 不计入环路，defect 仅 2 不应触发（§3.2/§5.3）"


# ==================================================================
# 级别3：全局轮数 —— 事件总数 >= max_rounds -> gate_request
# ==================================================================

def test_watchdog_level3_rounds_trigger_gate(thread_dir):
    """§5.3 级别3：线程事件总数 >= max_rounds -> 自动 gate_request(to=[human])。

    用小 max_rounds(5) 便于构造：落 5 条事件 -> 达阈值 -> 触发。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    seed_events(st, [
        {"sender": "backend", "type": "report", "to": ["moderator"], "body": f"r{i}"}
        for i in range(5)
    ])

    orch.scheduler.check_watchdogs(st, m1_config(max_rounds=5))

    assert _gate_requests_to_human(st), (
        "级别3 事件总数达 max_rounds 应自动 gate_request(to=[human])（§5.3）"
    )


def test_watchdog_level3_below_rounds_no_gate(thread_dir):
    """反向对照：事件总数 < max_rounds 时不触发。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    seed_events(st, [
        {"sender": "backend", "type": "report", "to": ["moderator"], "body": f"r{i}"}
        for i in range(3)
    ])

    orch.scheduler.check_watchdogs(st, m1_config(max_rounds=100))

    assert not _gate_requests_to_human(st), "事件总数未达 max_rounds 不得触发（§5.3）"


def test_watchdog_rounds_counted_from_log_not_persisted_counter(thread_dir):
    """§5.3：轮数**从日志现数、不落盘**。

    落 max_rounds 条事件即应触发，无需任何"轮数计数器"持久化字段——以"仅凭事件日志条数即可
    触发"作为可观察证据（thread_meta 不需要存在轮数键）。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    seed_events(st, [
        {"sender": "backend", "type": "report", "to": ["moderator"], "body": f"r{i}"}
        for i in range(6)
    ])

    orch.scheduler.check_watchdogs(st, m1_config(max_rounds=6))

    assert _gate_requests_to_human(st), "仅凭日志事件条数即应触发级别3（轮数不落盘，§5.3）"
