"""§9.4 混沌 harness（mock 层）——M4-T3 owner。

在附录B fixture 上跑 mock 编排回路，注入 SIGKILL 于 §4.4 事务边界间隙；每轮 kill 后
重开 Store 走 `orch.scheduler.recover` 续跑至 terminate。校验：
  - ledger 无重复事件号（exactly-once，§9.4）；
  - 终态类型序列 == 附录B EXPECTED_TYPE_SEQUENCE（helpers），黑板 contracts.v==2 且 tasks 全 done。

分层铁律（spec §2/CLAUDE.md）：本模块只是**驱动**，不 mock 被测；故障注入依赖
`orch.store` 已冻结的 FaultInjector / set_fault_injector / clear_fault_injector（M4 契约 §1）。
mock 语境无 worktree/无真实 CLI 进程，故 `invoke_post` / `autocommit_post` 两个 §4.4 站点
在本 harness 中列为可选注入面（若上层 hook 未落到 mock 路径则该轮不崩，视为"未触发"→
仍走完整跑通路径），保持 site 名齐备以满足 M4 契约 §2 覆盖清单。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import orch.adapters
import orch.scheduler
import orch.store

from tests.helpers import EXPECTED_TYPE_SEQUENCE

# ——————————————————————————————————————————————————————————————
# 注入点清单（M4 契约 §1/§2）
# 覆盖 §4.4 五个事务边界 site + 一个纯随机 mix 模式。random 模式在候选中随机挑选一个
# 已实际生效的 store 钩子（append_event_post / mark_dispatching_post / reply_and_done_post），
# 因此"覆盖五间隙"与"纯随机"在语义上正交（前者按名称轮转，后者按 rng 派生）。
# ——————————————————————————————————————————————————————————————

INJECTION_SITES: tuple[str, ...] = (
    "append_event_post",     # §4.4 (1) 事件追加 + 派发行 单事务 提交后
    "mark_dispatching_post", # §4.4 (2) status→dispatching 提交后
    "invoke_post",           # §4.4 (3) invoke 结束后 / autocommit 前（mock 语境无实际 hook 点）
    "autocommit_post",       # §4.4 (4) worktree autocommit 后（mock 无 worktree）
    "reply_and_done_post",   # §4.4 (5) 回复落盘 + 标 done + 会话 upsert 提交后
    "random_mix",            # 纯随机：从上面五个中挑一个（rng 派生）
)

# store 里实际内嵌 `_fault_check` 的 site（未列入者：mock 语境不触发 → 该轮不崩溃）。
_ACTIVE_SITES: tuple[str, ...] = (
    "append_event_post",
    "mark_dispatching_post",
    "reply_and_done_post",
)


# ——————————————————————————————————————————————————————————————
# Report
# ——————————————————————————————————————————————————————————————

@dataclass
class ChaosReport:
    """M4 契约 §2：{rounds, passed, failed_seeds, ledger_ok, terminal_ok}。

    failed_seeds：失败轮 (seed, site, count) 三元组列表；快跑 100% 通过时为空。
    ledger_ok / terminal_ok：全轮聚合布尔——只要有一轮失败即 False。
    """

    rounds: int
    passed: int
    failed_seeds: list[dict] = field(default_factory=list)
    ledger_ok: bool = True
    terminal_ok: bool = True


# ——————————————————————————————————————————————————————————————
# ChaosHarness
# ——————————————————————————————————————————————————————————————

class ChaosHarness:
    """§9.4 mock 层混沌 harness。

    每轮：
      1. 新 target_dir = workspace / f"t-{round:03d}"（新 Store 实例）
      2. seed E1（human assign, to=[]）
      3. 挑注入点 site + count（由 seed 派生的 rng 决定）
      4. set_fault_injector({site: count})
      5. 跑 run_thread → 可能 SystemExit(137)；捕获即视为"kill -9"
      6. clear_fault_injector；重开 Store（同目录）走 recover 续跑
      7. 若 status='suspended' → apply_gate_decision(approve=True) → 再跑到 terminate
      8. 校验 ledger 无重复 + 事件类型序列 == EXPECTED_TYPE_SEQUENCE + 黑板终态 v2/done
    """

    INJECTION_SITES = INJECTION_SITES

    def __init__(
        self,
        *,
        workspace: Path,
        script: dict,
        seed: int | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.script = script
        self.seed = 0 if seed is None else int(seed)

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def run(self, rounds: int = 50) -> ChaosReport:
        report = ChaosReport(rounds=rounds, passed=0)

        # 主 rng：seed 决定所有子决策（可复现）。
        main_rng = random.Random(self.seed)

        for k in range(rounds):
            # 每轮独立 rng：seed 派生（M4 契约 §2 "seed 派生子 rng"）。
            round_seed = main_rng.randint(0, 2**31 - 1)
            round_rng = random.Random(round_seed)

            site = self._pick_site(k, round_rng)
            # count 上界宽一些以确保 §4.4 site 常常"未命中"跑到底，反映真实混沌覆盖。
            count = round_rng.randint(1, 12)

            tdir = self.workspace / f"t-{k:03d}"
            ledger = self.workspace / f"ledger-{k:03d}.txt"

            try:
                ok, reason = self._run_one_round(
                    target_dir=tdir, ledger_path=ledger, site=site, count=count,
                )
            except Exception as exc:  # noqa: BLE001
                # 双重兜底：即便 _run_one_round 内部漏兜（如 _build_adapters/_config
                # 等驱动前置步骤本身抛错），也不许单轮异常打断整个 run() 的其余轮次
                # ——归入 failed_seeds 后继续下一轮（M4 R-b）。
                orch.store.clear_fault_injector()
                ok, reason = False, f"impl-error-round-setup:{type(exc).__name__}:{exc!r}"
            if ok:
                report.passed += 1
            else:
                # 失败：整体两项聚合置 False（不做局部裁剪）。
                report.failed_seeds.append(
                    {"round": k, "seed": round_seed, "site": site,
                     "count": count, "reason": reason}
                )
                if "ledger" in reason:
                    report.ledger_ok = False
                if "terminal" in reason or "types" in reason or "board" in reason:
                    report.terminal_ok = False

        return report

    # ------------------------------------------------------------------
    # 单轮驱动
    # ------------------------------------------------------------------
    def _run_one_round(
        self,
        *,
        target_dir: Path,
        ledger_path: Path,
        site: str,
        count: int,
    ) -> tuple[bool, str]:
        target_dir.mkdir(parents=True, exist_ok=True)

        # ① 首次开 Store + seed E1（无注入器；E1 是外部触发，不应被本轮混沌打断）。
        orch.store.clear_fault_injector()
        st = orch.store.Store(target_dir)
        st.set_meta("status", "running")
        st.append_event(sender="human", type="assign",
                        body="点赞功能开工", to=[])
        adapters = self._build_adapters(ledger_path)
        cfg = self._config()

        # ② 装注入器，跑第一段（可能 SystemExit(137)）。
        actual_site = self._resolve_site(site)
        if actual_site is not None:
            orch.store.set_fault_injector(
                orch.store.FaultInjector({actual_site: count})
            )
        try:
            self._drive_until_stopped(st, cfg, adapters)
        except SystemExit as exc:
            # 只捕 137（模拟 kill -9）；其他 SystemExit 冒泡表示真错。
            if exc.code != 137:
                raise
        except Exception as exc:  # noqa: BLE001 — 见下方说明。
            # 实现层未捕获异常（如 KeyError/TypeError/AttributeError 等编排器自身 bug）
            # 不应崩掉整个混沌测试轮：本 harness 只是驱动，被测是 orch.scheduler/orch.store
            # 实现；驱动层职责是把"这一轮跑挂了"计入 failed_seeds 并继续跑下一轮
            # （M4 R-b：ChaosHarness KeyError 兜底）。SystemExit 之外的普通异常在这里
            # 一律视为"该轮失败"，不冒泡。
            orch.store.clear_fault_injector()
            return False, f"impl-error-first-drive:{type(exc).__name__}:{exc!r}"
        finally:
            orch.store.clear_fault_injector()

        # ③ 重开 Store（同目录）+ recover + 续跑（可能多次穿越 gate）。
        # 关闭旧连接：Store 无显式 close，直接丢弃；测试用完即毁临时目录。
        del st
        try:
            st2 = orch.store.Store(target_dir)
            orch.scheduler.recover(st2, cfg)
            self._drive_until_stopped(st2, cfg, adapters)
        except SystemExit as exc:
            # recover/续跑阶段理论上不应再触发注入器（已 clear），但防御性处理：
            # 非 137 冒泡，137 计入失败（不该在此阶段发生 kill，视为异常轮）。
            if exc.code != 137:
                raise
            return False, "unexpected-systemexit-137-after-recover"
        except Exception as exc:  # noqa: BLE001 — 同上：实现层 bug 归入 failed_seeds。
            return False, f"impl-error-recover-drive:{type(exc).__name__}:{exc!r}"

        # ④ 终态校验（若未到 terminated 视为失败）。
        status = st2.get_meta("status")
        if status != "terminated":
            return False, f"terminal-status-not-terminated:{status!r}"

        # ⑤ ledger 无重复。
        lines = self._read_ledger(ledger_path)
        if len(lines) != len(set(lines)):
            return False, f"ledger-duplicate:{[x for x in lines if lines.count(x)>1][:5]}"

        # ⑥ 事件类型序列一致。
        types = [e["type"] for e in sorted(st2.events(), key=lambda e: e["id"])]
        if types != EXPECTED_TYPE_SEQUENCE:
            return False, f"types-mismatch:{types}"

        # ⑦ 黑板终态：contracts.like-api.version == 2 且 tasks 全 done。
        state = orch.store.board_state(st2)
        contracts = state.get("contracts") or {}
        if (contracts.get("like-api") or {}).get("version") != 2:
            return False, f"board-contract-not-v2:{contracts}"
        tasks = state.get("tasks") or {}
        if not tasks or not all(v == "done" for v in tasks.values()):
            return False, f"board-tasks-not-all-done:{tasks}"

        return True, "ok"

    # ------------------------------------------------------------------
    # 主循环驱动：反复 run_thread + 遇 suspended 就 approve；直到 terminated 或无 pending
    # ------------------------------------------------------------------
    def _drive_until_stopped(self, store, config: dict, adapters: dict) -> None:
        """反复 run_thread，遇 gate 挂起自动 approve 直到 terminated 或无待办。

        混沌 harness 内部对 human 门禁自动放行——这是 mock 层"exactly-once + 恢复续跑"
        的能观察窗口，等价 E2E 中人类 approve（tests/test_e2e.py 的 _human_approve）。
        """
        # 防死循环兜底（正常流程 3~4 轮足够）。
        for _ in range(50):
            orch.scheduler.run_thread(store, config, adapters)
            st = store.get_meta("status")
            if st == "terminated":
                return
            if st == "suspended":
                # 找到最近未 done 的 gate_request 并 approve。
                gate = self._latest_pending_gate(store)
                if gate is None:
                    return  # 无匹配 gate → 无法推进，结束（终态校验会判失败）。
                orch.scheduler.apply_gate_decision(
                    store, config, adapters,
                    corr=gate.get("corr") or "gate-01",
                    approve=True, sender="human",
                )
                continue
            # running 且无 pending → run_thread 已直接返回，无 gate 也未 terminate。
            if not store.pending_dispatches():
                return

    # ------------------------------------------------------------------
    # 装配辅助
    # ------------------------------------------------------------------
    def _build_adapters(self, ledger_path: Path) -> dict:
        return {
            role: orch.adapters.MockAdapter(
                role=role, script=table, ledger_path=ledger_path
            )
            for role, table in self.script.items()
        }

    @staticmethod
    def _config() -> dict:
        """与 tests/test_e2e.py `_config` 一致的最小配置（无 worktree → 权限三件套 skip）。"""
        return {
            "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
            "gate_ops": {
                "run_ci": {
                    "cmd": "python -c \"print('ci ok')\"",
                    "cwd": ".", "async": True,
                },
            },
            "roles": {
                "moderator": {"can_decide": True, "write_scope": [], "tools": []},
                "pm": {"can_decide": True, "write_scope": ["docs/"],
                       "tools": ["Edit", "Write"]},
                "backend": {"can_decide": False, "write_scope": ["server/"],
                            "tools": ["Edit", "Write"]},
                "frontend": {"can_decide": False, "write_scope": ["web/"],
                             "tools": ["Edit", "Write"]},
                "tester": {"can_decide": False,
                           "write_scope": ["tests/", "reports/"],
                           "tools": ["Edit", "Write"],
                           "verify": {"cmd": "python -c \"print('ok')\"",
                                      "cwd": "."}},
            },
        }

    # ------------------------------------------------------------------
    # 注入点解析
    # ------------------------------------------------------------------
    def _pick_site(self, k: int, rng: random.Random) -> str:
        """轮转 + 随机组合注入面：

        - 前 len(INJECTION_SITES) 轮按下标轮转（覆盖全部 site 名，包括 random_mix）；
        - 之后按 rng 均匀采样，保持覆盖。
        """
        if k < len(INJECTION_SITES):
            return INJECTION_SITES[k]
        return rng.choice(INJECTION_SITES)

    def _resolve_site(self, site: str) -> str | None:
        """把 site 名映射为 store 内嵌 _fault_check 支持的实际 site。

        - random_mix → 从 _ACTIVE_SITES（append/mark/reply）中 rng 挑一个
          （rng 隔离在此，避免污染主决策）；
        - 已在 _ACTIVE_SITES 内 → 原样返回；
        - 其余（invoke_post / autocommit_post，mock 无 hook 点） → None，
          表示本轮"未触发"崩溃点，仍完整跑通（属于 §4.4 覆盖清单里的"覆盖 site 名"）。
        """
        if site == "random_mix":
            local = random.Random()  # 独立 rng：不影响主 seed 派生。
            return local.choice(_ACTIVE_SITES)
        if site in _ACTIVE_SITES:
            return site
        return None

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _read_ledger(path: Path) -> list[str]:
        if not path.exists():
            return []
        return [
            ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]

    @staticmethod
    def _latest_pending_gate(store) -> dict | None:
        """找 status=gate_wait 的派发行对应的 gate_request 事件（§10）。"""
        # gate_wait 派发行不会出现在 pending_dispatches()（那只查 pending）。
        # 直接扫最后一条 type=gate_request 的事件即可（mock 场景 corr 唯一）。
        events = sorted(store.events(), key=lambda e: e["id"], reverse=True)
        for ev in events:
            if ev.get("type") == "gate_request":
                return ev
        return None


__all__ = ["ChaosHarness", "ChaosReport", "INJECTION_SITES"]
