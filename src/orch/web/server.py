"""玻璃感 Web 控制台网关（spec 之外的补充工具）。

设计铁律（本文件严格遵守）：
  · 零新增依赖：仅标准库 http.server / socketserver / json / os / uuid / urllib
    / traceback / pathlib，+ 已有 orch 包，+ 已装 pyyaml（仅 PUT /api/config 校验用，
    白名单内）。
  · 不重复实现业务逻辑：全部端点复用 src/orch/cli/main.py 已有的模块级 helper
    与 orch.store / orch.scheduler / orch.render，不拷第二份。
  · 进程内固定一个 workspace 根（make_server 参数决定）。

对外：make_server(workspace, host, port) -> ThreadingHTTPServer（便于测试 in-process 起停）。
"""

from __future__ import annotations

import json
import os
import re
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import orch.store
import orch.scheduler
import orch.render
import orch.protocol
# 只用其纯函数 parse_invoke_steps（展示用步骤解析）；wire_format 形状知识只留适配层一处。
import orch.adapters

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


# 线程 id 白名单。仓内真实命名（先查后定）：`orch new` / POST /api/threads 生成
# "t-" + uuid4().hex[:8]（cli/main.py:_new_thread_id），`_find_thread_dirs` 只认
# name.startswith("t-")，测试与实盘出现过的形态是字母/数字/连字符（t-bb2、
# t-fail-timeout、t-A）。故白名单 = ^t-[A-Za-z0-9-]+$ —— 里面既无 "/" 也无 "."，
# ".." 与任何路径分量都进不来。
_TID_RE = re.compile(r"^t-[A-Za-z0-9-]+$")


def _check_tid(tid: str) -> None:
    """线程 id 名字校验：不合白名单 → 404，且**在碰文件系统之前**返回。

    必须前置的理由：路由把 path 按 "/" 切开后不校验分量，tid=".." 能一路穿到
    ``orch.store.Store(目录)``，而它的构造**会建目录**（blackboard/、logs/、
    events.db），于是一个 GET /api/threads/../status 就能在 workspace 之外落文件。
    校验谓词只此一份，路由入口与 _require_thread 两处都调它（后者兜住 /api/gate
    这类 tid 来自**请求体**的入口）。
    """
    if not _TID_RE.match(tid or ""):
        raise _ApiError(404, f"线程 id 不合法: {tid}")


def _require_thread(ws: Path, tid: str) -> "orch.store.Store":
    _check_tid(tid)
    tdir = ws / tid
    if not tdir.exists():
        raise _ApiError(404, f"线程 {tid} 不存在")
    return orch.store.Store(tdir)


def _round_stats(events: list[dict]) -> dict:
    """「本轮」统计（控制台右栏统计卡的唯一数据源）：自最后一条人类消息起的窗口。

    「轮」在 spec 里零定义（run_thread 跑到终态才返回），故这里给出一个**可从盘上
    重建**的口径：窗口锚点 = 最后一条 sender=='human' 的事件，窗口 = 锚点及其后的
    全部事件（含锚点）；无 human 事件则窗口 = 全部事件、anchor_event_id=None。
    语义即"自我上次说话以来"，刷新/换进程都不丢（§16.9：每请求现算，零缓存、
    不留模块级可变状态）。

    键名冻结 anchor_event_id/duration_s/steps/invokes：
      · duration_s = 窗口内 last_ts − first_ts（窗口 ≤ 1 条 → 0.0，不编造）
      · steps      = 窗口事件数
      · invokes    = 窗口内 sender ∉ {human, system} 的事件数（system 是审计事件，
                     不是一次模型调用）
    **不出「工具数」一栏**：T4 修复合规债后 logs/ 落的已是 stdout 原文、工具调用在盘上
    有痕迹了，但那是**逐次 invoke 的日志文件**——凑一栏"本轮工具数"须把窗口内每条回复的
    日志都读一遍求和，把统计卡从"零查库的事件派生"变成 N 次读盘（1.5s 轮询下的最重热点）。
    工具明细改由 /steps 端点按需给出（点开某条气泡才取那一份日志），统计卡维持三栏。

    入参是 _ep_thread_events 的只读投影（含 sender/ts/id），不查库、不改数据。
    """
    evs = list(events or [])
    anchor_idx = None
    for i, ev in enumerate(evs):
        if ev.get("sender") == "human":
            anchor_idx = i          # 多条 human 取最后一条：不 break，扫到底
    window = evs if anchor_idx is None else evs[anchor_idx:]
    duration = 0.0
    if len(window) > 1:
        first_ts, last_ts = window[0].get("ts"), window[-1].get("ts")
        if first_ts is not None and last_ts is not None:
            duration = float(last_ts) - float(first_ts)
    return {
        "anchor_event_id": None if anchor_idx is None else evs[anchor_idx].get("id"),
        "duration_s": duration,
        "steps": len(window),
        "invokes": sum(1 for ev in window
                       if ev.get("sender") not in ("human", "system")),
    }


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
            # 该事件**自述**携带的 bb_ops（store 键名 blackboard_ops，已解析为数组
            # 或 None）。注意：落库 ≠ 生效——被 §3.3 门槛拒绝的 ops 照样在这里出现
            # （store.reply_and_done:319/345 无条件写 bb_ops_json）。故黑板三节的
            # 数据源**不是**它，而是 GET /api/threads/{id}/board 的权威投影；本键只
            # 供聊天流呈现"这条消息声明了什么"（自述 = 消息内容，如实展示不算采信）。
            "bb_ops": ev.get("blackboard_ops") or [],
            # 第三人称渲染走 orch.render（viewer_role 对单行不改格式，§6.2）。
            # 仅供 replay/审计口径参考；前端阅读列一律渲染 body 原文，绝不渲染此行（§16.7）。
            "third_person": orch.render.render_event_third_person(ev, viewer_role="human"),
        })
    # round_stats 是 events 的**同级键**（events 数组的元素结构是冻结面，一个键不动）。
    return 200, {"events": out, "round_stats": _round_stats(out)}


