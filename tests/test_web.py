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
# (w-9b) 二轮评审 应修1：**损坏**的 state.json 不许被当"空黑板"直出 200
#
# 复现依据：Store._write_state 是非原子 write_text（store/__init__.py:643-647），
# 崩在中途即得半截 JSON；而宽松读取把 JSONDecodeError 降级成空结构
# （store:_read_state），端点无从区分 —— 页面于是渲染成正常空态。
# spec §9.1 把"黑板文件缺失**或损坏**"并列，两者在恢复路径同解（rebuild），但在
# **展示**路径不同解：展示层不重建，只能如实说"读不出来"。错的空白比读不到更骗人。
# ——————————————————————————————————————————————————————————————

def _state_json_path(ws: Path, tid: str) -> Path:
    """权威状态文件（同 tests/helpers.py 的旁路口径，不碰 store 私有属性）。"""
    return ws / tid / "blackboard" / "state.json"


def _seed_board_state(ws: Path, tid: str) -> Path:
    """经**唯一**落盘路径写一份合法权威状态（apply_blackboard_ops），返回文件路径。"""
    store = orch.store.Store(ws / tid)
    orch.store.apply_blackboard_ops(store, [
        {"op": "set_task", "key": "alpha", "status": "doing"},
        {"op": "freeze_contract", "name": "like-api", "version": 1,
         "path": "docs/like-api.md"},
        {"op": "set_decision", "text": "先做 alpha"},
    ], 1)
    p = _state_json_path(ws, tid)
    assert json.loads(p.read_text(encoding="utf-8"))["tasks"] == {"alpha": "doing"}
    return p


