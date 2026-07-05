"""M4-T1 · 故障注入钩子测试（R-T1 重写为生产路径真触发）。

覆盖 §4.4 五个事务边界的**生产代码路径**注入（审计 A1 修复）：
  裸 FaultInjector 认得 site 名 ≠ 生产代码埋了钩子。旧版 (a-7) 只断言
  `FaultInjector({site:1}).check(site)` 会抛，属恒真断言（假绿）。本版对**每个** site
  各写一个用例：装上 FaultInjector 后**驱动真实 mock 流程（run_thread）**，断言恰在该
  site 抛 SystemExit(137)、盘上状态符合 §4.4 该间隙定义、且 recover() 后能续跑到 terminate。

站点与承载层：
  - append_event_post / mark_dispatching_post / reply_and_done_post：store 内嵌 _fault_check；
  - invoke_post / autocommit_post：调度层 core/async_core 控制流位置经 store.fault_check
    （Lead §17 裁决：按控制流位置触发，mock 无 worktree 照样命中）。

保留的机制单测（a-1..a-3）：验证 FaultInjector 计数语义与全局注入/清除接口本身；
这些不是假绿——它们测的是注入器机制，不冒充"生产埋点已落地"。
"""

from __future__ import annotations

import pytest

import orch.chaos
import orch.scheduler
import orch.store

from orch.chaos.expected import EXPECTED_TYPE_SEQUENCE


# ==================================================================
# (a-1) FaultInjector 存在 + check(site) 未命中时静默通过
# ==================================================================

def test_fault_injector_no_trigger_when_site_not_configured():
    """未针对某 site 配置的 check() 必须直接返回，不影响正常路径。"""
    fi = orch.store.FaultInjector({"append_event_post": 1})
    # 未配置的 site 名不应触发。
    fi.check("mark_dispatching_post")
    fi.check("reply_and_done_post")
    fi.check("invoke_post")
    fi.check("autocommit_post")


# ==================================================================
# (a-2) FaultInjector 命中 (site, count) → SystemExit(137)
# ==================================================================

def test_fault_injector_triggers_systemexit_137_on_matched_site():
    """(site, count) 首次命中 → 抛 SystemExit(137) 模拟 kill -9。"""
    fi = orch.store.FaultInjector({"append_event_post": 1})
    # 首次调用命中 count=1；code 137 = 128+SIGKILL(9)，spec §9.4 语义。
    with pytest.raises(SystemExit) as ei:
        fi.check("append_event_post")
    assert ei.value.code == 137


def test_fault_injector_respects_count_semantics():
    """count=N 表示"第 N 次调用命中"；前 N-1 次不抛。"""
    fi = orch.store.FaultInjector({"append_event_post": 3})
    # 前两次 pass，第三次抛。
    fi.check("append_event_post")
    fi.check("append_event_post")
    with pytest.raises(SystemExit) as ei:
        fi.check("append_event_post")
    assert ei.value.code == 137


# ==================================================================
# (a-3) 全局注入 / 清除接口存在，且不注入时 store 行为无变化
# ==================================================================

def test_store_set_fault_injector_none_is_noop(thread_dir):
    """set_fault_injector(None) 应清除注入器；不注入时 Store 一切照常。"""
    st = orch.store.Store(thread_dir)
    # 清除（若之前被别的用例设过）。
    orch.store.set_fault_injector(None)
    eid = st.append_event(sender="human", type="assign", body="hi", to=["moderator"])
    assert eid >= 1
    assert st.pending_dispatches(), "无注入时 append_event 应正常生成派发行"


def test_store_exposes_public_fault_check(thread_dir):
    """R-T1：store 暴露公共 fault_check(site)——调度层 invoke_post/autocommit_post 复用。

    与私有 _fault_check 共享同一全局 FaultInjector 单例；未注入零开销，注入命中抛 137。
    """
    orch.store.set_fault_injector(None)
    # 未注入：公共入口零开销直接返回。
    orch.store.fault_check("invoke_post")
    # 注入后命中：抛 137。
    orch.store.set_fault_injector(orch.store.FaultInjector({"invoke_post": 1}))
    try:
        with pytest.raises(SystemExit) as ei:
            orch.store.fault_check("invoke_post")
        assert ei.value.code == 137
    finally:
        orch.store.set_fault_injector(None)


# ==================================================================
# 生产路径真触发：五个 §4.4 site 各一用例（run_thread 驱动真实 mock 流程）
# ==================================================================

def _seed_thread(store):
    """按附录B fixture 起线程：status=running + E1（human assign, to=[]）。"""
    store.set_meta("status", "running")
    store.append_event(sender="human", type="assign", body="点赞功能开工", to=[])


