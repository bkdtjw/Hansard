"""§9.4 混沌 harness（mock 层）——M4-T3 owner / R-T1 注入面补全。

在附录B fixture 上跑 mock 编排回路，注入 SIGKILL 于 §4.4 事务边界间隙；每轮 kill 后
重开 Store 走 `orch.scheduler.recover` 续跑至 terminate。校验附录B 四断言：
  - 事件类型序列 == EXPECTED_TYPE_SEQUENCE（本包自有常量，审计 G 解耦）；
  - 黑板终态一致（contracts.like-api.version==2 且 tasks 全 done）；
  - mock ledger 无重复事件号（exactly-once，§9.4）；
  - **混沌轮终态与不中断基准逐字节一致**（附录B 第四断言，R-T1 补全）。

分层铁律（spec §2/CLAUDE.md）：本模块只是**驱动**，不 mock 被测；故障注入依赖
`orch.store` 已冻结的 FaultInjector / set_fault_injector / clear_fault_injector（M4 契约 §1）。

R-T1 注入面补全（审计 A1/A2/G）：
  - §4.4 五个事务边界 site 全部映射为**真实注入**（删除 invoke_post/autocommit_post
    的 None 降级）：前三个（append/mark/reply）落在 store 内嵌 _fault_check；后两个
    （invoke_post/autocommit_post）落在调度层 core/async_core 的控制流位置经
    store.fault_check 触发（Lead §17 裁决：按控制流位置触发，mock 无 worktree 照样命中）。
  - 附录B 第四断言：每 harness 先跑一次不中断基准，捕获终态产物字节（ledger + 黑板
    规范化 JSON），混沌轮终态逐字节比较，不一致=该轮失败。产物本就确定化（ledger 无
    时间戳；黑板 state.json 由 store._write_state 以 sort_keys=True dump），无需裁剪断言。

M5-T6 追加（本文件后半段 "M5 · 适配器切换间隙混沌"）：
  spec §15 M5 末段"切换间隙 kill -9 混沌 ≥ 20 轮 100% 通过（§9.4 第一层扩展场景）"、
  docs/m5-contract.md §8。场景 = 主 mock 于第 k 次 invoke 起抛额度错误 → §5.6.3 自动
  跳闸 → §5.6.2 备胎接手 → 附录B 仍跑到 terminated；kill -9 注入落在**切换间隙**
  （跳闸落盘前后 / 切换审计前后 / 换绑重派前后）+ 纯随机。入口 ``AdapterChaosHarness``、
  注入面 ``ADAPTER_INJECTION_SITES``。
"""

from __future__ import annotations

import json
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import orch.adapters
import orch.scheduler
import orch.store

from orch.adapters.state import AdapterAvailability
from orch.chaos.expected import EXPECTED_TYPE_SEQUENCE
from orch.scheduler.availability import (
    KIND_ADAPTER_BLOCKED,
    KIND_ADAPTER_TRIP,
    KIND_FALLBACK_SWITCH,
    METRIC_FALLBACK_SWITCH,
)


# ——————————————————————————————————————————————————————————————
# 幂等 mock 适配器（§9.2 层 2/3 的 mock 身：崩溃恢复后同 (role,event_id) 只落一次副作用）
# ——————————————————————————————————————————————————————————————

class _IdempotentMockAdapter(orch.adapters.MockAdapter):
    """MockAdapter 的**幂等**变体，供混沌 harness 忠实模拟"崩溃恢复后重发不重做"。

    根因（R-T1，审计 A1）：§4.4 间隙(3)invoke_post/(4)autocommit_post 是"崩溃高发区，
    盘上无痕迹"——invoke 已返回（mock 已向 ledger 追加一行副作用）但 reply_and_done 未
    落盘就 kill。§9.1 恢复对该无回复、未超时的 dispatching 行走 c) 重派发 → 主循环
    重新 invoke → 若 mock 无脑再追加一行，就产生**重复副作用**（ledger 重复行），违反
    §9.4 exactly-once。

    这**不是** orchestrator 的缺陷：§4.4 有意把"回复落盘 + 标 done"合并单事务消除该
    窗口，但 invoke↔reply 之间的窗口天然是"至少一次投递"（§9.2）。§9.2 明列去重责任
    分三层，其中**活会话/死会话层**要求 agent 幂等——"见重复编号原样重发上次信封，不重做
    操作"（层2）、"git log 已有 wip:{T}@E{n} → 只需补发信封"（层3）。mock 的 ledger 行
    `{role}:{event_id}` 正是那个 `wip:{T}@E{n}` 去重标记的 mock 对应物。

    本类据此做**通用规则**（非对某 seed 特判）：invoke 前先查 ledger 是否已含本批触发号
    对应的 `{role}:{event_id}` 行；已含 → 跳过副作用追加（"补发信封不重做"），否则委托
    父类正常追加。返回信封仍由父类查表得到（同 (role,event_id) 恒定，故补发一致）。
    """

    def invoke(self, view: dict, sess: dict | None) -> tuple[dict, dict | None]:
        event_id = max(view["event_ids"])
        marker = f"{self.role}:{event_id}"
        already = False
        if self.ledger_path.exists():
            existing = self.ledger_path.read_text(encoding="utf-8").splitlines()
            already = marker in existing
        if not already:
            # 首次处理该 (role,event_id)：父类正常查表 + 追加 ledger 副作用。
            return super().invoke(view, sess)
        # 重发（崩溃恢复后重派发）：只补发信封，不重做副作用（§9.2 层2/3）。
        scripted = self.script[event_id]
        env = {
            k: scripted[k]
            for k in orch.adapters._AUTHOR_FIELDS
            if k in scripted
        }
        return env, sess