def test_board_endpoint_flags_corrupt_state_json_instead_of_faking_empty(tmp_dir):
    """截半的 state.json → 200 + board_error（键形状不变），**不**伪装成空黑板。"""
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        p = _seed_board_state(tmp_dir, tid)
        good = p.read_text(encoding="utf-8")
        broken = good[: len(good) // 2]
        p.write_text(broken, encoding="utf-8")
        with pytest.raises(ValueError):
            json.loads(broken)      # 自证：截断后真解析不了（否则本用例假绿）

        code, board = _req(base, f"/api/threads/{tid}/board")

        assert code == 200, (code, board)
        err = board.get("board_error")
        assert isinstance(err, str) and err.strip(), f"损坏必须说出来：{board!r}"
        assert "state.json" in err, err
        assert "orch run" in err, f"错误人话应带 §9.1 自救提示：{err!r}"
        assert "\n" not in err, f"人话须压成一行（要进 JSON 与红字）：{err!r}"
        # 报错本身不回抄状态原文（黑板正文含契约路径/决策原文，不宜随报错外传）。
        assert "like-api" not in err, err
        # 空结构照给、键形状只多 board_error 一个键（前端渲染逻辑不用改形状）。
        assert set(board) == {"contracts", "decisions", "tasks", "task_order",
                              "task_evt", "board_error"}, sorted(board)
        assert board["contracts"] == {} and board["decisions"] == []
        assert board["tasks"] == {} and board["task_order"] == []
        assert board["task_evt"] == {}


def test_board_endpoint_missing_state_json_stays_healthy_empty(tmp_dir):
    """对照①：文件**不存在** —— 合法的"还没写过黑板"，健康空态、无 board_error。"""
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        p = _state_json_path(tmp_dir, tid)
        if p.exists():
            p.unlink()
        assert not p.exists()

        code, board = _req(base, f"/api/threads/{tid}/board")

        assert code == 200, (code, board)
        assert "board_error" not in board, f"没写过黑板不是错误：{board!r}"
        assert board == {"contracts": {}, "decisions": [], "tasks": {},
                         "task_order": [], "task_evt": {}}, board


def test_board_endpoint_valid_state_json_payload_unchanged(tmp_dir):
    """对照②：合法权威状态 —— 响应逐字同旧（五键一字不动，无 board_error）。"""
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        _seed_board_state(tmp_dir, tid)

        code, board = _req(base, f"/api/threads/{tid}/board")

        assert code == 200, (code, board)
        assert set(board) == {"contracts", "decisions", "tasks", "task_order",
                              "task_evt"}, sorted(board)
        assert board["tasks"] == {"alpha": "doing"}, board
        assert board["task_order"] == ["alpha"], board
        assert board["contracts"]["like-api"] == {
            "version": 1, "path": "docs/like-api.md", "frozen_at": 1,
        }, board
        assert [d["text"] for d in board["decisions"]] == ["先做 alpha"], board
        # ops 是直接灌的（source_event_id=1 那条是 human/assign，非 A 类），故溯源
        # 键查不到事件号 —— 如实空着，端点不编号（前端出 "—"）。
        assert board["task_evt"] == {}, board


def test_app_js_board_error_routes_into_explicit_failure_state(tmp_dir):
    """前端判据：board_error 走**既有** bd-fail 失败态（不得渲染成正常空态）。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert "data.board_error" in js, "前端须消费端点的 board_error 信号"
        # 与 HTTP 失败同一出口：data 置 null → renderBoard 据 lastBoard===null 出红字。
        assert "return { data: null, err: String(data.board_error) };" in js
        assert "黑板读取失败" in js and "bd-fail" in js
        # 失败文案只进文本位（既有 failMsg 已 escapeHtml），不新增属性位。
        assert "const failMsg = `黑板读取失败：${escapeHtml(lastBoardError" in js


# ——————————————————————————————————————————————————————————————
# (w-9c) 二轮评审 建议10：线程 id 白名单先行 —— ".." 不许穿透到文件系统
#
# 根因：路由把 path 按 "/" 切开后不校验分量（server.py:_route_api），tid=".."
# 一路走到 orch.store.Store(目录)，而它的构造**会建目录**（blackboard/、logs/、
# events.db）—— 一个 GET 就能在 workspace 之外落文件。
# ——————————————————————————————————————————————————————————————

def test_thread_routes_reject_bad_tid_before_touching_disk(tmp_dir):
    """不合白名单的 tid → 404，且 workspace 与其**父目录**都不许多出任何条目。"""
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        ws_before = sorted(p.name for p in tmp_dir.iterdir())
        parent_before = sorted(p.name for p in tmp_dir.parent.iterdir())

        for path in (
            "/api/threads/../status",          # ".." 直接当线程名（父目录 exists() 为真）
            "/api/threads/../board",
            "/api/threads/../x/status",
            "/api/threads/t-%2e%2e/status",    # 百分号编码的 ".."
            "/api/threads/%2e%2e/board",
            "/api/threads/t-bad_name/events",  # 下划线不在白名单（仓内真实命名无它）
        ):
            code, body = _req(base, path)
            assert code == 404, (path, code, body)

        assert sorted(p.name for p in tmp_dir.iterdir()) == ws_before, "workspace 被写脏"
        assert sorted(p.name for p in tmp_dir.parent.iterdir()) == parent_before, (
            "workspace **之外**被落了文件（Store 构造会建目录）"
        )
        # 合法 id（POST /api/threads 与 threads 列表给的就是这一形态）不受影响。
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)


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


# ——————————————————————————————————————————————————————————————
# (w-13) C1 控制台人性化：display_name 纯呈现键 + PUT /api/config 写盘加固
# ——————————————————————————————————————————————————————————————

def _cfg_with_display_names(ws: Path, *, pm_name: str | None = "产品经理") -> None:
    """workspace config：pm 配（或不配）display_name，moderator 一律不配。

    两个 adapter 都是 cli 型且 fallback 已声明 → 过 §11.1 校验（本组用例关心的是
    display_name 的投影，不是校验分支）。
    """
    pm_line = f"    display_name: {pm_name}\n" if pm_name is not None else ""
    (ws / "config.yaml").write_text(
        "adapters:\n"
        "  cli_a:\n"
        "    kind: cli\n"
        "    start_cmd: fake-a -p\n"
        "  cli_b:\n"
        "    kind: cli\n"
        "    start_cmd: fake-b -p\n"
        "roles:\n"
        "  pm:\n"
        "    adapter: cli_a\n"
        + pm_line +
        "    fallback: [cli_b]\n"
        "    can_decide: true\n"
        "  moderator:\n"
        "    adapter: cli_b\n"
        "    can_decide: true\n",
        encoding="utf-8",
    )


def _roles_row(body: dict, role: str) -> dict:
    rows = body.get("roles") or []
    return next(r for r in rows if r["role"] == role)


def test_status_roles_projection_carries_display_name(tmp_dir):
    """配了 display_name → 投影带出中文名；role/primary/effective/blocked 一个不动。"""
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        _cfg_with_display_names(tmp_dir)
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        pm = _roles_row(body, "pm")
        assert pm["display_name"] == "产品经理", pm
        # role id 仍是权威键：显示名绝不顶替它（机器匹配全靠它）。
        assert pm["role"] == "pm", pm
        assert pm["primary"] == "cli_a", pm
        assert pm["effective"] == "cli_a", pm
        assert pm["blocked"] is False, pm


def test_status_roles_display_name_defaults_to_role_id(tmp_dir):
    """没配 display_name（以及 config 里写成空值）→ 等于 role id 本身，不臆造别名。"""
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        _cfg_with_display_names(tmp_dir, pm_name=None)
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        for role in ("pm", "moderator"):
            row = _roles_row(body, role)
            assert row["display_name"] == role, row

        # 空串/null 也一律退回 role id（假值不得渲染成无名 chip）。
        _cfg_with_display_names(tmp_dir, pm_name='""')
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        assert _roles_row(body, "pm")["display_name"] == "pm", body


def test_app_js_display_name_only_in_human_readable_slots(tmp_dir):
    """铁律断言：displayOf 只进文本位；一切机器匹配位仍是 role id。

    错配比不显示更坏——单聊/筛选/路由靠 role id 比对，混进显示名会静默切断整条链。
    故逐一钉住：option.value、筛选 checkbox 的 value、dataset.member、CSS 类 r-{role}、
    isMemberRelated/activeMemberRole 的比较键。
    """
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert "function displayOf(role)" in js

        # ① #send-to 的 option：value=role id，只有 textContent 换显示名。
        assert "o.value = r; o.textContent = displayOf(r);" in js
        assert "o.value = displayOf" not in js

        # ② 筛选 chip：checkbox value 仍走 escapeHtmlAttr(r)（原文 id），标签才用显示名。
        fn = js.split("function populateFilterRoles(roles)", 1)
        assert len(fn) == 2, "populateFilterRoles 应存在"
        body = fn[1].split("\n}\n", 1)[0]
        assert 'value="${escapeHtmlAttr(r)}"' in body, body
        assert "escapeHtml(displayOf(r))" in body, body
        assert "value=\"${escapeHtmlAttr(displayOf" not in body, body

        # ③ 名册 chip：dataset 与高亮比较键都是 role id，只有 .mr-name 用显示名。
        fn = js.split("function renderMemberRoster()", 1)
        assert len(fn) == 2, "renderMemberRoster 应存在"
        body = fn[1].split("\n}\n", 1)[0]
        assert 'data-member="${escapeHtmlAttr(role)}"' in body, body
        assert 'active === role ? " active" : ""' in body, body
        assert 'class="mr-name" style="color:${col}">${escapeHtml(displayOf(role))}' in body, body

        # ④ 单聊比较键 / 焦点判据 / CSS 类：一处 displayOf 都不许出现。
        for fn_name in ("function isMemberRelated(ev, role)",
                        "function activeMemberRole()",
                        "function toggleMemberChat(role)",
                        "function readFilters()"):
            assert fn_name in js, fn_name
            body = js.split(fn_name, 1)[1].split("\n}\n", 1)[0]
            assert "displayOf" not in body, (fn_name, body)
        assert "div.className = `bubble r-${ev.sender" in js, "CSS 类必须仍用 role id"

        # ⑤ display_name 是用户可控文本 → 每处用法必须过转义：要么 escapeHtml(displayOf(…))
        # 进 innerHTML，要么赋给 textContent（DOM 自动转义）。裸 ${displayOf(…)} 拼进
        # innerHTML 就是一个 XSS 缺口（config.yaml 可写 → 可控）。
        allowed_prefixes = ("escapeHtml(", "textContent = ", "function ")
        naked = []
        idx = js.find("displayOf(")
        while idx != -1:
            head = js[:idx]
            if not any(head.endswith(p) for p in allowed_prefixes):
                naked.append(js[max(0, idx - 60):idx + 30])
            idx = js.find("displayOf(", idx + 1)
        assert not naked, f"displayOf 未过转义的用法：{naked}"
        assert js.count("escapeHtml(displayOf(") >= 5, js.count("escapeHtml(displayOf(")


def test_app_js_member_roster_always_shows_effective_binding(tmp_dir):
    """名册常显生效 CLI 名：降级中标 primary→effective，blocked 沿用 ⛔，读不出来写"绑定未知"。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        assert "function memberBindLine(row)" in js
        body = js.split("function memberBindLine(row)", 1)[1].split("\n}\n", 1)[0]
        assert "降级中" in body, body
        assert "${primary}→${effective}" in body, body
        assert "⛔ 无可用适配器" in body, body
        assert "绑定未知" in body, body
        # 常显（不是"有话要说才渲染"）：正常态也返回一枚带 effective 的小字。
        assert "return sub(\"\", effective);" in body, body
        # 名册侧新增，#role-bindings 的既有"无话不渲染"逻辑不动。
        rb = js.split("function renderRoleBindings()", 1)[1].split("\n}\n", 1)[0]
        assert 'if (!notable.length) { el.innerHTML = ""; return; }' in rb, rb
        code, css = _req(base, "/styles.css")
        assert code == 200, code
        assert ".mr-sub" in css and ".mr-sub.degraded" in css and ".mr-sub.blocked" in css
        # glass 风格红线：本次新增不得引入 backdrop-filter。
        sub_css = css.split(".mr-sub {", 1)[1].split("}", 1)[0]
        assert "backdrop-filter" not in sub_css, sub_css


_BAD_FALLBACK_YAML = (
    "adapters:\n"
    "  cli_a:\n"
    "    kind: cli\n"
    "    start_cmd: fake-a -p\n"
    "roles:\n"
    "  pm:\n"
    "    adapter: cli_a\n"
    "    fallback: [no_such_adapter]\n"
)


def test_config_put_rejects_semantically_invalid_config_bytewise_unchanged(tmp_dir):
    """能解析 ≠ 合法：fallback 指向未声明 adapter → 400 人话，盘上文件**逐字节**未变。

    §11.1 校验前置到写盘之前的理由：常驻 orch run 每轮重读 config，坏配置一旦落盘就会
    让它持续拒绝启动，而控制台此前会回 ok:true 让运维以为保存成功了。
    """
    good = "adapters: {}\nroles: {}\n"
    (tmp_dir / "config.yaml").write_text(good, encoding="utf-8")
    before = (tmp_dir / "config.yaml").read_bytes()
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/config", "PUT", {"yaml": _BAD_FALLBACK_YAML})
        assert code == 400, (code, body)
        assert "error" in body, body
        assert "no_such_adapter" in body["error"], body
        assert "§11.1" in body["error"], body
    after = (tmp_dir / "config.yaml").read_bytes()
    assert after == before, "校验不通过必须一个字节都不写"
    # 临时文件不得残留（原子替换失败/未发生都不留残迹）。
    assert not list(tmp_dir.glob(".config.yaml.*.tmp")), list(tmp_dir.glob(".config.yaml.*"))


def test_config_put_writes_atomically_via_os_replace(tmp_dir):
    """合法 → 写入且走原子替换（tmp + flush + fsync + os.replace），无 tmp 残留。

    代码断言 os.replace：非原子写会让常驻 run 读到半份文件（静默降级 Fake 或进程退出），
    这条判据只能在源码层钉住——HTTP 层看不出写法。
    """
    import inspect

    from orch.web.server import _atomic_write_bytes, _ep_config_put

    # C3 起原子写收口成**唯一**一个 helper（行级手术端点复用同一份，不拷第二套写盘）。
    # 三条机制断言随之落到 helper 上；再加一条"端点必须委托给它"，堵住"另开一条写路"。
    src = inspect.getsource(_atomic_write_bytes)
    assert "os.replace(tmp, target)" in src, src
    assert "os.fsync(fh.fileno())" in src, src
    assert "fh.flush()" in src, src
    put_src = inspect.getsource(_ep_config_put)
    assert "_atomic_write_bytes(" in put_src, put_src
    for s in (src, put_src):
        assert ".write_text(" not in s, "写盘不得走非原子的 write_text"

    good = (
        "adapters:\n"
        "  cli_a:\n"
        "    kind: cli\n"
        "    start_cmd: fake-a -p\n"
        "roles:\n"
        "  pm:\n"
        "    adapter: cli_a\n"
        "    display_name: 产品经理\n"
        "    fallback: [cli_a]\n"
    )
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/config", "PUT", {"yaml": good})
        assert code == 200, (code, body)
        assert body.get("ok") is True, body
        # 内容与请求正文**逐字节**相同（不加 BOM、不改内容、不改换行）。C3 起写盘走
        # bytes：文本模式在 Windows 上会把 \n 悄悄转成 \r\n，而行级手术端点必须逐字节
        # 保真——同一份文件上两条写路各持一套换行策略，会让"改一行"与"整存一次"互相
        # 把对方的换行全文重写。故这里从 read_text 收紧成 read_bytes。
        raw_bytes = (tmp_dir / "config.yaml").read_bytes()
        assert not raw_bytes.startswith(b"\xef\xbb\xbf"), "不得写 BOM"
        assert raw_bytes == good.encode("utf-8"), raw_bytes
        assert not list(tmp_dir.glob(".config.yaml.*.tmp")), list(tmp_dir.glob(".config.yaml.*"))
        # 读回一致（display_name 是纯呈现键，PUT 不因它报错）。
        code, body = _req(base, "/api/config")
        assert code == 200, (code, body)
        assert body["yaml"] == good, body


# ——————————————————————————————————————————————————————————————
# (w-14) C3 每角色选 CLI / 选模型：/status 投影加 model + POST /api/config/role-binding
#        （config.yaml 的**行级外科手术**：只改目标行的值，其余逐字节不动）
# ——————————————————————————————————————————————————————————————

# 块式样本：adapter 行与 model 行各带一句行内注释（注释保全是本卡硬指标）。
_BLOCK_CFG = (
    "# 顶部说明：这一行必须原样活着\n"
    "adapters:\n"
    "  cli_a:\n"
    "    kind: cli\n"
    "    start_cmd: fake-a -p\n"
    "  cli_b:\n"
    "    kind: cli\n"
    "    start_cmd: fake-b -p\n"
    "    model: b-default\n"
    "roles:\n"
    "  pm:\n"
    "    adapter: cli_a      # 主绑定：这句注释必须活着\n"
    "    display_name: 产品经理\n"
    "    can_decide: true\n"
    "  moderator:\n"
    "    adapter: cli_b\n"
    "    model: old-model    # 模型行的注释\n"
    "    can_decide: true\n"
    "thread_defaults:\n"
    "  max_rounds: 16\n"
)

# 内联式样本（演示床 hetero-ws 的真实写法：角色整条挤在一对花括号里 + 行尾注释）。
_INLINE_CFG = (
    "adapters:\n"
    "  cli_a: {kind: cli, start_cmd: fake-a -p}\n"
    "  cli_b: {kind: cli, start_cmd: fake-b -p}\n"
    "roles:\n"
    "  pm:        {adapter: cli_a, display_name: 产品经理, fallback: [cli_b],"
    " can_decide: true}   # 这条行尾注释必须活着\n"
    "  moderator: {adapter: cli_b, model: old-model, can_decide: true}\n"
)

# 命令行含 {model} 占位的 adapter：换绑到它而两层都没模型值 → §11.1 fail-closed。
_PLACEHOLDER_CFG = (
    "adapters:\n"
    "  cli_a:\n"
    "    kind: cli\n"
    "    start_cmd: fake-a -p\n"
    "  cli_m:\n"
    "    kind: cli\n"
    "    start_cmd: fake-m -m {model} -p\n"
    "roles:\n"
    "  pm:\n"
    "    adapter: cli_a\n"
)


def _write_cfg(ws: Path, text: str) -> bytes:
    (ws / "config.yaml").write_bytes(text.encode("utf-8"))
    return (ws / "config.yaml").read_bytes()


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _bind(base: str, **body):
    return _req(base, "/api/config/role-binding", "POST", body)


def _cfg_text(ws: Path) -> str:
    return (ws / "config.yaml").read_bytes().decode("utf-8")


def test_role_binding_block_replaces_adapter_value_and_keeps_comment(tmp_dir):
    """块式：只有 adapter 那一行的**值**变了；行内注释与其余全文逐字节不动。"""
    before = _write_cfg(tmp_dir, _BLOCK_CFG)
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role="pm", adapter="cli_b")
        assert code == 200, (code, body)
        assert body.get("ok") is True, body

    after = _cfg_text(tmp_dir)
    assert after == _BLOCK_CFG.replace(
        "    adapter: cli_a      # 主绑定：这句注释必须活着\n",
        "    adapter: cli_b      # 主绑定：这句注释必须活着\n",
    ), after
    # 反面钉死：注释没被吞、别的角色没被波及、顶部说明还在。
    assert "# 主绑定：这句注释必须活着" in after
    assert "# 顶部说明：这一行必须原样活着" in after
    assert "    model: old-model    # 模型行的注释\n" in after
    # 差异只有一行（证明不是"重排全文恰好等价"）。
    diff = [(a, b) for a, b in zip(before.decode("utf-8").splitlines(),
                                   after.splitlines()) if a != b]
    assert len(diff) == 1, diff
    assert not list(tmp_dir.glob(".config.yaml.*.tmp"))


