"""M1 · §6 上下文组装（视图渲染）验收测试 —— 测试先行，见红。

覆盖任务卡条目：
  (a) §6.1/§6.2 视图四层组装：render_view 返回 RenderedView；五段顺序快照
      (system->blackboard->background->focus->instruction)；系统层含
      权限申报原文 / 身份声明 / 输出格式 / 幂等指令；指令尾格式
      "你是 {role}。现在只针对 #{ids} 回应"。
  (b) §6.2 焦点窗第三人称：render_event_third_person 输出
      "#id [from->@to] (type): body摘要"；焦点窗判定
      (to∋role)∨(from==role)∨(re∩role事件≠∅)；无第一人称"我"原文流（§16.7）。
  (c) §3.2 保留策略在渲染中的落位：A->黑板、B相关->焦点、B不相关->背景、
      C->背景一行、D 超 chat_ttl->丢弃。
  (d) §6.3 预算压缩：超 context_window 场景，meta.dropped 顺序
      （背景最旧先丢 -> 焦点最旧截断保首尾）。

硬约束（契约 §6 / CLAUDE.md）：
  - 顶层只 import orch.render / orch.store / orch.scheduler（包级，T0 已保证可导入）；
    render_view / render_event_third_person / estimate_tokens 等具体符号在**函数体内**引用，
    使未实现表现为运行时红（fail/error）而非 collection 中断。
  - 只依赖 docs/m1-contract.md §1 冻结的公开签名与返回约定；不依赖未冻结内部细节。
  - §16.1：路由/焦点只认 to（信封->显示，不反向解析 body 的 @）。
  - §16.7：焦点窗第三人称，禁止第一人称"我"原文流。
"""

from __future__ import annotations

from pathlib import Path

import orch.render  # 包级导入（符号在函数体内引用）
import orch.store

from tests.fixtures.m1_helpers import PROMPT_MARKER, m1_config, seed_events


# ——————————————————————————————————————————————————————————————
# 小工具：稳定取五段顺序 / 焦点窗事件号（只读断言辅助，不含被测逻辑）
# ——————————————————————————————————————————————————————————————

_SECTION_ORDER = ["system", "blackboard", "background", "focus", "instruction"]


def _seed_like_thread(store):
    """铺一段"点赞功能"风格的多角色事件流，供保留策略/焦点窗断言。

    返回落盘事件号列表 ids（升序）。角色关系与附录B 同构：
      E1 human->∅ assign(D 计不进，用 assign=B)  实际见下方 specs 注释。
    """
    specs = [
        # 0: pm 对 backend,frontend 发起评审（B 类）——对 backend 视角是"相关"（to∋backend）。
        {"sender": "pm", "type": "review", "to": ["backend", "frontend"],
         "body": "PRD v1 发起评审"},
        # 1: pm->moderator 决策（A 类，freeze v2）——投影黑板，对所有角色黑板层可见。
        {"sender": "pm", "type": "decision", "to": ["moderator"],
         "body": "重复点赞=取消赞（幂等），契约升级 v2",
         "blackboard_ops": [
             {"op": "freeze_contract", "name": "like-api",
              "path": "docs/like-api.md", "version": 2},
             {"op": "set_decision", "text": "重复点赞=取消赞（幂等）"},
             {"op": "set_task", "key": "backend.impl", "status": "done"},
         ]},
        # 2: frontend->tester 报告（C 类）——背景层一行（对 backend 既非相关 B 也非 A）。
        {"sender": "frontend", "type": "report", "to": ["tester"],
         "body": "前端 mock 完成待联调"},
        # 3: tester->backend 缺陷（B 类，to∋backend）——backend 焦点窗全文。
        {"sender": "tester", "type": "defect", "to": ["backend"],
         "body": "已删帖子点赞返回 500 应为 404，详见 reports/r1.md"},
    ]
    return seed_events(store, specs)


# ==================================================================
# (a) §6.1/§6.2 四层组装：RenderedView 结构 + 五段顺序 + 系统层内容 + 指令尾
# ==================================================================

