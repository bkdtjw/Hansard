"""M4-T1 · 混沌 harness 测试（先行见红）。

覆盖任务卡条目 (b)：
  - `orch.chaos.ChaosHarness`（或 `orch.chaos.run_chaos`）：
      · 输入 = 附录B mock fixture（tests/fixtures/like_feature.yaml）；
      · 注入点覆盖 §4.4 五个间隙 + 纯随机；
      · 每轮 kill 后重启（新 Store 实例）走 `orch.scheduler.recover` 续跑至 terminate；
      · 返回 `ChaosReport{rounds, passed, failed_seeds, ledger_ok, terminal_ok}`。
  - 硬门槛（M4 契约 §5）：≥50 轮的严格验收由 T5 跑；本卡快跑 `rounds=3`。

约束（CLAUDE.md）：
  - 顶层只 `import orch.chaos`；符号在函数体内引用（未实现 → AttributeError）。
  - 断言"通过率 100% 于当前 rounds"（3 轮）；不弱化：与 spec §9.4 "mock 层 100%"一致。
  - 不真跑子进程；harness 使用 fixture 里已冻结的 MockAdapter 脚本，注入点靠 §4.4 钩子。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# 顶层只 import 包/模块；具体符号在函数体内引用（AttributeError → 红）。
import orch


# ==================================================================
# (b-1) orch.chaos 模块存在且导出 ChaosHarness
# ==================================================================

def test_chaos_module_exports_harness():
    """`orch.chaos` 应存在且导出 ChaosHarness 类（M4 契约 §2）。"""
    import orch.chaos  # noqa: F401
    assert hasattr(orch.chaos, "ChaosHarness"), (
        "M4 契约 §2：orch.chaos 应导出 ChaosHarness"
    )


# ==================================================================
# (b-2) ChaosReport 具备五个必需字段（rounds/passed/failed_seeds/ledger_ok/terminal_ok）
# ==================================================================

def test_chaos_report_has_required_fields(tmp_dir, like_feature_script):
    """M4 契约 §2：ChaosReport 必须含 rounds / passed / failed_seeds /
    ledger_ok / terminal_ok 五字段。快跑 rounds=3。"""
    import orch.chaos

    ws = tmp_dir / "chaos-ws"
    ws.mkdir()

    harness = orch.chaos.ChaosHarness(
        workspace=ws,
        script=like_feature_script,
        seed=1,
    )
    report = harness.run(rounds=3)

    # 允许 dataclass / dict / TypedDict 三形之一；用 getattr 兼容。
    def _f(obj, name):
        if isinstance(obj, dict):
            assert name in obj, f"ChaosReport 缺字段 {name!r}: {obj!r}"
            return obj[name]
        assert hasattr(obj, name), f"ChaosReport 缺字段 {name!r}: {obj!r}"
        return getattr(obj, name)

    assert _f(report, "rounds") == 3
    # passed 至少存在（数值型）；本卡不硬性锁死 == 3 —— 门槛判定见 (b-4)。
    _ = _f(report, "passed")
    _ = _f(report, "failed_seeds")
    _ = _f(report, "ledger_ok")
    _ = _f(report, "terminal_ok")


# ==================================================================
# (b-3) 注入点覆盖 §4.4 五个间隙 + 纯随机（共 6 种模式，harness 内部轮转）
# ==================================================================

def test_chaos_harness_covers_five_gaps_plus_random(tmp_dir, like_feature_script):
    """M4 契约 §2 + R-T1：注入点必须覆盖 §4.4 五间隙 + 纯随机（共 6 种模式）。

    harness 应对外暴露 `INJECTION_SITES`（或等价常量）—— 长度必须 ≥ 6，
    且包含五个 §4.4 site 与 "random" 关键字。
    """
    import orch.chaos

    sites = getattr(orch.chaos, "INJECTION_SITES", None)
    if sites is None:
        # 允许在 ChaosHarness 类上暴露。
        sites = getattr(orch.chaos.ChaosHarness, "INJECTION_SITES", None)
    assert sites is not None, (
        "orch.chaos 应对外暴露 INJECTION_SITES（覆盖 §4.4 五间隙 + random）"
    )
    site_set = {str(s) for s in sites}
    expected_gap_sites = {
        "append_event_post",
        "mark_dispatching_post",
        "invoke_post",
        "autocommit_post",
        "reply_and_done_post",
    }
    missing = expected_gap_sites - site_set
    assert not missing, f"缺 §4.4 注入点：{missing}"
    assert any("random" in s for s in site_set), "应含纯随机注入模式"


def test_resolve_site_returns_real_injection_for_all_five_gaps(
    tmp_dir, like_feature_script
):
    """R-T1（审计 A1）：_resolve_site 对 §4.4 五个 site 必须**均返回非 None** 真实注入。

    这直接顶替旧版"集合包含"断言——旧实现把 invoke_post/autocommit_post 静默降级为
    None（"本轮未触发,仍完整跑通"），使 50 轮硬门槛实际只在 3/5 间隙注入 = fail-open。
    本用例锁死：删除 None 降级后，五个 site 逐一解析为可真实注入的 site 名（自身或，
    对 random_mix，五者之一）。
    """
    import orch.chaos

    ws = tmp_dir / "chaos-ws"
    ws.mkdir()
    harness = orch.chaos.ChaosHarness(
        workspace=ws, script=like_feature_script, seed=1,
    )
    five_gaps = [
        "append_event_post",
        "mark_dispatching_post",
        "invoke_post",
        "autocommit_post",
        "reply_and_done_post",
    ]
    for site in five_gaps:
        resolved = harness._resolve_site(site)
        assert resolved is not None, (
            f"§4.4 site {site!r} 必须解析为真实注入（不得 None 降级）"
        )
        assert resolved in five_gaps, (
            f"{site!r} 解析结果 {resolved!r} 应是 §4.4 五个真实 site 之一"
        )

    # random_mix 也必须落到五个真实 site 之一（多次采样均非 None）。
    for _ in range(20):
        r = harness._resolve_site("random_mix")
        assert r in five_gaps, f"random_mix 解析结果 {r!r} 应是五个真实 site 之一"


# ==================================================================
# (b-4) rounds=3 快跑：ledger 无重复事件号 + 终态与不中断基准一致
# ==================================================================

def test_chaos_harness_rounds3_passes(tmp_dir, like_feature_script):
    """rounds=3 快跑：mock 层 100% 通过（passed == rounds）；
    ledger_ok / terminal_ok 均 True（§9.4 mock 层校验）。"""
    import orch.chaos

    ws = tmp_dir / "chaos-ws"
    ws.mkdir()

    harness = orch.chaos.ChaosHarness(
        workspace=ws,
        script=like_feature_script,
        seed=42,
    )
    report = harness.run(rounds=3)

    def _f(obj, name):
        return obj[name] if isinstance(obj, dict) else getattr(obj, name)

    assert _f(report, "rounds") == 3
    assert _f(report, "passed") == 3, (
        f"快跑 3 轮应 100% 通过；failed_seeds={_f(report, 'failed_seeds')}"
    )
    assert _f(report, "failed_seeds") == [] or _f(report, "failed_seeds") == (), (
        f"快跑 3 轮不应有失败种子：{_f(report, 'failed_seeds')}"
    )
    assert _f(report, "ledger_ok") is True, "mock ledger 应无重复事件号（§9.4）"
    assert _f(report, "terminal_ok") is True, "终态应与不中断基准一致（§9.4）"


# ==================================================================
# (b-5) 每轮 kill 后必须"新 Store 实例走 recover"续跑至 terminate
# ==================================================================

def test_chaos_harness_each_round_reaches_terminate(tmp_dir, like_feature_script):
    """每轮不论从哪个注入点崩溃，最终线程 status 必须到达 'terminated'。

    这是 §9.4 mock 层"kill 后重启续跑至终止"的直接可观察证据：
    harness 在完成某轮后，workspace 下该轮线程目录的 thread_meta.status
    应为 'terminated'（否则该轮判 failed）。
    """
    import orch.chaos
    import orch.store

    ws = tmp_dir / "chaos-ws"
    ws.mkdir()

    harness = orch.chaos.ChaosHarness(
        workspace=ws,
        script=like_feature_script,
        seed=7,
    )
    report = harness.run(rounds=3)

    def _f(obj, name):
        return obj[name] if isinstance(obj, dict) else getattr(obj, name)

    assert _f(report, "passed") == 3

    # 每轮应有一个独立线程目录；逐个校验 status='terminated'。
    thread_dirs = sorted(p for p in ws.iterdir() if p.is_dir() and p.name.startswith("t-"))
    assert len(thread_dirs) >= 3, (
        f"应至少产生 3 个线程目录（每轮一个）：实际 {len(thread_dirs)}"
    )
    for tdir in thread_dirs[:3]:
        st = orch.store.Store(tdir)
        status = st.get_meta("status")
        assert status == "terminated", (
            f"{tdir.name}: 混沌恢复后线程必须到达 terminated；实测 status={status!r}"
        )


# ==================================================================
# (b-6) R-T4 · ChaosHarness.run(metrics_store=...) 落 §13 混沌指标（缺省不变）
# ==================================================================

def test_chaos_run_metrics_store_records_rounds_and_pass_pct(tmp_dir, like_feature_script):
    """R-T4：ChaosHarness.run(metrics_store=store) 跑完后向该 store 落两条 §13 混沌指标：
    chaos_rounds（=rounds）与 chaos_mock_pass_pct（=passed/rounds*100）。3 轮全过 → 100%。"""
    import orch.chaos
    import orch.store

    ws = tmp_dir / "chaos-ws"
    ws.mkdir()
    mstore = orch.store.Store(ws / "t-metrics")

    harness = orch.chaos.ChaosHarness(
        workspace=ws / "rounds", script=like_feature_script, seed=3,
    )
    (ws / "rounds").mkdir(parents=True, exist_ok=True)
    report = harness.run(rounds=3, metrics_store=mstore)
    assert report.passed == 3

    def _vals(key):
        return [float(r["value"]) for r in mstore._con.execute(
            "SELECT value FROM metrics WHERE key=?", (key,)).fetchall()]

    assert _vals("chaos_rounds") == [3.0], "chaos_rounds 应落一条 = rounds(3)"
    pp = _vals("chaos_mock_pass_pct")
    assert pp and abs(pp[-1] - 100.0) < 0.01, f"3 轮全过 → mock 通过率 100%，实测 {pp}"


def test_chaos_run_default_metrics_store_none_no_side_effect(tmp_dir, like_feature_script):
    """R-T4 向后兼容：缺省 metrics_store（None）时 run 不落任何 chaos 指标、行为不变。"""
    import orch.chaos

    ws = tmp_dir / "chaos-ws2"
    ws.mkdir()
    harness = orch.chaos.ChaosHarness(
        workspace=ws, script=like_feature_script, seed=4,
    )
    report = harness.run(rounds=2)  # 不传 metrics_store
    assert report.rounds == 2 and report.passed == 2
