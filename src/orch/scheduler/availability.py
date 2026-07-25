"""§5.6 可用性与降级路由的**调度侧接线**（同步环 core.py 与异步环 async_core.py 共用）。

分工（不与 orch.adapters.state 重复）：
  · orch.adapters.state —— 事实底座：状态文件的读写、生效绑定解析（纯函数）、装载期校验；
  · 本模块 —— 调度侧决策与副作用：何时 reload、拿哪个实例、换绑冷启动、三种审计事件与
    两项指标、跳闸与 streak 记账。两条核心环**复用同一批函数**（契约 §3 "两条环对等"），
    不各写一份。

启用开关（Lead 裁决①）：状态文件路径取 ``config['adapter_state_path']``（绝对路径）。
**该键缺失 → 本模块全部逻辑退化为不启用**：``make_availability`` 返回 None，两条环走与
M0–M4 逐字相同的老路径（全 enabled、零新事件、零新指标）——这是既有测试零改动的前提。

三种审计事件（契约 §4，全部 sender='system'、type='system'、``make_dispatches=False``
"落盘但不生成派发行"，比照 terminate §5.4——它们是通告不是待办）：
  · meta.kind='fallback_switch' —— 生效绑定 ≠ 主绑定（meta: role/primary/effective/reason）
  · meta.kind='adapter_blocked' —— 该角色全链不可用（meta: role/primary）
  · meta.kind='adapter_trip'    —— 自动跳闸（meta: adapter/trigger/detail）

"首次才记"一律**现查日志**（§16.9 禁止内存驻留去重标志）：取本线程日志中最近一条
同 kind 同 role（跳闸按 adapter）的审计事件，比对其是否已表达同一状态。
"""

from __future__ import annotations

from typing import Any

from orch.adapters import AdapterUnavailableError
from orch.adapters.state import (
    DEFAULT_TRIP_AFTER,
    AdapterAvailability,
    resolve_effective_adapter,
)

__all__ = [
    "AdapterAvailability",
    "AdapterUnavailableError",
    "CONFIG_STATE_PATH_KEY",
    "KIND_ADAPTER_BLOCKED",
    "KIND_ADAPTER_RECOVERED",
    "KIND_ADAPTER_TRIP",
    "KIND_FALLBACK_SWITCH",
    "METRIC_FALLBACK_SWITCH",
    "METRIC_ADAPTER_TRIP",
    "TRANSPORT_FAILURE_ERRORS",
    "active_fallback_binding",
    "adapter_instance",
    "apply_rebinding",
    "make_availability",
    "note_blocked",
    "note_fallback_switch",
    "note_recovered",
    "on_invoke_success",
    "on_transport_failure",
    "on_unavailable",
    "on_watchdog_timeout",
    "prev_binding",
    "primary_adapter_name",
    "resolve_binding",
]

# 调度层读取状态文件路径的 config 键（Lead 裁决①；缺失 = 不启用）。
CONFIG_STATE_PATH_KEY = "adapter_state_path"

# 契约 §4 冻结的 meta.kind（R3/评审 major-4 追加第四种：回归主绑定的通告）。
KIND_FALLBACK_SWITCH = "fallback_switch"
KIND_ADAPTER_BLOCKED = "adapter_blocked"
KIND_ADAPTER_TRIP = "adapter_trip"
KIND_ADAPTER_RECOVERED = "adapter_recovered"

# 契约 §4 冻结的 metrics 键名（§13 两项：降级切换次数 / 自动跳闸次数）。
METRIC_FALLBACK_SWITCH = "fallback_switch"
METRIC_ADAPTER_TRIP = "adapter_trip"

# §5.6.3 第 2 条"传输级失败"在调度侧可见的异常型别：
#   超时 → TimeoutError（适配层统一转换）；进程失败 / 无法解析出信封（无 json 块或
#   JSON 解码失败）→ ValueError；子进程无法启动等 → OSError。
# 其余异常（如 mock 脚本缺键的 KeyError）**不是**传输级失败，一律原样上抛，既有语义不变。
TRANSPORT_FAILURE_ERRORS = (TimeoutError, ValueError, OSError)

