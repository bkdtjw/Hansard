"""§3.2 发送者约束 / 保留策略，§3.3 blackboard_ops 应用门槛。

纯函数、无副作用、不落盘。协议层只做规则判定；违规处理（降级为 report + 追加
system 审计事件、忽略 bb_ops + 追加 system 审计事件）由调度层据返回值执行（契约 §1）。

§16 自查：
  - 本层不校验系统字段（from/re/id/ts/meta 不属作者字段，§3.1）。
  - 路由只认 to；本层不解析 body @（反模式1）。allowed_sender 只判 (type, sender)。
"""

from __future__ import annotations

# —— §3.2 保留策略表：type -> "A"/"B"/"C"/"D" ——
# A 永久（投影黑板）；B 焦点/背景；C 摘要；D 丢弃（chat_ttl 后）。
# 逐行对齐 spec §3.2 表格。
TYPE_RETENTION: dict[str, str] = {
    "assign": "B",
    "review": "B",
    "question": "B",
    "answer": "B",
    "decision": "A",
    "handoff": "B",
    "report": "C",
    "defect": "B",  # 计入环路计数属调度层职责，保留策略仍为 B（§3.2）
    "acceptance": "A",
    "gate_request": "A",
    "gate_decision": "A",
    "system": "C",
    "terminate": "A",
    "chat": "D",
}

# §3.3：仅这三种 type 之决策类信封才可能应用 blackboard_ops。
_BB_OPS_TYPES: frozenset[str] = frozenset({"decision", "acceptance", "gate_decision"})

# §3.2 terminate 允许发送者。
_TERMINATE_SENDERS: frozenset[str] = frozenset({"moderator", "tester", "human"})


def allowed_sender(env_type: str, sender: str, *, can_decide: bool) -> bool:
    """§3.2 发送者约束。

    - decision：仅 can_decide 角色或 human；
    - gate_decision：仅 human；
    - system：仅编排器（sender == 'system'）；
    - terminate：仅 moderator / tester / human；
    - 其余 type（assign/review/question/answer/handoff/report/defect/
      acceptance/gate_request/chat）：任意发送者。

    can_decide 是 sender 角色的权限申报（§11.1），由调用方查配置后传入。
    """
    if env_type == "decision":
        return can_decide or sender == "human"
    if env_type == "gate_decision":
        return sender == "human"
    if env_type == "system":
        return sender == "system"
    if env_type == "terminate":
        return sender in _TERMINATE_SENDERS
    # 其余 type 无发送者约束（§3.2「任意」）。
    return True


def can_apply_blackboard_ops(env_type: str, *, sender_can_decide: bool) -> bool:
    """§3.3 门槛：type ∈ {decision, acceptance, gate_decision} 且 sender 具 can_decide。

    两条件皆真才放行 bb_ops 应用；否则调度层忽略 ops 并追加 system 审计事件（契约 §1）。
    本函数只判门槛，不判 op 结构合法性（结构由附录A schema 校验）。
    """
    return env_type in _BB_OPS_TYPES and sender_can_decide
