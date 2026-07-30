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
        # ⑤ 属性位一律 escapeHtmlAttr —— 含筛选勾选框的 value（单聊匹配关键路径）。
        assert 'data-member="${escapeHtmlAttr(role)}"' in js
        assert 'value="${escapeHtmlAttr(r)}" data-fkind="role"' in js


def test_app_js_cancel_single_chat_releases_send_to(tmp_dir):
    """R-应修1：取消单聊必须解锁 #send-to，否则下一条人类消息静默直发该成员。

    对称语义（激活即锁、取消即解锁）在 readFilters 里统一实现，覆盖四条离开路径：
    再点一次取消 / 清除全部 / chips 行 ✕ 移除 / 手动取消勾选。
    """
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        # 解锁判据本身：只在 value 恰为刚被取消的成员时回置兜底首项 ""。
        assert "let lastActiveMember = null;" in js
        assert "const nowActive = activeMemberRole();" in js
        assert "if (lastActiveMember && lastActiveMember !== nowActive) {" in js
        assert 'if (sel && sel.value === lastActiveMember) sel.value = "";' in js
        assert "lastActiveMember = nowActive;" in js
        # 放在 readFilters 内（"清除全部" / removeFilter 都经它）才覆盖得全。
        head = js.split("function readFilters()", 1)
        assert len(head) == 2, "readFilters 应存在"
        tail = head[1].split("\n}", 1)[0]
        assert "lastActiveMember" in tail, "解锁逻辑必须在 readFilters 内，否则清除全部不生效"
        # 切线程 / 切工作区也清标记（send-to 由 populateSendTo 重建）。
        assert js.count("lastActiveMember = null;") >= 3


def test_app_js_dispatch_summary_counts_live_only(tmp_dir):
    """R-应修2：线程头 dispatching chip 计数用 isLiveDispatch，崩溃滞留行不计入。

    否则「dispatching N」长亮假绿，与已按同一判据熄灭的"正在响应"胶囊同屏矛盾。
    明细表仍逐行显示盘上原始行，滞留行加标注。
    """
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert '["dispatching", disp.filter(isLiveDispatch).length]' in js
        assert '["dispatching", count("dispatching")]' not in js, "旧的裸 status 计数须撤除"
        # 明细表不做同样的过滤（盘上真相逐行可见），只加滞留标注。
        assert 'const stale = d.status === "dispatching" && !isLiveDispatch(d);' in js
        assert "滞留" in js


def test_app_js_gray_dot_titles_match_disk_facts(tmp_dir):
    """R-建议5：灰档细分——failed / gate_wait 行存在时 title 不得写"待命（无派发行）"。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert 'else if (d.status === "failed") failed = true;' in js
        assert 'else if (d.status === "gate_wait") gateWait = true;' in js
        assert 'if (gateWait) return "gate_wait";' in js
        assert 'if (failed) return "failed";' in js
        # 视觉仍是既有四色：灰档全部映射到同一个 d-idle 类，不加新色。
        assert 'MEMBER_DOT_GRAY = new Set(["idle", "stale", "failed", "gate_wait"])' in js
        assert 'MEMBER_DOT_GRAY.has(st) ? "idle" : st' in js
        # 只有"真的一条派发行都没有"才叫待命。
        assert 'idle: "待命（无派发行）"' in js


def test_app_js_live_dispatch_has_clock_skew_tolerance(tmp_dir):
    """R-建议：浏览器 Date.now 与服务端 time.time 可能不同源，绿灰互翻需容差。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert "const DISPATCH_CLOCK_SKEW_S = 3;" in js
        assert "dl > Date.now() / 1000 - DISPATCH_CLOCK_SKEW_S" in js
        # 注释须写明理由，且声明不回流调度判定。
        assert "不同源" in js and "不参与任何调度" in js


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


# ——————————————————————————————————————————————————————————————
# (w-12) 「本轮」统计：/events 同级键 round_stats
#        窗口锚点 = 最后一条 sender=='human' 的事件（含该条）；无 human → 全量。
#        服务端派生（纯函数、每请求现算、零缓存 §16.9），前端只渲染不重算。
# ——————————————————————————————————————————————————————————————

def _seed_thread(ws: Path, tid: str, rows: list[tuple]):
    """按 (sender, type, ts) 顺序真落盘一条线程（ts 已知 → duration_s 可打真值）。

    直接走 Store.append_event（同 CLI/web 的唯一落盘路径），不 mock；
    线程目录由 Store.__init__ 建，故 /api/threads/{tid}/... 的存在性检查会通过。
    """
    store = orch.store.Store(ws / tid)
    for sender, type_, ts in rows:
        store.append_event(
            sender=sender, type=type_, body=f"{sender}:{type_}",
            to=["moderator"], ts=ts,
        )
    store.set_meta("status", "running")
    return store


def _round_stats_of(base: str, tid: str) -> dict:
    code, body = _req(base, f"/api/threads/{tid}/events")
    assert code == 200, (code, body)
    assert "round_stats" in body, body
    return body["round_stats"]


def test_round_stats_window_is_all_events_when_no_human(tmp_dir):
    """锚点边界①：零 human 事件 → 窗口 = 全部事件，anchor_event_id 为 None。"""
    with _Serving(tmp_dir) as base:
        _seed_thread(tmp_dir, "t-nohuman", [
            ("pm", "chat", 1000.0),
            ("backend", "report", 1010.0),
            ("system", "system", 1015.0),
        ])
        rs = _round_stats_of(base, "t-nohuman")
        assert rs["anchor_event_id"] is None, rs
        assert rs["steps"] == 3, rs
        assert rs["duration_s"] == pytest.approx(15.0), rs
        # invoke = sender ∉ {human, system}：system 审计事件不算一次 invoke。
        assert rs["invokes"] == 2, rs


