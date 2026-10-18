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
  health: {},
  employeeId: "e_dana",
  agentId: null,
  sessions: [],
  docs: [],
  skills: [],
  docSnippets: {},     // doc id -> last excerpt the agent used (this browser session)
  docMode: "file",
  docPoll: null,
  editingSkill: null,
  loadToken: 0,        // bumps on employee switch so stale responses are dropped
  sessionMessages: 0,
};

// ---------------- helpers ----------------

// The admin token (entered once in the Reset demo prompt) is sent with every request, so the owner's own
// browser is exempt from the demo cost caps, e.g. while recording.
function adminHeaders() { const t = getToken(); return t ? { "X-Admin-Token": t } : {}; }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...adminHeaders(), ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText, limit = null;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : j.detail?.message || detail;
      limit = j.limit || null;
    } catch {}
    const err = new Error(`${res.status}: ${detail}`);
    err.status = res.status;
    err.detail = detail;
    err.limit = limit;
    throw err;
  }
  return res.json();
}

// Limits and size caps are expected outcomes: show the server's friendly sentence, not "429: ...".
const isFriendly = (e) => [409, 413, 429].includes(e?.status);
const friendly = (e) => (isFriendly(e) ? e.detail : e.message);

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

  state.health = health;
  const badge = $("#mode-badge");
  badge.textContent = `${health.llm === "vertex" ? health.chat_model : "FAKE LLM"} · ${health.store}`;
  badge.classList.toggle("warn", health.llm !== "vertex");

  const agentOpts = org.agents.map((a) => `<option value="${a.id}">${escapeHtml(a.name)}</option>`).join("");
  $("#compare-agent").innerHTML = agentOpts;
  const emps = [...org.employees].sort((a, b) => a.name.localeCompare(b.name));
  $("#employee-select").innerHTML = emps.map((e) =>
    `<option value="${e.id}">${escapeHtml(e.name)}</option>`).join("");
  if (!state.employees[state.employeeId]) state.employeeId = emps[0]?.id;
  $("#employee-select").value = state.employeeId;
  if (!state.agents[state.agentId]) state.agentId = (org.agents.find((a) => a.kind === "dev") || org.agents[0])?.id;
  renderPresets();
  renderAgents();
  renderRagToggle();
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
    $("#baseline-answer").innerHTML = $("#playbook-answer").innerHTML =
      `<p class="${isFriendly(e) ? "limit-note" : "error"}">${escapeHtml(friendly(e))}</p>`;
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

function timings(t) {
  if (!t) return "";
  return `summarize ${t.summarize}s · compile ${t.compile}s · total ${t.total}s`;
}

