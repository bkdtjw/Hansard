"""可用性审视快赢五连（docs/usability-review-20260706.md §五）——测试先行，见红。

覆盖四个行为项（第五项 USAGE.md 为文档，无测试）：
  Q1 run 过程日志：核心环经 logging("orch.run") 发派发/挂起进度（P1 可观测性）。
  Q2 入口强制 UTF-8：_force_utf8_stdio 把 GBK 流重配为 UTF-8；无 reconfigure 的
     流（测试替身/StringIO）静默容忍（P1 乱码根治）。
  Q3 无 config 静默 Fake：run --once 对无 adapters 配置的工作区必须显式警告
     "Fake"（P1 防"假跑"误导）。
  Q4 CLI 错误一行化：approve 拼错 corr 应得到一行人话 + 退出码 1，
     不得向用户喷 Traceback（P2）。

硬约束：顶层只 import 包；具体符号在函数体内引用（未实现 → 运行时红）。
"""

from __future__ import annotations

import io
import logging
import sys

import orch.adapters
import orch.cli
import orch.scheduler
import orch.store

from tests.fixtures.m1_helpers import m1_config


def _runner():
    from typer.testing import CliRunner
    return CliRunner()


# ==================================================================
# Q2 入口强制 UTF-8
# ==================================================================

def test_force_utf8_reconfigures_gbk_stream(monkeypatch):
    """GBK 编码的 stdout/stderr 应被重配为 utf-8。"""
    import orch.cli.main as clim
    gbk_out = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    gbk_err = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    monkeypatch.setattr(sys, "stdout", gbk_out)
    monkeypatch.setattr(sys, "stderr", gbk_err)
    clim._force_utf8_stdio()
    assert gbk_out.encoding.lower().replace("-", "") == "utf8"
    assert gbk_err.encoding.lower().replace("-", "") == "utf8"


def test_force_utf8_tolerates_streams_without_reconfigure(monkeypatch):
    """StringIO 等无 reconfigure 的流：静默跳过，不抛异常（CliRunner 场景）。"""
    import orch.cli.main as clim
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    clim._force_utf8_stdio()   # 不应 raise


# ==================================================================
# Q1 run 过程日志（核心环 logging("orch.run")）
# ==================================================================

def test_run_thread_emits_dispatch_and_gate_logs(thread_dir, tmp_dir, caplog):
    """派发一组 + 挂起一次，'orch.run' logger 应有对应进度记录。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="做完请示我", to=["pm"])
    adapters = {
        "pm": orch.adapters.MockAdapter(
            role="pm",
            script={1: {"to": ["human"], "type": "handoff", "body": "完工请确认"}},
            ledger_path=tmp_dir / "ledger.txt",
        ),
    }
    cfg = {**m1_config(), "gate_ops": {}}
    with caplog.at_level(logging.INFO, logger="orch.run"):
        orch.scheduler.run_thread(st, cfg, adapters)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "pm" in text and "派发" in text, f"应有派发进度日志；实际：{text!r}"
    assert "挂起" in text, f"target=human 挂起应有日志；实际：{text!r}"


# ==================================================================
# Q3 无 config 的 run --once 必须显式警告 Fake
# ==================================================================

def test_run_once_warns_fake_when_no_adapters_config(tmp_dir):
    ws = tmp_dir / "ws"
    ws.mkdir()
    r_new = _runner().invoke(orch.cli.app, [
        "new", "试一下", "--roles", "pm,moderator", "--workspace", str(ws),
    ])
    assert r_new.exit_code == 0, r_new.output
    r_run = _runner().invoke(orch.cli.app, [
        "run", "--once", "--workspace", str(ws),
    ])
    assert r_run.exit_code == 0, r_run.output
    assert "Fake" in r_run.output, \
        f"无 adapters 配置时必须显式警告使用 Fake 演示适配器；实际输出：{r_run.output!r}"


# ==================================================================
# Q4 approve 错误 corr → 一行人话 + exit 1，无 Traceback
# ==================================================================

def test_approve_bad_corr_clean_one_line_error(tmp_dir):
    ws = tmp_dir / "ws"
    ws.mkdir()
    r_new = _runner().invoke(orch.cli.app, [
        "new", "占位任务", "--roles", "pm,moderator",
        "--thread", "t-uxcheck1", "--workspace", str(ws),
    ])
    assert r_new.exit_code == 0, r_new.output

    r = _runner().invoke(orch.cli.app, [
        "approve", "gate-99", "--thread", "t-uxcheck1", "--workspace", str(ws),
    ])
    assert r.exit_code == 1, f"应以退出码 1 结束；实际 {r.exit_code}，输出：{r.output!r}"
    assert "未找到" in r.output, f"应输出一行人话错误；实际：{r.output!r}"
    assert "Traceback" not in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit), \
        f"不得向用户抛裸异常；实际 {type(r.exception)}"
