"""协议层验收测试（spec §3、附录A）。

覆盖任务卡条目：
  (a) 附录A 信封作者字段 schema 校验：合法 / 非法（缺 required、多余字段
      additionalProperties、type 非枚举、body 空串、blackboard_ops 结构错误）。
  (b) §3.2 发送者约束 + 保留策略映射；§3.3 blackboard_ops 应用门槛。

硬约束：顶层只 import 包（orch.protocol）；具体符号在函数体内引用，
未实现即运行时红。断言仅依赖契约 §1 公开签名（AUTHOR_SCHEMA / validate_author_fields /
TYPE_RETENTION / allowed_sender / can_apply_blackboard_ops）。
"""

from __future__ import annotations

import orch.protocol  # 包级导入（契约 §7）


# ——————————————————————————————————————————————————————————————
# (a) 附录A JSON Schema 校验：validate_author_fields 返回错误消息列表，空=合法
# ——————————————————————————————————————————————————————————————

def _minimal_valid_env() -> dict:
    """满足 required=[to,type,body] 的最小合法作者字段信封。"""
    return {"to": ["backend"], "type": "assign", "body": "开工"}


def test_author_schema_is_draft07_object():
    schema = orch.protocol.AUTHOR_SCHEMA
    assert isinstance(schema, dict)
    # 附录A 原样：draft-07、object、禁止多余字段、required 三件套。
    assert schema.get("$schema") == "http://json-schema.org/draft-07/schema#"
    assert schema.get("type") == "object"
    assert schema.get("additionalProperties") is False
    assert set(schema.get("required", [])) == {"to", "type", "body"}


def test_valid_minimal_envelope_passes():
    errs = orch.protocol.validate_author_fields(_minimal_valid_env())
    assert errs == []


def test_valid_full_envelope_passes():
    env = {
        "to": ["backend", "frontend"],
        "type": "review",
        "body": "PRD v1",
        "artifacts": ["docs/prd.md"],
        "corr": None,
        "blackboard_ops": [
            {"op": "freeze_contract", "name": "api", "path": "docs/api.md", "version": 1},
            {"op": "set_decision", "text": "幂等"},
            {"op": "set_task", "key": "backend.impl", "status": "done"},
        ],
    }
    assert orch.protocol.validate_author_fields(env) == []


def test_corr_null_is_allowed():
    env = _minimal_valid_env()
    env["corr"] = None
    assert orch.protocol.validate_author_fields(env) == []


def test_bb_ops_null_is_allowed():
    env = _minimal_valid_env()
    env["blackboard_ops"] = None
    assert orch.protocol.validate_author_fields(env) == []


def test_all_type_enum_values_pass_schema():
    for t in [
        "assign", "review", "question", "answer", "decision", "handoff",
        "report", "defect", "acceptance", "gate_request", "gate_decision",
        "system", "terminate", "chat",
    ]:
        env = {"to": [], "type": t, "body": "x"}
        assert orch.protocol.validate_author_fields(env) == [], f"type={t} 应合法"


# —— 非法：缺 required ——

def test_missing_to_is_rejected():
    env = {"type": "assign", "body": "x"}
    assert orch.protocol.validate_author_fields(env) != []


def test_missing_type_is_rejected():
    env = {"to": [], "body": "x"}
    assert orch.protocol.validate_author_fields(env) != []


def test_missing_body_is_rejected():
    env = {"to": [], "type": "assign"}
    assert orch.protocol.validate_author_fields(env) != []


# —— 非法：多余字段（additionalProperties=false）——

def test_additional_property_is_rejected():
    env = _minimal_valid_env()
    env["nickname"] = "bad"
    assert orch.protocol.validate_author_fields(env) != []


def test_system_field_from_in_author_payload_is_rejected():
    # 系统字段 from/re/id/ts/meta 不属作者字段；作者信封里带它们应被 additionalProperties 拒绝
    # （§3.1：系统字段由编排器权威赋值，不信模型自报）。
    env = _minimal_valid_env()
    env["from"] = "backend"
    assert orch.protocol.validate_author_fields(env) != []


# —— 非法：type 非枚举 ——

def test_type_not_in_enum_is_rejected():
    env = {"to": [], "type": "not_a_real_type", "body": "x"}
    assert orch.protocol.validate_author_fields(env) != []


# —— 非法：body 空串（minLength=1）——

def test_empty_body_is_rejected():
    env = {"to": [], "type": "assign", "body": ""}
    assert orch.protocol.validate_author_fields(env) != []


# —— 非法：to 非字符串数组 ——

def test_to_wrong_item_type_is_rejected():
    env = {"to": [123], "type": "assign", "body": "x"}
    assert orch.protocol.validate_author_fields(env) != []


def test_to_not_array_is_rejected():
    env = {"to": "backend", "type": "assign", "body": "x"}
    assert orch.protocol.validate_author_fields(env) != []


# —— 非法：blackboard_ops 结构错误 ——

def test_bb_ops_missing_op_key_is_rejected():
    env = _minimal_valid_env()
    env["blackboard_ops"] = [{"text": "无 op 键"}]
    assert orch.protocol.validate_author_fields(env) != []


