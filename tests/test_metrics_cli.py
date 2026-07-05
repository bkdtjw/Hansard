"""M4-T1 · `orch metrics` CLI 测试（先行见红）。

覆盖任务卡条目 (c)：
  - `orch metrics --workspace <ws>`：输出必须**至少字段名出现**覆盖 §13 全表：
      · 任务数 / 平均轮数 / 成本
      · 聚合节省 %
      · 首次合法率 %
      · 背景层压缩比
      · resume 输入 token 节省 %
      · 混沌轮数（两层结果）
      · 新增供应商 adapter 行数
  - 断言不采信具体数值（可为 0 / N/A / 未采集）；只查字段名。

约束（CLAUDE.md / M4 契约 §3）：
  - 顶层只 `import orch.cli`；`metrics` 命令通过 CliRunner 触发（未实现 → command 找不到 → 红）。
  - 不依赖 workspace 里有实际数据；空 workspace 也必须能输出 §13 全表字段名
    （数字可显示为 0 / N/A / 未采集）。
"""

from __future__ import annotations

import orch.cli  # noqa: F401


def _runner():
    from typer.testing import CliRunner
    return CliRunner()


def _app():
    return orch.cli.app


# ==================================================================
# (c-1) `orch metrics --help` 存在且识别 --workspace flag
# ==================================================================

def test_orch_metrics_help_lists_workspace_flag():
    r = _runner().invoke(_app(), ["metrics", "--help"])
    assert r.exit_code == 0, r.output
    assert "--workspace" in r.output, "orch metrics 必须支持 --workspace flag（§12）"


# ==================================================================
# (c-2) 空 workspace 也能输出 §13 全表字段名
# ==================================================================

def _assert_field_names_in_output(out: str) -> None:
    """§13 七类指标字段名逐条断言（宽松匹配：多个可能中文/英文表述取 OR）。

    每一条至少一个别名出现即可（保持对具体措辞的容忍，但字段"类别"必须齐全）。
    """
    lo = out.lower()

    def _any(alts: list[str]) -> bool:
        return any(a.lower() in lo for a in alts)

    # 1) 端到端任务数 / 平均轮数 / 成本
    assert _any(["任务数", "tasks", "task count", "任务"]), \
        f"§13 field [tasks] missing:\n{out}"
    assert _any(["平均轮数", "avg rounds", "average rounds", "rounds"]), \
        f"§13 field [rounds] missing:\n{out}"
    assert _any(["成本", "cost", "费用"]), \
        f"§13 field [cost] missing:\n{out}"

    # 2) 聚合节省 %
    assert _any(["聚合节省", "aggregate save", "batch save", "aggregation"]), \
        f"§13 field [aggregate save %] missing:\n{out}"

    # 3) 首次合法率 %
    assert _any(["首次合法率", "first legal", "first-legal", "first valid"]), \
        f"§13 field [first-legal %] missing:\n{out}"

    # 4) 背景层压缩比
    assert _any(["背景", "background"]), \
        f"§13 field [background compression] missing (bg):\n{out}"
    assert _any(["压缩比", "compression", "compress ratio"]), \
        f"§13 field [background compression] missing (ratio):\n{out}"

    # 5) resume 输入 token 节省 %
    assert "resume" in lo, f"§13 field [resume token save %] missing (resume):\n{out}"
    assert _any(["token", "tokens"]), \
        f"§13 field [resume token save %] missing (token):\n{out}"

    # 6) 混沌轮数与两层结果
    assert _any(["混沌", "chaos"]), f"§13 field [chaos rounds] missing:\n{out}"

    # 7) 新增供应商 adapter 行数
    assert "adapter" in lo, f"§13 field [adapter LoC] missing (adapter):\n{out}"
    assert _any(["行数", "loc", "lines", "cloc"]), \
        f"§13 field [adapter LoC] missing (loc):\n{out}"


def test_orch_metrics_empty_workspace_lists_all_section_13_fields(tmp_dir):
    """空 workspace：`orch metrics` 应输出 §13 全表字段名（数字可为 0 / N/A）。"""
    ws = tmp_dir / "ws-empty"
    ws.mkdir()
    r = _runner().invoke(_app(), ["metrics", "--workspace", str(ws)])
    assert r.exit_code == 0, r.output
    _assert_field_names_in_output(r.output)


# ==================================================================
# (c-3) 有一条 thread 的 workspace：仍输出 §13 全表字段名
# ==================================================================

def test_orch_metrics_with_seeded_thread_lists_all_fields(tmp_dir):
    """workspace 内有一个种子 thread 时也应输出 §13 全表字段名。"""
    import orch.store

    ws = tmp_dir / "ws-seed"
    ws.mkdir()

    st = orch.store.Store(ws / "t-abcd1234")
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="任务A", to=["pm"])

    r = _runner().invoke(_app(), ["metrics", "--workspace", str(ws)])
    assert r.exit_code == 0, r.output
    _assert_field_names_in_output(r.output)


# ==================================================================
# (c-4) 数值本身不被断言（占位显示允许）
# ==================================================================

def test_orch_metrics_output_does_not_require_specific_numbers(tmp_dir):
    """断言不采信具体数值：即便某项无采集，输出该字段名与占位（0 / N/A）即绿。"""
    ws = tmp_dir / "ws-x"
    ws.mkdir()
    r = _runner().invoke(_app(), ["metrics", "--workspace", str(ws)])
    assert r.exit_code == 0, r.output
    # 只要不抛异常、字段名全在 —— (c-2) / (c-3) 已覆盖，这里做冗余的字段名可见性再次校验。
    _assert_field_names_in_output(r.output)
