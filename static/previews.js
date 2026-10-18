// Preview (planned) pages and the static Roadmap page.
// Rules: every preview page shows a "Preview · planned" badge, all controls are disabled with a
// "Planned for …" tooltip, sample rows are labelled "Sample", and nothing here makes a network call.
(function () {
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const planText = (v) => (v === "December round" ? "Planned for the December round" : `Planned for ${v}`);

  // Wrap a disabled control so the tooltip works even though disabled elements swallow hover events.
  const planned = (v, html) => `<span class="planned" title="${planText(v)}">${html}</span>`;
  const btn = (v, label, cls = "btn primary") => planned(v, `<button class="${cls}" disabled>${esc(label)}</button>`);
  const sample = `<span class="sample-tag">Sample</span>`;

  function header(title, version, blurb) {
    return `<div class="preview-head">
        <div class="row"><h2>${esc(title)}</h2>
          <span class="preview-badge">Preview · planned</span>
          <span class="version-badge">${esc(version)}</span></div>
        <p class="what">${blurb}</p>
      </div>`;
  }

  // ---------------- Admin › Agents ----------------
  function agents(state) {
    const v = "V2";
    const live = Object.values(state.agents || {});
    const field = (label, control) => `<label class="field">${esc(label)}${planned(v, control)}</label>`;
    return `<div class="panel">${header("Agents", v,
      "Create and configure agents per team: instructions, tools (MCP servers), knowledge sources and model. In the December round agents move to ADK on Vertex AI Agent Engine with sub-agents.")}
      <h3>Deployed agents <span class="live-tag">Live</span></h3>
      <table class="table"><thead><tr><th>Agent</th><th>Team</th><th>What it does</th><th></th></tr></thead><tbody>
        ${live.length ? live.map((a) => `<tr><td><strong>${esc(a.name)}</strong></td><td>${esc(a.team)}</td><td>${esc(a.description)}</td>
          <td>${btn(v, "Edit", "btn ghost-light small")}</td></tr>`).join("")
          : `<tr><td colspan="4" class="muted">No agents loaded.</td></tr>`}
      </tbody></table>
      <h3>Create agent</h3>
      <form class="form-grid" onsubmit="return false">
        ${field("Name", `<input type="text" disabled placeholder="e.g. Finance Agent">`)}
        ${field("Team", `<select disabled><option>finance</option></select>`)}
        ${field("Description", `<input type="text" disabled placeholder="What this agent helps with">`)}
        ${field("Model", `<select disabled><option>gemini-2.5-flash</option></select>`)}
        <fieldset class="field"><legend>Tools / MCPs</legend>
          ${["Jenkins", "GitHub", "Google Drive", "Slack"].map((t) => planned(v, `<label class="check"><input type="checkbox" disabled> ${t}</label>`)).join("")}
        </fieldset>
        <fieldset class="field"><legend>Knowledge sources</legend>
          ${["Org playbooks", "Google Drive folders", "Uploaded sessions"].map((t) => planned(v, `<label class="check"><input type="checkbox" disabled> ${t}</label>`)).join("")}
        </fieldset>
        <div class="row end span-2">${btn(v, "Create agent")}</div>
      </form></div>`;
  }

  // ---------------- Admin › Users & Teams ----------------
  function users() {
    const v = "V2";
    return `<div class="panel">${header("Users & Teams", v,
      "Manage who uses which agents: sync the directory, group employees into teams and give each team its bundled agents. The demo's Acme employees are seeded data and aren't managed here yet.")}
      <div class="row between"><h3>Users</h3><div class="row">${btn(v, "Sync directory", "btn ghost-light small")}${btn(v, "Invite user", "btn primary small")}</div></div>
      <table class="table"><thead><tr><th>Name</th><th>Email</th><th>Team</th><th>Role</th></tr></thead>
        <tbody><tr><td colspan="4" class="empty">No users yet. Directory sync is planned for V2.</td></tr></tbody></table>
      <div class="row between"><h3>Teams</h3>${btn(v, "Create team", "btn primary small")}</div>
      <table class="table"><thead><tr><th>Team</th><th>Members</th><th>Agents</th></tr></thead>
        <tbody><tr><td colspan="3" class="empty">No teams yet.</td></tr></tbody></table></div>`;
  }

  // ---------------- Admin › MCPs ----------------
  function mcps() {
    const v = "V2";
    return `<div class="panel">${header("MCP servers", v,
      "Register MCP servers once at the org level; admins then attach them to agents as tools. No servers are registered in this version.")}
      <table class="table"><thead><tr><th>Server</th><th>Transport</th><th>Used by</th><th>Status</th><th></th></tr></thead><tbody>
        ${[["Jenkins MCP", "HTTP (streamable)", "Dev Agent"], ["GitHub MCP", "HTTP (streamable)", "Dev Agent"],
           ["Google Drive MCP", "HTTP (streamable)", "HR Agent"], ["Slack MCP", "HTTP (streamable)", "Dev, HR Agent"]]
          .map(([n, t, u]) => `<tr><td><strong>${n}</strong> ${sample}</td><td>${t}</td><td>${u}</td>
            <td><span class="status off">Not registered</span></td><td>${btn(v, "Configure", "btn ghost-light small")}</td></tr>`).join("")}
      </tbody></table>
      <h3>Register a server</h3>
      <form class="form-grid" onsubmit="return false">
        <label class="field">Name${planned(v, `<input type="text" disabled placeholder="e.g. Jenkins MCP">`)}</label>
        <label class="field">Server URL${planned(v, `<input type="text" disabled placeholder="https://…">`)}</label>
        <label class="field">Auth${planned(v, `<select disabled><option>OAuth</option></select>`)}</label>
        <div class="row end">${btn(v, "Register server")}</div>
      </form></div>`;
  }

  // ---------------- Admin › Skills & Sub-agents ----------------
  function skills(state) {
    const v = "December round";
    const pbs = Object.values(state.playbooks || {});
    return `<div class="panel">${header("Skills & Sub-agents", v,
      "Turn proven playbooks into reusable skills that any agent or sub-agent can load when its trigger matches, instead of only quoting them in answers.")}
      <div class="flow">
        <div class="flow-node"><strong>Playbook</strong><span>title · trigger · steps<br>distilled from resolved sessions</span></div>
        <div class="flow-arrow">→</div>
        <div class="flow-node"><strong>Skill</strong><span>trigger → when to use<br>steps → instructions<br>commands → MCP tools</span></div>
        <div class="flow-arrow">→</div>
        <div class="flow-node"><strong>Sub-agent</strong><span>e.g. a Deploy sub-agent under the Dev agent loads the skill and runs the steps with tools</span></div>
      </div>
      <div class="row between"><h3>Playbooks that could become skills <span class="live-tag">Live</span></h3>${btn(v, "New skill", "btn primary small")}</div>
      <table class="table"><thead><tr><th>Playbook</th><th>Agent</th><th>Sources</th><th></th></tr></thead><tbody>
        ${pbs.length ? pbs.map((p) => `<tr><td>${esc(p.title)}</td><td>${esc(p.agent_kind)}</td><td>${p.source_employee_ids.length} employees</td>
          <td>${btn(v, "Promote to skill", "btn ghost-light small")}</td></tr>`).join("")
          : `<tr><td colspan="4" class="empty">No playbooks yet. Run Distill on the Knowledge tab first.</td></tr>`}
      </tbody></table></div>`;
  }

  // ---------------- Admin › Roadmap (real, static) ----------------
  const ST = { now: "Working now", dec: "December round" };
  const item = (st, text) => `<li class="st-${st}"><span class="st-dot"></span>${text}</li>`;

  function roadmap() {
    return `<div class="panel">
      <h2>Roadmap</h2>
      <p class="muted">What runs today and what is planned for the December round. Colors show status.</p>
      <div class="legend">${Object.entries(ST).map(([k, l]) => `<span class="st-${k}"><span class="st-dot"></span>${l}</span>`).join("")}</div>
      <div class="arch">
        <div class="arch-node">
          <h4>Users / Teams</h4>
          <ul>${item("now", "Synthetic Acme employees, each with siloed sessions")}
              ${item("now", "User workspace: view as any employee, chat, My knowledge")}
              ${item("dec", "Users &amp; Teams admin, directory sync")}</ul>
        </div>
        <div class="arch-arrow" aria-hidden="true">→</div>
        <div class="arch-node">
          <h4>Agents</h4>
          <div class="arch-sub"><h5>Main agent</h5>
            <ul>${item("now", "Dev Agent, HR Agent (Gemini 2.5 Flash on Vertex AI)")}
                ${item("now", "Playbook retrieval + citations")}
                ${item("now", "Document upload + RAG on Vertex AI Search (private unless shared)")}
                ${item("now", "Daily cost caps (global + per IP) with today's usage vs limits in Admin › Traces")}
                ${item("dec", "Agent builder (create / edit agents)")}
                ${item("dec", "ADK on Vertex AI Agent Engine")}</ul></div>
          <div class="arch-sub"><h5>Sub-agents</h5>
            <ul>${item("dec", "Task sub-agents that load skills")}
                ${item("dec", "MCP connections (Jenkins, GitHub, Drive, Slack)")}
                ${item("dec", "MCP server registry")}</ul></div>
        </div>
        <div class="arch-arrow" aria-hidden="true">←</div>
        <div class="arch-node">
          <h4>Distillation</h4>
          <ul>${item("now", "Summarize → cluster across employees → compile")}
              ${item("now", "Only resolved sessions give steps; per-person values dropped")}
              ${item("now", "Traces: Cloud Trace + in-app trace view")}
              ${item("dec", "Upload local assistant sessions")}
              ${item("dec", "PII redaction of sessions before distill")}</ul>
        </div>
        <div class="arch-arrow" aria-hidden="true">→</div>
        <div class="arch-node">
          <h4>Playbooks / Skills</h4>
          <ul>${item("now", "Playbooks with sources and versions")}
              ${item("now", "Compare: with vs without playbooks")}
              ${item("now", "Skills learned from one employee's chat: review, approve, apply (private unless shared)")}
              ${item("now", "Share docs and skills with the org, behind an automatic PII/secret guardrail (not a guarantee)")}
              ${item("now", "Share audit log: every share attempt with its verdict and finding categories (Admin › Guardrails)")}
              ${item("dec", "Human review of shared items and playbooks before publish")}
              ${item("dec", "Skill creation from playbooks")}</ul>
        </div>
      </div>
      <p class="muted small">Arrows: users talk to agents; distillation feeds agents with playbooks compiled from everyone's resolved sessions.</p>
    </div>`;
  }

  const PAGES = {
    "admin-agents": agents, "admin-users": users,
    "admin-mcps": mcps, "admin-skills": skills, "admin-roadmap": roadmap,
  };

  window.OrgMindPreviews = {
    has: (key) => key in PAGES,
    render(key, state) {
      const el = document.getElementById(`page-${key}`);
      if (el && PAGES[key]) {
        el.innerHTML = PAGES[key](state)
          .replace(/<table/g, '<div class="table-wrap"><table').replace(/<\/table>/g, "</table></div>");
      }
    },
  };
})();
