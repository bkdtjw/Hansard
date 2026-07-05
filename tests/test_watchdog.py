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


# ==================================================================
# R-T2 · C：升级去重（水位持久化 thread_meta）—— approve→resume 不复触发；
#           灌入新一窗口后到达新水位仍会再次升级（护栏非永久豁免）。
# ==================================================================

def _latest_watchdog_gate(store) -> dict | None:
    """取最后一条看门狗升级的 gate_request（sender='system', to=[human]）。"""
    gate = None
    for e in sorted(store.events(), key=lambda e: e["id"]):
        if (e["type"] == "gate_request" and e.get("from") == "system"
                and "human" in (e.get("to") or [])):
            gate = e
    return gate


def _approve_watchdog_gate(store, config, gate) -> None:
    """模拟人工 approve：走冻结的 apply_gate_decision（§10 `orch approve`）。

    看门狗 gate 的 corr 记在 thread_meta（gate_corr:{id}，§10 corr 缺省 `gate-{事件号}`），
    事件 corr 列为空——apply_gate_decision 经 _find_gate_request 的 meta 回填路径按此 corr
    定位并越过（R-T2 · C 前提）。approve 后线程 resume（status='running'）。
    """
    corr = store.get_meta(f"gate_corr:{gate['id']}")
    assert corr is not None, "看门狗 gate 应把 corr 记入 thread_meta（§10）"
    orch.scheduler.apply_gate_decision(
        store, config, {}, corr=corr, approve=True, sender="human",
    )


def test_watchdog_level2_no_retrigger_after_approve_resume(thread_dir):
    """R-T2 · C：level2 升级 → approve → resume → 再跑 check_watchdogs **不复触发**同一 gate。

    审计否定旧行为：approve→resume 后同一 gate 立即复触发，人类无法越过（违反 §10 续走）。
    修复后：升级时把当时计数落盘为水位；下一轮门限前移一个 loop_limit → 同计数不再升级。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    cfg = m1_config(loop_limit=3)
    seed_events(st, [
        {"sender": "tester", "type": "defect", "to": ["backend"], "body": f"缺陷{i}"}
        for i in range(3)
    ])

    # 首次升级：产生 gate + suspended。
    orch.scheduler.check_watchdogs(st, cfg)
    gate = _latest_watchdog_gate(st)
    assert gate is not None, "level2 达阈值应升级 gate_request"
    assert st.get_meta("status") == "suspended"
    gate_count_before = len(_gate_requests_to_human(st))

    # 人工 approve → resume（§10）。
    _approve_watchdog_gate(st, cfg, gate)
    assert st.get_meta("status") == "running", "approve 后线程应 resume（§10）"

    # 再跑看门狗：defect 计数未变（仍 3）→ 门限已前移到 3+3=6 → **不复触发**。
    orch.scheduler.check_watchdogs(st, cfg)
    gate_count_after = len(_gate_requests_to_human(st))
    assert gate_count_after == gate_count_before, (
        "approve→resume 后同一 gate 不得复触发（R-T2 · C 升级去重；§10 无损续走）"
    )
    assert st.get_meta("status") == "running", "无损续走：不复触发时线程保持 running"


def test_watchdog_level2_retriggers_at_new_watermark(thread_dir):
    """R-T2 · C：护栏**非永久豁免**——灌入新一窗口 defect、到达新水位后仍再次升级。

    首次升级水位=3；再累积到 6（>= 3+loop_limit=6）应再次升级。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    cfg = m1_config(loop_limit=3)
    seed_events(st, [
        {"sender": "tester", "type": "defect", "to": ["backend"], "body": f"d{i}"}
        for i in range(3)
    ])

    orch.scheduler.check_watchdogs(st, cfg)          # 首升，水位=3
    gate1 = _latest_watchdog_gate(st)
    _approve_watchdog_gate(st, cfg, gate1)           # resume
    orch.scheduler.check_watchdogs(st, cfg)          # 计数仍3，不触发
    assert st.get_meta("status") == "running"
    n_after_first = len(_gate_requests_to_human(st))

    # 灌入新一窗口 defect：同对再 +3 → 计数达 6 >= 水位3 + loop_limit3。
    seed_events(st, [
        {"sender": "tester", "type": "defect", "to": ["backend"], "body": f"d2-{i}"}
        for i in range(3)
    ])
    orch.scheduler.check_watchdogs(st, cfg)
    n_after_new_window = len(_gate_requests_to_human(st))
    assert n_after_new_window == n_after_first + 1, (
        "到达新水位（计数 6 ≥ 3+loop_limit）后护栏须再次升级（非永久豁免，R-T2 · C）"
    )
    assert st.get_meta("status") == "suspended", "再次升级后线程应重新 suspended"


def test_watchdog_level3_no_retrigger_after_approve_resume(thread_dir):
    """R-T2 · C：level3 升级 → approve → resume → 不复触发；到达新水位后仍再升级。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    cfg = m1_config(max_rounds=5)
    seed_events(st, [
        {"sender": "backend", "type": "report", "to": ["moderator"], "body": f"r{i}"}
        for i in range(5)
    ])

    orch.scheduler.check_watchdogs(st, cfg)   # total>=5 首升，水位=当时总数
    gate = _latest_watchdog_gate(st)
    assert gate is not None, "level3 达 max_rounds 应升级"
    assert st.get_meta("status") == "suspended"
    total_at_first = len(st.events())          # 含升级产生的 gate_request 事件
    n_before = len(_gate_requests_to_human(st))

    _approve_watchdog_gate(st, cfg, gate)      # resume（会追加 gate_decision 事件）
    assert st.get_meta("status") == "running"

    # 再跑：总数虽因 gate_request/gate_decision 略增，但门限已前移到 水位+max_rounds
    # （水位≈total_at_first），当前总数远未达 → 不复触发。
    orch.scheduler.check_watchdogs(st, cfg)
    assert len(_gate_requests_to_human(st)) == n_before, (
        "approve→resume 后 level3 同一 gate 不得复触发（R-T2 · C；§10 无损续走）"
    )

    # 灌入新一窗口事件，令总数达到 水位 + max_rounds → 再次升级（护栏非永久豁免）。
    wm = int(st.get_meta("wd_l3_total"))
    need = wm + 5 - len(st.events())
    if need > 0:
        seed_events(st, [
            {"sender": "backend", "type": "report", "to": ["moderator"], "body": f"n{i}"}
            for i in range(need)
        ])
    orch.scheduler.check_watchdogs(st, cfg)
    assert len(_gate_requests_to_human(st)) == n_before + 1, (
        "到达新水位（总数 ≥ 水位+max_rounds）后 level3 须再次升级（非永久豁免，R-T2 · C）"
    )
