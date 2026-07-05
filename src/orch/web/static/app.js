/* orch 玻璃感控制台前端逻辑。
 *
 * 铁律：每个可点击控件都发起真实 fetch("/api/...") 并渲染真实响应/错误；
 * 无 alert("TODO")、无写死假数据、空状态显示真实空。
 */

"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let selectedThread = null;

// ————————————————————————————————————————————————
// 通用 fetch 封装：统一 JSON + 错误 toast（用后端真实 error 文案）。
// ————————————————————————————————————————————————
async function api(path, { method = "GET", body = null } = {}) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
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
async function loadThreads() {
  const list = $("#thread-list");
  try {
    const threads = await api("/api/threads");
    if (!threads.length) {
      list.innerHTML = '<li class="empty">暂无线程</li>';
      return;
    }
    list.innerHTML = "";
    for (const t of threads) {
      const li = document.createElement("li");
      if (t.id === selectedThread) li.classList.add("selected");
      const statusClass = t.status === "running" ? "running"
        : (t.status === "suspended" ? "gate" : "terminated");
      li.innerHTML =
        `<span class="dot ${statusClass}"></span>` +
        `<span class="t-id">${t.id}</span>` +
        `<span class="t-roles">${(t.roles || []).length} 角色</span>`;
      li.addEventListener("click", () => selectThread(t.id));
      list.appendChild(li);
    }
  } catch (e) {
    list.innerHTML = `<li class="empty">加载失败: ${e.message}</li>`;
    toast("刷新线程失败: " + e.message, "err");
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
  $("#workspace-empty").classList.add("hidden");
  $("#workspace").classList.remove("hidden");
  $("#wk-tid").textContent = tid;
  $$("#thread-list li").forEach((li) => {
    li.classList.toggle("selected", li.querySelector(".t-id")?.textContent === tid);
  });
  await Promise.all([loadEvents(), loadStatus(), populateSendTo()]);
}

// D3：目标下拉——首项语义 = to=[]（走 moderator 兜底路由，spec §5.2）；
// 首项文案不得出现广播式措辞（广播是 spec §16 反模式第3条，禁止）。
async function populateSendTo() {
  const sel = $("#send-to");
  try {
    const threads = await api("/api/threads");
    const t = threads.find((x) => x.id === selectedThread);
    const roles = (t && t.roles) ? t.roles : [];
    sel.innerHTML = '<option value="">不指定（由 moderator 路由）</option>';
    for (const r of roles) {
      if (r === "human") continue; // human 是自己，不作为发送目标项
      const o = document.createElement("option");
      o.value = r; o.textContent = r;
      sel.appendChild(o);
    }
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

// D9 卡片头：一行式 = 头像圆点 · 名字 · → · @目标chips · type · (弹性) · #n。
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
  return (
    `<div class="b-head">` +
      `<span class="avatar" style="background:${col}1f;color:${col};border-color:${col}66">${initial}</span>` +
      `<span class="speaker" style="color:${col}">${escapeHtml(sender)}</span>` +
      arrow +
      `<span class="to-chips">${chips}</span>` +
      `<span class="ev-type">${escapeHtml(ev.type || "")}</span>` +
      `<span class="head-spacer"></span>` +
      `<span class="ev-id">#${escapeHtml(String(ev.id))}</span>` +
    `</div>`
  );
}

async function loadEvents() {
  if (!selectedThread) return;
  const stream = $("#chat-stream");
  try {
    const data = await api(`/api/threads/${selectedThread}/events`);
    const evs = data.events || [];
    // D6：据线程是否已有事件决定 type 下拉默认（新线程→assign，进行中→question）。
    populateSendType(evs.length > 0);
    if (!evs.length) {
      stream.innerHTML = '<div class="chat-empty">暂无事件</div>';
      return;
    }
    stream.innerHTML = "";
    let sawGate = false;
    for (const ev of evs) {
      if (ev.type === "gate_request") sawGate = ev.corr || true;
      const div = document.createElement("div");
      div.className = `bubble r-${ev.sender || "system"}`;
      // D1：正文渲染 body 原文（escapeHtml + CSS white-space:pre-wrap），
      // 永远不渲染 third_person 视图行；再叠一道自指前缀防御剥离。
      const clean = stripSelfViewPrefix(ev.body || "", {
        id: ev.id, sender: ev.sender, type: ev.type, to: ev.to || [],
      });
      div.innerHTML =
        buildEventHead(ev) +
        `<div class="b-body">${escapeHtml(clean)}</div>`;
      stream.appendChild(div);
    }
    stream.scrollTop = stream.scrollHeight;
    // 门禁条：状态 suspended 或有 gate_request 时出现，回填 corr。
    updateGateBar(sawGate);
  } catch (e) {
    stream.innerHTML = `<div class="chat-empty">加载失败: ${e.message}</div>`;
  }
}

function updateGateBar(gateCorr) {
  const bar = $("#gate-bar");
  const statusBadge = $("#wk-status").textContent;
  const show = gateCorr || statusBadge === "suspended";
  bar.classList.toggle("hidden", !show);
  if (show && typeof gateCorr === "string") {
    $("#gate-corr").value = gateCorr;
  }
}

async function loadStatus() {
  if (!selectedThread) return;
  try {
    const s = await api(`/api/threads/${selectedThread}/status`);
    const badge = $("#wk-status");
    badge.textContent = s.status;
    badge.className = `badge ${s.status}`;
    applyStatusMatrix(s.status);
    updateGateBar(s.status === "suspended" ? $("#gate-corr").value || true : false);
    return s;
  } catch (e) {
    toast("状态加载失败: " + e.message, "err");
  }
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

function applyStatusMatrix(status) {
  const running = status === "running";
  const suspended = status === "suspended";
  const terminated = status === "terminated";

  setCtl("#btn-run", running, suspended
    ? "门禁挂起中，待批准/拒绝恢复后方可运行"
    : (terminated ? "线程已终止，请先重开" : "当前状态不可运行一轮"));
  setCtl("#btn-reopen", terminated, running
    ? "线程运行中，无需重开"
    : (suspended ? "线程挂起中，无需重开" : "仅已终止线程可重开"));
  setCtl("#btn-stop", running || suspended,
    terminated ? "线程已终止，无需停机" : "");
  // 状态/回放/接入：全状态可用。
  setCtl("#btn-status", true, "");
  setCtl("#btn-replay", true, "");
  setCtl("#btn-attach", true, "");

  // 发言：terminated 禁用；suspended 允许但入队提示；running 正常。
  const canSend = running || suspended;
  setCtl("#btn-send", canSend, terminated ? "线程已终止，不可发言" : "");
  const sendBody = $("#send-body");
  if (sendBody) sendBody.disabled = !canSend;
  const hint = $("#send-hint");
  if (hint) {
    if (suspended) hint.textContent = "线程挂起中：发言将入队，待门禁恢复后派发";
    else if (terminated) hint.textContent = "线程已终止：发言已禁用（可重开后继续）";
    else hint.textContent = "";
  }

  // 门禁批准/拒绝：仅 suspended 可用（gate-bar 显隐由 updateGateBar 管，这里同步禁用态）。
  setCtl("#btn-approve", suspended, "无挂起门禁");
  setCtl("#btn-reject", suspended, "无挂起门禁");
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
    $("#send-body").value = "";
    await loadEvents();
  } catch (e) {
    toast("发送失败: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

async function runOnce() {
  if (!selectedThread) return;
  const btn = $("#btn-run");
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
    await Promise.all([loadStatus(), loadThreads()]);
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
    let rows = (s.dispatches || []).map((d) =>
      `<tr><td>E${d.event_id}</td><td>${d.target}</td><td>${d.status}</td><td>${d.attempts}</td></tr>`
    ).join("");
    if (!rows) rows = '<tr><td colspan="4">（无 pending 派发）</td></tr>';
    const html =
      `<div class="b-head">status = ${s.status}</div>` +
      `<table class="tbl"><thead><tr><th>事件</th><th>目标</th><th>状态</th><th>重试</th></tr></thead>` +
      `<tbody>${rows}</tbody></table>`;
    showModalInStream(html);
  } catch (e) {
    toast("状态明细失败: " + e.message, "err");
  }
}

async function showReplay() {
  if (!selectedThread) return;
  try {
    const r = await api(`/api/threads/${selectedThread}/replay`);
    showModalInStream(`<div class="b-head">回放 (markdown)</div><pre class="b-body mono">${escapeHtml(r.markdown)}</pre>`);
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
    showModalInStream(
      `<div class="b-head">接入命令 · 角色 ${role}（点击复制）</div>` +
      `<pre class="b-body mono copyable" title="点击复制">${escapeHtml(r.command)}</pre>`
    );
    const pre = $("#chat-stream .copyable");
    if (pre) pre.addEventListener("click", () => {
      navigator.clipboard?.writeText(r.command);
      toast("已复制接入命令");
    });
  } catch (e) {
    toast("接入命令失败: " + e.message, "err");
  }
}

function showModalInStream(html) {
  const stream = $("#chat-stream");
  const div = document.createElement("div");
  div.className = "bubble r-system";
  div.innerHTML = html;
  stream.appendChild(div);
  stream.scrollTop = stream.scrollHeight;
}

// —— 门禁裁决 ——
async function gateDecision(decision) {
  if (!selectedThread) return;
  const corr = $("#gate-corr").value.trim();
  if (!corr) { toast("请填写门禁 corr", "err"); return; }
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
        `<div class="m-label">${row.label}</div>` +
        `<div class="m-value ${isNa ? "na" : ""}">${escapeHtml(String(row.value))}</div>`;
      grid.appendChild(card);
    }
  } catch (e) {
    grid.innerHTML = `<div class="chat-empty">加载失败: ${e.message}</div>`;
  }
}

// ————————————————————————————————————————————————
// 压测视图
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
      `<div>fixture=<b>${rep.fixture}</b> runs=<b>${rep.runs}</b></div>` +
      `<table><thead><tr><th>路径</th><th>每轮 tokens_in</th><th>均值</th></tr></thead><tbody>` +
      `<tr><td>关 resume（冷启动）</td><td>[${rep.no_resume.join(", ")}]</td><td>${fmt(rep.no_resume_mean)}</td></tr>` +
      `<tr><td>开 resume（热续）</td><td>[${rep.with_resume.join(", ")}]</td><td>${fmt(rep.with_resume_mean)}</td></tr>` +
      `</tbody></table>` +
      `<div style="margin-top:8px">tokens 节省 %: <b>${fmt(rep.saved_pct)}</b></div>`;
  } catch (e) {
    out.innerHTML = `<div class="chat-empty">压测失败: ${e.message}</div>`;
    toast("压测失败: " + e.message, "err");
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
  $("#btn-refresh-threads").addEventListener("click", loadThreads);
  $("#btn-create-thread").addEventListener("click", createThread);
  $("#btn-refresh-events").addEventListener("click", loadEvents);
  $("#btn-send").addEventListener("click", sendMessage);
  $("#btn-run").addEventListener("click", runOnce);
  $("#btn-reopen").addEventListener("click", reopenThread);
  $("#btn-stop").addEventListener("click", stopWorkspace);
  $("#btn-status").addEventListener("click", showStatusDetail);
  $("#btn-replay").addEventListener("click", showReplay);
  $("#btn-attach").addEventListener("click", showAttach);
  $("#btn-approve").addEventListener("click", () => gateDecision("approve"));
  $("#btn-reject").addEventListener("click", () => gateDecision("reject"));
  $("#btn-load-config").addEventListener("click", loadConfig);
  $("#btn-save-config").addEventListener("click", saveConfig);
  $("#btn-load-metrics").addEventListener("click", loadMetrics);
  $("#btn-run-bench").addEventListener("click", runBench);
}

// —— 启动 ——
window.addEventListener("DOMContentLoaded", () => {
  bind();
  loadHealth();
  loadThreads();
});