async function runDistill() {
  const btn = $("#distill-btn");
  setBusy(btn, true, "Distilling… 0s");
  // The server doesn't stream progress, so show the pipeline order plus a real elapsed timer.
  $("#distill-report").innerHTML = `<div class="report running">
    <div class="phase-line">Summarizing sessions → clustering → writing playbooks</div>
    <div class="muted">Running · <span id="distill-elapsed">0</span>s elapsed (live Gemini; usually 10–55s)</div></div>`;
  const t0 = Date.now();
  const timer = setInterval(() => {
    const secs = Math.floor((Date.now() - t0) / 1000);
    btn.textContent = `Distilling… ${secs}s`;
    const el = $("#distill-elapsed");
    if (el) el.textContent = secs;
  }, 1000);
  try {
    const r = await api("/api/distill", { method: "POST", body: {} });
    const items = r.clusters.map((c) => {
      const who = c.employee_ids.map(empName).join(", ");
      const label = c.title ? escapeHtml(c.title) : escapeHtml(c.reason);
      return `<li><span class="outcome ${c.outcome}">${c.outcome}</span> <span class="tag ${c.agent_kind}">${c.agent_kind}</span>
        ${label}<br><span class="muted">${c.session_ids.length} sessions · ${c.resolved_session_ids.length} resolved · ${c.employee_ids.length} employee${c.employee_ids.length === 1 ? "" : "s"}: ${escapeHtml(who)}</span></li>`;
    }).join("");
    $("#distill-report").innerHTML = `<div class="report">
      <div class="stats"><span>${r.sessions_scanned} sessions</span><span>${r.clusters.length} recurring clusters</span>
        <span class="outcome created">+${r.playbooks_created} new</span><span class="outcome updated">${r.playbooks_updated} updated</span></div>
      <div class="muted">${r.sessions_summarized} summarized this run · ${timings(r.timings_s)} · ${Math.round((Date.now() - t0) / 1000)}s end to end</div>
      ${items ? `<ul>${items}</ul>` : ""}</div>`;
    const changed = r.clusters.filter((c) => ["created", "updated"].includes(c.outcome)).map((c) => c.playbook_id);
    await loadPlaybooks(changed);
  } catch (e) {
    $("#distill-report").innerHTML = `<div class="report ${isFriendly(e) ? "limit-note" : "error"}">${escapeHtml(friendly(e))}</div>`;
    if (e.status === 429) { clearInterval(timer); setBusy(btn, false, "Limit reached · try later"); return; }
  } finally {
    clearInterval(timer);
    if (btn.textContent.startsWith("Distilling")) setBusy(btn, false, "Distill sessions");
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

// ---------------- user workspace ----------------

const fmtDate = (iso) => new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
const fmtElapsed = (secs) => `${Math.floor(secs / 60)}:${String(Math.floor(secs % 60)).padStart(2, "0")}`;
const initials = (name) => name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
const ragOn = () => state.health.rag && state.health.rag !== "off";
const empId = () => state.employeeId;

function citationChipsTyped({ playbooks = [], skills = [], docs = [] }) {
  const chips = [
    ...playbooks.map((c) => `<button class="chip cite pb" data-playbook="${escapeHtml(c.id)}"><span class="ci">◆</span> Playbook · ${escapeHtml(c.title)}${c.version > 1 ? ` · v${c.version}` : ""}</button>`),
    ...skills.map((c) => `<button class="chip cite sk" data-skill="${escapeHtml(c.id)}"><span class="ci">⚙</span> Skill · ${escapeHtml(c.name)}</button>`),
    ...docs.map((c) => `<button class="chip cite doc" data-doc="${escapeHtml(c.id)}"><span class="ci">▤</span> Doc · ${escapeHtml(c.title)}</button>`),
  ];
  return chips.length ? `<div class="chips">${chips.join("")}</div>` : "";
}

function renderProfile() {
  const e = state.employees[empId()];
  if (!e) return;
  $("#profile-avatar").textContent = initials(e.name);
  $("#profile-avatar").style.background = e.avatar_color;
  $("#profile-role").textContent = `${e.role} · ${e.team}`;
  $("#stat-docs").textContent = state.docs.filter((d) => d.owner_employee_id === e.id).length;
  $("#stat-skills").textContent = state.skills.filter((s) => s.owner_employee_id === e.id).length;
  $("#stat-agents").textContent = Object.keys(state.agents).length;
}

function renderAgents() {
  // Seed ties agents to a team but not employees to agents: every employee can use every agent.
  $("#agent-cards").innerHTML = Object.values(state.agents).map((a) => {
    const caps = ["Playbooks", "Skills", ...(ragOn() ? ["Documents"] : [])];
    const sel = a.id === state.agentId;
    return `<button class="agent-card ${sel ? "selected" : ""}" role="radio" aria-checked="${sel}" data-agent="${a.id}">
      <span class="agent-icon ${a.kind}" aria-hidden="true">${a.kind === "dev" ? "&lt;/&gt;" : "♥"}</span>
      <span class="agent-text"><b>${escapeHtml(a.name)}</b><span class="muted">${escapeHtml(a.description)}</span>
        <span class="cap-chips">${caps.map((c) => `<span class="cap">${c}</span>`).join("")}</span></span>
      <span class="radio" aria-hidden="true"></span></button>`;
  }).join("");
}

async function loadSessions() {
  const token = state.loadToken;
  const list = await api(`/api/sessions?employee_id=${encodeURIComponent(empId())}&agent_id=${encodeURIComponent(state.agentId)}`);
  if (token !== state.loadToken) return;
  state.sessions = list;
  $("#session-list").innerHTML = list.length ? list.map((s) => `
    <li data-session="${s.id}" class="${s.id === state.sessionId ? "active" : ""}">
      <span class="s-title">${escapeHtml(s.title)}</span>
      <span class="when">${fmtDate(s.created_at)} · ${s.status === "closed" ? "Closed" : "Open"}</span></li>`).join("")
    : `<li class="muted small empty">No chats yet with this agent.</li>`;
  $("#session-select").innerHTML = `<option value="">${list.length ? "Select a chat…" : "No chats yet"}</option>` +
    list.map((s) => `<option value="${s.id}" ${s.id === state.sessionId ? "selected" : ""}>${escapeHtml(s.title.slice(0, 60))} · ${fmtDate(s.created_at)}</option>`).join("");
}

function renderMessage(m, cites) {
  const div = document.createElement("div");
  div.className = `msg ${m.role}`;
  let chips = "";
  if (m.role === "assistant") {
    const c = cites || {
      playbooks: (m.cited_playbook_ids || []).map((id) => ({ id, title: state.playbooks[id]?.title || "Playbook", version: state.playbooks[id]?.version })),
      skills: (m.cited_skill_ids || []).map((id) => ({ id, name: state.skills.find((s) => s.id === id)?.name || "Skill" })),
      docs: (m.cited_doc_ids || []).map((id) => ({ id, title: state.docs.find((d) => d.id === id)?.title || "Document" })),
    };
    chips = citationChipsTyped(c);
  }
  div.innerHTML = `<div class="bubble">${md(m.content)}</div>${chips}`;
  $("#messages").appendChild(div);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return div;
}

function enableComposer(on) {
  $("#message-input").disabled = !on;
  $("#send-btn").disabled = !on;
}

function renderChatHeader(session) {
  const agent = state.agents[state.agentId];
  $("#chat-title").innerHTML = session
    ? `<b>${escapeHtml(agent?.name || "Agent")}</b> <span class="muted">· ${escapeHtml(session.title)}</span>`
    : `<b>${escapeHtml(agent?.name || "Agent")}</b> <span class="muted">· select a chat or start a new one</span>`;
  $("#learn-btn").hidden = !session;
}

async function openSession(id) {
  state.sessionId = id;
  $("#learn-result").hidden = true;
  const { session, messages } = await api(`/api/sessions/${encodeURIComponent(id)}`);
  state.sessionMessages = messages.length;
  renderChatHeader(session);
  $("#messages").innerHTML = messages.length ? "" : `<p class="muted empty-chat">Ask ${escapeHtml(state.agents[state.agentId]?.name || "the agent")} anything.</p>`;
  messages.forEach((m) => renderMessage(m));
  enableComposer(true);
  $("#message-input").focus();
  await loadSessions();
}

async function newSession() {
  try {
    const s = await api("/api/sessions", { method: "POST", body: { employee_id: empId(), agent_id: state.agentId } });
    await openSession(s.id);
  } catch (e) {
    if (!isFriendly(e)) throw e;
    resetChat();
    showNotice(e.detail);
  }
}

function showNotice(text) {
  const div = document.createElement("div");
  div.className = "msg notice";
  div.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
  $("#messages").appendChild(div);
  $("#messages").scrollTop = $("#messages").scrollHeight;
}

async function sendMessage(e) {
  e.preventDefault();
  const input = $("#message-input");
  const content = input.value.trim();
  if (!content || !state.sessionId || state.busy) return;
  state.busy = true;
  enableComposer(false);
  input.value = "";
  $("#messages .empty-chat")?.remove();
  renderMessage({ role: "user", content });
  const typing = renderMessage({ role: "assistant", content: "_Thinking…_" });
  typing.querySelector(".bubble").classList.add("typing");
  try {
    const res = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/messages`, {
      method: "POST", body: { content, use_playbooks: $("#use-playbooks").checked,
                              use_skills: $("#use-skills").checked, use_rag: $("#use-rag").checked } });
    typing.remove();
    (res.rag?.retrieved || []).forEach((h) => { state.docSnippets[h.doc_id] = h.snippet; });
    renderMessage(res.message, { playbooks: res.cited, skills: res.cited_skills || [], docs: res.cited_docs || [] });
    if (res.rag?.error) toast(`Documents unavailable, answered without them (${res.rag.error})`, true);
    if (res.skills?.error) toast(`Skills unavailable, answered without them (${res.skills.error})`, true);
    state.sessionMessages += 2;
    await loadSessions();
    const s = state.sessions.find((x) => x.id === state.sessionId);
    if (s) renderChatHeader(s);
  } catch (err) {
    typing.remove();
    if (isFriendly(err)) showNotice(err.detail); else toast(err.message, true);
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
  $("#learn-result").hidden = true;
  renderChatHeader(null);
  enableComposer(false);
}

async function learnSkills() {
  const btn = $("#learn-btn");
  const box = $("#learn-result");
  setBusy(btn, true, "✦ Learning…");
  box.hidden = true;
  try {
    const r = await api("/api/skills/extract", { method: "POST", body: { session_id: state.sessionId, employee_id: empId() } });
    const n = r.skills.length;
    box.className = `learn-result ${n ? "ok" : ""}`;
    box.textContent = n ? `Learned ${n} skill${n > 1 ? "s" : ""} as draft: review ${n > 1 ? "them" : "it"} under My knowledge › Skills, then approve.`
                        : "No reusable skill found in this chat.";
    await loadSkills();
    if (n) openKnowledge();
  } catch (e) {
    box.className = "learn-result warn";
    box.textContent = e.status === 400 ? "This chat is too short to learn from yet (it needs at least 4 messages)." : friendly(e);
  } finally {
    box.hidden = false;
    setBusy(btn, false, "✦ Learn skills from this chat");
  }
}

// ---------- My knowledge: documents ----------

async function loadDocs() {
  if (!ragOn()) {
    state.docs = [];
    $("#doc-add-btn").hidden = true;
    $("#doc-count").textContent = "0";
    $("#doc-list").innerHTML = `<li class="muted small empty">Document upload is turned off on this server.</li>`;
    renderProfile();
    return;
  }
  const token = state.loadToken;
  const list = await api(`/api/rag/docs?employee_id=${encodeURIComponent(empId())}`);
  if (token !== state.loadToken) return;
  state.docs = list;
  const mine = list.filter((d) => d.owner_employee_id === empId());
  $("#doc-count").textContent = mine.length;
  const chip = (d) => {
    if (d.status === "ready") return `<span class="st ready">Ready</span>`;
    if (d.status === "failed") return `<span class="st failed" title="${escapeHtml(d.error || "")}">Failed</span>`;
    return `<span class="st indexing">Indexing · <span class="elapsed" data-since="${d.created_at}">0:00</span></span>`;
  };
  const docActions = (d) => {
    if (d.scope === "org") return `<button class="btn ghost-light small" data-share-act="unshare" data-kind="doc" data-id="${d.id}">Unshare</button>`;
    if (d.status !== "ready") return "";
    if (d.mime_type === "application/pdf") return `<span class="muted small" title="PDFs can't be scanned yet">PDF: can't be shared yet</span>`;
    return `<button class="btn ghost-light small" data-share-act="share" data-kind="doc" data-id="${d.id}">Share</button>`;
  };
  $("#doc-list").innerHTML = mine.length ? mine.map((d) => `
    <li data-doc="${d.id}" class="doc-item">
      <span class="doc-icon ${d.mime_type === "application/pdf" ? "pdf" : ""}" aria-hidden="true">▤</span>
      <span class="doc-main"><span class="doc-title">${escapeHtml(d.title)}</span>
        <span class="muted small">${(d.size / 1024).toFixed(1)} KB · ${fmtDate(d.created_at)}</span>
        <span class="row doc-actions">${scopeChip(d)}${docActions(d)}</span></span>
      ${chip(d)}</li>`).join("")
    : `<li class="muted small empty">No documents yet.</li>`;
  renderShared();
  tickElapsed();
  renderProfile();
  clearTimeout(state.docPoll);
  if (mine.some((d) => d.status === "indexing" || d.status === "uploading")) {
    state.docPoll = setTimeout(() => loadDocs().catch(() => {}), 10000);
  }
}

function tickElapsed() {
  document.querySelectorAll(".elapsed[data-since]").forEach((el) => {
    el.textContent = fmtElapsed(Math.max(0, (Date.now() - new Date(el.dataset.since).getTime()) / 1000));
  });
}

function setDocMode(mode) {
  document.querySelectorAll("[data-docmode]").forEach((b) => b.classList.toggle("active", b.dataset.docmode === mode));
  document.querySelectorAll("[data-docpane]").forEach((p) => { p.hidden = p.dataset.docpane !== mode; });
  state.docMode = mode;
}

async function submitDoc(e) {
  e.preventDefault();
  const err = $("#doc-error");
  err.textContent = "";
  const btn = $("#doc-submit");
  let req;
  if (state.docMode === "file") {
    const f = $("#doc-file").files[0];
    if (!f) { err.textContent = "Choose a file."; return; }
    if (!/\.(txt|md|pdf)$/i.test(f.name)) { err.textContent = "Use a .txt, .md or .pdf file."; return; }
    if (f.size > 5 * 1024 * 1024) { err.textContent = "The file is larger than 5 MB."; return; }
    const fd = new FormData();
    fd.append("employee_id", empId());
    fd.append("file", f);
    req = { method: "POST", body: fd };
  } else {
    const title = $("#doc-title").value.trim(), text = $("#doc-text").value;
    if (!title || !text.trim()) { err.textContent = "Add a title and some text."; return; }
    if (new Blob([text]).size > 200 * 1024) { err.textContent = "Pasted text is larger than 200 KB."; return; }
    req = { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ employee_id: empId(), title, text }) };
  }
  setBusy(btn, true, "Uploading…");
  try {
    req.headers = { ...(req.headers || {}), ...adminHeaders() };
    const res = await fetch("/api/rag/docs", req);
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = typeof j.detail === "string" ? j.detail : j.detail?.message || detail; } catch {}
      throw new Error(detail);
    }
    $("#doc-form").reset();
    $("#doc-form").hidden = true;
    toast("Uploaded. Indexing usually takes about a minute and a half.");
    await loadDocs();
  } catch (e2) {
    err.textContent = e2.message;
  } finally {
    setBusy(btn, false, "Upload");
  }
}