def test_round_stats_anchor_is_last_human_event(tmp_dir):
    """锚点边界②：多条 human → 取**最后一条**（"自我上次说话以来"）。"""
    with _Serving(tmp_dir) as base:
        store = _seed_thread(tmp_dir, "t-multi", [
            ("human", "assign", 100.0),
            ("pm", "report", 110.0),
            ("human", "question", 200.0),    # ← 锚点（含该条）
            ("pm", "answer", 230.0),
            ("backend", "report", 260.0),
        ])
        ids = [e["id"] for e in store.events()]
        rs = _round_stats_of(base, "t-multi")
        assert rs["anchor_event_id"] == ids[2], (rs, ids)
        assert rs["steps"] == 3, rs
        assert rs["duration_s"] == pytest.approx(60.0), rs
        assert rs["invokes"] == 2, rs


def test_round_stats_anchor_with_system_and_human_only(tmp_dir):
    """锚点边界③：只有 system + human → 窗口含 human 锚点及其后 system，invokes=0。"""
    with _Serving(tmp_dir) as base:
        store = _seed_thread(tmp_dir, "t-sysonly", [
            ("system", "system", 10.0),
            ("human", "assign", 20.0),       # ← 锚点
            ("system", "system", 25.0),
        ])
        ids = [e["id"] for e in store.events()]
        rs = _round_stats_of(base, "t-sysonly")
        assert rs["anchor_event_id"] == ids[1], (rs, ids)
        assert rs["steps"] == 2, rs
        assert rs["duration_s"] == pytest.approx(5.0), rs
        assert rs["invokes"] == 0, rs


def test_round_stats_single_event_window_and_empty_thread(tmp_dir):
    """duration_s 数值边界：窗口 ≤ 1 条 → 0.0（不是 None、也不编造）。"""
    with _Serving(tmp_dir) as base:
        # 末条即人类消息 → 窗口只有 1 条。
        store = _seed_thread(tmp_dir, "t-tail", [
            ("pm", "report", 500.0),
            ("human", "question", 900.0),    # ← 锚点即末条
        ])
        ids = [e["id"] for e in store.events()]
        rs = _round_stats_of(base, "t-tail")
        assert rs["anchor_event_id"] == ids[-1], (rs, ids)
        assert rs["steps"] == 1, rs
        assert rs["duration_s"] == 0.0, rs
        assert rs["invokes"] == 0, rs

        # 空线程（目录在、无事件）→ 全零 + anchor None。
        orch.store.Store(tmp_dir / "t-empty")
        rs2 = _round_stats_of(base, "t-empty")
        assert rs2 == {
            "anchor_event_id": None, "duration_s": 0.0, "steps": 0, "invokes": 0,
        }, rs2


def test_events_response_adds_round_stats_without_touching_event_shape(tmp_dir):
    """round_stats 是 events 的**同级键**；events 元素结构一个键都不许动（冻结面）。"""
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        code, body = _req(base, f"/api/threads/{tid}/events")
        assert code == 200, (code, body)
        assert set(body) == {"events", "round_stats"}, sorted(body)
        assert set(body["round_stats"]) == {
            "anchor_event_id", "duration_s", "steps", "invokes",
        }, body["round_stats"]
        assert set(body["events"][0]) == {
            "id", "sender", "type", "to", "body", "corr", "re", "ts",
            "meta", "artifacts", "bb_ops", "third_person",
        }, sorted(body["events"][0])


# ——————————————————————————————————————————————————————————————
# (w-13) 待办清单：首次声明序 + 展示层状态归一化（渲染在 app.js，判据两侧各打一半）
# ——————————————————————————————————————————————————————————————

def test_task_declaration_order_differs_from_lexicographic(tmp_dir):
    """乱序声明两个 key（后声明者字典序更小）→ 两种排序结果必须相反。

    这里打的是 /events 的**服务端真值**：按 id 升序直出 bb_ops，"首次声明事件号"
    可从中复原。声明序的**计算**在 R3 后已从前端移到 /board 端点（前端不再据
    bb_ops 自投影，见 w-14），本用例继续守住它的原料：事件序与 bb_ops 原文。
    """
    with _Serving(tmp_dir) as base:
        store = orch.store.Store(tmp_dir / "t-order")
        e1 = store.append_event(
            sender="pm", type="decision", body="先声明 zzz", to=["moderator"],
            blackboard_ops=[{"op": "set_task", "key": "zzz.first", "status": "doing"}])
        e2 = store.append_event(
            sender="pm", type="decision", body="后声明 aaa", to=["moderator"],
            blackboard_ops=[{"op": "set_task", "key": "aaa.second", "status": "todo"}])
        e3 = store.append_event(
            sender="pm", type="decision", body="更新 zzz", to=["moderator"],
            blackboard_ops=[{"op": "set_task", "key": "zzz.first", "status": "done"}])
        store.set_meta("status", "running")

        code, body = _req(base, "/api/threads/t-order/events")
        assert code == 200, (code, body)
        evs = body["events"]
        assert [e["id"] for e in evs] == [e1, e2, e3], evs

        first_seen: dict[str, int] = {}
        latest: dict[str, str] = {}
        for ev in evs:
            for op in (ev["bb_ops"] or []):
                if op.get("op") == "set_task":
                    first_seen.setdefault(op["key"], ev["id"])
                    latest[op["key"]] = op["status"]
        decl_order = sorted(first_seen, key=lambda k: first_seen[k])
        assert decl_order == ["zzz.first", "aaa.second"], first_seen
        assert decl_order != sorted(first_seen), "fixture 无区分力：两种排序恰好同序"
        # 同 key 后写只更新 status，首次声明事件号不变（位次不动）。
        assert first_seen["zzz.first"] == e1, first_seen
        assert latest["zzz.first"] == "done", latest
        # 落盘 status 原文未被任何"规范化"改写（协议里 status 是自由字符串）。
        assert latest["aaa.second"] == "todo", latest


