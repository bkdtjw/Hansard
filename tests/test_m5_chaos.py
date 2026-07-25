"""M5-T1 · 适配器切换间隙混沌（opt-in，测试先行见红）。

【依据】
  spec §15 M5 验收末段："切换间隙 kill -9 混沌 ≥ 20 轮 100% 通过（§9.4 第一层扩展场景）"；
  spec §9.4 第一层不变量："任意时刻 kill -9，重启后每个 dispatching 行必落入 §9.1 的
  a–c 之一，且每个事件的副作用恰好生效一次"；
  docs/m5-contract.md §8（混沌与 E2E）：
    · MockAdapter 双实例扮主/备（同一脚本表）；场景 = 主 adapter 于第 k 次 invoke 起抛
      额度错误 → 自动跳闸 + fallback 接手 → 跑完附录B 至 terminated；
    · 终态比较沿 R-T1 口径（ledger + 黑板与"不中断基准"一致）；
    · kill -9 扩展：在"跳闸落盘前后 / 切换审计前后 / 换绑重派前后"间隙注入 + 纯随机，
      ≥ 20 轮 100%。

【运行策略（opt-in，沿 --chaos-50 同一惯例）】
  本文件全部用例打 `chaos_m5` 标记，默认 **skip**（见 tests/conftest.py 的
  `pytest_collection_modifyitems`）；显式传 `--chaos-m5` 才收集并运行：
      python -m pytest tests/test_m5_chaos.py -q --chaos-m5
  与 M4 的 `--chaos-50` 彼此独立，互不影响。

【T1 自决的入口命名（契约 §8 只描述场景、未冻结符号名；已在汇报 ④ 升级 Lead）】
  契约 §8 未给出 orch.chaos 侧的具体符号，本卡按既有 M4 命名风格约定如下，T6 据此实现：
    orch.chaos.AdapterChaosHarness(
        *, workspace: Path, script: dict, seed: int | None = None,
        unavailable_after: int = 2,      # 主 mock 第 k 次 invoke 起抛额度错误
    )
      .run(rounds: int = 20) -> ChaosReport      # 复用 M4 契约 §2 冻结的 ChaosReport
    orch.chaos.ADAPTER_INJECTION_SITES: tuple[str, ...]
      必须覆盖契约 §8 的三个 M5 间隙 + 纯随机：
        "adapter_trip_post"      跳闸落盘后（disable 已写状态文件、审计事件未落/未回 pending）
        "fallback_switch_post"   切换审计事件落盘后（换绑重派尚未发生）
        "rebind_dispatch_post"   换绑重派后（sessions 换绑 + attempts 归零已落盘）
        "random_mix"             纯随机（从上述真实 site 中派生挑选）

【硬约束（CLAUDE.md）】
  - 顶层只 `import orch`；M5 符号在函数体内引用（未实现 → AttributeError → 红）。
  - 100% 通过 = passed == rounds 且 failed_seeds 为空；两者缺一不可，不弱化门槛。
  - 无 try/except 吞错、无恒真断言；skip 只用于 opt-in 门控本身。
"""

from __future__ import annotations

import pytest

import orch


def _report_field(report, name: str):
    """ChaosReport 允许 dataclass / dict 两形（同 tests/test_chaos.py 口径）。"""
    if isinstance(report, dict):
        assert name in report, f"ChaosReport 缺字段 {name!r}: {report!r}"
        return report[name]
    assert hasattr(report, name), f"ChaosReport 缺字段 {name!r}: {report!r}"
    return getattr(report, name)


# ==================================================================
# (h-1) 场景入口与注入面：契约 §8 三个 M5 间隙 + 纯随机
# ==================================================================

@pytest.mark.chaos_m5
def test_adapter_chaos_entry_and_injection_sites_exported():
    """契约 §8：orch.chaos 须导出 M5 场景入口与切换间隙注入面清单。"""
    import orch.chaos

    assert hasattr(orch.chaos, "AdapterChaosHarness"), (
        "契约 §8：orch.chaos 应导出 AdapterChaosHarness（M5 切换间隙混沌场景入口）"
    )
    assert hasattr(orch.chaos, "ADAPTER_INJECTION_SITES"), (
        "契约 §8：orch.chaos 应导出 ADAPTER_INJECTION_SITES（M5 三个切换间隙 + 纯随机）"
    )
    sites = tuple(orch.chaos.ADAPTER_INJECTION_SITES)
    for expected in ("adapter_trip_post", "fallback_switch_post",
                     "rebind_dispatch_post", "random_mix"):
        assert expected in sites, f"注入面缺 {expected!r}：{sites}"


