"""Q7 裁决（选项 A）· 触发件可见性验收测试——测试先行，见红。

裁决（QUESTIONS.md Q7，2026-07-06 人类裁决）：
  本轮触发批次（view.event_ids）内的事件**无论保留策略**一律全文入焦点窗；
  批次外的 A/C/D 语义不变（A 仍只投影黑板、C 仍一行摘要、D 仍过期丢弃）。

背景（真实联跑铁证）：calc 线程 E5 gate_decision(approve)→moderator，渲出的
prompt 指令尾要求"只针对 #5 回应"，正文却不含 approve 任何字样（§6.2 焦点窗
只渲 B 类 × §10 要求申请者知道裁决的内部张力）——kimi 答"未收到 #5 事件内容"。

硬约束：顶层只 import 包；具体符号在函数体内引用（未实现 → 运行时红）。
"""

from __future__ import annotations

import orch.render
import orch.store

from tests.fixtures.m1_helpers import m1_config, seed_events


def _seed_gate_decision_thread(store) -> list[int]:
    """#1 B 类铺垫；#2 gate_decision(A 类, to=[moderator], body='approve')；
    #3 decision(A 类, 不在触发批次, 哨兵正文)；#4 B 类相关事件。"""
    specs = [
        {"sender": "moderator", "type": "handoff", "to": ["human"],
         "body": "活干完了请老板确认"},
        {"sender": "human", "type": "gate_decision", "to": ["moderator"],
         "body": "approve"},
        {"sender": "human", "type": "decision", "to": ["moderator"],
         "body": "决策哨兵甲未触发不应出现在焦点"},
        {"sender": "human", "type": "question", "to": ["moderator"],
         "body": "顺带问一句进度如何"},
    ]
    return seed_events(store, specs)


# ==================================================================
# (1) render_view：A 类触发件必须全文进焦点窗
# ==================================================================

def test_render_view_trigger_a_class_event_visible_in_focus(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_gate_decision_thread(st)
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="moderator", event_ids=[ids[1]],   # 触发件 = #2 gate_decision
        cold_start=True, instruction="请续走流程",
    )
    text = view["text"]
    assert "approve" in text, \
        "Q7 裁决 A：触发批次内的 gate_decision 必须全文可见（原缺陷：正文完全缺席）"
    assert "(gate_decision)" in text, "触发件应按焦点窗全文格式渲染（第三人称 + type）"


# ==================================================================
# (2) render_view：批次外 A 类语义不变（仍只投影黑板，不进焦点/背景）
# ==================================================================

def test_render_view_non_trigger_a_class_still_excluded(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_gate_decision_thread(st)
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="moderator", event_ids=[ids[3]],   # 触发件 = #4（B 类）
        cold_start=True, instruction="回答问题",
    )
    text = view["text"]
    assert "决策哨兵甲未触发不应出现在焦点" not in text, \
        "批次外 A 类事件仍只投影黑板（§3.2 语义不变），不得混入事件流"


# ==================================================================
# (3) render_delta：热续增量同享触发件可见性
# ==================================================================

def test_render_delta_trigger_a_class_event_visible(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_gate_decision_thread(st)
    cfg = m1_config()

    view = orch.render.render_delta(
        st, cfg, role="moderator",
        event_ids=[ids[1]],          # 触发件 = #2 gate_decision（A 类）
        last_evt=ids[0],             # 会话已消化到 #1
        instruction="请续走流程",
    )
    text = view["text"]
    assert "approve" in text, \
        "Q7 裁决 A：热续增量中触发批次的 A 类事件同样必须全文可见"