def test_app_js_task_checklist_order_and_status_glyphs(tmp_dir):
    """app.js 关键判据存在性（沿 test_app_js_served 的字符串断言风格）。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        # ① 次序取端点给的声明序；字典序那行必须不在，前端也不得自造第二套判据。
        assert "Object.keys(tasks).sort()" not in js, "字典序排序必须被替换"
        assert "bd.task_order" in js, "待办次序须取权威端点的 task_order"
        assert "firstSeen" not in js, "前端不得再自算首次声明序（判据只留服务端一处）"
        # ② 展示层归一化三档（只映射字形，不写回落盘、不改 protocol/schema.py）。
        assert "function taskGlyph(status)" in js
        assert "TASK_DONE_WORDS" in js and "TASK_DOING_WORDS" in js
        for word in ("completed", "resolved", "已完成", "in_progress", "进行中"):
            assert word in js, word
        assert "✓" in js and "◐" in js and "○" in js
        assert "不写回" in js, "须注明展示映射不是数据清洗"
        # 未知值原文照排（不吞不改）。
        assert "未识别状态" in js
        # ③ 进度头「第 N/M 步」，M=0 不显示。
        assert "bd-progress" in js
        assert "步</span>" in js
        # ④ 「本轮」统计卡：数据取自 /events 的 round_stats，前端不重算。
        assert "lastRoundStats" in js
        assert "data.round_stats" in js
        assert "evData.round_stats" in js
        assert "自最后一条人类消息起" in js
        assert "工具数" in js, "缺栏理由须留注释（盘上无工具调用痕迹）"
        # ⑤ 属性位一律 escapeHtmlAttr。
        assert 'data-goto="${escapeHtmlAttr(s)}"' in js


# ——————————————————————————————————————————————————————————————
# (w-14) 黑板三节 = **权威**黑板（GET /api/threads/{id}/board）
#
# 落库 ≠ 生效：被 §3.3 门槛拒绝的 bb_ops 照样进库（store.reply_and_done 无条件写
# bb_ops_json），调度层 _apply_bb_if_eligible（scheduler/core.py:666-688）只是不
# 应用 + 追加 system 审计事件。若展示层据事件重投影，被拒绝的 agent 自述就会被
# 勾选与「第 N/M 步」算进完成度 —— §16 第 5 条"采信 agent 自述"的展示层形态。
#
# 下面用例的盘上事实**全部由真调度器产生**（FakeApiAdapter 脚本化回复 +
# orch.scheduler.run_thread）：应用/拒绝的判定出自 _apply_bb_if_eligible，
# 测试不复刻门槛逻辑，只断言 HTTP 真值。
# ——————————————————————————————————————————————————————————————

def _run_bb_chain(ws: Path, tid: str):
    """真跑一条链，产出「两条权威待办 + 一条被拒绝的自述」的盘上事实。

    E1(human→pm)
      → pm#1  decision   to=[backend]   set_task zzz.first=doing     （can_decide=true → 应用）
      → backend#1 acceptance to=[pm]    set_task backend.selfclaim=done
                                        set_decision 自封的决策       （can_decide=false → 整批拒绝）
      → pm#2  decision   to=[moderator] set_task aaa.second=todo
                                        set_task zzz.first=done
                                        freeze_contract like-api v1
                                        set_decision 真决策           （应用）
      → moderator#1 terminate

    三处设计要点：
      · backend **不配 verify** → §8.3 不会把 acceptance 降级成 report
        （scheduler/core.py:589 明文"未配 verify 的角色 acceptance 原样放行"），
        于是命中的正是 §3.3 门槛里 can_decide 那一半 —— 也只有 acceptance 能走到
        这里：decision/gate_decision 早被 §3.2 发送者约束拦下降级为 report。
      · 两条权威待办的声明序与字典序**相反**（zzz 先、aaa 后），排序判据有区分力。
      · 拒绝产生的 system 审计事件 to=[moderator]，与 pm#2 的回复同轮分组，组间按
        最小 event_id 升序（core.py:242-254）→ pm 组先跑，链路结果确定。
    """
    import orch.adapters
    import orch.scheduler

    store = orch.store.Store(ws / tid)
    store.set_meta("status", "running")
    store.set_meta("roles", json.dumps(["pm", "backend", "moderator"], ensure_ascii=False))
    store.append_event(sender="human", type="assign", body="点赞功能", to=["pm"])

    config = {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "roles": {
            "pm": {"can_decide": True, "write_scope": [], "tools": []},
            "backend": {"can_decide": False, "write_scope": [], "tools": []},
            "moderator": {"can_decide": True, "write_scope": [], "tools": []},
        },
    }
    adapters = {
        "pm": orch.adapters.FakeApiAdapter(
            role="pm", config={"kind": "api"},
            scripted_replies={
                1: {"type": "decision", "to": ["backend"], "body": "先立 zzz",
                    "blackboard_ops": [
                        {"op": "set_task", "key": "zzz.first", "status": "doing"},
                    ]},
                2: {"type": "decision", "to": ["moderator"], "body": "补 aaa 并收 zzz",
                    "blackboard_ops": [
                        {"op": "set_task", "key": "aaa.second", "status": "todo"},
                        {"op": "set_task", "key": "zzz.first", "status": "done"},
                        {"op": "freeze_contract", "name": "like-api",
                         "version": 1, "path": "docs/like-api.md"},
                        {"op": "set_decision", "text": "真决策：先做 aaa"},
                    ]},
            }),
        "backend": orch.adapters.FakeApiAdapter(
            role="backend", config={"kind": "api"},
            scripted_reply={
                "type": "acceptance", "to": ["pm"], "body": "我自己说我做完了",
                "blackboard_ops": [
                    {"op": "set_task", "key": "backend.selfclaim", "status": "done"},
                    {"op": "set_decision", "text": "自封的决策"},
                ]}),
        "moderator": orch.adapters.FakeApiAdapter(
            role="moderator", config={"kind": "api"},
            scripted_reply={"type": "terminate", "to": [], "body": "收工"}),
    }
    orch.scheduler.run_thread(store, config, adapters)
    return store


def _board_of(base: str, tid: str) -> dict:
    code, body = _req(base, f"/api/threads/{tid}/board")
    assert code == 200, (code, body)
    return body


def test_board_excludes_rejected_self_claim_while_chat_keeps_the_event(tmp_dir):
    """①被拒绝的自述：黑板不呈现，聊天流照旧看得见（如实展示消息 ≠ 采信自述）。"""
    with _Serving(tmp_dir) as base:
        store = _run_bb_chain(tmp_dir, "t-bb")
        board = _board_of(base, "t-bb")

        # 任务节 / 声明序 / 溯源 三处都不给它位置。
        assert "backend.selfclaim" not in board["tasks"], board["tasks"]
        assert "backend.selfclaim" not in board["task_order"], board["task_order"]
        assert "backend.selfclaim" not in board["task_evt"], board["task_evt"]
        # 同一条 acceptance 里的 set_decision 一并被拒 → 决策节也没有它。
        texts = [d.get("text") for d in board["decisions"]]
        assert "自封的决策" not in texts, texts
        # 权威侧亲自复核：state.json 里本来就没有（端点没有"过滤掉"什么，是没有）。
        assert "backend.selfclaim" not in orch.store.board_state(store)["tasks"]

        # 聊天流：这条 acceptance 与它的自述 ops 一字不少（bb_ops 是消息内容）。
        code, ev_body = _req(base, "/api/threads/t-bb/events")
        assert code == 200, (code, ev_body)
        claims = [e for e in ev_body["events"]
                  if e["sender"] == "backend" and e["type"] == "acceptance"]
        assert len(claims) == 1, [(e["sender"], e["type"]) for e in ev_body["events"]]
        assert [op.get("key") for op in claims[0]["bb_ops"]] == [
            "backend.selfclaim", None], claims[0]["bb_ops"]
        # 调度器确实走了"拒绝"分支：审计事件在盘上（不是"什么都没发生"）。
        audits = [e for e in ev_body["events"]
                  if e["sender"] == "system" and "blackboard_ops" in e["body"]]
        assert len(audits) == 1, [e["body"] for e in ev_body["events"]
                                  if e["sender"] == "system"]
        assert "backend" in audits[0]["body"] and "acceptance" in audits[0]["body"]


def test_board_shows_authoritative_ops_in_declaration_order(tmp_dir):
    """②can_decide 角色的合法 ops：三节如实呈现，待办按声明序（非字典序）。"""
    with _Serving(tmp_dir) as base:
        _run_bb_chain(tmp_dir, "t-bb2")
        board = _board_of(base, "t-bb2")
        code, ev_body = _req(base, "/api/threads/t-bb2/events")
        assert code == 200, (code, ev_body)
        pm_evts = [e["id"] for e in ev_body["events"]
                   if e["sender"] == "pm" and e["type"] == "decision"]
        assert len(pm_evts) == 2, pm_evts
        pm1, pm2 = pm_evts

        # 任务节：只有两条权威待办，status 为落盘原文（zzz 已被二次声明改成 done）。
        assert board["tasks"] == {"zzz.first": "done", "aaa.second": "todo"}, board["tasks"]
        # 声明序：zzz 先声明（pm#1）→ 排在字典序更小的 aaa 前面。
        assert board["task_order"] == ["zzz.first", "aaa.second"], board["task_order"]
        assert board["task_order"] != sorted(board["tasks"]), "fixture 无区分力"
        # 溯源 #evt = 最近一次声明该 key 的事件号。
        assert board["task_evt"]["zzz.first"] == pm2, (board["task_evt"], pm1, pm2)
        assert board["task_evt"]["aaa.second"] == pm2, board["task_evt"]
        # 契约节：frozen_at 由权威 state.json 直出（前端 #evt 取它）。
        assert board["contracts"]["like-api"]["version"] == 1, board["contracts"]
        assert board["contracts"]["like-api"]["path"] == "docs/like-api.md"
        assert board["contracts"]["like-api"]["frozen_at"] == pm2, board["contracts"]
        # 决策节：只剩合法那条，带事件号。
        assert [d["text"] for d in board["decisions"]] == ["真决策：先做 aaa"], board["decisions"]
        assert board["decisions"][0]["evt"] == pm2, board["decisions"]


def test_board_progress_denominator_counts_only_authoritative_tasks(tmp_dir):
    """③「第 N/M 步」的分子分母只数权威任务：M=2 而非把自述算进去的 3。"""
    with _Serving(tmp_dir) as base:
        _run_bb_chain(tmp_dir, "t-bb3")
        board = _board_of(base, "t-bb3")
        keys = board["task_order"]
        assert len(keys) == 2, keys                      # M：分母
        done = [k for k in keys if board["tasks"][k] == "done"]
        assert done == ["zzz.first"], board["tasks"]     # N：分子（自述那条不在其中）
        # 前端拿的正是这份 tasks —— 进度头用同一批 key 算，不另开数据源。
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert "taskKeys.filter((k) => taskGlyph(tasks[k]).cls === \"done\")" in js
        assert "只数权威黑板里的任务" in js


def test_board_endpoint_shape_and_empty_thread(tmp_dir):
    """端点键名冻结 + 空线程给真空（不是报错，也不是编造）+ 方法/线程存在性。"""
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        board = _board_of(base, tid)
        assert set(board) == {
            "contracts", "decisions", "tasks", "task_order", "task_evt",
        }, sorted(board)
        assert board == {"contracts": {}, "decisions": [], "tasks": {},
                         "task_order": [], "task_evt": {}}, board
        code, body = _req(base, f"/api/threads/{tid}/board", "POST", {})
        assert code == 405, (code, body)
        code, body = _req(base, "/api/threads/no-such-thread/board")
        assert code == 404, (code, body)


def test_app_js_board_reads_endpoint_and_shows_read_failure(tmp_dir):
    """前端判据：黑板只吃 /board 端点，前端自投影退役，读不到时显式失败态。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        # ① 数据源 = 只读端点；前端 bb_ops 自投影函数整体退役。
        assert "/api/threads/${tid}/board" in js
        assert "projectBoard" not in js, "前端 bb_ops 自投影必须退役，不留兜底"
        assert "lastBoard" in js
        # ② 三节字段改吃 state.json 口径（契约 frozen_at / 待办 task_evt）。
        assert "c.frozen_at" in js
        assert "bd.task_evt" in js
        # ③ 读不到 = 失败态，不得渲染成"暂无"。
        assert "黑板读取失败" in js and "bd-fail" in js
        code, css = _req(base, "/styles.css")
        assert code == 200, code
        assert ".bd-fail" in css