def test_render_view_returns_rendered_view_shape(thread_dir):
    """render_view 返回 RenderedView：含 role/event_ids/text/sections/meta（契约 §1）。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_like_thread(st)
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[ids[3]],
        cold_start=True, instruction="修复缺陷",
    )

    # 契约 §1 RenderedView 五键。
    for key in ("role", "event_ids", "text", "sections", "meta"):
        assert key in view, f"RenderedView 必须含键 {key}（契约 §1）"
    assert view["role"] == "backend"
    assert view["event_ids"] == [ids[3]], "event_ids = 本轮要回应的事件号（升序）"
    assert isinstance(view["text"], str) and view["text"], "text 为五段拼接文本"
    assert isinstance(view["sections"], dict)
    assert isinstance(view["meta"], dict)


def test_render_view_five_section_order(thread_dir):
    """§6.1 五段固定顺序 system->blackboard->background->focus->instruction。

    既断言 sections 键齐全，又断言这五段在 text 中的**出现次序**与规定一致
    （位置效应：两端强中间弱，顺序固定）。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_like_thread(st)
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[ids[3]],
        cold_start=True, instruction="修复缺陷",
    )
    sections = view["sections"]

    # 五段键齐全（契约 §1）。
    for seg in _SECTION_ORDER:
        assert seg in sections, f"sections 必须含 {seg} 段（契约 §1/§6.1）"

    # 五段在 text 中按固定顺序出现（用各段非空文本在 text 中的 index 单调递增判定）。
    text = view["text"]
    positions = []
    for seg in _SECTION_ORDER:
        body = sections[seg]
        assert isinstance(body, str)
        if body.strip():
            idx = text.find(body.strip().splitlines()[0])
            assert idx >= 0, f"{seg} 段内容必须出现在拼接 text 中"
            positions.append((seg, idx))
    seg_names = [s for s, _ in positions]
    idxs = [i for _, i in positions]
    assert idxs == sorted(idxs), (
        f"五段顺序必须为 system->blackboard->background->focus->instruction，实际次序 {seg_names}"
    )


def test_render_view_system_layer_permission_and_identity(thread_dir):
    """§6.2 系统层含：角色身份原文 + 权限申报原文 + 身份声明 + 输出格式 + 幂等指令。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_like_thread(st)
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[ids[3]],
        cold_start=True, instruction="修复缺陷",
    )
    system = view["sections"]["system"]

    # 角色身份：读 config.roles[backend].prompt 文件内容（含夹具标记）。
    assert PROMPT_MARKER["backend"] in system, (
        "系统层必须含角色 prompt 文件原文（§6.2 身份与职责）"
    )

    # 权限申报原文（§6.2 逐字）：可写: {write_scope}；可用工具: {tools}；越权写入会被系统整体拒收。
    assert "可写:" in system and "server/" in system, "权限申报须含 write_scope"
    assert "可用工具:" in system, "权限申报须含 可用工具: 前缀"
    assert "越权写入会被系统整体拒收" in system, "权限申报原文须逐字含此句（§6.2）"

    # 身份声明（§6.2 逐字，含角色名）。
    assert "以下历史中标注 [backend] 的发言是你自己说过的话" in system, (
        "系统层须含身份声明原文（§6.2）"
    )

    # 输出格式：要求以 json 信封结束 + 最小示例（§6.2）。
    assert "json" in system.lower(), "输出格式须要求 json 信封（§6.2）"
    # 幂等指令（§6.2）。
    assert "编号" in system and ("已处理" in system or "重发" in system), (
        "系统层须含幂等指令：已处理过的编号直接重发当次信封（§6.2）"
    )


def test_render_view_instruction_tail_format(thread_dir):
    """§6.2 指令尾格式："你是 {role}。现在只针对 #{ids} 回应：{instruction}"。

    §6.2：长对话抗角色漂移主要靠这一句；#ids 须逐一出现。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_like_thread(st)
    cfg = m1_config()

    # 用两个事件号（聚合批），断言两个 # 号都出现在指令尾。
    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[ids[0], ids[3]],
        cold_start=True, instruction="并批处理",
    )
    instruction = view["sections"]["instruction"]

    assert "你是 backend" in instruction, "指令尾须含 [你是 {role}] 句式（§6.2）"
    assert "现在只针对" in instruction and "回应" in instruction, (
        "指令尾须含「现在只针对 #{ids} 回应」（§6.2）"
    )
    for eid in (ids[0], ids[3]):
        assert f"#{eid}" in instruction, f"指令尾须逐一含事件号 #{eid}"
    assert "并批处理" in instruction, "指令尾须含本轮指令原文"


