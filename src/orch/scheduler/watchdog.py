"""§5.3 看门狗三级（核心环每轮主动调用）。

spec §5.3 表格原文（宪法定义，逐字抄录，不做任何简化）：

| 级别     | 判定                                        | 动作                                        |
|----------|---------------------------------------------|---------------------------------------------|
| 单次调用 | now > deadline_ts                           | kill 子进程；attempts+1；重试 1 次；再败 →   |
|          |                                              | failed + 转 moderator                        |
| 互@环路  | 同一有序对 (A→B) 的 defect 数 ≥ loop_limit  | 自动 gate_request 升级人类                   |
| 全局轮数 | 线程事件总数 ≥ max_rounds                    | 自动 gate_request                            |

**M1 本层实现范围的诚实说明（不得掩盖的缺口）**：
  1. 单次调用（级别1）本层【仅】实现 attempts+1（bump_attempt 一次），不实现
     "kill 子进程 / 重试 1 次 / 再败→failed+转 moderator" 的完整动作序列。
  2. 完整动作序列需要一个真实子进程句柄去 kill、以及重试执行的真实后端；M1
     worker 派发目前是 mock（无真实子进程对象可 kill），故该完整对账被**推迟
     到 M2 真实后端实现**，此处只是提前占位计一次 attempt，不是"已对账"。
  3. 此外，在当前核心环活循环里，mock 派发同步返回结果、不会残留
     status='dispatching' 的行，因此 check_watchdogs 每轮主动触发时，级别1的
     for 循环通常枚举不到任何行——即级别1在 M1 的“核心环主动触发”路径下
     实际是 no-op；唯一能观察到 bump_attempt 生效的路径是测试里注入假时钟 /
     手工构造 dispatching 行（例如 §9.1 崩溃恢复相关测试路径），而非真实活
     循环中的主动触发。
  以上三点均为 M1 阶段性事实，不代表功能"已完整"或"已对账"，仅为诚实记录，
  避免"做一半当做完"。

铁律（spec §5.3 / §16.2 / §16.11，本层遵守，与上面的范围声明不冲突）：
  - 定时依据一律**落盘绝对时间戳** dispatches.deadline_ts；判定只用注入/取样的 now
    与该时间戳比较，**禁止**内存倒计时 / sleep。
  - 环路与轮数每次**从日志现数、不落盘**（§9.1 可推导，无任何计数器持久化字段）。
  - 看门狗触发的 gate_request（to=[human]）复用 M0 门禁机制：本层追加 gate_request 事件
    （sender='system'，系统字段编排器权威赋值 §16.11）、把其 human 派发行标 gate_wait、
    线程 status→suspended（§10：整体停机，挂起不消耗资源）。
  - 所有写一律走冻结 store 原语（bump_attempt / append_event / mark_gate_wait /
    set_meta）；只读旁路（枚举 dispatching 行）走 _dispatch（读盘观察落盘真相，契约 §7）。
"""

from __future__ import annotations

import time

from orch.scheduler._dispatch import dispatching_rows

# §5.3 默认阈值（config.thread_defaults 未给时兜底；与 spec 括注一致）。
_DEFAULT_LOOP_LIMIT = 3
_DEFAULT_MAX_ROUNDS = 100


def _thread_defaults(config: dict) -> dict:
    return (config.get("thread_defaults") or {}) if config else {}


def _loop_limit(config: dict) -> int:
    v = _thread_defaults(config).get("loop_limit", _DEFAULT_LOOP_LIMIT)
    try:
        return int(v)
    except (TypeError, ValueError):
        return _DEFAULT_LOOP_LIMIT


def _max_rounds(config: dict) -> int:
    v = _thread_defaults(config).get("max_rounds", _DEFAULT_MAX_ROUNDS)
    try:
        return int(v)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ROUNDS


def _defect_pair_counts(events: list[dict]) -> dict[tuple[str, str], int]:
    """从日志现数：同一有序对 (from→to) 的 **defect** 事件数（§5.3 互@环路）。

    仅 type=='defect' 计入（§3.2：defect「计入环路计数」）；一条 defect 若 to 有多个目标，
    每个 (from, to_i) 有序对各计一次。不落盘、每次重数（§5.3）。
    """
    counts: dict[tuple[str, str], int] = {}
    for ev in events:
        if ev.get("type") != "defect":
            continue
        sender = ev.get("from")
        for tgt in ev.get("to") or []:
            key = (sender, tgt)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _raise_gate(store, *, body: str) -> int:
    """追加一条看门狗 gate_request(to=[human]) 并复用 M0 门禁机制挂起线程（§5.3/§10）。

    步骤（系统字段编排器权威赋值，§16.11）：
      ① append_event(sender='system', type='gate_request', to=['human'],
         corr='gate-{事件号}')——落盘时为 human 生成一行 pending 派发；
      ② 把该 human 派发行标 gate_wait（§10：target=human 的 pending → gate_wait）；
      ③ thread status → 'suspended'（挂起可整体停机，不消耗资源）。
    corr 缺省由编排器生成 `gate-{事件号}`（§10）；先落盘取得事件号再回填 corr。
    返回 gate_request 事件 id。
    """
    gate_id = store.append_event(
        sender="system", type="gate_request", body=body, to=["human"],
    )
    # §10：corr 缺省 → 编排器生成 gate-{事件号}。落盘后回填（不影响派发行）。
    store.set_meta(f"gate_corr:{gate_id}", f"gate-{gate_id}")
    # ② human 派发行标 gate_wait；③ 线程挂起（§10）。
    store.mark_gate_wait(gate_id, "human")
    store.set_meta("status", "suspended")
    return gate_id