# ——————————————————————————————————————————————————————————————
# (w-10) T4：GET /api/threads/{id}/steps —— invoke 执行流步骤摘要（只读）
#
# 裁决口径（QUESTIONS.md Q11 采 A）：只出**解析后的摘要**（工具名 + 命令摘要 + 计数），
# stdout 原文不经 HTTP 直出（原文留 logs/ 供审计）。故本节除三态外，必须有一条
# **敏感串不外泄**断言：fixture 原文里埋 sessionId 样式串，断言响应全文不含它。
# ——————————————————————————————————————————————————————————————

# 与 tests/test_invoke_steps.py 同源的埋点串（那边验解析层，这边验 HTTP 出网关）。
STEPS_FIXTURE_SID = "ses_019f98e73524-0758bd76e429"


def _opencode_raw_stdout(sid: str = STEPS_FIXTURE_SID) -> str:
    """opencode run --format json 形状的 stdout 原文（含工具行与敏感 sessionId）。"""
    envelope = '```json\n{"to":["moderator"],"type":"report","body":"跑完了"}\n```'
    lines = [
        {"type": "step_start", "sessionID": sid, "part": {"type": "step-start"}},
        {"type": "tool", "sessionID": sid, "part": {
            "type": "tool", "tool": "bash",
            "state": {"status": "completed",
                      "input": {"command": "pytest -q tests/test_web.py"},
                      "output": f"3 passed; session={sid}",
                      "time": {"start": 1785037196000, "end": 1785037198500}}}},
        {"type": "text", "sessionID": sid,
         "part": {"type": "text", "text": "结论：\n" + envelope}},
        {"type": "step_finish", "sessionID": sid,
         "part": {"type": "step-finish", "reason": "stop"}},
    ]
    return "\n".join(json.dumps(x, ensure_ascii=False) for x in lines)


