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
    "KIND_ADAPTER_TRIP",
    "KIND_FALLBACK_SWITCH",
    "METRIC_FALLBACK_SWITCH",
    "METRIC_ADAPTER_TRIP",
    "TRANSPORT_FAILURE_ERRORS",
    "adapter_instance",
    "make_availability",
    "note_blocked",
    "note_fallback_switch",
    "on_invoke_success",
    "on_transport_failure",
    "on_unavailable",
    "primary_adapter_name",
    "rebind_session_if_needed",
    "resolve_binding",
]

# 调度层读取状态文件路径的 config 键（Lead 裁决①；缺失 = 不启用）。
CONFIG_STATE_PATH_KEY = "adapter_state_path"

# 契约 §4 冻结的 meta.kind。
KIND_FALLBACK_SWITCH = "fallback_switch"
KIND_ADAPTER_BLOCKED = "adapter_blocked"
KIND_ADAPTER_TRIP = "adapter_trip"

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
    """按"adapter 名 → 实例"取 invoke 实例（契约 §3）；保留角色名键的既有映射兼容。

    取用顺序：
      1) adapters[effective] —— M5 起的标准键（适配器名作键）；
      2) 生效绑定 == 主绑定时回落 adapters[role] —— 既有 302 用例的角色名键映射
         （role 无 adapter 声明时 effective 本就等于角色名，天然同键）。
    生效绑定 ≠ 主绑定却找不到该名字的实例 → KeyError：备胎没有实例可用时**禁止**
    悄悄拿主绑定实例顶替（那会让"备胎接手"的审计与指标全部失真）。
    """
    if effective in adapters:
        return adapters[effective]
    if effective == primary and role in adapters:
        return adapters[role]
    raise KeyError(
        f"生效绑定 {effective!r}（角色 {role!r}）没有对应的 adapter 实例；"
        f"可用实例键：{sorted(adapters)}"
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

def _latest_audit_meta(store, kind: str, field: str, value: str) -> dict | None:
    """本线程日志中**最近一条**同 kind 且 meta[field]==value 的审计事件的 meta。

    §16.9：去重判据只许现查日志，禁止任何内存驻留标志。无匹配 → None。
    """
    match = None
    for ev in store.events():
        meta = ev.get("meta") or {}
        if meta.get("kind") == kind and meta.get(field) == value:
            match = meta
    return match


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
    last = _latest_audit_meta(store, KIND_FALLBACK_SWITCH, "role", role)
    if last is not None and last.get("effective") == effective:
        return  # 同状态连续派发：只补指标，不再重复通告（§5.6.2）。
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


def note_blocked(store, role: str, primary: str) -> None:
    """§5.6.2 全部不可用：首次进入阻塞态追加一条通告事件（不生成派发行、无指标）。"""
    last = _latest_audit_meta(store, KIND_ADAPTER_BLOCKED, "role", role)
    if last is not None and last.get("primary") == primary:
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

def rebind_session_if_needed(
    store, session_row: dict | None, role: str, effective: str, event_ids: list[int]
) -> bool:
    """§5.6.2：effective ≠ sessions.backend → 视为会话死亡（sid 置空、gen+1、backend 更新）
    并把本组各派发行 attempts 归零（新后端享有完整重试预算）。返回是否发生换绑。

    无 sessions 行 = 该角色本就没有活会话（mock/API 型常态）→ 无会话可作废，不写盘：
    随后的组装自然走冷启动全量（§6.1–6.4），与 spec 语义一致。
    """
    if not session_row:
        return False
    backend = session_row.get("backend")
    if not backend or backend == effective:
        return False
    store.upsert_session(
        role=role, sid=None, gen=int(session_row.get("gen") or 0) + 1,
        backend=effective,
    )
    for eid in event_ids:
        store.reset_attempts(eid, role)
    return True


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


def on_transport_failure(
    store, config: dict, availability: AdapterAvailability, effective: str,
    exc: Exception,
) -> bool:
    """§5.6.3 第 2 条（连续失败）：fail_streak += 1，达阈值则跳闸 + 审计 + 指标。

    **叠加**在既有 attempts / 重试语义之上（§5.1 那条路径逐字不变，调用方负责）；
    schema 校验失败**不得**走这里（§5.6.3：输出质量问题不是可用性问题）。
    返回"本次是否跳闸"。
    """
    reason = _summarize(f"{type(exc).__name__}: {exc}")
    tripped = availability.record_failure(
        effective, trip_after=trip_after_for(config, effective), reason=reason,
    )
    if tripped:
        note_trip(store, effective, "streak", reason)
    return tripped


def on_invoke_success(availability: AdapterAvailability, effective: str) -> None:
    """§5.6.3：成功 invoke → fail_streak 归零（streak 原为 0 时 state 模块不写盘）。"""
    availability.record_success(effective)