# ==================================================================
# (h-2) 场景骨架：主 mock unavailable_after=k → 自动跳闸 + 备胎接手跑完，终态与基准一致
# ==================================================================

@pytest.mark.chaos_m5
def test_adapter_trip_fallback_scenario_matches_baseline(tmp_dir, like_feature_script):
    """契约 §8 场景骨架（快跑 3 轮，沿 tests/test_chaos.py 快跑口径）。

    主 mock 自第 k 次 invoke 起抛额度错误 → §5.6.3 自动跳闸 → §5.6.2 备胎接手 →
    附录B 任务仍跑到 terminated，且 ledger 与黑板终态与"不中断基准"一致
    （ledger_ok / terminal_ok 由 harness 内部按 R-T1 口径校验并汇总）。
    """
    import orch.chaos

    ws = tmp_dir / "m5-chaos-scenario"
    ws.mkdir()

    harness = orch.chaos.AdapterChaosHarness(
        workspace=ws,
        script=like_feature_script,
        seed=20260725,
        unavailable_after=2,
    )
    report = harness.run(rounds=3)

    assert _report_field(report, "rounds") == 3
    assert _report_field(report, "passed") == 3, _report_field(report, "failed_seeds")
    assert list(_report_field(report, "failed_seeds")) == []
    assert _report_field(report, "ledger_ok") is True
    assert _report_field(report, "terminal_ok") is True


# ==================================================================
# (h-3) 硬门槛：切换间隙 kill -9 ≥ 20 轮，100% 通过（spec §15 M5 / §9.4 扩展）
# ==================================================================

@pytest.mark.chaos_m5
@pytest.mark.parametrize("seed", [20260725, 7])
def test_adapter_switch_chaos_20_rounds_hard_gate(tmp_dir, like_feature_script, seed):
    """spec §15 M5：切换间隙 kill -9 混沌 ≥ 20 轮，通过率必须 100%。

    通过判据（缺一即失败）：
      1. rounds == 20
      2. passed == 20
      3. failed_seeds 为空
      4. ledger_ok is True（每个事件副作用恰好一次，§9.4）
      5. terminal_ok is True（终态与不中断基准一致）

    【为什么必须跨两个 seed（R5 · 评审 major-3）】
      单 seed 的 20 轮对 R2 类缺陷是**盲**的：R2（"跳闸后本轮 continue、下轮才重解析"
      偏离 spec §5.6.3"立即重解析"）只在 kill 把双角色跳闸错开时暴露，出现率 ≈1.5%
      轮次，seed=20260725 的 20 轮恰好一次都没踩中。T6 的跨 seed 取证已把 seed=7 证成
      **敏感锚点**：monkeypatch 关掉 R2 修复后，seed=7 精确复现 types-mismatch
      （report/defect 相邻互换）；打开修复后 20/20 全绿。故把 7 固化进硬门槛参数，
      让这条回归今后跑不掉（IMPLEMENTATION_NOTES.md「M5 独立评审」major-3 裁决）。
    """
    import orch.chaos

    ws = tmp_dir / f"m5-chaos-20-seed{seed}"
    ws.mkdir()

    rounds = 20
    harness = orch.chaos.AdapterChaosHarness(
        workspace=ws,
        script=like_feature_script,
        seed=seed,
        unavailable_after=2,
    )
    report = harness.run(rounds=rounds)

    assert rounds >= 20, f"M5 §15 硬门槛：轮数不得少于 20，实测 {rounds}"
    assert _report_field(report, "rounds") == rounds
    failed = list(_report_field(report, "failed_seeds"))
    assert _report_field(report, "passed") == rounds and failed == [], (
        f"M5 §15 硬门槛：{rounds} 轮必须 100% 通过；"
        f"passed={_report_field(report, 'passed')}/{rounds}, "
        f"failed_seeds({len(failed)}):\n" + "\n".join(f"  - {fs!r}" for fs in failed)
    )
    assert _report_field(report, "ledger_ok") is True, failed
    assert _report_field(report, "terminal_ok") is True, failed