// ---------- My knowledge: skills ----------

async function loadSkills() {
  const token = state.loadToken;
  const list = await api(`/api/skills?employee_id=${encodeURIComponent(empId())}`);
  if (token !== state.loadToken) return;
  state.skills = list;
  const mine = list.filter((s) => s.owner_employee_id === empId());
  $("#skill-count").textContent = mine.length;
  $("#skill-list").innerHTML = mine.length ? mine.map(skillCard).join("")
    : `<li class="muted small empty">No skills yet. Learn skills from a chat.</li>`;
  renderShared();
  renderProfile();
}

function scopeChip(item) {
  return item.scope === "org" ? `<span class="scope org">Shared with org</span>` : `<span class="scope">Private</span>`;
}

// Read-only list of what colleagues shared with the org (docs + active skills).
function renderShared() {
  const docs = state.docs.filter((d) => d.scope === "org" && d.owner_employee_id !== empId());
  const sks = state.skills.filter((s) => s.scope === "org" && s.owner_employee_id !== empId());
  $("#shared-count").textContent = docs.length + sks.length;
  const by = (x) => `<span class="muted small">Shared by ${escapeHtml(x.shared_by_name || state.employees[x.owner_employee_id]?.name || "a colleague")}</span>`;
  $("#shared-list").innerHTML = (docs.length || sks.length) ? [
    ...sks.map((s) => `<li class="shared-item" data-skill-view="${s.id}"><span class="si-kind sk">Skill</span>
      <span class="doc-main"><span class="doc-title">${escapeHtml(s.name)}</span>${by(s)}</span><span class="scope org">Read-only</span></li>`),
    ...docs.map((d) => `<li class="shared-item" data-doc="${d.id}"><span class="si-kind doc">Doc</span>
      <span class="doc-main"><span class="doc-title">${escapeHtml(d.title)}</span>${by(d)}</span>
      ${d.status === "ready" ? `<span class="scope org">Read-only</span>` : `<span class="st indexing">Indexing</span>`}</li>`),
  ].join("") : `<li class="muted small empty">Nothing shared by colleagues yet.</li>`;
}