def _write_config(ws: Path, wire_format: str = "opencode-stream", timeout_s: int = 300):
    """workspace 级 config.yaml：pm 绑一个 CLI 型 adapter（wire_format 可调）。"""
    (ws / "config.yaml").write_text(
        "adapters:\n"
        "  oc:\n"
        "    kind: cli\n"
        "    start_cmd: oc run --format json\n"
        f"    wire_format: {wire_format}\n"
        "  other:\n"
        "    kind: cli\n"
        "    start_cmd: kimi -p\n"
        "    wire_format: stream-json\n"
        "roles:\n"
        "  pm:\n"
        "    adapter: oc\n"
        "    can_decide: true\n"
        f"    caps: {{timeout_s: {timeout_s}}}\n"
        "  moderator:\n"
        "    adapter: oc\n"
        "    can_decide: true\n",
        encoding="utf-8",
    )


def _seed_reply_with_log(ws: Path, tid: str, raw_stdout: str | None,
                         role: str = "pm") -> int:
    """造一条角色回复气泡（re=[1]）+ 其 invoke 日志；返回该回复的事件号。

    日志用 store.write_invoke_log 落（§14 文件名约定的**权威**写者，不手拼文件名）。
    raw_stdout=None → 只有回复、没有日志（"未找到执行日志"那一态）。
    """
    store = orch.store.Store(ws / tid)
    eid = store.append_event(sender=role, type="report", body="跑完了", to=["moderator"],
                             re=[1])
    if raw_stdout is not None:
        store.write_invoke_log(event_ids=[1], role=role,
                               view_text="=== 焦点窗 ===\n#1 …", output_text=raw_stdout)
    return eid


def _steps_of(base: str, tid: str, event_id) -> dict:
    code, body = _req(base, f"/api/threads/{tid}/steps?event_id={event_id}")
    assert code == 200, (code, body)
    return body


def test_steps_endpoint_parses_tool_steps_from_invoke_log(tmp_dir):
    """有步骤态：流式后端的 logs/ 原文 → 工具名 + 命令摘要 + counts + log_file。"""
    _write_config(tmp_dir)
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        eid = _seed_reply_with_log(tmp_dir, tid, _opencode_raw_stdout())

        body = _steps_of(base, tid, eid)
        assert set(body) == {"steps", "counts", "wire_format", "log_file", "note",
                             "truncated"}, body
        assert body["wire_format"] == "opencode-stream", body
        assert body["truncated"] is False, body
        assert body["log_file"] and body["log_file"].endswith("_E1_pm.log"), body
        tools = [s for s in body["steps"] if s["kind"] == "tool"]
        assert len(tools) == 1, body["steps"]
        assert tools[0]["name"] == "bash"
        assert "pytest -q tests/test_web.py" in tools[0]["summary"]
        assert tools[0]["dur_ms"] == 2500
        assert body["counts"]["tool"] == 1, body["counts"]
        assert body["counts"]["text"] == 1, body["counts"]
        # seq 连续，供前端直接列序号。
        assert [s["seq"] for s in body["steps"]] == list(
            range(1, len(body["steps"]) + 1))


def test_steps_endpoint_never_exposes_raw_stdout_or_session_id(tmp_dir):
    """Q11 硬口径：响应**全文**不得含 stdout 原文片段与 sessionId（原文只在盘上）。"""
    _write_config(tmp_dir)
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        eid = _seed_reply_with_log(tmp_dir, tid, _opencode_raw_stdout())

        code, raw = _req(base, f"/api/threads/{tid}/steps?event_id={eid}")
        assert code == 200, raw
        blob = json.dumps(raw, ensure_ascii=False)
        assert STEPS_FIXTURE_SID not in blob, blob
        assert "3 passed" not in blob, "工具输出正文不得随摘要外泄"
        assert "sessionID" not in blob, blob
        # 对照：原文确实在盘上（审计不受影响，只是不经 HTTP）。
        logs = list((tmp_dir / tid / "logs").iterdir())
        assert logs and STEPS_FIXTURE_SID in logs[0].read_text(encoding="utf-8")


