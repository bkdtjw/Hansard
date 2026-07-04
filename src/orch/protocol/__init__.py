"""协议层：信封结构与 schema 校验（spec §3、附录A）。

对外冻结符号（docs/m0-contract.md §1）：
  AUTHOR_SCHEMA                              附录A 原样 JSON Schema（draft-07）
  validate_author_fields(obj) -> list[str]   作者字段校验，空列表 = 合法
  TYPE_RETENTION                             type -> 保留策略 "A"/"B"/"C"/"D"（§3.2）
  allowed_sender(env_type, sender, *, can_decide) -> bool   §3.2 发送者约束
  can_apply_blackboard_ops(env_type, *, sender_can_decide) -> bool  §3.3 门槛
"""

from __future__ import annotations

from orch.protocol.rules import (
    TYPE_RETENTION,
    allowed_sender,
    can_apply_blackboard_ops,
)
from orch.protocol.schema import AUTHOR_SCHEMA, validate_author_fields

__all__ = [
    "AUTHOR_SCHEMA",
    "validate_author_fields",
    "TYPE_RETENTION",
    "allowed_sender",
    "can_apply_blackboard_ops",
]