async function shareAction(btn) {
  const act = btn.dataset.shareAct, kind = btn.dataset.kind, id = btn.dataset.id;
  const base = kind === "doc" ? `/api/rag/docs/${id}` : `/api/skills/${id}`;
  if (act === "unshare") {
    try {
      btn.disabled = true;
      await api(`${base}/unshare`, { method: "POST", body: { employee_id: empId() } });
      toast("Unshared: it's private again");
    } catch (e) { toast(friendly(e), true); }
    return kind === "doc" ? loadDocs() : loadSkills();
  }
  const label = btn.textContent;
  setBusy(btn, true, act === "redacted" ? "Redacting…" : "Scanning…");
  try {
    const res = await api(act === "redacted" ? `${base}/share-redacted` : `${base}/share`, { method: "POST", body: { employee_id: empId() } });
    showShareResult(kind, id, res, act === "redacted");
  } catch (e) {
    showShareResult(kind, id, { verdict: "refused", message: friendly(e), findings: [] }, act === "redacted");
  } finally {
    if (document.body.contains(btn)) setBusy(btn, false, label);
    await (kind === "doc" ? loadDocs() : loadSkills());
  }
}

function showShareResult(kind, id, res, redacted) {
  const findings = res.findings || [];
  const list = findings.length ? `<ul class="findings">${findings.map((f) => `<li><span class="cat">${escapeHtml(f.category.replace(/_/g, " "))}</span>
      <code>${escapeHtml(f.preview)}</code> <span class="muted small">${escapeHtml(f.reason || "")}</span></li>`).join("")}</ul>` : "";
  const note = `<p class="muted small">Checked automatically for personal data and secrets (patterns + a Gemini check), which can miss things. Human review is planned (V3), not built yet.</p>`;
  let html;
  if (res.verdict === "clean" && redacted) {
    html = `<h2 class="share-ok">Shared a redacted copy</h2><p>“${escapeHtml(res.doc?.title || "")}” replaces every finding with [REDACTED]. It is indexing now; colleagues will see it once it is Ready. The original stays private.</p>${note}`;
  } else if (res.verdict === "clean") {
    html = `<h2 class="share-ok">Shared with your org</h2><p>Colleagues can now find and cite it, read-only, marked “Shared by ${escapeHtml(state.employees[empId()]?.name || "you")}”. You can unshare it at any time.</p>${note}`;
  } else if (res.verdict === "blocked") {
    html = `<h2 class="share-blocked">Blocked: not shared</h2><p>${redacted ? "The redacted copy still contains:" : "It stays private because the scan found:"}</p>${list}
      ${kind === "doc" && !redacted ? `<div class="row"><button class="btn primary small" data-share-act="redacted" data-kind="doc" data-id="${id}">Share a redacted copy</button></div>` : ""}
      ${kind === "skill" ? `<p class="small">Edit the skill to remove these values (use a parameter such as {project_id}), then share again.</p>` : ""}${note}`;
  } else {
    html = `<h2 class="share-blocked">${res.verdict === "error" ? "Couldn't verify" : "Not shared"}</h2><p>${escapeHtml(res.message || "Please try again.")}</p>${list}${note}`;
  }
  $("#modal-body").innerHTML = `<div class="share-result" data-verdict="${escapeHtml(res.verdict)}">${html}</div>`;
  $("#modal").classList.remove("hidden");
}