def test_role_binding_block_inserts_model_line_after_adapter(tmp_dir):
    """块式：角色原本没有 model 键 → 紧跟 adapter 行之后、同缩进插入一行。"""
    _write_cfg(tmp_dir, _BLOCK_CFG)
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role="pm", model="opencode/big-pickle")
        assert code == 200, (code, body)

    after = _cfg_text(tmp_dir)
    assert after == _BLOCK_CFG.replace(
        "    adapter: cli_a      # 主绑定：这句注释必须活着\n",
        "    adapter: cli_a      # 主绑定：这句注释必须活着\n"
        "    model: opencode/big-pickle\n",
    ), after
    # 插入位置与缩进都钉死（不是"随便找个地方塞进去"）。
    lines = after.splitlines()
    i = lines.index("    model: opencode/big-pickle")
    assert lines[i - 1].startswith("    adapter: cli_a"), lines[i - 3:i + 2]


def test_role_binding_block_model_null_deletes_the_key(tmp_dir):
    """block 式 model=null → 整行删掉（回落 adapter 层缺省），其余逐字节不动。"""
    _write_cfg(tmp_dir, _BLOCK_CFG)
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role="moderator", model=None)
        assert code == 200, (code, body)

    after = _cfg_text(tmp_dir)
    assert after == _BLOCK_CFG.replace("    model: old-model    # 模型行的注释\n", ""), after
    assert "old-model" not in after
    # 删了 role 层的键，adapter 层的缺省还在（这正是"回落"的落点）。
    assert "    model: b-default\n" in after


