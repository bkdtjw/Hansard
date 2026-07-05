"""M3-T1 · §5.1 写域并行 + §5.2 长作业真异步验收测试——测试先行，见红。

覆盖任务卡：
  (b) §5.1 run_thread_async：写域不相交的多目标同批 asyncio.TaskGroup 并行 invoke
      （用 asyncio.Event/Barrier 断言**真并发**，不使用 sleep 假证）；
      写域相交组串行；聚合 batch_size 记 metrics。
  (c) §5.2 register_async_job：subprocess 非阻塞启动、完成后 append system 事件回调，
      jobs 表状态流转 pending→running→done；异步不阻塞 run_thread_async 主流。

硬约束（CLAUDE.md / M3 契约 §3）：
  - 顶层只 `import orch.scheduler / orch.store / orch.adapters`；具体 M3 符号
    `run_thread_async` / `register_async_job` 在**函数体内**引用；未实现表现为运行时红。
  - 并发断言用 asyncio.Event 断言真并发（两 adapter 相互等对方进入 invoke 才放行），
    禁止 sleep 假证（§CLAUDE.md 硬约束）。
  - 禁止引入 asyncio 外的第三方并发库；只用标准库 asyncio。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import orch.adapters
import orch.scheduler
import orch.store


# ——————————————————————————————————————————————————————————————
# 直读 sqlite 观察 metrics/jobs 落盘真相（契约 §7：读盘观察合法）
# ——————————————————————————————————————————————————————————————

def _read_metrics(thread_dir: Path, key: str) -> list[dict]:
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT ts, key, value, extra FROM metrics WHERE key=? ORDER BY ts ASC",
            (key,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _read_jobs(thread_dir: Path) -> list[dict]:
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT corr, kind, cmd, callback_to, started_evt, status FROM jobs"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _config():
    return {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {},
        "roles": {
            "backend": {"can_decide": False, "write_scope": ["server/"]},
            "frontend": {"can_decide": False, "write_scope": ["web/"]},
            "moderator": {"can_decide": True, "write_scope": []},
        },
    }


# ——————————————————————————————————————————————————————————————
# 并发 fake adapter：进入 invoke 时先 set event，再 await barrier
# 用来断言**真并发**（两个 adapter 必须都进入 invoke 才能推进）
# ——————————————————————————————————————————————————————————————

class _ParallelBarrierAdapter:
    """异步 adapter：进入 invoke 时把自己的 Event set，然后 await peer_event。

    两个此类实例互引对方 event。若调度器**串行**调用它们 → 第一个 await peer_event
    永远超时（peer 尚未进 invoke） → 抛 TimeoutError；调度器**并行**调用 → 两个都
    进入 invoke，各自 set 后 await 成功 → 返回预置信封。
    """

    def __init__(
        self,
        *,
        role: str,
        my_event: asyncio.Event,
        peer_event: asyncio.Event,
        reply: dict,
        timeout: float = 2.0,
    ) -> None:
        self.role = role
        self.my_event = my_event
        self.peer_event = peer_event
        self.reply = reply
        self.timeout = timeout
        self.invoked: bool = False
        # 与 Fake* 保持接口对齐（能力申报占位）。
        self.caps = {
            "context_window": 1_000_000, "tools": [], "write_scope": [],
            "cost_tier": "cheap", "supports_resume": False,
            "timeout_s": 0, "max_concurrent": 4,
        }

    async def ainvoke(
        self, view: dict, sess: dict | None
    ) -> tuple[dict, dict | None]:
        self.invoked = True
        self.my_event.set()
        # 若调度器串行 → peer 尚未进入 invoke，此处将挂到超时。
        await asyncio.wait_for(self.peer_event.wait(), timeout=self.timeout)
        return dict(self.reply), None

    # 与 sync invoke 接口保持一致（M3 可能同时保留 sync 路径，调度层择一路径调用）。
    def invoke(self, view: dict, sess: dict | None):  # pragma: no cover - 非本测试路径
        raise NotImplementedError(
            "_ParallelBarrierAdapter 仅提供 ainvoke（异步路径），不支持同步 invoke"
        )


# ==================================================================
# (b-1) §5.1 写域不相交 → 并行（asyncio.Event 断言真并发）
# ==================================================================

def test_run_thread_async_parallel_when_write_scopes_disjoint(thread_dir):
    """§5.1：backend(server/) 与 frontend(web/) 写域不相交 → 同批并行 invoke。

    断言真并发：两 adapter 互 barrier；若串行调度则 barrier 超时；并行则两 event 都被 set。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")

    # 铺 moderator 同批发出的两个派发：#1 pending -> backend；#2 pending -> frontend。
    # 用 human 发一个同时 to backend/frontend 的 assign（同 event_id 出两目标 → 并行判定基础）。
    e1 = st.append_event(
        sender="human", type="assign", body="do parallel",
        to=["backend", "frontend"],
    )

    ev_backend = asyncio.Event()
    ev_frontend = asyncio.Event()

    ad_backend = _ParallelBarrierAdapter(
        role="backend",
        my_event=ev_backend, peer_event=ev_frontend,
        reply={"to": ["moderator"], "type": "report", "body": "backend done"},
    )
    ad_frontend = _ParallelBarrierAdapter(
        role="frontend",
        my_event=ev_frontend, peer_event=ev_backend,
        reply={"to": ["moderator"], "type": "report", "body": "frontend done"},
    )
    # moderator 简单 terminate 收尾（避免死循环）。
    ad_moderator = orch.adapters.FakeApiAdapter(
        role="moderator", config={"kind": "api"},
        scripted_reply={"to": [], "type": "terminate", "body": "done"},
    )

    adapters = {
        "backend": ad_backend,
        "frontend": ad_frontend,
        "moderator": ad_moderator,
    }

    async def _run():
        await orch.scheduler.run_thread_async(st, _config(), adapters)

    # 若调度器仍走 M2 串行路径 → 第一个 adapter 会在 barrier 上超时（2s）→ 触发 TimeoutError。
    # asyncio.run 会向外抛，测试直接 fail。M3 并行实现下应 <2s 完成。
    t0 = time.time()
    asyncio.run(_run())
    elapsed = time.time() - t0

    assert ad_backend.invoked and ad_frontend.invoked, \
        "§5.1：写域不相交时 backend/frontend 都应被 invoke"
    # 并发路径下两个 event 均已被 set。
    assert ev_backend.is_set() and ev_frontend.is_set()
    # 并发路径应远小于两次 barrier timeout 之和（2s+2s=4s）。
    assert elapsed < 3.5, f"§5.1：真并发应在 barrier timeout 内完成，实测 {elapsed:.2f}s"