def _ep_thread_board(ws: Path, tid: str) -> tuple[int, dict]:
    """§4.6 **权威**黑板的只读投影（控制台右栏三节的唯一数据源）。

    权威 = blackboard/state.json 这份材料化状态，用公开只读函数
    ``orch.store.board_state_checked`` 读出（docs/m0-contract.md §2 的对外符号）；
    本端点不写盘、不改配置、不触发重建。

    **为什么用 checked 版而非 board_state**：Store._write_state 是非原子 write_text，
    崩溃截断可复现；而宽松读取把 JSONDecodeError 降级成空结构（store:_read_state），
    端点无从区分，损坏的 state.json 会被当"空黑板"直出 200 —— 页面渲染成正常空态，
    错的空白比读不到更骗人。故损坏时照给空结构（键形状不变）**并**加一个顶层
    board_error 人话，前端据它走显式失败态。健康路径（含"还没写过 state.json"）
    响应逐字同旧：无 board_error 键。§9.1 的重建仍只由调度/恢复路径做（`orch run`），
    展示层只负责如实说"读不出来"。

    **为什么不据事件的 bb_ops 重投影**：落库 ≠ 生效。被 §3.3 门槛拒绝的 ops 照样
    进库（store.reply_and_done:319/345 无条件写 bb_ops_json），调度层
    ``_apply_bb_if_eligible``（scheduler/core.py:678-688）只是"不应用 + 追加一条
    system 审计事件"。据事件重投影 = 把**被拒绝的 agent 自述**当成既成事实展示，
    勾选与「第 N/M 步」会把它算进完成度 —— 这正是 §16 第 5 条"采信 agent 自述"
    在展示层的形态。权威状态里没有的，页面上就不该有。

    tasks 的**声明序**与 #evt 跳转在 state.json 里查不到，只能另扫事件补：
      · tasks 结构是 {key: status}，不带事件号；
      · Store._write_state 用 ``json.dumps(..., sort_keys=True)`` 落盘
        （store/__init__.py:643-647），插入序在盘上即被字典序抹平 —— 读回来的
        dict 顺序是**字典序**，不是首次声明序。
    故补两个**溯源**键，且只对权威已存在的 key 生效：
      task_order —— 首次声明序（事件 id 升序；同一事件内多 key 按 key 名兜底）
      task_evt   —— 最近一次声明该 key 的事件号（#evt 跳转用；查不到则缺键）
    权威 tasks 里没有的 key 不可能出现在这两个键里 —— 被拒绝的自述既不进列表，
    也不进「第 N/M 步」的分母。

    键名冻结：contracts / decisions / tasks（state.json 逐字段原样）+
    task_order / task_evt（溯源）+ board_error（**仅**损坏时出现）。
    每请求现查盘、零缓存、无模块级状态（§16.9）。
    """
    store = _require_thread(ws, tid)
    state, board_error = orch.store.board_state_checked(store)
    tasks = dict(state.get("tasks") or {})

    first_evt: dict[str, int] = {}
    last_evt: dict[str, int] = {}
    for ev in store.events():
        # 只认 §3.3 的 A 类三型：复用协议层同一谓词，把 can_decide 那一半置真、
        # 单判 type 那一半（can_decide 的判定结果已经体现在权威 state 里，此处
        # 不重判、不读 config —— 判据只留调度层一处，展示层不做第二套）。
        if not orch.protocol.can_apply_blackboard_ops(
            ev.get("type"), sender_can_decide=True
        ):
            continue
        for op in (ev.get("blackboard_ops") or []):
            if op.get("op") != "set_task":
                continue
            key = op.get("key")
            if key not in tasks:
                continue           # 权威里没有 = 没被应用，不给它任何露面机会
            first_evt.setdefault(key, ev["id"])
            last_evt[key] = ev["id"]

    def _order_key(k: str):
        # 有溯源 → 按首次声明事件号；查不到溯源的（理论上只有人工改盘才会出现）
        # 排在最后并按 key 名定序：不编造事件号，也不让它插队。
        return (0, first_evt[k], k) if k in first_evt else (1, 0, k)

    payload = {
        "contracts": dict(state.get("contracts") or {}),
        "decisions": list(state.get("decisions") or []),
        "tasks": tasks,
        "task_order": sorted(tasks, key=_order_key),
        "task_evt": last_evt,
    }
    if board_error:
        # 顶层新键，值是**字符串**（同 /status 的 config_error 口径）：无错误时该键
        # 不出现，故健康路径的响应与键集合逐字同旧。
        payload["board_error"] = board_error
    return 200, payload


def _role_binding_projection(
    ws: Path, store: "orch.store.Store", cfg: dict, cfg_error: str | None,
) -> list[dict]:
    """§12 可用性呈现的控制台数据源（评审 minor-2）：角色 → 生效绑定的只读投影。

    返回投影行，键名 {role, display_name, model, primary, effective, blocked}；
    effective=None ⇔ blocked=True（该角色主绑定与全部备胎均已停用，§5.6.2）。解析复用
    state.resolve_effective_adapter，与调度层同一判据；本函数**不写盘、不改配置**。

    model（C3 追加的**纯呈现**键，同 display_name/started_ts 加键先例）= 该角色生效的
    模型名，取值链 role 层 model → 主绑定 adapter 层 model → None。两处细节：
      · 取值优先级与装配层的合并语义同源（`cli.main._build_adapters_from_config` 的
        ``{**ac, **rc}``）：role 层**存在该键就赢，含显式空值**——写 ``model: ""`` 是
        "我不要模型名"的表态，偷偷回落 adapter 层缺省等于替配置者猜（§11.1 fail-closed
        正是要禁这个），故此处一律呈现成 None 而不是 adapter 层那个值；
      · 回落只看**主绑定**那一段 adapter 的缺省，不看降级后的 effective：降级到异构备胎
        时，备胎段的缺省模型名与本角色的配置无关（grok 的名字喂给 opencode 必然失败），
        把它当"本角色的模型"展示是编造。降级中的真实走向由 effective 那一栏说。

    display_name（config roles 层可选键）是**纯呈现**键，与 wire_format 同类先例：
    引擎一侧无视它——模型视图只取 prompt/write_scope/tools（render/__init__.py::
    _build_system / _read_prompt）与 adapter.context_window（同文件 _context_window），
    别的 roles 键一个不读，故中文名不会漏进提示词（已跑 render_view 实测确认）；
    机器匹配位（路由/筛选/CSS 类/单聊比较键）永远用 role id，本投影把两者并列给出，
    前端才不必自己去猜哪个能当键用。缺配 → 等于 role id 本身（不臆造别名）。

    (cfg, cfg_error) 由调用方传入（`clim._read_config_file_checked` 的原样产物）：
    同一请求内 config 只读一次，角色投影与派发行的 started_ts 推算同源同拍——两处
    各读一次会在"运维正保存 config"的瞬间给出互相矛盾的两半。

    两条"不猜测"的空投影出口，同向不同信号：
      · 状态文件损坏 → 空投影（run 端点会给人话报错，读页面不该被打断）；
      · config.yaml 读不出来（语法错/顶层非映射/读不动）→ 空投影，错误信号由调用方
        （_ep_thread_status 的 config_error 顶层键）承载（评审"应修3"）。绝不用
        "角色名当主绑定"兜底：``state.is_enabled`` 对未记录的名字返回 True，兜底会让
        每一行都是 blocked=false 的**假健康**，而真实主绑定可能正被人工停用，
        §12「阻塞角色必须显著警示」在该路径恒不触发。
    """
    from orch.adapters.state import AdapterStateError, resolve_effective_adapter

    if cfg_error:
        return []
    roles_cfg = cfg.get("roles") or {}
    adapters_cfg = cfg.get("adapters")
    if not isinstance(adapters_cfg, dict):
        adapters_cfg = {}
    roles = clim._thread_roles(store) or [str(r) for r in roles_cfg]
    if not roles:
        return []
    try:
        availability = clim._open_availability(clim._workspace_config_path(ws))
    except AdapterStateError:
        return []
    out = []
    for role in roles:
        rc = roles_cfg.get(role) or {}
        primary = str(rc.get("adapter") or role)
        effective = resolve_effective_adapter(role, roles_cfg, availability)
        # display_name 只在**非空字符串**时生效：config 里写成 null / 空串 / 数字 0 等
        # 假值一律退回 role id，避免前端拿到空名字渲染出无名 chip。
        raw_name = rc.get("display_name")
        display = str(raw_name) if (raw_name and str(raw_name).strip()) else role
        if "model" in rc:
            raw_model = rc.get("model")
        else:
            ac = adapters_cfg.get(primary)
            raw_model = ac.get("model") if isinstance(ac, dict) else None
        model = (str(raw_model)
                 if (raw_model is not None and str(raw_model).strip()) else None)
        out.append({
            "role": role,
            "display_name": display,
            "model": model,
            "primary": primary,
            "effective": effective,
            "blocked": effective is None,
        })
    return out