# 审计事件 detail / reason 摘要长度上限（只折空白与截断，不解读语义）。
_SUMMARY_LIMIT = 200


def _summarize(text: object, limit: int = _SUMMARY_LIMIT) -> str:
    """多行报错压成一行摘要（供审计事件 body/meta 展示）。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ——————————————————————————————————————————————————————————————
# 装载 / 解析
# ——————————————————————————————————————————————————————————————

def make_availability(config: dict) -> AdapterAvailability | None:
    """按 config['adapter_state_path'] 造可用性视图；键缺失 → None（本卡逻辑整体不启用）。

    这里**只造不读**：首次读取由调用方在每轮调度前的 ``reload()`` 完成（§5.6.1
    "调度器每轮调度前重读该文件；禁止只在启动时读一次"）。文件损坏时 reload 抛
    AdapterStateError（§5.6.1 启动报错，禁止猜测），由调用方向上传递。
    """
    path = (config or {}).get(CONFIG_STATE_PATH_KEY)
    if not path:
        return None
    return AdapterAvailability(path)


def primary_adapter_name(config: dict, role: str) -> str:
    """该角色的**主绑定**名（roles[role].adapter）；缺省用角色名兜底。

    与 ``core._adapter_name`` / ``state.resolve_effective_adapter`` 同一约定（三处一致，
    否则"effective ≠ 主绑定"的判定会漂移）。本模块自持一份以避免与 core 互相 import。
    """
    role_conf = ((config or {}).get("roles") or {}).get(role) or {}
    return str(role_conf.get("adapter") or role)


def resolve_binding(
    config: dict, role: str, availability: AdapterAvailability
) -> str | None:
    """§5.6.2 生效绑定解析（现算不落盘）：[主绑定] + fallback 中首个 enabled；全不可用 → None。"""
    return resolve_effective_adapter(role, (config or {}).get("roles") or {}, availability)


def adapter_instance(adapters: dict, role: str, effective: str, primary: str) -> Any:
    """取本次 invoke 的实例（评审 major-2 冻结的三级兜底链）：

      1) ``f"{role}::{effective}"`` —— **复合键**，R4 装配为"每角色 × 主绑定+各 fallback"
         各建一个实例（绑该角色自己的 worktree/tools），同名 adapter 被多角色引用时
         必须各归各的，故优先；
      2) ``effective`` —— 适配器名键（M5 既有分支 / 手工装配的简单映射）；
      3) ``role`` —— **仅当 effective == 主绑定**时回落（M0–M4 角色名键映射兼容）。

    生效绑定 ≠ 主绑定却前两键皆缺 → KeyError（响亮失败）：备胎没有实例时**禁止**
    悄悄拿角色键（= 主绑定）实例顶替，那会让"备胎接手"的审计与指标全部失真。
    """
    composite = f"{role}::{effective}"
    if composite in adapters:
        return adapters[composite]
    if effective in adapters:
        return adapters[effective]
    if effective == primary and role in adapters:
        return adapters[role]
    raise KeyError(
        f"生效绑定 {effective!r}（角色 {role!r}）没有对应的 adapter 实例："
        f"复合键 {composite!r} 与名字键均不在场；可用实例键：{sorted(adapters)}"
    )


def trip_after_for(config: dict, adapter_name: str) -> int:
    """该 adapter 的连续失败阈值：config.adapters[name].trip_after，缺省 DEFAULT_TRIP_AFTER。"""
    conf = ((config or {}).get("adapters") or {}).get(adapter_name) or {}
    raw = conf.get("trip_after") if isinstance(conf, dict) else None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TRIP_AFTER
    return value if value >= 1 else DEFAULT_TRIP_AFTER


# ——————————————————————————————————————————————————————————————
# 审计事件（三种）+ 指标（两项）
# ——————————————————————————————————————————————————————————————

def _latest_audit(store, kind: str, field: str, value: str) -> tuple[int, dict] | None:
    """本线程日志中**最近一条**同 kind 且 meta[field]==value 的审计事件 (id, meta)。

    §16.9：去重判据只许现查日志，禁止任何内存驻留标志。无匹配 → None。
    """
    match: tuple[int, dict] | None = None
    for ev in store.events():
        meta = ev.get("meta") or {}
        if meta.get("kind") == kind and meta.get(field) == value:
            match = (int(ev["id"]), meta)
    return match


def active_fallback_binding(store, role: str) -> str | None:
    """盘上现查：该角色**当前是否处于降级中**，是则返回其生效的降级绑定名（否则 None）。

    判据（评审 major-4 冻结，全部只查日志、零内存态 §16.9）：按事件号升序扫该角色的
    切换/回归通告——``fallback_switch`` 置位为其 meta.effective，``adapter_recovered``
    清位。故"最近一条 fallback_switch 之后没有 recovered"才算仍在降级中。

    这个推导同时服务三处：切换通告的"连续"判据、回归通告的触发条件、换绑
    attempts 归零的 prev-binding（不再依赖 sessions 行是否存在，评审 major-6）。
    """
    active: str | None = None
    for ev in store.events():
        meta = ev.get("meta") or {}
        if meta.get("role") != role:
            continue
        kind = meta.get("kind")
        if kind == KIND_FALLBACK_SWITCH:
            active = str(meta.get("effective") or "") or None
        elif kind == KIND_ADAPTER_RECOVERED:
            active = None
    return active


def prev_binding(store, role: str, primary: str) -> str:
    """换绑判据用的"上一次生效绑定"：降级中 → 该降级绑定；否则主绑定（评审 major-6）。

    spec §5.6.2"换绑重派时该派发行 attempts 归零"原文并无"该角色须有 sessions 行"的
    前提；mock/API 型角色本就无会话行，旧实现把归零挂在会话行上会让它们降级后仍背着
    旧后端消耗掉的重试预算。改从审计链推导后，两类角色一视同仁。
    """
    return active_fallback_binding(store, role) or primary


def _role_replied_after(store, role: str, event_id: int) -> bool:
    """该角色在 event_id 之后是否产出过回复事件（sender==role）——阻塞态断链的盘上锚。"""
    for ev in store.events():
        if int(ev["id"]) > event_id and ev.get("from") == role:
            return True
    return False


def _append_audit(store, *, body: str, meta: dict) -> int:
    """追加一条 M5 审计事件：sender='system'、type='system'、**不生成派发行**。

    §5.6.2 明文"比照 terminate（§5.4）：落盘但不生成派发行"——通告不是待办；
    §16.11 系统字段（from/id/ts）由编排器权威赋值。
    """
    return store.append_event(
        sender="system", type="system", body=body, to=[], meta=dict(meta),
        make_dispatches=False,
    )


def note_fallback_switch(
    store, availability: AdapterAvailability, role: str, primary: str, effective: str
) -> None:
    """§5.6.2 + §13：生效绑定 ≠ 主绑定时的两件事——**采样口径刻意不同**（Lead R1 裁决）：

      · §13 指标 key='fallback_switch'：**每一次**以降级绑定执行的派发各记一条
        （spec §13 原句"每次 effective ≠ 主绑定的派发记一条"，计数按 (role, adapter)
        分组可复算）——故落在去重判定**之外**，本函数每次被调用都记；
      · §5.6.2 审计事件 meta.kind='fallback_switch'：**首次才记**（"同一（role，生效
        绑定）连续派发只在首次记录"），"首次"= 现查日志中最近一条同 kind 同 role 的
        事件尚未表达同一 effective（§16.9 禁止内存驻留去重标志）。

    调用点保证本函数**仅**在 effective ≠ 主绑定的派发前被调用一次（两条环各一处），
    因此"每次调用记一条"与 §13 的"每次降级派发记一条"逐字对应。

    "连续"的判据（评审 major-4 冻结）：以 ``active_fallback_binding`` 现查——最近一条同
    role 的 fallback_switch 之后**若已有 adapter_recovered**（角色曾回归主绑定），链即
    断开，再次降级属新的"首次"，必须重新通告；否则才算连续同状态、只补指标不重复通告。
    """
    reason = _summarize(
        (availability.snapshot().get(primary) or {}).get("reason")
        or f"主绑定 {primary} 当前不可用"
    )
    # §13 降级切换次数：先记指标——它与审计事件的去重**解耦**（每次降级派发都是一次切换）。
    store.record_metric(
        METRIC_FALLBACK_SWITCH, 1.0,
        extra=f"role={role}:from={primary}:to={effective}",
    )
    if active_fallback_binding(store, role) == effective:
        return  # 同状态连续派发（链未被 recovered 打断）：只补指标，不重复通告（§5.6.2）。
    _append_audit(
        store,
        body=(
            f"降级路由生效：角色 {role} 的主绑定 {primary} 不可用，"
            f"本次派发改由 {effective} 承接（原因：{reason}；§5.6.2）。"
        ),
        meta={
            "kind": KIND_FALLBACK_SWITCH,
            "role": role,
            "primary": primary,
            "effective": effective,
            "reason": reason,
        },
    )


def note_recovered(store, role: str, primary: str) -> None:
    """评审 major-4：角色**回归主绑定**时追加一条 adapter_recovered 通告（不生成派发行）。

    仅当盘上审计链显示该角色仍"降级中"才记——它既是运维可读的回归锚点，也是
    ``fallback_switch`` 去重链的**断链标记**。连续回归天然只记一次：本事件自身即把
    ``active_fallback_binding`` 清位，下一次仍在主绑定时查得 None，不再重复（零内存态）。
    """
    previous = active_fallback_binding(store, role)
    if previous is None:
        return  # 本就没在降级中（或已记过回归）——无事可通告。
    _append_audit(
        store,
        body=(
            f"降级路由解除：角色 {role} 的主绑定 {primary} 已恢复可用，"
            f"本次派发回归主绑定（此前生效绑定：{previous}）。"
        ),
        meta={
            "kind": KIND_ADAPTER_RECOVERED,
            "role": role,
            "primary": primary,
            "previous": previous,
        },
    )


def note_blocked(store, role: str, primary: str) -> None:
    """§5.6.2 全部不可用：首次进入阻塞态追加一条通告事件（不生成派发行、无指标）。

    "首次"= **连续状态的首次**（评审 major-5）：最近一条同 role 的 adapter_blocked
    之后，若该角色已产出过回复事件（sender==role，盘上现查），说明它一度脱离阻塞态，
    再次进入即新的首次，必须重新通告——否则第二次阻塞在盘上无锚。
    """
    last = _latest_audit(store, KIND_ADAPTER_BLOCKED, "role", role)
    if (last is not None and last[1].get("primary") == primary
            and not _role_replied_after(store, role, last[0])):
        return
    _append_audit(
        store,
        body=(
            f"角色 {role} 当前无可用适配器（主绑定 {primary} 与其全部备胎均已停用）："
            f"相关待办保持 pending 等待人工 `orch adapter enable`，其余角色照常调度（§5.6.2）。"
        ),
        meta={"kind": KIND_ADAPTER_BLOCKED, "role": role, "primary": primary},
    )


def note_trip(store, adapter_name: str, trigger: str, detail: str) -> None:
    """§5.6.3 跳闸审计事件（不生成派发行）+ §13 自动跳闸次数埋点（按触发条件分类）。

    跳闸是一次**状态变更**（离散事件），每次发生都记——与切换/阻塞的"首次才记"不同。
    """
    _append_audit(
        store,
        body=(
            f"适配器 {adapter_name} 自动跳闸（触发条件：{trigger}）：{detail}。"
            f"恢复仅限人工 `orch adapter enable`（§5.6.3）。"
        ),
        meta={
            "kind": KIND_ADAPTER_TRIP,
            "adapter": adapter_name,
            "trigger": trigger,
            "detail": detail,
        },
    )
    store.record_metric(
        METRIC_ADAPTER_TRIP, 1.0,
        extra=f"adapter={adapter_name}:trigger={trigger}",
    )


# ——————————————————————————————————————————————————————————————
# 换绑（会话死亡）与失败记账
# ——————————————————————————————————————————————————————————————

def apply_rebinding(
    store, session_row: dict | None, role: str, effective: str, event_ids: list[int],
    prev: str,
) -> bool:
    """§5.6.2 换绑的两件事——**判据各自独立**（评审 major-6 拆解）：

      · attempts 归零：只看"生效绑定是否变了"（effective ≠ prev，prev 由审计链盘上推导），
        与该角色有没有 sessions 行**无关**——spec 原文没有这个前提，mock/API 型角色也
        必须享有新后端的完整重试预算；
      · 会话作废（sid 置空 / gen+1 / backend 更新）：只在**确有** sessions 行且其 backend
        与生效绑定不同时执行——无会话行就没有会话可作废，禁止凭空造行（§4.2 会话表是
        工作状态不是事件真相）。

    返回是否发生了"绑定变更"（供调用方日志/观察，不影响落盘）。
    """
    backend = (session_row or {}).get("backend")
    session_stale = bool(session_row) and bool(backend) and backend != effective
    rebound = (effective != prev) or session_stale
    if session_stale:
        store.upsert_session(
            role=role, sid=None, gen=int((session_row or {}).get("gen") or 0) + 1,
            backend=effective,
        )
    if rebound:
        for eid in event_ids:
            store.reset_attempts(eid, role)
    return rebound


def on_unavailable(
    store, availability: AdapterAvailability, effective: str, exc: Exception
) -> str:
    """§5.6.3 第 1 条（特征命中）：立即跳闸 + 审计 + 指标。返回报错摘要。

    跳闸记在**调度层自己解析出的生效绑定名**上——``exc.adapter_name`` 只是适配层的
    审计线索（契约 §2 明示），不足以作为记账依据（同一实例可能被多个配置名引用）。
    该次失败不计 attempts（调用方负责把派发行放回 pending），本轮跳过，下轮重解析
    通常由 fallback 接手（§5.6.3）。
    """
    detail = _summarize(getattr(exc, "detail", "") or str(exc))
    availability.disable(effective, reason=detail, by="auto")
    note_trip(store, effective, "pattern", detail)
    return detail


def on_streak_failure(
    store, config: dict, availability: AdapterAvailability, effective: str, reason: str,
) -> bool:
    """§5.6.3 第 2 条的唯一记账入口：fail_streak += 1，达阈值则跳闸 + 审计 + 指标。

    调用者有二（两条路径同源，评审 minor-4）：invoke 抛出的传输级失败
    （``on_transport_failure``）与看门狗单次调用超时（``on_watchdog_timeout``）——
    §5.6.4 明文"超时既走看门狗路径也计入 fail_streak"。返回"本次是否跳闸"。
    """
    tripped = availability.record_failure(
        effective, trip_after=trip_after_for(config, effective), reason=reason,
    )
    if tripped:
        note_trip(store, effective, "streak", reason)
    return tripped


def on_transport_failure(
    store, config: dict, availability: AdapterAvailability, effective: str,
    exc: Exception,
) -> bool:
    """§5.6.3 第 2 条（连续失败）：fail_streak += 1，达阈值则跳闸 + 审计 + 指标。

    **叠加**在既有 attempts / 重试语义之上（§5.1 那条路径逐字不变，调用方负责）；
    schema 校验失败**不得**走这里（§5.6.3：输出质量问题不是可用性问题）。
    返回"本次是否跳闸"。
    """
    return on_streak_failure(
        store, config, availability, effective,
        _summarize(f"{type(exc).__name__}: {exc}"),
    )


def on_watchdog_timeout(
    store, config: dict, availability: AdapterAvailability, effective: str,
    *, detail: str,
) -> bool:
    """§5.6.4：看门狗单次调用超时同样计入 fail_streak（评审 minor-4）。

    生效绑定名由调用方**现场 resolve**（不读 sessions：那是会话工作状态，可能滞后于
    本次派发实际用的绑定）。与 invoke 路径共用 ``on_streak_failure``，跳闸语义一字不差。
    """
    return on_streak_failure(store, config, availability, effective, _summarize(detail))


def on_invoke_success(availability: AdapterAvailability, effective: str) -> None:
    """§5.6.3：成功 invoke → fail_streak 归零（streak 原为 0 时 state 模块不写盘）。"""
    availability.record_success(effective)
