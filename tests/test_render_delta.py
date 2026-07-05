"""M3-T1 · §6.5 热续增量提示 render_delta 验收测试——测试先行，见红。

覆盖任务卡 (a)：
  - §6.5 规则1：黑板 diff **必须显式**——渲染 last_evt 以来的 A 类变化，
    前缀「以下决策覆盖旧结论：」。
  - §6.5 规则3：新事件全文 = event_id > last_evt 的 B 类事件，第三人称，带 # 号。
  - §6.2 指令尾**必发**（§6.5 明列：热续时必须照发）。
  - §6.5 规则2：契约 version 变更 ≥ 1 → 主动作废 sid → meta.needs_cold_start = True。

硬约束（CLAUDE.md / M3 契约 §5）：
  - 顶层只 `import orch.render`；render_delta 具体符号在**函数体内**引用，
    未实现表现为运行时红（AttributeError）而非 collection 中断。
  - 只依赖 docs/m3-contract.md §2 冻结的公开签名与语义；不依赖未冻结内部细节。
  - §16.1/§16.7：路由/焦点只认 to，焦点第三人称，禁止解析 body 的 @/裸留第一人称。
"""

from __future__ import annotations

from pathlib import Path

import orch.render  # 包级导入（M3 新符号 render_delta 在函数体内引用）
import orch.store

from tests.fixtures.m1_helpers import m1_config, seed_events


# ——————————————————————————————————————————————————————————————
# 小工具：铺一段带 last_evt 前后 A/B 事件的线程
# ——————————————————————————————————————————————————————————————

def _seed_thread_with_history(store) -> list[int]:
    """铺 6 条事件：#1..#3 = 旧的 A/B 类（会话已消化），#4..#6 = 新的 A/B 类。

    对 backend 视角：
      #1 pm->backend,frontend review（B 相关；旧）
      #2 pm->moderator decision v1（A；旧；契约 like-api v1）
      #3 backend->pm question（B 相关；旧）
      #4 tester->backend defect（B 相关；新）
      #5 pm->moderator decision v2（A；新；契约 like-api v2，version 提升）
      #6 frontend->backend review（B 相关；新）
    """
    specs = [
        {"sender": "pm", "type": "review", "to": ["backend", "frontend"],
         "body": "PRD v1 发起评审"},
        {"sender": "pm", "type": "decision", "to": ["moderator"],
         "body": "契约 like-api v1 冻结",
         "blackboard_ops": [
             {"op": "freeze_contract", "name": "like-api",
              "path": "docs/like-api.md", "version": 1},
         ]},
        {"sender": "backend", "type": "question", "to": ["pm"],
         "body": "重复点赞语义?"},
        {"sender": "tester", "type": "defect", "to": ["backend"],
         "body": "已删帖子返回500"},
        {"sender": "pm", "type": "decision", "to": ["moderator"],
         "body": "契约 like-api v2 冻结",
         "blackboard_ops": [
             {"op": "freeze_contract", "name": "like-api",
              "path": "docs/like-api.md", "version": 2},
             {"op": "set_decision", "text": "重复点赞=取消赞（幂等）"},
         ]},
        {"sender": "frontend", "type": "review", "to": ["backend"],
         "body": "联调评审"},
    ]
    return seed_events(store, specs)


# ==================================================================
# (a-1) §6.5 规则3：热续增量含"event_id > last_evt 的 B 类新事件全文（第三人称、带 # 号）"
# ==================================================================

def test_render_delta_includes_new_b_events_only(thread_dir):
    """§6.5 规则3：新事件段只含 event_id > last_evt 的 B 类事件（第三人称、带 # 号）。

    对 backend 视角，last_evt=3，则新事件应含 #4 defect（相关，to∋backend）与
    #6 review（to∋backend）；#5 是 A 类（黑板），不进新事件段。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_thread_with_history(st)
    cfg = m1_config()

    view = orch.render.render_delta(
        st, cfg,
        role="backend",
        event_ids=[ids[3]],           # 本轮触发件 = #4
        last_evt=ids[2],              # 会话上次消化到 #3
        instruction="修 defect",
    )

    text = view["text"]
    # 事件号 4 与 6（B 类相关）应出现；5（A 类）不应作为"事件"再次出现在焦点段。
    assert "#4" in text, "§6.5 规则3：新事件段应含 #4"
    assert "#6" in text, "§6.5 规则3：新事件段应含 #6"
    # 第三人称标签（§16.7）；焦点行含 [from->@to] (type):。
    assert "[tester->@backend] (defect)" in text
    assert "[frontend->@backend] (review)" in text
    # 旧的 #1/#3（已消化）不再重复出现在新事件段——热续只发增量。
    # 用严格新事件段的判定：#1 出现的行不该含"[pm->@backend,@frontend] (review)"
    # 更宽松的断言：本轮无 #3 的事件行（不再重发已消化事件全文）。
    assert "#3 [backend->" not in text, "§6.5：event_id ≤ last_evt 的事件不重发"


# ==================================================================
# (a-2) §6.5 规则1：黑板 diff 前缀"以下决策覆盖旧结论："
# ==================================================================

def test_render_delta_blackboard_diff_has_override_prefix(thread_dir):
    """§6.5 规则1：last_evt 以来的 A 类变化，前缀「以下决策覆盖旧结论：」。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_thread_with_history(st)
    cfg = m1_config()

    view = orch.render.render_delta(
        st, cfg,
        role="backend",
        event_ids=[ids[3]],
        last_evt=ids[2],              # #3 之后有 A 类 #5（decision v2）
        instruction="修 defect",
    )
    text = view["text"]
    assert "以下决策覆盖旧结论：" in text, \
        "§6.5 规则1：黑板 diff 必须显式带前缀「以下决策覆盖旧结论：」"


