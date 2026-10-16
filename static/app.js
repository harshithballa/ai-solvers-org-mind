// OrgMind frontend: plain JS against the FastAPI JSON API.
const $ = (sel) => document.querySelector(sel);

const PRESETS = {
  dev: [
    "My staging deploy is failing with ACME_VAULT_TOKEN expired. How do I fix it?",
    "The CI deploy job says vault permission denied even though I refreshed my token locally.",
    "`acme deploy staging` fails with `Error: image not found in registry`. How do I fix it?",
  ],
  hr: [
    "My home office stipend claim was rejected with 'Missing cost center'. What do I do?",
    "What's the limit for home-office equipment reimbursement and which expense category should I use?",
    "How many PTO days can I carry over to next year?",
  ],
};

const state = {
  org: null,
  employees: {},   // id -> employee
  agents: {},      // id -> agent
  playbooks: {},   // id -> playbook
  sessionId: null,
  busy: false,
};

// ---------------- helpers ----------------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    const err = new Error(`${res.status}: ${detail}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Minimal, escape-first Markdown: code blocks, inline code, bold, italics, lists, paragraphs.
function md(src) {
  const blocks = [];
  let text = escapeHtml(src || "").replace(/```[a-z]*\n?([\s\S]*?)```/g, (_, code) => {
    blocks.push(`<pre><code>${code}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const ol = line.match(/^\s*\d+\.\s+(.*)/);
    const ul = line.match(/^\s*[-*]\s+(.*)/);
    if (ol || ul) {
      const kind = ol ? "ol" : "ul";
      if (list !== kind) { closeList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline((ol || ul)[1])}</li>`);
    } else if (!line.trim()) {
      closeList();
    } else if (/^\u0000\d+\u0000$/.test(line.trim())) {
      closeList(); out.push(line.trim());
    } else {
      closeList(); out.push(`<p>${inline(line.replace(/^#+\s*/, ""))}</p>`);
    }
  }
  closeList();
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), isError ? 6000 : 3000);
}