# ==================================================================
# (b-2) §5.1 写域相交 → 串行（不构造真并发 barrier，只观察不并发即可）
# ==================================================================

def test_run_thread_async_serial_when_write_scopes_intersect(thread_dir):
    """§5.1：写域相交（同 server/）→ 只能串行调度（不能同时进 invoke）。

    构造：两个不同 role 但写域相同（server/），断言不会同时进入 invoke——用
    调度进程中"任何时刻并发数"上限观察。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(
        sender="human", type="assign", body="serial", to=["backend", "backend2"],
    )

    # 两个 role 写域都是 server/ → 相交 → 强制串行。
    cfg = _config()
    cfg["roles"]["backend2"] = {"can_decide": False, "write_scope": ["server/"]}

    active = 0
    peak = [0]

    async def _tracked_invoke(reply):
        nonlocal active
        active += 1
        peak[0] = max(peak[0], active)
        await asyncio.sleep(0)  # 让出，给对方"抢并发"的机会
        active -= 1
        return dict(reply), None

    class _TrackAd:
        def __init__(self, reply):
            self.reply = reply
            self.caps = {
                "context_window": 1_000_000, "tools": [], "write_scope": ["server/"],
                "cost_tier": "cheap", "supports_resume": False,
                "timeout_s": 0, "max_concurrent": 4,
            }

        async def ainvoke(self, view, sess):
            return await _tracked_invoke(self.reply)

    ad_backend = _TrackAd({"to": ["moderator"], "type": "report", "body": "b"})
    ad_backend2 = _TrackAd({"to": ["moderator"], "type": "report", "body": "c"})
    ad_moderator = orch.adapters.FakeApiAdapter(
        role="moderator", config={"kind": "api"},
        scripted_reply={"to": [], "type": "terminate", "body": "done"},
    )
    adapters = {"backend": ad_backend, "backend2": ad_backend2, "moderator": ad_moderator}

    async def _run():
        await orch.scheduler.run_thread_async(st, cfg, adapters)

    asyncio.run(_run())
    assert peak[0] == 1, f"§5.1：写域相交组应串行；实测同时并发峰值 {peak[0]}"


# ==================================================================
# (b-3) §13 聚合 batch_size 记 metrics（M3 契约 §3 补齐）
# ==================================================================

def test_run_thread_async_records_batch_size_metric(thread_dir):
    """§13：每次聚合派发记 batch_size（同目标同批多 event_id → 一次 invoke）。

    构造：pm 收到同一批 backend/frontend 各发一件（re 到 pm），聚合为一次 invoke → batch_size=2。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 两条同 target=pm 的 pending，模拟聚合。
    e1 = st.append_event(sender="backend", type="question", body="q1", to=["pm"])
    e2 = st.append_event(sender="frontend", type="answer", body="a1", to=["pm"])

    cfg = _config()
    cfg["roles"]["pm"] = {"can_decide": True, "write_scope": ["docs/"]}
    ad_pm = orch.adapters.FakeApiAdapter(
        role="pm", config={"kind": "api"},
        scripted_reply={"to": ["moderator"], "type": "handoff", "body": "aggregated"},
    )
    ad_mod = orch.adapters.FakeApiAdapter(
        role="moderator", config={"kind": "api"},
        scripted_reply={"to": [], "type": "terminate", "body": "done"},
    )
    adapters = {"pm": ad_pm, "moderator": ad_mod}

    async def _run():
        await orch.scheduler.run_thread_async(st, cfg, adapters)

    asyncio.run(_run())

    rows = _read_metrics(thread_dir, "batch_size")
    assert any(int(r["value"]) == 2 for r in rows), \
        f"§13：聚合 batch_size=2 应入 metrics；实测 {rows}"


