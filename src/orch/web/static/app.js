/* orch 玻璃感控制台前端逻辑。
 *
 * 铁律：每个可点击控件都发起真实 fetch("/api/...") 并渲染真实响应/错误；
 * 无 alert("TODO")、无写死假数据、空状态显示真实空。
 *
 * 版式/性能红线（console-ui-revision.md E2）：阅读列/黑板栏/工具条一律纯
 * rgba + border，绝不 blur（全站唯一 backdrop-filter 仅顶栏）；transition
 * 只用 transform/opacity/background-color/border-color/color/box-shadow；
 * 闪烁/高亮只用 transform:scale + opacity 或 background-color 过渡。
 */

"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let selectedThread = null;
// D16 过滤状态（纯前端投影，不改数据、不发请求）。null = 不过滤该维度。
const filterState = { roles: null, types: null, aOnly: false };
// D18/D19：最近一次 status 明细缓存，供派发表展开与门禁 corr 提取。
let lastStatus = null;
// 当前线程事件缓存（供过滤重渲染 / 跳转，均为库的只读投影）。
let currentEvents = [];
// 黑板三节的**唯一**数据源 = GET /api/threads/{id}/board 的权威投影（服务端读
// blackboard/state.json）。前端**不再**据事件的 bb_ops 自行投影：落库 ≠ 生效，
// 被 §3.3 门槛拒绝的 ops 照样在事件里，据它投影会把被拒绝的自述算进完成度。
// null = 尚未取到 / 取失败 → 渲染成**显式失败态**，绝不退回前端投影兜底
// （错的黑板比"读不到"更坏：它长得跟真的一样）。
let lastBoard = null;
let lastBoardError = "";
// 载荷指纹：事件没变但黑板变了（如崩溃恢复后 rebuild）也要重绘。变化检测用，
// 不是缓存 —— 每次轮询都真取一次端点，只是内容一样时省掉一次 innerHTML 重排。
let lastBoardSig = "";
// 「本轮」统计：/events 响应的同级键 round_stats 原样存放（服务端每请求现算）。
// 前端**不重算**任何一项——它只随事件变化，故随 renderBoard 一起重绘即可。
let lastRoundStats = null;
// R5：最近一次线程列表缓存（切换器/标题复用，仍是端点数据的只读投影）。
let lastThreads = [];
// T4 View Steps：折叠组的**展开态 + 已取回的步骤摘要**（纯前端投影）。
// 为何要存：renderStream 每次变化都整流重建 innerHTML，DOM 里的展开态会被吞掉；
// 存下来后重建时按这两份回填，**不重打端点** —— 点开才 lazy fetch，1.5s 轮询
// 因此不会被放大成 N 次读盘（服务端每请求现读盘、零缓存，代价在这一侧省）。
// 键是事件号，故切线程/切工作区必须清（事件号跨线程会撞车）。
const stepsCache = new Map();   // eid → /steps 响应原样（前端不重算任何一项）
const stepsOpen = new Set();    // eid → 当前展开
// R5 D3：每线程"首个 A 类自动展开一次"记忆 + A 类计数变化检测（新条目高亮）。
const boardAutoShown = new Set();
let lastBoardACount = -1;
// ③迟到标记（P3 展示层）：最后一条 terminate 的事件号；其后落盘的非 system 事件
// = 终止前已在飞行中的在途回复（日志=真相，如实入账但加标记免困惑）。
let lateAfterId = null;
// ② 多工作区单控制台：当前工作区名（null=单工作区模式，请求不加 ?ws=）。
let currentWs = null;

// —— R5：UI 偏好（折叠态等视图偏好，可存 localStorage；不持有任何库状态） ——
const UIPREF = {
  get(k, d) {
    try { const v = localStorage.getItem("orch.ui." + k); return v === null ? d : v === "1"; }
    catch (e) { return d; }
  },
  raw(k) { try { return localStorage.getItem("orch.ui." + k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem("orch.ui." + k, v ? "1" : "0"); } catch (e) {} },
};

// ————————————————————————————————————————————————
// R5 D1/D3/D4：三区骨架折叠态（类切换，禁布局 transition）
// ————————————————————————————————————————————————
function setRailCollapsed(collapsed, persist = true) {
  $("#threads-layout").classList.toggle("rail-collapsed", collapsed);
  const t = $("#rail-toggle");
  if (t) { t.textContent = collapsed ? "⟩" : "⟨"; t.title = collapsed ? "展开线程栏" : "折叠线程栏"; }
  if (persist) UIPREF.set("railCollapsed", collapsed);
}
function setBoardOpen(open, persist = true) {
  $("#threads-layout").classList.toggle("board-open", open);
  if (persist) UIPREF.set("boardOpen", open);
}
function initLayoutPrefs() {
  // D9：<1280 线程栏默认折叠；黑板任何宽度默认 rail（D3）。localStorage 覆盖默认。
  const narrow = window.matchMedia("(max-width: 1279px)").matches;
  const stored = UIPREF.raw("railCollapsed");
  setRailCollapsed(stored === null ? narrow : stored === "1", false);
  setBoardOpen(UIPREF.get("boardOpen", false), false);
}

// ————————————————————————————————————————————————
// R5 浮层体系：backdrop + 模态（新建线程 / 回放·接入·明细 / 切换器）
// ————————————————————————————————————————————————
function openModal(id) {
  $("#modal-backdrop").classList.remove("hidden");
  $(id).classList.remove("hidden");
}
function closeModals() {
  $("#modal-backdrop").classList.add("hidden");
  $$(".modal").forEach((m) => m.classList.add("hidden"));
}
// Q4：回放/接入/派发明细改为真正浮层，不再注入事件流（轮询重渲染会吞流内假气泡）。
function showOverlay(title, html) {
  $("#op-title").textContent = title;
  $("#op-body").innerHTML = html;
  openModal("#overlay-panel");
}

// ————————————————————————————————————————————————
// 通用 fetch 封装：统一 JSON + 错误 toast（用后端真实 error 文案）。
// ————————————————————————————————————————————————
async function api(path, { method = "GET", body = null } = {}) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  // ② 多工作区：全部请求自动携带当前工作区（单工作区时 currentWs=null 不加参）。
  const url = currentWs
    ? path + (path.includes("?") ? "&" : "?") + "ws=" + encodeURIComponent(currentWs)
    : path;
  const resp = await fetch(url, opts);
  let data;
  const text = await resp.text();
  try {
    data = JSON.parse(text);
  } catch (e) {
    data = { _raw: text };
  }
  if (!resp.ok) {
    const msg = (data && data.error) ? data.error : `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  // 后端用 200 承载 {error}（如 config 校验失败）：也当错误抛。
  if (data && data.error) {
    throw new Error(data.error);
  }
  return data;
}

function toast(msg, kind = "ok") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = `toast ${kind}`;
  setTimeout(() => { t.className = "toast hidden"; }, 3200);
}

// ————————————————————————————————————————————————
// 顶栏：health + tab 切换
// ————————————————————————————————————————————————
// D2：工作区在系统临时目录时告警——事件日志是唯一真相层（spec §0 命题3），
// Temp 会被 Windows 磁盘清理/存储感知随时清空，属数据安全缺陷而非质感瑕疵。
function isTempWorkspace(p) {
  const s = String(p || "");
  // 兼容正反斜杠 + 大小写：%TEMP% 常见形态 与 AppData\Local\Temp。
  return /(^|[\\/])temp([\\/]|$)/i.test(s) || /appdata[\\/]local[\\/]temp/i.test(s);
}

// ————————————————————————————————————————————————
// ② 多工作区单控制台：顶栏下拉切换（>1 个才显示；偏好存 localStorage）。
// ————————————————————————————————————————————————
async function loadWorkspaces() {
  const sel = $("#ws-select");
  if (!sel) return;
  try {
    const w = await api("/api/workspaces");
    const list = w.workspaces || [];
    if (list.length <= 1) {
      sel.classList.add("hidden");
      currentWs = null;
      return;
    }
    let stored = null;
    try { stored = localStorage.getItem("orch.ui.ws"); } catch (e) { /* 忽略 */ }
    currentWs = list.some((x) => x.name === stored) ? stored : (w.default || list[0].name);
    sel.innerHTML = list.map((x) =>
      `<option value="${escapeHtml(x.name)}"${x.name === currentWs ? " selected" : ""}>` +
      `${escapeHtml(x.name)}</option>`
    ).join("");
    sel.classList.remove("hidden");
  } catch (e) {
    // 旧后端无 workspaces 端点：静默按单工作区模式。
    sel.classList.add("hidden");
    currentWs = null;
  }
}

function switchWorkspace(name) {
  if (name === currentWs) return;
  currentWs = name;
  try { localStorage.setItem("orch.ui.ws", name); } catch (e) { /* 忽略 */ }
  // 清干净线程态：停轮询、回空态首页，再拉新工作区数据。
  selectedThread = null;
  currentEvents = [];
  lastRoundStats = null;   // 跨工作区不沿用上一条线程的统计窗口
  lastBoard = null;        // 黑板同理：工作区换了，上一条线程的三节必须清掉
  lastBoardError = "";
  lastBoardSig = "";
  lastStatus = null;
  lastRoles = [];
  lastConfigError = "";   // 工作区换了，上一个工作区的 config 报错不该跟着走
  lastThreadRoles = [];
  lastActiveMember = null;
  stepsCache.clear();      // 步骤摘要按事件号存：跨工作区/线程会撞车，必须清
  stepsOpen.clear();
  clearTypingClock();      // 走字定时器也要停（建议12：否则切完还每秒空转一次）
  renderMemberRoster();
  unseenCount = 0;
  clearTimeout(pollTimer);
  pollActive = false;
  updateLiveIndicator("hidden");
  updateNewMsgsFloat();
  $("#workspace").classList.add("hidden");
  $("#workspace-empty").classList.remove("hidden");
  loadHealth();
  loadThreads();
  loadAdapters(true);   // 适配器状态是工作区级事实（状态文件与 config.yaml 同目录）
}

async function loadHealth() {
  const pathEl = $("#ws-path");
  const warnEl = $("#ws-warn");
  try {
    const h = await api("/api/health");
    pathEl.textContent = h.workspace;
    $("#conn-dot").className = "dot running";
    const danger = isTempWorkspace(h.workspace);
    pathEl.classList.toggle("danger", danger);
    warnEl.classList.toggle("hidden", !danger);
    if (danger) {
      const tip = "工作区位于临时目录，事件日志可能被系统清理";
      pathEl.title = tip;
      warnEl.title = tip;
      warnEl.setAttribute("aria-hidden", "false");
    } else {
      pathEl.title = h.workspace;
      warnEl.setAttribute("aria-hidden", "true");
    }
  } catch (e) {
    pathEl.textContent = "连接失败";
    pathEl.classList.remove("danger");
    pathEl.title = "";
    warnEl.classList.add("hidden");
    $("#conn-dot").className = "dot terminated";
    toast("连接后端失败: " + e.message, "err");
  }
}

function switchView(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "config") loadConfig();
  if (name === "metrics") loadMetrics();
  if (name === "adapters") loadAdapters();
}

// ————————————————————————————————————————————————
// M5 §12 适配器面板：列全部 adapter（✅/⛔ 徽章 + enable/disable 开关按钮，
// disable 可填 reason），行为与 CLI 两命令等价（同一原子替换写路径）。
// 刷新沿 D6 轮询节奏（pollLoop 内捎带），另有手动 ⟳ 与切页即拉。
// ————————————————————————————————————————————————
let adapterRows = [];

function fmtAdapterTs(ts) {
  const v = Number(ts || 0);
  if (!isFinite(v) || v <= 0) return "—";
  return fmtLastActivity(v);   // 状态变更时刻：今天显示时分，跨天带月日
}

function renderAdapters() {
  const box = $("#adapters-list");
  if (!box) return;
  // 轮询重渲染**不得**吞掉用户正在填的停用原因：先存后还。
  const typed = {};
  box.querySelectorAll(".adp-reason").forEach((i) => {
    if (i.value) typed[i.dataset.adpReason] = i.value;
  });
  if (!adapterRows.length) {
    box.innerHTML = '<div class="chat-empty">config.yaml 未声明 adapters，且状态文件无记录</div>';
    return;
  }
  box.innerHTML = adapterRows.map((r) => {
    const off = r.status !== "enabled";
    const name = escapeHtmlAttr(String(r.name));   // 同时进属性值与文本，按属性口径转义
    const meta = [
      off ? `by=${escapeHtml(String(r.by || "-"))}` : null,
      off && r.ts ? fmtAdapterTs(r.ts) : null,
      `fail_streak=${escapeHtml(String(r.fail_streak))}`,
    ].filter(Boolean).join(" · ");
    const reason = off && r.reason
      ? `<div class="adp-reason-txt">原因：${escapeHtml(String(r.reason))}</div>` : "";
    const ctl = off
      ? `<button class="btn-primary adp-btn" data-adp="${name}" data-act="enable">✅ 恢复</button>`
      : `<input class="inp adp-reason" data-adp-reason="${name}" placeholder="停用原因（可选）" />` +
        `<button class="btn-danger adp-btn" data-adp="${name}" data-act="disable">⛔ 停用</button>`;
    return (
      `<div class="adp-row${off ? " off" : ""}">` +
        `<span class="adp-badge ${off ? "off" : "ok"}">${off ? "⛔" : "✅"}</span>` +
        `<span class="adp-name mono">${name}</span>` +
        `<span class="adp-meta">${escapeHtml(meta)}</span>` +
        `<span class="adp-ctl">${ctl}</span>` +
        reason +
      `</div>`
    );
  }).join("");
  box.querySelectorAll(".adp-reason").forEach((i) => {
    const v = typed[i.dataset.adpReason];
    if (v) i.value = v;
  });
}

// M5 §12（评审 minor-2）：警示分两档——
//   · **阻塞**（某角色主绑定与全部备胎都停用，status 端点的 roles[].blocked）→ 红色点名角色；
//   · 仅"有 disabled 但备胎兜住" → 温和提示（编排在继续，不该长期红着吓人）。
// 角色投影的**单一数据源**：status 端点的 roles[]（两条线都往这里写——事件轮询的
// applyStatusPayload，与 N5 减负后只喂角色渲染的可用性心跳）。切线程/切工作区清空。
let lastRoles = [];
// config.yaml 读不出来时 status 端点给的一句人话（无错误则后端不给该键 → 恒为 ""）。
// 它与 lastRoles 是**互斥**的两种事实：有 config_error 时后端一律给空投影，
// 前端必须把"读不出来"说出来，否则"一枚 chip 都不渲染"与"一切正常"长得一模一样。
let lastConfigError = "";

function blockedRolesOf() {
  return lastRoles.filter((r) => r.blocked).map((r) => String(r.role));
}

function renderRoleBindings() {
  const el = $("#role-bindings");
  if (!el) return;
  if (lastConfigError) {
    el.innerHTML = `<span class="rb-chip blocked" title="${escapeHtmlAttr(lastConfigError)}">` +
                   "⚠ 配置读取失败·绑定未知</span>";
    return;
  }
  const rows = lastRoles;
  // 只渲染"有话要说"的角色（降级中或阻塞）：一切正常时一枚不渲染（恒态 chip = 噪音）。
  const notable = rows.filter((r) => r.blocked || r.effective !== r.primary);
  if (!notable.length) { el.innerHTML = ""; return; }
  el.innerHTML = notable.map((r) => {
    const role = escapeHtml(String(r.role));
    const primary = escapeHtml(String(r.primary));
    if (r.blocked) {
      return `<span class="rb-chip blocked" title="主绑定 ${primary} 与全部备胎均已停用：` +
             `待办保持 pending，等待人工恢复">${role} ⚠ 无可用</span>`;
    }
    return `<span class="rb-chip degraded" title="主绑定 ${primary} 已停用，本轮由备胎承接">` +
           `${role} ⛔${primary}→${escapeHtml(String(r.effective))}</span>`;
  }).join("");
}