def test_render_view_instruction_tail_always_present(thread_dir):
    """§6.2：指令尾**必须**照发（热续也不省略）。cold_start=False 时指令尾仍非空。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_like_thread(st)
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[ids[3]],
        cold_start=False, instruction="续跑",
    )
    assert view["sections"]["instruction"].strip(), "指令尾热续必发，不得省略（§6.2）"
    assert f"#{ids[3]}" in view["sections"]["instruction"]


def test_render_view_blackboard_layer_projects_board(thread_dir):
    """§6.1 黑板层 = board.md 全文；A 类决策（freeze v2）投影后应出现在黑板段。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_like_thread(st)
    # 应用 E(decision) 的 bb_ops（调度层已判权限；此处测试直接投影，模拟已冻结）。
    dec = st.events()[1]
    orch.store.apply_blackboard_ops(st, dec["blackboard_ops"], dec["id"])
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[ids[3]],
        cold_start=True, instruction="x",
    )
    blackboard = view["sections"]["blackboard"]
    assert "like-api" in blackboard, "黑板层须含冻结契约名（board.md 投影，§6.1）"
    assert "v2" in blackboard or "2" in blackboard, "黑板层须反映契约版本 v2"


# ==================================================================
# (b) §6.2 焦点窗第三人称：格式 + 判定 + 无第一人称
# ==================================================================

def test_render_event_third_person_format(thread_dir):
    """§6.2 第三人称单事件渲染："#id [from->@to] (type): body摘要"。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_like_thread(st)
    # E(tester->backend defect)。
    defect = st.events()[3]

    line = orch.render.render_event_third_person(defect, viewer_role="backend")

    assert isinstance(line, str) and line.strip()
    assert f"#{defect['id']}" in line, "须带事件号 #id"
    # 第三人称角色标签 [from->@to]（方向恒为 信封->显示，§16.1）。
    assert "tester" in line and "backend" in line, "须含 from(tester) 与 to(backend)"
    assert "->" in line, "须用 [from->@to] 箭头标签（§6.2）"
    assert "@" in line, "to 目标须带 @ 前缀（由 to 渲染，§16.1）"
    assert "(defect)" in line, "须带 (type) 标签"


def test_render_event_third_person_multi_to(thread_dir):
    """to 多目标时逐一 @ 渲染（§16.1 信封->显示，不从 body 反解析）。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    _seed_like_thread(st)
    review = st.events()[0]  # pm->[backend,frontend] review

    line = orch.render.render_event_third_person(review, viewer_role="frontend")

    assert f"#{review['id']}" in line
    assert "pm" in line, "from=pm"
    assert "@backend" in line and "@frontend" in line, "to 两目标都须 @ 渲染"
    assert "(review)" in line


def test_focus_window_predicate_included_cases(thread_dir):
    """§6.2 焦点窗判定 (to∋role)∨(from==role)∨(re∩role事件≠∅)：满足者进焦点窗全文。

    构造三条 backend 应入焦点的 B 类事件（各命中一个析取项），断言焦点段含其正文。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    specs = [
        # (1) to∋backend
        {"sender": "tester", "type": "defect", "to": ["backend"],
         "body": "FOCUS_TO_BACKEND 缺陷正文"},
        # (2) from==backend（backend 自己发的）
        {"sender": "backend", "type": "handoff", "to": ["tester"],
         "body": "FOCUS_FROM_BACKEND 交接正文"},
    ]
    ids = seed_events(st, specs)
    # (3) re∩backend事件≠∅：pm 回复 re 含 backend 发过的事件 ids[1]。
    e3 = st.append_event(sender="pm", type="answer", to=["moderator"],
                         body="FOCUS_RE_BACKEND 回应正文", re=[ids[1]])
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[ids[0]],
        cold_start=True, instruction="x",
    )
    focus = view["sections"]["focus"]

    assert "FOCUS_TO_BACKEND" in focus, "to∋role -> 焦点窗全文"
    assert "FOCUS_FROM_BACKEND" in focus, "from==role -> 焦点窗全文"
    assert "FOCUS_RE_BACKEND" in focus, "re∩role事件≠∅ -> 焦点窗全文"


def test_focus_window_excludes_unrelated(thread_dir):
    """§6.2：与 backend 无关的 B 类（to/from/re 均不涉 backend）不进焦点窗。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # frontend↔pm 的往来，与 backend 无关。
    specs = [
        {"sender": "frontend", "type": "question", "to": ["pm"],
         "body": "UNRELATED_TO_BACKEND 前端提问"},
    ]
    ids = seed_events(st, specs)
    # 给 backend 一个触发事件（自身相关），确保 render 有焦点锚点但不含无关件。
    e_trigger = st.append_event(sender="tester", type="defect", to=["backend"],
                                body="BACKEND_TRIGGER")
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[e_trigger],
        cold_start=True, instruction="x",
    )
    assert "UNRELATED_TO_BACKEND" not in view["sections"]["focus"], (
        "与 backend 无关的 B 类不得进焦点窗（§6.2 判定为假）"
    )