function skillCard(s) {
  if (state.editingSkill === s.id) {
    return `<li class="skill-card editing" data-skill-card="${s.id}">
      <label class="field">Name<input type="text" class="ed-name" value="${escapeHtml(s.name)}"></label>
      <label class="field">Description<input type="text" class="ed-desc" value="${escapeHtml(s.description)}"></label>
      <label class="field">Steps (one per line)<textarea class="ed-steps" rows="5">${escapeHtml(s.steps.join("\n"))}</textarea></label>
      <p class="error small ed-error"></p>
      <div class="row end"><button class="btn ghost-light small" data-skill-act="cancel" data-id="${s.id}">Cancel</button>
        <button class="btn primary small" data-skill-act="save" data-id="${s.id}">Save</button></div></li>`;
  }
  const trig = s.trigger.length > 140
    ? `<span class="trig-short">${escapeHtml(s.trigger.slice(0, 140))}… <button class="btn link small" data-skill-act="more">show more</button></span><span class="trig-full" hidden>${escapeHtml(s.trigger)} <button class="btn link small" data-skill-act="less">show less</button></span>`
    : escapeHtml(s.trigger);
  const n = s.evidence.length;
  return `<li class="skill-card" data-skill-card="${s.id}">
    <div class="row between"><b class="sk-name">${escapeHtml(s.name)}</b><span class="row">${scopeChip(s)}<span class="st ${s.status}">${s.status[0].toUpperCase() + s.status.slice(1)}</span></span></div>
    <p class="sk-desc">${escapeHtml(s.description)}</p>
    <p class="small"><span class="muted">When to use:</span> ${trig}</p>
    <ol class="sk-steps">${s.steps.map((x) => `<li>${md(x).replace(/^<p>|<\/p>$/g, "")}</li>`).join("")}</ol>
    ${s.parameters.length ? `<p class="small"><span class="muted">Asks you for:</span> ${s.parameters.map((p) => `<code>${escapeHtml(p.name)}</code>`).join(" ")}</p>` : ""}
    <p class="muted small">Based on ${n} message${n === 1 ? "" : "s"} · v${s.version}</p>
    <div class="row sk-actions">
      ${s.status !== "archived" ? `<button class="btn ghost-light small" data-skill-act="edit" data-id="${s.id}">Edit</button>` : ""}
      ${s.status === "draft" ? `<button class="btn primary small" data-skill-act="approve" data-id="${s.id}">Approve</button>` : ""}
      ${s.status !== "archived" ? `<button class="btn ghost-light small" data-skill-act="archive" data-id="${s.id}">Archive</button>` : ""}
      ${s.status === "active" && s.scope !== "org" ? `<button class="btn ghost-light small" data-share-act="share" data-kind="skill" data-id="${s.id}">Share</button>` : ""}
      ${s.scope === "org" ? `<button class="btn ghost-light small" data-share-act="unshare" data-kind="skill" data-id="${s.id}">Unshare</button>` : ""}
    </div></li>`;
}

async function skillAction(btn) {
  const act = btn.dataset.skillAct, id = btn.dataset.id;
  const card = btn.closest(".skill-card");
  if (act === "more" || act === "less") {
    card.querySelector(".trig-short").hidden = act === "more";
    card.querySelector(".trig-full").hidden = act === "less";
    return;
  }
  if (act === "edit") { state.editingSkill = id; return loadSkills(); }
  if (act === "cancel") { state.editingSkill = null; return loadSkills(); }
  try {
    btn.disabled = true;
    if (act === "save") {
      const steps = card.querySelector(".ed-steps").value.split("\n").map((x) => x.trim()).filter(Boolean);
      const res = await api(`/api/skills/${id}`, { method: "PATCH", body: { employee_id: empId(), name: card.querySelector(".ed-name").value.trim(),
        description: card.querySelector(".ed-desc").value.trim(), steps } });
      state.editingSkill = null;
      toast(res.unshared ? res.message : "Skill saved");
    } else {
      await api(`/api/skills/${id}/${act}`, { method: "POST", body: { employee_id: empId() } });
      toast(act === "approve" ? "Skill approved: the agent can use it when Skills is on" : "Skill archived");
    }
    await loadSkills();
  } catch (e) {
    const errEl = card.querySelector(".ed-error");
    if (errEl) errEl.textContent = e.message; else toast(e.message, true);
    btn.disabled = false;
  }
}

function openSkill(id) {
  const s = state.skills.find((x) => x.id === id);
  $("#modal").classList.remove("hidden");
  if (!s) { $("#modal-body").innerHTML = `<p class="muted">This skill is no longer visible to ${escapeHtml(state.employees[empId()]?.name || "you")}.</p>`; return; }
  $("#modal-body").innerHTML = `
    <span class="tag sk">skill</span><span class="tag">${escapeHtml(s.status)}</span><span class="tag">v${s.version}</span>
    <h2>${escapeHtml(s.name)}</h2>
    <p>${escapeHtml(s.description)}</p>
    <p><strong>When to use:</strong> ${escapeHtml(s.trigger)}</p>
    <p><strong>Steps:</strong></p><ol>${s.steps.map((x) => `<li>${md(x).replace(/^<p>|<\/p>$/g, "")}</li>`).join("")}</ol>
    ${s.parameters.length ? `<p><strong>Asks you for:</strong></p><ul>${s.parameters.map((p) => `<li><code>${escapeHtml(p.name)}</code>: ${escapeHtml(p.description)}</li>`).join("")}</ul>` : ""}
    <p class="muted small">Learned from one of ${escapeHtml(state.employees[s.owner_employee_id]?.name || "")}'s chats, based on ${s.evidence.length} messages.
      ${s.scope === "org" ? `<b>Shared by ${escapeHtml(s.shared_by_name || state.employees[s.owner_employee_id]?.name || "")}</b> with the org (read-only for colleagues).` : "Private to its owner."}</p>`;
}