def test_render_delta_no_blackboard_diff_when_no_new_A(thread_dir):
    """last_evt 之后**无** A 类事件 → 不产生"覆盖旧结论"段（避免误报覆盖）。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_thread_with_history(st)
    cfg = m1_config()

    # last_evt = #5，last_evt 后 A 类=∅，只剩 #6（B）。
    view = orch.render.render_delta(
        st, cfg,
        role="backend",
        event_ids=[ids[5]],
        last_evt=ids[4],
        instruction="继续",
    )
    assert "以下决策覆盖旧结论：" not in view["text"]


# ==================================================================
# (a-3) §6.2/§6.5：指令尾**必发**
# ==================================================================

def test_render_delta_instruction_tail_always_present(thread_dir):
    """§6.2/§6.5：热续时指令尾"你是 {role}。现在只针对 #{ids} 回应：{instruction}"必发。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_thread_with_history(st)
    cfg = m1_config()

    view = orch.render.render_delta(
        st, cfg,
        role="backend",
        event_ids=[ids[3]],
        last_evt=ids[2],
        instruction="修 defect",
    )
    text = view["text"]
    assert "你是 backend" in text, "§6.2/§6.5：指令尾必发（角色声明）"
    assert "#4" in text, "指令尾应含本轮触发件号 #4"
    assert "修 defect" in text, "指令尾应含本轮指令原文"


# ==================================================================
# (a-4) §6.5 规则2：契约 version 变更 ≥ 1 → 主动作废 sid，meta.needs_cold_start=True
# ==================================================================

def test_render_delta_version_bump_sets_needs_cold_start(thread_dir):
    """§6.5 规则2：契约 version 变更 ≥ 1 → 提示需作废 sid，meta.needs_cold_start=True。

    last_evt=#3，last_evt 之后 A 类新事件 #5 把 like-api 从 v1(#2) 提到 v2 → 版本变更 1。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_thread_with_history(st)
    cfg = m1_config()

    view = orch.render.render_delta(
        st, cfg,
        role="backend",
        event_ids=[ids[3]],
        last_evt=ids[2],
        instruction="修 defect",
    )
    assert view["meta"].get("needs_cold_start") is True, \
        "§6.5 规则2：契约 version 变更 ≥ 1 应触发 needs_cold_start=True"


def test_render_delta_no_version_bump_keeps_hot_resume(thread_dir):
    """无契约 version 变更 → meta.needs_cold_start=False（继续热续增量）。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_thread_with_history(st)
    cfg = m1_config()

    # last_evt=#5：之后只剩 #6（B 类），无 A 类。
    view = orch.render.render_delta(
        st, cfg,
        role="backend",
        event_ids=[ids[5]],
        last_evt=ids[4],
        instruction="继续",
    )
    assert view["meta"].get("needs_cold_start") is False


# ==================================================================
# (a-5) 增量事件号照带（§6.5 规则3：配合系统层幂等指令）
# ==================================================================

def test_render_delta_carries_event_numbers(thread_dir):
    """§6.5 规则3：增量中事件号**照带**（配合系统层幂等指令实现会话端去重）。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_thread_with_history(st)
    cfg = m1_config()

    view = orch.render.render_delta(
        st, cfg,
        role="backend",
        event_ids=[ids[3]],
        last_evt=ids[2],
        instruction="修 defect",
    )
    # 事件号 4/6 应出现（连续），且第三人称格式带 # 前缀。
    assert "#4 [" in view["text"]
    assert "#6 [" in view["text"]
