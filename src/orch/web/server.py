"""玻璃感 Web 控制台网关（spec 之外的补充工具）。

设计铁律（本文件严格遵守）：
  · 零新增依赖：仅标准库 http.server / socketserver / json / urllib / traceback
    / pathlib，+ 已有 orch 包，+ 已装 pyyaml（仅 PUT /api/config 校验用，白名单内）。
  · 不重复实现业务逻辑：全部端点复用 src/orch/cli/main.py 已有的模块级 helper
    与 orch.store / orch.scheduler / orch.render，不拷第二份。
  · 进程内固定一个 workspace 根（make_server 参数决定）。

对外：make_server(workspace, host, port) -> ThreadingHTTPServer（便于测试 in-process 起停）。
"""

from __future__ import annotations

import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import orch.store
import orch.scheduler
import orch.render

# 复用 CLI 层既有 helper（同包私有名允许直接 import；不拷业务逻辑）。
import orch.cli.main as clim


_STATIC_DIR = Path(__file__).resolve().parent / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

_JSON_CT = "application/json; charset=utf-8"


class _ApiError(Exception):
    """携带 HTTP 状态码的受控错误（前端可见 message，不泄漏堆栈）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ——————————————————————————————————————————————————————————————
# 端点实现：每个都复用 CLI helper / Store / scheduler / render，不拷逻辑。
# 约定：handler 方法把 (workspace, path_parts, query, body) 传进来，返回
#       (status_code, json_serializable)。抛 _ApiError → 对应状态码 JSON。
# ——————————————————————————————————————————————————————————————

def _ep_health(ws: Path) -> tuple[int, dict]:
    return 200, {"ok": True, "workspace": str(ws)}


def _ep_threads_list(ws: Path) -> tuple[int, list]:
    out = []
    for d in clim._find_thread_dirs(ws):
        store = orch.store.Store(d)
        out.append({
            "id": d.name,
            "status": store.get_meta("status") or "unknown",
            "roles": clim._thread_roles(store),
        })
    return 200, out


def _ep_thread_create(ws: Path, body: dict) -> tuple[int, dict]:
    task = body.get("task")
    if not task or not isinstance(task, str):
        raise _ApiError(400, "task 必填（字符串）")
    roles = body.get("roles") or ["pm", "moderator"]
    if not isinstance(roles, list) or not roles:
        raise _ApiError(400, "roles 必须是非空数组")
    roles = [str(r).strip() for r in roles if str(r).strip()]
    if not roles:
        raise _ApiError(400, "roles 至少一个非空角色")

    ws.mkdir(parents=True, exist_ok=True)
    tid = clim._new_thread_id()
    store = clim._open_thread_store(ws, tid)
    # 等价 orch new：首角色（含 pm 用 pm，否则 roles[0]）承载 E1 任务描述。
    first_target = "pm" if "pm" in roles else roles[0]
    store.append_event(
        sender="human", type="assign", body=task,
        to=[first_target], meta={"roles": roles},
    )
    store.set_meta("status", "running")
    store.set_meta("roles", json.dumps(roles, ensure_ascii=False))
    return 200, {"id": tid}


def _require_thread(ws: Path, tid: str) -> "orch.store.Store":
    tdir = ws / tid
    if not tdir.exists():
        raise _ApiError(404, f"线程 {tid} 不存在")
    return orch.store.Store(tdir)


def _ep_thread_events(ws: Path, tid: str) -> tuple[int, dict]:
    store = _require_thread(ws, tid)
    out = []
    for ev in store.events():
        # store.events() 已按协议名解析：re/meta 为已解析结构、ts 为原始 REAL、
        # body 为库内原文（只读投影，不做规范化——C1 根因是前端把 §6.2 视图行
        # 当正文渲染，库内 body 本身干净，故此处直出原文）。
        out.append({
            "id": ev["id"],
            "sender": ev.get("from"),
            "type": ev.get("type"),
            "to": ev.get("to") or [],
            "body": ev.get("body", ""),
            "corr": ev.get("corr"),
            "re": ev.get("re") or [],          # 回复链事件号数组（R3/D12 用）
            "ts": ev.get("ts"),                # 原始时间戳 REAL（R3/D13 用）
            "meta": ev.get("meta") or {},      # {tokens_in/out, duration_s, verify…}（R3 用）
            # D14 artifacts chips：库内 artifacts_json 已由 store.events() 解析为数组直出。
            "artifacts": ev.get("artifacts") or [],
            # D17 黑板投影数据源：A 类事件的 bb_ops（store 键名 blackboard_ops，
            # 已解析为数组或 None）。前端据 A 类事件 + 这些 ops 还原黑板三节
            # （契约 freeze_contract / 决策 set_decision / 任务 set_task），
            # 无需新增只读端点——投影足以从已投影事件重建（spec §4.6 增量=重放一致）。
            "bb_ops": ev.get("blackboard_ops") or [],
            # 第三人称渲染走 orch.render（viewer_role 对单行不改格式，§6.2）。
            # 仅供 replay/审计口径参考；前端阅读列一律渲染 body 原文，绝不渲染此行（§16.7）。
            "third_person": orch.render.render_event_third_person(ev, viewer_role="human"),
        })
    return 200, {"events": out}


def _role_binding_projection(ws: Path, store: "orch.store.Store") -> list[dict]:
    """§12 可用性呈现的控制台数据源（评审 minor-2）：角色 → 生效绑定的只读投影。

    键名冻结：{role, primary, effective, blocked}；effective=None ⇔ blocked=True
    （该角色主绑定与全部备胎均已停用，§5.6.2）。解析复用 state.resolve_effective_adapter，
    与调度层同一判据；本函数**不写盘、不改配置**。
    状态文件损坏 → 空投影（不猜测；run 端点会给出人话报错，读页面不该被打断）。
    """
    from orch.adapters.state import AdapterStateError, resolve_effective_adapter

    cfg_path = clim._workspace_config_path(ws)
    cfg = clim._read_config_file(cfg_path)
    roles_cfg = cfg.get("roles") or {}
    roles = clim._thread_roles(store) or [str(r) for r in roles_cfg]
    if not roles:
        return []
    try:
        availability = clim._open_availability(cfg_path)
    except AdapterStateError:
        return []
    out = []
    for role in roles:
        rc = roles_cfg.get(role) or {}
        primary = str(rc.get("adapter") or role)
        effective = resolve_effective_adapter(role, roles_cfg, availability)
        out.append({
            "role": role,
            "primary": primary,
            "effective": effective,
            "blocked": effective is None,
        })
    return out


def _ep_thread_status(ws: Path, tid: str) -> tuple[int, dict]:
    store = _require_thread(ws, tid)
    pend = store.pending_dispatches()
    return 200, {
        "status": store.get_meta("status") or "unknown",
        "dispatches": [
            {
                "event_id": r["event_id"], "target": r["target"],
                "status": r["status"], "attempts": r["attempts"],
            }
            for r in pend
        ],
        # M5 §12：角色行的生效绑定 / 阻塞点名（前端据此渲染，不再只看"有没有 disabled"）。
        "roles": _role_binding_projection(ws, store),
    }


def _ep_thread_send(ws: Path, tid: str, body: dict) -> tuple[int, dict]:
    store = _require_thread(ws, tid)
    text = body.get("body")
    if text is None or not isinstance(text, str):
        raise _ApiError(400, "body 必填（字符串）")
    to = body.get("to")
    type_ = body.get("type") or "assign"
    target = to if (to and isinstance(to, str)) else "moderator"
    eid = store.append_event(sender="human", type=str(type_), body=text, to=[target])
    return 200, {"event_id": eid}


def _ep_thread_run(ws: Path, tid: str, body: dict) -> tuple[int, dict]:
    store = _require_thread(ws, tid)
    roles = clim._thread_roles(store)
    if not roles:
        raise _ApiError(400, f"线程 {tid} 无角色配置，无法装配 adapters")
    config = clim._load_config(ws)
    # config 定义了真实 adapters+roles → 装真实 CLI 后端（与 orch run 同一判断，Q1/Q2 陪跑）；
    # 否则回退默认 Fake（mock 冒烟）。真实后端下 run_thread 会同步跑真实 CLI，请求耗时较长。
    if config.get("adapters") and config.get("roles"):
        # M5 §5.6/§11.1 生产装配接线（与 orch run 同源：clim._prepare_availability_config）：
        # 先装载期校验，错误清单非空 → 400 一行人话；合法则把状态文件路径写回 config，
        # 调度层每轮 reload 自然感知控制台/CLI 的 enable/disable，无需任何推送通道。
        # 校验 + 状态文件探载失败 → JSON 错误响应（**不退进程**：网关进程常驻，
        # 一个坏 workspace 不该拖垮控制台；运维在页面上就能看到人话原因）。
        errors = clim._prepare_availability_config(
            config, clim._workspace_config_path(ws), roles=roles,
        )
        if errors:
            raise _ApiError(
                400,
                "适配器装载/装配检查未通过（§5.6.1/§7.3/§11.1）：" + "；".join(errors))
        adapters = clim._build_adapters_from_config(roles, config, ws / tid)
    else:
        adapters = clim._build_default_adapters(roles)
    # 等价 orch run --once 对单线程：run_thread 跑到 terminated/suspended 返回。
    orch.scheduler.run_thread(store, config, adapters)
    return 200, {"ran": True, "status": store.get_meta("status") or "unknown"}


def _ep_thread_reopen(ws: Path, tid: str) -> tuple[int, dict]:
    store = _require_thread(ws, tid)
    store.set_meta("status", "running")
    return 200, {"status": "running"}


def _ep_thread_attach(ws: Path, tid: str, role: str) -> tuple[int, dict]:
    store = _require_thread(ws, tid)
    sess = clim._lookup_session(store, role)
    if sess is None or not sess.get("sid"):
        cmd = (
            f"# 暂无活会话 sid（尚未冷启动或 API 型角色无 sid）。冷启动示例：\n"
            f"cd {ws / tid}\n"
            f"claude --print --output-format json < view.txt"
        )
        return 200, {"command": cmd}
    backend = sess.get("backend") or "claude"
    sid = sess["sid"]
    cmd = f"{backend} --resume {sid}"
    return 200, {"command": cmd}


def _ep_thread_replay(ws: Path, tid: str) -> tuple[int, dict]:
    store = _require_thread(ws, tid)
    events = store.events()
    lines = [f"# orch replay —— thread={tid}"]
    if not events:
        lines.append(f"(thread {tid} 暂无事件)")
    else:
        # 复用 CLI 层整流渲染（第三人称行 + ③终止后到达标记），不拷格式。
        lines.extend(clim._render_replay_lines(events))
    return 200, {"markdown": "\n".join(lines)}


def _ep_gate(ws: Path, body: dict) -> tuple[int, dict]:
    tid = body.get("thread")
    corr = body.get("corr")
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        raise _ApiError(400, "decision 必须是 approve 或 reject")
    if not tid or not corr:
        raise _ApiError(400, "thread 与 corr 必填")
    store = _require_thread(ws, str(tid))
    config = clim._load_config(ws)
    orch.scheduler.apply_gate_decision(
        store, config, {}, corr=str(corr),
        approve=(decision == "approve"), sender="human",
    )
    return 200, {"ok": True}


def _ep_stop(ws: Path) -> tuple[int, dict]:
    ws.mkdir(parents=True, exist_ok=True)
    marker = clim._stop_marker_path(ws)
    import time as _t
    marker.write_text(f"stopped_at={_t.time()}\n", encoding="utf-8")
    return 200, {"ok": True}


def _ep_config_get(ws: Path) -> tuple[int, dict]:
    cfg = ws / "config.yaml"
    if not cfg.exists():
        return 200, {"yaml": "", "exists": False}
    try:
        raw = cfg.read_text(encoding="utf-8")
    except OSError as e:
        raise _ApiError(500, "config 读取失败") from e
    return 200, {"yaml": raw, "exists": True}


def _ep_config_put(ws: Path, body: dict) -> tuple[int, dict]:
    raw = body.get("yaml")
    if raw is None or not isinstance(raw, str):
        raise _ApiError(400, "yaml 必填（字符串）")
    import yaml  # pyyaml 已在依赖白名单
    try:
        yaml.safe_load(raw)  # 仅校验可解析；不改结构
    except yaml.YAMLError as e:
        # 校验失败：不写盘，返回 error（HTTP 200 承载 {error} 便于前端统一处理）。
        return 200, {"error": f"YAML 解析失败: {e}"}
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "config.yaml").write_text(raw, encoding="utf-8")
    return 200, {"ok": True}


# ——————————————————————————————————————————————————————————————
# M5 §5.6/§12：适配器可用性三端点。写路径与 CLI **同一** AdapterAvailability
# （整文件 JSON 原子替换），控制台开关与 `orch adapter enable|disable` 完全等价。
# 投影口径（config 声明 ∪ 状态文件记录）复用 clim._adapter_rows，不拷第二份。
# ——————————————————————————————————————————————————————————————

def _availability_for(ws: Path):
    """(config 字典, 可用性视图)；状态文件损坏 → 500 一行人话（§5.6.1 禁止猜测）。"""
    from orch.adapters.state import AdapterStateError

    cfg_path = clim._workspace_config_path(ws)
    cfg = clim._read_config_file(cfg_path)
    try:
        return cfg, clim._open_availability(cfg_path)
    except AdapterStateError as exc:
        raise _ApiError(500, str(exc)) from exc


def _ep_adapters(ws: Path) -> tuple[int, dict]:
    """GET /api/adapters → {"adapters":[{name,status,reason,by,ts,fail_streak}…]}。"""
    cfg, availability = _availability_for(ws)
    return 200, {"adapters": clim._adapter_rows(cfg, availability)}


def _ep_adapter_set(ws: Path, action: str, body: dict) -> tuple[int, dict]:
    """POST /api/adapters/{enable,disable} → 200 + 新 snapshot；未知 name → 400。

    disable 一律 by="human"（控制台是人工写者，与 §5.6.3 的 by="auto" 跳闸区分开）；
    enable 顺带清零 fail_streak（§5.6.3 唯一恢复路径）。
    """
    name = body.get("name")
    if not name or not isinstance(name, str):
        raise _ApiError(400, "name 必填（字符串）")
    cfg, availability = _availability_for(ws)
    known = clim._known_adapter_names(cfg, availability)
    # known 为空 = 该 workspace 既无 config 声明也无历史记录：无从校验，不拒（与 CLI 同规则）。
    if known and name not in known:
        raise _ApiError(400, f"未知 adapter 名: {name}")
    if action == "disable":
        reason = body.get("reason")
        availability.disable(name, reason=str(reason or ""), by="human")
    else:
        availability.enable(name)
    return 200, {"adapters": clim._adapter_rows(cfg, availability)}


def _ep_metrics(ws: Path, query: dict) -> tuple[int, dict]:
    """§13 全表——复用 CLI 层聚合口径 helper，字段名齐全，无数据显示 N/A。"""
    thread = None
    if query.get("thread"):
        thread = query["thread"][0]
        if not thread:
            thread = None
    dirs = clim._thread_dirs_for_metrics(ws, thread)

    import statistics

    round_counts: list[int] = []
    cost_values: list[float] = []
    batch_sizes: list[float] = []
    token_row_count = 0
    schema_retry_vals: list[float] = []
    bg_orig_vals: list[float] = []
    bg_summarized_vals: list[float] = []
    resume_save_vals: list[float] = []
    chaos_rounds_vals: list[float] = []
    chaos_mock_pass_vals: list[float] = []
    chaos_real_pass_vals: list[float] = []

    for d in dirs:
        store = orch.store.Store(d)
        round_counts.append(len(store.events()))
        cost_values.extend(clim._collect_metric_values(store, "cost"))
        batch_sizes.extend(clim._collect_metric_values(store, "batch_size"))
        token_row_count += len(clim._collect_metric_values(store, "tokens"))
        schema_retry_vals.extend(clim._collect_metric_values(store, "schema_retry"))
        bg_orig_vals.extend(clim._collect_metric_values(store, "bg_orig_tokens"))
        bg_summarized_vals.extend(clim._collect_metric_values(store, "bg_summarized_tokens"))
        resume_save_vals.extend(clim._collect_metric_values(store, "resume_token_save_pct"))
        chaos_rounds_vals.extend(clim._collect_metric_values(store, "chaos_rounds"))
        chaos_mock_pass_vals.extend(clim._collect_metric_values(store, "chaos_mock_pass_pct"))
        chaos_real_pass_vals.extend(clim._collect_metric_values(store, "chaos_real_pass_pct"))

    task_count = len(dirs)
    avg_rounds = statistics.mean(round_counts) if round_counts else None
    total_cost = sum(cost_values) if cost_values else None

    if batch_sizes and sum(batch_sizes) > 0:
        saved = sum(max(0.0, b - 1.0) for b in batch_sizes)
        agg_save_pct = saved / sum(batch_sizes) * 100.0
    else:
        agg_save_pct = None

    retry_calls = sum(schema_retry_vals)
    first_legal_pct = (1.0 - retry_calls / token_row_count) * 100.0 if token_row_count > 0 else None

    if bg_orig_vals and bg_summarized_vals and sum(bg_orig_vals) > 0:
        comp_ratio = sum(bg_summarized_vals) / sum(bg_orig_vals)
    else:
        comp_ratio = None

    resume_save_pct = statistics.mean(resume_save_vals) if resume_save_vals else None
    chaos_rounds = sum(chaos_rounds_vals) if chaos_rounds_vals else None
    chaos_mock_pct = statistics.mean(chaos_mock_pass_vals) if chaos_mock_pass_vals else None
    chaos_real_pct = statistics.mean(chaos_real_pass_vals) if chaos_real_pass_vals else None
    adapter_loc = clim._count_adapter_loc_from_third()

    rows = [
        {"label": "tasks", "value": str(task_count)},
        {"label": "avg_rounds", "value": clim._fmt_num(avg_rounds)},
        {"label": "cost", "value": clim._fmt_num(total_cost)},
        {"label": "aggregate_save_pct", "value": clim._fmt_pct(agg_save_pct)},
        {"label": "first_legal_pct", "value": clim._fmt_pct(first_legal_pct)},
        {"label": "background_compression_ratio", "value": clim._fmt_num(comp_ratio)},
        {"label": "resume_token_save_pct", "value": clim._fmt_pct(resume_save_pct)},
        {"label": "chaos_rounds", "value": clim._fmt_num(chaos_rounds)},
        {"label": "chaos_mock_pass_pct", "value": clim._fmt_pct(chaos_mock_pct)},
        {"label": "chaos_real_pass_pct", "value": clim._fmt_pct(chaos_real_pct)},
        {"label": "adapter_loc", "value": adapter_loc},
    ]
    # §13 可用性两项（M5，评审 major-1）：标签用 §13 行名，值 = metrics 表现查行数；
    # 分组子行的标签**不含**这两个字面（前端与测试都按字面唯一定位这两行）。
    avail = clim._availability_metric_summary(dirs)
    rows.append({"label": "降级切换次数", "value": str(avail["switch_total"])})
    rows.extend({"label": f"· {label}", "value": str(n)}
                for label, n in avail["switch_groups"])
    rows.append({"label": "自动跳闸次数", "value": str(avail["trip_total"])})
    rows.extend({"label": f"· {label}", "value": str(n)}
                for label, n in avail["trip_groups"])
    return 200, {"rows": rows}


def _ep_bench(ws: Path, body: dict) -> tuple[int, dict]:
    """等价 orch bench resume——复用 CLI 层 _run_bench_series，不重跑子进程。"""
    import statistics

    fixture = str(body.get("fixture") or "like")
    try:
        runs = int(body.get("runs") or 3)
    except (TypeError, ValueError):
        raise _ApiError(400, "runs 必须是整数")
    if runs < 1:
        raise _ApiError(400, "runs 至少为 1")

    ws.mkdir(parents=True, exist_ok=True)
    cold = clim._run_bench_series(ws, fixture, runs, use_resume=False)
    warm = clim._run_bench_series(ws, fixture, runs, use_resume=True)
    cold_mean = statistics.mean(cold) if cold else None
    warm_mean = statistics.mean(warm) if warm else None
    saved_pct = None
    if cold_mean and warm_mean is not None and cold_mean > 0:
        saved_pct = (cold_mean - warm_mean) / cold_mean * 100.0

    report = {
        "fixture": fixture,
        "runs": runs,
        "no_resume": cold,
        "with_resume": warm,
        "no_resume_mean": cold_mean,
        "with_resume_mean": warm_mean,
        "saved_pct": saved_pct,
    }
    return 200, {"report": report}


# ——————————————————————————————————————————————————————————————
# HTTP handler：路由分发 + 静态资源 + 错误 JSON。
# ——————————————————————————————————————————————————————————————

def _make_handler(ws_map: dict, default_name: str):
    """② 多工作区单控制台：ws_map = {名字: Path}（有序），每请求按 ?ws= 解析。"""

    def _pick_ws(query) -> Path:
        vals = query.get("ws") or []
        if not vals or not vals[0]:
            return ws_map[default_name]   # 缺省 = 第一个（向后兼容零参数请求）
        name = vals[0]
        if name not in ws_map:
            raise _ApiError(404, f"未知工作区: {name}")
        return ws_map[name]

    class Handler(BaseHTTPRequestHandler):
        server_version = "orch-web/1.0"

        # —— 输出辅助 ——
        def _send_json(self, status: int, payload) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", _JSON_CT)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_static(self, rel: str) -> None:
            # 仅允许 static 目录内文件（防目录穿越）。
            safe = (_STATIC_DIR / rel).resolve()
            if not str(safe).startswith(str(_STATIC_DIR.resolve())) or not safe.is_file():
                self._send_json(404, {"error": f"not found: {rel}"})
                return
            ct = _CONTENT_TYPES.get(safe.suffix, "application/octet-stream")
            data = safe.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise _ApiError(400, "请求体不是合法 JSON")
            return parsed if isinstance(parsed, dict) else {}

        # 静默默认日志（避免污染 stdout；错误仍走 stderr）。
        def log_message(self, fmt, *args):  # noqa: A003
            return

        # —— 路由核心 ——
        def _dispatch(self, method: str):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            # 静态资源 / 首页。
            if method == "GET" and not path.startswith("/api/"):
                if path in ("/", "/index.html", ""):
                    self._send_static("index.html")
                    return
                self._send_static(path.lstrip("/"))
                return

            if not path.startswith("/api/"):
                self._send_json(404, {"error": f"未知路径: {path}"})
                return

            # API：先解析 body（仅写方法）。
            body = {}
            if method in ("POST", "PUT"):
                body = self._read_body()

            parts = [p for p in path.split("/") if p]  # e.g. ['api','threads','t-x','run']

            status, payload = self._route_api(method, parts, query, body)
            self._send_json(status, payload)

        def _route_api(self, method, parts, query, body):
            # ② 多工作区：每请求解析目标工作区（?ws=名字；缺省第一个）。
            ws = _pick_ws(query)

            # parts[0] == 'api'
            # —— 顶层端点 ——
            if parts == ["api", "workspaces"]:
                if method != "GET":
                    raise _ApiError(405, "workspaces 仅支持 GET")
                return 200, {
                    "workspaces": [{"name": n, "path": str(p)} for n, p in ws_map.items()],
                    "default": default_name,
                }

            if parts == ["api", "health"]:
                if method != "GET":
                    raise _ApiError(405, "health 仅支持 GET")
                return _ep_health(ws)

            if parts == ["api", "threads"]:
                if method == "GET":
                    return _ep_threads_list(ws)
                if method == "POST":
                    return _ep_thread_create(ws, body)
                raise _ApiError(405, "threads 仅支持 GET/POST")

            if parts == ["api", "gate"]:
                if method != "POST":
                    raise _ApiError(405, "gate 仅支持 POST")
                return _ep_gate(ws, body)

            if parts == ["api", "stop"]:
                if method != "POST":
                    raise _ApiError(405, "stop 仅支持 POST")
                return _ep_stop(ws)

            if parts == ["api", "config"]:
                if method == "GET":
                    return _ep_config_get(ws)
                if method == "PUT":
                    return _ep_config_put(ws, body)
                raise _ApiError(405, "config 仅支持 GET/PUT")

            if parts == ["api", "metrics"]:
                if method != "GET":
                    raise _ApiError(405, "metrics 仅支持 GET")
                return _ep_metrics(ws, query)

            if parts == ["api", "bench"]:
                if method != "POST":
                    raise _ApiError(405, "bench 仅支持 POST")
                return _ep_bench(ws, body)

            # —— M5 §12：适配器可用性（列表 + 两个开关端点）——
            if parts == ["api", "adapters"]:
                if method != "GET":
                    raise _ApiError(405, "adapters 仅支持 GET")
                return _ep_adapters(ws)

            if len(parts) == 3 and parts[:2] == ["api", "adapters"]:
                action = parts[2]
                if action not in ("enable", "disable"):
                    raise _ApiError(404, f"未知 adapters 子路径: {action}")
                if method != "POST":
                    raise _ApiError(405, f"adapters/{action} 仅支持 POST")
                return _ep_adapter_set(ws, action, body)

            # —— /api/threads/{id}/... ——
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "threads":
                tid = parts[2]
                sub = parts[3] if len(parts) >= 4 else None

                if sub is None:
                    raise _ApiError(404, f"未知线程子路径: {'/'.join(parts)}")

                if sub == "events":
                    if method != "GET":
                        raise _ApiError(405, "events 仅支持 GET")
                    return _ep_thread_events(ws, tid)
                if sub == "status":
                    if method != "GET":
                        raise _ApiError(405, "status 仅支持 GET")
                    return _ep_thread_status(ws, tid)
                if sub == "send":
                    if method != "POST":
                        raise _ApiError(405, "send 仅支持 POST")
                    return _ep_thread_send(ws, tid, body)
                if sub == "run":
                    if method != "POST":
                        raise _ApiError(405, "run 仅支持 POST")
                    return _ep_thread_run(ws, tid, body)
                if sub == "reopen":
                    if method != "POST":
                        raise _ApiError(405, "reopen 仅支持 POST")
                    return _ep_thread_reopen(ws, tid)
                if sub == "replay":
                    if method != "GET":
                        raise _ApiError(405, "replay 仅支持 GET")
                    return _ep_thread_replay(ws, tid)
                if sub == "attach":
                    if method != "GET":
                        raise _ApiError(405, "attach 仅支持 GET")
                    role = parts[4] if len(parts) >= 5 else None
                    if not role:
                        raise _ApiError(400, "attach 需指定角色")
                    return _ep_thread_attach(ws, tid, role)
                raise _ApiError(404, f"未知线程子路径: {sub}")

            raise _ApiError(404, f"未知 API 路径: {'/'.join(parts)}")

        # —— HTTP 动词入口：统一 try/except 兜底 ——
        def _handle(self, method):
            try:
                self._dispatch(method)
            except _ApiError as e:
                self._send_json(e.status, {"error": e.message})
            except BrokenPipeError:
                # 客户端提前断开：静默。
                pass
            except Exception:  # noqa: BLE001 网关顶层兜底：不泄漏堆栈到前端，记 stderr。
                traceback.print_exc()
                try:
                    self._send_json(500, {"error": "internal error"})
                except Exception:  # noqa: BLE001
                    pass

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        def do_PUT(self):  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self):  # noqa: N802
            self._handle("DELETE")

    return Handler


def make_server(workspace, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    """构造并返回一个常驻 HTTP server（调用方负责 serve_forever / shutdown）。

    workspace：单个 workspace 根（Path/str，签名向后兼容）或 **list**（② 多工作区
    单控制台）：每请求经 `?ws=名字` 选择，缺省第一个；名字 = 目录名（同名去重加
    -2/-3 后缀）。port=0 → OS 选空闲端口，实际端口从 server.server_address[1] 取。
    """
    if isinstance(workspace, (list, tuple)):
        paths = [Path(w).resolve() for w in workspace]
    else:
        paths = [Path(workspace).resolve()]
    ws_map: dict = {}
    for p in paths:
        base = p.name or str(p)
        name, k = base, 2
        while name in ws_map:      # 同名目录去重：alpha、alpha-2、alpha-3…
            name = f"{base}-{k}"
            k += 1
        ws_map[name] = p
    default_name = next(iter(ws_map))
    handler = _make_handler(ws_map, default_name)
    server = ThreadingHTTPServer((host, port), handler)
    return server