def test_focus_window_no_first_person_raw_stream(thread_dir):
    """§16.7：焦点窗禁止保留第一人称原文流——统一第三人称角色标签。

    构造一条 backend 自己发的、正文含第一人称"我"的事件，断言焦点窗渲染后该事件
    以第三人称标签 [backend->@…] 呈现（而非原样第一人称流）。判据：焦点段每条渲染行
    都带 [from->@to] (type) 第三人称标签前缀。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="backend", type="handoff", to=["tester"],
                         body="我已经修复了缺陷")  # 原文第一人称
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[e1],
        cold_start=True, instruction="x",
    )
    focus = view["sections"]["focus"]

    # 该事件在焦点窗必须带第三人称标签（[backend->@tester] (handoff)）。
    assert f"#{e1}" in focus
    assert "[backend" in focus and "@tester" in focus and "(handoff)" in focus, (
        "焦点窗须以第三人称角色标签渲染，不得裸留第一人称原文流（§16.7）"
    )


# ==================================================================
# (c) §3.2 保留策略在渲染中的落位：A->黑板 / B相关->焦点 / B不相关->背景 / C->背景 / D超ttl->丢弃
# ==================================================================

def test_retention_A_projects_to_blackboard_not_focus(thread_dir):
    """§3.2 A 类（decision）投影黑板层，不作为普通事件出现在焦点/背景窗。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_like_thread(st)
    dec = st.events()[1]  # pm decision（A）
    orch.store.apply_blackboard_ops(st, dec["blackboard_ops"], dec["id"])
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[ids[3]],
        cold_start=True, instruction="x",
    )
    # A 类投影黑板（契约名出现在黑板段）。
    assert "like-api" in view["sections"]["blackboard"]
    # A 类不以第三人称事件行 "#<id> [pm->@moderator] (decision)" 混入焦点/背景（它走黑板投影）。
    dec_line_tag = f"#{dec['id']} [pm"
    assert dec_line_tag not in view["sections"]["focus"], "A 类不进焦点窗事件流（走黑板投影）"
    assert dec_line_tag not in view["sections"]["background"], "A 类不进背景层事件流"


def test_retention_B_relevant_to_focus(thread_dir):
    """§3.2 B 类且与 backend 相关（to∋backend）-> 焦点窗全文。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_like_thread(st)
    defect = st.events()[3]  # tester->backend defect（B，相关）
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[ids[3]],
        cold_start=True, instruction="x",
    )
    focus = view["sections"]["focus"]
    assert f"#{defect['id']}" in focus, "相关 B 类须在焦点窗"
    assert "已删帖子" in focus, "焦点窗为全文（含正文细节）"


def test_retention_B_irrelevant_to_background(thread_dir):
    """§3.2 B 类但与 backend 不相关 -> 背景层一行摘要（非焦点全文）。

    pm->[backend,frontend] review（ids[0]）对 backend 其实相关（to∋backend）；
    改用一条 frontend->pm 的 B 类（question）作"不相关 B"样本。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 不相关 B：frontend->pm。
    e_irr = st.append_event(sender="frontend", type="question", to=["pm"],
                            body="IRRELEVANT_B 前端问 pm 的问题正文很长很长")
    # backend 的触发锚点（相关）。
    e_trig = st.append_event(sender="tester", type="defect", to=["backend"],
                             body="锚点缺陷")
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[e_trig],
        cold_start=True, instruction="x",
    )
    background = view["sections"]["background"]
    focus = view["sections"]["focus"]

    assert f"#{e_irr}" in background, "不相关 B 类须落背景层一行摘要"
    assert "IRRELEVANT_B" not in focus, "不相关 B 类不得进焦点窗"