def test_steps_endpoint_no_log_returns_empty_with_note(tmp_dir):
    """无日志态：200 + steps=[] + 一句人话（不 404、不猜）。"""
    _write_config(tmp_dir)
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        eid = _seed_reply_with_log(tmp_dir, tid, None)

        body = _steps_of(base, tid, eid)
        assert body["steps"] == [] and body["log_file"] is None, body
        assert "未找到本次执行日志" in body["note"], body
        assert body["counts"] == {"tool": 0, "thinking": 0, "text": 0, "other": 0}, body


def test_steps_endpoint_non_stream_backend_explains_empty(tmp_dir):
    """非流式态：wire_format=json 的后端没有逐行事件 → 空 + 说明为什么空。"""
    _write_config(tmp_dir, wire_format="json")
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        eid = _seed_reply_with_log(
            tmp_dir, tid,
            json.dumps({"text": "收到", "sessionId": STEPS_FIXTURE_SID}))

        body = _steps_of(base, tid, eid)
        assert body["wire_format"] == "json", body
        assert body["steps"] == [], body
        assert "不产生步骤流" in body["note"], body
        assert STEPS_FIXTURE_SID not in json.dumps(body, ensure_ascii=False)


def test_steps_endpoint_ignores_current_binding_and_sniffs_the_log(tmp_dir):
    """换绑后**不得**按当前绑定硬解析：格式只认日志内容（评审 应修2）。

    旧行为（错态，本用例前身只断言 wire_format 取值、不断言产物，等于把错态写进测试）：
    sessions.backend 换成 stream-json 家的 adapter 后，同一份 opencode 原文被按
    stream-json 逐行解析 → 顶层 type=="tool" 命中，吐出
    `{"kind":"tool","name":"tool","summary":""}` 的**假步骤**，真实工具名与命令全丢。
    现在：嗅探认出 opencode → 产物必须与未换绑时逐字一致。
    """
    _write_config(tmp_dir)
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        eid = _seed_reply_with_log(tmp_dir, tid, _opencode_raw_stdout())
        store = orch.store.Store(tmp_dir / tid)
        store.upsert_session(role="pm", sid="s1", gen=1, backend="other")

        body = _steps_of(base, tid, eid)
        assert body["wire_format"] == "opencode-stream", body
        tools = [s for s in body["steps"] if s["kind"] == "tool"]
        assert len(tools) == 1, body["steps"]
        assert tools[0]["name"] == "bash", "换绑不得把真实工具名换成占位 'tool'"
        assert "pytest -q tests/test_web.py" in tools[0]["summary"], body["steps"]
        # 假步骤的判据：kind=tool 但 name 是占位、summary 空 —— 一条都不许有。
        assert not [s for s in body["steps"]
                    if s["kind"] == "tool" and s["name"] == "tool" and not s["summary"]], body


def test_steps_endpoint_refuses_to_guess_unrecognized_log_shape(tmp_dir):
    """两家特征都不像的逐行 JSON → 诚实空态 + "不猜测"，绝不出假步骤（应修2）。"""
    _write_config(tmp_dir)
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        # 逐行 JSON，但既无 opencode 的 part/sessionID，也无 stream-json 的
        # role/tool_calls/事件型名 —— 例如某个未知后端（或未来改了形状的一家）。
        weird = "\n".join(json.dumps({"evt": i, "payload": {"cmd": "rm -rf /"}})
                          for i in range(4))
        eid = _seed_reply_with_log(tmp_dir, tid, weird)

        body = _steps_of(base, tid, eid)
        assert body["wire_format"] is None, body
        assert body["steps"] == [], body
        assert "无法判定该日志的流式格式" in body["note"], body
        assert "不猜测" in body["note"], body


def test_steps_endpoint_sniffs_stream_json_shape(tmp_dir):
    """反向对照：stream-json 形状的日志即使当前绑定是 opencode，也按 stream-json 解析。"""
    _write_config(tmp_dir)      # pm 主绑定 oc = opencode-stream
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        raw = "\n".join(json.dumps(x, ensure_ascii=False) for x in [
            {"role": "assistant", "content": "我先看一下测试"},
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "Bash", "arguments": '{"command":"pytest -q"}'}}]},
            {"role": "meta", "type": "session.resume_hint",
             "session_id": STEPS_FIXTURE_SID},
        ])
        eid = _seed_reply_with_log(tmp_dir, tid, raw)

        body = _steps_of(base, tid, eid)
        assert body["wire_format"] == "stream-json", body
        tools = [s for s in body["steps"] if s["kind"] == "tool"]
        assert [t["name"] for t in tools] == ["Bash"], body["steps"]
        assert STEPS_FIXTURE_SID not in json.dumps(body, ensure_ascii=False)


def test_steps_endpoint_caps_step_count_and_flags_truncated(tmp_dir):
    """步数上限（评审 建议4）：超限只给前 N 条 + truncated=True + note 说明。"""
    from orch.web.server import _STEP_LIMIT

    _write_config(tmp_dir)
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        many = "\n".join(
            json.dumps({"type": "tool", "sessionID": STEPS_FIXTURE_SID, "part": {
                "type": "tool", "tool": f"t{i}",
                "state": {"input": {"command": f"echo {i}"}}}}, ensure_ascii=False)
            for i in range(_STEP_LIMIT + 40))
        eid = _seed_reply_with_log(tmp_dir, tid, many)

        body = _steps_of(base, tid, eid)
        assert len(body["steps"]) == _STEP_LIMIT, len(body["steps"])
        assert body["truncated"] is True, body
        assert "展示上限" in body["note"], body
        assert body["counts"]["tool"] == _STEP_LIMIT, body["counts"]