# ==================================================================
# (c-1) §5.2 register_async_job：非阻塞启动 + 状态流转 pending/running→done + 系统事件回调
# ==================================================================

def test_register_async_job_nonblocking_and_state_transitions(thread_dir):
    """§5.2：注册长作业时立即返回（非阻塞）；作业跑完后 append system 事件回调；
    jobs 表状态从 running → done（M3 契约 §3 register_async_job）。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")

    # 用一个立即成功的极短命令（跨平台无害）。
    # Windows 与 POSIX 都可执行 `python -c "pass"`。
    py = sys.executable
    cmd = [py, "-c", "import sys; sys.exit(0)"]

    async def _run():
        # 非阻塞：register_async_job 立刻返回，长作业在后台异步跑。
        orch.scheduler.register_async_job(
            st,
            corr="job-01",
            cmd=cmd,
            callback_to="moderator",
        )
        # 等待作业完成 + 回调（最多 5 秒）。避免 sleep 假证：轮询 jobs 表真相。
        for _ in range(50):
            rows = _read_jobs(thread_dir)
            if rows and rows[0]["status"] == "done":
                return
            await asyncio.sleep(0.1)

    asyncio.run(_run())

    rows = _read_jobs(thread_dir)
    assert rows, "§5.2：register_async_job 应登记 jobs 表"
    row = rows[0]
    assert row["corr"] == "job-01"
    assert row["callback_to"] == "moderator"
    assert row["status"] == "done", f"§5.2：作业完成后状态应 done，实测 {row['status']!r}"

    # 完成后 append 一条 system 事件（§5.2/§4.4）；corr 回填 job-01。
    events = st.events()
    sys_evs = [e for e in events if e.get("type") == "system" and e.get("corr") == "job-01"]
    assert sys_evs, "§5.2：作业完成后应 append system 事件回调（corr=job-01）"
    assert sys_evs[-1].get("from") == "system"


# ==================================================================
# (c-2) §5.2：register_async_job 不阻塞 run_thread_async 主流
# ==================================================================

def test_register_async_job_does_not_block_run_thread_async(thread_dir):
    """§5.2：长作业异步登记后，run_thread_async 主流应能立即推进后续事件；
    作业回调作为独立事件流入队，主循环不因等待作业而阻塞。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")

    # 用一个"慢"但会自然结束的命令 —— 500ms sleep（跨平台）。
    py = sys.executable
    cmd = [py, "-c", "import time; time.sleep(0.5)"]

    # 主流：pm 收到 assign → 立刻 terminate（不等作业）。
    st.append_event(sender="human", type="assign", body="main flow", to=["pm"])

    cfg = _config()
    cfg["roles"]["pm"] = {"can_decide": True, "write_scope": ["docs/"]}
    ad_pm = orch.adapters.FakeApiAdapter(
        role="pm", config={"kind": "api"},
        scripted_reply={"to": [], "type": "terminate", "body": "pm terminate"},
    )
    ad_mod = orch.adapters.FakeApiAdapter(
        role="moderator", config={"kind": "api"},
        scripted_reply={"to": [], "type": "terminate", "body": "done"},
    )
    adapters = {"pm": ad_pm, "moderator": ad_mod}

    async def _run():
        # 先启动长作业（异步），再跑主流；主流应立即完成而不等作业。
        orch.scheduler.register_async_job(
            st, corr="job-slow", cmd=cmd, callback_to="moderator",
        )
        t0 = time.time()
        await orch.scheduler.run_thread_async(st, cfg, adapters)
        elapsed = time.time() - t0
        # 主流耗时应远小于作业 500ms（真异步的最直接证据）。
        assert elapsed < 0.4, f"§5.2：register_async_job 应不阻塞主流，实测 {elapsed:.2f}s"

    asyncio.run(_run())