def test_retention_C_report_to_background_one_line(thread_dir):
    """§3.2 C 类（report）一律一行摘要进背景层（不论相关与否）。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    ids = _seed_like_thread(st)
    report_ev = st.events()[2]  # frontend->tester report（C）
    e_trig = st.append_event(sender="tester", type="defect", to=["backend"],
                             body="锚点")
    cfg = m1_config()

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[e_trig],
        cold_start=True, instruction="x",
    )
    background = view["sections"]["background"]
    assert f"#{report_ev['id']}" in background, "C 类 report 须在背景层"
    # 背景层是"一行摘要"：该事件在背景段占的渲染不应包含焦点窗式全文换行块——
    # 以行数近似断言（该事件号所在行是单行摘要）。
    bg_lines = [ln for ln in background.splitlines() if f"#{report_ev['id']}" in ln]
    assert len(bg_lines) == 1, "C 类在背景层应为恰一行摘要（§3.2/§6.2）"


def test_retention_D_chat_over_ttl_dropped(thread_dir):
    """§3.2 D 类（chat）距今超过 chat_ttl（默认 10 事件）后不再渲染。

    造 1 条早期 chat，其后再落 >chat_ttl 条事件把它推出窗口 -> 渲染任何段都不含它。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    old_chat = st.append_event(sender="frontend", type="chat", to=["backend"],
                               body="OLD_CHAT_SHOULD_DROP 很久以前的闲聊")
    # 再落 12 条普通事件（> chat_ttl=10），把 old_chat 推到窗口外。
    filler_specs = [
        {"sender": "backend", "type": "report", "to": ["moderator"], "body": f"f{i}"}
        for i in range(12)
    ]
    seed_events(st, filler_specs)
    e_trig = st.append_event(sender="tester", type="defect", to=["backend"],
                             body="锚点")
    cfg = m1_config(chat_ttl=10)

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[e_trig],
        cold_start=True, instruction="x",
    )
    whole = view["text"]
    assert "OLD_CHAT_SHOULD_DROP" not in whole, (
        "D 类 chat 超 chat_ttl 必须被丢弃、不再渲染（§3.2/§6.2）"
    )


def test_retention_D_chat_within_ttl_kept(thread_dir):
    """反向对照：D 类在 chat_ttl 窗口内应仍渲染（背景层），证明 D 判定按窗口而非一律丢。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    recent_chat = st.append_event(sender="frontend", type="chat", to=["backend"],
                                  body="RECENT_CHAT_KEEP 最近闲聊")
    e_trig = st.append_event(sender="tester", type="defect", to=["backend"],
                             body="锚点")
    cfg = m1_config(chat_ttl=10)

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[e_trig],
        cold_start=True, instruction="x",
    )
    assert "RECENT_CHAT_KEEP" in view["text"], "chat_ttl 窗口内的 D 类应仍渲染"


# ==================================================================
# (d) §6.3 预算压缩：超 context_window -> meta.dropped 顺序（背景最旧先丢 -> 焦点最旧截断保首尾）
# ==================================================================

def test_estimate_tokens_is_monotonic(thread_dir):
    """§17/契约 §1：estimate_tokens 是全系统唯一 token 近似；更长文本 token 不更少。"""
    short = orch.render.estimate_tokens("abc")
    longer = orch.render.estimate_tokens("abc" * 1000)
    assert isinstance(short, int) and isinstance(longer, int)
    assert longer > short, "更长文本 token 估算应更大（字符系数法单调）"
    assert orch.render.estimate_tokens("") == 0, "空串 token 估算为 0"


def test_budget_over_window_records_dropped(thread_dir):
    """§6.3：超 context_window 时压缩，meta.dropped 非空，记录压缩动作供断言。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 造大量事件：许多背景件 + 若干 backend 焦点件，正文放大以撑爆小窗口。
    big = "L" * 500
    bg_specs = [
        {"sender": "frontend", "type": "report", "to": ["moderator"],
         "body": f"BG{i} {big}"}
        for i in range(20)
    ]
    seed_events(st, bg_specs)
    focus_specs = [
        {"sender": "tester", "type": "defect", "to": ["backend"],
         "body": f"FOCUS{i} {big}"}
        for i in range(6)
    ]
    fids = seed_events(st, focus_specs)
    # 极小 context_window，强制压缩。
    cfg = m1_config(context_window=200)

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[fids[-1]],
        cold_start=True, instruction="x",
    )
    dropped = view["meta"].get("dropped")
    assert isinstance(dropped, list), "meta.dropped 须为列表（契约 §1/§6.3）"
    assert dropped, "超 context_window 场景 meta.dropped 必非空（发生了压缩）"