# ——————————————————————————————————————————————————————————————
# 注入点清单（M4 契约 §1/§2；R-T1 补全为 5 site 全真实注入）
# 覆盖 §4.4 五个事务边界 site + 一个纯随机 mix 模式。random 模式从五个真实 site 中
# 随机挑选（rng 派生），因此"覆盖五间隙"与"纯随机"在语义上正交。
# ——————————————————————————————————————————————————————————————

INJECTION_SITES: tuple[str, ...] = (
    "append_event_post",     # §4.4 (1) 事件追加 + 派发行 单事务 提交后（store 内嵌）
    "mark_dispatching_post", # §4.4 (2) status→dispatching 提交后（store 内嵌）
    "invoke_post",           # §4.4 (3) invoke 结束后 / reply 前（调度层控制流位置）
    "autocommit_post",       # §4.4 (4) autocommit + 越权审计后 / reply 前（调度层控制流位置）
    "reply_and_done_post",   # §4.4 (5) 回复落盘 + 标 done + 会话 upsert 提交后（store 内嵌）
    "random_mix",            # 纯随机：从上面五个真实 site 中挑一个（rng 派生）
)

# R-T1：五个 §4.4 site 全部为真实注入面（不再有 None 降级）。
# store 内嵌 3 个（append/mark/reply）；调度层控制流 2 个（invoke_post/autocommit_post）
# 经 store.fault_check 从同一全局 FaultInjector 触发。
_ACTIVE_SITES: tuple[str, ...] = (
    "append_event_post",
    "mark_dispatching_post",
    "invoke_post",
    "autocommit_post",
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

    seed（M5-T6 追加，**纯附加**、带缺省值，M4 契约 §2 的五字段一字未动）：本次 run 的
    主 seed，供"失败可复现"（Lead 裁决⑥：seed 记录进 ChaosReport）。所有子决策
    （每轮 round_seed → site / count / kill 段数）都由它派生，故记它即可完整回放。
    """

    rounds: int
    passed: int
    failed_seeds: list[dict] = field(default_factory=list)
    ledger_ok: bool = True
    terminal_ok: bool = True
    seed: int | None = None


# ——————————————————————————————————————————————————————————————
# 不中断基准终态产物（附录B 第四断言的比较基线）
# ——————————————————————————————————————————————————————————————

@dataclass
class BaselineArtifacts:
    """一次不中断基准跑的终态产物字节（逐字节比较基线，R-T1）。

    ledger_bytes：mock ledger 文件原始字节（无时间戳，天然确定化）。
    state_bytes：黑板 state.json 原始字节（store._write_state 已 sort_keys=True dump，
                 确定化；frozen_at 是事件号而非时间戳，无非确定字段）。
    """

    ledger_bytes: bytes
    state_bytes: bytes


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
      8. 校验附录B 四断言：ledger 无重复 + 类型序列一致 + 黑板 v2/done +
         终态产物与不中断基准逐字节一致
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
        # 不中断基准：懒计算一次并缓存（fixture 确定性 → 终态与 seed 无关）。
        self._baseline: BaselineArtifacts | None = None

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def run(self, rounds: int = 50, metrics_store=None) -> ChaosReport:
        """跑 rounds 轮混沌；可选 metrics_store 落 §13 混沌指标（R-T4）。

        metrics_store（缺省 None）：**行为不变**——不传时不落任何指标，与 R-T1 既有
        50 轮硬门槛调用路径逐字一致（test_chaos_50rounds 直接调 _run_one_round，也不受
        本参数影响）。传入 Store 时，全部轮跑完后落两条 §13 混沌指标行（可复算）：
          - `chaos_rounds`           = rounds（轮数）；
          - `chaos_mock_pass_pct`    = passed / rounds * 100（mock 层通过率，硬门槛应 100）。
        真实层完成率仍不落（chaos_real_pass_pct）→ orch metrics 显示 N/A（Q1/Q2 陪跑边界，
        不伪造）。落盘走 store.record_metric 公开原语（不改 metrics DDL §4.3）。
        """
        report = ChaosReport(rounds=rounds, passed=0, seed=self.seed)

        # 先跑不中断基准（附录B 第四断言的逐字节比较基线，R-T1）。
        baseline = self._ensure_baseline()

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
                    baseline=baseline,
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
                if ("terminal" in reason or "types" in reason
                        or "board" in reason or "baseline" in reason):
                    report.terminal_ok = False

        # §13 采集点4（R-T4）：可选把混沌轮数与 mock 层通过率落 metrics 表（可复算）。
        # 缺省 metrics_store=None → 完全跳过（行为不变，向后兼容 50 轮硬门槛）。
        if metrics_store is not None:
            mock_pass_pct = (report.passed / rounds * 100.0) if rounds else 0.0
            metrics_store.record_metric("chaos_rounds", float(rounds), extra="mock")
            metrics_store.record_metric(
                "chaos_mock_pass_pct", float(mock_pass_pct), extra="mock",
            )

        return report

    # ------------------------------------------------------------------
    # 不中断基准
    # ------------------------------------------------------------------
    def _ensure_baseline(self) -> BaselineArtifacts:
        """跑一次不中断（无故障注入）的完整流程，捕获终态产物字节（懒计算，缓存）。"""
        if self._baseline is not None:
            return self._baseline
        base_dir = self.workspace / "_baseline"
        base_ledger = self.workspace / "_baseline-ledger.txt"
        orch.store.clear_fault_injector()
        st = orch.store.Store(base_dir)
        st.set_meta("status", "running")
        st.append_event(sender="human", type="assign", body="点赞功能开工", to=[])
        adapters = self._build_adapters(base_ledger)
        cfg = self._config()
        self._drive_until_stopped(st, cfg, adapters)
        # 终态：必须已 terminated（基准跑不注入故障，应一次到底）。
        status = st.get_meta("status")
        if status != "terminated":
            raise RuntimeError(f"baseline 未到达 terminated：status={status!r}")
        self._baseline = self._capture_artifacts(base_dir, base_ledger)
        return self._baseline

    @staticmethod
    def _capture_artifacts(target_dir: Path, ledger_path: Path) -> BaselineArtifacts:
        """读终态产物原始字节：ledger 文件 + 黑板 state.json（均已确定化）。"""
        ledger_bytes = (
            ledger_path.read_bytes() if ledger_path.exists() else b""
        )
        state_path = target_dir / "blackboard" / "state.json"
        state_bytes = state_path.read_bytes() if state_path.exists() else b""
        return BaselineArtifacts(ledger_bytes=ledger_bytes, state_bytes=state_bytes)

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
        baseline: BaselineArtifacts | None = None,
    ) -> tuple[bool, str]:
        target_dir.mkdir(parents=True, exist_ok=True)
        if baseline is None:
            baseline = self._ensure_baseline()

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

        # ⑧ 附录B 第四断言：终态产物与不中断基准逐字节一致（R-T1）。
        actual = self._capture_artifacts(target_dir, ledger_path)
        if actual.ledger_bytes != baseline.ledger_bytes:
            return False, (
                "baseline-ledger-mismatch:"
                f"len={len(actual.ledger_bytes)}!=base{len(baseline.ledger_bytes)}"
            )
        if actual.state_bytes != baseline.state_bytes:
            return False, (
                "baseline-board-mismatch:"
                f"len={len(actual.state_bytes)}!=base{len(baseline.state_bytes)}"
            )

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
        # R-T1：用幂等 mock 变体（§9.2 层2/3）——崩溃恢复后同 (role,event_id) 只落一次
        # ledger 副作用，模拟"重发信封不重做操作"。基准跑与混沌轮共用同一构造，故基准
        # 产物与混沌恢复终态可逐字节比较（不引入构造差异）。
        return {
            role: _IdempotentMockAdapter(
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
        """把 site 名映射为实际注入 site（R-T1：5 个 §4.4 site 全部真实注入，无 None 降级）。

        - random_mix → 从 _ACTIVE_SITES（5 个真实 site）中 rng 挑一个
          （rng 隔离在此，避免污染主决策）；
        - 已在 _ACTIVE_SITES 内（含 invoke_post / autocommit_post）→ 原样返回；
          它们的 fault_check 已在 store（append/mark/reply）或调度层控制流位置
          （invoke_post/autocommit_post，经 store.fault_check）落到 mock 路径上，故本轮
          真的会在该 site 崩溃。
        - 其余（仅 "random_mix" 以外的未知名）→ None（防御，正常不会到达）。
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


# ======================================================================
# M5 · 适配器切换间隙混沌（T6）
#   依据：spec §15 M5 末段 / §9.4 第一层扩展场景 / §5.6.2 / §5.6.3；
#         docs/m5-contract.md §8。
#
# 场景（契约 §8）：同一附录B 脚本表，每个角色配 main_{role}（主）与 spare_{role}
# （备）两个 mock 实例；主实例自该角色**第 unavailable_after 次调用**起抛额度类错误
# → 调度层 §5.6.3 第 1 条立即跳闸（disable by=auto，落全局状态文件）→ 派发行回 pending
# → 下轮 §5.6.2 重解析由备胎接手 → 附录B 任务仍跑到 terminated。
#
# 与 M4 的差别只在**注入面**与**比较口径**，被测对象与驱动方式完全一致：
#   · 注入面：六个 M5 切换间隙（见 ADAPTER_INJECTION_SITES）+ 纯随机（含 §4.4 五间隙）；
#   · 比较口径：降级跑合法地多出若干条"落盘但不生成派发行"的 M5 审计事件，事件号必然
#     整体偏移，故先**剔除 M5 审计事件、把事件号映射为名次**，再逐字节比 ledger 与黑板
#     （与 tests/test_m5_availability.py G 组同一口径；附录B 本就允许事件号偏移）。
# ======================================================================

# 契约 §4 冻结的三种 M5 审计事件 meta.kind（比较前一律剔除；它们不生成派发行）。
_M5_AUDIT_KINDS: tuple[str, ...] = (
    KIND_FALLBACK_SWITCH, KIND_ADAPTER_BLOCKED, KIND_ADAPTER_TRIP,
)

ADAPTER_INJECTION_SITES: tuple[str, ...] = (
    # —— ① 跳闸间隙（§5.6.3 第 1 条）——
    "adapter_trip_pre",       # 额度错误刚抛出：状态文件未写、审计未落、行仍 dispatching
    "adapter_trip_post",      # disable 已原子替换落盘：审计事件未落、行未回 pending
    # —— ② 切换审计间隙（§5.6.2）——
    "fallback_switch_pre",    # 解析出降级绑定：指标与审计事件均未落、换绑未做
    "fallback_switch_post",   # 切换审计事件已落盘：换绑重派尚未发生
    # —— ③ 换绑重派间隙（§5.6.2）——
    "rebind_dispatch_pre",    # 判定会话死亡、sessions 尚未改写
    "rebind_dispatch_post",   # sessions 换绑 + attempts 归零已落盘、重派未标 dispatching
    # —— ④ 纯随机 ——
    "random_mix",             # 从全部真实 site（六个 M5 间隙 + §4.4 五间隙）中派生挑选
)

# 六个真实的 M5 切换间隙（random_mix 之外的全部）。
_ADAPTER_ACTIVE_SITES: tuple[str, ...] = ADAPTER_INJECTION_SITES[:-1]

# random_mix 候选池 = 六个 M5 切换间隙 + M4 §4.4 五个事务边界。
# §9.4 不变量说的是"**任意时刻** kill -9"：把 §4.4 五间隙一并纳入纯随机池，才能覆盖到
# "invoke 已写 ledger、回复未落盘"这类窗口——那正是 §9.2 层2/3 幂等重发语义的考场，
# 也是"断粮计数必须可从盘上重建"的考场（见 _AdapterSwitchMock）。
_ADAPTER_RANDOM_POOL: tuple[str, ...] = _ADAPTER_ACTIVE_SITES + _ACTIVE_SITES

# 单轮内最多"kill → 重启续跑"多少段（裁决⑤"同一现场反复 kill 重启"）；末段恒不注入，
# 保证每轮都能收敛到 terminated（否则校验的是 harness 耐心而非编排器不变量）。
_MAX_KILL_SEGMENTS = 3
# 单段注入计数上界：各 site 每段可命中次数约 5~11 次，取 6 使多数段真的被 kill，
# 同时保留"计数未命中 → 该段跑到底"的自然覆盖（与 M4 同一思路）。
_MAX_SITE_HITS = 6


# ——————————————————————————————————————————————————————————————
# 主/备 mock：调用序与"断粮计数"全部从 ledger（盘上事实）推导
# ——————————————————————————————————————————————————————————————

class _AdapterSwitchMock(orch.adapters.MockAdapter):
    """M5 混沌专用 mock 变体：**同一角色的主备两实例共享一条盘上调用序**。

    为什么不能直接用 MockAdapter / M4 的 _IdempotentMockAdapter（Lead 裁决④）：
      1. 附录B 脚本表以**事件号**为键，而 M5 降级跑合法地插入若干条审计事件（§5.6.2
         "落盘但不生成派发行"），其后事件号整体偏移 → 按事件号查表必然落空。故改按
         **该角色第 i 次调用**取脚本表第 i 项（与 M2 契约 §2 的 scripted_replies[call_no]
         同一惯例，附录B 明文允许事件号偏移）。
      2. 父类的 ``call_no`` 与 M4 变体的重发分支都是**内存态**：一次 kill -9 就归零，
         "第 k 次起断粮"的语义在重启后立刻失真，主实例会重新拥有额度、无限跳闸/恢复。
         主备是两个实例，内存计数也无法在"主跑了 1 次、备接着跑第 2 次"之间接力。
      3. 故本类把**唯一计数依据**定为盘上事实 ——mock ledger 中属于本角色的行数：
             第 i 次调用（i 从 1 起） := 该角色已落 ledger 行数 + 1
         ledger 由主备共写同一文件、崩溃后原样保留，因此该计数：
           · 跨 kill -9 重启成立（盘上事实，不依赖任何内存态）；
           · 在主备之间自然接力（主跑掉的那几次已计入行数）；
           · 与 §9.4 的 exactly-once 对账口径同源（ledger 行 = 副作用恰好一次）。

    §9.2 层2/3 幂等重发：ledger 已含本批触发号的 ``{role}:{event_id}`` 标记 = 本事件的
    副作用早已生效（崩溃发生在 invoke 之后、回复落盘之前），此时**只补发上次那张信封、
    不重做副作用、也不计入断粮计数**——重发不是新的一次调用。补发取的脚本项由该标记在
    本角色 ledger 行中的**位置**决定，故与首次落盘时逐字相同。

    会话（§5.6.2 换绑判据）：返回 ``{"sid", "gen"}`` 使 sessions 表真的有行，
    "effective ≠ sessions.backend → 视为会话死亡"才有东西可换绑（否则 rebind 分支
    在 mock 语境恒为 no-op，"换绑重派"两个注入点就成了摆设）。caps.supports_resume
    仍为 False（_MOCK_CAPS），故渲染恒走冷启动全量，与 M0–M4 路径一致。
    """

    def _role_lines(self) -> list[str]:
        """本角色已落的 ledger 行（顺序即调用序）——唯一的调用序真相源。"""
        if not self.ledger_path.exists():
            return []
        prefix = f"{self.role}:"
        return [
            ln for ln in self.ledger_path.read_text(encoding="utf-8").splitlines()
            if ln.startswith(prefix)
        ]

    def _scripted(self, index: int) -> dict:
        """脚本表第 index 项（键升序）规范化为只含作者字段的信封（§3.1/§7.6）。"""
        keys = sorted(self.script)
        if index >= len(keys):
            raise KeyError(
                f"{self.role} 脚本已耗尽：第 {index + 1} 次取用，表内只有 {len(keys)} 项"
            )
        scripted = self.script[keys[index]]
        return {
            k: scripted[k] for k in orch.adapters._AUTHOR_FIELDS if k in scripted
        }

    def _session(self, index: int) -> dict:
        """确定性会话（同 index 恒同值，故补发与首次逐字一致）。"""
        return {"sid": f"{self.role}-sid-{index + 1}", "gen": 1}

    def invoke(self, view: dict, sess: dict | None) -> tuple[dict, dict | None]:
        event_id = max(view["event_ids"])
        marker = f"{self.role}:{event_id}"
        lines = self._role_lines()

        if marker in lines:
            # 重发（§9.2 层2/3）：补发上次信封，不重做副作用，不占用断粮计数。
            index = lines.index(marker)
            return self._scripted(index), self._session(index)

        index = len(lines)          # 本次是该角色第 index+1 次调用（盘上事实推导）。
        if (self.unavailable_after is not None
                and index + 1 >= int(self.unavailable_after)):
            # 断粮：第 unavailable_after 次起（含该次）恒抛额度类错误（契约 §2）。
            # 注入点 ①-pre：错误已抛出、调度层尚未 disable 落盘。
            orch.store.fault_check("adapter_trip_pre")
            raise orch.adapters.AdapterUnavailableError(
                self.adapter_name, self.unavailable_text,
            )

        env = self._scripted(index)
        # 副作用：ledger 追加一行（= exactly-once 校验依据，同时推进调用序）。
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{marker}\n")
        return env, self._session(index)


# ——————————————————————————————————————————————————————————————
# 探针 Store：把五个 M5 切换间隙映射到 store 公开方法的调用边界
# ——————————————————————————————————————————————————————————————

class _AdapterProbeStore(orch.store.Store):
    """在 Store 的公开落盘边界上补挂 M5 切换间隙的 ``fault_check``（驱动层，不改被测）。

    §4.4 五个事务边界的注入点是 store/调度层**内嵌**的（M4 已冻结）；M5 三个切换间隙
    落在 ``orch.scheduler.availability`` 的控制流里，那里没有、也不该为了混沌而新增
    注入点（可写路径只有 src/orch/chaos/**，且注入钩子不属产品语义）。本类改从**外部**
    观察同一批落盘动作，把间隙精确地锚在它们之间——注入的仍是同一个全局 FaultInjector
    （同一计数语义），被测代码一行未动：

      · ``adapter_trip_post``    ← 追加 meta.kind='adapter_trip' 审计事件**之前**。
        此刻 ``availability.disable()`` 已原子替换落盘（on_unavailable 先 disable 再
        note_trip），审计事件未落、派发行未回 pending —— 正是"跳闸落盘后"的间隙。
      · ``fallback_switch_pre``  ← 记 §13 fallback_switch 指标**之前**（note_fallback_switch
        的第一步）：降级绑定已解析出、指标与审计事件都还没落、换绑没做。
      · ``fallback_switch_post`` ← 追加 meta.kind='fallback_switch' 审计事件**之后**：
        通告已落盘，换绑重派尚未发生。
      · ``rebind_dispatch_pre``  ← ``upsert_session``（换绑第一步）**之前**。该公开原语在
        本场景只被 §5.6.2 的 rebind_session_if_needed 调用（reply_and_done 走的是私有
        _upsert_session_row，不经此处）。
      · ``rebind_dispatch_post`` ← 换绑（upsert_session + 全部 reset_attempts）落盘后的
        **首个** mark_dispatching 之前：sessions 换绑与 attempts 归零都已落盘、重派还没
        标 dispatching。``reset_attempts`` 是 M5 契约 §5 为换绑新增的原语，只此一个调用
        点，故"见到 reset_attempts → 下一次 mark_dispatching 就是重派"是确定的。

    ``_rebind_dirty`` 只是**注入点选择**的进程内游标（一次 kill 就随进程消失，重启后
    该轮换绑早已落盘、不会再触发），不承载任何被测语义——§16.9 禁的是"去重/计数等
    判据驻留内存"，不是驱动层选点。
    """

    def __init__(self, thread_dir) -> None:
        super().__init__(thread_dir)
        self._rebind_dirty = False

    def append_event(self, **kwargs) -> int:
        kind = (kwargs.get("meta") or {}).get("kind")
        if kind == KIND_ADAPTER_TRIP:
            orch.store.fault_check("adapter_trip_post")
        event_id = super().append_event(**kwargs)
        if kind == KIND_FALLBACK_SWITCH:
            orch.store.fault_check("fallback_switch_post")
        return event_id

    def record_metric(self, key: str, value: float, extra: str = "") -> None:
        if key == METRIC_FALLBACK_SWITCH:
            orch.store.fault_check("fallback_switch_pre")
        return super().record_metric(key, value, extra)

    def upsert_session(self, role: str, sid, gen: int, *,
                       backend: str | None = None,
                       last_evt: int | None = None) -> None:
        orch.store.fault_check("rebind_dispatch_pre")
        return super().upsert_session(
            role, sid, gen, backend=backend, last_evt=last_evt,
        )

    def reset_attempts(self, event_id: int, target: str) -> None:
        super().reset_attempts(event_id, target)
        self._rebind_dirty = True

    def mark_dispatching(self, event_id: int, target: str,
                         deadline_ts: float) -> None:
        if self._rebind_dirty:
            self._rebind_dirty = False
            orch.store.fault_check("rebind_dispatch_post")
        return super().mark_dispatching(event_id, target, deadline_ts)


# ——————————————————————————————————————————————————————————————
# 终态规范化（G 组同一口径：剔除 M5 审计事件 → 事件号映射为名次 → 逐字节比）
# ——————————————————————————————————————————————————————————————

@dataclass
class AdapterBaselineArtifacts:
    """M5 不中断基准的**规范化**终态产物（比较基线）。

    与 M4 的 ``BaselineArtifacts``（原始字节）刻意不同：M5 降级跑必然多出若干条审计
    事件、事件号整体偏移，直接比原始字节等于把 spec 明文允许的偏移判成失败。规范化后
    的两份文本仍是逐字符比较——ledger 行数/顺序/角色、契约版本、决策、任务一个都不放松。
    """

    ledger_text: str
    state_text: str


def _rank_map(events: list[dict]) -> dict[int, int]:
    """事件号 → 名次（1 起）：剔除 M5 审计事件后按 id 升序编号（双射、确定）。"""
    ids = [
        ev["id"] for ev in sorted(events, key=lambda e: e["id"])
        if (ev.get("meta") or {}).get("kind") not in _M5_AUDIT_KINDS
    ]
    return {eid: i + 1 for i, eid in enumerate(ids)}


def _normalized_ledger(ledger_path: Path, rank: dict[int, int]) -> str:
    """ledger 逐行 ``{role}:{event_id}`` → ``{role}:{名次}``。"""
    out: list[str] = []
    for line in ChaosHarness._read_ledger(ledger_path):
        role, _, eid = line.rpartition(":")
        out.append(f"{role}:{rank[int(eid)]}")
    return "\n".join(out)


def _normalized_state(target_dir: Path, rank: dict[int, int]) -> str:
    """黑板 state.json 内嵌的两处事件号（contracts[*].frozen_at / decisions[*].evt）换名次。"""
    path = Path(target_dir) / "blackboard" / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for contract in (raw.get("contracts") or {}).values():
        contract["frozen_at"] = rank[int(contract["frozen_at"])]
    for decision in raw.get("decisions") or []:
        decision["evt"] = rank[int(decision["evt"])]
    return json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True)


