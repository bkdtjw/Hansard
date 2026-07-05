"""调度层：核心循环、触发源与崩溃恢复（spec §5、§9）。

对外冻结符号（docs/m0-contract.md §4 + §8；M1 契约 §2 追加 check_watchdogs）：
  run_thread(store, config, adapters) -> None            §5.1 核心循环（单线程串行版）
  recover(store, config) -> None                         §9.1 崩溃恢复算法
  apply_gate_decision(store, config, adapters, *, corr, approve, sender='human') -> None
                                                         §10/§5.5 门禁裁决 + 系统执行器入口
  check_watchdogs(store, config, *, now=None) -> list[dict]   §5.3 看门狗三级（核心环每轮）

分层铁律（spec §2）：视图组装（四层/第三人称/重锚定，M1）属本层职责，与厂商无关；
本层只决定"调用谁、给什么"，不含任何格式转换（那是适配层 §7）。
"""

from __future__ import annotations

from orch.scheduler.core import run_thread
from orch.scheduler.recover import recover
from orch.scheduler.systemexec import apply_gate_decision
from orch.scheduler.watchdog import check_watchdogs

__all__ = [
    "run_thread",
    "recover",
    "apply_gate_decision",
    "check_watchdogs",
]