function updateAdapterAlerts() {
  const offNames = adapterRows.filter((r) => r.status !== "enabled").map((r) => r.name);
  const blocked = blockedRolesOf();
  // config 读不出来 → **最高优先级**警示。必须先判它：此时后端给空投影，blocked 恒空，
  // 不先判就会掉进"无话可说"分支把警示条静默隐藏，正是评审"应修3"的假绿形态。
  const text = lastConfigError
    ? `${lastConfigError}　—— 角色绑定与可用性无法判定，控制台不显示绑定行；` +
      "修好 config.yaml 后自动恢复。"
    : (blocked.length
        ? `角色 ${blocked.join("、")} 无可用适配器（主绑定与全部备胎均已停用）：` +
          "相关待办保持 pending 不消耗重试预算，需人工恢复后自动接手。"
        : (offNames.length
            ? `已停用 ${offNames.length} 个适配器：${offNames.join("、")}` +
              "　—— 相关角色已降级到备胎，编排继续。"
            : ""));
  const show = Boolean(text);
  const strong = Boolean(lastConfigError || blocked.length);   // 强档：红；否则温和档
  const bar = $("#adapter-warn");
  if (bar) {
    bar.classList.toggle("hidden", !show);
    bar.classList.toggle("mild", show && !strong);
    const t = $("#adapter-warn-text");
    if (t) t.textContent = text;
    const ic = $("#adapter-warn-icon");
    if (ic) ic.textContent = strong ? "⛔" : "⚠";
  }
  const alert = $("#adapters-alert");
  if (alert) {
    alert.classList.toggle("hidden", !show);
    alert.classList.toggle("mild", show && !strong);
    alert.textContent = show ? (strong ? "⛔ " : "⚠ ") + text : "";
  }
  const dot = $("#tab-adp-dot");
  if (dot) dot.classList.toggle("hidden", !offNames.length);
}

async function loadAdapters(quiet = false) {
  try {
    const d = await api("/api/adapters");
    adapterRows = d.adapters || [];
  } catch (e) {
    if (!quiet) {
      const box = $("#adapters-list");
      if (box) box.innerHTML = `<div class="chat-empty">加载失败: ${escapeHtml(e.message)}</div>`;
      toast("载入适配器失败: " + e.message, "err");
    }
    return;
  }
  renderAdapters();
  updateAdapterAlerts();
}

async function setAdapter(name, act) {
  const body = { name };
  if (act === "disable") {
    const inp = document.querySelector(`.adp-reason[data-adp-reason="${CSS.escape(name)}"]`);
    const reason = inp ? inp.value.trim() : "";
    if (reason) body.reason = reason;
  }
  try {
    const d = await api(`/api/adapters/${act}`, { method: "POST", body });
    adapterRows = d.adapters || adapterRows;
    renderAdapters();
    updateAdapterAlerts();
    toast(act === "disable" ? `已停用 ${name}` : `已恢复 ${name}（fail_streak 清零）`);
  } catch (e) {
    toast((act === "disable" ? "停用失败: " : "恢复失败: ") + e.message, "err");
  }
}

// ————————————————————————————————————————————————
// 常驻成员名册（F4/F2）：线程头下方一条横栏，**每成员恒有一枚状态点**。
// 数据源 = /status 同一响应的 roles 投影（role/primary/effective/blocked）
// + 全量 dispatches 行（五态 + deadline_ts），纯前端计算，不新增端点。
// 点击成员 = 专人单聊：按该成员过滤消息流 + 把 #send-to 锁到该角色（再点取消）。
// ————————————————————————————————————————————————
// /api/threads 给的该线程 roles；仅作 lastRoles 为空（无 config / 状态文件不可读，
// server.py:_role_binding_projection 会返回空投影）时的兜底名单，保证名册"常驻"。
let lastThreadRoles = [];

// 展示侧时钟容差：浏览器 Date.now() 与服务端 time.time()（deadline_ts 的来源）可能
// 不同源——控制台常开在另一台机器上，NTP 未必对齐。几秒漂移就会让绿↔灰在轮询间来回
// 翻，故给 3 秒宽限。**只影响展示档位**，绝不参与任何调度/超时判定（那条线一律用
// 服务端落盘的绝对时间戳，§16.2）。
const DISPATCH_CLOCK_SKEW_S = 3;

// dispatching 行是否**真的在跑**：必须判绝对截止时间戳。进程崩溃后 dispatching 行会
// 滞留在盘上（src/orch/scheduler/watchdog.py:203-205："行留在盘上会被每一轮 check
// 重新枚举"），只看 status 就会长亮假绿 / 假"正在响应"。
function isLiveDispatch(d) {
  if (!d || d.status !== "dispatching") return false;
  const dl = Number(d.deadline_ts);
  return isFinite(dl) && dl > Date.now() / 1000 - DISPATCH_CLOCK_SKEW_S;
}

// 成员状态点：blocked=⛔无可用适配器 · busy=绿正在响应 · queued=琥珀排队中 ·
// 其余一律灰。灰档**再细分**（视觉同色、不加新色，差别只在 title）：崩溃滞留的
// dispatching 行 / failed 行 / gate_wait 行 / 真的一条派发行都没有——盘上有 failed
// 或 gate_wait 行却写"无派发行"是撒谎，故各自如实成文。
function memberDotState(role, dispatches, blocked) {
  if (blocked) return "blocked";
  let queued = false;
  let stale = false;
  let failed = false;
  let gateWait = false;
  for (const d of dispatches || []) {
    if (String(d.target) !== role) continue;
    if (isLiveDispatch(d)) return "busy";
    if (d.status === "dispatching") stale = true;
    else if (d.status === "pending") queued = true;
    else if (d.status === "failed") failed = true;
    else if (d.status === "gate_wait") gateWait = true;
  }
  if (queued) return "queued";
  if (gateWait) return "gate_wait";
  if (failed) return "failed";
  if (stale) return "stale";
  return "idle";
}

// 归灰的全部档位（四色红线：只有 blocked/busy/queued 各占一色，其余共用灰）。
const MEMBER_DOT_GRAY = new Set(["idle", "stale", "failed", "gate_wait"]);

const MEMBER_DOT_TITLE = {
  busy: "正在响应（dispatching 行未过截止时间）",
  queued: "排队中（有 pending 派发行；要等下一次运行才真正 invoke，不是 IM 即时语义）",
  gate_wait: "等待门禁（有 gate_wait 派发行：须人工批准/驳回后才继续）",
  failed: "有 failed 派发行（该派发已判失败，不再自动重试）",
  stale: "无在跑派发（有已过截止的 dispatching 行：进程可能已崩溃，等看门狗对账）",
  idle: "待命（无派发行）",
  blocked: "无可用适配器：主绑定与全部备胎均已停用，待办保持 pending 等人工恢复",
};

// 单聊激活成员 = **派生量**（不另存一份状态）：角色筛选恰好锁定一名成员时即单聊态。
function activeMemberRole() {
  if (!filterState.roles || filterState.roles.size !== 1) return null;
  return Array.from(filterState.roles)[0];
}

// 上一拍的单聊成员。**唯一用途**：实现"取消即解锁 #send-to"的对称语义，不是状态源
// （单聊态本身仍由 filterState 派生）。放在 readFilters 里比较，是为了让**所有**离开
// 单聊的路径都覆盖到——再点一次取消、筛选面板"清除全部"、chips 行 ✕ 移除该角色、
// 手动取消勾选，四条路都经 readFilters。
let lastActiveMember = null;

// 人类展示判据（成员名册单聊 / 角色筛选专用）：事件是否与该成员相关 =
// **他发的，或发给他的**。
// 它**不是** spec §6.2 的焦点窗（那是给模型的不对称视图渲染判据，实现在
// src/orch/render/__init__.py）；二者有意分离，本函数只喂前端展示过滤，
// **不得回流任何调度判定**（路由/重试/聚合/超时/保留策略一律与它无关）。
function isMemberRelated(ev, role) {
  return ev.sender === role || (ev.to || []).includes(role);
}

// 点击名册成员：激活/取消单聊。复用既有角色筛选通道（筛选 chips 行、"清除全部"照常生效）。
// 对称承诺：激活时把 #send-to 锁到该成员，取消时**必须**解锁（解锁在 readFilters 内
// 统一做，见 lastActiveMember）——否则下一条人类消息会静默直发他而非走 moderator 兜底。
function toggleMemberChat(role) {
  if (!role) return;
  const on = activeMemberRole() === role;
  $$("#filter-roles input").forEach((c) => { c.checked = !on && c.value === role; });
  if (!on) {
    // @成员语义**只**通过写 #send-to.value 实现；禁止任何对消息正文的解析
    // （spec §16 第 1 条：从正文解析 @ 做路由）。方向只能是信封 → 显示。
    const sel = $("#send-to");
    if (sel && Array.from(sel.options).some((o) => o.value === role)) sel.value = role;
  }
  readFilters();   // 重渲染事件流 + 筛选 chips + 名册高亮（readFilters 内已带上名册）
}

function renderMemberRoster() {
  const box = $("#member-roster");
  if (!box) return;
  if (!selectedThread) { box.className = "member-roster"; box.innerHTML = ""; return; }
  const rows = (lastRoles && lastRoles.length)
    ? lastRoles
    : lastThreadRoles.map((r) => ({ role: r, primary: null, effective: null, blocked: false }));
  if (!rows.length) { box.className = "member-roster"; box.innerHTML = ""; return; }
  const disp = (lastStatus && lastStatus.dispatches) || [];
  const active = activeMemberRole();
  const tstatus = (lastStatus && lastStatus.status) || "";
  // 终态（terminated/suspended）：整栏降饱和 + 标注，避免把终态快照读成活状态。
  const terminal = tstatus === "terminated" || tstatus === "suspended";
  box.className = "member-roster" + (terminal ? " terminal" : "");
  const chips = rows.map((r) => {
    const role = String(r.role);
    const st = memberDotState(role, disp, r.blocked === true);
    const dotCls = MEMBER_DOT_GRAY.has(st) ? "idle" : st;   // 灰档细分只体现在 title
    const col = escapeHtmlAttr(roleColor(role));
    // 「兜底路由」徽标：to 为空的信封一律落到 moderator（spec §5.2 / §4.4(1)）。
    // 「可裁决」徽标本卡未做：can_decide 只在 config.yaml 的 roles 段里，而
    // GET /api/config 回的是**原始 YAML 文本**（server.py:_ep_config_get），响应里
    // 没有解析后的 roles；前端零依赖铁律下无 YAML 解析器，手搓子集解析会把徽标挂错
    // （错标"可裁决"比不标更坏），故此处只出 moderator 一枚。
    const badge = role === "moderator"
      ? `<span class="mr-badge fallback">兜底路由</span>` : "";
    // config 读不出来时名册走的是 lastThreadRoles 兜底（blocked 恒 false）——那个 false
    // 是"不知道"而不是"没阻塞"，必须在 tip 里说清，否则名册会替坏配置背书成健康态。
    const bindTip = lastConfigError
      ? "；config.yaml 读取失败，绑定与可用性未知"
      : ((r.effective && r.primary && r.effective !== r.primary)
          ? `；主绑定 ${r.primary} 已停用，本轮由 ${r.effective} 承接` : "");
    const tip = (MEMBER_DOT_TITLE[st] || "") + bindTip +
      "；点击 = 只看与该成员相关的消息并把发送目标锁到他（再点一次取消）";
    return (
      `<button class="mr-chip${active === role ? " active" : ""}"` +
      ` data-member="${escapeHtmlAttr(role)}" title="${escapeHtmlAttr(tip)}">` +
        `<span class="mr-dot d-${escapeHtmlAttr(dotCls)}"></span>` +
        `<span class="mr-name" style="color:${col}">${escapeHtml(role)}</span>` +
        badge +
      `</button>`
    );
  }).join("");
  const note = terminal
    ? `<span class="mr-note">${escapeHtml(tstatus === "terminated"
        ? "线程已终止 · 名册为终态快照" : "线程挂起中 · 待门禁裁决")}</span>`
    : (active
        ? `<span class="mr-note">单聊 ${escapeHtml(active)} · 再点该成员取消</span>` : "");
  box.innerHTML = `<span class="mr-label">成员</span>` + chips + note;
}

// ————————————————————————————————————————————————
// 线程列表 + 新建
// ————————————————————————————————————————————————
// D20：线程列表项增强——主标题=E1 正文前 20 字摘要；次要行=id（等宽）·
// 状态文字（running/suspended/terminated 取代裸色点）·事件数·最后活动时间。
// suspended 项加琥珀徽标（D19）。数据全来自 /api/threads（后端已投影）。
const STATUS_TEXT = { running: "running", suspended: "suspended", terminated: "terminated" };