# ——————————————————————————————————————————————————————————————
# AdapterChaosHarness
# ——————————————————————————————————————————————————————————————

class AdapterChaosHarness(ChaosHarness):
    """§15 M5 / §9.4 第一层扩展场景：适配器切换间隙 kill -9 混沌。

    每轮（裁决⑤ "同一现场反复 kill 重启"，轮与轮之间彻底重置）：
      1. 重置本轮全部现场：线程目录 + ledger + adapter_state.json（防串轮）；
      2. seed E1（human assign），主备 mock 各就位（主 unavailable_after=k）；
      3. 装注入器跑一段 → 可能 SystemExit(137)；捕获即视为 kill -9；
      4. 清注入器 → 重开 Store（同目录）→ ``orch.scheduler.recover`` → **重建 adapter
         实例**（一次 kill 等于换了进程，内存态不许过河）→ 续跑；
         3–4 最多重复 _MAX_KILL_SEGMENTS 段，末段恒不注入、跑到 terminated；
      5. 校验：terminated + ledger 无重复 + 剔除审计后类型序列 == 附录B 期望 + 黑板
         终态 + 场景完整性（五个主绑定都被 by=auto 跳闸、五个角色都有降级切换审计）
         + ledger/黑板与不中断基准规范化后逐字节一致。

    ``adapter_state.json`` 在**轮内**跨重启保留（§5.6.1 它是真相层，"启动时装载"是
    §5.6.4 唯一新增的恢复动作）——跳闸过的主绑定重启后仍是 disabled、继续由备胎接手，
    这正是本卡要验的语义；轮**间**则连同线程目录一起删除，避免上一轮的跳闸串味。
    """

    INJECTION_SITES = ADAPTER_INJECTION_SITES

    def __init__(
        self,
        *,
        workspace: Path,
        script: dict,
        seed: int | None = None,
        unavailable_after: int = 2,
    ) -> None:
        super().__init__(workspace=workspace, script=script, seed=seed)
        # None = 不断粮（不中断基准跑用）；int = 第 k 次调用起抛额度错误。
        self.unavailable_after = (
            None if unavailable_after is None else int(unavailable_after)
        )
        self._adapter_baseline: AdapterBaselineArtifacts | None = None

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def run(self, rounds: int = 20, metrics_store=None) -> ChaosReport:
        """跑 rounds 轮切换间隙混沌（spec §15 M5 硬门槛 ≥ 20 轮 100%）。

        metrics_store 语义与 M4 ``ChaosHarness.run`` 逐字相同（缺省 None → 不落指标）。
        """
        report = ChaosReport(rounds=rounds, passed=0, seed=self.seed)
        baseline = self._ensure_adapter_baseline()
        main_rng = random.Random(self.seed)

        for k in range(rounds):
            round_seed = main_rng.randint(0, 2**31 - 1)
            round_rng = random.Random(round_seed)
            site = self._pick_site(k, round_rng)
            try:
                ok, reason = self._run_one_adapter_round(
                    index=k, site=site, rng=round_rng, baseline=baseline,
                )
            except Exception as exc:  # noqa: BLE001 — 同 M4：单轮异常不打断其余轮次。
                orch.store.clear_fault_injector()
                ok, reason = False, (
                    f"impl-error-round-setup:{type(exc).__name__}:{exc!r}"
                )
            if ok:
                report.passed += 1
            else:
                report.failed_seeds.append(
                    {"round": k, "seed": round_seed, "site": site, "reason": reason}
                )
                if "ledger" in reason:
                    report.ledger_ok = False
                if any(tag in reason for tag in
                       ("terminal", "types", "board", "baseline", "scenario")):
                    report.terminal_ok = False

        if metrics_store is not None:
            mock_pass_pct = (report.passed / rounds * 100.0) if rounds else 0.0
            metrics_store.record_metric("chaos_rounds", float(rounds), extra="mock")
            metrics_store.record_metric(
                "chaos_mock_pass_pct", float(mock_pass_pct), extra="mock",
            )
        return report

    # ------------------------------------------------------------------
    # 注入点选择
    # ------------------------------------------------------------------
    def _pick_site(self, k: int, rng: random.Random) -> str:
        """前 len(ADAPTER_INJECTION_SITES) 轮按下标轮转（保证每个 site 名都跑到），其后随机。"""
        if k < len(ADAPTER_INJECTION_SITES):
            return ADAPTER_INJECTION_SITES[k]
        return rng.choice(ADAPTER_INJECTION_SITES)

    def _resolve_site(self, site: str, rng: random.Random | None = None) -> str | None:
        """site 名 → 实际注入 site。random_mix 用**轮 rng** 挑（seed 可完整回放，裁决⑥）。"""
        if site == "random_mix":
            local = rng if rng is not None else random.Random()
            return local.choice(_ADAPTER_RANDOM_POOL)
        if site in _ADAPTER_ACTIVE_SITES:
            return site
        return None

    # ------------------------------------------------------------------
    # 装配
    # ------------------------------------------------------------------
    def _adapter_config(self, state_path: Path) -> dict:
        """M4 最小配置 + M5 主备绑定与状态文件路径（契约 §1/§3；G 组同一配置形状）。"""
        cfg = self._config()
        adapters_cfg: dict = {}
        for role in cfg["roles"]:
            adapters_cfg[f"main_{role}"] = {"kind": "cli"}
            adapters_cfg[f"spare_{role}"] = {"kind": "cli"}
            cfg["roles"][role]["adapter"] = f"main_{role}"
            cfg["roles"][role]["fallback"] = [f"spare_{role}"]
        cfg["adapters"] = adapters_cfg
        cfg["config_path"] = str(Path(state_path).parent / "config.yaml")
        # 调度侧启用开关（契约 §3 / availability.CONFIG_STATE_PATH_KEY）。
        cfg["adapter_state_path"] = str(state_path)
        return cfg

    def _build_adapter_pair(self, ledger_path: Path, *,
                            unavailable_after: int | None) -> dict:
        """主备两套 mock 实例（同一脚本表、同一 ledger 文件）；键 = adapter 名（契约 §3）。"""
        adapters: dict = {}
        for role, table in self.script.items():
            adapters[f"main_{role}"] = _AdapterSwitchMock(
                role=role, script=table, ledger_path=ledger_path,
                adapter_name=f"main_{role}",
                unavailable_after=unavailable_after,
                unavailable_text="quota exceeded (mock main adapter)",
            )
            adapters[f"spare_{role}"] = _AdapterSwitchMock(
                role=role, script=table, ledger_path=ledger_path,
                adapter_name=f"spare_{role}",
            )
        return adapters

    @staticmethod
    def _reset_ground(target_dir: Path, ledger_path: Path, state_path: Path) -> None:
        """轮间彻底重置现场（裁决⑤）：线程目录 + ledger + 适配器状态文件一并清掉。"""
        for path in (Path(ledger_path), Path(state_path)):
            path.unlink(missing_ok=True)
        target = Path(target_dir)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)

    def _seed_thread(self, target_dir: Path) -> "_AdapterProbeStore":
        """新开线程 + 落 E1（human assign）——E1 是外部触发，不应被本轮混沌打断。"""
        store = _AdapterProbeStore(target_dir)
        store.set_meta("status", "running")
        store.append_event(sender="human", type="assign", body="点赞功能开工", to=[])
        return store

    # ------------------------------------------------------------------
    # 不中断基准（同一场景、同一配置，只是主绑定不断粮、也不注入故障）
    # ------------------------------------------------------------------
    def _ensure_adapter_baseline(self) -> AdapterBaselineArtifacts:
        if self._adapter_baseline is not None:
            return self._adapter_baseline

        tdir = self.workspace / "_m5-baseline"
        ledger = self.workspace / "_m5-baseline-ledger.txt"
        state_path = self.workspace / "_m5-baseline-state.json"
        self._reset_ground(tdir, ledger, state_path)

        orch.store.clear_fault_injector()
        cfg = self._adapter_config(state_path)
        adapters = self._build_adapter_pair(ledger, unavailable_after=None)
        store = self._seed_thread(tdir)
        self._drive_until_stopped(store, cfg, adapters)

        status = store.get_meta("status")
        if status != "terminated":
            raise RuntimeError(f"M5 不中断基准未到达 terminated：status={status!r}")
        events = sorted(store.events(), key=lambda e: e["id"])
        strays = [
            ev for ev in events
            if (ev.get("meta") or {}).get("kind") in _M5_AUDIT_KINDS
        ]
        if strays:
            raise RuntimeError(
                f"M5 不中断基准不应产生任何降级/跳闸审计事件，实得 {len(strays)} 条"
            )
        rank = _rank_map(events)
        self._adapter_baseline = AdapterBaselineArtifacts(
            ledger_text=_normalized_ledger(ledger, rank),
            state_text=_normalized_state(tdir, rank),
        )
        return self._adapter_baseline

    # ------------------------------------------------------------------
    # 单轮驱动
    # ------------------------------------------------------------------
    def _run_one_adapter_round(
        self,
        *,
        index: int,
        site: str,
        rng: random.Random,
        baseline: AdapterBaselineArtifacts | None = None,
    ) -> tuple[bool, str]:
        if baseline is None:
            baseline = self._ensure_adapter_baseline()

        tdir = self.workspace / f"t-{index:03d}"
        ledger = self.workspace / f"ledger-{index:03d}.txt"
        state_path = self.workspace / f"adapter_state-{index:03d}.json"
        self._reset_ground(tdir, ledger, state_path)

        cfg = self._adapter_config(state_path)
        orch.store.clear_fault_injector()
        store = self._seed_thread(tdir)

        # 同一现场反复 kill 重启（裁决⑤）：每段各自装注入器，被 kill 就重开 Store + recover。
        segments = rng.randint(1, _MAX_KILL_SEGMENTS)
        kills: list[str] = []
        for _ in range(segments):
            actual_site = self._resolve_site(site, rng)
            count = rng.randint(1, _MAX_SITE_HITS)
            adapters = self._build_adapter_pair(
                ledger, unavailable_after=self.unavailable_after,
            )
            killed = False
            if actual_site is not None:
                orch.store.set_fault_injector(
                    orch.store.FaultInjector({actual_site: count})
                )
            try:
                self._drive_until_stopped(store, cfg, adapters)
            except SystemExit as exc:
                if exc.code != 137:      # 只认 137（模拟 kill -9），其余是真错。
                    raise
                killed = True
            except Exception as exc:  # noqa: BLE001 — 实现层 bug 计入该轮失败，不打断整跑。
                orch.store.clear_fault_injector()
                return False, f"impl-error-drive:{type(exc).__name__}:{exc!r}"
            finally:
                orch.store.clear_fault_injector()
            if not killed:
                break
            kills.append(f"{actual_site}#{count}")
            # 一次 kill = 换了进程：Store 与 adapter 实例全部重建，内存态不许过河；
            # adapter_state.json 与 ledger 留在盘上（真相层，重启装载延续）。
            store = _AdapterProbeStore(tdir)
            try:
                orch.scheduler.recover(store, cfg)
            except Exception as exc:  # noqa: BLE001
                return False, f"impl-error-recover:{type(exc).__name__}:{exc!r}"

        # 末段：不注入，跑到终止（"最后一次重启后不再被 kill"）。
        orch.store.clear_fault_injector()
        adapters = self._build_adapter_pair(
            ledger, unavailable_after=self.unavailable_after,
        )
        try:
            self._drive_until_stopped(store, cfg, adapters)
        except SystemExit as exc:
            if exc.code != 137:
                raise
            return False, "unexpected-systemexit-137-after-recover"
        except Exception as exc:  # noqa: BLE001
            return False, f"impl-error-final-drive:{type(exc).__name__}:{exc!r}"

        return self._verify_adapter_round(
            store=store, target_dir=tdir, ledger_path=ledger,
            state_path=state_path, baseline=baseline, kills=kills,
        )

    # ------------------------------------------------------------------
    # 单轮校验
    # ------------------------------------------------------------------
    def _verify_adapter_round(
        self, *, store, target_dir: Path, ledger_path: Path, state_path: Path,
        baseline: AdapterBaselineArtifacts, kills: list[str],
    ) -> tuple[bool, str]:
        # ① 终态必须 terminated。
        status = store.get_meta("status")
        if status != "terminated":
            return False, f"terminal-status-not-terminated:{status!r}(kills={kills})"

        # ② ledger 无重复（§9.4 exactly-once）。
        lines = self._read_ledger(ledger_path)
        if len(lines) != len(set(lines)):
            dups = sorted({x for x in lines if lines.count(x) > 1})
            return False, f"ledger-duplicate:{dups[:5]}(kills={kills})"

        events = sorted(store.events(), key=lambda e: e["id"])

        # ③ 剔除 M5 审计事件后的类型序列 == 附录B 期望（裁决③）。
        types = [
            ev["type"] for ev in events
            if (ev.get("meta") or {}).get("kind") not in _M5_AUDIT_KINDS
        ]
        if types != EXPECTED_TYPE_SEQUENCE:
            return False, f"types-mismatch:{types}(kills={kills})"

        # ④ 黑板终态：契约 v2 + 任务全 done。
        state = orch.store.board_state(store)
        contracts = state.get("contracts") or {}
        if (contracts.get("like-api") or {}).get("version") != 2:
            return False, f"board-contract-not-v2:{contracts}"
        tasks = state.get("tasks") or {}
        if not tasks or not all(v == "done" for v in tasks.values()):
            return False, f"board-tasks-not-all-done:{tasks}"

        # ⑤ 场景完整性：本轮真的走过"自动跳闸 + 备胎接手"，而不是悄悄退化成正常跑。
        try:
            snapshot = AdapterAvailability.load(state_path).snapshot()
        except Exception as exc:  # noqa: BLE001
            return False, f"scenario-state-file-unreadable:{exc!r}"
        for role in self.script:
            entry = snapshot.get(f"main_{role}") or {}
            if entry.get("status") != "disabled" or entry.get("by") != "auto":
                return False, (
                    f"scenario-primary-not-auto-tripped:{role}:{entry!r}(kills={kills})"
                )
        switched = {
            (ev.get("meta") or {}).get("role") for ev in events
            if (ev.get("meta") or {}).get("kind") == KIND_FALLBACK_SWITCH
        }
        if switched != set(self.script):
            return False, (
                f"scenario-fallback-switch-missing:{sorted(s for s in switched if s)}"
                f"(kills={kills})"
            )

        # ⑥ 与不中断基准逐字节一致（事件号按名次规范化，裁决③ / G 组口径）。
        rank = _rank_map(events)
        missing = sorted(
            {int(ln.rpartition(":")[2]) for ln in lines} - set(rank)
        )
        if missing:
            return False, f"ledger-references-audit-event:{missing}"
        actual_ledger = _normalized_ledger(ledger_path, rank)
        if actual_ledger != baseline.ledger_text:
            return False, (
                "baseline-ledger-mismatch:"
                f"{actual_ledger.splitlines()} != {baseline.ledger_text.splitlines()}"
            )
        try:
            actual_state = _normalized_state(target_dir, rank)
        except KeyError as exc:
            return False, f"baseline-board-event-id-unmapped:{exc!r}"
        if actual_state != baseline.state_text:
            return False, (
                f"baseline-board-mismatch:len={len(actual_state)}"
                f"!=base{len(baseline.state_text)}"
            )
        return True, "ok"


__all__ = [
    "AdapterBaselineArtifacts",
    "AdapterChaosHarness",
    "ADAPTER_INJECTION_SITES",
    "BaselineArtifacts",
    "ChaosHarness",
    "ChaosReport",
    "INJECTION_SITES",
]