def test_role_binding_inline_replaces_adapter_value_and_keeps_comment(tmp_dir):
    """内联式（单行花括号）：只换花括号内 adapter 项的值；行尾注释与其余项原样。"""
    _write_cfg(tmp_dir, _INLINE_CFG)
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role="pm", adapter="cli_b")
        assert code == 200, (code, body)

    after = _cfg_text(tmp_dir)
    assert after == _INLINE_CFG.replace("{adapter: cli_a,", "{adapter: cli_b,"), after
    assert "# 这条行尾注释必须活着" in after
    # 花括号内其余项（含那串对齐空格）一个字符都没动。
    assert "display_name: 产品经理, fallback: [cli_b], can_decide: true}" in after


def test_role_binding_inline_inserts_model_item_after_adapter(tmp_dir):
    """内联式：没有 model 项 → 在花括号内 adapter 项之后插 `, model: <值>`。"""
    _write_cfg(tmp_dir, _INLINE_CFG)
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role="pm", adapter="cli_b", model="grok-4.5-latest")
        assert code == 200, (code, body)

    after = _cfg_text(tmp_dir)
    assert after == _INLINE_CFG.replace(
        "{adapter: cli_a,", "{adapter: cli_b, model: grok-4.5-latest,"), after
    assert "# 这条行尾注释必须活着" in after
    import yaml as _yaml
    pm = _yaml.safe_load(after)["roles"]["pm"]
    assert pm["adapter"] == "cli_b" and pm["model"] == "grok-4.5-latest", pm
    assert pm["fallback"] == ["cli_b"] and pm["display_name"] == "产品经理", pm


def test_role_binding_inline_model_null_removes_the_item(tmp_dir):
    """内联式 model=null → 整项连同分隔逗号消失，其余项原样。"""
    _write_cfg(tmp_dir, _INLINE_CFG)
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role="moderator", model=None)
        assert code == 200, (code, body)

    after = _cfg_text(tmp_dir)
    assert after == _INLINE_CFG.replace(
        "{adapter: cli_b, model: old-model, can_decide: true}",
        "{adapter: cli_b, can_decide: true}"), after


def test_role_binding_unknown_role_is_404(tmp_dir):
    """role 不在该 config 的 roles 集合里 → 404 人话（不是 400：那是"没这个东西"）。"""
    before = _write_cfg(tmp_dir, _BLOCK_CFG)
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role="nobody", adapter="cli_a")
        assert code == 404, (code, body)
        assert "nobody" in body["error"], body
    assert (tmp_dir / "config.yaml").read_bytes() == before