function timeAgo(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function citationChips(cited) {
  if (!cited || !cited.length) return "";
  return `<div class="chips">${cited.map((c) =>
    `<button class="chip cite" data-playbook="${escapeHtml(c.id)}"><span class="pb-icon">◆</span> ${escapeHtml(c.title)}${c.version > 1 ? ` · v${c.version}` : ""}</button>`
  ).join("")}</div>`;
}

function setBusy(btn, busy, label) {
  btn.disabled = busy;
  if (label) btn.textContent = label;
}

// ---------------- bootstrap ----------------

async function loadOrg() {
  const [org, health] = await Promise.all([api("/api/org"), api("/api/health")]);
  state.org = org.org;
  state.employees = Object.fromEntries(org.employees.map((e) => [e.id, e]));
  state.agents = Object.fromEntries(org.agents.map((a) => [a.id, a]));
  $("#org-name").textContent = org.org.name;

  const badge = $("#mode-badge");
  badge.textContent = `${health.llm === "vertex" ? health.chat_model : "FAKE LLM"} · ${health.store}`;
  badge.classList.toggle("warn", health.llm !== "vertex");

  const agentOpts = org.agents.map((a) => `<option value="${a.id}">${escapeHtml(a.name)}</option>`).join("");
  $("#compare-agent").innerHTML = agentOpts;
  $("#agent-select").innerHTML = agentOpts;
  const emps = [...org.employees].sort((a, b) => a.name.localeCompare(b.name));
  $("#employee-select").innerHTML = emps.map((e) =>
    `<option value="${e.id}">${escapeHtml(e.name)} — ${escapeHtml(e.role)}</option>`).join("");
  if (state.employees.e_dana) $("#employee-select").value = "e_dana";
  renderPresets();
}

// ---------------- compare ----------------

function renderPresets() {
  const kind = state.agents[$("#compare-agent").value]?.kind || "dev";
  $("#compare-presets").innerHTML = (PRESETS[kind] || []).map((q) =>
    `<button class="chip" data-q="${escapeHtml(q)}">${escapeHtml(q)}</button>`).join("");
}

async function runCompare() {
  const question = $("#compare-question").value.trim();
  if (!question || state.busy) return;
  state.busy = true;
  const btn = $("#compare-btn");
  setBusy(btn, true, "Asking…");
  $("#baseline-answer").innerHTML = `<p class="typing">Thinking…</p>`;
  $("#playbook-answer").innerHTML = `<p class="typing">Retrieving playbooks and thinking…</p>`;
  $("#playbook-citations").innerHTML = "";
  try {
    const res = await api("/api/compare", { method: "POST", body: { agent_id: $("#compare-agent").value, question } });
    $("#baseline-answer").innerHTML = md(res.baseline.answer);
    $("#playbook-answer").innerHTML = md(res.with_playbooks.answer);
    const wp = res.with_playbooks;
    const retrieved = wp.retrieved.length
      ? `<div class="muted">Retrieved: ${wp.retrieved.map((r) => `${escapeHtml(r.title)} (${r.score.toFixed(2)})`).join(", ")}</div>`
      : `<div class="muted">No playbook matched this question, so the agent answered on its own.</div>`;
    $("#playbook-citations").innerHTML =
      (wp.cited.length ? `<strong>Cited:</strong>${citationChips(wp.cited)}` : `<strong>Cited:</strong> none`) + retrieved;
  } catch (e) {
    $("#baseline-answer").innerHTML = $("#playbook-answer").innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
  } finally {
    state.busy = false;
    setBusy(btn, false, "Ask both");
  }
}

// ---------------- playbooks + distill ----------------

async function loadPlaybooks(flashIds = []) {
  const list = await api("/api/playbooks");
  state.playbooks = Object.fromEntries(list.map((p) => [p.id, p]));
  $("#playbook-count").textContent = list.length;
  $("#playbook-list").innerHTML = list.length ? list.map((p) => `
    <li data-playbook="${p.id}" class="${flashIds.includes(p.id) ? "flash" : ""}">
      <div class="pb-title">${escapeHtml(p.title)}</div>
      <div class="pb-meta"><span class="tag ${p.agent_kind}">${p.agent_kind}</span>
        v${p.version} · ${p.steps.length} steps · from ${p.source_employee_ids.length} employees</div>
    </li>`).join("")
    : `<li class="muted small" style="cursor:default;border-style:dashed">No playbooks yet. Click <b>Distill sessions</b>.</li>`;
}

function empName(id) { return state.employees[id]?.name || id; }

async function runDistill() {
  const btn = $("#distill-btn");
  setBusy(btn, true, "Distilling…");
  $("#distill-report").innerHTML = `<div class="report typing">Summarizing sessions, clustering across employees, compiling playbooks…</div>`;
  try {
    const r = await api("/api/distill", { method: "POST", body: {} });
    const items = r.clusters.map((c) => {
      const who = c.employee_ids.map(empName).join(", ");
      const label = c.title ? escapeHtml(c.title) : escapeHtml(c.reason);
      return `<li><span class="outcome ${c.outcome}">${c.outcome}</span> <span class="tag ${c.agent_kind}">${c.agent_kind}</span>
        ${label}<br><span class="muted">${c.session_ids.length} sessions · ${c.resolved_session_ids.length} resolved · ${escapeHtml(who)}</span></li>`;
    }).join("");
    $("#distill-report").innerHTML = `<div class="report">
      <div class="stats"><span>${r.sessions_scanned} sessions</span><span>${r.clusters.length} recurring clusters</span>
        <span class="outcome created">+${r.playbooks_created} new</span><span class="outcome updated">${r.playbooks_updated} updated</span></div>
      <div class="muted">${r.sessions_summarized} summarized this run · ${r.timings_s?.total ?? "?"}s</div>
      ${items ? `<ul>${items}</ul>` : ""}</div>`;
    const changed = r.clusters.filter((c) => ["created", "updated"].includes(c.outcome)).map((c) => c.playbook_id);
    await loadPlaybooks(changed);
  } catch (e) {
    $("#distill-report").innerHTML = `<div class="report error">${escapeHtml(e.message)}</div>`;
  } finally {
    setBusy(btn, false, "Distill sessions");
  }
}

async function openPlaybook(id) {
  $("#modal-body").innerHTML = `<p class="typing">Loading…</p>`;
  $("#modal").classList.remove("hidden");
  try {
    const { playbook: p, sources } = await api(`/api/playbooks/${encodeURIComponent(id)}`);
    $("#modal-body").innerHTML = `
      <span class="tag ${p.agent_kind}">${p.agent_kind}</span><span class="tag">v${p.version}</span>
      <h2>${escapeHtml(p.title)}</h2>
      <p><strong>When to use:</strong> ${escapeHtml(p.trigger)}</p>
      ${p.symptoms.length ? `<p><strong>Symptoms:</strong></p><ul>${p.symptoms.map((s) => `<li>${md(s).replace(/^<p>|<\/p>$/g, "")}</li>`).join("")}</ul>` : ""}
      <p><strong>Steps:</strong></p>
      <ol>${p.steps.map((s) => `<li>${md(s).replace(/^<p>|<\/p>$/g, "")}</li>`).join("")}</ol>
      <h3>Distilled from</h3>
      ${sources.map((s) => `<div class="source ${s.resolved ? "" : "unresolved"}">
        ${s.resolved ? `<span class="ok">✓ resolved</span>` : `<span class="warn">unresolved · not used for steps</span>`} <strong>${escapeHtml(s.employee)}</strong> · ${escapeHtml(s.title)}
        <span class="muted">· ${timeAgo(s.created_at)}</span></div>`).join("")}`;
  } catch (e) {
    $("#modal-body").innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
  }
}

// ---------------- chat ----------------

async function loadSessions() {
  const emp = $("#employee-select").value, agent = $("#agent-select").value;
  const list = await api(`/api/sessions?employee_id=${encodeURIComponent(emp)}&agent_id=${encodeURIComponent(agent)}`);
  $("#session-list").innerHTML = list.length ? list.map((s) => `
    <li data-session="${s.id}" class="${s.id === state.sessionId ? "active" : ""}">
      ${escapeHtml(s.title)}<span class="when">${timeAgo(s.updated_at)}</span></li>`).join("")
    : `<li class="muted small" style="cursor:default">No conversations yet.</li>`;
}

function renderMessage(m, cited) {
  const div = document.createElement("div");
  div.className = `msg ${m.role}`;
  const chips = cited ?? (m.cited_playbook_ids || []).map((id) => ({ id, title: state.playbooks[id]?.title || "Playbook", version: state.playbooks[id]?.version }));
  div.innerHTML = `<div class="bubble">${md(m.content)}</div>${m.role === "assistant" ? citationChips(chips) : ""}`;
  $("#messages").appendChild(div);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return div;
}

function enableComposer(on) {
  $("#message-input").disabled = !on;
  $("#send-btn").disabled = !on;
}

async function openSession(id) {
  state.sessionId = id;
  const { session, messages } = await api(`/api/sessions/${encodeURIComponent(id)}`);
  const agent = state.agents[session.agent_id];
  $("#chat-header").textContent = `${agent?.name || "Agent"} · ${empName(session.employee_id)} · ${session.title}`;
  $("#messages").innerHTML = "";
  messages.forEach((m) => renderMessage(m));
  enableComposer(true);
  $("#message-input").focus();
  await loadSessions();
}

async function newSession() {
  const s = await api("/api/sessions", { method: "POST",
    body: { employee_id: $("#employee-select").value, agent_id: $("#agent-select").value } });
  await openSession(s.id);
}

async function sendMessage(e) {
  e.preventDefault();
  const input = $("#message-input");
  const content = input.value.trim();
  if (!content || !state.sessionId || state.busy) return;
  state.busy = true;
  enableComposer(false);
  input.value = "";
  renderMessage({ role: "user", content });
  const typing = renderMessage({ role: "assistant", content: "_Thinking…_" });
  typing.querySelector(".bubble").classList.add("typing");
  try {
    const res = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/messages`, {
      method: "POST", body: { content, use_playbooks: $("#use-playbooks").checked } });
    typing.remove();
    renderMessage(res.message, res.cited);
    await loadSessions();
    const title = document.querySelector(`#session-list li[data-session="${state.sessionId}"]`)?.firstChild?.textContent.trim();
    if (title) $("#chat-header").textContent = $("#chat-header").textContent.replace(/[^·]*$/, ` ${title}`);
  } catch (err) {
    typing.remove();
    toast(err.message, true);
    input.value = content;
  } finally {
    state.busy = false;
    enableComposer(true);
    input.focus();
  }
}

function resetChat() {
  state.sessionId = null;
  $("#messages").innerHTML = "";
  $("#chat-header").textContent = "Select or start a conversation";
  enableComposer(false);
}

// ---------------- reset (admin) ----------------

function getToken() { try { return localStorage.getItem("orgmind_admin_token") || ""; } catch { return ""; } }
function saveToken(t) { try { localStorage.setItem("orgmind_admin_token", t); } catch {} }

async function confirmReset() {
  const token = $("#token-input").value;
  const btn = $("#token-confirm");
  setBusy(btn, true, "Resetting…");
  $("#token-error").textContent = "";
  try {
    await api("/api/demo/reset", { method: "POST", headers: { "X-Admin-Token": token } });
    saveToken(token);
    $("#token-modal").classList.add("hidden");
    $("#distill-report").innerHTML = "";
    resetChat();
    await loadOrg();
    await Promise.all([loadPlaybooks(), loadSessions()]);
    toast("Demo data reset");
  } catch (e) {
    $("#token-error").textContent = e.status === 401 ? "Invalid admin token." : e.message;
  } finally {
    setBusy(btn, false, "Reset");
  }
}

// ---------------- wiring ----------------

function switchView(view) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $("#view-compare").classList.toggle("hidden", view !== "compare");
  $("#view-chat").classList.toggle("hidden", view !== "chat");
  if (view === "chat") loadSessions().catch((e) => toast(e.message, true));
}

