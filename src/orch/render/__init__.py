"""视图组装（spec §6）——不对称四层视图渲染，属**调度层**职责、与厂商无关。

分层铁律（§2/§16.6）：视图组装只做"事件流 → 五段文本"的调度层裁剪与第三人称化，
**不含**任何厂商原生格式转换（那是适配层 §7.6 的输入翻译）。本模块只依赖
orch.store 的公开读取接口（events / thread_dir 下 blackboard/board.md）与 config
的公开结构（docs/m1-contract.md §5），不反向依赖调度器或适配器。

对外符号（docs/m1-contract.md §1 冻结）：
  render_view / render_event_third_person / estimate_tokens
  以及 RenderedView（TypedDict，供类型标注）。

M1 有意退化（记 IMPLEMENTATION_NOTES.md）：
  1. token 估算 = 字符系数近似（§17），estimate_tokens 是全系统唯一实现。
  2. §6.4 worktree 现场段对 mock 角色 no-op（真实 CLI 属 M2）；分支保留。
  3. 热续增量（§6.5）不实现：render 恒走冷启动全量（M3）；但指令尾热续必发。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TypedDict

import orch.protocol as _protocol

# ——————————————————————————————————————————————————————————————
# 常量：五段顺序、保留策略、渲染格式片段
# ——————————————————————————————————————————————————————————————

# §6.1 五段固定顺序（位置效应：两端强、中间弱）。
_SECTION_ORDER = ("system", "blackboard", "background", "focus", "instruction")

# §3.2 保留策略表（type -> "A"/"B"/"C"/"D"）；权威来源 orch.protocol.TYPE_RETENTION，
# 缺失时退化到本地拷贝（防御，避免 render 因协议层未导出而整体不可用）。
_LOCAL_RETENTION = {
    "assign": "B", "review": "B", "question": "B", "answer": "B",
    "decision": "A", "handoff": "B", "report": "C", "defect": "B",
    "acceptance": "A", "gate_request": "A", "gate_decision": "A",
    "system": "C", "terminate": "A", "chat": "D",
}

# §6.3 预算分配约束：焦点窗 ≥ 50%、黑板 ≤ 20%、背景 ≤ 20%，其余归系统层 + 指令尾。
_FOCUS_MIN_RATIO = 0.50
_BLACKBOARD_MAX_RATIO = 0.20
_BACKGROUND_MAX_RATIO = 0.20

# thread_defaults 默认（§11.1 子集；config 未给时兜底）。
_DEFAULT_CHAT_TTL = 10

# §17 token 字符系数：ASCII 约 4 字符/词元；CJK/全角每字约 1 词元（更贵）。
_ASCII_PER_TOKEN = 4.0
_CJK_WEIGHT = 1.0


# ——————————————————————————————————————————————————————————————
# RenderedView（契约 §1）
# ——————————————————————————————————————————————————————————————

class RenderedView(TypedDict):
    role: str
    event_ids: list[int]      # 本轮要回应的事件号（升序）
    text: str                 # 五段拼接的完整视图文本
    sections: dict            # {system,blackboard,background,focus,instruction}
    meta: dict                # {token_est,budget,dropped,...}


# ——————————————————————————————————————————————————————————————
# §17：token 估算（全系统唯一实现）
# ——————————————————————————————————————————————————————————————

def estimate_tokens(text: str) -> int:
    """字符系数近似（§17）。空串 → 0；单调不减；CJK 计更重。

    近似：token ≈ Σ(ASCII 字符 / 4 + CJK/全角字符 * 1)，向上取整。
    仅用于**相对**预算裁剪，非精确计费；全系统统一走此实现即可。
    """
    if not text:
        return 0
    ascii_units = 0.0
    cjk_units = 0.0
    for ch in text:
        if ord(ch) < 128:
            ascii_units += 1.0
        else:
            cjk_units += 1.0
    total = ascii_units / _ASCII_PER_TOKEN + cjk_units * _CJK_WEIGHT
    return int(math.ceil(total))


# ——————————————————————————————————————————————————————————————
# §6.2：单事件第三人称渲染
# ——————————————————————————————————————————————————————————————

def _summarize(body: str, *, limit: int = 200) -> str:
    """正文摘要：折叠换行为单空格 + 截断（背景一行 / 焦点摘要复用）。"""
    one_line = " ".join((body or "").split())
    if len(one_line) > limit:
        return one_line[:limit] + "…"
    return one_line


def _to_labels(event: dict) -> str:
    """把信封 to 渲染成 '@a,@b'（§16.1：只从 to 渲染 @，方向恒为 信封→显示）。

    绝不从 body 反解析 @。to 为空按兜底路由显示 @moderator（与 §4.4(1) 落盘一致）。
    """
    targets = list(event.get("to") or [])
    if not targets:
        targets = ["moderator"]
    return ",".join(f"@{t}" for t in targets)


def render_event_third_person(event: dict, viewer_role: str) -> str:
    """焦点窗单事件第三人称渲染（§6.2）：

        '#12 [tester->@backend] (defect): {body摘要}'

    统一第三人称角色标签（[from->@to]），**禁止**保留第一人称原文流（§16.7）：
    正文只作摘要嵌入标签行，不裸留"我…"式第一人称段落。viewer_role 目前不改变
    单行格式（第三人称对所有观察者一致），保留形参以对齐契约签名与未来扩展。
    """
    eid = event.get("id")
    sender = event.get("from")
    etype = event.get("type")
    summary = _summarize(event.get("body", ""))
    return f"#{eid} [{sender}->{_to_labels(event)}] ({etype}): {summary}"


def _render_event_full(event: dict, viewer_role: str) -> str:
    """焦点窗"全文"渲染：第三人称标签行 + 正文全文（仍以标签前缀，不裸留第一人称）。

    §6.2 焦点窗要求"全文"，但§16.7 要求第三人称标签统辖——故格式为
    '#id [from->@to] (type):' 标签行后接原正文（多行原样保留在标签之下）。
    """
    eid = event.get("id")
    sender = event.get("from")
    etype = event.get("type")
    header = f"#{eid} [{sender}->{_to_labels(event)}] ({etype}):"
    body = event.get("body", "")
    return f"{header} {body}"


# ——————————————————————————————————————————————————————————————
# 保留策略与焦点窗判定（§3.2 / §6.2）
# ——————————————————————————————————————————————————————————————

def _retention_of(etype: str) -> str:
    table = getattr(_protocol, "TYPE_RETENTION", None) or _LOCAL_RETENTION
    return table.get(etype, "B")  # 未知 type 保守按 B 处理（相关性再细判）。


def _role_authored_ids(events: list[dict], role: str) -> set[int]:
    """role 自己发过的事件号集合（供焦点窗 re 相交判定）。"""
    return {ev["id"] for ev in events if ev.get("from") == role}


def _is_focus_relevant(event: dict, role: str, authored: set[int]) -> bool:
    """§6.2 焦点窗判定：(to∋role) ∨ (from==role) ∨ (re∩role的事件≠∅)。"""
    if role in (event.get("to") or []):
        return True
    if event.get("from") == role:
        return True
    re_list = event.get("re") or []
    return any(r in authored for r in re_list)


def _classify(events: list[dict], role: str, chat_ttl: int):
    """把事件流按保留策略 + 相关性分桶（§3.2 A/B/C/D）。

    返回 (focus_events, background_items)：
      focus_events: 需入焦点窗的 B 类事件（相关），升序。
      background_items: 需入背景层的 (event, summary_line) 列表，升序，含
        不相关 B、全部 C、以及在 chat_ttl 窗口内的 D。
    A 类不进事件流（走黑板投影）；D 超 chat_ttl 丢弃。
    """
    authored = _role_authored_ids(events, role)
    total = len(events)
    focus: list[dict] = []
    background: list[tuple[dict, str]] = []

    for pos, ev in enumerate(events):
        pol = _retention_of(ev.get("type", ""))
        if pol == "A":
            # A 类永久投影黑板层，不作为普通事件混入焦点/背景（§3.2）。
            continue
        if pol == "D":
            # D 类：距今（以事件序号距末尾计）超过 chat_ttl 丢弃（§3.2）。
            distance = total - 1 - pos
            if distance >= chat_ttl:
                continue
            background.append((ev, _bg_line(ev)))
            continue
        if pol == "C":
            # C 类一律一行摘要进背景层（§3.2）。
            background.append((ev, _bg_line(ev)))
            continue
        # B 类：相关 → 焦点窗全文；否则背景层一行摘要（§3.2）。
        if _is_focus_relevant(ev, role, authored):
            focus.append(ev)
        else:
            background.append((ev, _bg_line(ev)))

    return focus, background


def _bg_line(event: dict) -> str:
    """背景层一行摘要（§6.2）：'#3 [pm->@backend,@frontend] review: PRD v1 发起评审'。

    与焦点第三人称同源（[from->@to]），但 type 不带括号、正文只作短摘要——
    确保恰占一行（测试断言背景件在背景段为单行）。
    """
    eid = event.get("id")
    sender = event.get("from")
    etype = event.get("type")
    summary = _summarize(event.get("body", ""), limit=120)
    return f"#{eid} [{sender}->{_to_labels(event)}] {etype}: {summary}"


# ——————————————————————————————————————————————————————————————
# 系统层 / 黑板层 / 指令尾 组装（§6.2）
# ——————————————————————————————————————————————————————————————

_ENVELOPE_EXAMPLE = (
    "```json\n"
    "{\"to\": [\"tester\"], \"type\": \"handoff\", \"body\": \"实现完成\","
    " \"artifacts\": [], \"corr\": null, \"blackboard_ops\": null}\n"
    "```"
)


def _read_prompt(config: dict, role: str) -> str:
    """读 config.roles[role].prompt 文件原文（§6.2 身份与职责）。

    文件缺失/不可读时退化为占位标题（不抛错，保证 render 始终产出视图）。
    """
    roles = config.get("roles") or {}
    role_cfg = roles.get(role) or {}
    prompt_path = role_cfg.get("prompt")
    if prompt_path:
        try:
            return Path(prompt_path).read_text(encoding="utf-8")
        except OSError:
            pass
    return f"# 角色 {role}\n（未配置 prompt 文件）"


def _build_system(config: dict, role: str) -> str:
    """系统层（§6.2 冷启动全文）：身份 + 权限申报 + 身份声明 + 输出格式 + 幂等指令。"""
    roles = config.get("roles") or {}
    role_cfg = roles.get(role) or {}
    write_scope = role_cfg.get("write_scope") or []
    tools = role_cfg.get("tools") or []

    identity = _read_prompt(config, role)

    # 权限申报原文（§6.2 逐字）。
    perm = (
        f"可写: {', '.join(write_scope)}；"
        f"可用工具: {', '.join(tools)}；"
        f"越权写入会被系统整体拒收"
    )
    # 身份声明（§6.2 逐字，含角色名）。
    identity_decl = f"以下历史中标注 [{role}] 的发言是你自己说过的话"
    # 输出格式（§6.2）：要求以 json 信封结束 + 最小示例。
    output_fmt = (
        "输出格式：回复必须以一个 ```json 代码块结束，内容为信封对象"
        "（字段 to / type / body / artifacts / corr / blackboard_ops），"
        "其余字段由系统赋值。最小示例：\n" + _ENVELOPE_EXAMPLE
    )
    # 幂等指令（§6.2）。
    idempotent = (
        "输入事件均带 # 编号；若某编号你已处理过，直接重发当次信封，"
        "不要重复执行任何操作。"
    )

    return "\n\n".join([
        "=== 系统层 ===",
        identity.rstrip(),
        perm,
        identity_decl,
        output_fmt,
        idempotent,
    ])


def _read_board(store) -> str:
    """黑板层 = board.md 全文（§6.1）。

    经 store 公开的线程目录读 blackboard/board.md；不存在（尚无 A 类投影）→ 空串。
    只读文件，不触碰 store 私有实现。
    """
    try:
        board_path = Path(store.thread_dir) / "blackboard" / "board.md"
        if board_path.exists():
            return board_path.read_text(encoding="utf-8")
    except OSError:
        pass
    return ""


def _build_blackboard(store) -> str:
    board = _read_board(store)
    if not board.strip():
        return ""
    return "=== 黑板层 ===\n" + board.rstrip()


def _build_instruction(role: str, event_ids: list[int], instruction: str) -> str:
    """指令尾（§6.2）：'你是 {role}。现在只针对 #{ids} 回应：{instruction}'。

    热续时**必须**照发（§6.2），故本函数无条件产出非空文本。
    """
    ids_part = " ".join(f"#{eid}" for eid in event_ids)
    return f"你是 {role}。现在只针对 {ids_part} 回应：{instruction}"


# ——————————————————————————————————————————————————————————————
# §6.3 预算压缩
# ——————————————————————————————————————————————————————————————

def _join_background(bg_items: list[tuple[dict, str]]) -> str:
    if not bg_items:
        return ""
    lines = [line for _, line in bg_items]
    return "=== 背景层 ===\n" + "\n".join(lines)


def _join_focus(focus_rendered: list[str]) -> str:
    if not focus_rendered:
        return ""
    return "=== 焦点窗 ===\n" + "\n\n".join(focus_rendered)


def _truncate_blackboard_to_quota(board_text: str, quota: int) -> str:
    """§6.3 黑板层 ≤20% window：超配额时**尾部截断、保留头部结构**（Lead 裁决）。

    board_text 为已带 "=== 黑板层 ===" 头的黑板段全文；按行自尾向前丢弃，直到
    整段 estimate_tokens ≤ quota，始终保留段头（"=== 黑板层 ==="）与其后首行
    结构。返回截断后的黑板段文本（若本就 ≤quota 原样返回）。
    """
    if not board_text.strip() or estimate_tokens(board_text) <= quota:
        return board_text
    lines = board_text.split("\n")
    # 段头（"=== 黑板层 ==="）恒保留；其后为 board.md 首行=头部结构，亦力保。
    # 至少保留前两行（段头 + 头部结构首行）；自尾逐行丢弃直至 ≤quota。
    keep = list(lines)
    while len(keep) > 2 and estimate_tokens("\n".join(keep)) > quota:
        keep.pop()  # 尾部先截（保头部结构）。
    truncated = "\n".join(keep)
    # 仍超配额（头部两行本身即超）——保底截到段头 + 头部结构首行，不再强截字符
    # （§6.3 语义为"保留头部结构"，宁可略超也不破坏结构可读性；实践中配额远大于两行）。
    return truncated


def _compress(
    *,
    focus_events: list[dict],
    background_items: list[tuple[dict, str]],
    role: str,
    fixed_tokens: int,
    budget: int,
    bg_quota: int,
) -> tuple[list[str], str, list[dict]]:
    """§6.3 分层配比 + 超预算压缩：

    先按**分配约束（地板配额）**强制配比——背景层 ≤ bg_quota（=20% window）：
    即便总量未超窗，超配额的背景层也丢最旧摘要至 ≤ 配额（Lead 裁决）。随后若
    总量仍超预算，继续走既有两阶段总量压缩：先丢背景最旧摘要 → 再截断焦点窗
    最旧事件正文（保首尾各一段）。焦点窗受 ≥50% 保底保护：总量压缩时最后动、
    且保首尾。

    返回 (focus_rendered_lines, background_text, dropped)。dropped 元素为
    {'layer': 'background'|'focus', 'event_id': int, 'order': int}，顺序即压缩发生
    顺序（背景整体先于焦点）——供测试断言。

    fixed_tokens = 系统层 + 黑板层 + 指令尾（不参与本函数压缩）的 token 估算之和。
    budget = context_window 上限。总量压缩目标：fixed + 焦点 + 背景 ≤ budget。
    bg_quota = 背景层配额上限（20% window），配比裁剪的地板。
    """
    dropped: list[dict] = []
    order = 0

    # 焦点渲染缓存：event_id -> 当前渲染文本（可被截断替换）。
    focus_text: dict[int, str] = {
        ev["id"]: _render_event_full(ev, role) for ev in focus_events
    }
    bg = list(background_items)  # 复制，最旧在前。

    def _focus_tokens() -> int:
        return estimate_tokens(_join_focus([focus_text[ev["id"]] for ev in focus_events]))

    def _bg_tokens() -> int:
        return estimate_tokens(_join_background(bg))

    def _over() -> bool:
        return fixed_tokens + _focus_tokens() + _bg_tokens() > budget

    # —— 配比约束（地板）：背景层 ≤ 20% window，超配额丢最旧摘要（oldest first）——
    # 即便总量未超窗也强制（§6.3 分配约束）；与总量压缩同用"最旧先丢"顺序，二者
    # 连续记入 dropped(background)，不破坏"背景整体先于焦点"的既有顺序断言。
    while bg and _bg_tokens() > bg_quota:
        ev, _line = bg.pop(0)          # 最旧在列首。
        dropped.append({"layer": "background", "event_id": ev["id"], "order": order})
        order += 1

    # —— 总量压缩第一阶段：仍超预算则继续丢背景最旧摘要 ——
    while _over() and bg:
        ev, _line = bg.pop(0)
        dropped.append({"layer": "background", "event_id": ev["id"], "order": order})
        order += 1

    # —— 总量压缩第二阶段：截断焦点窗最旧事件正文，保首尾各一段 ——
    # 可截断集合 = 焦点事件去掉首(head)与尾(tail=本轮触发件)，最旧在前。
    if len(focus_events) > 2:
        truncatable = focus_events[1:-1]  # 中间段，最旧在前。
    else:
        truncatable = []  # 只有 ≤2 件时首尾即全部，不截断（§6.3 保首尾各一段）。

    ti = 0
    while _over() and ti < len(truncatable):
        ev = truncatable[ti]
        eid = ev["id"]
        # 截断正文：保正文首尾各一段，中段以省略号替代（仍带第三人称标签头）。
        focus_text[eid] = _truncate_focus_body(ev, role)
        dropped.append({"layer": "focus", "event_id": eid, "order": order})
        order += 1
        ti += 1

    focus_rendered = [focus_text[ev["id"]] for ev in focus_events]
    return focus_rendered, _join_background(bg), dropped


def _truncate_focus_body(event: dict, role: str, *, head: int = 40, tail: int = 40) -> str:
    """截断单个焦点事件正文，保首尾各一段（§6.3），标签头保持第三人称。"""
    eid = event.get("id")
    sender = event.get("from")
    etype = event.get("type")
    header = f"#{eid} [{sender}->{_to_labels(event)}] ({etype}):"
    body = event.get("body", "")
    if len(body) <= head + tail:
        trimmed = body
    else:
        trimmed = f"{body[:head]} …【中段略】… {body[-tail:]}"
    return f"{header} {trimmed}"


# ——————————————————————————————————————————————————————————————
# 顶层：render_view（§6.1-§6.4 四层组装）
# ——————————————————————————————————————————————————————————————

def _context_window(config: dict, role: str) -> int:
    """预算上限 = 该 role 绑定 adapter 的 context_window（§6.3，经 config）。"""
    roles = config.get("roles") or {}
    adapters = config.get("adapters") or {}
    role_cfg = roles.get(role) or {}
    adapter_name = role_cfg.get("adapter")
    adapter_cfg = adapters.get(adapter_name) or {}
    cw = adapter_cfg.get("context_window")
    if isinstance(cw, int) and cw > 0:
        return cw
    return 1_000_000  # 未配置视为极大窗口（不压缩）。


def render_view(
    store,
    config,
    *,
    role: str,
    event_ids: list[int],
    cold_start: bool = True,
    instruction: str = "",
) -> RenderedView:
    """§6.1-§6.4 四层组装（单线程 mock 语境；热续增量 §6.5 是 M3，本函数只走冷启动全量路径）。

    五段固定顺序：system → blackboard → background → focus → instruction。
    只从 to 渲染 @（§3.1/§16.1，方向恒为 信封→显示）；焦点窗第三人称（§16.7）。
    §6.4 worktree 现场段对 mock 角色 no-op（真实 CLI 属 M2）。
    """
    ids = sorted(int(e) for e in (event_ids or []))
    events = store.events()

    thread_defaults = config.get("thread_defaults") or {}
    chat_ttl = int(thread_defaults.get("chat_ttl", _DEFAULT_CHAT_TTL))
    budget = _context_window(config, role)

    # —— 分桶（§3.2）——
    focus_events, background_items = _classify(events, role, chat_ttl)

    # —— 不参与总量压缩的三段（系统层 / 黑板层 / 指令尾）——
    system_text = _build_system(config, role)
    blackboard_text = _build_blackboard(store)
    instruction_text = _build_instruction(role, ids, instruction)

    # §6.4 冷启动附加段（仅 CLI 型 worktree 角色）：mock 角色无 worktree → no-op（M1）。
    #   分支保留：真实 CLI（M2）在此把 git log/status/diff 摘要插在黑板层后。
    # （M1 mock：不追加任何现场段。）

    # —— 分层配比约束（地板配额，§6.3）：焦点 ≥50% / 黑板 ≤20% / 背景 ≤20% window ——
    #   黑板层是"固定段"，其配额强制在此处（尾部截断保头部结构）；背景配额随
    #   焦点/背景一起进 _compress。焦点 ≥50% 保底 = 总量压缩时焦点最后动且保首尾。
    board_quota = int(budget * _BLACKBOARD_MAX_RATIO)
    bg_quota = int(budget * _BACKGROUND_MAX_RATIO)

    blackboard_dropped: list[dict] = []
    if blackboard_text.strip() and estimate_tokens(blackboard_text) > board_quota:
        blackboard_text = _truncate_blackboard_to_quota(blackboard_text, board_quota)
        blackboard_dropped.append({"layer": "blackboard", "event_id": None, "order": 0})

    fixed_tokens = (
        estimate_tokens(system_text)
        + estimate_tokens(blackboard_text)
        + estimate_tokens(instruction_text)
    )

    # —— 预算压缩（§6.3）：配比裁剪（地板）+ 超上限总量压缩 ——
    focus_rendered, background_text, dropped = _compress(
        focus_events=focus_events,
        background_items=background_items,
        role=role,
        fixed_tokens=fixed_tokens,
        budget=budget,
        bg_quota=bg_quota,
    )
    # dropped 顺序：背景（配比+总量，最旧先丢）→ 黑板尾截 → 焦点截断，保持
    # "背景整体先于焦点"的既有断言，黑板裁剪夹在二者之间（既非 background 亦非 focus）。
    if blackboard_dropped:
        bg_count = sum(1 for d in dropped if d["layer"] == "background")
        dropped = (
            dropped[:bg_count] + blackboard_dropped + dropped[bg_count:]
        )
        for i, d in enumerate(dropped):
            d["order"] = i
    focus_text = _join_focus(focus_rendered)

    sections = {
        "system": system_text,
        "blackboard": blackboard_text,
        "background": background_text,
        "focus": focus_text,
        "instruction": instruction_text,
    }

    # —— 五段按固定顺序拼接为完整视图文本 ——
    parts = [sections[name] for name in _SECTION_ORDER if sections[name].strip()]
    text = "\n\n".join(parts)

    token_est = estimate_tokens(text)
    meta = {
        "token_est": token_est,
        "budget": {
            "context_window": budget,
            "fixed_tokens": fixed_tokens,
            "focus_min_ratio": _FOCUS_MIN_RATIO,
            "blackboard_max_ratio": _BLACKBOARD_MAX_RATIO,
            "background_max_ratio": _BACKGROUND_MAX_RATIO,
        },
        "dropped": dropped,
        "cold_start": bool(cold_start),
    }

    return {
        "role": role,
        "event_ids": ids,
        "text": text,
        "sections": sections,
        "meta": meta,
    }


# ——————————————————————————————————————————————————————————————
# §6.5 热续增量：render_delta
# ——————————————————————————————————————————————————————————————

def _render_a_event_diff(event: dict) -> str:
    """把单个 A 类事件（decision/acceptance/gate_decision）渲染为黑板 diff 单行。

    保持第三人称标签 + 事件号，便于会话端定位；正文摘要嵌入。此为"黑板 diff"
    专用（§6.5 规则1），不同于焦点窗全文——它是"决策覆盖"的紧凑增量。
    """
    eid = event.get("id")
    sender = event.get("from")
    etype = event.get("type")
    summary = _summarize(event.get("body", ""), limit=200)
    return f"#{eid} [{sender}->{_to_labels(event)}] ({etype}): {summary}"


def _contract_versions_from_events(events: list[dict]) -> dict:
    """扫描给定事件列表的 blackboard_ops，返回 {contract_name: latest_version}。

    只识别 op == "freeze_contract"（§3.3）且 type 属于 A 类 bb_op 集合（decision /
    acceptance / gate_decision）——与 store._BB_OP_TYPES 同域，确保只统计"已获权限
    并投影"过的契约版本。同名后到者覆盖，取最新 version。
    """
    _bb_op_types = frozenset({"decision", "acceptance", "gate_decision"})
    versions: dict = {}
    for ev in events:
        if ev.get("type") not in _bb_op_types:
            continue
        ops = ev.get("blackboard_ops") or []
        for op in ops:
            if op.get("op") == "freeze_contract":
                name = op.get("name")
                ver = op.get("version")
                if name is not None and ver is not None:
                    versions[name] = ver
    return versions


def _needs_cold_start_by_version(
    events: list[dict], last_evt: int
) -> bool:
    """§6.5 规则2：契约 version 变更 ≥ 1 → needs_cold_start = True。

    比较 last_evt 前后（含 last_evt 前的全部 vs 全部事件）中每个契约名的最新
    version：任一名的差值 ≥ 1 即触发。新增契约（旧无）视为差值 = new_version，
    只要 ≥ 1 也算变更（与"新冻结重量级契约"语义一致）。
    """
    before = [ev for ev in events if int(ev.get("id", 0)) <= int(last_evt)]
    after_all = events  # 全流（含 before + last_evt 之后新事件）。
    v_before = _contract_versions_from_events(before)
    v_after = _contract_versions_from_events(after_all)
    for name, new_ver in v_after.items():
        old_ver = v_before.get(name, 0)
        try:
            if int(new_ver) - int(old_ver) >= 1:
                return True
        except (TypeError, ValueError):
            # 非数值 version：任何变化都视为需要冷启动（保守）。
            if new_ver != old_ver:
                return True
    return False


def _build_blackboard_diff(events: list[dict], last_evt: int, role: str) -> str:
    """§6.5 规则1：黑板 diff = last_evt 之后的 A 类事件，前缀"以下决策覆盖旧结论："。

    只含 A 类（§3.2：decision / acceptance / gate_decision——保留策略 A）；
    无新 A 类 → 返回空串（避免误报"覆盖"）。
    """
    a_events_after = [
        ev for ev in events
        if int(ev.get("id", 0)) > int(last_evt)
        and _retention_of(ev.get("type", "")) == "A"
    ]
    if not a_events_after:
        return ""
    lines = [_render_a_event_diff(ev) for ev in a_events_after]
    return "=== 黑板层 ===\n以下决策覆盖旧结论：\n" + "\n".join(lines)


def _build_focus_delta(events: list[dict], last_evt: int, role: str) -> str:
    """§6.5 规则3：新事件全文 = event_id > last_evt 的 B 类事件（相关，第三人称、带 # 号）。

    A 类（黑板 diff 段处理）与 C/D 类（背景/丢弃）不进焦点。相关性判定同 §6.2：
    (to∋role) ∨ (from==role) ∨ (re∩role的事件≠∅)——注意 authored 判定用全流，
    确保"role 早期发过的事件被后续 re"仍能命中焦点。
    """
    authored = _role_authored_ids(events, role)
    picks: list[dict] = []
    for ev in events:
        if int(ev.get("id", 0)) <= int(last_evt):
            continue
        pol = _retention_of(ev.get("type", ""))
        if pol != "B":
            continue
        if not _is_focus_relevant(ev, role, authored):
            continue
        picks.append(ev)
    if not picks:
        return ""
    rendered = [_render_event_full(ev, role) for ev in picks]
    return "=== 焦点窗 ===\n" + "\n\n".join(rendered)


def _build_system_delta(role: str) -> str:
    """§6.5 规则4：热续 system 段最小签名——不重新灌 prompt 原文。

    只保留身份声明与幂等指令的最小骨架，避免热续把冷启动全文再刷一遍浪费 token。
    保留段头以便 sections 键值一致（sections['system'] 非空/无空则拼入 text）。
    """
    identity_decl = f"以下历史中标注 [{role}] 的发言是你自己说过的话"
    idempotent = (
        "输入事件均带 # 编号；若某编号你已处理过，直接重发当次信封，"
        "不要重复执行任何操作。"
    )
    return "\n\n".join(["=== 系统层 ===", identity_decl, idempotent])


def render_delta(
    store,
    config,
    *,
    role: str,
    event_ids: list[int],
    last_evt: int,
    instruction: str = "",
) -> RenderedView:
    """§6.5 热续增量视图：

      - system：最小签名（不重灌 prompt 原文）
      - blackboard：只 diff（last_evt 之后 A 类，前缀"以下决策覆盖旧结论："）
      - background：空（热续不重发已消化历史）
      - focus：新事件（id > last_evt 的相关 B 类，第三人称）
      - instruction：指令尾（§6.2 必发，含角色声明 + 事件号 + instruction 原文）

    meta.needs_cold_start：§6.5 规则2——契约 version 变更 ≥ 1 → True（调度层据此作废
    sid 回冷启动）；否则 False（继续热续）。

    本函数**不接入调度**（属 T3 责任）；仅提供签名与语义。签名与 docs/m3-contract.md §2 一致。
    """
    ids = sorted(int(e) for e in (event_ids or []))
    events = store.events()
    last_evt_i = int(last_evt)

    system_text = _build_system_delta(role)
    blackboard_text = _build_blackboard_diff(events, last_evt_i, role)
    background_text = ""  # §6.5：热续不再灌背景层。
    focus_text = _build_focus_delta(events, last_evt_i, role)
    instruction_text = _build_instruction(role, ids, instruction)

    sections = {
        "system": system_text,
        "blackboard": blackboard_text,
        "background": background_text,
        "focus": focus_text,
        "instruction": instruction_text,
    }

    parts = [sections[name] for name in _SECTION_ORDER if sections[name].strip()]
    text = "\n\n".join(parts)

    needs_cold_start = _needs_cold_start_by_version(events, last_evt_i)

    meta = {
        "token_est": estimate_tokens(text),
        "cold_start": False,
        "needs_cold_start": needs_cold_start,
        "last_evt": last_evt_i,
    }

    return {
        "role": role,
        "event_ids": ids,
        "text": text,
        "sections": sections,
        "meta": meta,
    }
