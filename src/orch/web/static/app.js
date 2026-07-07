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
// 当前线程事件缓存（供过滤重渲染 / 跳转 / 黑板投影，均为库的只读投影）。
let currentEvents = [];
// R5：最近一次线程列表缓存（切换器/标题复用，仍是端点数据的只读投影）。
let lastThreads = [];
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
  lastStatus = null;
  unseenCount = 0;
  clearTimeout(pollTimer);
  updateLiveIndicator("hidden");
  updateNewMsgsFloat();
  $("#workspace").classList.add("hidden");
  $("#workspace-empty").classList.remove("hidden");
  loadHealth();
  loadThreads();
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
    sel.innerHTML = '<option value="">由 moderator 路由</option>';
    for (const r of roles) {
      if (r === "human") continue; // human 是自己，不作为发送目标项
      const o = document.createElement("option");
      o.value = r; o.textContent = r;
      sel.appendChild(o);
    }
    // D16：角色过滤下拉也随线程角色刷新。
    populateFilterRoles(roles);
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
      : "");
  return div;
}

// D16 过滤投影：把 currentEvents 按 filterState 过滤后返回（不改数据）。
function applyFilters(evs) {
  return evs.filter((ev) => {
    if (filterState.aOnly && !A_CLASS_TYPES.has(ev.type)) return false;
    if (filterState.roles && filterState.roles.size && !filterState.roles.has(ev.sender)) return false;
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
function ingestEvents(evs, force = false) {
  const stream = $("#chat-stream");
  const prevLen = currentEvents.length;
  const changed = force
    || evs.length !== prevLen
    || (evs.length > 0 && prevLen > 0
        && evs[evs.length - 1].id !== currentEvents[prevLen - 1].id);
  currentEvents = evs;
  if (!changed) return;
  const stick = force || isAtBottom(stream);
  updateThreadTitle();
  populateSendType(evs.length > 0);
  renderBoard(evs);
  renderStream(stick);
  refreshGateFromEvents(evs, lastStatus && lastStatus.status);
  if (!stick) unseenCount += Math.max(0, evs.length - prevLen);
  else unseenCount = 0;
  updateNewMsgsFloat();
}

async function loadEvents(force = true) {
  if (!selectedThread) return;
  const tid = selectedThread;
  const stream = $("#chat-stream");
  try {
    const data = await api(`/api/threads/${tid}/events`);
    if (tid !== selectedThread) return;   // 线程已切换：丢弃过期响应
    ingestEvents(data.events || [], force);
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
// D17 黑板栏：据 A 类事件 + bb_ops 投影三节（契约/决策/任务状态）。
// 数据源=前端投影（不加只读端点）：events 端点已返回 A 类事件与其 bb_ops
// （store 键名 blackboard_ops → 端点 bb_ops），三种 op 与黑板三节一一对应：
//   freeze_contract → 契约（name·vX·path，同名后写覆盖）
//   set_decision    → 决策（逐条 append，带 #evt 链接）
//   set_task        → 任务状态（key→status，同 key 后写覆盖）
// 投影顺序=事件 id 升序（与 store.rebuild_blackboard / _apply_ops_into 一致，§4.6）。
// ————————————————————————————————————————————————
function projectBoard(evs) {
  const contracts = {};     // name -> {version, path, evt}
  const decisions = [];     // {evt, text}
  const tasks = {};         // key -> {status, evt}
  for (const ev of evs) {   // evs 已按 id 升序（端点 ORDER BY id ASC）
    if (!A_CLASS_TYPES.has(ev.type)) continue;
    for (const op of (ev.bb_ops || [])) {
      if (op.op === "freeze_contract") {
        contracts[op.name] = { version: op.version, path: op.path, evt: ev.id };
      } else if (op.op === "set_decision") {
        decisions.push({ evt: ev.id, text: op.text });
      } else if (op.op === "set_task") {
        tasks[op.key] = { status: op.status, evt: ev.id };
      }
    }
  }
  return { contracts, decisions, tasks };
}

function renderBoard(evs) {
  const el = $("#board-content");
  if (!el) return;
  const { contracts, decisions, tasks } = projectBoard(evs);
  const names = Object.keys(contracts).sort();
  const taskKeys = Object.keys(tasks).sort();

  const contractRows = names.length
    ? names.map((n) => {
        const c = contracts[n];
        return `<li><span class="bd-name">${escapeHtml(n)}</span>` +
          `<span class="bd-ver">v${escapeHtml(String(c.version))}</span>` +
          `<span class="bd-path mono">${escapeHtml(String(c.path || ""))}</span>` +
          `<button class="ln-chip bd-goto" data-goto="${escapeHtml(String(c.evt))}">#${escapeHtml(String(c.evt))}</button></li>`;
      }).join("")
    : '<li class="bd-empty">决策冻结后会出现在这里</li>';

  const decisionRows = decisions.length
    ? decisions.map((d) =>
        `<li><span class="bd-text">${escapeHtml(String(d.text == null ? "" : d.text))}</span>` +
        `<button class="ln-chip bd-goto" data-goto="${escapeHtml(String(d.evt))}">#${escapeHtml(String(d.evt))}</button></li>`
      ).join("")
    : '<li class="bd-empty">decision 类事件自动沉淀到这里</li>';

  const taskRows = taskKeys.length
    ? taskKeys.map((k) => {
        const t = tasks[k];
        return `<tr><td class="mono">${escapeHtml(k)}</td>` +
          `<td><span class="task-status ts-${escapeHtml(String(t.status || "").replace(/[^a-z0-9_-]/gi, ""))}">${escapeHtml(String(t.status))}</span></td>` +
          `<td><button class="ln-chip bd-goto" data-goto="${escapeHtml(String(t.evt))}">#${escapeHtml(String(t.evt))}</button></td></tr>`;
      }).join("")
    : '<tr><td colspan="3" class="bd-empty">PM 建立任务后显示状态</td></tr>';

  el.innerHTML =
    `<section class="bd-sec"><h4>契约</h4><ul class="bd-list">${contractRows}</ul></section>` +
    `<section class="bd-sec"><h4>决策</h4><ul class="bd-list">${decisionRows}</ul></section>` +
    `<section class="bd-sec"><h4>任务状态</h4><table class="bd-tasks"><thead><tr><th>任务</th><th>状态</th><th></th></tr></thead><tbody>${taskRows}</tbody></table></section>`;

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
    return `<label class="f-chip"><input type="checkbox" value="${escapeHtml(r)}" data-fkind="role"><span style="color:${c}">${escapeHtml(r)}</span></label>`;
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
  renderStream();
  updateFilterUI();
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
function updateTypingBar(s) {
  const bar = $("#typing-bar");
  if (!bar) return;
  const roles = Array.from(new Set((s.dispatches || [])
    .filter((d) => d.status === "dispatching").map((d) => String(d.target))));
  if (!roles.length) { bar.classList.add("hidden"); bar.innerHTML = ""; return; }
  bar.innerHTML =
    `<span class="typing-pill"><span class="typing-dots"><i></i><i></i><i></i></span>` +
    `${roles.map(escapeHtml).join("、")} 正在响应</span>`;
  bar.classList.remove("hidden");
}

// D18/R5 D5：线程头派发摘要 chips——仅渲染非零项；全零时一枚不渲染（恒零胶囊=噪音）。
function renderDispatchSummary(s) {
  const el = $("#dispatch-summary");
  if (!el) return;
  const disp = s.dispatches || [];
  const count = (st) => disp.filter((d) => d.status === st).length;
  const items = [
    ["pending", count("pending")],
    ["dispatching", count("dispatching")],
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
  if (!selectedThread) { updateLiveIndicator("hidden"); return; }
  if (document.hidden) return;                     // 不可见：暂停（visibilitychange 恢复）
  if (pollInFlight) { schedulePoll(); return; }    // 防堆积：上次未返回不发下次
  const tid = selectedThread;
  pollInFlight = true;
  let ok = true;
  try {
    const [evData, s] = await Promise.all([
      api(`/api/threads/${tid}/events`),
      api(`/api/threads/${tid}/status`),
    ]);
    if (tid !== selectedThread) { pollInFlight = false; return; }  // 已切线程：丢弃
    ingestEvents(evData.events || [], false);
    applyStatusPayload(s);
  } catch (e) {
    ok = false;
  } finally {
    pollInFlight = false;
  }
  pollInterval = ok ? POLL_BASE : Math.min(pollInterval * 2, POLL_MAX);  // 退避加倍/复位
  if (lastStatus && lastStatus.status === "terminated") {
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
  pollLoop();
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
    let rows = (s.dispatches || []).map((d) =>
      `<tr><td>E${escapeHtml(String(d.event_id))}</td><td>${escapeHtml(String(d.target))}</td><td>${escapeHtml(String(d.status))}</td><td>${escapeHtml(String(d.attempts))}</td></tr>`
    ).join("");
    if (!rows) rows = '<tr><td colspan="4">（无 pending 派发）</td></tr>';
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

  $("#btn-send").addEventListener("click", sendMessage);
  $("#btn-approve").addEventListener("click", () => gateDecision("approve"));
  $("#btn-reject").addEventListener("click", () => gateDecision("reject"));
  $("#btn-load-config").addEventListener("click", loadConfig);
  $("#btn-save-config").addEventListener("click", saveConfig);
  $("#btn-load-metrics").addEventListener("click", loadMetrics);
  $("#btn-run-bench").addEventListener("click", runBench);

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

  // R5 D6：页面不可见暂停轮询；可见立即拉取一次。
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(pollTimer);
      updateLiveIndicator("paused");
    } else if (selectedThread) {
      pollInterval = POLL_BASE;
      pollLoop();
    }
  });

  // 事件流内委托：re/corr/黑板 #evt 跳转（data-goto）+ 折叠展开 + system 分组展开
  // + 筛选 chip 移除（R5 D2）。
  document.addEventListener("click", (e) => {
    const fx = e.target.closest(".fc-x");
    if (fx) { removeFilter(fx.dataset.fkind, fx.dataset.fval); return; }
    const goto = e.target.closest("[data-goto]");
    if (goto) { gotoEvent(goto.dataset.goto); return; }
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
});
