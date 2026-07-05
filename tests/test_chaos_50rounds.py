"""M4-T5 · 混沌 50 轮硬门槛（spec §15 M4 验收）。

【硬门槛来源】
  spec §15 M4 验收：`ChaosHarness.run(rounds=50)` 必须 100% 通过（passed == 50，
  failed_seeds == [] 或 ()，ledger_ok == True，terminal_ok == True）。
  §9.4 「mock 层 100%」= 50 轮全体不失败；本用例是 M4 的**唯一**硬门槛判据。

【运行策略】
  默认 pytest **不跑** 本文件（打了 `chaos_50` 标记；见 conftest.py 的
  `pytest_collection_modifyitems` 自动 skip）。
  仅当命令行显式传入 `--chaos-50` 时才收集并运行。这样：
    - 常规 CI 全套只跑 200+ 用例的其它绿基线（不吃 50 轮时长）；
    - M4 验收阶段人手运行 `pytest --chaos-50 tests/test_chaos_50rounds.py` 触发硬门槛。

【失败诊断】
  为了让每一失败轮的 (seed, site, reason) 都进 pytest 报告：
    - 本用例**逐轮**驱动 harness._run_one_round(...)，把 harness 未捕获的异常
      （SystemExit(137) 除外——那是模拟 kill -9 的正常路径）以字符串化 reason
      归入 `failed_seeds`；
    - 断言仍强制 `passed == 50`，异常轮同样计为失败；**不弱化**门槛。
  逐轮驱动只是把 `ChaosHarness.run(rounds=50)` 的循环体从内部搬到测试里，
  逻辑与 T3 harness `run()` 一致（同一 seed → 同一 rng 派生序列 → 同一 site/count），
  不修改被测代码。Lead 拿到本条报告即可据 seed + site + reason 精确回放，
  在 T3/T2 修复后再次 --chaos-50 复跑至 100% 通过。

【CLAUDE.md 约束】
  - 顶层只 `import orch.chaos`；符号在函数体内引用（未实现即 AttributeError → 红）。
  - 100% 通过 = passed == 50 且 failed_seeds 为空；两者缺一不可。
  - `ledger_ok` / `terminal_ok` 独立断言，方便定位失败面。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

import orch


# ==================================================================
# M4 硬门槛：50 轮 100% 通过
# ==================================================================

@pytest.mark.chaos_50
def test_chaos_harness_50_rounds_hard_gate(tmp_dir, like_feature_script):
    """spec §15 M4：`ChaosHarness.run(rounds=50)` 必须 100% 通过。

    通过判据（缺一即失败）：
      1. rounds == 50
      2. passed == 50
      3. failed_seeds == [] 或 == ()
      4. ledger_ok is True
      5. terminal_ok is True

    逐轮驱动细节见文件抬头「失败诊断」；等价于 `harness.run(rounds=50)`，
    仅把异常轮归入 failed_seeds 而不是整测试 abort。
    """
    import orch.chaos

    ws = tmp_dir / "chaos-50-ws"
    ws.mkdir()

    # 固定 seed=20260704（今日锚点）：确保失败可完整复现给 Lead。
    seed = 20260704
    rounds = 50

    harness = orch.chaos.ChaosHarness(
        workspace=ws,
        script=like_feature_script,
        seed=seed,
    )

    # ---- 逐轮驱动，与 harness.run 内部循环等价（同 seed → 同 rng 序列）----
    main_rng = random.Random(seed)
    passed = 0
    failed_seeds: list[dict] = []
    ledger_ok = True
    terminal_ok = True

    for k in range(rounds):
        round_seed = main_rng.randint(0, 2**31 - 1)
        round_rng = random.Random(round_seed)
        site = harness._pick_site(k, round_rng)
        count = round_rng.randint(1, 12)

        tdir = ws / f"t-{k:03d}"
        ledger = ws / f"ledger-{k:03d}.txt"

        try:
            ok, reason = harness._run_one_round(
                target_dir=tdir, ledger_path=ledger, site=site, count=count,
            )
        except SystemExit:
            # harness 本已内部捕 137；若冒泡说明 exit code 非 137，真错。
            raise
        except BaseException as exc:
            ok = False
            reason = f"uncaught-{type(exc).__name__}:{exc!s}"[:200]

        if ok:
            passed += 1
        else:
            failed_seeds.append({
                "round": k, "seed": round_seed, "site": site,
                "count": count, "reason": reason,
            })
            if "ledger" in reason:
                ledger_ok = False
            if "terminal" in reason or "types" in reason or "board" in reason:
                terminal_ok = False

    # ---- 断言（与直接跑 harness.run 后校验 ChaosReport 等价）----

    # ① 轮数字面（防止误改 rounds）。
    assert rounds == 50, (
        f"M4 §15 硬门槛：rounds 必须 == 50；实测 rounds={rounds}"
    )

    # ② 100% 通过：任何失败都要打完整 failed_seeds（每条含 seed+site+reason）。
    assert passed == 50 and failed_seeds == [], (
        f"M4 §15 硬门槛：50 轮必须 100% 通过；"
        f"passed={passed}/50, failed_seeds({len(failed_seeds)}):\n"
        + "\n".join(f"  - {fs!r}" for fs in failed_seeds)
    )

    # ③ ledger_ok / terminal_ok 独立断言（§9.4 mock 层校验两个面）。
    assert ledger_ok is True, (
        f"M4 §9.4：ledger 必须无重复事件号（exactly-once）；"
        f"ledger_ok={ledger_ok!r}, failed_seeds={failed_seeds!r}"
    )
    assert terminal_ok is True, (
        f"M4 §9.4：终态类型序列与黑板必须与不中断基准一致；"
        f"terminal_ok={terminal_ok!r}, failed_seeds={failed_seeds!r}"
    )