def test_budget_compression_order_background_before_focus(thread_dir):
    """§6.3 压缩顺序：**先丢背景最旧摘要 -> 再截断焦点窗最旧事件正文（保首尾各一段）**。

    断言 meta.dropped 中"背景丢弃"动作整体先于"焦点截断"动作出现（顺序即 spec 规定）。
    dropped 元素约定含可判类别的字段（如 {'layer': 'background'|'focus', 'event_id': int, ...}）；
    本测试只依赖"能区分 background 与 focus 两类动作"与"其相对顺序"，不绑定其余字段细节。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    big = "L" * 500
    # 多背景（会被最旧先丢） + 多焦点（其后最旧被截断）。
    seed_events(st, [
        {"sender": "frontend", "type": "report", "to": ["moderator"], "body": f"BG{i} {big}"}
        for i in range(15)
    ])
    fids = seed_events(st, [
        {"sender": "tester", "type": "defect", "to": ["backend"], "body": f"FOCUS{i} {big}"}
        for i in range(8)
    ])
    cfg = m1_config(context_window=300)

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[fids[-1]],
        cold_start=True, instruction="x",
    )
    dropped = view["meta"].get("dropped")
    assert isinstance(dropped, list) and dropped

    def _layer(item) -> str:
        # dropped 元素为 dict 时取 'layer'；为字符串时按包含判断（宽松，避免绑死细节）。
        if isinstance(item, dict):
            return str(item.get("layer", ""))
        s = str(item)
        if "background" in s or "背景" in s:
            return "background"
        if "focus" in s or "焦点" in s:
            return "focus"
        return ""

    layers = [_layer(x) for x in dropped]
    bg_idx = [i for i, L in enumerate(layers) if L == "background"]
    fc_idx = [i for i, L in enumerate(layers) if L == "focus"]

    assert bg_idx, "压缩应先记录丢弃背景摘要动作（§6.3）"
    if fc_idx:
        # 若同时发生焦点截断：所有背景丢弃动作须在所有焦点截断动作之前（§6.3 顺序）。
        assert max(bg_idx) < min(fc_idx), (
            "压缩顺序必须「背景最旧先丢 -> 再截断焦点最旧」（§6.3）"
        )


def test_budget_focus_truncation_keeps_head_and_tail(thread_dir):
    """§6.3 焦点截断"保首尾各一段"：即便发生焦点压缩，最新（尾）焦点事件仍应保留。

    断言本轮触发的最新焦点事件（fids[-1]，即指令尾要回应者）其正文标记仍在焦点段——
    "保尾"是 §6.3 明文（保首尾各一段），触发件属尾部，绝不能被整段丢弃。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    big = "L" * 500
    seed_events(st, [
        {"sender": "frontend", "type": "report", "to": ["moderator"], "body": f"BG{i} {big}"}
        for i in range(15)
    ])
    fids = seed_events(st, [
        {"sender": "tester", "type": "defect", "to": ["backend"],
         "body": f"FOCUS_MARK_{i} {big}"}
        for i in range(8)
    ])
    cfg = m1_config(context_window=300)

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[fids[-1]],
        cold_start=True, instruction="x",
    )
    focus = view["sections"]["focus"]
    # 触发件（尾）必须保留其事件号（§6.3 保首尾）。
    assert f"#{fids[-1]}" in focus, "焦点截断须保尾：最新触发焦点事件不得被整段丢弃（§6.3）"


# ==================================================================
# (d') §6.3 分层配比约束（配额为地板，即便总量未超窗口也强制）
#      焦点窗 ≥ 50% window / 黑板 ≤ 20% window / 背景 ≤ 20% window（Lead 裁决口径）
# ==================================================================

