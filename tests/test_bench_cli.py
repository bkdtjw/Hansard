"""M3-T1 · §12 `orch bench resume` CLI 验收测试——测试先行，见红。

覆盖任务卡 (e)：
  CliRunner 跑 --fixture <name> --runs 3 --no-resume 与 --with-resume，
  输出 tokens_in 均值差；断言输出含"tokens saved %"或类似百分比段。

硬约束（CLAUDE.md / M3 契约 §4）：
  - 顶层只 `import orch.cli`；`bench` 命令通过 CliRunner 触发（未实现则命令查找失败→红）。
  - 不启子进程（不真跑外部 CLI/API）；bench 内部用 pytest fixture 生成简化任务
    （M3 契约 §5：bench resume 用 pytest fixture 而非附录B）。
  - --runs 至少支持 3 次；开/关 resume 分别跑 → 汇总 tokens_in 均值差 + 百分比。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import orch.cli  # 包级导入（bench 子命令在函数体内触发）


def _runner():
    from typer.testing import CliRunner
    return CliRunner()


def _app():
    return orch.cli.app


# ==================================================================
# (e-1) `orch bench resume --help` 命令必须存在
# ==================================================================

def test_orch_bench_resume_help_lists_flags(tmp_dir):
    """§12：bench resume 子命令应在 typer app 中注册且支持核心 flag。"""
    r = _runner().invoke(_app(), ["bench", "resume", "--help"])
    assert r.exit_code == 0, r.output
    # 至少要能识别核心参数（--fixture / --runs 与 resume 开关）。
    text = r.output
    assert "--fixture" in text
    assert "--runs" in text
    # resume 开关（--with-resume / --no-resume 或类似语义）：至少支持二者之一。
    assert ("--with-resume" in text) or ("--no-resume" in text)


# ==================================================================
# (e-2) `orch bench resume --fixture X --runs 3` 输出含 tokens_in 均值差 + 百分比段
# ==================================================================

def test_orch_bench_resume_outputs_tokens_saved_percentage(tmp_dir):
    """§12/§13：bench resume 应产出开/关 resume 的 tokens_in 均值差与百分比。

    合规产出至少含"tokens"字样 + 百分比段（'%' 或 'percent'）。fixture 名称属 §17
    开放决策（M3 契约 §5 用 pytest fixture 简化任务）；本测试用一个稳定别名 'like'。
    """
    ws = tmp_dir / "ws"
    ws.mkdir()

    r = _runner().invoke(_app(), [
        "bench", "resume",
        "--fixture", "like",
        "--runs", "3",
        "--workspace", str(ws),
    ])
    assert r.exit_code == 0, r.output
    out = r.output.lower()
    # 至少含 "tokens" 字样 + 百分比段（"%" 或 "percent"）。
    assert "tokens" in out, f"§13：bench resume 应输出 tokens_in 均值差；实测:\n{r.output}"
    assert ("%" in r.output) or ("percent" in out) or ("saved" in out), \
        f"§13：bench resume 应输出 tokens saved 百分比段；实测:\n{r.output}"


# ==================================================================
# (e-3) --no-resume / --with-resume 两条路径各能独立触发
# ==================================================================

def test_orch_bench_resume_no_resume_and_with_resume_paths(tmp_dir):
    """两条路径各能独立触发（不依赖对方），--runs 支持 3 次以上。"""
    ws = tmp_dir / "ws"
    ws.mkdir()

    r_no = _runner().invoke(_app(), [
        "bench", "resume",
        "--fixture", "like",
        "--runs", "3",
        "--no-resume",
        "--workspace", str(ws),
    ])
    assert r_no.exit_code == 0, r_no.output

    r_with = _runner().invoke(_app(), [
        "bench", "resume",
        "--fixture", "like",
        "--runs", "3",
        "--with-resume",
        "--workspace", str(ws),
    ])
    assert r_with.exit_code == 0, r_with.output