function summarize(text, n) {
  const s = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  if (!s) return "（无正文）";
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function fmtClock(tsSec) {
  // ev.ts 是 epoch 秒（REAL）。tabular-nums 由 CSS 提供。
  const n = Number(tsSec);
  if (!isFinite(n) || n <= 0) return "";
  const d = new Date(n * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtLastActivity(tsSec) {
  const n = Number(tsSec);
  if (!isFinite(n) || n <= 0) return "—";
  const d = new Date(n * 1000);
  const p = (x) => String(x).padStart(2, "0");
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (sameDay) return `${p(d.getHours())}:${p(d.getMinutes())}`;
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function loadThreads() {
  const list = $("#thread-list");
  try {
    const threads = await api("/api/threads");
    lastThreads = threads;
    if (!threads.length) {
      list.innerHTML = '<li class="empty">暂无线程</li>';
      return;
    }
    list.innerHTML = "";
    for (const t of threads) {
      const li = document.createElement("li");
      li.className = "thread-item";
      if (t.id === selectedThread) li.classList.add("selected");
      const status = t.status || "unknown";
      const statusCls = STATUS_TEXT[status] ? status : "terminated";
      const title = summarize(t.summary, 20);
      const suspended = status === "suspended";
      // D20 主标题=摘要；次要行=id·状态文字·事件数·最后活动。D19 suspended 琥珀徽标。
      li.dataset.tid = t.id;
      // 基础行即时渲染（id/状态文字/角色数占位）；摘要/事件数/最后活动由
      // enrichThreadRow 从 events 端点（本卡唯一放开的只读投影）异步补全——
      // 不改 /api/threads 端点（本卡 server 白名单只放开 events 端点）。
      // R5 D4：折叠 rail 态显示状态色圆徽 + 首字（展开态由 CSS 隐藏）。
      const stColor = status === "running" ? "#4ade80" : (status === "suspended" ? "#fbbf24" : "#94a3b8");
      li.innerHTML =
        `<span class="ti-mini" style="color:${stColor};border-color:${stColor}66;background:${stColor}1f" title="${escapeHtml(title)}">${escapeHtml(title[0] || "?")}</span>` +
        `<div class="ti-title">` +
          (suspended ? `<span class="ti-gate" title="门禁待审批">⚠ 待审批</span>` : "") +
          `<span class="ti-summary">${escapeHtml(title)}</span>` +
        `</div>` +
        `<div class="ti-meta">` +
          `<span class="ti-id">${escapeHtml(t.id)}</span>` +
          `<span class="ti-status s-${statusCls}">${escapeHtml(STATUS_TEXT[status] || status)}</span>` +
          `<span class="ti-count">${Number((t.roles || []).length)} 角色</span>` +
          `<span class="ti-time">—</span>` +
        `</div>`;
      li.addEventListener("click", () => selectThread(t.id));
      list.appendChild(li);
      enrichThreadRow(li, t.id);
    }
  } catch (e) {
    list.innerHTML = `<li class="empty">加载失败: ${escapeHtml(e.message)}</li>`;
    toast("刷新线程失败: " + e.message, "err");
  }
}

// D20 渐进增强：从 events 端点取该线程 E1 摘要 + 事件数 + 最后活动时间，
// 回填列表行（不阻塞基础渲染；events 是本卡唯一放开的只读投影端点）。
async function enrichThreadRow(li, tid) {
  try {
    const data = await api(`/api/threads/${tid}/events`);
    const evs = data.events || [];
    if (!li.isConnected) return;
    const sumEl = li.querySelector(".ti-summary");
    const cntEl = li.querySelector(".ti-count");
    const timeEl = li.querySelector(".ti-time");
    if (evs.length && sumEl) {
      // E1 = 首条事件正文（剥自指前缀后）前 20 字。
      const e1 = evs[0];
      const clean = stripSelfViewPrefix(e1.body || "", {
        id: e1.id, sender: e1.sender, type: e1.type, to: e1.to || [],
      });
      const s = summarize(clean, 20);
      sumEl.textContent = s;
      const mini = li.querySelector(".ti-mini");
      if (mini) { mini.textContent = s[0] || "?"; mini.title = s; }
    }
    if (cntEl) cntEl.textContent = `${evs.length} 事件`;
    if (timeEl && evs.length) timeEl.textContent = fmtLastActivity(evs[evs.length - 1].ts);
  } catch (e) {
    /* 增强失败静默：基础行仍可用（id/状态/角色数） */
  }
}

async function createThread() {
  const task = $("#new-task").value.trim();
  if (!task) { toast("请填写任务描述", "err"); return; }
  const roles = $$("#new-roles input:checked").map((c) => c.value);
  if (!roles.length) { toast("至少选一个角色", "err"); return; }
  const btn = $("#btn-create-thread");
  btn.disabled = true;
  try {
    const r = await api("/api/threads", { method: "POST", body: { task, roles } });
    toast("已建线程 " + r.id + "（E1 入队）");
    $("#new-task").value = "";
    closeModals();
    await loadThreads();
    await selectThread(r.id);
  } catch (e) {
    toast("建线程失败: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

// ————————————————————————————————————————————————
// 选中线程 → 载入事件/状态/发言下拉
// ————————————————————————————————————————————————
async function selectThread(tid) {
  selectedThread = tid;
  lastBoardACount = -1;   // 跨线程不比较 A 类计数（防误闪烁）
  lastBoard = null;       // 黑板是线程内事实：切线程即作废，绝不让上一条的三节留屏
  lastBoardError = "";
  lastBoardSig = "";
  lastRoundStats = null;  // 统计窗口是线程内概念，切线程即作废（loadEvents 立刻重填）
  lastRoles = [];         // 跨线程不沿用上一条线程的角色投影
  lastConfigError = "";   // 同理：config 报错是工作区级事实，下一拍 status 会重填
  lastThreadRoles = [];   // 名册兜底名单同理（populateSendTo 会立刻重填）
  stepsCache.clear();     // 事件号是线程内标识：切线程必须清，否则回填到别人的气泡上
  stepsOpen.clear();
  clearTypingClock();     // 同上（建议12）：清投影 + 停走字定时器，一并做掉
  availTick = 0;          // 新线程立刻补一次角色投影（心跳第一拍就取）
  // 切线程清掉角色筛选（含单聊态）：成员名单换了，留着旧成员的过滤只会显示空流。
  // #send-to 由 populateSendTo 重建 options（value 自然回到兜底首项），故此处只清标记。
  filterState.roles = null;
  lastActiveMember = null;
  $$("#filter-roles input:checked").forEach((c) => { c.checked = false; });
  updateFilterUI();
  renderMemberRoster();
  $("#workspace-empty").classList.add("hidden");
  $("#workspace").classList.remove("hidden");
  $("#wk-title").textContent = tid;   // 占位；loadEvents 后由 E1 摘要替换
  $("#wk-title").title = tid;
  $$("#thread-list li").forEach((li) => {
    li.classList.toggle("selected", li.querySelector(".ti-id")?.textContent === tid);
  });
  unseenCount = 0;
  updateNewMsgsFloat();
  await Promise.all([loadEvents(), loadStatus(), populateSendTo()]);
  startPolling();   // R5 D6：选中即进入实时跟新（terminated 会自动停轮）
}

// D3：目标下拉——首项语义 = to=[]（走 moderator 兜底路由，spec §5.2）；
// 首项文案不得出现广播式措辞（广播是 spec §16 反模式第3条，禁止）。
async function populateSendTo() {
  const sel = $("#send-to");
  try {
    const threads = await api("/api/threads");
    const t = threads.find((x) => x.id === selectedThread);
    const roles = (t && t.roles) ? t.roles : [];
    lastThreadRoles = roles.slice();   // 名册兜底名单（/status 的 roles 投影为空时用）
    sel.innerHTML = '<option value="">由 moderator 路由</option>';
    for (const r of roles) {
      if (r === "human") continue; // human 是自己，不作为发送目标项
      const o = document.createElement("option");
      o.value = r; o.textContent = r;
      sel.appendChild(o);
    }
    // D16：角色过滤下拉也随线程角色刷新。
    populateFilterRoles(roles);
    renderMemberRoster();   // 名册先按线程 roles 出人（状态点等 /status 回来再精化）
  } catch (e) {
    /* 保底：下拉至少有默认项（HTML 内已含首项） */
  }
}

// D6：type 下拉按 §3.2 发送者约束过滤——只含 human 允许发送的类型。
// human 可 can_decide 故保留 decision；排除 system（仅编排器）、gate_decision
// （仅经门禁审批流产生）、gate_request/acceptance/terminate 等非自由发言类型。
// 默认值动态：线程无事件（新线程）→ assign（首条派活）；已有事件 → question。
const HUMAN_SEND_TYPES = [
  "assign", "chat", "question", "answer",
  "handoff", "report", "review", "decision",
];

function populateSendType(hasEvents) {
  const sel = $("#send-type");
  if (!sel) return;
  const prev = sel.value; // 保留用户当前选择（若仍合法）
  sel.innerHTML = "";
  for (const ty of HUMAN_SEND_TYPES) {
    const o = document.createElement("option");
    o.value = ty; o.textContent = ty;
    sel.appendChild(o);
  }
  if (prev && HUMAN_SEND_TYPES.includes(prev)) {
    sel.value = prev;
  } else {
    sel.value = hasEvents ? "question" : "assign";
  }
}

// ————————————————————————————————————————————————
// D9 角色视觉身份：固定角色→色映射，全站一致（深底）。
// ————————————————————————————————————————————————
const ROLE_COLORS = {
  human: "#4ade80",
  moderator: "#94a3b8",
  pm: "#a78bfa",
  backend: "#60a5fa",
  frontend: "#22d3ee",
  tester: "#fbbf24",
  system: "#64748b",
};
function roleColor(role) {
  return ROLE_COLORS[role] || ROLE_COLORS.system;
}

// D11：type 徽章语义类（14 种全量）。CSS 类 tb-{type} 提供语义色。
// A 类事件（spec §3.2 保留策略 A）：decision/acceptance/gate_request/
// gate_decision/terminate——卡片整体加重（边框换 A 类色）+ "已入黑板"图钉。
const A_CLASS_TYPES = new Set([
  "decision", "acceptance", "gate_request", "gate_decision", "terminate",
]);
// 14 种全量 type（校验/过滤下拉用）。
const ALL_TYPES = [
  "assign", "review", "question", "answer", "decision", "handoff",
  "report", "defect", "acceptance", "gate_request", "gate_decision",
  "system", "terminate", "chat",
];

// D1 防御性剥离：库内 body 本身干净（C1 根因=前端曾把 §6.2 视图行当正文），
// 但仍加一道防御——仅当 body 首行严格等于"本信封自身"的视图行
// `#{id} [{from}->@{to}] (type): ` 时才剥（id/from/to/type 逐项与卡片头系统字段
// 完全一致）。这样正文中"引用他人消息"的 #n[...] 片段不会被误伤。
// 参数 sys = {id, sender, type, to:[...]} 取自本信封系统字段。
function stripSelfViewPrefix(body, sys) {
  const s = String(body == null ? "" : body);
  const m = /^#(\d+)\s+\[([^\]]*)\]\s+\(([^)]*)\):[ \t]?/.exec(s);
  if (!m) return s;
  const [, idStr, label, typeStr] = m;
  // 重建"期望前缀"的角色标签：from->@to1,@to2（与 render._to_labels 同序同格式）。
  const toLabel = (sys.to && sys.to.length)
    ? sys.to.map((r) => "@" + r).join(",")
    : "@";
  const expectLabel = `${sys.sender}->${toLabel}`;
  const same =
    Number(idStr) === Number(sys.id) &&
    label === expectLabel &&
    typeStr === String(sys.type);
  // 完全一致才认定是自指视图前缀，剥掉首行前缀；否则原样返回（防误伤引用）。
  return same ? s.slice(m[0].length) : s;
}

// ————————————————————————————————————————————————
// D14 安全 markdown 渲染（原生，无外部库/CDN）。
// 安全铁律（防 XSS，F7）：
//   1) 先对整段 body 做 escapeHtml（转义 & < > "），此后原文里任何 HTML
//      标签都已成为可见文本（`<script>` → `&lt;script&gt;`），不可能执行。
//   2) 仅在"已转义文本"上，用正则把有限 markdown 语法还原为受控白名单标签
//      （code/pre/strong/em/ul/ol/li/a/br）。绝不把原始 body 作为 innerHTML 直插。
//   3) 链接 href 仅允许 http/https（禁 javascript:/data: 等），href 值再次转义引号。
//   4) 代码块/行内代码内部内容不再二次解析 markdown（用占位符隔离）。
// ————————————————————————————————————————————————
function escapeHtmlAttr(s) {
  // 属性值上下文：在 &<> 基础上再转义引号（escapeHtml 只转 &<>）。
  return escapeHtml(s).replaceAll('"', "&quot;");
}

function safeLinkHref(raw) {
  // 仅放行 http/https 绝对链接；其余（javascript:/data:/相对/协议相对）一律拒绝。
  const u = String(raw || "").trim();
  if (/^https?:\/\//i.test(u)) return u;
  return null;
}

function renderInlineMd(escaped) {
  // 输入：已 escapeHtml 的文本片段（无 code 占位）。输出：受控内联 HTML。
  let s = escaped;
  // 行内代码 `code`：内容已是转义文本，包进 <code> 即可（内部不再解析）。
  s = s.replace(/`([^`\n]+)`/g, (_, c) => `<code>${c}</code>`);
  // 链接 [text](url)：url 经 http/https 白名单校验，失败则退化为纯文本。
  s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
    const href = safeLinkHref(url);
    if (!href) return m; // 非法协议：原样保留转义文本，不生成链接
    return `<a href="${escapeHtmlAttr(href)}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  });
  // 粗体 **text**（在斜体前，避免 * 抢占）。
  s = s.replace(/\*\*([^*\n]+)\*\*/g, (_, t) => `<strong>${t}</strong>`);
  // 斜体 *text*（避开已消耗的 **）。
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, (_, pre, t) => `${pre}<em>${t}</em>`);
  return s;
}

function renderMarkdown(rawBody) {
  // ① 全文先转义——这一步之后，body 中不可能再存在可执行 HTML。
  const escapedAll = escapeHtml(String(rawBody == null ? "" : rawBody)).replaceAll('"', "&quot;");
  // ② 抽取代码块 ```...```，用占位符隔离（内部不参与后续 markdown 解析）。
  const codeBlocks = [];
  let text = escapedAll.replace(/```[^\n]*\n?([\s\S]*?)```/g, (_, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push(code.replace(/\n$/, ""));
    return ` CB${idx} `;
  });

  // ③ 按行处理块级结构：无序/有序列表 + 段落，行内再走 renderInlineMd。
  const lines = text.split("\n");
  const out = [];
  let listType = null; // 'ul' | 'ol' | null
  const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };

  for (const line of lines) {
    // 代码块占位行：整行就是占位符 → 直接吐 <pre><code>。
    const cbMatch = /^ CB(\d+) $/.exec(line.trim());
    if (cbMatch) {
      closeList();
      out.push(`<pre class="md-pre"><code>${codeBlocks[Number(cbMatch[1])]}</code></pre>`);
      continue;
    }
    const ul = /^\s*[-*]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+\.\s+(.*)$/.exec(line);
    if (ul) {
      if (listType !== "ul") { closeList(); out.push('<ul class="md-ul">'); listType = "ul"; }
      out.push(`<li>${renderInlineMd(ul[1])}</li>`);
    } else if (ol) {
      if (listType !== "ol") { closeList(); out.push('<ol class="md-ol">'); listType = "ol"; }
      out.push(`<li>${renderInlineMd(ol[1])}</li>`);
    } else if (line.trim() === "") {
      closeList();
      out.push("__BR__");
    } else {
      closeList();
      out.push(`<span class="md-line">${renderInlineMd(line)}</span>`);
    }
  }
  closeList();
  // 相邻两个 md-line 之间插 <br>（段落内换行）；空行折叠；列表/代码块相邻不插。
  // 用哨兵 __BR__ 标空行后剔除，避免全局 \n→<br> 触及代码块内部（曾致 bug）。
  const parts = out.filter((x) => x !== "");
  const html = [];
  for (let k = 0; k < parts.length; k++) {
    const cur = parts[k];
    if (cur === "__BR__") continue;
    html.push(cur);
    const nxt = parts[k + 1];
    // 仅当当前与下一片段都是 md-line（段落内相邻行）才补 <br>。
    if (cur.startsWith('<span class="md-line">') && nxt && nxt.startsWith('<span class="md-line">')) {
      html.push("<br>");
    }
  }
  return html.join("");
}

// ————————————————————————————————————————————————
// D12 回复链 chip + D13 时间/meta/verify 徽章 + D11 徽章/A 类加重。
// ————————————————————————————————————————————————
function buildReplyChips(ev) {
  const parts = [];
  for (const n of (ev.re || [])) {
    // 平滑滚动至目标卡 + 高亮闪烁（点击处理在 loadEvents 里委托绑定）。
    parts.push(`<button class="ln-chip re-chip" data-goto="${escapeHtml(String(n))}" title="跳到 #${escapeHtml(String(n))}">↩ 回复 #${escapeHtml(String(n))}</button>`);
  }
  if (ev.corr) {
    const c = String(ev.corr);
    // corr 语义标签：门禁 gate-xx / 作业 job-xx，其余直出。
    const label = /^gate/i.test(c) ? c : (/^job/i.test(c) ? c : c);
    parts.push(`<span class="ln-chip corr-chip" title="关联 corr=${escapeHtml(c)}">🔗 ${escapeHtml(label)}</span>`);
  }
  return parts.length ? `<div class="b-links">${parts.join("")}</div>` : "";
}

// D13 verify 徽章：acceptance 卡必须显示——exit_code===0 绿"verify ✓ exit 0"，
// 缺失或非零 红"⚠ 无系统侧验证"（spec §8.3：无硬证据的验收会被降级，UI 须可见）。
function buildVerifyBadge(ev) {
  if (ev.type !== "acceptance") return "";
  const v = (ev.meta && ev.meta.verify) || null;
  if (v && Number(v.exit_code) === 0) {
    return `<span class="verify-badge ok" title="系统侧 verify 通过（exit 0）">verify ✓ exit 0</span>`;
  }
  const code = (v && v.exit_code !== undefined && v.exit_code !== null) ? ` (exit ${escapeHtml(String(v.exit_code))})` : "";
  return `<span class="verify-badge bad" title="缺系统侧验证证据（meta.verify）或退出码非 0，验收会被降级为 report">⚠ 无系统侧验证${code}</span>`;
}

// D13 meta hover 浮层：tokens_in/out、duration_s（来自 ev.meta）。
function buildMetaTip(ev) {
  const m = ev.meta || {};
  const bits = [];
  if (m.tokens_in !== undefined && m.tokens_in !== null) bits.push(`tokens_in ${escapeHtml(String(m.tokens_in))}`);
  if (m.tokens_out !== undefined && m.tokens_out !== null) bits.push(`tokens_out ${escapeHtml(String(m.tokens_out))}`);
  if (m.duration_s !== undefined && m.duration_s !== null) bits.push(`duration ${escapeHtml(String(m.duration_s))}s`);
  if (!bits.length) return "";
  return `<span class="meta-info" tabindex="0" aria-label="事件 meta">ⓘ<span class="meta-pop">${bits.join(" · ")}</span></span>`;
}

// D14 artifacts chips：路径等宽字体，只读展示（不下载，避免越权）。
function buildArtifactChips(ev) {
  const arts = ev.artifacts || [];
  if (!arts.length) return "";
  const chips = arts.map((a) =>
    `<span class="art-chip" title="${escapeHtml(String(a))}">📄 ${escapeHtml(String(a))}</span>`
  ).join("");
  return `<div class="b-arts">${chips}</div>`;
}

// ————————————————————————————————————————————————
// T4 View Steps：每条**后端回复**气泡挂一个执行流折叠组（QUESTIONS.md Q11 裁决 A）。
//
// 边界（写进代码旁，改这段前先读）：
//   · 只作**人类展示**。步骤既不回流任何调度判定（路由/重试/聚合/超时），也不进
//     orch.render 视图层——spec §7.1 行396「调度器不知道信封背后是一步还是一百步」
//     的主语是调度器，展示层看明细不越界。
//   · 数据全部来自 GET /steps 的**解析后摘要**；stdout 原文不经 HTTP（原文含 sessionId
//     的实证见 QUESTIONS.md Q9），页面上永远拿不到原文，要看原文去 logs/。
//   · **点开才取数**：折叠组构建函数里没有任何 fetch，取数只在点击处理里发生一次。
//   · acceptance/decision 上额外标注"后端自述·非系统验证"，且折叠组固定在气泡
//     **末尾**（verify 徽章在正文之上），两者不相邻：§16 第 5 条—— meta.verify 才是
//     系统侧证据，后端自述的工具明细不得与它争夺证据地位。
// ————————————————————————————————————————————————
// 需要"这是自述、不是系统验证"标注的型：黑板/验收类（A 类里带裁决效力的三型）。
const SELF_CLAIM_TYPES = new Set(["acceptance", "decision", "gate_decision"]);
// 步骤字形（tool/thinking/text 各一形，其余归 ·）。
const STEP_GLYPH = { tool: "🔧", thinking: "💭", text: "💬", other: "·" };
// 字形位与 CSS 档位的**前端白名单**（评审 建议11）：kind 虽然由服务端枚举产出，
// 但它源头是模型可控文本，前端不拿服务端契约当输入校验——白名单外一律归 other，
// 于是 `k-` 类名永远只有四种字面量，不存在"把服务端串拼进 class 属性"这条路。
const STEP_KIND_CLASS = { tool: "tool", thinking: "thinking", text: "text" };
function stepKindClass(kind) {
  return STEP_KIND_CLASS[String(kind)] || "other";
}

// 空态文案：一律照**盘上事实**说话（同 bd-fail 的口径：读不到 ≠ 没有）。
function stepsEmptyText(res) {
  // 取数本身失败时先认这一条：那时 wire_format 是前端填的 null，不是服务端的判定，
  // 照 wire_format 分支说话会把网络/服务端故障误报成"格式判不出"。
  if (res && res.read_failed) return "执行步骤读取失败（读不到 ≠ 没有步骤）";
  // wire_format 是服务端**从日志原文嗅探**出的格式（不是当前绑定的配置值）。
  const wf = res && res.wire_format;
  if (wf === "json" || wf === "text") return `该后端（wire_format=${wf}）不产生步骤流`;
  if (res && !res.log_file) return "未找到本次执行日志";
  // 下面这句刻意写成服务端 note 的**前缀**（服务端会补"（可能产生自其他历史绑定），
  // 不猜测"）：renderStepsBody 的空态去重靠 startsWith，改一个字就会两句并出重复。
  if (!wf) return "无法判定该日志的流式格式";
  return "本次执行日志里没有可解析的步骤";
}

function renderStepsBody(res) {
  if (!res) return `<div class="steps-empty">正在读取执行日志…</div>`;
  const rows = res.steps || [];
  const counts = res.counts || {};
  const chips = Object.keys(counts)
    .filter((k) => Number(counts[k]) > 0)
    .map((k) => `<span class="sc-chip">${escapeHtml(k)} ${escapeHtml(String(counts[k]))}</span>`)
    .join("");
  const meta =
    (res.wire_format ? `<span class="sc-chip">wire ${escapeHtml(String(res.wire_format))}</span>` : "") +
    // truncated：步骤列表不完整（条数触顶 / 只解析了日志尾部）。必须显式标出——
    // 一个不带标记的 500 步列表会被读成"这就是全部"（原因由服务端 note 给）。
    (res.truncated ? `<span class="sc-chip warn">已截断</span>` : "") +
    (res.log_file ? `<span class="sc-chip mono" title="${escapeHtmlAttr("logs/" + res.log_file)}">日志 ${escapeHtml(String(res.log_file))}</span>` : "");
  const head = (chips || meta) ? `<div class="steps-counts">${chips}${meta}</div>` : "";
  // 服务端的一句人话（多份日志/无配置等）原样带出，不改写、不吞。
  const note = (res.note) ? `<div class="steps-note">${escapeHtml(String(res.note))}</div>` : "";
  if (!rows.length) {
    // 空态**只出一句**：服务端 note 多半就是前端通用文案的详版（同一句开头 + 括注
    // 原因），那时只出 note——它信息更全，且原样不改写；两句都出会读成"说了两遍"。
    // note 说的是另一回事（多份日志、读取失败等）时才两句并出，不吞任何一句。
    const line = stepsEmptyText(res);
    const said = res.note ? String(res.note) : "";
    if (said.startsWith(line)) {
      return head + `<div class="steps-empty">${escapeHtml(said)}</div>`;
    }
    return head + `<div class="steps-empty">${escapeHtml(line)}</div>` + note;
  }
  const list = rows.map((s) => {
    // kind 过前端白名单（建议11）：class 与 title 都只用白名单值，不回显服务端串。
    const kind = stepKindClass(s.kind);
    const glyph = STEP_GLYPH[kind] || STEP_GLYPH.other;
    const dur = (s.dur_ms === undefined || s.dur_ms === null)
      ? ""
      : `<span class="step-dur mono">${escapeHtml(String(s.dur_ms))} ms</span>`;
    // summary 是**模型可控文本**（工具命令/正文摘要）：文本位一律 escapeHtml。
    return (
      `<div class="step-row k-${kind}">` +
        `<span class="step-seq mono">${escapeHtml(String(s.seq))}</span>` +
        `<span class="step-glyph" title="${kind}">${glyph}</span>` +
        `<span class="step-name">${escapeHtml(String(s.name || kind))}</span>` +
        `<span class="step-sum">${escapeHtml(String(s.summary || ""))}</span>` +
        dur +
      `</div>`
    );
  }).join("");
  return head + `<div class="steps-list">${list}</div>` + note;
}

function buildStepsBlock(ev) {
  const sender = ev.sender || "system";
  // human 是人类发言、system 是编排器审计事件：都不是一次 invoke，不挂折叠组。
  if (sender === "human" || sender === "system") return "";
  const eid = Number(ev.id);
  const res = stepsCache.has(eid) ? stepsCache.get(eid) : null;
  const open = stepsOpen.has(eid);
  // N 未知（尚未取数）时按钮只写 "View Steps"，取回后回填计数——不预告不知道的数。
  const label = res ? `View Steps · ${(res.steps || []).length}` : "View Steps";
  const claim = SELF_CLAIM_TYPES.has(ev.type)
    ? `<span class="steps-selfclaim" title="${escapeHtmlAttr("这些步骤来自后端自己的输出原文（logs/），不是系统侧验证；系统侧证据只有正文上方的 meta.verify 徽章")}">后端自述·非系统验证</span>`
    : "";
  return (
    `<div class="b-steps">` +
      `<div class="steps-head">` +
        `<button class="steps-toggle" data-steps-eid="${escapeHtmlAttr(String(eid))}"` +
        ` data-expanded="${open ? "1" : "0"}"` +
        ` title="${escapeHtmlAttr("展开这次 invoke 的执行步骤（读 logs/ 原文解析出的摘要，点开才取）")}">` +
          `${open ? "▾" : "▸"} ${escapeHtml(label)}</button>` +
        claim +
      `</div>` +
      `<div class="steps-body${open ? "" : " hidden"}">${open ? renderStepsBody(res) : ""}</div>` +
    `</div>`
  );
}

// 只重绘某一条气泡的折叠组（不走 renderStream 整流：那会打断滚动位置与展开态）。
function paintStepsBlock(eid) {
  const ev = currentEvents.find((e) => Number(e.id) === Number(eid));
  if (!ev) return;
  const sel = `#chat-stream .bubble[data-eid="${CSS.escape(String(eid))}"] .b-steps`;
  const host = $(sel);
  if (!host) return;
  host.outerHTML = buildStepsBlock(ev);
}

// 点开时的**唯一**取数点（lazy）：一条气泡一次，结果进 stepsCache 供重建回填。
async function loadSteps(eid) {
  const tid = selectedThread;
  if (!tid) return;
  try {
    const res = await api(`/api/threads/${tid}/steps?event_id=${encodeURIComponent(eid)}`);
    if (tid !== selectedThread) return;      // 线程已切换：丢弃过期响应
    stepsCache.set(Number(eid), res);
  } catch (e) {
    // 取不到就如实写"取不到"，不留空白也不编空态（读不到 ≠ 没有步骤）。
    // read_failed 标记让空态说"读取失败"而不是照 wire_format=null 说"查不到配置"。
    stepsCache.set(Number(eid), {
      steps: [], counts: {}, wire_format: null, log_file: null, read_failed: true,
      note: "执行步骤读取失败: " + (e.message || "未知错误"),
    });
  }
  paintStepsBlock(eid);
}

// D9 卡片头：一行式 = 头像圆点 · 名字 · → · @目标chips · type 徽章 · [已入黑板]
//            · (弹性空隙) · meta · #n · 时间。
// @目标只从信封 to 渲染（§16.1，禁广播）。
function buildEventHead(ev) {
  const sender = ev.sender || "system";
  const col = roleColor(sender);
  const initial = escapeHtml((sender[0] || "?").toUpperCase());
  const chips = (ev.to || []).map((r) => {
    const c = roleColor(r);
    return `<span class="to-chip" style="color:${c};border-color:${c}66">@${escapeHtml(r)}</span>`;
  }).join("");
  const arrow = (ev.to && ev.to.length) ? '<span class="head-arrow">→</span>' : "";
  const type = ev.type || "";
  const isA = A_CLASS_TYPES.has(type);
  // D11：type 徽章语义类 tb-{type}；A 类附"已入黑板"图钉。
  const typeBadge = `<span class="ev-type tb-${escapeHtml(type)}">${escapeHtml(type)}</span>`;
  const pin = isA ? `<span class="bb-pin" title="A 类事件：已投影进黑板">📌 已入黑板</span>` : "";
  // ③迟到标记：终止后到达的在途回复（非 system）。
  const late = (lateAfterId !== null && ev.id > lateAfterId && sender !== "system")
    ? `<span class="late-pin" title="该调用在线程终止前已发出，回复在终止后落盘（在途回复，如实入账）">⏱ 终止后到达</span>`
    : "";
  const clock = fmtClock(ev.ts);
  const clockEl = clock ? `<span class="ev-time" title="ts">${clock}</span>` : "";
  return (
    `<div class="b-head">` +
      `<span class="avatar" style="background:${col}1f;color:${col};border-color:${col}66">${initial}</span>` +
      `<span class="speaker" style="color:${col}">${escapeHtml(sender)}</span>` +
      arrow +
      `<span class="to-chips">${chips}</span>` +
      typeBadge +
      pin +
      late +
      `<span class="head-spacer"></span>` +
      buildMetaTip(ev) +
      `<span class="ev-id">#${escapeHtml(String(ev.id))}</span>` +
      clockEl +
    `</div>`
  );
}

// D15 折叠：正文渲染后若行数 > 8 则 clamp + 展开/收起。
const CLAMP_LINES = 8;

function buildBubble(ev) {
  const div = document.createElement("div");
  const type = ev.type || "";
  const isA = A_CLASS_TYPES.has(type);
  div.className = `bubble r-${ev.sender || "system"}` + (isA ? ` a-class a-${escapeHtml(type)}` : "");
  div.dataset.eid = String(ev.id);
  // D1：正文渲染 body 原文；先自指前缀防御剥离，再 D14 安全 markdown。
  const clean = stripSelfViewPrefix(ev.body || "", {
    id: ev.id, sender: ev.sender, type: ev.type, to: ev.to || [],
  });
  const bodyHtml = renderMarkdown(clean);
  // 行数估算（用于 D15 clamp）：原文换行数 + 1。
  const lineCount = (clean.match(/\n/g) || []).length + 1;
  const clampCls = lineCount > CLAMP_LINES ? " clamped" : "";
  div.innerHTML =
    buildEventHead(ev) +
    buildReplyChips(ev) +
    buildVerifyBadge(ev) +
    `<div class="b-body${clampCls}">${bodyHtml}</div>` +
    buildArtifactChips(ev) +
    (lineCount > CLAMP_LINES
      ? `<button class="toggle-clamp" data-expanded="0">展开（${lineCount} 行）</button>`
      : "") +
    // T4：执行流折叠组固定在**最末**——与正文上方的 verify 徽章隔着整段正文，
    // 不与它相邻争位（§16 第 5 条，见 buildStepsBlock 顶部注释）。
    buildStepsBlock(ev);
  return div;
}

// D16 过滤投影：把 currentEvents 按 filterState 过滤后返回（不改数据）。
function applyFilters(evs) {
  return evs.filter((ev) => {
    if (filterState.aOnly && !A_CLASS_TYPES.has(ev.type)) return false;
    if (filterState.roles && filterState.roles.size) {
      // 角色维度走 isMemberRelated（发出 ∪ 收到）：旧判据只看 ev.sender，选中 backend
      // 时看不到"发给 backend"的消息，单聊只有半边。events 端点早已回 to，纯前端可修。
      let hit = false;
      for (const r of filterState.roles) { if (isMemberRelated(ev, r)) { hit = true; break; } }
      if (!hit) return false;
    }
    if (filterState.types && filterState.types.size && !filterState.types.has(ev.type)) return false;
    return true;
  });
}

// R5 D6：滚动锚定——视口贴底判定（±40px 容差）。
function isAtBottom(stream) {
  return stream.scrollTop + stream.clientHeight >= stream.scrollHeight - 40;
}

// R5 D6：事件摄取——变化检测（长度 + 末事件号），无变化不重渲染（性能红线）；
// 有变化时按锚定规则滚动：贴底自动跟入；上翻则冻结位置并累计"↓ n 条新消息"。
// 返回本次是否真的重渲染了（黑板另一条线据此决定要不要补一次 renderBoard）。
function ingestEvents(evs, force = false) {
  const stream = $("#chat-stream");
  const prevLen = currentEvents.length;
  const changed = force
    || evs.length !== prevLen
    || (evs.length > 0 && prevLen > 0
        && evs[evs.length - 1].id !== currentEvents[prevLen - 1].id);
  currentEvents = evs;
  if (!changed) return false;
  const stick = force || isAtBottom(stream);
  updateThreadTitle();
  populateSendType(evs.length > 0);
  renderBoard(evs);
  renderStream(stick);
  refreshGateFromEvents(evs, lastStatus && lastStatus.status);
  if (!stick) unseenCount += Math.max(0, evs.length - prevLen);
  else unseenCount = 0;
  updateNewMsgsFloat();
  return true;
}

// 黑板取数：失败**不**连坐事件流（两个端点各读各的），但失败必须显式落到面板上。
async function fetchBoard(tid) {
  try {
    return { data: await api(`/api/threads/${tid}/board`), err: "" };
  } catch (e) {
    return { data: null, err: e.message || "读取失败" };
  }
}

// 应用一次 board 载荷（loadEvents 与 pollLoop 两条取数线共用），返回是否有变化。
function applyBoardPayload(res) {
  const sig = res.err ? "ERR:" + res.err : JSON.stringify(res.data);
  const changed = sig !== lastBoardSig;
  lastBoardSig = sig;
  lastBoard = res.err ? null : (res.data || null);
  lastBoardError = res.err;
  return changed;
}

async function loadEvents(force = true) {
  if (!selectedThread) return;
  const tid = selectedThread;
  const stream = $("#chat-stream");
  try {
    const [data, bd] = await Promise.all([
      api(`/api/threads/${tid}/events`),
      fetchBoard(tid),
    ]);
    if (tid !== selectedThread) return;   // 线程已切换：丢弃过期响应
    lastRoundStats = data.round_stats || null;   // 供 renderBoard 的统计卡取用
    const boardChanged = applyBoardPayload(bd);
    // 事件没变但黑板变了（如恢复期 rebuild）：ingestEvents 会跳过重渲染，这里补一次。
    if (!ingestEvents(data.events || [], force) && boardChanged) renderBoard(currentEvents);
  } catch (e) {
    stream.innerHTML = `<div class="chat-empty">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

// R5 D5：线程头标题 = E1 摘要（id 收进 title 提示）。
function updateThreadTitle() {
  const el = $("#wk-title");
  if (!el || !selectedThread) return;
  if (currentEvents.length) {
    const e1 = currentEvents[0];
    const clean = stripSelfViewPrefix(e1.body || "", {
      id: e1.id, sender: e1.sender, type: e1.type, to: e1.to || [],
    });
    el.textContent = summarize(clean, 32);
    el.title = `${selectedThread} · ${summarize(clean, 80)}`;
  } else {
    el.textContent = selectedThread;
    el.title = selectedThread;
  }
}

// 依据当前过滤状态渲染事件流（D15 连续 system 聚合 + clamp/展开委托）。
function renderStream(stickBottom = false) {
  const stream = $("#chat-stream");
  const prevTop = stream.scrollTop;   // R5 D6：非贴底时冻结滚动位置
  const termIds = currentEvents.filter((e) => e.type === "terminate").map((e) => e.id);
  lateAfterId = termIds.length ? Math.max(...termIds) : null;   // ③迟到标记基准
  const shown = applyFilters(currentEvents);
  if (!shown.length) {
    stream.innerHTML = currentEvents.length
      ? '<div class="chat-empty">（当前过滤无匹配事件）</div>'
      : '<div class="chat-empty">E1 已入队，运行一轮开始派发</div>';
    return;
  }
  stream.innerHTML = "";
  // D15：连续 system 事件聚合为一条可展开分组行，默认折叠。
  let i = 0;
  while (i < shown.length) {
    const ev = shown[i];
    if (ev.type === "system") {
      let j = i;
      const group = [];
      while (j < shown.length && shown[j].type === "system") { group.push(shown[j]); j++; }
      stream.appendChild(buildSystemGroup(group));
      i = j;
    } else {
      stream.appendChild(buildBubble(ev));
      i++;
    }
  }
  if (stickBottom) stream.scrollTop = stream.scrollHeight;
  else stream.scrollTop = prevTop;
}

// D15：连续 system 事件分组行（默认折叠，点击展开逐条卡片）。
function buildSystemGroup(group) {
  const wrap = document.createElement("div");
  wrap.className = "sys-group";
  if (group.length === 1) {
    // 单条 system：直接作为普通（弱化）卡片，不做分组包裹。
    return buildBubble(group[0]);
  }
  const ids = group.map((g) => "#" + g.id).join(" ");
  wrap.innerHTML =
    `<button class="sys-group-head" data-expanded="0">▸ ${group.length} 条系统事件（${escapeHtml(ids)}）</button>` +
    `<div class="sys-group-body hidden"></div>`;
  const body = wrap.querySelector(".sys-group-body");
  for (const g of group) body.appendChild(buildBubble(g));
  return wrap;
}

// D12 点击 re chip：平滑滚动至目标卡 + 高亮闪烁一次（transform/opacity/bg 过渡）。
function gotoEvent(eid) {
  const stream = $("#chat-stream");
  let target = stream.querySelector(`.bubble[data-eid="${CSS.escape(String(eid))}"]`);
  // 若目标在折叠的 system 分组里，先展开该分组。
  if (!target) {
    const groups = $$("#chat-stream .sys-group");
    for (const g of groups) {
      const inner = g.querySelector(`.bubble[data-eid="${CSS.escape(String(eid))}"]`);
      if (inner) {
        const head = g.querySelector(".sys-group-head");
        const gbody = g.querySelector(".sys-group-body");
        if (gbody && gbody.classList.contains("hidden")) {
          gbody.classList.remove("hidden");
          if (head) { head.dataset.expanded = "1"; head.textContent = head.textContent.replace("▸", "▾"); }
        }
        target = inner;
        break;
      }
    }
  }
  if (!target) { toast(`#${eid} 不在当前过滤视图内`, "err"); return; }
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.remove("flash");
  // 触发重排以便重放动画。
  void target.offsetWidth;
  target.classList.add("flash");
  setTimeout(() => target.classList.remove("flash"), 1200);
}

// ————————————————————————————————————————————————
// D17 黑板栏：三节（契约/决策/任务状态）一律**只**渲染权威黑板，
// 数据源 = GET /api/threads/{id}/board（服务端读 blackboard/state.json，§4.6）。
//
// 旧实现（前端据 A 类事件的 bb_ops 自投影）已**退役**，无兜底：它按 5 型 A 类放行
// 且完全不判 can_decide，而 §3.3 的门槛是「type ∈ {decision, acceptance,
// gate_decision} 且发件角色 can_decide」（protocol/rules.py:64-70）。可达反例：
// can_decide=false 的角色发 acceptance + set_task status=done —— 调度器
// (scheduler/core.py:678-688) 拒绝应用并追加 system 审计事件，但 ops 照样落库
// (store/__init__.py:319/345)，前端照投，✓ 与「第 N/M 步」于是把**被拒绝的自述**
// 算进完成度 —— §16 第 5 条"采信 agent 自述"的展示层形态。
// 端点读不到时渲染显式失败态：错的黑板比"读不到"更坏（它长得跟真的一样）。
//
// 三节与 state.json 字段的对应：
//   contracts {name:{version,path,frozen_at}} → 契约（#evt 取 frozen_at）
//   decisions [{evt,text}]                    → 决策（应用序，端点原样直出）
//   tasks     {key:status}                    → 任务状态；次序取端点 task_order
//                                               （声明序），#evt 取 task_evt
// ————————————————————————————————————————————————

// 待办状态的**展示层**归一化：spec §3.3 里 set_task 的 status 是自由字符串
// （protocol/schema.py 无枚举），这里只把常见写法映射到三档字形，**不写回**、
// 不规范化任何落盘数据——是展示映射，不是数据清洗。未识别的值原文照排在字形后
// （不吞不改），原始 status 另进 title。
const TASK_DONE_WORDS = new Set(["done", "completed", "closed", "resolved", "完成", "已完成"]);
const TASK_DOING_WORDS = new Set(["doing", "in_progress", "wip", "running", "进行中"]);

function taskGlyph(status) {
  const raw = String(status == null ? "" : status).trim();
  const key = raw.toLowerCase();
  if (TASK_DONE_WORDS.has(key)) return { glyph: "✓", cls: "done", text: "已完成", raw };
  if (TASK_DOING_WORDS.has(key)) return { glyph: "◐", cls: "doing", text: "进行中", raw };
  // 其余（含未知）归"未开始"：空值显示分类名，非空则原文照排。
  return { glyph: "○", cls: "todo", text: raw || "未开始", raw, unknown: Boolean(raw) };
}

// 黑板行的 #evt 跳转 chip。事件号缺失时如实出"—"并把原因写进 title：
// 权威 state.json 的 tasks 不带事件号（见端点注释），溯源查不到就是查不到，
// 不编一个号出来，也不静默省掉这一列。
function bdGotoChip(evt, missingTip = "权威状态里没有留下该条目的事件号") {
  if (evt === undefined || evt === null || evt === "") {
    return `<span class="bd-noevt" title="${escapeHtmlAttr(missingTip)}">—</span>`;
  }
  const s = String(evt);
  return `<button class="ln-chip bd-goto" data-goto="${escapeHtmlAttr(s)}">#${escapeHtml(s)}</button>`;
}

// 「本轮」统计卡：mm:ss（超一小时 h:mm:ss）。
// ts 由 append 时 time.time() 赋值、库内递增；负值只可能来自人工造数，钳到 0
// 免得显示成读不通的 -0:05（原始秒数仍在 title 里，不吞）。
function fmtDuration(sec) {
  const v = Number(sec);
  if (!isFinite(v)) return "—";
  const total = Math.max(0, Math.floor(v));
  const p = (x) => String(x).padStart(2, "0");
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  return h > 0 ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}

// 统计卡三栏 = 耗时 / 步骤 / invoke 次数。数据**全部**取自 /events 响应的同级键
// round_stats（服务端每请求现算），前端不重算——口径只留一处，避免两处漂移。
// 「工具数」一栏仍**不做**：T4 修复合规债后工具调用在 logs/ 有痕迹了，但那是逐次
// invoke 的日志文件，凑一栏"本轮工具数"要把窗口内每条回复的日志都读一遍求和（1.5s
// 轮询下最重的读盘热点）。工具明细改由每条气泡的 View Steps 折叠组按需给出。
function buildRoundStatsSection() {
  const st = lastRoundStats;
  if (!st) return "";
  const anchor = st.anchor_event_id;
  const tip = (anchor === null || anchor === undefined)
    ? "本线程尚无人类消息：窗口 = 全部事件"
    : `窗口 = 最后一条人类消息 #${anchor} 及其后的全部事件（含该条）`;
  const cell = (val, lbl, title) =>
    `<div class="rs-cell" title="${escapeHtmlAttr(title)}">` +
      `<span class="rs-val mono">${escapeHtml(val)}</span>` +
      `<span class="rs-lbl">${escapeHtml(lbl)}</span></div>`;
  return (
    `<section class="bd-sec rs-sec">` +
      `<h4>本轮</h4>` +
      `<div class="rs-grid">` +
        cell(fmtDuration(st.duration_s), "耗时", `${st.duration_s} 秒（窗口内末条 ts − 首条 ts）`) +
        cell(String(st.steps), "步骤", "窗口内事件数") +
        cell(String(st.invokes), "invoke 次数", "窗口内 sender 非 human/system 的事件数") +
      `</div>` +
      `<p class="rs-note" title="${escapeHtmlAttr(tip)}">自最后一条人类消息起</p>` +
    `</section>`
  );
}

function renderBoard(evs) {
  const el = $("#board-content");
  if (!el) return;
  const bd = lastBoard;                       // null = 端点没读到（失败态，非空态）
  const contracts = (bd && bd.contracts) || {};
  const decisions = (bd && bd.decisions) || [];
  const tasks = (bd && bd.tasks) || {};       // {key: status}，status 是落盘原文
  const taskEvt = (bd && bd.task_evt) || {};
  const names = Object.keys(contracts).sort();
  // 待办次序 = 端点给的声明序（服务端据事件首次声明号算，见端点注释）；端点没给
  // 该键时退回权威 tasks 的键序，**不**在前端另造一套排序判据。
  const taskKeys = ((bd && bd.task_order) || Object.keys(tasks))
    .filter((k) => Object.prototype.hasOwnProperty.call(tasks, k));

  // 读不到黑板：三节都如实说读不到（绝不显示成"暂无"——那是在替 store 编空态）。
  const failMsg = `黑板读取失败：${escapeHtml(lastBoardError || "未知原因")}`;
  const failLi = `<li class="bd-empty bd-fail">${failMsg}</li>`;
  const failTr = `<tr><td colspan="3" class="bd-empty bd-fail">${failMsg}</td></tr>`;

  const contractRows = names.length
    ? names.map((n) => {
        const c = contracts[n] || {};
        return `<li><span class="bd-name">${escapeHtml(n)}</span>` +
          `<span class="bd-ver">v${escapeHtml(String(c.version))}</span>` +
          `<span class="bd-path mono">${escapeHtml(String(c.path || ""))}</span>` +
          bdGotoChip(c.frozen_at) + `</li>`;
      }).join("")
    : (bd ? '<li class="bd-empty">决策冻结后会出现在这里</li>' : failLi);

  const decisionRows = decisions.length
    ? decisions.map((d) =>
        `<li><span class="bd-text">${escapeHtml(String(d.text == null ? "" : d.text))}</span>` +
        bdGotoChip(d.evt) + `</li>`
      ).join("")
    : (bd ? '<li class="bd-empty">decision 类事件自动沉淀到这里</li>' : failLi);

  const taskRows = taskKeys.length
    ? taskKeys.map((k) => {
        const g = taskGlyph(tasks[k]);
        const stTip = g.unknown
          ? `未识别状态「${g.raw}」：原文照排，归为未开始`
          : `落盘 status=${g.raw || "（空）"}`;
        return `<tr class="bd-task tk-${escapeHtmlAttr(g.cls)}">` +
          `<td class="bd-task-key"><span class="task-glyph">${g.glyph}</span>` +
          `<span class="mono">${escapeHtml(k)}</span></td>` +
          `<td><span class="task-status ts-${escapeHtmlAttr(g.cls)}" title="${escapeHtmlAttr(stTip)}">${escapeHtml(g.text)}</span></td>` +
          `<td>${bdGotoChip(taskEvt[k], "权威状态里有这一项，但事件里找不到声明它的 A 类事件")}</td></tr>`;
      }).join("")
    : (bd ? '<tr><td colspan="3" class="bd-empty">PM 建立任务后显示状态</td></tr>' : failTr);

  // 进度头「第 N/M 步」：M/N 只数**权威**任务（被拒绝的自述连 tasks 都进不来，
  // 自然不进分母），完成判定用下面同一套展示映射。M=0 时不出进度头。
  const doneCount = taskKeys.filter((k) => taskGlyph(tasks[k]).cls === "done").length;
  const progress = taskKeys.length
    ? `<span class="bd-progress" title="${escapeHtmlAttr(`已完成 ${doneCount} / 共 ${taskKeys.length} 项（只数权威黑板里的任务）`)}">` +
      `第 ${escapeHtml(String(doneCount))}/${escapeHtml(String(taskKeys.length))} 步</span>`
    : "";

  el.innerHTML =
    `<section class="bd-sec"><h4>契约</h4><ul class="bd-list">${contractRows}</ul></section>` +
    `<section class="bd-sec"><h4>决策</h4><ul class="bd-list">${decisionRows}</ul></section>` +
    buildRoundStatsSection() +
    `<section class="bd-sec"><h4>任务状态${progress}</h4><table class="bd-tasks"><thead><tr><th>任务</th><th>状态</th><th></th></tr></thead><tbody>${taskRows}</tbody></table></section>`;

  // R5 D3：rail 图标条 A 类计数徽标。
  const aCount = evs.filter((e) => A_CLASS_TYPES.has(e.type)).length;
  const badge = $("#board-count");
  if (badge) {
    badge.textContent = String(aCount);
    badge.classList.toggle("hidden", aCount === 0);
  }
  // 首个 A 类落盘 → 自动展开一次（此后尊重用户折叠选择，不再自动展开）。
  const layout = $("#threads-layout");
  if (aCount > 0 && selectedThread && !boardAutoShown.has(selectedThread)) {
    boardAutoShown.add(selectedThread);
    if (!layout.classList.contains("board-open")) setBoardOpen(true, false);
  }
  // 新条目高亮（仅同线程内计数增长且面板展开时；transform/bg 过渡，无布局动画）。
  if (lastBoardACount >= 0 && aCount > lastBoardACount && layout.classList.contains("board-open")) {
    const items = el.querySelectorAll(".bd-list li:not(.bd-empty), .bd-tasks tbody tr");
    const last = items[items.length - 1];
    if (last) { last.classList.add("flash"); setTimeout(() => last.classList.remove("flash"), 1200); }
  }
  lastBoardACount = aCount;
}

// ————————————————————————————————————————————————
// D16 过滤工具条：角色多选、type 多选、只看 A 类、#n 跳转。纯前端投影。
// ————————————————————————————————————————————————
function populateFilterRoles(roles) {
  const box = $("#filter-roles");
  if (!box) return;
  const uniq = Array.from(new Set(roles || []));
  if (!uniq.includes("system")) uniq.push("system"); // system 事件可能出现
  box.innerHTML = uniq.map((r) => {
    const c = roleColor(r);
    // value 走 escapeHtmlAttr（escapeHtml 不转引号）：该属性值已是名册单聊的匹配关键
    // 路径——toggleMemberChat 拿 c.value 与成员名比对，坏引号会静默切断整条单聊链。
    return `<label class="f-chip"><input type="checkbox" value="${escapeHtmlAttr(r)}" data-fkind="role"><span style="color:${escapeHtmlAttr(c)}">${escapeHtml(r)}</span></label>`;
  }).join("");
}

function populateFilterTypes() {
  const box = $("#filter-types");
  if (!box || box.dataset.built === "1") return;
  box.innerHTML = ALL_TYPES.map((ty) =>
    `<label class="f-chip"><input type="checkbox" value="${escapeHtml(ty)}" data-fkind="type"><span class="tb-mini tb-${escapeHtml(ty)}">${escapeHtml(ty)}</span></label>`
  ).join("");
  box.dataset.built = "1";
}

function readFilters() {
  const roleChecks = $$('#filter-roles input:checked').map((c) => c.value);
  const typeChecks = $$('#filter-types input:checked').map((c) => c.value);
  filterState.roles = roleChecks.length ? new Set(roleChecks) : null;
  filterState.types = typeChecks.length ? new Set(typeChecks) : null;
  filterState.aOnly = $("#filter-aonly")?.checked || false;
  // 离开某成员的单聊态（取消 / 清除全部 / 移除该 chip / 手动取消勾选）→ 解锁发送目标：
  // 只在 value 恰好还锁在那个刚被取消的成员身上时回置为 ""（兜底首项 = to 为空 →
  // moderator 路由）。不这么做的话 sendMessage 与 showAttach 会继续拿着旧目标跑；
  // "恰好等于"这条限定保证不碰用户自己从下拉里挑的目标。
  const nowActive = activeMemberRole();
  if (lastActiveMember && lastActiveMember !== nowActive) {
    const sel = $("#send-to");
    if (sel && sel.value === lastActiveMember) sel.value = "";
  }
  lastActiveMember = nowActive;
  renderStream();
  updateFilterUI();
  renderMemberRoster();   // 单聊高亮是筛选态的派生量，筛选一变就跟着重算
}

// R5 D2：筛选弹出面板开关。
function toggleFilterPop(force) {
  const pop = $("#filter-pop");
  if (!pop) return;
  const show = force !== undefined ? force : pop.classList.contains("hidden");
  pop.classList.toggle("hidden", !show);
}

// R5 D2：按钮计数徽标 + 激活筛选 chips 行（仅有筛选时出现，逐个可移除）。
function updateFilterUI() {
  const n = (filterState.roles ? filterState.roles.size : 0)
    + (filterState.types ? filterState.types.size : 0)
    + (filterState.aOnly ? 1 : 0);
  const badge = $("#filter-count");
  if (badge) {
    badge.textContent = String(n);
    badge.classList.toggle("hidden", n === 0);
  }
  const row = $("#filter-chips");
  if (!row) return;
  const chips = [];
  for (const r of (filterState.roles || [])) chips.push({ kind: "role", val: r, label: r });
  for (const t of (filterState.types || [])) chips.push({ kind: "type", val: t, label: t });
  if (filterState.aOnly) chips.push({ kind: "aonly", val: "1", label: "只看 A 类" });
  if (!chips.length) { row.classList.add("hidden"); row.innerHTML = ""; return; }
  row.classList.remove("hidden");
  row.innerHTML = chips.map((c) =>
    `<span class="fc-chip">${escapeHtml(c.label)}` +
    `<button class="fc-x" data-fkind="${c.kind}" data-fval="${escapeHtml(c.val)}" title="移除该筛选">✕</button></span>`
  ).join("");
}

function removeFilter(kind, val) {
  if (kind === "aonly") {
    const cb = $("#filter-aonly");
    if (cb) cb.checked = false;
  } else {
    const box = kind === "role" ? $("#filter-roles") : $("#filter-types");
    const cb = box && box.querySelector(`input[value="${CSS.escape(val)}"]`);
    if (cb) cb.checked = false;
  }
  readFilters();
}

function clearAllFilters() {
  $$("#filter-roles input:checked").forEach((c) => { c.checked = false; });
  $$("#filter-types input:checked").forEach((c) => { c.checked = false; });
  const a = $("#filter-aonly");
  if (a) a.checked = false;
  readFilters();
}

// ————————————————————————————————————————————————
// 状态 / 派发（D18）
// ————————————————————————————————————————————————
// 状态载荷统一应用（loadStatus 与轮询共用）：徽章/矩阵/派发 chips/正在响应/门禁。
function applyStatusPayload(s) {
  lastStatus = s;
  const badge = $("#wk-status");
  badge.textContent = s.status;
  badge.className = `badge ${s.status}`;
  applyStatusMatrix(s.status);
  renderDispatchSummary(s);
  lastRoles = s.roles || [];  // M5 §12：角色投影单一数据源
  lastConfigError = s.config_error || "";   // 坏 config 信号与投影同源同拍，不另起轮询
  renderRoleBindings();       // 角色行生效绑定
  renderMemberRoster();       // 常驻名册：角色投影 + 本次响应的全量派发行
  updateAdapterAlerts();      // 阻塞点名依赖角色投影，状态一变就重评警示档位
  updateTypingBar(s);
  // D19：以 status 端点权威状态重评门禁 banner。
  refreshGateFromEvents(currentEvents, s.status);
}

async function loadStatus() {
  if (!selectedThread) return;
  const tid = selectedThread;
  try {
    const s = await api(`/api/threads/${tid}/status`);
    if (tid !== selectedThread) return;   // 线程已切换：丢弃过期响应
    applyStatusPayload(s);
    return s;
  } catch (e) {
    toast("状态加载失败: " + e.message, "err");
  }
}

// R5 D6："⋯ {role} 正在响应"胶囊——数据来自既有 status 端点的 dispatching 行，
// 不新增后端功能；多个并行则并列角色名。
//
// T4 升级为「正在处理中 mm:ss」：起点取 /status 派发行的**可选**键 started_ts
// （服务端由落盘的 deadline_ts − 该角色超时秒逆算，见 server.py:_dispatch_started_ts；
// 推不出来时该键缺席）。走字是**纯展示**：setInterval 每秒只重画文案，前端不持有
// 任何权威状态，也**不参与任何调度判定**（超时那条线一律用服务端落盘的绝对时间戳，
// §16.2）。缺 started_ts 的行维持既有"正在响应"文案，绝不编造起点。
let typingRows = [];        // [{role, started_ts|null}]，最近一次 status 的派生投影
let typingTimer = null;     // 走字定时器（无在跑派发时清掉，不留空转）

// 切线程 / 切工作区的清场（评审 建议12）：只清 typingRows 会留下每秒空转一次的
// 定时器（renderTypingPill 见空投影就只隐藏胶囊，然后一直白跑到下一拍 status）。
// 投影与定时器是一对，必须一起清——故收口成一个函数，不在两处各写两行。
function clearTypingClock() {
  typingRows = [];
  if (typingTimer) {
    clearInterval(typingTimer);
    typingTimer = null;
  }
  renderTypingPill();      // 立刻收掉旧胶囊，不等下一拍
}

function updateTypingBar(s) {
  // 数据源复活后本条自然点亮；但**必须**沿用 isLiveDispatch 判 deadline_ts——
  // 崩溃滞留的 dispatching 行否则会让胶囊永久常亮（同一个假绿根因）。
  const byRole = new Map();
  for (const d of (s.dispatches || []).filter(isLiveDispatch)) {
    const role = String(d.target);
    const st = Number(d.started_ts);
    const started = isFinite(st) ? st : null;
    if (!byRole.has(role)) { byRole.set(role, started); continue; }
    // 同角色多条在跑（同批多事件）：取最早起点，显示的是"这一轮处理了多久"。
    const cur = byRole.get(role);
    if (started !== null && (cur === null || started < cur)) byRole.set(role, started);
  }
  typingRows = Array.from(byRole, ([role, started_ts]) => ({ role, started_ts }));
  renderTypingPill();
  if (typingRows.length && !typingTimer) {
    typingTimer = setInterval(renderTypingPill, 1000);
  } else if (!typingRows.length && typingTimer) {
    clearInterval(typingTimer);
    typingTimer = null;
  }
}

function renderTypingPill() {
  const bar = $("#typing-bar");
  if (!bar) return;
  if (!typingRows.length) { bar.classList.add("hidden"); bar.innerHTML = ""; return; }
  const parts = typingRows.map((r) => {
    const name = escapeHtml(r.role);
    if (r.started_ts === null) return `${name} 正在响应`;
    // fmtDuration 已是 mm:ss（超一小时 h:mm:ss），与统计卡同一格式，不另造一套。
    const el = escapeHtml(fmtDuration(Date.now() / 1000 - r.started_ts));
    return `${name} 正在处理中 <span class="typing-clock mono">${el}</span>`;
  });
  bar.innerHTML =
    `<span class="typing-pill"><span class="typing-dots"><i></i><i></i><i></i></span>` +
    `${parts.join("、")}</span>`;
  bar.classList.remove("hidden");
}

// D18/R5 D5：线程头派发摘要 chips——仅渲染非零项；全零时一枚不渲染（恒零胶囊=噪音）。
function renderDispatchSummary(s) {
  const el = $("#dispatch-summary");
  if (!el) return;
  const disp = s.dispatches || [];
  const count = (st) => disp.filter((d) => d.status === st).length;
  // dispatching 计数与"正在响应"胶囊、名册绿点走**同一** isLiveDispatch 判据：崩溃
  // 滞留行不计入，否则线程头「dispatching N」长亮，与已熄灭的胶囊在同屏自相矛盾。
  // 盘上原始行不因此消失——点开的派发明细表仍逐行如实显示并标注滞留。
  const items = [
    ["pending", count("pending")],
    ["dispatching", disp.filter(isLiveDispatch).length],
    ["gate", s.status === "suspended" ? 1 : 0],
    ["failed", count("failed")],
  ].filter(([, n]) => n > 0);
  if (!items.length) { el.innerHTML = ""; return; }
  el.innerHTML =
    `<button id="btn-status" class="disp-chips" title="点击展开完整派发表">` +
    items.map(([k, n]) => `<span class="dc ${k}">${k} ${n}</span>`).join("") +
    `</button>`;
  // 重新绑定（innerHTML 覆盖了旧节点）。
  $("#btn-status").addEventListener("click", showStatusDetail);
}

// D4：线程状态→操作矩阵。禁用态视觉可辨（CSS button:disabled 有 opacity）+ title 原因。
// running   : 运行一轮/停机/状态/回放/接入/发言 可 ； 重开 禁
// suspended : 门禁批准拒绝/状态/回放/接入/发言(入队提示) 可 ； 运行一轮 禁
// terminated: 重开/回放/状态/接入 可 ； 运行一轮/停机/发言 禁
function setCtl(id, enabled, reason) {
  const el = $(id);
  if (!el) return;
  el.disabled = !enabled;
  if (!enabled && reason) {
    el.dataset.baseTitle = el.dataset.baseTitle || el.title || "";
    el.title = reason;
  } else if (el.dataset.baseTitle !== undefined) {
    el.title = el.dataset.baseTitle;
  }
}

// R5 D5：单一主按钮（状态→动作）；suspended 主按钮滚动至门禁 banner。
const PRIMARY_BY_STATUS = {
  running: { label: "▶ 运行一轮", act: () => runOnce() },
  suspended: {
    label: "⚠ 处理门禁",
    act: () => {
      const g = $("#gate-bar");
      if (!g || g.classList.contains("hidden")) { toast("无挂起门禁", "err"); return; }
      g.scrollIntoView({ behavior: "smooth", block: "center" });
      g.classList.remove("flash"); void g.offsetWidth; g.classList.add("flash");
      setTimeout(() => g.classList.remove("flash"), 1200);
    },
  },
  terminated: { label: "↻ 重开", act: () => reopenThread() },
};
let primaryAction = null;

function applyStatusMatrix(status) {
  const running = status === "running";
  const suspended = status === "suspended";
  const terminated = status === "terminated";

  // 主按钮：状态决定文案与动作（禁用规则沿用 R1 矩阵）。
  const p = PRIMARY_BY_STATUS[status] || PRIMARY_BY_STATUS.running;
  const btn = $("#btn-primary-action");
  if (btn) {
    btn.textContent = p.label;
    btn.disabled = !(running || suspended || terminated);
  }
  primaryAction = p.act;

  // ⋯ 溢出菜单项：回放/接入全状态可用；重开仅 terminated；停机 running/suspended。
  setCtl("#btn-replay", true, "");
  setCtl("#btn-attach", true, "");
  setCtl("#btn-reopen", terminated, running
    ? "线程运行中，无需重开"
    : (suspended ? "线程挂起中，无需重开" : "仅已终止线程可重开"));
  setCtl("#btn-stop", running || suspended,
    terminated ? "线程已终止，无需停机" : "");

  // composer：terminated 禁用（placeholder 给原因）；suspended 入队提示；running 正常。
  const canSend = running || suspended;
  setCtl("#btn-send", canSend, terminated ? "线程已终止，不可发言" : "");
  const sendBody = $("#send-body");
  if (sendBody) {
    sendBody.disabled = !canSend;
    sendBody.placeholder = terminated
      ? "线程已终止：发言已禁用（可重开后继续）"
      : "发言…（Enter 发送，Shift+Enter 换行）";
  }
  const hint = $("#send-hint");
  if (hint) {
    const msg = suspended ? "线程挂起中：发言将入队，待门禁恢复后派发" : "";
    hint.textContent = msg;
    hint.classList.toggle("hidden", !msg);
  }

  // 门禁批准/拒绝：仅 suspended 可用（gate banner 显隐由 refreshGate 管）。
  setCtl("#btn-approve", suspended, "无挂起门禁");
  setCtl("#btn-reject", suspended, "无挂起门禁");
}

// ————————————————————————————————————————————————
// D19 门禁流：suspended 时琥珀 banner，corr 自动从最近未决 gate_request 提取。
// ————————————————————————————————————————————————
function refreshGateFromEvents(evs, statusOverride) {
  evs = evs || [];
  // 找最近一条 gate_request，且其后没有同 corr 的 gate_decision（未决）。
  const decidedCorrs = new Set(
    evs.filter((e) => e.type === "gate_decision" && e.corr).map((e) => String(e.corr))
  );
  let pending = null;
  for (let i = evs.length - 1; i >= 0; i--) {
    const e = evs[i];
    if (e.type === "gate_request") {
      const c = e.corr ? String(e.corr) : "";
      if (!c || !decidedCorrs.has(c)) { pending = e; break; }
    }
  }
  // §10 corr 缺省生成："非正式门禁"（任意 to=human 信封挂起，无 gate_request）——
  // 兜底取最近发往 human 的信封，corr = 其自带 corr 或编排器生成形 gate-{事件号}，
  // 让批准/拒绝按钮对这类挂起同样可用（后端 apply_gate_decision 已支持反解）。
  if (!pending) {
    for (let i = evs.length - 1; i >= 0; i--) {
      const e = evs[i];
      if ((e.to || []).includes("human") && e.type !== "gate_decision") {
        const c = e.corr ? String(e.corr) : `gate-${e.id}`;
        if (!decidedCorrs.has(c)) pending = { id: e.id, corr: c, body: e.body };
        break;
      }
    }
  }
  // 权威状态优先用调用方传入（loadStatus 拿到的 status 端点值）；否则回退当前徽章。
  const status = (statusOverride || $("#wk-status").textContent || "").trim();
  updateGateBanner(pending, status);
}

function updateGateBanner(pendingReq, status) {
  const bar = $("#gate-bar");
  if (!bar) return;
  const show = status === "suspended" || !!pendingReq;
  bar.classList.toggle("hidden", !show);
  if (!show) return;
  const corr = pendingReq && pendingReq.corr ? String(pendingReq.corr) : "";
  const excerpt = pendingReq ? summarize(pendingReq.body, 60) : "（未找到 gate_request 正文）";
  const corrEl = $("#gate-corr");
  if (corrEl) corrEl.value = corr; // 隐藏字段承载 corr，供 gateDecision 复用
  const label = $("#gate-label");
  if (label) {
    label.innerHTML =
      `<span class="gate-corr-tag">${escapeHtml(corr || "gate")}</span> 待审批：` +
      `<span class="gate-excerpt">${escapeHtml(excerpt)}</span>`;
  }
}

// ————————————————————————————————————————————————
// R5 D6：实时跟新引擎——1.5s 轮询（≤2s），失败退避加倍（至 12s），
// 页面不可见暂停、可见立即拉取；防并发堆积（上次未返回不发下次）；
// terminated 停轮。手动刷新按钮已删除。
// ————————————————————————————————————————————————
const POLL_BASE = 1500;
const POLL_MAX = 12000;
let pollTimer = null;
let pollInterval = POLL_BASE;
let pollInFlight = false;
// 事件轮询是否仍在自我重排（终态/未选线程/页面不可见 → false）。可用性心跳据此
// 决定要不要自己去拉 status，避免两条线重复请求同一端点。
let pollActive = false;
let pollTick = 0;
let unseenCount = 0;

function updateLiveIndicator(state) {
  const el = $("#live-ind");
  const txt = $("#live-text");
  if (!el) return;
  if (state === "hidden") { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.classList.toggle("paused", state === "paused");
  el.classList.toggle("off", state === "off");
  if (txt) txt.textContent = state === "live" ? "实时" : (state === "paused" ? "重连中" : "静止");
}

function updateNewMsgsFloat() {
  const f = $("#new-msgs");
  if (!f) return;
  const n = $("#new-msgs-n");
  if (n) n.textContent = String(unseenCount);
  f.classList.toggle("hidden", unseenCount === 0);
}

function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollLoop, pollInterval);
}

async function pollLoop() {
  if (!selectedThread) { pollActive = false; updateLiveIndicator("hidden"); return; }
  if (document.hidden) { pollActive = false; return; }  // 不可见：暂停（visibilitychange 恢复）
  pollActive = true;
  if (pollInFlight) { schedulePoll(); return; }    // 防堆积：上次未返回不发下次
  const tid = selectedThread;
  pollInFlight = true;
  let ok = true;
  try {
    // N4：/api/adapters **不**在这条线拉——适配器是线程之外的全局事实，单一数据源
    // 是可用性心跳（availabilityHeartbeat）；这里只管本线程的事件与状态。
    const [evData, s, bd] = await Promise.all([
      api(`/api/threads/${tid}/events`),
      api(`/api/threads/${tid}/status`),
      fetchBoard(tid),
    ]);
    if (tid !== selectedThread) { pollInFlight = false; return; }  // 已切线程：丢弃
    lastRoundStats = evData.round_stats || null;
    const boardChanged = applyBoardPayload(bd);
    if (!ingestEvents(evData.events || [], false) && boardChanged) renderBoard(currentEvents);
    applyStatusPayload(s);
  } catch (e) {
    ok = false;
  } finally {
    pollInFlight = false;
  }
  pollInterval = ok ? POLL_BASE : Math.min(pollInterval * 2, POLL_MAX);  // 退避加倍/复位
  if (lastStatus && lastStatus.status === "terminated") {
    pollActive = false;                            // 交棒给可用性心跳（它仍按 D6 跑）
    updateLiveIndicator("off");                    // 终态流不再变化：停轮
    return;
  }
  updateLiveIndicator(ok ? "live" : "paused");
  pollTick += 1;
  if (pollTick % 5 === 0) loadThreads();           // Q5：线程列表低频捎带自刷
  schedulePoll();
}

function startPolling() {
  clearTimeout(pollTimer);
  pollInterval = POLL_BASE;
  pollTick = 0;
  pollActive = true;
  pollLoop();
}

// ————————————————————————————————————————————————
// M5 §12 · 可用性心跳（R4b 修复）——**独立于**线程事件轮询的一条线。
//
// 根因：pollLoop 对 terminated 线程按 D6 语义"停轮"且不再重排，于是
// applyStatusPayload → renderRoleBindings / updateAdapterAlerts 这条链在终态线程上
// 永久停摆；而适配器可用性是**线程之外的事实**（CLI/控制台随时改），停轮那一刻的
// 快照就冻住了 —— 页面因此看不到 disable 后的阻塞警示（§12"必须显著警示"落空）。
// 未选中线程时更是一点驱动都没有。
//
// 修法：把"可用性 + 角色投影"解耦成常驻心跳：
//   · 恒定 D6 节奏（POLL_BASE），页面不可见则空转重排（不发请求），可见即恢复；
//   · 线程是否选中 / 是否终态**都跑**（未选中时只刷 /api/adapters）；
//   · 事件轮询活跃时（pollActive）不重复拉 status —— 那条线已经在喂同一个渲染函数；
//   · **不碰 /events、不动 live 指示灯**：终态"事件流不再变化"的 D6 语义原样保留。
// ————————————————————————————————————————————————
let availTimer = null;
let availInFlight = false;
let availTick = 0;
// N5：事件轮询停摆时（终态线程），角色投影按**每 3 拍**（≈4.5s）取一次即可——它只随
// 人工 enable/disable 变化；/api/adapters 仍每拍拉（全局开关要即时）。
const ROLE_STATUS_EVERY = 3;

async function availabilityHeartbeat() {
  clearTimeout(availTimer);
  if (document.hidden || availInFlight) {          // 不可见/上轮未回：空转，绝不堆积
    availTimer = setTimeout(availabilityHeartbeat, POLL_BASE);
    return;
  }
  availInFlight = true;
  try {
    const adp = await api("/api/adapters").catch(() => null);
    if (adp) {
      adapterRows = adp.adapters || [];
      const box = $("#adapters-list");
      if ($("#view-adapters").classList.contains("active")
          && box && !box.contains(document.activeElement)) renderAdapters();
    }
    // 事件轮询停摆（终态 / 尚未启动）时，由心跳补角色投影：**只喂角色相关渲染**，
    // 不走 applyStatusPayload —— 那条路会连带跑状态矩阵/派发 chips/正在响应/门禁
    // banner，而这些的数据源（事件流）在终态线程上根本不再变化（N5 减负）。
    if (selectedThread && !pollActive
        && availTick % ROLE_STATUS_EVERY === 0) {
      const tid = selectedThread;
      const s = await api(`/api/threads/${tid}/status`).catch(() => null);
      if (s && tid === selectedThread) {
        lastRoles = s.roles || [];
        lastConfigError = s.config_error || "";   // 终态线程上改坏 config 也要亮警示
        renderRoleBindings();
        renderMemberRoster();   // 终态线程上名册的 ⛔ 档位也要跟着人工 enable/disable 走
      }
    }
    updateAdapterAlerts();
  } finally {
    availTick += 1;
    availInFlight = false;
  }
  availTimer = setTimeout(availabilityHeartbeat, POLL_BASE);
}

function startAvailabilityHeartbeat() {
  clearTimeout(availTimer);
  availabilityHeartbeat();
}

// ————————————————————————————————————————————————
// R5 D8：线程切换浮层（Ctrl/⌘+K；↑↓ 选择、Enter 切换、Esc 关闭）。
// 列表数据复用线程栏缓存（lastThreads + DOM 内摘要），不发额外请求。
// ————————————————————————————————————————————————
let swItems = [];
let swIndex = 0;

function threadSummaryOf(tid) {
  const li = document.querySelector(`#thread-list li[data-tid="${CSS.escape(tid)}"]`);
  const s = li && li.querySelector(".ti-summary");
  return s ? s.textContent : "";
}

function renderSwitcherList(query) {
  const list = $("#switcher-list");
  const qq = String(query || "").trim().toLowerCase();
  swItems = lastThreads.filter((t) => {
    if (!qq) return true;
    return t.id.toLowerCase().includes(qq)
      || threadSummaryOf(t.id).toLowerCase().includes(qq);
  });
  if (swIndex >= swItems.length) swIndex = Math.max(0, swItems.length - 1);
  if (!swItems.length) { list.innerHTML = '<li class="sw-empty">无匹配线程</li>'; return; }
  list.innerHTML = swItems.map((t, i) =>
    `<li class="${i === swIndex ? "active" : ""}" data-tid="${escapeHtml(t.id)}">` +
      `<span class="sw-sum">${escapeHtml(threadSummaryOf(t.id) || "（无正文）")}</span>` +
      `<span class="sw-id">${escapeHtml(t.id)}</span></li>`
  ).join("");
}

function openSwitcher() {
  swIndex = 0;
  const inp = $("#switcher-input");
  inp.value = "";
  renderSwitcherList("");
  openModal("#switcher");
  inp.focus();
}

// ————————————————————————————————————————————————
// 发言 / 控制条动作
// ————————————————————————————————————————————————
async function sendMessage() {
  if (!selectedThread) return;
  const body = $("#send-body").value.trim();
  if (!body) { toast("发言正文为空", "err"); return; }
  const to = $("#send-to").value;
  const type = $("#send-type").value;
  const btn = $("#btn-send");
  btn.disabled = true;
  try {
    const payload = { body, type };
    if (to) payload.to = to;
    const r = await api(`/api/threads/${selectedThread}/send`, { method: "POST", body: payload });
    toast("已发送 E" + r.event_id);
    const ta = $("#send-body");
    ta.value = "";
    autoGrowComposer();
    await loadEvents();
    ta.focus();          // D7：发送成功后清空并保持焦点
  } catch (e) {
    toast("发送失败: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

// R5 D7：composer 正文随内容自动增高（至 ~5 行，超出内部滚动）。
function autoGrowComposer() {
  const ta = $("#send-body");
  if (!ta) return;
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 132) + "px";
}

async function runOnce() {
  if (!selectedThread) return;
  const btn = $("#btn-primary-action");
  btn.disabled = true;
  try {
    const r = await api(`/api/threads/${selectedThread}/run`, { method: "POST", body: { once: true } });
    toast("运行一轮完成，status=" + r.status);
    await Promise.all([loadEvents(), loadStatus()]);
    await loadThreads();
  } catch (e) {
    toast("运行失败: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

async function reopenThread() {
  if (!selectedThread) return;
  try {
    const r = await api(`/api/threads/${selectedThread}/reopen`, { method: "POST", body: {} });
    toast("已重开，status=" + r.status);
    await Promise.all([loadEvents(), loadStatus(), loadThreads()]);
    startPolling();   // 重开后恢复实时跟新
  } catch (e) {
    toast("重开失败: " + e.message, "err");
  }
}

async function stopWorkspace() {
  try {
    await api("/api/stop", { method: "POST", body: {} });
    toast("已写 orch.stop 停机标志");
  } catch (e) {
    toast("停机失败: " + e.message, "err");
  }
}

async function showStatusDetail() {
  if (!selectedThread) return;
  try {
    const s = await api(`/api/threads/${selectedThread}/status`);
    lastStatus = s;
    // 明细表 = 盘上原始行，一行不删（与线程头 chips 的"只数在跑的"有意不同口径）；
    // 已过截止的 dispatching 行加「滞留」标注，让两处口径的差额当场可解释。
    let rows = (s.dispatches || []).map((d) => {
      const stale = d.status === "dispatching" && !isLiveDispatch(d);
      const st = escapeHtml(String(d.status)) +
        // 行内 style 而非新 CSS 类：本回环卡白名单不含 styles.css，用 :root 已有的
        // --gate 变量着色，零新增样式表改动。
        (stale ? ' <span class="disp-stale" style="color:var(--gate)"' +
                 ' title="已过截止时间：进程可能已崩溃，等看门狗对账">滞留</span>' : "");
      return `<tr><td>E${escapeHtml(String(d.event_id))}</td><td>${escapeHtml(String(d.target))}</td><td>${st}</td><td>${escapeHtml(String(d.attempts))}</td></tr>`;
    }).join("");
    // 投影已是全五态快照（含 done/gate_wait），空态文案随之改口径。
    if (!rows) rows = '<tr><td colspan="4">（无派发行）</td></tr>';
    const html =
      `<table class="tbl"><thead><tr><th>事件</th><th>目标</th><th>状态</th><th>重试</th></tr></thead>` +
      `<tbody>${rows}</tbody></table>`;
    showOverlay(`派发明细 · ${s.status}`, html);
  } catch (e) {
    toast("状态明细失败: " + e.message, "err");
  }
}

async function showReplay() {
  if (!selectedThread) return;
  try {
    const r = await api(`/api/threads/${selectedThread}/replay`);
    showOverlay("回放（markdown）", `<pre>${escapeHtml(r.markdown)}</pre>`);
  } catch (e) {
    toast("回放失败: " + e.message, "err");
  }
}

async function showAttach() {
  if (!selectedThread) return;
  // 取第一个角色的接入命令（也可扩展成逐角色）。
  const sel = $("#send-to");
  const role = sel.value || (sel.options[1] ? sel.options[1].value : "pm");
  try {
    const r = await api(`/api/threads/${selectedThread}/attach/${encodeURIComponent(role)}`);
    showOverlay(
      `接入命令 · 角色 ${role}（点击复制）`,
      `<pre class="copyable" title="点击复制">${escapeHtml(r.command)}</pre>`
    );
    const pre = $("#op-body .copyable");
    if (pre) pre.addEventListener("click", () => {
      navigator.clipboard?.writeText(r.command);
      toast("已复制接入命令");
    });
  } catch (e) {
    toast("接入命令失败: " + e.message, "err");
  }
}


// —— 门禁裁决 ——
async function gateDecision(decision) {
  if (!selectedThread) return;
  const corr = $("#gate-corr").value.trim();
  if (!corr) { toast("未提取到门禁 corr（无未决 gate_request）", "err"); return; }
  try {
    await api("/api/gate", { method: "POST", body: { thread: selectedThread, corr, decision } });
    toast(`门禁已${decision === "approve" ? "批准" : "驳回"}`);
    await Promise.all([loadEvents(), loadStatus(), loadThreads()]);
  } catch (e) {
    toast("门禁裁决失败: " + e.message, "err");
  }
}

// ————————————————————————————————————————————————
// 配置视图
// ————————————————————————————————————————————————
async function loadConfig() {
  try {
    const c = await api("/api/config");
    $("#config-text").value = c.yaml || "";
    if (!c.exists) toast("workspace 尚无 config.yaml（可新建后保存）");
  } catch (e) {
    toast("载入 config 失败: " + e.message, "err");
  }
}

async function saveConfig() {
  const yaml = $("#config-text").value;
  const btn = $("#btn-save-config");
  btn.disabled = true;
  try {
    await api("/api/config", { method: "PUT", body: { yaml } });
    toast("config.yaml 已保存（校验通过）");
  } catch (e) {
    toast("保存失败: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

// ————————————————————————————————————————————————
// 指标视图
// ————————————————————————————————————————————————
async function loadMetrics() {
  const grid = $("#metrics-grid");
  try {
    const m = await api("/api/metrics");
    grid.innerHTML = "";
    for (const row of (m.rows || [])) {
      const card = document.createElement("div");
      card.className = "metric-card";
      const isNa = String(row.value).startsWith("N/A");
      card.innerHTML =
        `<div class="m-label">${escapeHtml(String(row.label))}</div>` +
        `<div class="m-value ${isNa ? "na" : ""}">${escapeHtml(String(row.value))}</div>`;
      grid.appendChild(card);
    }
  } catch (e) {
    grid.innerHTML = `<div class="chat-empty">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

// ————————————————————————————————————————————————
// 基准视图（对应 orch bench resume；原"压测"入口改名"基准"，保留不删）
// ————————————————————————————————————————————————
async function runBench() {
  const fixture = $("#bench-fixture").value.trim() || "like";
  const runs = parseInt($("#bench-runs").value, 10) || 3;
  const btn = $("#btn-run-bench");
  const out = $("#bench-result");
  btn.disabled = true;
  out.innerHTML = '<div class="chat-empty">运行中…</div>';
  try {
    const r = await api("/api/bench", { method: "POST", body: { fixture, runs } });
    const rep = r.report;
    const fmt = (v) => (v === null || v === undefined) ? "N/A" : (typeof v === "number" ? v.toFixed(1) : v);
    out.innerHTML =
      `<div>fixture=<b>${escapeHtml(String(rep.fixture))}</b> runs=<b>${escapeHtml(String(rep.runs))}</b></div>` +
      `<table><thead><tr><th>路径</th><th>每轮 tokens_in</th><th>均值</th></tr></thead><tbody>` +
      `<tr><td>关 resume（冷启动）</td><td>[${rep.no_resume.join(", ")}]</td><td>${fmt(rep.no_resume_mean)}</td></tr>` +
      `<tr><td>开 resume（热续）</td><td>[${rep.with_resume.join(", ")}]</td><td>${fmt(rep.with_resume_mean)}</td></tr>` +
      `</tbody></table>` +
      `<div style="margin-top:8px">tokens 节省 %: <b>${fmt(rep.saved_pct)}</b></div>`;
  } catch (e) {
    out.innerHTML = `<div class="chat-empty">基准失败: ${escapeHtml(e.message)}</div>`;
    toast("基准失败: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

// ————————————————————————————————————————————————
// 工具
// ————————————————————————————————————————————————
function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

// ————————————————————————————————————————————————
// 绑定：每个控件 → 真实 fetch 动作
// ————————————————————————————————————————————————
function bind() {
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));

  // ② 多工作区切换。
  $("#ws-select").addEventListener("change", (e) => switchWorkspace(e.target.value));

  // R5 D4：新建线程 = 弹层流程（不再常驻左栏）。
  $("#btn-new-thread").addEventListener("click", () => { openModal("#new-thread-modal"); $("#new-task").focus(); });
  $("#btn-new-thread-empty").addEventListener("click", () => { openModal("#new-thread-modal"); $("#new-task").focus(); });
  $("#nt-close").addEventListener("click", closeModals);
  $("#btn-create-thread").addEventListener("click", createThread);
  $("#modal-backdrop").addEventListener("click", closeModals);
  $("#op-close").addEventListener("click", closeModals);

  // R5 D1/D3/D4：三区折叠开关。
  $("#rail-toggle").addEventListener("click", () =>
    setRailCollapsed(!$("#threads-layout").classList.contains("rail-collapsed")));
  $("#board-rail-btn").addEventListener("click", () => setBoardOpen(true));
  $("#board-toggle").addEventListener("click", () => setBoardOpen(false));

  // R5 D5：单一主按钮 + ⋯ 溢出菜单。
  $("#btn-primary-action").addEventListener("click", () => { if (primaryAction) primaryAction(); });
  $("#btn-more").addEventListener("click", (e) => {
    e.stopPropagation();
    $("#more-menu").classList.toggle("hidden");
  });
  $("#btn-replay").addEventListener("click", () => { $("#more-menu").classList.add("hidden"); showReplay(); });
  $("#btn-attach").addEventListener("click", () => { $("#more-menu").classList.add("hidden"); showAttach(); });
  $("#btn-reopen").addEventListener("click", () => { $("#more-menu").classList.add("hidden"); reopenThread(); });
  $("#btn-stop").addEventListener("click", () => { $("#more-menu").classList.add("hidden"); stopWorkspace(); });

  // 成员名册：点击某成员 = 专人单聊（过滤消息流 + 锁定 #send-to）；再点取消。
  const roster = $("#member-roster");
  if (roster) roster.addEventListener("click", (e) => {
    const chip = e.target.closest(".mr-chip");
    if (chip) toggleMemberChat(chip.dataset.member);
  });

  $("#btn-send").addEventListener("click", sendMessage);
  $("#btn-approve").addEventListener("click", () => gateDecision("approve"));
  $("#btn-reject").addEventListener("click", () => gateDecision("reject"));
  $("#btn-load-config").addEventListener("click", loadConfig);
  $("#btn-save-config").addEventListener("click", saveConfig);
  $("#btn-load-metrics").addEventListener("click", loadMetrics);
  $("#btn-run-bench").addEventListener("click", runBench);

  // M5 §12：适配器面板 —— 刷新 / 每行开关按钮（委托）/ 警示条直达。
  $("#btn-load-adapters").addEventListener("click", () => loadAdapters());
  $("#btn-goto-adapters").addEventListener("click", () => switchView("adapters"));
  $("#adapters-list").addEventListener("click", (e) => {
    const btn = e.target.closest(".adp-btn");
    if (!btn) return;
    setAdapter(btn.dataset.adp, btn.dataset.act);
  });
  $("#adapters-list").addEventListener("keydown", (e) => {
    // 停用原因输入框内回车 = 点"停用"（与 composer 的 Enter 语义一致）。
    if (e.key !== "Enter") return;
    const inp = e.target.closest(".adp-reason");
    if (!inp) return;
    e.preventDefault();
    setAdapter(inp.dataset.adpReason, "disable");
  });

  // R5 D2：筛选弹出面板 + #n 回车跳转（无跳转按钮）。
  populateFilterTypes();
  $("#btn-filter").addEventListener("click", (e) => { e.stopPropagation(); toggleFilterPop(); });
  $("#filter-pop").addEventListener("click", (e) => e.stopPropagation());
  $("#filter-clear").addEventListener("click", clearAllFilters);
  const froles = $("#filter-roles");
  const ftypes = $("#filter-types");
  if (froles) froles.addEventListener("change", readFilters);
  if (ftypes) ftypes.addEventListener("change", readFilters);
  const faonly = $("#filter-aonly");
  if (faonly) faonly.addEventListener("change", readFilters);
  const fgoto = $("#filter-goto");
  if (fgoto) fgoto.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const raw = (fgoto.value || "").replace(/[^\d]/g, "");
    if (!raw) { toast("请输入事件号 #n", "err"); return; }
    gotoEvent(raw);
  });

  // R5 D7：composer 键盘语义 + 自动增高。
  const sendBody = $("#send-body");
  sendBody.addEventListener("input", autoGrowComposer);
  sendBody.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  // 全局：点击空白关闭菜单/弹出；Esc 关闭一切浮层。
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".menu-wrap")) $("#more-menu").classList.add("hidden");
    if (!e.target.closest(".pop-wrap")) toggleFilterPop(false);
  });
  document.addEventListener("keydown", (e) => {
    // R5 D8：Ctrl/⌘+K 线程切换器（快捷键仅此一组 + Esc）。
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      openSwitcher();
      return;
    }
    if (e.key === "Escape") {
      closeModals();
      toggleFilterPop(false);
      $("#more-menu").classList.add("hidden");
    }
  });

  // R5 D8：切换器输入过滤 + ↑↓/Enter 键盘导航；点击项切换。
  const swInput = $("#switcher-input");
  swInput.addEventListener("input", () => { swIndex = 0; renderSwitcherList(swInput.value); });
  swInput.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (swIndex < swItems.length - 1) { swIndex++; renderSwitcherList(swInput.value); }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (swIndex > 0) { swIndex--; renderSwitcherList(swInput.value); }
    } else if (e.key === "Enter") {
      e.preventDefault();
      const t = swItems[swIndex];
      if (t) { closeModals(); selectThread(t.id); }
    }
  });
  $("#switcher-list").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-tid]");
    if (li) { closeModals(); selectThread(li.dataset.tid); }
  });

  // R5 D6：新消息浮标点击回底；用户自行滚回贴底时自动清零。
  $("#new-msgs").addEventListener("click", () => {
    const stream = $("#chat-stream");
    stream.scrollTop = stream.scrollHeight;
    unseenCount = 0;
    updateNewMsgsFloat();
  });
  $("#chat-stream").addEventListener("scroll", () => {
    if (unseenCount > 0 && isAtBottom($("#chat-stream"))) {
      unseenCount = 0;
      updateNewMsgsFloat();
    }
  }, { passive: true });

  // R5 D6：页面不可见暂停轮询；可见立即拉取一次（可用性心跳同步暂停/立即补一拍）。
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(pollTimer);
      pollActive = false;
      updateLiveIndicator("paused");
    } else {
      startAvailabilityHeartbeat();   // 回到前台先补一次可用性（不依赖选中线程）
      if (selectedThread) {
        pollInterval = POLL_BASE;
        pollLoop();
      }
    }
  });

  // 事件流内委托：re/corr/黑板 #evt 跳转（data-goto）+ 折叠展开 + system 分组展开
  // + 筛选 chip 移除（R5 D2）。
  document.addEventListener("click", (e) => {
    const fx = e.target.closest(".fc-x");
    if (fx) { removeFilter(fx.dataset.fkind, fx.dataset.fval); return; }
    const goto = e.target.closest("[data-goto]");
    if (goto) { gotoEvent(goto.dataset.goto); return; }
    // T4 View Steps：**唯一**的取数触发点——点开才 fetch，且同一条只 fetch 一次
    // （已在 stepsCache 里就直接回填，收起再展开不重打端点，轮询更不会打）。
    const stepsBtn = e.target.closest(".steps-toggle");
    if (stepsBtn) {
      const eid = Number(stepsBtn.dataset.stepsEid);
      if (stepsOpen.has(eid)) {
        stepsOpen.delete(eid);
      } else {
        stepsOpen.add(eid);
        if (!stepsCache.has(eid)) loadSteps(eid);
      }
      paintStepsBlock(eid);
      return;
    }
    const clamp = e.target.closest(".toggle-clamp");
    if (clamp) {
      const body = clamp.previousElementSibling;
      // b-body 可能被 artifacts chips 隔开：向上找同卡的 .b-body。
      const bubble = clamp.closest(".bubble");
      const bodyEl = bubble ? bubble.querySelector(".b-body") : body;
      const expanded = clamp.dataset.expanded === "1";
      if (bodyEl) bodyEl.classList.toggle("clamped", expanded);
      clamp.dataset.expanded = expanded ? "0" : "1";
      clamp.textContent = expanded
        ? clamp.textContent.replace("收起", "展开").replace("▾", "").trim() || "展开"
        : "收起";
      if (!expanded) clamp.textContent = "收起";
      else {
        const n = bubble ? ((bubble.querySelector(".b-body")?.textContent.match(/\n/g) || []).length + 1) : 0;
        clamp.textContent = "展开" + (n ? `（${n} 行）` : "");
      }
      return;
    }
    const sysHead = e.target.closest(".sys-group-head");
    if (sysHead) {
      const gbody = sysHead.nextElementSibling;
      const hidden = gbody.classList.toggle("hidden");
      sysHead.dataset.expanded = hidden ? "0" : "1";
      sysHead.textContent = (hidden ? "▸ " : "▾ ") + sysHead.textContent.replace(/^[▸▾]\s*/, "");
      return;
    }
  });
}

// —— 启动 ——
window.addEventListener("DOMContentLoaded", async () => {
  bind();
  initLayoutPrefs();
  await loadWorkspaces();   // ②：先定当前工作区，后续请求自动携带 ?ws=
  loadHealth();
  loadThreads();
  // M5：可用性心跳常驻（进站即知；且不随线程终态/未选中而停 —— R4b 根因修复）。
  startAvailabilityHeartbeat();
});
