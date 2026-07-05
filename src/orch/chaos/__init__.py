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
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

import orch.adapters
import orch.scheduler
import orch.store

from orch.chaos.expected import EXPECTED_TYPE_SEQUENCE


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
    """

    rounds: int
    passed: int
    failed_seeds: list[dict] = field(default_factory=list)
    ledger_ok: bool = True
    terminal_ok: bool = True


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
        report = ChaosReport(rounds=rounds, passed=0)

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


__all__ = ["ChaosHarness", "ChaosReport", "BaselineArtifacts", "INJECTION_SITES"]