def check_watchdogs(store, config: dict, *, now: float | None = None) -> list[dict]:
    """核心环每轮调用。now 可注入假时钟（测试用，默认 time.time()）。返回触发动作列表。

    §5.3 三级判定与动作（本函数的**实现范围**，非 spec 表格全文——完整表格见本模块
    顶部 docstring）：
      级别1 单次调用超时：对每个 status='dispatching' 的 (E_n,T)，若 now > deadline_ts →
        仅 bump_attempt（计一次 attempt）。spec §5.3 表格要求的"kill 子进程 / 重试 1 次 /
        再败 → failed + 转 moderator"完整动作序列【本层未实现】，留待 M2 真实后端（M1
        worker 派发为 mock，无真实子进程可 kill）。此外 mock 派发同步返回、活循环中不会
        残留 dispatching 行，故此级别在真实活循环主动触发路径下通常是 no-op；能触发
        bump_attempt 的只有测试注入假时钟 / 手工构造 dispatching 行的场景。时间只来自
        now 参数（§16.2）。
      级别2 互@环路：同一有序对 (A→B) 的 defect 数 ≥ loop_limit → gate_request(to=[human])
        + 线程 suspended。（完整实现，非部分。）
      级别3 全局轮数：线程事件总数 ≥ max_rounds → gate_request(to=[human]) + suspended。
        （完整实现，非部分。）

    返回的 list[dict] 每项形如 {'level':1|2|3, ...}，供调用方/测试观察；测试不绑定其内部
    结构，只观察落盘真相（attempts / gate_request 事件 / status）。级别2/3 一旦触发即挂起，
    本轮不再继续（挂起后线程停机，§10）。
    """
    ts_now = time.time() if now is None else float(now)
    actions: list[dict] = []

    # —— 级别1：单次调用超时 → 仅 bump_attempt 计一次 attempt（落盘绝对时间戳判定，§16.2）。
    # 【范围声明】spec §5.3 要求的 kill 子进程/重试1次/再败转 moderator 完整动作序列
    # 本层未实现，留待 M2 真实后端；M1 worker 派发为 mock 且同步返回，活循环中通常
    # 枚举不到 dispatching 行，此级别在真实活循环里实际是 no-op（详见模块与函数
    # docstring 的完整说明）——本行只做计数，不代表已完成对账。 ——
    for row in dispatching_rows(store):
        deadline = row.get("deadline_ts")
        if deadline is None:
            continue
        if ts_now > float(deadline):
            eid = int(row["event_id"])
            tgt = row["target"]
            new_attempts = store.bump_attempt(eid, tgt)
            actions.append({
                "level": 1, "event_id": eid, "target": tgt,
                "attempts": new_attempts,
            })

    events = store.events()

    # —— 级别2：互@环路（同一有序对 defect 数 ≥ loop_limit）→ gate_request + suspend ——
    loop_limit = _loop_limit(config)
    for (sender, tgt), cnt in _defect_pair_counts(events).items():
        if cnt >= loop_limit:
            gate_id = _raise_gate(
                store,
                body=(f"看门狗·互@环路：有序对 ({sender}→{tgt}) 的 defect 数达 {cnt}"
                      f"（≥ loop_limit={loop_limit}），自动升级人类门禁（§5.3）"),
            )
            actions.append({
                "level": 2, "pair": [sender, tgt], "count": cnt,
                "loop_limit": loop_limit, "gate_id": gate_id,
            })
            return actions  # 挂起后停机，本轮不再检测其它级别（§10）。

    # —— 级别3：全局轮数（事件总数 ≥ max_rounds）→ gate_request + suspend ——
    max_rounds = _max_rounds(config)
    total = len(events)
    if total >= max_rounds:
        gate_id = _raise_gate(
            store,
            body=(f"看门狗·全局轮数：线程事件总数达 {total}（≥ max_rounds={max_rounds}），"
                  f"自动升级人类门禁（§5.3）"),
        )
        actions.append({
            "level": 3, "total": total, "max_rounds": max_rounds,
            "gate_id": gate_id,
        })
        return actions

    return actions