document.addEventListener("click", (e) => {
  const pb = e.target.closest("[data-playbook]");
  if (pb) return openPlaybook(pb.dataset.playbook);
  const preset = e.target.closest("[data-q]");
  if (preset) { $("#compare-question").value = preset.dataset.q; return runCompare(); }
  const ses = e.target.closest("[data-session]");
  if (ses) return openSession(ses.dataset.session).catch((err) => toast(err.message, true));
  const tab = e.target.closest(".tab");
  if (tab) return switchView(tab.dataset.view);
});

$("#compare-btn").addEventListener("click", runCompare);
$("#compare-question").addEventListener("keydown", (e) => { if (e.key === "Enter") runCompare(); });
$("#compare-agent").addEventListener("change", renderPresets);
$("#distill-btn").addEventListener("click", runDistill);
$("#composer").addEventListener("submit", sendMessage);
$("#new-session-btn").addEventListener("click", () => newSession().catch((e) => toast(e.message, true)));
$("#employee-select").addEventListener("change", () => { resetChat(); loadSessions(); });
$("#agent-select").addEventListener("change", () => { resetChat(); loadSessions(); });
$("#modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); });
$("#reset-btn").addEventListener("click", () => {
  $("#token-input").value = getToken();
  $("#token-error").textContent = "";
  $("#token-modal").classList.remove("hidden");
  $("#token-input").focus();
});
$("#token-cancel").addEventListener("click", () => $("#token-modal").classList.add("hidden"));
$("#token-confirm").addEventListener("click", confirmReset);
$("#token-input").addEventListener("keydown", (e) => { if (e.key === "Enter") confirmReset(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("#modal").classList.add("hidden"); $("#token-modal").classList.add("hidden"); }
});

(async () => {
  try {
    await loadOrg();
    await loadPlaybooks();
  } catch (e) {
    toast(`Failed to load: ${e.message}`, true);
  }
})();
