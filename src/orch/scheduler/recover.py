"""§9.1 崩溃恢复算法（启动时对线程机械执行）。

铁律（spec §9.1、§16.10）：恢复**禁止任何猜测**，每一步只允许查表与数日志。
本模块不做任何基于内存/推测的分支，全部依据落盘的派发行状态、事件日志与线程元数据。

算法（spec §9.1 / 契约 §4）：
  1. 黑板文件缺失或损坏 → rebuild_blackboard（清空后重放全部 A 类事件的 bb_ops）。
  2. thread status == suspended → 保持挂起：gate_wait 行不动、同期 dispatching 行也不对账，
     直接返回（§9.1：挂起可整体停机，只等 gate_decision）。
  3. 对每个 status=='dispatching' 的 (E_n, T)，按**固定优先级**落入唯一分支：
       a) 存在 sender==T 且 n ∈ re 的回复 → mark_done（纵深防御，合并事务后理论不出现）；
       b) now > deadline_ts               → 看门狗路径：bump_attempt（计一次 attempt）；
       c) 其余                            → set_pending（重派发，主循环接手）。
     优先级 a) > b) > c)：超时且无回复必须走 b)（否则漏计 attempt，见测试
     test_recover_case_b_precedence_when_deadline_passed_and_no_reply）。
  4. pending 行不处理（主循环自然接手）；环路/轮数计数器由日志现数（§5.3），恢复不落盘。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import orch.store

from orch.scheduler._dispatch import dispatching_rows, has_matching_reply


def _blackboard_intact(store) -> bool:
    """黑板 state.json 是否存在且为合法 JSON（§9.1：缺失或损坏即需 rebuild）。

    只读盘、不改盘；store._read_state 会把损坏静默当空，故此处独立判定原始文件真相，
    避免"损坏被静默吞掉后不 rebuild"。
    """
    state_path = Path(store.thread_dir) / "blackboard" / "state.json"
    if not state_path.exists():
        return False
    try:
        json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False
    return True


def recover(store: "orch.store.Store", config: dict) -> None:
    """§9.1 恢复算法。对单线程机械执行，禁止猜测，只查表与数日志。"""
    # —— 1) 黑板缺失/损坏 → rebuild（§4.6/§9.1）——
    if not _blackboard_intact(store):
        orch.store.rebuild_blackboard(store)

    # —— 2) suspended → 保持挂起直接返回（§9.1）——
    # gate_wait 行不动；同期 dispatching 行不对账（挂起可整体停机）。
    if store.get_meta("status") == "suspended":
        return

    # —— 3) 逐个 dispatching 行对账（a > b > c 固定优先级）——
    events = store.events()
    now = time.time()
    for row in dispatching_rows(store):
        n = int(row["event_id"])
        t = row["target"]

        # a) 有匹配回复（sender=T 且 n∈re）→ 补标 done（纵深防御，§9.1 a）。
        if has_matching_reply(events, n, t):
            store.mark_done(n, t)
            continue

        # b) 超时（now > deadline_ts）→ 看门狗路径，计一次 attempt（§5.3/§9.1 b）。
        deadline = row["deadline_ts"]
        if deadline is not None and now > float(deadline):
            store.bump_attempt(n, t)
            continue

        # c) 其余 → 重派发（§9.1 c）。
        store.set_pending(n, t)

    # —— 4) pending 行不处理；计数器由日志现数（§5.3，恢复不落盘）——