def _dispatch_started_ts(cfg: dict, target: str, deadline_ts) -> float | None:
    """dispatching 行的**推得**起点 = deadline_ts − 该 target 的超时秒（不新增落盘字段）。

    scheduler/core.py `_dispatch_group` 落盘时用的正是
    `time.time() + _timeout_for(config, target)`（§4.4 事务(2)），故此处 import **同一个**
    `_timeout_for` 逆算：超时口径只留一处，复制第二份就会让页面上的"已处理 mm:ss"
    与真实超时判据慢慢分叉。纯函数、不查库、不写盘。

    返回 None（→ 端点省略该键，前端维持既有"正在响应"胶囊）的情形：
      · deadline_ts 不是有限数（pending 行本就没有截止时间戳）；
      · 超时秒推不出正数。
    调用方还须在 config **读不出来**时整个跳过本函数：`_timeout_for` 那时会退到
    600s 默认值，而那未必是当时真正用的超时秒，据它算出的起点是编的。
    """
    from orch.scheduler.core import _timeout_for

    try:
        deadline = float(deadline_ts)
    except (TypeError, ValueError):
        return None
    if deadline != deadline or deadline in (float("inf"), float("-inf")):
        return None                      # NaN / inf：盘上被人工改坏，不猜
    timeout_s = float(_timeout_for(cfg or {}, str(target)))
    if timeout_s <= 0:
        return None
    return deadline - timeout_s


def _ep_thread_status(ws: Path, tid: str) -> tuple[int, dict]:
    store = _require_thread(ws, tid)
    # 全五态派发行（含 deadline_ts）：控制台的成员状态点/"正在响应"要区分"真的在跑"
    # 与"崩溃后滞留在盘上的 dispatching 行"（watchdog.py:203-205：滞留行会被每一轮
    # check 重新枚举），只给 pending 或给了 dispatching 却不给 deadline_ts 都会长亮假绿。
    # 键名冻结 event_id/target/status/deadline_ts/attempts —— **不得**把 target 改名成
    # role：tests/test_m5_availability.py 的 _role_projection 按"首个每项含 role 键的
    # 顶层列表"结构探测 roles 投影，派发行一旦带 role 键会被误命中。
    # T4 追加**可选**键 started_ts（Lead 批准的加键，不是改名）：由 deadline_ts 逆算
    # （见 _dispatch_started_ts），推不出来就**不出现**——键缺失时前端维持既有胶囊。
    # 它同样不含 role 键，故上述结构探测不受影响。
    # 每请求现查盘、零缓存（§16.9）。
    rows = store.dispatches_snapshot()
    # config 每请求读一次，两处投影共用（见 _role_binding_projection 的入参说明）。
    cfg, cfg_error = clim._read_config_file_checked(clim._workspace_config_path(ws))
    roles = _role_binding_projection(ws, store, cfg, cfg_error)
    dispatches = []
    for r in rows:
        row = {
            "event_id": r["event_id"], "target": r["target"],
            "status": r["status"], "deadline_ts": r["deadline_ts"],
            "attempts": r["attempts"],
        }
        if not cfg_error:
            started = _dispatch_started_ts(cfg, r["target"], r["deadline_ts"])
            if started is not None:
                row["started_ts"] = started
        dispatches.append(row)
    payload = {
        "status": store.get_meta("status") or "unknown",
        "dispatches": dispatches,
        # M5 §12：角色行的生效绑定 / 阻塞点名（前端据此渲染，不再只看"有没有 disabled"）。
        "roles": roles,
    }
    if cfg_error:
        # 顶层新键，值是**字符串**而非列表：test_m5_availability 的 _role_projection
        # 按"首个每项含 role 键的顶层列表"结构探测投影，字符串不可能被误命中。
        # 无错误时该键**不出现** —— 健康路径（含"根本没有 config.yaml"）的响应逐字同旧。
        payload["config_error"] = cfg_error
    return 200, payload


# ——————————————————————————————————————————————————————————————
# T4：invoke 执行流步骤（GET /api/threads/{id}/steps?event_id=N）
#
# 裁决边界（QUESTIONS.md Q11 采 A）：**只作人类展示**——本端点的产物不回流任何调度
# 判定（路由/重试/聚合/超时/可用性），也不进 orch.render 任何视图层；spec §7.1 行396
# 「调度器不知道信封背后是一步还是一百步」的主语是调度器，展示层不越界。
# 暴露口径：只出**解析后的步骤摘要**（工具名 + 命令摘要截断 + 计数）。stdout 原文
# （已实证含 sessionId，Q9 档案）**不经 HTTP 直出**，原文只留 logs/ 供审计
# （orch attach / 现场勘查照旧）。§12 缺省绑 127.0.0.1 但无鉴权，故这条口径是硬的。
# 每请求现读盘、零缓存、无模块级可变状态（§16.9）。
# ——————————————————————————————————————————————————————————————

# store.write_invoke_log 写入的段分隔行（store/__init__.py:622-627 逐字）。本端点只
# **读**它写的文件，格式权威在那一处；此常量是镜像而非第二处定义——那边一旦改格式，
# 本端点当场退成"没有可解析的步骤"（诚实空态），不会给出错误步骤。
_LOG_OUTPUT_MARKER = "=== OUTPUT ==="

# counts 的键（前端按固定四档渲染字形）；零也出，省得前端分不清"没有"与"没算"。
_STEP_KINDS = ("tool", "thinking", "text", "other")

