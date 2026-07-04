"""附录A：信封作者字段 JSON Schema（draft-07）。

本模块只承载 schema 常量与校验函数。schema **必须**与 spec 附录A 逐字一致：
draft-07、object、additionalProperties=false、required=[to,type,body]、
type 枚举 14 项、body minLength=1、blackboard_ops 结构约束。

系统字段（id/thread_id/ts/from/re/meta）不在此 schema 内（spec §3.1）：
编排器权威赋值，模型输出中的同名字段一律丢弃。故信封若携带 from/re/... 等系统字段，
会被 additionalProperties=false 拒绝——这正是 §16 反模式11 的协议侧强制点。

发送者约束（§3.2）与 bb_ops 门槛（§3.3）在 schema 校验之后单独执行，见 rules.py。
"""

from __future__ import annotations

# 附录A 原样 JSON Schema（draft-07）。字段顺序、约束逐字对齐 spec 附录A。
AUTHOR_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "required": ["to", "type", "body"],
    "properties": {
        "to": {"type": "array", "items": {"type": "string"}},
        "type": {
            "enum": [
                "assign",
                "review",
                "question",
                "answer",
                "decision",
                "handoff",
                "report",
                "defect",
                "acceptance",
                "gate_request",
                "gate_decision",
                "system",
                "terminate",
                "chat",
            ]
        },
        "body": {"type": "string", "minLength": 1},
        "artifacts": {"type": "array", "items": {"type": "string"}},
        "corr": {"type": ["string", "null"]},
        "blackboard_ops": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "required": ["op"],
                "properties": {
                    "op": {"enum": ["set_decision", "freeze_contract", "set_task"]},
                    "text": {"type": "string"},
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "version": {"type": "integer"},
                    "key": {"type": "string"},
                    "status": {"type": "string"},
                },
            },
        },
    },
}


def validate_author_fields(obj: dict) -> list[str]:
    """用 jsonschema 校验作者字段（附录A）。返回错误消息列表；空列表 = 合法。

    只校验作者字段；系统字段（from/re/id/ts/meta）不在此校验——它们若出现在作者
    信封里会被 additionalProperties=false 判为非法（§3.1、§16.11）。

    收集全部校验错误（而非首个即停），便于调用方一次性反馈。
    """
    # 延迟导入，保持包级 import orch.protocol 不因缺依赖而崩（依赖在白名单内，§14）。
    from jsonschema import Draft7Validator

    validator = Draft7Validator(AUTHOR_SCHEMA)
    return [error.message for error in validator.iter_errors(obj)]