def test_steps_endpoint_caps_read_size_on_huge_log(tmp_dir):
    """读取上限（建议4）：超限只解析日志尾部 + truncated=True + note 写明字节数。

    不真造 244MB：把上限临时压到很小，验证的是"按上限只读尾部"这条路，与阈值大小无关。
    """
    import orch.web.server as srv

    _write_config(tmp_dir)
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        head_line = json.dumps({"type": "tool", "sessionID": STEPS_FIXTURE_SID, "part": {
            "type": "tool", "tool": "EARLIEST",
            "state": {"input": {"command": "x" * 400}}}}, ensure_ascii=False)
        tail_line = json.dumps({"type": "tool", "sessionID": STEPS_FIXTURE_SID, "part": {
            "type": "tool", "tool": "LATEST",
            "state": {"input": {"command": "final step"}}}}, ensure_ascii=False)
        eid = _seed_reply_with_log(
            tmp_dir, tid, "\n".join([head_line] * 12 + [tail_line]))

        old = srv._LOG_TAIL_LIMIT_BYTES
        srv._LOG_TAIL_LIMIT_BYTES = 900        # 小于该日志体积 → 走尾部读取分支
        try:
            body = _steps_of(base, tid, eid)
        finally:
            srv._LOG_TAIL_LIMIT_BYTES = old

        assert body["truncated"] is True, body
        assert "仅解析末尾" in body["note"], body
        names = [s["name"] for s in body["steps"]]
        assert "LATEST" in names, names       # 尾部（收尾几步）必须在
        assert len(names) < 13, names         # 靠前的步骤如实缺席，不假装完整
        assert STEPS_FIXTURE_SID not in json.dumps(body, ensure_ascii=False)


def test_steps_endpoint_does_not_cry_truncation_when_output_section_fits(tmp_dir):
    """尾部窗口里能找到分隔行 ⇒ OUTPUT 段完整（切掉的只是 VIEW）→ 不许报截断。

    报了就是虚惊：读者会以为少了几步，跑去 logs/ 白找一遍。
    """
    import orch.web.server as srv

    _write_config(tmp_dir)
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        store = orch.store.Store(tmp_dir / tid)
        eid = store.append_event(sender="pm", type="report", body="ok",
                                 to=["moderator"], re=[1])
        # VIEW 段极大、OUTPUT 段很小：尾部窗口必然把分隔行连同整个 OUTPUT 段一起框住。
        store.write_invoke_log(
            event_ids=[1], role="pm", view_text="v" * 4000,
            output_text=_opencode_raw_stdout())

        old = srv._LOG_TAIL_LIMIT_BYTES
        srv._LOG_TAIL_LIMIT_BYTES = 1500
        try:
            body = _steps_of(base, tid, eid)
        finally:
            srv._LOG_TAIL_LIMIT_BYTES = old

        assert body["truncated"] is False, body
        assert "仅解析末尾" not in body["note"], body
        tools = [s for s in body["steps"] if s["kind"] == "tool"]
        assert [t["name"] for t in tools] == ["bash"], body["steps"]


def test_steps_endpoint_human_and_unknown_event_are_not_invokes(tmp_dir):
    """human/system 事件与不存在的事件号：都是 200 + 空 + 说明，不 404。"""
    _write_config(tmp_dir)
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        body = _steps_of(base, tid, 1)          # E1 = human assign
        assert body["steps"] == [] and "不是一次后端 invoke" in body["note"], body
        body = _steps_of(base, tid, 9999)
        assert body["steps"] == [] and "没有事件 #9999" in body["note"], body


def test_steps_endpoint_requires_event_id_and_get(tmp_dir):
    """event_id 必填（整数）；方法白名单只放 GET；未知线程仍 404。"""
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        code, body = _req(base, f"/api/threads/{tid}/steps")
        assert code == 400, (code, body)
        code, body = _req(base, f"/api/threads/{tid}/steps?event_id=abc")
        assert code == 400, (code, body)
        code, body = _req(base, f"/api/threads/{tid}/steps?event_id=1", "POST", {})
        assert code == 405, (code, body)
        code, body = _req(base, "/api/threads/no-such/steps?event_id=1")
        assert code == 404, (code, body)


# ——————————————————————————————————————————————————————————————
# (w-11) T4：/status 派发行的可选键 started_ts（由 deadline_ts 逆算，不新增落盘字段）
# ——————————————————————————————————————————————————————————————

def test_status_dispatch_row_carries_started_ts_derived_from_deadline(tmp_dir):
    """started_ts = deadline_ts − 该角色 caps.timeout_s（与调度层 _timeout_for 同源）。"""
    _write_config(tmp_dir, timeout_s=300)
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        store = orch.store.Store(tmp_dir / tid)
        e1 = store.events()[0]["id"]
        deadline = time.time() + 120
        store.mark_dispatching(e1, "pm", deadline)

        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        row = _dispatch_row(body["dispatches"], e1, "pm")
        assert abs(float(row["started_ts"]) - (deadline - 300)) < 0.001, row
        # 既有键一个不动（键名冻结）+ 仍不得出现 role 键。
        for key in ("event_id", "target", "status", "deadline_ts", "attempts"):
            assert key in row, row
        assert "role" not in row, row


def test_status_omits_started_ts_when_deadline_or_config_missing(tmp_dir):
    """两条"不猜"出口：pending 行无 deadline_ts → 无该键；config 坏了 → 整体不给。"""
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        # (a) 无 config.yaml：pending 行没有 deadline_ts，键必须缺席。
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        pending = [d for d in body["dispatches"] if d["status"] == "pending"]
        assert pending, body
        assert all("started_ts" not in d for d in pending), pending

        # (b) config.yaml 语法错：超时秒取不到 → dispatching 行也不给 started_ts。
        store = orch.store.Store(tmp_dir / tid)
        e1 = store.events()[0]["id"]
        store.mark_dispatching(e1, "pm", time.time() + 60)
        (tmp_dir / "config.yaml").write_text("roles:\n  pm: [oops\n", encoding="utf-8")
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        assert body.get("config_error"), body
        row = _dispatch_row(body["dispatches"], e1, "pm")
        assert "started_ts" not in row, row
        assert row["deadline_ts"] is not None, row      # 原有键照旧如实给