def test_role_binding_undeclared_adapter_is_400(tmp_dir):
    """adapter 不在已声明集合里 → 400 人话，盘上一个字节不动（写进去也是启动即报错）。"""
    before = _write_cfg(tmp_dir, _BLOCK_CFG)
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role="pm", adapter="no_such_cli")
        assert code == 400, (code, body)
        assert "no_such_cli" in body["error"], body
    assert (tmp_dir / "config.yaml").read_bytes() == before
    assert not list(tmp_dir.glob(".config.yaml.*.tmp"))


@pytest.mark.parametrize("cfg_text, role", [
    # 角色的值是个裸标量（既非缩进块也非花括号）。
    ("adapters:\n  cli_a: {kind: cli, start_cmd: fake-a -p}\n"
     "  cli_b: {kind: cli, start_cmd: fake-b -p}\nroles:\n  pm: cli_a\n", "pm"),
    # 当前值带引号：不是裸标量，token 边界不敢认。
    ("adapters:\n  cli_a: {kind: cli, start_cmd: fake-a -p}\n"
     "  cli_b: {kind: cli, start_cmd: fake-b -p}\n"
     "roles:\n  pm:\n    adapter: 'cli_a'\n", "pm"),
    # 流式列表跨行：块内出现不是 `键: 值` 的续行。
    ("adapters:\n  cli_a: {kind: cli, start_cmd: fake-a -p}\n"
     "  cli_b: {kind: cli, start_cmd: fake-b -p}\n"
     "roles:\n  pm:\n    fallback: [\n      cli_b,\n    ]\n    adapter: cli_a\n", "pm"),
    # 角色根本没有显式 adapter 行（主绑定按角色名兜底）：没有值可替，不猜着插。
    ("adapters:\n  cli_b: {kind: cli, start_cmd: fake-b -p}\n"
     "roles:\n  pm:\n    can_decide: true\n", "pm"),
])
def test_role_binding_unrecognized_shape_400_bytewise_unchanged(tmp_dir, cfg_text, role):
    """认不出的写法一律 400 + 一句人话，且**盘上逐字节不变**——宁 400 不猜。"""
    before = _write_cfg(tmp_dir, cfg_text)
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role=role, adapter="cli_b")
        assert code == 400, (code, body)
        assert "无法安全自动修改" in body["error"], body
        assert "手工编辑" in body["error"], body
    assert (tmp_dir / "config.yaml").read_bytes() == before
    assert not list(tmp_dir.glob(".config.yaml.*.tmp"))


def test_role_binding_model_placeholder_without_value_400(tmp_dir):
    """换到命令行含 {model} 占位的 adapter 而两层都没模型值 → §11.1 校验拦下，400 转人话。

    这一条是 C2 的 fail-closed 在改绑路径上的闸门：坏配置绝不允许先落盘再报错——
    常驻 `orch run` 每轮重读 config，落了盘它就持续拒绝启动。
    """
    before = _write_cfg(tmp_dir, _PLACEHOLDER_CFG)
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role="pm", adapter="cli_m")
        assert code == 400, (code, body)
        assert "§11.1" in body["error"], body
        assert "{model}" in body["error"], body
        assert "cli_m" in body["error"], body
        assert (tmp_dir / "config.yaml").read_bytes() == before, "校验不过必须一个字节不写"

        # 同一次请求里连模型一起给 → 过闸并落盘（证明拦的是"缺值"不是"这个 adapter"）。
        code, body = _bind(base, role="pm", adapter="cli_m", model="m-1")
        assert code == 200, (code, body)
    after = _cfg_text(tmp_dir)
    assert "    adapter: cli_m\n    model: m-1\n" in after, after


def test_role_binding_requires_at_least_one_field(tmp_dir):
    """adapter 与 model 一个都不给 → 400（避免"什么都没说"的空写请求）。"""
    before = _write_cfg(tmp_dir, _BLOCK_CFG)
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role="pm")
        assert code == 400, (code, body)
    assert (tmp_dir / "config.yaml").read_bytes() == before


def test_role_binding_no_op_does_not_touch_disk(tmp_dir):
    """删一个本就不存在的 model 键 = 无实质改动：200 + changed=false，且不写盘。"""
    before = _write_cfg(tmp_dir, _BLOCK_CFG)
    mtime = (tmp_dir / "config.yaml").stat().st_mtime_ns
    with _Serving(tmp_dir) as base:
        code, body = _bind(base, role="pm", model=None)
        assert code == 200, (code, body)
        assert body.get("changed") is False, body
    assert (tmp_dir / "config.yaml").read_bytes() == before
    assert (tmp_dir / "config.yaml").stat().st_mtime_ns == mtime


def test_status_roles_projection_carries_effective_model(tmp_dir):
    """/status 的 roles[] 带 model：role 层优先 → 主绑定 adapter 层缺省 → 都没有则 None。"""
    (tmp_dir / "config.yaml").write_text(
        "adapters:\n"
        "  cli_a:\n"
        "    kind: cli\n"
        "    start_cmd: fake-a -p\n"
        "  cli_b:\n"
        "    kind: cli\n"
        "    start_cmd: fake-b -p\n"
        "    model: b-default\n"
        "roles:\n"
        "  pm:\n"
        "    adapter: cli_b\n"
        "    model: pm-pinned\n"
        "  moderator:\n"
        "    adapter: cli_b\n"
        "  tester:\n"
        "    adapter: cli_a\n",
        encoding="utf-8",
    )
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator", "tester"])
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        assert _roles_row(body, "pm")["model"] == "pm-pinned"        # role 层赢
        assert _roles_row(body, "moderator")["model"] == "b-default"  # 回落 adapter 层
        assert _roles_row(body, "tester")["model"] is None            # 两层都没有
        # 语义键一个不动（加键不是改名）。
        for role in ("pm", "moderator", "tester"):
            row = _roles_row(body, role)
            for key in ("role", "display_name", "primary", "effective", "blocked"):
                assert key in row, row


def test_role_binding_change_is_visible_in_status_immediately(tmp_dir):
    """端到端：改绑后同一进程的 /status 立刻反映新 adapter 与新 model（零缓存，§16.9）。"""
    _write_cfg(tmp_dir, _BLOCK_CFG)
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert _roles_row(body, "pm")["primary"] == "cli_a", body
        assert _roles_row(body, "pm")["model"] is None, body

        code, body = _bind(base, role="pm", adapter="cli_b", model="pinned-1")
        assert code == 200, (code, body)

        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        pm = _roles_row(body, "pm")
        assert pm["primary"] == "cli_b", pm
        assert pm["effective"] == "cli_b", pm
        assert pm["model"] == "pinned-1", pm


