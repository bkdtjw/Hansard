"""② 多工作区单控制台（审视 P2）——测试先行，见红。

设计（QUESTIONS/审视报告 ②）：
  · make_server 接受单个 workspace（现状，向后兼容）或 list（新能力）。
  · 每请求经 `?ws=名字` 选择工作区；缺省 = 第一个（既有前端零参数请求不破坏）。
  · 新端点 GET /api/workspaces → {workspaces: [{name, path}...], default}。
  · 未知 ws 名 → 404（不猜测）。
真起 make_server(port=0) + urllib 打真实 HTTP（沿用 test_web.py 模式）。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import orch.store


def _mk_thread(ws, tid: str) -> None:
    st = orch.store.Store(ws / tid)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body=f"任务@{tid}", to=["pm"])


def _start(workspace):
    from orch.web.server import make_server
    srv = make_server(workspace, "127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_multi_workspace_routing_and_listing(tmp_dir):
    ws_a = tmp_dir / "alpha"
    ws_b = tmp_dir / "beta"
    ws_a.mkdir()
    ws_b.mkdir()
    _mk_thread(ws_a, "t-aaaa0001")
    _mk_thread(ws_b, "t-bbbb0001")
    _mk_thread(ws_b, "t-bbbb0002")

    srv, base = _start([ws_a, ws_b])
    try:
        # /api/workspaces 列出全部 + 缺省名。
        wss = _get(base, "/api/workspaces")
        names = [w["name"] for w in wss["workspaces"]]
        assert names == ["alpha", "beta"], f"应按传入序列出工作区；实际 {names}"
        assert wss["default"] == "alpha"

        # 缺省（无 ?ws=）= 第一个工作区（向后兼容既有前端）。
        assert len(_get(base, "/api/threads")) == 1

        # ?ws= 切换到第二个工作区。
        assert len(_get(base, "/api/threads?ws=beta")) == 2
        assert _get(base, "/api/health?ws=beta")["workspace"].endswith("beta")

        # 线程子路径同样吃 ?ws=。
        evs = _get(base, "/api/threads/t-bbbb0001/events?ws=beta")
        assert len(evs["events"]) == 1

        # 未知工作区名 → 404。
        try:
            _get(base, "/api/threads?ws=nope")
            raise AssertionError("未知 ws 应 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


def test_single_workspace_back_compat(tmp_dir):
    ws = tmp_dir / "solo"
    ws.mkdir()
    _mk_thread(ws, "t-solo0001")

    srv, base = _start(ws)   # 传单个 Path：既有签名不变
    try:
        assert len(_get(base, "/api/threads")) == 1
        wss = _get(base, "/api/workspaces")
        assert len(wss["workspaces"]) == 1 and wss["default"] == "solo"
    finally:
        srv.shutdown()


def test_same_basename_workspaces_deduped(tmp_dir):
    a = tmp_dir / "x" / "ws"
    b = tmp_dir / "y" / "ws"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    srv, base = _start([a, b])
    try:
        names = [w["name"] for w in _get(base, "/api/workspaces")["workspaces"]]
        assert len(set(names)) == 2, f"同名目录应去重命名；实际 {names}"
    finally:
        srv.shutdown()