function openDoc(id) {
  const d = state.docs.find((x) => x.id === id);
  $("#modal").classList.remove("hidden");
  if (!d) { $("#modal-body").innerHTML = `<p class="muted">This document is no longer visible.</p>`; return; }
  const snippet = state.docSnippets[id];
  $("#modal-body").innerHTML = `
    <span class="tag doc">document</span><span class="tag">${escapeHtml(d.status)}</span><span class="tag">${d.scope === "org" ? "Shared with org" : "Private"}</span>
    ${d.scope === "org" ? `<p class="small"><b>Shared by ${escapeHtml(d.shared_by_name || state.employees[d.owner_employee_id]?.name || "")}</b>${d.owner_employee_id !== empId() ? " (read-only)" : ""}</p>` : ""}
    <h2>${escapeHtml(d.title)}</h2>
    <p class="muted small">${escapeHtml(d.mime_type)} · ${(d.size / 1024).toFixed(1)} KB · uploaded ${fmtDate(d.created_at)} by ${escapeHtml(state.employees[d.owner_employee_id]?.name || d.owner_employee_id)}</p>
    ${snippet ? `<p><strong>Excerpt the agent used:</strong></p><blockquote class="excerpt-quote">${escapeHtml(snippet)}</blockquote>`
              : `<p class="muted small">Excerpts are shown for answers given in this browser session.</p>`}`;
}

function openKnowledge() {
  const k = $("#knowledge");
  k.classList.remove("collapsed-narrow");
  $("#knowledge-toggle").setAttribute("aria-expanded", "true");
}

async function selectEmployee(id) {
  state.employeeId = id;
  state.loadToken++;
  state.editingSkill = null;
  clearTimeout(state.docPoll);
  resetChat();
  renderProfile();
  await Promise.all([loadSessions(), loadDocs(), loadSkills()]);
}

async function selectAgent(id) {
  state.agentId = id;
  renderAgents();
  resetChat();
  await loadSessions();
}

function renderRagToggle() {
  const on = ragOn();
  $("#use-rag").disabled = !on;
  $("#rag-toggle").classList.toggle("disabled", !on);
  $("#rag-help").textContent = on ? "Answer only from documents you can see (yours and shared)" : "Turned off on this server";
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
    await loadOrg();
    await Promise.all([loadPlaybooks(), selectEmployee(state.employeeId)]);
    toast("Demo data reset");
  } catch (e) {
    $("#token-error").textContent = e.status === 401 ? "Invalid admin token." : e.message;
  } finally {
    setBusy(btn, false, "Reset");
  }
}

// ---------------- traces ----------------

const fmtMs = (ms) => (ms == null ? "–" : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`);

function cloudTraceUrl(traceId) {
  const project = state.health.project;
  if (!project) return "";
  return traceId ? `https://console.cloud.google.com/traces/list?project=${encodeURIComponent(project)}&tid=${encodeURIComponent(traceId)}`
                 : `https://console.cloud.google.com/traces/list?project=${encodeURIComponent(project)}`;
}