def test_bb_ops_bad_op_enum_is_rejected():
    env = _minimal_valid_env()
    env["blackboard_ops"] = [{"op": "delete_everything"}]
    assert orch.protocol.validate_author_fields(env) != []


def test_bb_ops_version_wrong_type_is_rejected():
    env = _minimal_valid_env()
    env["blackboard_ops"] = [
        {"op": "freeze_contract", "name": "api", "path": "p", "version": "two"}
    ]
    assert orch.protocol.validate_author_fields(env) != []


def test_bb_ops_not_array_of_objects_is_rejected():
    env = _minimal_valid_env()
    env["blackboard_ops"] = ["set_decision"]
    assert orch.protocol.validate_author_fields(env) != []


# ——————————————————————————————————————————————————————————————
# (b) §3.2 保留策略映射（TYPE_RETENTION：type -> "A"/"B"/"C"/"D"）
# ——————————————————————————————————————————————————————————————

def test_type_retention_covers_all_types():
    tr = orch.protocol.TYPE_RETENTION
    assert isinstance(tr, dict)
    for t in [
        "assign", "review", "question", "answer", "decision", "handoff",
        "report", "defect", "acceptance", "gate_request", "gate_decision",
        "system", "terminate", "chat",
    ]:
        assert t in tr, f"缺 {t} 的保留策略"
        assert tr[t] in {"A", "B", "C", "D"}


def test_type_retention_permanent_A_class():
    tr = orch.protocol.TYPE_RETENTION
    # §3.2 表：decision / acceptance 为 A（永久，投影黑板）。
    assert tr["decision"] == "A"
    assert tr["acceptance"] == "A"
    assert tr["gate_request"] == "A"
    assert tr["gate_decision"] == "A"
    assert tr["terminate"] == "A"


def test_type_retention_B_class():
    tr = orch.protocol.TYPE_RETENTION
    for t in ["assign", "review", "question", "answer", "handoff", "defect"]:
        assert tr[t] == "B", f"{t} 应为 B 类"


def test_type_retention_C_class():
    tr = orch.protocol.TYPE_RETENTION
    assert tr["report"] == "C"
    assert tr["system"] == "C"


def test_type_retention_D_class():
    assert orch.protocol.TYPE_RETENTION["chat"] == "D"


# ——————————————————————————————————————————————————————————————
# (b) §3.2 发送者约束 allowed_sender(env_type, sender, *, can_decide)
# ——————————————————————————————————————————————————————————————

def test_decision_sender_requires_can_decide_or_human():
    # decision：仅 can_decide 角色或 human。
    assert orch.protocol.allowed_sender("decision", "pm", can_decide=True) is True
    assert orch.protocol.allowed_sender("decision", "human", can_decide=False) is True
    assert orch.protocol.allowed_sender("decision", "backend", can_decide=False) is False


def test_gate_decision_sender_only_human():
    assert orch.protocol.allowed_sender("gate_decision", "human", can_decide=False) is True
    assert orch.protocol.allowed_sender("gate_decision", "moderator", can_decide=True) is False
    assert orch.protocol.allowed_sender("gate_decision", "pm", can_decide=True) is False


def test_system_sender_only_orchestrator():
    # system：仅编排器（sender == 'system'）。
    assert orch.protocol.allowed_sender("system", "system", can_decide=False) is True
    assert orch.protocol.allowed_sender("system", "moderator", can_decide=True) is False
    assert orch.protocol.allowed_sender("system", "human", can_decide=False) is False


def test_terminate_sender_moderator_tester_human_only():
    for s in ["moderator", "tester", "human"]:
        assert orch.protocol.allowed_sender("terminate", s, can_decide=(s == "moderator")) is True
    for s in ["backend", "frontend", "pm"]:
        assert orch.protocol.allowed_sender("terminate", s, can_decide=False) is False


def test_open_types_allow_any_sender():
    # §3.2：assign/review/question/answer/handoff/report/defect/acceptance/chat/gate_request → 任意
    for t in ["assign", "review", "question", "answer", "handoff",
              "report", "defect", "acceptance", "chat", "gate_request"]:
        assert orch.protocol.allowed_sender(t, "backend", can_decide=False) is True, f"{t} 应任意发送者"


# ——————————————————————————————————————————————————————————————
# (b) §3.3 blackboard_ops 门槛 can_apply_blackboard_ops(env_type, *, sender_can_decide)
# ——————————————————————————————————————————————————————————————

def test_bb_ops_gate_type_and_can_decide_required():
    can = orch.protocol.can_apply_blackboard_ops
    # 门槛：type ∈ {decision, acceptance, gate_decision} 且 sender 具 can_decide。
    for t in ["decision", "acceptance", "gate_decision"]:
        assert can(t, sender_can_decide=True) is True, f"{t}+can_decide 应放行"
        assert can(t, sender_can_decide=False) is False, f"{t} 无 can_decide 应拒"


def test_bb_ops_gate_rejects_other_types():
    can = orch.protocol.can_apply_blackboard_ops
    for t in ["assign", "review", "report", "handoff", "defect",
              "gate_request", "system", "terminate", "chat", "question", "answer"]:
        assert can(t, sender_can_decide=True) is False, f"{t} 非 A 决策类应拒绝应用 bb_ops"