def test_budget_background_ratio_enforced_even_when_total_fits(thread_dir):
    """§6.3 分配约束：背景层 ≤ 20% window，是**地板配额**——即便 fixed+focus+bg
    总量不超 window，超 20% 配额的背景层也必须被压缩至 ≤20%（Lead 裁决）。

    构造：小背景件很多（撑过 20% 配额），但正文短、焦点很小，使
    fixed+focus+bg 仍 < window（旧的"仅总量比较"压缩不会触发）。断言：
      - 背景段 token ≤ 20% window（配比被强制）；
      - meta.dropped 含 background 类丢弃（反映裁剪）；
      - 焦点触发件仍在焦点段（焦点保底不被误伤）。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 40 条短背景件：每条 body 约 40 ASCII 字符 -> 背景段整体远超 20% 小窗配额，
    # 但总量（fixed+focus+bg）仍应低于 window（下方 window=4000 足够宽）。
    bg_specs = [
        {"sender": "frontend", "type": "report", "to": ["moderator"],
         "body": f"BGRATIO{i:02d} short background summary line here"}
        for i in range(40)
    ]
    seed_events(st, bg_specs)
    # 单个小焦点触发件（焦点极小，不至于把总量顶到超窗）。
    fid = st.append_event(sender="tester", type="defect", to=["backend"],
                          body="small focus body")
    # window 取中等大小：20% = 800 token 配额；40 条背景约 > 800 token 需被裁到 ≤800，
    # 但 fixed(系统层含 prompt) + 焦点 + 背景 全量估计 < 4000（不触发旧总量压缩）。
    cfg = m1_config(context_window=4000)

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[fid],
        cold_start=True, instruction="x",
    )
    budget = view["meta"]["budget"]["context_window"]
    bg_quota = int(budget * 0.20)
    bg_tokens = orch.render.estimate_tokens(view["sections"]["background"])

    assert bg_tokens <= bg_quota, (
        f"背景层须被强制压缩至 ≤20% window 配额（§6.3）："
        f"bg_tokens={bg_tokens} > quota={bg_quota}"
    )
    dropped = view["meta"].get("dropped")
    assert isinstance(dropped, list) and dropped, (
        "背景超配额时 meta.dropped 须记录背景裁剪（§6.3）"
    )
    assert any(
        (isinstance(d, dict) and d.get("layer") == "background")
        for d in dropped
    ), "meta.dropped 须含 background 类丢弃（配比裁剪反映其中）"
    # 焦点保底：触发件不得被误删。
    assert f"#{fid}" in view["sections"]["focus"], "焦点触发件须保留（焦点≥50%保底不误伤）"


def test_budget_background_ratio_drops_oldest_first(thread_dir):
    """§6.3 配比裁剪仍按压缩顺序：背景超配额时**丢最旧摘要**（oldest first）。

    背景件按事件号升序即时间序；裁到 ≤20% 后，留下的应是较新的背景件，
    最旧的若干件被丢。断言最旧背景件不在背景段、最新背景件仍在。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    bg_ids = seed_events(st, [
        {"sender": "frontend", "type": "report", "to": ["moderator"],
         "body": f"BGORDER{i:02d} background summary content padding here"}
        for i in range(40)
    ])
    fid = st.append_event(sender="tester", type="defect", to=["backend"],
                          body="focus anchor body")
    cfg = m1_config(context_window=4000)

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[fid],
        cold_start=True, instruction="x",
    )
    background = view["sections"]["background"]
    dropped = view["meta"].get("dropped")
    bg_dropped_ids = [
        d.get("event_id") for d in dropped
        if isinstance(d, dict) and d.get("layer") == "background"
    ]
    assert bg_dropped_ids, "应发生背景配比裁剪（§6.3）"
    # 被丢的应是最旧的一段（升序前缀）；最新背景件应仍在背景段。
    # 用 "#{id} [" 作精确边界，避免 "#1" 误配 "#10/#11"（背景行格式 '#id [from->@to] ...'）。
    assert f"#{bg_ids[0]} [" not in background, "最旧背景件应先被丢（oldest first，§6.3）"
    assert bg_ids[0] in bg_dropped_ids, "最旧背景件应记入 dropped(background)"
    assert f"#{bg_ids[-1]} [" in background, "最新背景件应保留（未超配额时不丢新件）"