def test_app_js_member_binding_editor(tmp_dir):
    """名册内就地改绑：⚙ + CLI 下拉 + 模型 datalist + 保存走真实 POST + 失败在名片内红字。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code

        # ① ⚙ 与面板是 chip 的兄弟（chip 是 <button>，嵌控件会把点击冒泡成单聊）。
        assert 'class="mr-edit" data-edit-role=' in js
        assert "function memberEditPanel(row)" in js
        # ② CLI 下拉的 options 来自 GET /api/adapters 的 name 列表，当前主绑定选中。
        assert "adapterRows.map((a) => String(a.name))" in js
        assert 'n === cur ? " selected" : ""' in js
        # ③ 保存 = 真实 POST（不是假按钮）；成功后立刻重拉 /status 刷新副标题。
        assert 'api("/api/config/role-binding", { method: "POST", body })' in js
        assert "await loadStatus();" in js.split("async function saveMemberBinding", 1)[1]
        # ④ 失败：后端人话原样进名片内红字，不 toast 走掉、面板不关。
        save = js.split("async function saveMemberBinding", 1)[1].split("\n}\n", 1)[0]
        assert "memberEditErr = e.message;" in save, save
        assert 'pop.querySelector(".mre-err")' in save, save
        # ⑤ 模型候选：四家族 + 猜不出给并集；注明只影响提示不影响可填值。
        assert "function modelCandidates(adapterName)" in js
        for probe in ("grok-4.5-latest", "kimi-code/k3-256k", "opencode/big-pickle",
                      "qwen/qwen3.8-max-preview", "fable"):
            assert probe in js, probe
        assert "候选仅供参考，可手输任意值" in js
        assert "不影响可填值" in js, "必须写明子串猜测只影响候选提示"
        # ⑥ 副标题追加生效模型（有 model 才出）。
        assert "function memberModelLine(row)" in js
        mm = js.split("function memberModelLine(row)", 1)[1].split("\n}\n", 1)[0]
        assert 'if (!m || lastConfigError) return "";' in mm, mm
        assert "escapeHtml(m)" in mm and "escapeHtmlAttr(" in mm, mm
        # ⑦ 转义口径：属性位 escapeHtmlAttr、文本位 escapeHtml（model 值来自 config，可控）。
        panel = js.split("function memberEditPanel(row)", 1)[1].split("\n}\n", 1)[0]
        assert 'value="${escapeHtmlAttr(model)}"' in panel, panel
        assert 'data-edit-role="${escapeHtmlAttr(role)}"' in panel, panel
        # ⑧ 名册每 1.5s 一拍：编辑区展开时暂停整栏重绘，否则模型名打不完。
        roster = js.split("function renderMemberRoster()", 1)[1].split("\n}\n", 1)[0]
        assert "memberEditRole" in roster, roster
        # ⑨ 冷启动头一拍（心跳还没回来）就点 ⚙：**先补 adapterRows 再展开**——展开后
        #    整栏冻结重绘，晚到的列表补不进来，下拉会只剩当前那一个，看着像"无处可选"。
        tog = js.split("async function toggleMemberEdit(role)", 1)[1].split("\n}\n", 1)[0]
        assert "await loadAdapters(true);" in tog, tog

        code, css = _req(base, "/styles.css")
        assert code == 200, code
        for sel in (".mr-item", ".mr-edit-pop", ".mre-row", ".mre-err", ".mr-model"):
            assert sel in css, sel
        # glass 红线：新增浮层复用 .glass-pop，不得自己再引 backdrop-filter。
        pop_css = css.split(".mr-edit-pop {", 1)[1].split("}", 1)[0]
        assert "backdrop-filter" not in pop_css, pop_css


def test_app_js_typing_pill_uses_display_name(tmp_dir):
    """走字胶囊的角色名是纯文本位 → 过 displayOf；typingRows 里存的仍是 role id。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        pill = js.split("function renderTypingPill()", 1)[1].split("\n}\n", 1)[0]
        assert "escapeHtml(displayOf(r.role))" in pill, pill
        assert "escapeHtml(r.role)" not in pill, pill
        # 机器判据不动：分组键仍是 String(d.target) 的 role id。
        bar = js.split("function updateTypingBar(s)", 1)[1].split("\n}\n", 1)[0]
        assert "const role = String(d.target);" in bar, bar
        assert "displayOf" not in bar, bar


# ——————————————————————————————————————————————————————————————
# (w-15) C3 评审回环：两条配置写路的并发防线（进程内锁 + 跨源 CAS）、
#        行级改写第二道闸的可单测化、GET/PUT 往返保真、⚙ 备胎提示
# ——————————————————————————————————————————————————————————————

