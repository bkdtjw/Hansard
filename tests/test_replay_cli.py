"""M4-T1 · `orch replay` CLI 测试（先行见红）。

覆盖任务卡条目 (d)：
  - `orch replay --thread t-xxx [--workspace ws]`：按事件号升序输出**第三人称群聊 markdown**。
  - 每条渲染必须含 `[from->@to]` 形式的标签（`[<sender>->@<r1>[,@<r2>...]]`），
    即"第三人称"的路由投影（§16.1 只认 to 字段；不从正文解析 @）。
  - 事件号严格升序（§4.4：事件表按 id 升序）。

约束（CLAUDE.md / M4 契约 §4）：
  - 顶层只 `import orch.cli`；命令通过 CliRunner 触发（未实现 → command 找不到 → 红）。
  - 断言含"第三人称标签"这一形状约束——`[<sender>-><@角色列表>]`；措辞其余不锁死。
"""

from __future__ import annotations

import re

import orch.cli  # noqa: F401
import orch.store


def _runner():
    from typer.testing import CliRunner
    return CliRunner()


def _app():
    return orch.cli.app


# ==================================================================
# (d-1) `orch replay --help` 存在且识别 --thread flag
# ==================================================================

def test_orch_replay_help_lists_thread_flag():
    r = _runner().invoke(_app(), ["replay", "--help"])
    assert r.exit_code == 0, r.output
    assert "--thread" in r.output, "orch replay 必须支持 --thread flag（§12）"


# ==================================================================
# (d-2) 空线程：输出 exit=0；至少含线程 id 提示
# ==================================================================

def test_orch_replay_empty_thread_exits_zero(tmp_dir):
    ws = tmp_dir / "ws"
    ws.mkdir()
    # 建一个空线程（仅目录 + events.db，无事件）。
    orch.store.Store(ws / "t-empty000")

    r = _runner().invoke(_app(), [
        "replay", "--thread", "t-empty000", "--workspace", str(ws),
    ])
    assert r.exit_code == 0, r.output
    # 至少提示线程 id（避免空输出误导）。
    assert "t-empty000" in r.output


# ==================================================================
# (d-3) 有事件：按 id 升序 + 每条含 [from->@to] 第三人称标签
# ==================================================================

def _seed_three_events(store: orch.store.Store) -> None:
    """三条事件：human→pm，pm→backend,frontend（多目标），backend→pm（对 E2 的 re）。"""
    e1 = store.append_event(
        sender="human", type="assign", body="做点赞功能", to=["pm"],
    )
    e2 = store.append_event(
        sender="pm", type="review", body="发起评审", to=["backend", "frontend"],
        re=[e1],
    )
    store.append_event(
        sender="backend", type="answer", body="收到，字段已确认",
        to=["pm"], re=[e2],
    )


def test_orch_replay_outputs_third_person_labels_and_id_order(tmp_dir):
    """输出必须：① 事件号升序；② 每条含 [<sender>->@<r1>[,@<r2>...]] 第三人称标签。"""
    ws = tmp_dir / "ws"
    ws.mkdir()
    st = orch.store.Store(ws / "t-repl0001")
    st.set_meta("status", "running")
    _seed_three_events(st)

    r = _runner().invoke(_app(), [
        "replay", "--thread", "t-repl0001", "--workspace", str(ws),
    ])
    assert r.exit_code == 0, r.output
    out = r.output

    # —— 第三人称标签形状：`[<sender>->@<one>[, @<two>...]]` ——
    # 关键要素：`[`、`->@`、`]`；容忍角色间分隔用空格或逗号。
    label_re = re.compile(r"\[[a-zA-Z_][a-zA-Z0-9_]*->\s*@[a-zA-Z_][a-zA-Z0-9_]*")
    labels = label_re.findall(out)
    assert len(labels) >= 3, (
        f"replay 应对每条事件产出第三人称标签 `[from->@to]`，"
        f"实测标签数={len(labels)}：\n{out}"
    )

    # 关键 sender/target 应命中：
    assert re.search(r"\[human->\s*@pm", out), (
        f"E1 应渲染 [human->@pm] 第三人称标签：\n{out}"
    )
    assert re.search(r"\[pm->\s*@backend", out), (
        f"E2 应渲染 [pm->@backend...] 第三人称标签：\n{out}"
    )
    assert re.search(r"\[backend->\s*@pm", out), (
        f"E3 应渲染 [backend->@pm] 第三人称标签：\n{out}"
    )

    # —— 事件号严格升序（找到 E1/E2/E3 或 id 1/2/3 的相对位置）——
    # 采用宽松匹配：优先找 "E{n}" 或 "id={n}" 或 "#{n}"；三者任一均可。
    def _first_pos(pattern: str) -> int:
        m = re.search(pattern, out)
        return -1 if m is None else m.start()

    positions = [
        _first_pos(r"E1\b|id=1\b|#1\b"),
        _first_pos(r"E2\b|id=2\b|#2\b"),
        _first_pos(r"E3\b|id=3\b|#3\b"),
    ]
    assert all(p >= 0 for p in positions), (
        f"每条事件应含事件号标记（E<n> / id=<n> / #<n>）：pos={positions}\n{out}"
    )
    assert positions[0] < positions[1] < positions[2], (
        f"事件必须按 id 升序渲染：pos={positions}\n{out}"
    )


# ==================================================================
# (d-4) 路由只认 to 字段（§16.1）：body 里的 @ 不参与路由标签
# ==================================================================

def test_orch_replay_does_not_parse_at_from_body(tmp_dir):
    """§16.1 硬约束：路由**只**认 to 字段。body 里出现的 @moderator 不得进入
    [from->@to] 标签的 @to 部分。"""
    ws = tmp_dir / "ws"
    ws.mkdir()
    st = orch.store.Store(ws / "t-nobody0")
    st.set_meta("status", "running")
    # body 提到 @moderator 但 to=[pm] —— 标签只能是 [human->@pm]。
    st.append_event(
        sender="human", type="assign",
        body="请 @moderator 关注一下，交给 pm 出方案",
        to=["pm"],
    )

    r = _runner().invoke(_app(), [
        "replay", "--thread", "t-nobody0", "--workspace", str(ws),
    ])
    assert r.exit_code == 0, r.output
    out = r.output

    # 第三人称标签只能命中 @pm；不得出现 [human->@moderator（来自 body 解析）。
    assert re.search(r"\[human->\s*@pm", out), f"应从 to=[pm] 生成标签：\n{out}"
    assert not re.search(r"\[human->\s*@moderator", out), (
        f"§16.1：不得从 body 解析 @moderator 进入路由标签：\n{out}"
    )
