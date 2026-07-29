"""W1 玻璃感 Web 控制台验收测试（spec 之外的补充交付）。

证明"无假按钮"= 每个 REST 端点都有真实副作用：真起 make_server（port=0，
后台线程 serve_forever），用 urllib 打真实 HTTP，逐端点断言磁盘/db 的真变化。
不 mock 任何被测对象——真起 server、真查 sqlite。

临时目录沿用仓库约定的 `tmp_dir` fixture（tests/conftest.py：项目本地 .pytmp/，
用后即清），不用不可写的内置 tmp_path。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import orch.store


# ——————————————————————————————————————————————————————————————
# 起服务辅助：make_server(port=0) + 后台线程；测试结束 shutdown。
# ——————————————————————————————————————————————————————————————

def _make_server(workspace: Path):
    """真起服务（port=0 让 OS 选空闲端口），返回 (srv, base_url, thread)。"""
    from orch.web.server import make_server

    srv = make_server(workspace, "127.0.0.1", 0)
    host, port = srv.server_address[0], srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://{host}:{port}", t


class _Serving:
    """上下文管理器：with _Serving(ws) as (base): ... 自动起停。"""

    def __init__(self, workspace: Path):
        self.workspace = workspace

    def __enter__(self):
        self.srv, self.base, self.thread = _make_server(self.workspace)
        return self.base

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
        return False


def _req(base: str, path: str, method: str = "GET", body: dict | None = None):
    """打一次真实 HTTP，返回 (status_code, parsed_json_or_text)。"""
    url = base + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        code = e.code
    ctype = None
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = raw
    return code, parsed


def _new_thread(base: str, task: str = "点赞功能", roles=None) -> str:
    roles = roles or ["pm", "moderator"]
    code, body = _req(base, "/api/threads", "POST", {"task": task, "roles": roles})
    assert code == 200, (code, body)
    assert "id" in body, body
    return body["id"]


# ——————————————————————————————————————————————————————————————
# (w-0) health + 静态入口
# ——————————————————————————————————————————————————————————————

def test_health_reports_workspace(tmp_dir):
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/health")
        assert code == 200, (code, body)
        assert body.get("ok") is True
        assert Path(body["workspace"]) == tmp_dir.resolve() or body["workspace"].endswith(tmp_dir.name)


def test_root_serves_glass_index(tmp_dir):
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/")
        assert code == 200, code
        # index.html 必须含玻璃感标志性内容（class 或属性名）。
        assert isinstance(body, str)
        assert ("glass" in body) or ("backdrop-filter" in body), "index 应含玻璃感标志"


def test_styles_css_has_glassmorphism(tmp_dir):
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/styles.css")
        assert code == 200, code
        assert "backdrop-filter" in body, "styles.css 应实现磨砂玻璃 backdrop-filter"


def test_app_js_served(tmp_dir):
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/app.js")
        assert code == 200, code
        assert "fetch(" in body, "app.js 应有真实 fetch 调用（非假按钮）"


# ——————————————————————————————————————————————————————————————
# (w-1) POST /api/threads → 磁盘出现 t-*/events.db 且 E1 入队
# ——————————————————————————————————————————————————————————————

def test_create_thread_writes_db_and_enqueues_e1(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, task="点赞功能", roles=["pm", "moderator"])
        # 磁盘真出现 t-*/events.db
        tdir = tmp_dir / tid
        assert (tdir / "events.db").exists(), "应真建 events.db"
        # E1 = human assign 入队
        store = orch.store.Store(tdir)
        evs = store.events()
        assert len(evs) >= 1
        assert evs[0]["from"] == "human"
        assert evs[0]["type"] == "assign"
        assert evs[0]["body"] == "点赞功能"
        # roles meta 落盘
        assert store.get_meta("status") == "running"


def test_threads_list_reflects_created(tmp_dir):
    with _Serving(tmp_dir) as base:
        # 空 workspace → 真实空
        code, body = _req(base, "/api/threads")
        assert code == 200
        assert body == []
        tid = _new_thread(base)
        code, body = _req(base, "/api/threads")
        assert code == 200
        ids = [t["id"] for t in body]
        assert tid in ids
        row = next(t for t in body if t["id"] == tid)
        assert row["status"] == "running"
        assert set(row["roles"]) >= {"pm", "moderator"}


# ——————————————————————————————————————————————————————————————
# (w-2) POST send → events 表新增该事件
# ——————————————————————————————————————————————————————————————

def test_send_appends_event(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        store = orch.store.Store(tmp_dir / tid)
        n0 = len(store.events())
        code, body = _req(
            base, f"/api/threads/{tid}/send", "POST",
            {"to": "pm", "type": "chat", "body": "请开始设计"},
        )
        assert code == 200, (code, body)
        assert "event_id" in body
        store2 = orch.store.Store(tmp_dir / tid)
        evs = store2.events()
        assert len(evs) == n0 + 1
        last = evs[-1]
        assert last["from"] == "human"
        assert last["type"] == "chat"
        assert last["body"] == "请开始设计"
        assert last["to"] == ["pm"]


def test_send_default_to_moderator(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        code, body = _req(
            base, f"/api/threads/{tid}/send", "POST",
            {"body": "无 to 默认 moderator"},
        )
        assert code == 200, (code, body)
        store = orch.store.Store(tmp_dir / tid)
        last = store.events()[-1]
        assert last["to"] == ["moderator"]


def test_events_endpoint_has_third_person(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        code, body = _req(base, f"/api/threads/{tid}/events")
        assert code == 200, (code, body)
        assert "events" in body
        assert len(body["events"]) >= 1
        e0 = body["events"][0]
        for k in ("id", "sender", "type", "to", "body", "third_person"):
            assert k in e0, f"事件投影缺字段 {k}"
        # 第三人称渲染形如 #id [from->@to] (type): ...
        assert e0["third_person"].startswith("#"), e0["third_person"]
        assert "[" in e0["third_person"] and "->" in e0["third_person"]


# ——————————————————————————————————————————————————————————————
# (w-3) POST run{once} → 线程状态推进（默认 Fake adapters → terminated）
# ——————————————————————————————————————————————————————————————

def test_run_once_advances_status(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        code, body = _req(base, f"/api/threads/{tid}/run", "POST", {"once": True})
        assert code == 200, (code, body)
        assert body.get("ran") is True
        # _build_default_adapters 让 moderator terminate → 期望 terminated
        assert body.get("status") == "terminated", body
        # db 落盘也应 terminated
        store = orch.store.Store(tmp_dir / tid)
        assert store.get_meta("status") == "terminated"


# ——————————————————————————————————————————————————————————————
# (w-4) POST reopen → status=running
# ——————————————————————————————————————————————————————————————

def test_reopen_sets_running(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        store = orch.store.Store(tmp_dir / tid)
        store.set_meta("status", "terminated")
        code, body = _req(base, f"/api/threads/{tid}/reopen", "POST", {})
        assert code == 200, (code, body)
        assert body.get("status") == "running"
        store2 = orch.store.Store(tmp_dir / tid)
        assert store2.get_meta("status") == "running"


# ——————————————————————————————————————————————————————————————
# (w-5) POST gate approve → 落 gate_decision + resume（真查 db）
# ——————————————————————————————————————————————————————————————

def _seed_gate_request(store, corr: str, sender: str = "moderator") -> int:
    """种入 gate_request(to=[human], corr) + 手工 gate_wait + suspended（同 CLI 测试口径）。"""
    eid = store.append_event(
        sender=sender, type="gate_request", body="need approval",
        to=["human"], corr=corr,
    )
    store.mark_gate_wait(eid, "human")
    store.set_meta("status", "suspended")
    return eid


def test_gate_approve_records_decision(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        store = orch.store.Store(tmp_dir / tid)
        _seed_gate_request(store, corr="gate-web-1")

        code, body = _req(
            base, "/api/gate", "POST",
            {"thread": tid, "corr": "gate-web-1", "decision": "approve"},
        )
        assert code == 200, (code, body)
        assert body.get("ok") is True

        store2 = orch.store.Store(tmp_dir / tid)
        assert any(
            ev.get("type") == "gate_decision" and ev.get("corr") == "gate-web-1"
            and ev.get("body") == "approve"
            for ev in store2.events()
        ), "approve 应真落 gate_decision(approve) 事件"
        assert store2.get_meta("status") == "running", "应 resume"


def test_gate_reject_records_decision(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        store = orch.store.Store(tmp_dir / tid)
        _seed_gate_request(store, corr="gate-web-2")
        code, body = _req(
            base, "/api/gate", "POST",
            {"thread": tid, "corr": "gate-web-2", "decision": "reject"},
        )
        assert code == 200, (code, body)
        store2 = orch.store.Store(tmp_dir / tid)
        assert any(
            ev.get("type") == "gate_decision" and ev.get("corr") == "gate-web-2"
            and ev.get("body") == "reject"
            for ev in store2.events()
        )


def test_gate_invalid_decision_400(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        code, body = _req(
            base, "/api/gate", "POST",
            {"thread": tid, "corr": "x", "decision": "maybe"},
        )
        assert code == 400, (code, body)
        assert "error" in body


# ——————————————————————————————————————————————————————————————
# (w-6) status / replay / attach 端点
# ——————————————————————————————————————————————————————————————

def test_status_endpoint(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        assert body.get("status") == "running"
        assert "dispatches" in body
        assert isinstance(body["dispatches"], list)


# ——————————————————————————————————————————————————————————————
# (w-6b) F4 数据源复活：/status 的 dispatches 投影全五态 + deadline_ts
# ——————————————————————————————————————————————————————————————

def _dispatch_row(rows: list, event_id: int, target: str) -> dict:
    for r in rows:
        if r["event_id"] == event_id and r["target"] == target:
            return r
    raise AssertionError(f"派发行 (E{event_id}, {target}) 不在投影里：{rows}")


def test_status_dispatches_expose_dispatching_with_deadline(tmp_dir):
    """mark_dispatching 后 /status 必须回该行：status=='dispatching' 且带 deadline_ts。

    旧行为只投影 pending 行（store.pending_dispatches），前端"正在响应"胶囊与角色
    状态点因此是死代码；缺 deadline_ts 则崩溃后滞留的 dispatching 行会长亮假绿
    （src/orch/scheduler/watchdog.py:203-205）。
    """
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        store = orch.store.Store(tmp_dir / tid)
        e1 = store.events()[0]["id"]
        deadline = time.time() + 60
        store.mark_dispatching(e1, "pm", deadline)

        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        row = _dispatch_row(body["dispatches"], e1, "pm")
        assert row["status"] == "dispatching", row
        assert row["deadline_ts"] is not None, row
        assert abs(float(row["deadline_ts"]) - deadline) < 1.0, row
        assert "attempts" in row, row
        # 键名冻结：target 不得改名成 role —— tests/test_m5_availability.py 的
        # _role_projection 按"每项含 role 键的顶层列表"结构探测 roles 投影。
        assert "role" not in row, row


def test_status_dispatches_include_done_rows_and_pending_semantics_intact(tmp_dir):
    """投影是全量快照（含 done）；同时 pending_dispatches() 的"只回 pending"语义不动。"""
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        store = orch.store.Store(tmp_dir / tid)
        e1 = store.events()[0]["id"]
        store.mark_done(e1, "pm")

        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        assert _dispatch_row(body["dispatches"], e1, "pm")["status"] == "done"
        # 调度侧判据未被污染：pending 视图仍不含 done 行。
        store2 = orch.store.Store(tmp_dir / tid)
        assert all(d["status"] == "pending" for d in store2.pending_dispatches())
        assert not any(d["event_id"] == e1 and d["target"] == "pm"
                       for d in store2.pending_dispatches())


def test_status_payload_feeds_member_roster(tmp_dir):
    """名册数据源：同一个 /status 响应须同时给出 roles 投影与全量 dispatches。

    前端纯用这两份算每成员的状态点（绿/琥珀/灰/⛔），不新增端点、服务端不留缓存。
    """
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        assert isinstance(body.get("dispatches"), list), body
        roles = body.get("roles")
        assert isinstance(roles, list) and roles, body
        names = {r["role"] for r in roles}
        assert {"pm", "moderator"} <= names, roles
        for r in roles:
            for key in ("role", "primary", "effective", "blocked"):
                assert key in r, r
        # 名册"排队中"（琥珀）档的盘上依据：E1 给 pm 排了一条 pending 行。
        pm_rows = [d for d in body["dispatches"] if d["target"] == "pm"]
        assert pm_rows and any(d["status"] == "pending" for d in pm_rows), body


def test_direct_send_carries_to_for_member_filter(tmp_dir):
    """单聊过滤的服务端可测面：定向 send 后 /events 该事件的 to 含目标角色。

    前端 isMemberRelated(ev, role) 的 `to` 分支据此命中（旧判据只看 sender，
    "发给某成员"的消息看不到）。@ 成员只经 #send-to 下拉表达，不解析正文（§16.1）。
    """
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        code, body = _req(
            base, f"/api/threads/{tid}/send", "POST",
            {"to": "pm", "type": "question", "body": "进度如何"},
        )
        assert code == 200, (code, body)
        eid = body["event_id"]
        code, body = _req(base, f"/api/threads/{tid}/events")
        assert code == 200, (code, body)
        ev = next(e for e in body["events"] if e["id"] == eid)
        assert ev["sender"] == "human", ev
        assert "pm" in (ev.get("to") or []), ev
        # 正文原样落盘，未被任何 @ 解析改写。
        assert ev["body"] == "进度如何", ev


def test_app_js_member_roster_and_single_chat_predicates(tmp_dir):
    """app.js 关键判据存在性（全仓无 JS 单测，沿 test_app_js_served 的字符串断言风格）。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        # ① 单聊过滤具名判据：发出 ∪ 收到，且注释显式声明不是 §6.2 焦点窗、不回流调度。
        assert "function isMemberRelated(ev, role)" in js
        assert "ev.sender === role || (ev.to || []).includes(role)" in js
        assert "§6.2" in js and "焦点窗" in js
        assert "不得回流任何调度判定" in js
        # ② 绿点/正在响应必须判 deadline_ts（崩溃滞留行不得假绿）。
        assert "function isLiveDispatch(d)" in js
        assert "d.deadline_ts" in js
        assert "Date.now() / 1000" in js
        assert ".filter(isLiveDispatch)" in js, "updateTypingBar 应复用同一 deadline 判据"
        # ③ 名册四态 + 点击单聊。
        assert "function memberDotState(role, dispatches, blocked)" in js
        assert "function renderMemberRoster()" in js
        assert "function toggleMemberChat(role)" in js
        # ④ @ 成员语义只许写 #send-to.value，禁止解析正文（spec §16 第 1 条）。
        assert "sel.value = role" in js
        assert "禁止任何对消息正文的解析" in js
        # ⑤ 属性位一律 escapeHtmlAttr。
        assert 'data-member="${escapeHtmlAttr(role)}"' in js