def test_config_writes_serialize_under_lock_no_lost_update(tmp_dir):
    """两个改**不同角色**的并发 POST 必须都落盘（应修1a）。

    无锁时二者各自读到同一份旧全文、各改各的一行、后写者整份覆盖前者，而**两个请求
    都回 ok:true** —— 静默丢更新（评审真并发实测 12 轮全中）。这里同样跑 12 轮，每轮
    用 Barrier 把两个线程对齐到同一瞬间发车，再断言两条改动同时在盘上。
    """
    import threading as _th

    with _Serving(tmp_dir) as base:
        for i in range(12):
            _write_cfg(tmp_dir, _BLOCK_CFG)
            results = {}
            gate = _th.Barrier(2, timeout=10)

            def hit(role, value):
                gate.wait()
                results[role] = _bind(base, role=role, model=value)

            threads = [_th.Thread(target=hit, args=("pm", f"pm-{i}")),
                       _th.Thread(target=hit, args=("moderator", f"mod-{i}"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
                assert not t.is_alive(), "并发请求卡死（锁用错了？）"

            assert sorted(results) == ["moderator", "pm"], results
            assert all(code == 200 for code, _ in results.values()), results
            text = _cfg_text(tmp_dir)
            # 两条都在 = 没有一条被对方整份覆盖掉。
            assert f"model: pm-{i}" in text, f"第 {i} 轮丢了 pm 的改动：\n{text}"
            assert f"model: mod-{i}" in text, f"第 {i} 轮丢了 moderator 的改动：\n{text}"
            # 谁都没把对方的注释/结构带走。
            assert "# 主绑定：这句注释必须活着" in text
    assert not list(tmp_dir.glob(".config.yaml.*.tmp"))


def test_config_put_cas_409_on_stale_full_text(tmp_dir):
    """配置页载入 → ⚙ 改绑 → 再点保存：陈旧全文必须 409，不许整份盖掉 ⚙ 的改动（应修1b）。

    这条路上两个请求**根本不并发**（人点的，隔着几分钟），进程内锁一点忙帮不上；
    只有比较交换拦得住。
    """
    _write_cfg(tmp_dir, _BLOCK_CFG)
    with _Serving(tmp_dir) as base:
        code, got = _req(base, "/api/config")
        assert code == 200, (code, got)
        assert isinstance(got.get("fingerprint"), str) and got["fingerprint"], got
        stale_text, stale_fp = got["yaml"], got["fingerprint"]

        # 别处（名册 ⚙）改了绑 —— 盘上指纹随之变了。
        code, _ = _bind(base, role="pm", adapter="cli_b")
        assert code == 200
        after_gear = _cfg_text(tmp_dir)
        assert "adapter: cli_b" in after_gear

        code, body = _req(base, "/api/config", "PUT",
                          {"yaml": stale_text, "base_fingerprint": stale_fp})
        assert code == 409, (code, body)
        assert "已被别处修改" in body["error"], body
        assert _cfg_text(tmp_dir) == after_gear, "409 必须一个字节都不写"

        # 重新载入拿到新基线 → 同一份全文这次存得进去（人明确选择覆盖）。
        code, fresh = _req(base, "/api/config")
        assert code == 200
        code, body = _req(base, "/api/config", "PUT",
                          {"yaml": stale_text, "base_fingerprint": fresh["fingerprint"]})
        assert code == 200, (code, body)
        assert body["fingerprint"] == _sha256_text(stale_text), body
    assert _cfg_text(tmp_dir) == stale_text


def test_config_put_cas_treats_missing_file_as_a_real_baseline(tmp_dir):
    """空串指纹 = "我载入时这里还没有文件"，也是**有效基线**：别人抢先建出来就得 409。"""
    with _Serving(tmp_dir) as base:
        code, got = _req(base, "/api/config")
        assert code == 200 and got["exists"] is False, got
        assert got["fingerprint"] == "", got

        # 别处先建了一份。
        _write_cfg(tmp_dir, _BLOCK_CFG)
        code, body = _req(base, "/api/config", "PUT",
                          {"yaml": "adapters: {}\nroles: {}\n", "base_fingerprint": ""})
        assert code == 409, (code, body)
        assert _cfg_text(tmp_dir) == _BLOCK_CFG


def test_config_put_without_base_fingerprint_keeps_old_behavior(tmp_dir):
    """不带 base_fingerprint（老前端 / 脚本）→ 行为逐字同旧：无条件覆盖，不 409。"""
    _write_cfg(tmp_dir, _BLOCK_CFG)
    good = "adapters: {}\nroles: {}\n"
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/config", "PUT", {"yaml": good})
        assert code == 200, (code, body)
        assert body.get("ok") is True, body
    assert _cfg_text(tmp_dir) == good


def test_config_get_put_roundtrip_preserves_crlf(tmp_dir):
    """GET→PUT 往返逐字节保真（建议3）：GET 读字节解码，不做通用换行归一。

    原缺陷：GET 走 read_text（\\r\\n → \\n）而 PUT 逐字节写，于是"看一眼配置再存回去"
    就把盘上一份 CRLF 文件整份翻成 LF —— 全文 diff 噪音，且与行级手术的保真口径打架。
    """
    crlf = _BLOCK_CFG.replace("\n", "\r\n").encode("utf-8")
    (tmp_dir / "config.yaml").write_bytes(crlf)
    with _Serving(tmp_dir) as base:
        code, got = _req(base, "/api/config")
        assert code == 200, (code, got)
        assert "\r\n" in got["yaml"], "GET 不得把盘上的 CRLF 归一成 LF"
        assert got["fingerprint"] == _sha256_bytes(crlf), got
        code, body = _req(base, "/api/config", "PUT",
                          {"yaml": got["yaml"], "base_fingerprint": got["fingerprint"]})
        assert code == 200, (code, body)
    assert (tmp_dir / "config.yaml").read_bytes() == crlf, "往返必须逐字节不变"


# —— 应修2：行级改写第二道闸抽成纯函数后的**真覆盖**（删闸必红）——

_VERIFY_BEFORE = (
    "roles:\n"
    "  pm:\n"
    "    adapter: cli_a\n"
    "  moderator:\n"
    "    adapter: cli_b\n"
)


def test_verify_surgical_rewrite_accepts_a_clean_rewrite():
    """干净改写：只有目标键变了 → 放行，并把解析产物给调用方（省一次 safe_load）。"""
    from orch.web.server import _verify_surgical_rewrite

    after_raw = _VERIFY_BEFORE.replace("    adapter: cli_a\n", "    adapter: cli_b\n")
    doc = _verify_surgical_rewrite(_VERIFY_BEFORE, after_raw, "pm", {"adapter": "cli_b"})
    assert doc["roles"]["pm"]["adapter"] == "cli_b"
    assert doc["roles"]["moderator"]["adapter"] == "cli_b"


def test_verify_surgical_rewrite_rejects_collateral_change_on_another_role():
    """模拟替换器定位错行：目标值改对了，却顺手把**另一个角色**的一行也改了。

    字节层面这仍是"只改了两行"，diff 看着人畜无害——只有语义比对能把它抓出来。
    """
    from orch.web.server import _ApiError, _verify_surgical_rewrite

    polluted = (
        "roles:\n"
        "  pm:\n"
        "    adapter: cli_b\n"          # 目标：改对了
        "  moderator:\n"
        "    adapter: cli_zzz\n"        # 波及：这一行根本不该动
    )
    with pytest.raises(_ApiError) as ei:
        _verify_surgical_rewrite(_VERIFY_BEFORE, polluted, "pm", {"adapter": "cli_b"})
    assert ei.value.status == 400
    assert "波及" in ei.value.message, ei.value.message


def test_verify_surgical_rewrite_rejects_extra_key_in_target_role():
    """波及面也包括**目标角色自己的别的键**：多插一个 can_decide 同样要拦。"""
    from orch.web.server import _ApiError, _verify_surgical_rewrite

    polluted = (
        "roles:\n"
        "  pm:\n"
        "    adapter: cli_b\n"
        "    can_decide: true\n"        # 原文没有这一行
        "  moderator:\n"
        "    adapter: cli_b\n"
    )
    with pytest.raises(_ApiError):
        _verify_surgical_rewrite(_VERIFY_BEFORE, polluted, "pm", {"adapter": "cli_b"})


def test_verify_surgical_rewrite_rejects_wrong_or_undeleted_target_value():
    """目标键本身没变成想要的样子：值不对 / 该删的没删掉，两种都拦。"""
    from orch.web.server import _ApiError, _verify_surgical_rewrite

    wrong = _VERIFY_BEFORE.replace("    adapter: cli_a\n", "    adapter: cli_c\n")
    with pytest.raises(_ApiError) as ei:
        _verify_surgical_rewrite(_VERIFY_BEFORE, wrong, "pm", {"adapter": "cli_b"})
    assert "与请求不符" in ei.value.message, ei.value.message

    kept = (
        "roles:\n"
        "  pm:\n"
        "    adapter: cli_a\n"
        "    model: m1\n"
        "  moderator:\n"
        "    adapter: cli_b\n"
    )
    with pytest.raises(_ApiError) as ei:
        _verify_surgical_rewrite(kept, kept, "pm", {"model": None})
    assert "没能删掉" in ei.value.message, ei.value.message


def test_role_binding_write_path_is_locked_and_verified():
    """HTTP 写路径确实经过这两道闸（删掉任一道，本条即红）。"""
    import inspect

    from orch.web.server import (_apply_role_binding, _ep_config_put,
                                 _ep_config_role_binding)

    apply_src = inspect.getsource(_apply_role_binding)
    assert "_verify_surgical_rewrite(" in apply_src, apply_src
    # 自证在写盘**之前**（顺序错了等于没闸）。
    assert (apply_src.index("_verify_surgical_rewrite(")
            < apply_src.index("_atomic_write_bytes(")), apply_src
    assert "with _CONFIG_WRITE_LOCK:" in inspect.getsource(_ep_config_role_binding)
    assert "with _CONFIG_WRITE_LOCK:" in inspect.getsource(_ep_config_put)


def test_status_roles_projection_carries_fallback(tmp_dir):
    """/status 的 roles[] 带 fallback 原样名单（⚙ 面板的备胎提示要点名，建议4）。"""
    (tmp_dir / "config.yaml").write_text(
        "adapters:\n"
        "  cli_a:\n"
        "    kind: cli\n"
        "    start_cmd: fake-a -p\n"
        "  cli_b:\n"
        "    kind: cli\n"
        "    start_cmd: fake-b -p\n"
        "roles:\n"
        "  pm:\n"
        "    adapter: cli_a\n"
        "    fallback: [cli_b, cli_a]\n"
        "  moderator:\n"
        "    adapter: cli_b\n",
        encoding="utf-8",
    )
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base, roles=["pm", "moderator"])
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        assert _roles_row(body, "pm")["fallback"] == ["cli_b", "cli_a"]
        assert _roles_row(body, "moderator")["fallback"] == []


def test_app_js_config_page_does_cas_with_fingerprint(tmp_dir):
    """配置页：载入存指纹、保存带上、409 走"提示 + 重新载入"（应修1b 前端半边）。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        load = js.split("async function loadConfig()", 1)[1].split("\n}\n", 1)[0]
        assert "configFingerprint =" in load, load
        save = js.split("async function saveConfig()", 1)[1].split("\n}\n", 1)[0]
        assert "body.base_fingerprint = configFingerprint;" in save, save
        assert "e.status === 409" in save, save
        assert "await loadConfig();" in save, save
        # 有未保存手改时**不**拿盘上版本覆盖它（刷新不能吃掉人正在写的东西）。
        assert "configLoadedText" in save, save
        # api() 把状态码带出来，调用方才分得清 409 与普通失败。
        assert "err.status = resp.status;" in js


def test_app_js_editor_warns_model_flows_into_fallbacks(tmp_dir):
    """⚙ 面板：该角色 fallback 非空时点名提示"模型名同样用于备胎"（建议4，纯提示不拦截）。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        panel = js.split("function memberEditPanel(row)", 1)[1].split("\n}\n", 1)[0]
        assert "row.fallback" in panel, panel
        assert "模型名将同样用于备胎" in panel, panel
        assert "异构备胎可能不认识该名称" in panel, panel
        # 名单是 config 可控文本 → 文本位必须转义。
        assert "escapeHtml(fb.join" in panel, panel
        # 空名单时一个字都不出（恒态提示 = 噪音）。
        assert 'fb.length' in panel and '": ""' not in panel, panel
        code, css = _req(base, "/styles.css")
        assert ".mre-warn" in css


def test_app_js_role_binding_chip_title_uses_attr_escape(tmp_dir):
    """#role-bindings 的 title 是属性位 → escapeHtmlAttr（建议6：escapeHtml 不转引号）。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        rb = js.split("function renderRoleBindings()", 1)[1].split("\n}\n", 1)[0]
        assert "const primaryAttr = escapeHtmlAttr(String(r.primary));" in rb, rb
        assert "title=\"主绑定 ${primaryAttr} 与全部备胎均已停用" in rb, rb
        assert "title=\"主绑定 ${primaryAttr} 已停用" in rb, rb
        # 文本位仍走 escapeHtml（两种位置各按各的口径，不是一刀切）。
        assert "${role} ⛔${primary}→${escapeHtml(String(r.effective))}" in rb, rb


def test_app_js_model_family_guess_does_not_overmatch_oc(tmp_dir):
    """家族猜测：'oc' 只认独立分段，local_cli / mock_cli 不再被误判成 opencode（建议7）。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code
        fn = js.split("function modelCandidates(adapterName)", 1)[1].split("\n}\n", 1)[0]
        assert 'n.includes("oc")' not in fn, fn
        assert 'segs.includes("oc")' in fn, fn
        assert "n.split(" in fn, fn
        assert "只影响候选提示" in js


def test_styles_member_edit_pop_width_is_clamped(tmp_dir):
    """⚙ 弹层宽度钳制到视口内，不再用 min-width 顶穿窄视口（建议8）。"""
    with _Serving(tmp_dir) as base:
        code, css = _req(base, "/styles.css")
        assert code == 200, code
        block = css.split(".mr-edit-pop {", 1)[1].split("}", 1)[0]
        assert "min-width" not in block, block
        assert "width: min(268px, calc(100vw - 24px));" in block, block
        assert "backdrop-filter" not in block, block