def test_budget_blackboard_ratio_tail_truncated_keeps_head(thread_dir):
    """§6.3 分配约束：黑板层 ≤ 20% window。超配额时**尾部截断、保留头部结构**
    （Lead 裁决），且总量仍未超窗时也强制（地板配额）。

    构造：一个很长的 board.md（远超 20% 小窗配额），焦点/背景极小，
    使总量本不超窗。断言黑板段被截到 ≤20%、头部结构（首行）仍在、
    meta.dropped 记录 blackboard 裁剪。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 直接写一份很长的 board.md（黑板层 = board.md 全文，§6.1）。
    board_dir = Path(st.thread_dir) / "blackboard"
    board_dir.mkdir(parents=True, exist_ok=True)
    head_line = "# BOARD_HEAD_STRUCTURE 冻结契约头部"
    tail_marker = "BOARD_TAIL_SHOULD_TRUNCATE"
    long_board = head_line + "\n" + "\n".join(
        f"- 决策条目 {i:03d} 填充内容占位以撑过黑板配额上限 padding line"
        for i in range(120)
    ) + "\n" + tail_marker
    (board_dir / "board.md").write_text(long_board, encoding="utf-8")

    fid = st.append_event(sender="tester", type="defect", to=["backend"],
                          body="small focus")
    cfg = m1_config(context_window=2000)

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[fid],
        cold_start=True, instruction="x",
    )
    blackboard = view["sections"]["blackboard"]
    budget = view["meta"]["budget"]["context_window"]
    board_quota = int(budget * 0.20)
    board_tokens = orch.render.estimate_tokens(blackboard)

    assert board_tokens <= board_quota, (
        f"黑板层须被截至 ≤20% window 配额（§6.3）："
        f"board_tokens={board_tokens} > quota={board_quota}"
    )
    # 保留头部结构（Lead 裁决：尾部截断、保头部）。
    assert "BOARD_HEAD_STRUCTURE" in blackboard, "黑板截断须保留头部结构（§6.3 裁决）"
    assert tail_marker not in blackboard, "黑板超配额尾部内容应被截断（§6.3 裁决）"
    dropped = view["meta"].get("dropped")
    assert any(
        (isinstance(d, dict) and d.get("layer") == "blackboard")
        for d in dropped
    ), "meta.dropped 须记录 blackboard 裁剪（§6.3）"


# ==================================================================
# R-T4 · §13 背景层压缩比采集点：render_view.meta 携带 orig/summarized token
# ==================================================================

def test_render_view_meta_carries_background_compression_tokens(thread_dir):
    """§13 采集点3（R-T4）：render_view.meta 必须携带 bg_orig_tokens /
    bg_summarized_tokens——供调度层派发后 record_metric（render 不持 store，§2）。

    背景不超配额场景：两值应相等（未触发压缩），且 = 背景段 token 估算，可复算。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 少量背景件（不超 20% 配额，不触发压缩）：一条 report（C 类 → 背景层）。
    st.append_event(sender="frontend", type="report", to=["moderator"],
                    body="一条进背景层的进度报告")
    fid = st.append_event(sender="tester", type="defect", to=["backend"],
                          body="焦点触发件")
    cfg = m1_config(context_window=100000)

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[fid],
        cold_start=True, instruction="x",
    )
    meta = view["meta"]
    assert "bg_orig_tokens" in meta and "bg_summarized_tokens" in meta, (
        "meta 必须携带 §13 背景压缩比采集点 bg_orig_tokens / bg_summarized_tokens"
    )
    orig = meta["bg_orig_tokens"]
    summ = meta["bg_summarized_tokens"]
    assert isinstance(orig, int) and isinstance(summ, int)
    # 未触发压缩 → 摘要 token == 原文 token；且 = 最终背景段 estimate_tokens（可复算）。
    assert summ == orig, f"未压缩时 orig==summarized，实测 orig={orig} summ={summ}"
    assert summ == orch.render.estimate_tokens(view["sections"]["background"]), (
        "bg_summarized_tokens 必须 = 最终背景段 estimate_tokens（可复算对照）"
    )


def test_render_view_meta_compression_ratio_shrinks_when_over_quota(thread_dir):
    """§13 采集点3：背景超配额被压缩时，bg_summarized_tokens < bg_orig_tokens
    （压缩比 = summarized/orig < 1，反映真实压缩，非恒等）。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 大量背景件撑过 20% 小窗配额，强制压缩。
    for i in range(40):
        st.append_event(sender="frontend", type="report", to=["moderator"],
                        body=f"BGRATIO{i:02d} background summary padding content line here")
    fid = st.append_event(sender="tester", type="defect", to=["backend"],
                          body="focus anchor")
    cfg = m1_config(context_window=4000)

    view = orch.render.render_view(
        st, cfg, role="backend", event_ids=[fid],
        cold_start=True, instruction="x",
    )
    meta = view["meta"]
    orig = meta["bg_orig_tokens"]
    summ = meta["bg_summarized_tokens"]
    assert orig > 0, "本场景背景层非空"
    assert summ < orig, (
        f"背景超配额被压缩 → summarized({summ}) < orig({orig})（压缩比 < 1）"
    )