# ——— 双上限（评审 建议4）：本端点读的是**无界**产物，必须自己设界 ———
# 读取上限：只解析输出段**尾部**这么多字节。实测一份 244MB 的 invoke 日志会解析出
# 约 10 万步 → 18.5MB 响应 / 1.31s，单个 GET 就能把控制台（1.5s 轮询、单进程
# ThreadingHTTPServer）压住。取尾部而非头部：一次 invoke 的收尾几步（最后的工具调用
# 与产出信封那一段）才是"这条回复怎么来的"的答案，开头几步反而最不相关。
_LOG_TAIL_LIMIT_BYTES = 4 * 1024 * 1024
# 步数上限：4MB 尾部仍可能是几万行事件。500 步远超任何人愿意在一个折叠组里读的量，
# 又足够覆盖真实长任务；超限如实置 truncated=True 并在 note 里说明，不静默截断。
_STEP_LIMIT = 500


def _steps_payload(*, steps=None, wire_format=None, log_file=None, note="",
                   truncated=False) -> dict:
    """/steps 的响应外形（键名冻结 steps/counts/wire_format/log_file/note/truncated）。

    · wire_format —— **由日志原文嗅探**出的格式（`orch.adapters.sniff_log_wire_format`），
      不是当前绑定的配置值；嗅不出来时为 None（见 _ep_thread_steps 的诚实空态）。
    · truncated —— 步骤列表**不完整**：条数触顶或只解析了日志尾部（原因写在 note）。
    note = 一句人话说明；有步骤且无附注时是空串。**任何字段都不含 stdout 原文全文**。
    """
    rows = list(steps or [])
    return {
        "steps": rows,
        "counts": {k: sum(1 for s in rows if s.get("kind") == k) for k in _STEP_KINDS},
        "wire_format": wire_format,
        "log_file": log_file,
        "note": note,
        "truncated": bool(truncated),
    }


def _log_output_section(text: str) -> str:
    """从 invoke 日志里切出 OUTPUT 段（分隔行之后的全部内容）。

    取**最后一个**分隔行：VIEW 段是渲染出的视图文本，其中含 agent 正文（模型可控），
    正文里伪造一行分隔符是可能的。取最后一个的结果是"至多少显示几步"，而不是把视图
    正文当输出去解析——两种偏差里选不会张冠李戴的那种。
    """
    idx = text.rfind(_LOG_OUTPUT_MARKER)
    if idx < 0:
        return ""
    return text[idx + len(_LOG_OUTPUT_MARKER):].lstrip("\r\n")


def _invoke_log_suffix(event_ids: list[int], role: str) -> str:
    """§14 日志文件名的后缀（store/__init__.py:618-621 的命名约定镜像）。

    定位链：一条角色回复气泡 → 该事件的 re（= 本批触发事件号，core.py:1244 由编排器
    权威赋值）+ from（= 角色名）→ 文件名 `{ts:.6f}_E{ids}_{role}.log` 的后缀。
    """
    ids_part = "-".join(str(int(i)) for i in event_ids) if event_ids else "none"
    return f"_E{ids_part}_{role}.log"


def _find_invoke_logs(store: "orch.store.Store", suffix: str) -> list[Path]:
    """logs/ 下名字以 suffix 结尾的日志，按文件名（= 时间戳前缀）旧→新排序。

    用后缀比对而不是 glob：角色名/事件号进 glob 模式还要转义，比对更直白也更严格。
    同一 (批, 角色) 可有多份——schema 校验失败会原地重调，每次各落一份（§5.1）。
    """
    logs_dir = store.thread_dir / "logs"
    if not logs_dir.is_dir():
        return []
    out = [p for p in logs_dir.iterdir() if p.is_file() and p.name.endswith(suffix)]
    out.sort(key=lambda p: p.name)
    return out


def _read_log_output_tail(path: Path) -> tuple[str, str]:
    """读该日志的 OUTPUT 段 → (原文文本, 体积附注)。超限时只读**尾部**（建议4）。

    小于上限 → 整份读进来走 `_log_output_section` 正常切段。超限 → 只 seek 到
    `-_LOG_TAIL_LIMIT_BYTES` 读尾部：
      · 丢掉窗口首行（字节切口几乎必然落在某行中间，半行解析不出东西还会误导嗅探）；
      · 窗口内若还能找到分隔行，照旧按它切（说明 VIEW 段极大而 OUTPUT 段很小）；
        找不到就把整个窗口当输出段——OUTPUT 段大到把窗口填满，分隔行必在窗口之前。
    体积附注非空即表示"这不是全部"，由调用方并入 note 且置 truncated。
    """
    size = path.stat().st_size
    if size <= _LOG_TAIL_LIMIT_BYTES:
        return _log_output_section(path.read_text(encoding="utf-8", errors="replace")), ""
    with path.open("rb") as fh:
        fh.seek(size - _LOG_TAIL_LIMIT_BYTES)
        chunk = fh.read()
    text = chunk.decode("utf-8", errors="replace").split("\n", 1)[-1]
    if _LOG_OUTPUT_MARKER in text:
        # 分隔行落在窗口内 ⇒ 它之后的内容（= 整个 OUTPUT 段）全在窗口里，被切掉的只有
        # VIEW 段——而本端点根本不读 VIEW。步骤是完整的，故**不报**截断（报了就是虚惊）。
        return _log_output_section(text), ""
    return text, (f"执行日志过大（{size} 字节），仅解析末尾 {_LOG_TAIL_LIMIT_BYTES} 字节"
                  "，靠前的步骤未计入")