# ——————————————————————————————————————————————————————————————
# (w-12) T4 前端判据：View Steps 折叠组（lazy fetch / 空态 / 自述标注 / 走字）
# ——————————————————————————————————————————————————————————————

def test_app_js_view_steps_is_lazy_and_cached(tmp_dir):
    """点开才 fetch（轮询不得放大）；重渲染用缓存回填，不重复打端点。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        # ① 只在点击处理里发请求；折叠组构建函数里没有 fetch。
        assert "/api/threads/${tid}/steps?event_id=" in js
        assert "async function loadSteps(eid)" in js
        head = js.split("function buildStepsBlock(ev)", 1)
        assert len(head) == 2, "buildStepsBlock 应存在"
        assert "api(" not in head[1].split("\n}\n", 1)[0], "构建折叠组时不得取数（lazy）"
        # ② 展开态与结果都存在前端投影里（轮询重建气泡后不重打端点）。
        assert "const stepsCache = new Map();" in js
        assert "const stepsOpen = new Set();" in js
        # ③ 计数回填按钮文案。
        assert "View Steps" in js
        assert "View Steps · " in js


def test_app_js_view_steps_empty_states_are_honest(tmp_dir):
    """空态两句都在：非流式后端 / 未找到日志；且服务端 note 原样带出。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert "不产生步骤流" in js
        assert "未找到本次执行日志" in js
        assert "res.note" in js
        # 空态不得说"暂无步骤"这类含糊话（同 bd-fail 的口径：读不到 ≠ 没有）。
        assert "steps-empty" in js
        # 嗅不出格式那一档也得有诚实文案（应修2 的前端半边），且不冒充"查不到配置"。
        assert "无法判定该日志的流式格式" in js
        assert "查不到该角色的适配器配置" not in js
        # truncated 必须在页面上有标记，否则 500 步会被读成"这就是全部"（建议4）。
        assert "res.truncated" in js and "已截断" in js


def test_app_js_step_kind_class_is_front_end_whitelisted(tmp_dir):
    """kind → class 走前端白名单（评审 建议11）：不把服务端串拼进 class 属性。

    kind 的源头是模型可控文本，前端不拿服务端契约当输入校验；白名单外一律 other，
    于是 `k-` 类名永远只有四种字面量。
    """
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert "const STEP_KIND_CLASS" in js
        assert "function stepKindClass(kind)" in js
        assert "step-row k-${kind}" in js
        # 旧写法（直接转义服务端串再拼进 class）不得残留。
        assert "k-${escapeHtmlAttr(kind)}" not in js


def test_app_js_clears_typing_clock_on_thread_and_workspace_switch(tmp_dir):
    """切线程/切工作区必须连**定时器**一起清（评审 建议12）。

    只清 typingRows 会留一个每秒空转的 setInterval：renderTypingPill 见空投影只隐藏
    胶囊，然后一直白跑到下一拍 status。
    """
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert "function clearTypingClock()" in js
        body = js.split("function clearTypingClock()", 1)[1].split("\n}\n", 1)[0]
        assert "clearInterval(typingTimer)" in body, body
        assert "typingTimer = null" in body, body
        # 两处清场都改走它（不是各写两行、漏掉定时器那一行）。
        assert js.count("clearTypingClock();") >= 2, js.count("clearTypingClock();")
        for fn_name in ("function switchWorkspace(name)", "async function selectThread(tid)"):
            assert fn_name in js, fn_name
            fn = js.split(fn_name, 1)[1].split("\n}\n", 1)[0]
            assert "clearTypingClock();" in fn, fn_name


def test_app_js_view_steps_marks_self_claim_on_a_class_bubbles(tmp_dir):
    """acceptance/decision 上必须标注"后端自述·非系统验证"，且不与 verify 徽章争位。

    §16 第 5 条：verify 徽章才是系统侧证据。折叠组挂在气泡**末尾**（徽章在正文之上），
    两者不相邻。
    """
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert "后端自述·非系统验证" in js
        assert "steps-selfclaim" in js
        # 结构：verify 徽章在正文之前、折叠组在 artifacts/展开按钮之后（气泡最末）。
        # 定位必须先切到 buildBubble 的**函数体**再比次序：子串 "buildStepsBlock(ev)"
        # 在全文里还命中函数定义（`function buildStepsBlock(ev)`）与 paintStepsBlock 的
        # 单气泡重绘，两处都在 buildBubble 之前，全文 str.index 取首次出现会定位到定义
        # 上——那测的是"定义写在哪"，不是接线次序。
        parts = js.split("function buildBubble(ev)", 1)
        assert len(parts) == 2, "buildBubble 应存在"
        fn = parts[1].split("\n}\n", 1)[0]
        badge_i = fn.index("buildVerifyBadge(ev) +")
        steps_i = fn.index("buildStepsBlock(ev)")
        assert badge_i < steps_i, "折叠组必须在 verify 徽章之后（不相邻争位）"
        # 且隔着整段正文与 artifacts chips：不是"紧贴徽章"的换位摆法。
        assert badge_i < fn.index('class="b-body') < steps_i, fn
        assert fn.index("buildArtifactChips(ev)") < steps_i, fn
        # 折叠组是拼接的最后一项（其后只剩 return）。
        assert "buildStepsBlock(ev);" in fn.split("return div;", 1)[0], fn
        code, css = _req(base, "/styles.css")
        assert code == 200, code
        assert ".steps-selfclaim" in css and ".b-steps" in css


def test_app_js_typing_pill_counts_up_from_started_ts(tmp_dir):
    """「正在处理中 mm:ss」每秒走字：纯前端 setInterval，数据源是落盘推得的 started_ts。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert "正在处理中" in js
        assert "d.started_ts" in js
        assert "setInterval(renderTypingPill, 1000)" in js
        # 缺 started_ts 时维持既有胶囊文案（不编造起点）。
        assert "正在响应" in js
        # 计时只是展示：不得参与任何调度判定（注释显式声明）。
        assert "不参与任何调度判定" in js
