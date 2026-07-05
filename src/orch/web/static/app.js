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
async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("#ws-path").textContent = h.workspace;
    $("#ws-path").title = h.workspace;
    $("#conn-dot").className = "dot running";
  } catch (e) {
    $("#ws-path").textContent = "连接失败";
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

async function populateSendTo() {
  const sel = $("#send-to");
  try {
    const threads = await api("/api/threads");
    const t = threads.find((x) => x.id === selectedThread);
    const roles = (t && t.roles) ? t.roles : [];
    sel.innerHTML = '<option value="">（全部/默认 moderator）</option>';
    for (const r of roles) {
      const o = document.createElement("option");
      o.value = r; o.textContent = r;
      sel.appendChild(o);
    }
  } catch (e) {
    /* 保底：下拉至少有默认项 */
  }
}

async function loadEvents() {
  if (!selectedThread) return;
  const stream = $("#chat-stream");
  try {
    const data = await api(`/api/threads/${selectedThread}/events`);
    const evs = data.events || [];
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
      const toStr = (ev.to || []).map((r) => "@" + r).join(", ") || "@(默认)";
      div.innerHTML =
        `<div class="b-head">#${ev.id} · ${ev.sender} → ${toStr} · (${ev.type})</div>` +
        `<div class="b-body">${escapeHtml(ev.third_person || ev.body || "")}</div>`;
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
    updateGateBar(s.status === "suspended" ? $("#gate-corr").value || true : false);
    return s;
  } catch (e) {
    toast("状态加载失败: " + e.message, "err");
  }
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
