"""CLI 语法统一（审视 P2 → 实为 spec §12 回归）——测试先行，见红。

spec §12 命令表（宪法）：
  `orch replay t-001`      —— thread 位置参数（现实现 --thread 偏离）
  `orch metrics [t-001]`   —— thread 可选位置参数（现实现 --thread 偏离）
  `orch approve|reject <corr>` —— 只有 corr；thread 应缺省自动定位（唯一命中），
                               多线程撞 corr 时才需 --thread 消歧
统一语法规则（记 IMPLEMENTATION_NOTES）：必需目标=位置参数；可选过滤/消歧=选项。
旧 --thread 写法保留为兼容别名（不破坏既有脚本）。
"""

from __future__ import annotations

import orch.adapters
import orch.cli
import orch.scheduler
import orch.store

from tests.fixtures.m1_helpers import m1_config


def _runner():
    from typer.testing import CliRunner
    return CliRunner()


def _mk_thread(ws, tid: str) -> None:
    """在 ws 下建一个含 E1 的最小线程目录。"""
    st = orch.store.Store(ws / tid)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="占位任务", to=["pm"])


def _mk_informal_gate_thread(ws, tid: str, tmp_dir) -> None:
    """建一个经真实调度挂起的非正式门禁线程（E2 handoff→human，corr=gate-2）。"""
    st = orch.store.Store(ws / tid)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="做完请示我", to=["pm"])
    adapters = {
        "pm": orch.adapters.MockAdapter(
            role="pm",
            script={1: {"to": ["human"], "type": "handoff", "body": "请确认"}},
            ledger_path=tmp_dir / f"ledger-{tid}.txt",
        ),
    }
    orch.scheduler.run_thread(st, {**m1_config(), "gate_ops": {}}, adapters)
    assert st.get_meta("status") == "suspended"


# ==================================================================
# replay：spec §12 位置参数 + 旧 --thread 兼容
# ==================================================================

def test_replay_thread_positional_per_spec(tmp_dir):
    ws = tmp_dir / "ws"
    ws.mkdir()
    _mk_thread(ws, "t-repl01")
    r = _runner().invoke(orch.cli.app, ["replay", "t-repl01", "--workspace", str(ws)])
    assert r.exit_code == 0, f"spec §12 `orch replay t-001` 应可用；实际：{r.output!r}"
    assert "#1" in r.output


def test_replay_thread_option_back_compat(tmp_dir):
    ws = tmp_dir / "ws"
    ws.mkdir()
    _mk_thread(ws, "t-repl02")
    r = _runner().invoke(orch.cli.app, ["replay", "--thread", "t-repl02", "--workspace", str(ws)])
    assert r.exit_code == 0, f"旧 --thread 写法应保留兼容；实际：{r.output!r}"


def test_replay_no_thread_clean_error(tmp_dir):
    ws = tmp_dir / "ws"
    ws.mkdir()
    r = _runner().invoke(orch.cli.app, ["replay", "--workspace", str(ws)])
    assert r.exit_code == 1
    assert "Traceback" not in r.output


# ==================================================================
# metrics：spec §12 可选位置参数
# ==================================================================

def test_metrics_optional_thread_positional_per_spec(tmp_dir):
    ws = tmp_dir / "ws"
    ws.mkdir()
    _mk_thread(ws, "t-metr01")
    r = _runner().invoke(orch.cli.app, ["metrics", "t-metr01", "--workspace", str(ws)])
    assert r.exit_code == 0, f"spec §12 `orch metrics [t-001]` 应可用；实际：{r.output!r}"


# ==================================================================
# approve：spec §12 只带 corr——单线程唯一命中自动定位；撞车才要 --thread
# ==================================================================

def test_approve_corr_only_auto_resolves_unique_thread(tmp_dir):
    ws = tmp_dir / "ws"
    ws.mkdir()
    _mk_informal_gate_thread(ws, "t-gate0a", tmp_dir)

    r = _runner().invoke(orch.cli.app, ["approve", "gate-2", "--workspace", str(ws)])
    assert r.exit_code == 0, \
        f"spec §12 `orch approve <corr>` 唯一命中应自动定位线程；实际：{r.output!r}"
    st = orch.store.Store(ws / "t-gate0a")
    assert st.get_meta("status") == "running", "approve 后应 resume"


def test_approve_corr_only_ambiguous_needs_thread(tmp_dir):
    ws = tmp_dir / "ws"
    ws.mkdir()
    _mk_informal_gate_thread(ws, "t-gate1a", tmp_dir)
    _mk_informal_gate_thread(ws, "t-gate1b", tmp_dir)

    r = _runner().invoke(orch.cli.app, ["approve", "gate-2", "--workspace", str(ws)])
    assert r.exit_code == 1, f"corr 撞车应报错而非乱选；实际：{r.output!r}"
    assert "--thread" in r.output, "错误信息应引导使用 --thread 消歧"
    assert "Traceback" not in r.output
    # 两个线程都不得被误动。
    assert orch.store.Store(ws / "t-gate1a").get_meta("status") == "suspended"
    assert orch.store.Store(ws / "t-gate1b").get_meta("status") == "suspended"
