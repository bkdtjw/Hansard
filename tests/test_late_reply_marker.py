"""③ 迟到回复标记（审视 P3）——测试先行，见红。

真实案例（fluency 线程 E10）：moderator E8 terminate、E9 终止清单后 16s，
backend 在途回复 E10 落盘。行为符合"日志=真相"+§5.4（只拒新派发），但读者
初见困惑。裁决：纯展示层标记——终止号之后落盘的非 system 事件，replay 行
与控制台卡片加"⏱ 终止后到达"，不改协议/DDL/落盘。
"""

from __future__ import annotations

import orch.cli
import orch.store


def _runner():
    from typer.testing import CliRunner
    return CliRunner()


def _seed_terminated_with_late_reply(ws) -> None:
    st = orch.store.Store(ws / "t-late01")
    st.set_meta("status", "terminated")
    st.append_event(sender="human", type="assign", body="干活", to=["backend"])       # E1
    st.append_event(sender="moderator", type="terminate", body="线程结束", to=["human"])  # E2
    st.append_event(sender="system", type="system", body="线程终止清单：…", to=["moderator"])  # E3
    st.append_event(sender="backend", type="handoff", body="迟到的在途回复", to=["moderator"])  # E4


def test_replay_marks_late_reply_after_terminate(tmp_dir):
    ws = tmp_dir / "ws"
    ws.mkdir()
    _seed_terminated_with_late_reply(ws)

    r = _runner().invoke(orch.cli.app, ["replay", "t-late01", "--workspace", str(ws)])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    late = [ln for ln in lines if ln.startswith("#4 ")]
    early = [ln for ln in lines if ln.startswith("#1 ")]
    sysln = [ln for ln in lines if ln.startswith("#3 ")]
    assert late and "终止后到达" in late[0], f"E4 应带迟到标记；实际：{late}"
    assert early and "终止后到达" not in early[0], "终止前事件不得误标"
    assert sysln and "终止后到达" not in sysln[0], "system 终止清单是正常收尾产物，不标"


def test_replay_no_marker_when_not_terminated(tmp_dir):
    ws = tmp_dir / "ws"
    ws.mkdir()
    st = orch.store.Store(ws / "t-late02")
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="干活", to=["backend"])
    st.append_event(sender="backend", type="handoff", body="正常回复", to=["moderator"])

    r = _runner().invoke(orch.cli.app, ["replay", "t-late02", "--workspace", str(ws)])
    assert r.exit_code == 0, r.output
    assert "终止后到达" not in r.output, "无 terminate 的线程不得出现迟到标记"