def _ep_thread_steps(ws: Path, tid: str, query: dict) -> tuple[int, dict]:
    """某条角色回复气泡背后那次 invoke 的执行步骤摘要（只读，见本节顶部裁决边界）。

    找不到日志 / 该事件不是一次 invoke / 后端不产生步骤流 → 一律 **200 + steps=[] +
    note 一句人话**，不 404、不猜。
    """
    store = _require_thread(ws, tid)
    raw_id = (query.get("event_id") or [""])[0]
    try:
        event_id = int(str(raw_id).strip())
    except (TypeError, ValueError):
        raise _ApiError(400, "event_id 必填（整数：该条回复气泡的事件号）")

    ev = next((e for e in store.events() if int(e["id"]) == event_id), None)
    if ev is None:
        return 200, _steps_payload(note=f"线程 {tid} 里没有事件 #{event_id}")
    role = str(ev.get("from") or "")
    if role in ("", "human", "system"):
        return 200, _steps_payload(
            note=(f"#{event_id} 的发件人是 {role or '未知'}，不是一次后端 invoke"
                  "（human 是人类发言、system 是编排器审计事件），没有执行日志"))

    suffix = _invoke_log_suffix([int(i) for i in (ev.get("re") or [])], role)
    logs = _find_invoke_logs(store, suffix)
    if not logs:
        return 200, _steps_payload(
            note=f"未找到本次执行日志（logs/ 下没有以 {suffix} 结尾的文件）")

    path = logs[-1]          # 多份时取最新：重调过的话，产出这条回复的是最后一次
    try:
        raw_output, size_note = _read_log_output_tail(path)
    except OSError as exc:
        return 200, _steps_payload(
            log_file=path.name, note=f"执行日志读取失败：{clim._one_line(exc)}")

    # 格式判定只认**日志内容**（评审 应修2）：不读 config、不看 sessions.backend——
    # 那些描述的是**当前**绑定，而这份日志是历史产物，换绑后按当前绑定硬解析会吐假步骤。
    wire = orch.adapters.sniff_log_wire_format(raw_output)
    # 原文**到此为止**：只有解析产物出网关，raw_output 本身绝不进响应任何字段。
    # 多取一条用来判断"是不是被条数上限切掉了"，恰好 _STEP_LIMIT 条时不误报 truncated。
    steps = orch.adapters.parse_invoke_steps(raw_output, wire or "",
                                             max_steps=_STEP_LIMIT + 1)
    truncated = bool(size_note)
    notes = [size_note] if size_note else []
    if len(logs) > 1:
        notes.append(f"该事件与角色下有 {len(logs)} 份日志"
                     "（schema 校验失败会原地重调，每次各落一份），此处取最新一份")
    if len(steps) > _STEP_LIMIT:
        steps = steps[:_STEP_LIMIT]
        truncated = True
        notes.append(f"步骤数超过展示上限，只给前 {_STEP_LIMIT} 条"
                     "（完整原文在 logs/，审计不受影响）")
    if not steps:
        if wire in ("json", "text"):
            notes.append(f"该后端（wire_format={wire}）不产生步骤流："
                         "整段 stdout 是单个 JSON / 直出文本，没有逐行事件")
        elif not raw_output.strip():
            notes.append("执行日志的 OUTPUT 段是空的")
        elif wire is None:
            notes.append("无法判定该日志的流式格式（可能产生自其他历史绑定），不猜测")
        else:
            notes.append("本次执行日志里没有可解析的步骤"
                         "（原文可能来自非流式后端，或适配器未提供 stdout 原文）")
    return 200, _steps_payload(steps=steps, wire_format=wire, log_file=path.name,
                               note="；".join(notes), truncated=truncated)


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
        parsed = yaml.safe_load(raw)  # 仅校验可解析；不改结构
    except yaml.YAMLError as e:
        # 校验失败：不写盘，返回 error（HTTP 200 承载 {error} 便于前端统一处理）。
        return 200, {"error": f"YAML 解析失败: {e}"}
    # §11.1 装载期校验**前置到写盘之前**：能解析 ≠ 合法。fallback 指向未声明的
    # adapter、带工具角色绑 api 型，这些错只有 orch run 启动时才会炸——而那时坏配置
    # 已经在盘上，常驻 run 每轮重读会持续拒绝启动。复用 state 里的同一个纯函数
    # （不拷第二份判据，也不在此处收紧规则）。顶层非映射（列表/标量）跳过校验：
    # 那种形状 validate_availability_config 本就只回空表，装载期由别处报错。
    from orch.adapters.state import validate_availability_config

    if isinstance(parsed, dict):
        errors = validate_availability_config(parsed)
        if errors:
            raise _ApiError(400, "配置校验未通过（§11.1）：" + "；".join(errors))
    ws.mkdir(parents=True, exist_ok=True)
    # 写盘走**唯一**的原子写入口 _atomic_write_bytes（见那里的存在理由）。这里刻意
    # 自己 encode 成 bytes 而不再用文本模式：文本模式在 Windows 上会把 \n 悄悄转成
    # \r\n，而 C3 的行级手术端点必须逐字节保真——同一份 config.yaml 上两条写路各持
    # 一套换行策略，会让"改一行"与"整存一次"互相把对方的换行全文重写，diff 全是噪音。
    # 现在两条路一致：请求正文里是什么字节，盘上就是什么字节。
    _atomic_write_bytes(ws / "config.yaml", raw.encode("utf-8"))
    return 200, {"ok": True}


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """原子替换（临时文件 + flush + fsync + os.replace，照 adapters/state.py::_flush）。

    **存在理由**：常驻 `orch run` 每轮重读 config.yaml——非原子写有一段窗口让它读到
    半份文件，于是要么 adapters/roles 段读空、静默退回 Fake 后端（假绿），要么 YAML
    解析失败让整个进程报错退出。os.replace 保证盘上永远是完整的一份：要么旧的、要么
    新的。临时文件与目标同目录（跨目录 rename 非原子），失败即删不留残迹。

    只收 bytes：调用方自己决定编码与换行，本函数一个字节都不改写。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ——————————————————————————————————————————————————————————————
# C3：POST /api/config/role-binding —— 每角色改 CLI / 改模型的**行级外科手术**。
#
# 改写口径（用户裁决，本文件唯一）：程序动 config.yaml 一律**只改目标行的那个值
# token，其余逐字节不动**；认不出的写法一律 400 让人手工编辑，绝不猜。
# 故这里刻意**不**走 safe_load → 改结构 → safe_dump 回写：那条路会把注释、缩进风格、
# 单行花括号写法、键序、锚点全抹平，等于拿一份"语义等价但面目全非"的文件覆盖运维手写
# 的配置（演示床的 config.yaml 里近半数行是注释与踩坑记录，抹了就没了）。
#
# 支持的两种现存形态（仓内演示床两份 config 各占一种，别再加第三种"顺手支持"）：
#   块式    roles:\n  pm:\n    adapter: grok_cli\n    model: x
#   内联式  roles:\n  pm: {adapter: grok_cli, display_name: 产品经理, …}
# 每一步定位都要求**唯一命中**，任一步含糊即整体拒绝，盘上一个字节不动。
# ——————————————————————————————————————————————————————————————

# 允许写进 config 的裸标量字符集：不含空白/引号/井号/冒号/花括号方括号逗号。
# 收得这么窄是刻意的——值不带引号直接落进 YAML，任何一个流式指示符都会改变语义；
# 真实的 adapter 名与模型名（opencode/big-pickle、kimi-code/k3-256k、grok-4.5-latest…）
# 全在这个集合内，落在集合外的名字宁可 400 让人手工加引号编辑。
_YAML_BARE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.+/@-]+$")

# roles: 顶层行（冒号后除行内注释外不许有内容——`roles: {pm: …}` 这种整段内联不支持）。
_ROLES_HEAD_RE = re.compile(r"^roles[ \t]*:[ \t]*(#.*)?$")

# "键:"行/项的形状（键名限保守字符集；匹配不上即视为不认识的写法）。
_MAP_KEY_RE = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:")


def _hand_edit_error(reason: str) -> _ApiError:
    """认不出的写法 → 统一一句人话（前端在名片内红字原样显示）。"""
    return _ApiError(
        400,
        f"该角色的配置写法无法安全自动修改，请到配置页手工编辑（原因：{reason}；"
        f"程序只做行级替换，认不出的写法一律不猜）",
    )


def _split_eol(line: str) -> tuple[str, str]:
    """(正文, 行尾符)。行尾符原样保留，改写不许顺手统一换行。"""
    for eol in ("\r\n", "\n", "\r"):
        if line.endswith(eol):
            return line[: -len(eol)], eol
    return line, ""


def _line_indent(line: str) -> int:
    """行首空格数；用制表符缩进 → -1（YAML 明令禁止，也不猜等价宽度）。"""
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            return -1
        else:
            break
    return n


def _is_skippable(line: str) -> bool:
    """空行或整行注释：扫描时跳过——既不作块边界，也不参与缩进判定。"""
    s = line.strip()
    return (not s) or s.startswith("#")


def _split_inline_comment(text: str) -> tuple[str, str]:
    """把"值 + 可选行内注释"切成 (值区, 注释区)。

    注释判据用 YAML 原规则：**前面是空白**的 ``#`` 才起注释（`a#b` 是值的一部分）。
    这就是"注释保全"的实现点——值替换只动值区那一段，注释区原样接回去。
    """
    for i, ch in enumerate(text):
        if ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i], text[i:]
    return text, ""


def _replace_bare_value(text: str, key: str, new_value: str) -> str:
    """把 ``…key: <值>`` 里的**值 token** 换成 new_value；其余逐字保留。

    "其余"= 缩进、键名、冒号前后空白、值后的补白、行内注释。**禁止**写成
    ``re.sub(rf"{key}:.*$", …)`` 这类扫到行尾的贪婪式：那会把 ``# 注释`` 一起吞掉，
    而注释保全是本卡的硬指标。
    """
    m = re.match(r"^([ \t]*" + re.escape(key) + r"[ \t]*:)([ \t]*)(.*)$", text)
    if not m:
        raise _hand_edit_error(f"{key} 那一行不是 `键: 值` 的形状")
    head, gap, rest = m.group(1), m.group(2), m.group(3)
    if not gap:
        # `key:value` 在 YAML 里整体是一个标量而非映射项，改它等于改错东西。
        raise _hand_edit_error(f"{key} 的冒号后缺空格，不构成映射项")
    value_region, comment = _split_inline_comment(rest)
    value = value_region.rstrip(" \t")
    pad = value_region[len(value):]          # 值与注释之间的补白，原样留住
    if not _YAML_BARE_VALUE_RE.match(value):
        raise _hand_edit_error(f"{key} 的当前值 {value!r} 不是裸标量（引号/流式/空值不改）")
    return head + gap + new_value + pad + comment


def _flow_scan(text: str, start: int) -> int:
    """从 text[start] 处的 ``{`` 出发找**同一行内**配对的 ``}``；找不到 → -1。

    引号内的字符一律不计（含 ``{}``/``[]``/``,``）；遇到 YAML 流式里的注释起点
    （前有空白的 ``#``）即放弃——那说明花括号跨了行，本卡只认单行内联式。
    """
    depth = 0
    quote = ""
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            continue
        if ch == "#" and i > start and text[i - 1] in " \t":
            return -1
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i if ch == "}" else -1
            if depth < 0:
                return -1
    return -1


def _flow_split(inside: str) -> list[str]:
    """按**顶层**逗号切分流式映射的内容，每段保留原文（含两侧空白）。

    切的是逗号字符本身，故 ``",".join(段)`` 能逐字节还原 inside —— 未被改动的项
    因此一个字符都不会漂移。引号与嵌套 ``{}``/``[]`` 内的逗号不切。
    """
    segs: list[str] = []
    depth = 0
    quote = ""
    cur = []
    for ch in inside:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            cur.append(ch)
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == "," and depth == 0:
            segs.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    segs.append("".join(cur))
    if len(segs) == 1 and not segs[0].strip():
        return []                     # `{}` 空映射
    return segs


def _seg_key(seg: str) -> str | None:
    m = _MAP_KEY_RE.match(seg)
    return m.group(1) if m else None


def _locate_role_line(lines: list[str], role: str) -> tuple[int, int, int]:
    """定位角色键行 → (行下标, roles 段结束下标, 角色键缩进)。

    三次唯一性检查，任一步含糊即抛 400：顶层 roles: 行唯一；段内首个有效行确定角色键
    缩进；该缩进上叫 role 的键唯一。
    """
    heads = [i for i, ln in enumerate(lines)
             if _ROLES_HEAD_RE.match(_split_eol(ln)[0])]
    if len(heads) != 1:
        raise _hand_edit_error(
            "找不到唯一的顶层 `roles:` 段" if not heads else "文件里有多个顶层 `roles:` 段")
    start = heads[0] + 1
    end = len(lines)
    for i in range(start, len(lines)):
        if _is_skippable(lines[i]):
            continue
        if _line_indent(lines[i]) <= 0:
            end = i
            break
    role_indent = None
    for i in range(start, end):
        if _is_skippable(lines[i]):
            continue
        role_indent = _line_indent(lines[i])
        break
    if role_indent is None or role_indent <= 0:
        raise _hand_edit_error("roles 段下没有缩进的角色键")
    key_re = re.compile(r"^[ \t]*" + re.escape(role) + r"[ \t]*:")
    hits = [i for i in range(start, end)
            if not _is_skippable(lines[i])
            and _line_indent(lines[i]) == role_indent
            and key_re.match(lines[i])]
    if len(hits) != 1:
        raise _hand_edit_error(
            f"roles 段里 {role} 这一行定位不唯一（命中 {len(hits)} 处）")
    return hits[0], end, role_indent


def _edit_block_role(lines: list[str], ridx: int, end: int, role_indent: int,
                     role: str, adapter: str | None, model: str | None,
                     set_model: bool, dominant_eol: str) -> list[str]:
    """块式（role 键下缩进若干 `键: 值` 行）的改写。"""
    sub_start = ridx + 1
    sub_end = end
    for i in range(sub_start, end):
        if _is_skippable(lines[i]):
            continue
        if _line_indent(lines[i]) <= role_indent:
            sub_end = i
            break
    sub_indent = None
    for i in range(sub_start, sub_end):
        if _is_skippable(lines[i]):
            continue
        sub_indent = _line_indent(lines[i])
        break
    if sub_indent is None or sub_indent <= role_indent:
        raise _hand_edit_error(f"角色 {role} 的缩进块是空的或缩进异常")

    def _find(key: str) -> list[int]:
        kre = re.compile(r"^[ \t]*" + re.escape(key) + r"[ \t]*:")
        out = []
        for i in range(sub_start, sub_end):
            ln = lines[i]
            if _is_skippable(ln):
                continue
            ind = _line_indent(ln)
            if ind > sub_indent:
                continue                    # 更深 = 嵌套内容（块标量/子映射），不是本角色的直接键
            if ind < sub_indent:
                raise _hand_edit_error(f"角色 {role} 块内缩进不齐（{ind} < {sub_indent}）")
            if not _MAP_KEY_RE.match(ln):
                # 列表项、流式续行、`]` 收尾行…… 都落这里：结构超出行级替换的把握范围。
                raise _hand_edit_error(f"角色 {role} 块内出现非 `键: 值` 的行")
            if kre.match(ln):
                out.append(i)
        return out

    adapter_hits = _find("adapter")
    out = list(lines)
    if adapter is not None:
        if len(adapter_hits) != 1:
            raise _hand_edit_error(
                f"角色 {role} 没有显式的 adapter 行（主绑定按角色名兜底）"
                if not adapter_hits else f"角色 {role} 有多行 adapter")
        body, eol = _split_eol(out[adapter_hits[0]])
        out[adapter_hits[0]] = _replace_bare_value(body, "adapter", adapter) + eol
    if not set_model:
        return out
    model_hits = _find("model")
    if len(model_hits) > 1:
        raise _hand_edit_error(f"角色 {role} 有多行 model")
    if model is None:
        if model_hits:
            del out[model_hits[0]]          # 整行删（含其行内注释——那句注释是在说这个键）
        return out
    if model_hits:
        body, eol = _split_eol(out[model_hits[0]])
        out[model_hits[0]] = _replace_bare_value(body, "model", model) + eol
        return out
    # 插入：紧跟 adapter 行之后、同缩进新起一行；没有 adapter 行则放块首。
    # （YAML 映射键无序，位置纯粹为了好读，不影响语义。）
    anchor = adapter_hits[0] if adapter_hits else None
    if anchor is None:
        for i in range(sub_start, sub_end):
            if not _is_skippable(out[i]):
                anchor = i
                break
    if anchor is None:
        raise _hand_edit_error(f"角色 {role} 块内找不到插入锚点")
    _, anchor_eol = _split_eol(out[anchor])
    new_line = " " * sub_indent + f"model: {model}"
    if anchor_eol:
        out.insert(anchor + 1, new_line + anchor_eol)
    else:
        # 锚点是没有末尾换行的文件末行：给它补上，新行自己不带（保持"原本没有末尾换行"）。
        out[anchor] = out[anchor] + dominant_eol
        out.insert(anchor + 1, new_line)
    return out


def _edit_inline_role(lines: list[str], ridx: int, role: str, head_end: int,
                      adapter: str | None, model: str | None,
                      set_model: bool) -> list[str]:
    """内联式（`pm: {adapter: …, …}` 单行花括号）的改写。"""
    body, eol = _split_eol(lines[ridx])
    open_at = body.index("{", head_end)
    close_at = _flow_scan(body, open_at)
    if close_at < 0:
        raise _hand_edit_error(f"角色 {role} 的花括号没有在同一行内闭合")
    tail = body[close_at + 1:]
    if tail.strip() and not tail.lstrip().startswith("#"):
        raise _hand_edit_error(f"角色 {role} 的花括号之后还有内容")
    segs = _flow_split(body[open_at + 1:close_at])
    for seg in segs:
        if _seg_key(seg) is None:
            raise _hand_edit_error(f"角色 {role} 花括号内有不是 `键: 值` 的项")

    def _idx(key: str) -> list[int]:
        return [i for i, s in enumerate(segs) if _seg_key(s) == key]

    adapter_idx = _idx("adapter")
    if adapter is not None:
        if len(adapter_idx) != 1:
            raise _hand_edit_error(
                f"角色 {role} 花括号内没有 adapter 项（主绑定按角色名兜底）"
                if not adapter_idx else f"角色 {role} 花括号内有多个 adapter 项")
        segs[adapter_idx[0]] = _replace_bare_value(segs[adapter_idx[0]], "adapter", adapter)
    if set_model:
        model_idx = _idx("model")
        if len(model_idx) > 1:
            raise _hand_edit_error(f"角色 {role} 花括号内有多个 model 项")
        if model is None:
            if model_idx:
                segs.pop(model_idx[0])      # 整项删（连同它前面那个逗号，由 join 自然消掉）
        elif model_idx:
            segs[model_idx[0]] = _replace_bare_value(segs[model_idx[0]], "model", model)
        else:
            pos = (adapter_idx[0] + 1) if adapter_idx else len(segs)
            segs.insert(pos, f" model: {model}")
    out = list(lines)
    out[ridx] = body[: open_at + 1] + ",".join(segs) + body[close_at:] + eol
    return out


def _edit_role_binding_text(raw: str, role: str, *, adapter: str | None,
                            model: str | None, set_model: bool) -> str:
    """在 config.yaml **原文**上做行级替换，返回新全文；认不出的写法抛 _ApiError(400)。

    纯函数：不读盘、不写盘。调用方负责改写后的自证（可解析 + 目标键就是想要的值 +
    其余解析产物完全相等 + §11.1 校验）与原子落盘。
    """
    lines = raw.splitlines(keepends=True)
    ridx, end, role_indent = _locate_role_line(lines, role)
    body, _eol = _split_eol(lines[ridx])
    key_re = re.compile(r"^[ \t]*" + re.escape(role) + r"[ \t]*:")
    head_end = key_re.match(body).end()
    rest = body[head_end:]
    dominant_eol = "\r\n" if "\r\n" in raw else "\n"
    if rest.lstrip().startswith("{"):
        out = _edit_inline_role(lines, ridx, role, head_end, adapter, model, set_model)
    elif (not rest.strip()) or rest.lstrip().startswith("#"):
        out = _edit_block_role(lines, ridx, end, role_indent, role,
                               adapter, model, set_model, dominant_eol)
    else:
        raise _hand_edit_error(
            f"角色 {role} 的值既不是缩进块也不是单行花括号（实得 {rest.strip()[:24]!r}）")
    return "".join(out)


def _ep_config_role_binding(ws: Path, body: dict) -> tuple[int, dict]:
    """POST /api/config/role-binding —— 改某角色的主绑定 adapter 与/或模型名。

    入参 {role, adapter?, model?}：adapter 与 model 至少给一项；``model: null`` 是
    **显式删键**（回落 adapter 层缺省），与"不传 model"是两回事。fallback 不在本端点。

    校验分工（各管各的，不互相兜底）：
      · role   —— 必须在**该 config 的 roles 集合**内，不在 → 404（不是 400：那是"没有
                  这个东西"，不是"参数写错了"）；
      · adapter—— 必须在已声明的 adapters 集合内，不在 → 400（写进去也是启动即报错）；
      · model  —— 只查"非空字符串或 null"。**不查名字有效性**：模型清单在各家 CLI 手里、
                  联网才知道，把清单写进代码等于每次供应商改名就撒谎（与
                  ``state.validate_availability_config`` 规则 3 的边界同源）。UI 侧的候选
                  下拉是提示，不是判据。
    """
    import copy

    import yaml  # pyyaml 已在依赖白名单

    from orch.adapters.state import validate_availability_config

    role = body.get("role")
    if not role or not isinstance(role, str):
        raise _ApiError(400, "role 必填（字符串）")
    role = role.strip()
    has_adapter, has_model = "adapter" in body, "model" in body
    if not has_adapter and not has_model:
        raise _ApiError(400, "adapter 与 model 至少给一项")
    adapter = None
    if has_adapter:
        adapter = body.get("adapter")
        if not isinstance(adapter, str) or not adapter.strip():
            raise _ApiError(400, "adapter 必须是非空字符串")
        adapter = adapter.strip()
        if not _YAML_BARE_VALUE_RE.match(adapter):
            raise _ApiError(400, f"adapter 名 {adapter!r} 含无法安全写入 YAML 的字符")
    model = None
    if has_model:
        model = body.get("model")
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise _ApiError(
                    400, "model 必须是非空字符串；要清除该角色的模型请传 null"
                         "（回落到 adapter 段的缺省）")
            model = model.strip()
            if not _YAML_BARE_VALUE_RE.match(model):
                raise _ApiError(
                    400, f"模型名 {model!r} 含空白/引号等字符，无法安全写成裸标量，"
                         f"请到配置页手工编辑")

    target = clim._workspace_config_path(ws)
    try:
        raw_bytes = target.read_bytes()
    except FileNotFoundError:
        raise _ApiError(404, f"该工作区还没有 config.yaml，无从改角色 {role} 的绑定")
    except OSError as exc:
        raise _ApiError(500, f"config.yaml 读取失败：{clim._one_line(exc)}") from exc
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _ApiError(400, "config.yaml 不是 UTF-8 编码，无法安全自动修改，请手工编辑") from exc
    try:
        before = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        # 先修语法再改绑：坏文件上做行级替换 = 在看不懂的东西上动刀。
        raise _ApiError(
            400, "config.yaml 当前无法解析（语法错），请先到配置页修好再改绑") from exc
    if not isinstance(before, dict):
        raise _ApiError(400, "config.yaml 顶层不是映射，无法改绑")
    roles_cfg = before.get("roles")
    if not isinstance(roles_cfg, dict) or role not in roles_cfg:
        raise _ApiError(404, f"config.yaml 的 roles 段里没有角色 {role}")
    adapters_cfg = before.get("adapters")
    if not isinstance(adapters_cfg, dict):
        adapters_cfg = {}
    if adapter is not None and adapter not in adapters_cfg:
        raise _ApiError(
            400, f"adapters 段未声明 {adapter}：请先在配置页声明它，再来改绑")

    new_raw = _edit_role_binding_text(
        raw, role, adapter=adapter, model=model, set_model=has_model)
    if new_raw == raw:
        # 没有实质改动（如删一个本就不存在的 model 键）：不写盘，盘上一个字节不动。
        return 200, {"ok": True, "changed": False, "role": role}

    # —— 改写后的三重自证（全过才落盘；任一条不过 = 盘上一个字节不动）——
    try:
        after = yaml.safe_load(new_raw)
    except yaml.YAMLError as exc:
        raise _hand_edit_error("改写后的 config 反而解析不了") from exc
    rc_after = after.get("roles", {}).get(role) if isinstance(after, dict) else None
    if not isinstance(rc_after, dict):
        raise _hand_edit_error("改写后角色段的形状变了")
    # ① 目标键确实变成了想要的值（防止引号/转义把值写成别的东西）。
    if adapter is not None and str(rc_after.get("adapter")) != adapter:
        raise _hand_edit_error("改写后 adapter 的值与请求不符")
    if has_model:
        if model is None and "model" in rc_after:
            raise _hand_edit_error("model 键没能删掉")
        if model is not None and str(rc_after.get("model")) != model:
            raise _hand_edit_error("改写后 model 的值与请求不符")
    # ② 其余配置**一个键都没波及**：把本次刻意改的键抹平后两份解析产物必须完全相等。
    #    字节层面由行级替换保证，这一条是语义层面的第二道闸（防定位错行改到别人头上）。
    b2, a2 = copy.deepcopy(before), copy.deepcopy(after)
    for doc in (b2, a2):
        rc = doc.get("roles", {}).get(role)
        if isinstance(rc, dict):
            if adapter is not None:
                rc.pop("adapter", None)
            if has_model:
                rc.pop("model", None)
    if b2 != a2:
        raise _hand_edit_error("改写波及了本次目标之外的配置")
    # ③ §11.1 装载期校验前置到写盘之前（与 PUT /api/config 同一函数、同一口径）：
    #    换到命令行含 {model} 占位的 adapter 而两层都没模型值，就在这里被拦下。
    errors = validate_availability_config(after)
    if errors:
        raise _ApiError(400, "配置校验未通过（§11.1）：" + "；".join(errors))

    _atomic_write_bytes(target, new_raw.encode("utf-8"))
    return 200, {"ok": True, "changed": True, "role": role,
                 "adapter": rc_after.get("adapter"), "model": rc_after.get("model")}


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

            # C3：结构化改绑（行级外科手术）。与 PUT /api/config 的"整份文本覆盖"
            # 并存而不重叠：整存是运维在配置页手改全文，本端点是名册上点两下改一个值。
            if parts == ["api", "config", "role-binding"]:
                if method != "POST":
                    raise _ApiError(405, "config/role-binding 仅支持 POST")
                return _ep_config_role_binding(ws, body)

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
                # 名字校验先行：任何 threads/{tid}/* 处理之前，且此前一步都不碰盘
                # （评审建议10；判据见 _check_tid）。
                _check_tid(tid)
                sub = parts[3] if len(parts) >= 4 else None

                if sub is None:
                    raise _ApiError(404, f"未知线程子路径: {'/'.join(parts)}")

                if sub == "events":
                    if method != "GET":
                        raise _ApiError(405, "events 仅支持 GET")
                    return _ep_thread_events(ws, tid)
                if sub == "board":
                    if method != "GET":
                        raise _ApiError(405, "board 仅支持 GET")
                    return _ep_thread_board(ws, tid)
                if sub == "status":
                    if method != "GET":
                        raise _ApiError(405, "status 仅支持 GET")
                    return _ep_thread_status(ws, tid)
                if sub == "steps":
                    if method != "GET":
                        raise _ApiError(405, "steps 仅支持 GET")
                    return _ep_thread_steps(ws, tid, query)
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
