"""M2 T1 · ApiAdapter / FakeApiAdapter 验收测试（spec §7.3，M2 契约 §2）。

覆盖任务卡条目 (b)：
  - 直连 messages 接口（假响应 dict，不打真实 API）。
  - supports_resume = False（§7.3 硬性：无会话概念）。
  - 永远**全量组装**（§7.3：不复用旧会话，每次都全量视图）。
  - 单步：本项目 API 型角色（moderator）不配工具，保持单步。

M2 边界：本次只测 FakeApiAdapter（可注入假 messages 响应 dict）；真实 ApiAdapter 骨架
类被断言存在（未实现即红）。

硬约束（契约 §1/§7）：
  - 顶层只 `import orch.adapters`；具体符号在函数体内引用（未实现 → 运行时红）。
  - 不打真实网络；用 monkeypatch/scripted_reply 注入假响应。
"""

from __future__ import annotations

import pytest

import orch.adapters  # 包级导入


def _api_cfg(**over) -> dict:
    """§11.1 API 型 config 最小结构。"""
    base = {
        "kind": "api",
        "model": "fake-model",
        "timeout_s": 30,
    }
    base.update(over)
    return base


def _view(role: str = "moderator", event_ids: list[int] | None = None,
          text: str = "view text"):
    return {
        "role": role,
        "event_ids": list(event_ids or [1]),
        "text": text,
        "sections": {},
        "meta": {},
    }


# ——————————————————————————————————————————————————————————————
# (b1) FakeApiAdapter：假响应 dict 直连
# ——————————————————————————————————————————————————————————————

def test_fake_api_adapter_returns_scripted_envelope():
    """§7.3：FakeApiAdapter 用可注入 scripted_reply 假装 messages 返回，
    将其归一化为**只含作者字段**的信封（适配层职责，§7.6 输出规范化）。"""
    scripted = {
        "to": ["pm"], "type": "assign", "body": "start",
    }
    ad = orch.adapters.FakeApiAdapter(
        role="moderator", config=_api_cfg(),
        scripted_reply=scripted,
    )
    env, sess = ad.invoke(_view("moderator"), None)
    assert env["type"] == "assign"
    assert env["to"] == ["pm"]
    assert env["body"] == "start"


def test_fake_api_adapter_returns_only_author_fields():
    """§3.1/§7.6：API 型信封归一化后**只含作者字段**（不带 from/re/id/ts/meta）。"""
    scripted = {
        "to": ["backend"], "type": "review", "body": "please review",
        # 假 API 端可能同时返回系统字段假名，但适配层必须剥掉（§16.11）。
        "from": "moderator", "id": 999, "ts": 0.0,
    }
    ad = orch.adapters.FakeApiAdapter(
        role="moderator", config=_api_cfg(),
        scripted_reply=scripted,
    )
    env, _ = ad.invoke(_view("moderator"), None)
    author = {"to", "type", "body", "artifacts", "corr", "blackboard_ops"}
    system = {"id", "thread_id", "ts", "from", "re", "meta"}
    assert set(env.keys()) <= author
    assert not (set(env.keys()) & system), "API 型适配器不得携带系统字段"


# ——————————————————————————————————————————————————————————————
# (b2) supports_resume = False（§7.3 硬性）
# ——————————————————————————————————————————————————————————————

def test_api_adapter_class_caps_supports_resume_false():
    """§7.3：API 型 supports_resume = False（无会话，每次全量组装）。
    真实 ApiAdapter 骨架类符号存在 + caps.supports_resume == False。"""
    ad = orch.adapters.ApiAdapter(role="moderator", config=_api_cfg())
    assert ad.caps["supports_resume"] is False


def test_fake_api_adapter_caps_supports_resume_false():
    ad = orch.adapters.FakeApiAdapter(
        role="moderator", config=_api_cfg(),
        scripted_reply={"to": ["pm"], "type": "assign", "body": "x"},
    )
    assert ad.caps["supports_resume"] is False


# ——————————————————————————————————————————————————————————————
# (b3) 永远全量组装：sess 参数即使非 None 也不复用
# ——————————————————————————————————————————————————————————————

def test_fake_api_adapter_ignores_incoming_session_returns_none():
    """§7.3：API 型无会话；即便调用方递入 sess=dict(...)，返回 sess 也应为 None
    （每次全量组装，不复用会话状态）。"""
    ad = orch.adapters.FakeApiAdapter(
        role="moderator", config=_api_cfg(),
        scripted_reply={"to": ["pm"], "type": "assign", "body": "x"},
    )
    incoming = {"sid": "should-be-ignored", "gen": 5, "last_evt": 42}
    _, sess = ad.invoke(_view("moderator"), incoming)
    assert sess is None


def test_fake_api_adapter_full_view_sent_every_call(monkeypatch):
    """§7.3：每次调用都发送**全量**视图文本（不做增量剪裁）。
    观察点：FakeApiAdapter 记录 last_view_text，两次调用两次都应等同于传入。"""
    ad = orch.adapters.FakeApiAdapter(
        role="moderator", config=_api_cfg(),
        scripted_reply={"to": ["pm"], "type": "assign", "body": "x"},
    )
    v1 = _view("moderator", [1], text="FULL-V1")
    v2 = _view("moderator", [2], text="FULL-V2")
    ad.invoke(v1, None)
    assert ad.last_view_text == "FULL-V1"
    ad.invoke(v2, None)
    assert ad.last_view_text == "FULL-V2"
    # 不做增量差分（无 diff）：两次的 view.text 均全量落入 FakeApiAdapter。


# ——————————————————————————————————————————————————————————————
# (b4) 单步：无工具循环（本项目 moderator 不配工具）
# ——————————————————————————————————————————————————————————————

def test_fake_api_adapter_single_step_no_tool_loop():
    """§7.3：API 型保持单步；无工具循环。FakeApiAdapter 应记录 step_count == 1。"""
    ad = orch.adapters.FakeApiAdapter(
        role="moderator", config=_api_cfg(),
        scripted_reply={"to": ["pm"], "type": "assign", "body": "x"},
    )
    ad.invoke(_view("moderator"), None)
    assert ad.step_count == 1


def test_api_adapter_moderator_default_tools_empty():
    """§7.3：本项目 API 型角色（moderator）不配工具；caps.tools == []。"""
    ad = orch.adapters.ApiAdapter(role="moderator", config=_api_cfg())
    assert ad.caps["tools"] == []