def _drive_to_kill(harness, store, cfg, adapters, site, count):
    """装注入器驱动 mock 流程，返回是否恰在注入生效时抛 SystemExit(137)。

    返回 True = 确实抛了 137（该 site 的生产埋点在 mock 路径上被触发）。
    """
    orch.store.set_fault_injector(orch.store.FaultInjector({site: count}))
    killed = False
    try:
        harness._drive_until_stopped(store, cfg, adapters)
    except SystemExit as exc:
        assert exc.code == 137, f"{site}: 期望 SystemExit(137)，实得 {exc.code}"
        killed = True
    finally:
        orch.store.set_fault_injector(None)
    return killed


@pytest.mark.parametrize(
    "site",
    [
        "append_event_post",     # §4.4 (1)
        "mark_dispatching_post", # §4.4 (2)
        "invoke_post",           # §4.4 (3) 调度层控制流位置
        "autocommit_post",       # §4.4 (4) 调度层控制流位置
        "reply_and_done_post",   # §4.4 (5)
    ],
)
def test_production_path_fault_triggers_at_each_gap(tmp_dir, like_feature_script, site):
    """§4.4 每个 site：装 FaultInjector 后驱动真实 mock 流程（run_thread）必被 kill 137。

    这直接顶替旧版 (a-7) 恒真断言：证明**生产代码真的在该 site 埋了钩子并在 mock 路径
    上触发**（而非仅裸 FaultInjector 认得名字）。count=1 保证第一次命中即崩（§4.4 该间隙
    在附录B 首轮必然被穿越）。
    """
    ws = tmp_dir / f"ws-{site}"
    ws.mkdir()
    ledger = ws / "ledger.txt"
    tdir = ws / "t-000"
    tdir.mkdir(parents=True, exist_ok=True)

    harness = orch.chaos.ChaosHarness(
        workspace=ws, script=like_feature_script, seed=1,
    )
    orch.store.set_fault_injector(None)
    st = orch.store.Store(tdir)
    _seed_thread(st)
    adapters = harness._build_adapters(ledger)
    cfg = harness._config()

    killed = _drive_to_kill(harness, st, cfg, adapters, site, count=1)
    assert killed, (
        f"§4.4 site {site!r} 的生产埋点未在 mock 路径上触发 SystemExit(137)"
        f"——说明该 site 未真正埋进生产代码（假绿）。"
    )

    # —— 盘上状态符合 §4.4 该间隙定义 —— #
    status = st.get_meta("status")
    assert status not in ("terminated",), (
        f"{site}: kill 于中途，线程不应已 terminated（status={status!r}）"
    )
    if site == "append_event_post":
        # (1) 事件已追加 + 其派发行 pending（提交后崩，reply 尚未处理）。
        assert st.events(), "append_event_post: 事件应已落盘"
    elif site == "mark_dispatching_post":
        # (2) 该派发行已 dispatching（提交后崩）。
        from orch.scheduler._dispatch import dispatching_rows
        assert dispatching_rows(st), "mark_dispatching_post: 应有 dispatching 行"
    elif site in ("invoke_post", "autocommit_post"):
        # (3)/(4) invoke 已返回、reply 尚未落盘：该批派发行仍 dispatching、无对应回复。
        from orch.scheduler._dispatch import dispatching_rows
        assert dispatching_rows(st), (
            f"{site}: invoke 已返回但 reply 未落盘，派发行应仍 dispatching"
        )
    elif site == "reply_and_done_post":
        # (5) 回复已落盘 + done 已标（单事务提交后崩）：至少有一条回复事件。
        assert len(st.events()) >= 2, "reply_and_done_post: 回复事件应已落盘"

    del st

    # —— recover() 后能续跑到 terminate（§9.1 三情形 + §16.10 只查表数日志）—— #
    st2 = orch.store.Store(tdir)
    orch.scheduler.recover(st2, cfg)
    harness._drive_until_stopped(st2, cfg, adapters)
    assert st2.get_meta("status") == "terminated", (
        f"{site}: recover 后应续跑至 terminated；实测 {st2.get_meta('status')!r}"
    )

    # 终态类型序列一致（§9.4 mock 层：恢复续跑终态与不中断基准一致的类型面）。
    types = [e["type"] for e in sorted(st2.events(), key=lambda e: e["id"])]
    assert types == EXPECTED_TYPE_SEQUENCE, (
        f"{site}: 恢复后终态类型序列与附录B 期望不符：{types}"
    )