// Group recorded LLM calls by request trace, newest first.
function renderTraces(calls, compact) {
  if (!calls.length) return `<p class="muted">No LLM calls recorded yet. Ask a question, chat or run Distill.</p>`;
  const groups = [];
  const byId = {};
  for (const c of calls) {
    const key = c.trace_id || c.id;
    if (!byId[key]) { byId[key] = { key, trace_id: c.trace_id, request: c.request, ts: c.ts, calls: [] }; groups.push(byId[key]); }
    byId[key].calls.push(c);
  }
  return groups.map((g) => {
    const total = g.calls.reduce((a, c) => a + (c.latency_ms || 0), 0);
    const errs = g.calls.filter((c) => c.error).length;
    const tin = g.calls.reduce((a, c) => a + (c.input_tokens || 0), 0);
    const tout = g.calls.reduce((a, c) => a + (c.output_tokens || 0), 0);
    const link = g.trace_id && state.health.tracing === "cloudtrace" ? cloudTraceUrl(g.trace_id) : "";
    const rows = g.calls.map((c) => `
      <tr class="${c.error ? "err" : ""}">
        <td><code>${escapeHtml(c.operation)}</code></td><td>${escapeHtml(c.model || "")}</td>
        <td>${fmtMs(c.latency_ms)}</td>
        <td>${c.input_tokens ?? "–"} / ${c.output_tokens ?? "–"}</td>
        <td>${(c.playbook_ids || []).map((id) => `<button class="chip cite small" data-playbook="${escapeHtml(id)}">${escapeHtml(state.playbooks[id]?.title || id)}</button>`).join("") || ""}</td>
        <td>${c.error ? `<span class="error">${escapeHtml(c.error)}</span>` : ""}
          ${compact ? "" : `<details><summary>excerpt</summary><div class="excerpt"><b>prompt</b><pre>${escapeHtml(c.prompt_excerpt || "")}</pre><b>response</b><pre>${escapeHtml(c.response_excerpt || "")}</pre></div></details>`}</td>
      </tr>`).join("");
    return `<div class="trace-group">
      <div class="row between trace-head">
        <span><strong>${escapeHtml(g.request || "request")}</strong> <span class="muted">· ${g.calls.length} calls · ${fmtMs(total)} in model calls${tin || tout ? ` · ${tin}/${tout} tokens` : ""}${errs ? ` · <span class="error">${errs} errors</span>` : ""} · ${timeAgo(g.ts)}</span></span>
        ${link ? `<a class="small" href="${link}" target="_blank" rel="noopener">Cloud Trace ↗</a>` : ""}
      </div>
      <div class="table-wrap"><table class="table compact"><thead><tr><th>Operation</th><th>Model</th><th>Latency</th><th>Tokens in/out</th><th>Playbooks</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
  }).join("");
}

async function loadTraces(target, compact) {
  target.innerHTML = `<p class="typing">Loading…</p>`;
  try {
    const { calls } = await api(`/api/traces?limit=${compact ? 20 : 50}`);
    target.innerHTML = renderTraces(calls, compact);
  } catch (e) {
    target.innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
  }
}

function openDrawer() {
  $("#trace-drawer").hidden = false;
  loadTraces($("#drawer-body"), true);
}

// ---------------- usage (admin) ----------------

async function loadUsage() {
  const box = $("#usage-view");
  if (!getToken()) {
    box.innerHTML = `<div class="row"><span class="muted small">Today's usage vs limits needs the admin token.</span>
      <input type="password" id="usage-token" placeholder="Admin token"><button class="btn ghost-light small" id="usage-go">Show</button></div>`;
    $("#usage-go").onclick = () => { saveToken($("#usage-token").value); loadUsage(); };
    return;
  }
  try {
    const u = await api("/api/usage");
    box.innerHTML = `<div class="muted small">Today (${escapeHtml(u.date)}, resets ${escapeHtml(u.resets)}) ·
      limits ${u.limits_enabled ? "on" : "<b>off</b>"} · per IP per hour: ${Object.entries(u.ip_limits_per_hour).map(([k, v]) => `${escapeHtml(k)} ${v}`).join(", ")}</div>
      <div class="usage-grid">${u.buckets.map((b) => {
        const pct = b.count == null ? 0 : Math.min(100, Math.round((100 * b.count) / Math.max(1, b.limit)));
        return `<div class="usage-row"><span>${escapeHtml(b.bucket)}</span><span class="usage-bar"><span style="width:${pct}%"></span></span>
          <span class="muted small">${b.count ?? "?"} / ${b.limit}</span></div>`;
      }).join("")}</div>`;
  } catch (e) {
    box.innerHTML = `<p class="muted small">${e.status === 401 ? "Admin token not accepted." : escapeHtml(e.message)}</p>`;
    if (e.status === 401) saveToken("");
  }
}

// ---------------- guardrails (admin) ----------------

async function loadGuardrails() {
  const box = $("#guardrail-view");
  if (!getToken()) {
    box.innerHTML = `<div class="row"><span class="muted small">Guardrail activity needs the admin token.</span>
      <input type="password" id="gr-token" placeholder="Admin token"><button class="btn ghost-light small" id="gr-go">Show</button></div>`;
    $("#gr-go").onclick = () => { saveToken($("#gr-token").value); loadGuardrails(); };
    return;
  }
  try {
    const g = await api("/api/guardrail/summary");
    const c = g.counts;
    box.innerHTML = `
      <div class="report"><div class="stats"><span class="outcome created">${c.clean} shared clean</span>
        <span class="share-blocked">${c.blocked} blocked</span><span>${c.couldnt_verify} couldn't verify</span>
        <span class="muted">${c.unshared} unshared</span><span class="muted">human review: ${escapeHtml(g.human_review)}</span></div></div>
      <h3>Detectors</h3>
      <div class="table-wrap"><table class="table compact"><thead><tr><th>Layer</th><th>Detects</th><th>Category</th></tr></thead><tbody>
        ${g.detectors.map((d) => `<tr><td>${d.layer === "llm" ? "Gemini" : "Pattern"}</td><td>${escapeHtml(d.label)}</td><td><code>${escapeHtml(d.category)}</code></td></tr>`).join("")}
      </tbody></table></div>
      <h3>Last ${g.events.length} share events</h3>
      <div class="table-wrap"><table class="table compact"><thead><tr><th>When</th><th>Who</th><th>Item</th><th>Action</th><th>Verdict</th><th>Finding categories</th></tr></thead><tbody>
        ${g.events.length ? g.events.map((e) => `<tr><td>${timeAgo(e.created_at)}</td><td>${escapeHtml(e.actor || "")}</td>
          <td>${escapeHtml(e.item_type)}</td><td>${escapeHtml(e.action)}</td><td>${escapeHtml(e.verdict)}</td>
          <td>${e.finding_categories.map((x) => `<code>${escapeHtml(x)}</code>`).join(" ") || "–"}</td></tr>`).join("")
          : `<tr><td colspan="6" class="empty">No share attempts yet.</td></tr>`}
      </tbody></table></div>`;
  } catch (e) {
    box.innerHTML = `<p class="muted small">${e.status === 401 ? "Admin token not accepted." : escapeHtml(e.message)}</p>`;
    if (e.status === 401) saveToken("");
  }
}

// ---------------- routing ----------------

const ADMIN_TABS = [
  { id: "knowledge", label: "Knowledge" },
  { id: "traces", label: "Traces" },
  { id: "agents", label: "Agents", preview: true },
  { id: "users", label: "Users & Teams", preview: true },
  { id: "mcps", label: "MCPs", preview: true },
  { id: "skills", label: "Skills & Sub-agents", preview: true },
  { id: "guardrails", label: "Guardrails" },
  { id: "roadmap", label: "Roadmap" },
];
const DEFAULT_ROUTE = "#/user";

function parseRoute() {
  if (/^#\/user(\/.*)?$/.test(location.hash)) return { mode: "user", tab: "home" };   // #/user/chat etc. still land here
  const [, tab] = location.hash.match(/^#\/admin\/([a-z]+)/) || [];
  if (ADMIN_TABS.some((t) => t.id === tab)) return { mode: "admin", tab };
  return null;
}

function route() {
  const r = parseRoute();
  if (!r) { history.replaceState(null, "", DEFAULT_ROUTE); return route(); }
  if (r.mode === "user" && location.hash !== "#/user") history.replaceState(null, "", "#/user");
  const key = `${r.mode}-${r.tab}`;
  const admin = r.mode === "admin";
  document.body.classList.toggle("mode-admin", admin);
  $("#admin-link").textContent = admin ? "← Your workspace" : "Admin ↗";
  $("#admin-link").href = admin ? "#/user" : "#/admin/knowledge";
  $("#subnav").hidden = !admin;
  $("#subnav").innerHTML = admin ? ADMIN_TABS.map((t) =>
    `<a href="#/admin/${t.id}" class="${t.id === r.tab ? "active" : ""}">${escapeHtml(t.label)}${t.preview ? `<span class="nav-preview" title="Preview · planned">preview</span>` : ""}</a>`).join("") : "";
  document.querySelectorAll(".page").forEach((p) => { p.hidden = p.id !== `page-${key}`; });
  if (window.OrgMindPreviews?.has(key)) window.OrgMindPreviews.render(key, state);
  if (key === "user-home") selectEmployee(state.employeeId).catch((e) => toast(e.message, true));
  if (key === "admin-traces") {
    $("#cloud-trace-link").href = cloudTraceUrl() || "#";
    $("#cloud-trace-link").hidden = !cloudTraceUrl();
    loadTraces($("#traces-view"), false);
    loadUsage();
  }
  if (key === "admin-guardrails") loadGuardrails();
}

// ---------------- collapsible side panels ----------------

function initPanels() {
  document.querySelectorAll("[data-collapsible]").forEach((panel) => {
    const key = `orgmind_panel_${panel.dataset.collapsible}`;
    const set = (collapsed) => {
      panel.classList.toggle("collapsed", collapsed);
      panel.closest(".split")?.classList.toggle("side-collapsed", collapsed);
      try { localStorage.setItem(key, collapsed ? "1" : "0"); } catch {}
    };
    let saved = "0";
    try { saved = localStorage.getItem(key) || "0"; } catch {}
    set(saved === "1");
    panel.querySelector(".collapse-btn").addEventListener("click", () => set(true));
    panel.querySelector(".rail").addEventListener("click", () => set(false));
  });
}

// ---------------- wiring ----------------

document.addEventListener("click", (e) => {
  const share = e.target.closest("[data-share-act]");
  if (share) { e.preventDefault(); e.stopPropagation(); return shareAction(share); }
  const skv = e.target.closest("[data-skill-view]");
  if (skv) return openSkill(skv.dataset.skillView);
  const pb = e.target.closest("[data-playbook]");
  if (pb) return openPlaybook(pb.dataset.playbook);
  const sk = e.target.closest(".chip[data-skill]");
  if (sk) return openSkill(sk.dataset.skill);
  const doc = e.target.closest("[data-doc]");
  if (doc) return openDoc(doc.dataset.doc);
  const act = e.target.closest("[data-skill-act]");
  if (act) { e.preventDefault(); return skillAction(act); }
  const ag = e.target.closest("[data-agent]");
  if (ag) return selectAgent(ag.dataset.agent).catch((err) => toast(err.message, true));
  const mode = e.target.closest("[data-docmode]");
  if (mode) return setDocMode(mode.dataset.docmode);
  const preset = e.target.closest("[data-q]");
  if (preset) { $("#compare-question").value = preset.dataset.q; return runCompare(); }
  const ses = e.target.closest("[data-session]");
  if (ses) return openSession(ses.dataset.session).catch((err) => toast(err.message, true));
});

window.addEventListener("hashchange", route);
$("#trace-btn").addEventListener("click", openDrawer);
$("#drawer-close").addEventListener("click", () => { $("#trace-drawer").hidden = true; });
$("#drawer-all").addEventListener("click", () => { $("#trace-drawer").hidden = true; });
$("#traces-refresh").addEventListener("click", () => loadTraces($("#traces-view"), false));
$("#guardrail-refresh").addEventListener("click", loadGuardrails);
$("#compare-btn").addEventListener("click", runCompare);
$("#compare-question").addEventListener("keydown", (e) => { if (e.key === "Enter") runCompare(); });
$("#compare-agent").addEventListener("change", renderPresets);
$("#distill-btn").addEventListener("click", runDistill);
$("#composer").addEventListener("submit", sendMessage);
$("#new-session-btn").addEventListener("click", () => newSession().catch((e) => toast(e.message, true)));
$("#employee-select").addEventListener("change", (e) => selectEmployee(e.target.value).catch((err) => toast(err.message, true)));
$("#session-select").addEventListener("change", (e) => { if (e.target.value) openSession(e.target.value).catch((err) => toast(err.message, true)); });
$("#learn-btn").addEventListener("click", learnSkills);
$("#doc-add-btn").addEventListener("click", () => { $("#doc-form").hidden = !$("#doc-form").hidden; $("#doc-error").textContent = ""; });
$("#doc-cancel").addEventListener("click", () => { $("#doc-form").reset(); $("#doc-form").hidden = true; });
$("#doc-form").addEventListener("submit", submitDoc);
$("#knowledge-toggle").addEventListener("click", () => {
  const k = $("#knowledge");
  const collapsed = k.classList.toggle("collapsed-narrow");
  $("#knowledge-toggle").setAttribute("aria-expanded", String(!collapsed));
});
setInterval(tickElapsed, 1000);
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
  if (e.key === "Escape") { $("#modal").classList.add("hidden"); $("#token-modal").classList.add("hidden"); $("#trace-drawer").hidden = true; }
});

(async () => {
  initPanels();
  try {
    await loadOrg();
    await loadPlaybooks();
  } catch (e) {
    toast(`Failed to load: ${e.message}`, true);
  }
  route();
})();