def test_replay_endpoint_markdown(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        code, body = _req(base, f"/api/threads/{tid}/replay")
        assert code == 200, (code, body)
        assert "markdown" in body
        # 至少含首条事件的第三人称行 #1 [...]
        assert "#1" in body["markdown"] or "#" in body["markdown"]


def test_attach_endpoint_returns_command(tmp_dir):
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        code, body = _req(base, f"/api/threads/{tid}/attach/pm")
        assert code == 200, (code, body)
        assert "command" in body
        assert isinstance(body["command"], str) and body["command"]


# ——————————————————————————————————————————————————————————————
# (w-7) POST stop → 写 workspace 级 orch.stop
# ——————————————————————————————————————————————————————————————

def test_stop_writes_marker(tmp_dir):
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/stop", "POST", {})
        assert code == 200, (code, body)
        assert body.get("ok") is True
        assert (tmp_dir / "orch.stop").exists(), "应真写 workspace 级 orch.stop"


# ——————————————————————————————————————————————————————————————
# (w-8) config：非法 yaml 不写盘；合法写盘；读回一致
# ——————————————————————————————————————————————————————————————

def test_config_get_empty_when_absent(tmp_dir):
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/config")
        assert code == 200, (code, body)
        assert body.get("exists") is False
        assert body.get("yaml") == ""


def test_config_put_invalid_yaml_not_written(tmp_dir):
    with _Serving(tmp_dir) as base:
        bad = "roles: [unclosed\n  pm: x"
        code, body = _req(base, "/api/config", "PUT", {"yaml": bad})
        assert code == 200 or code == 400
        assert "error" in body, body
        # 关键：非法 yaml 不写盘
        assert not (tmp_dir / "config.yaml").exists(), "非法 yaml 不得落盘"


def test_config_put_valid_yaml_written_and_readback(tmp_dir):
    with _Serving(tmp_dir) as base:
        good = "thread_defaults:\n  max_rounds: 100\n  loop_limit: 3\nroles:\n  pm:\n    adapter: mock\n"
        code, body = _req(base, "/api/config", "PUT", {"yaml": good})
        assert code == 200, (code, body)
        assert body.get("ok") is True
        assert (tmp_dir / "config.yaml").exists()
        # 读回一致
        code, body = _req(base, "/api/config")
        assert code == 200
        assert body.get("exists") is True
        assert body["yaml"] == good


# ——————————————————————————————————————————————————————————————
# (w-9) metrics：§13 全字段名齐全
# ——————————————————————————————————————————————————————————————

_METRIC_LABELS = {
    "tasks", "avg_rounds", "cost", "aggregate_save_pct", "first_legal_pct",
    "background_compression_ratio", "resume_token_save_pct",
    "chaos_rounds", "chaos_mock_pass_pct", "chaos_real_pass_pct", "adapter_loc",
}


def test_metrics_has_all_section13_labels(tmp_dir):
    with _Serving(tmp_dir) as base:
        _new_thread(base)
        code, body = _req(base, "/api/metrics")
        assert code == 200, (code, body)
        assert "rows" in body
        labels = {r["label"] for r in body["rows"]}
        missing = _METRIC_LABELS - labels
        assert not missing, f"§13 指标缺字段: {missing}"


def test_metrics_no_data_shows_na(tmp_dir):
    """空 workspace（无采集数据）关键指标显示 N/A（不编造数值）。"""
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/metrics")
        assert code == 200, (code, body)
        rows = {r["label"]: r["value"] for r in body["rows"]}
        # cost / first_legal 等无采集 → N/A
        assert rows["cost"] == "N/A"


# ——————————————————————————————————————————————————————————————
# (w-10) bench：真跑 orch.render 估算，report 两侧样本
# ——————————————————————————————————————————————————————————————

def test_bench_returns_report(tmp_dir):
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/bench", "POST", {"fixture": "like", "runs": 3})
        assert code == 200, (code, body)
        assert "report" in body
        rep = body["report"]
        assert "no_resume" in rep and "with_resume" in rep
        assert isinstance(rep["no_resume"], list) and len(rep["no_resume"]) == 3
        assert isinstance(rep["with_resume"], list) and len(rep["with_resume"]) == 3


# ——————————————————————————————————————————————————————————————
# (w-11) 错误路径：404 / 405
# ——————————————————————————————————————————————————————————————

def test_unknown_api_path_404(tmp_dir):
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/nope")
        assert code == 404, (code, body)
        assert "error" in body


def test_method_not_allowed_405(tmp_dir):
    with _Serving(tmp_dir) as base:
        # /api/health 只支持 GET；DELETE → 405
        code, body = _req(base, "/api/health", "DELETE")
        assert code == 405, (code, body)
        assert "error" in body
