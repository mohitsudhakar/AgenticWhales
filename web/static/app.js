// AgenticWhales web UI — single-page app, no build step.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  config: null,
  sessions: [],
  batches: [],
  view: "empty",       // "empty" | "config" | "session" | "batch-config" | "batch"
  activeSessionId: null,
  activeBatchId: null,
  session: null,       // full session payload
  batch: null,         // full batch payload
  ws: null,
  batchWs: null,
};

// ---------- bootstrap ----------

window.addEventListener("DOMContentLoaded", async () => {
  // Attach listeners FIRST so the UI is responsive even if data load fails.
  $("#new-session-btn").addEventListener("click", guardCreate(openConfigView));
  $("#new-batch-btn").addEventListener("click", guardCreate(openBatchConfigView));
  $("#bf-provider").addEventListener("change", syncBatchProviderDependentFields);
  $("#portfolio-btn").addEventListener("click", openPortfolioView);
  $("#pf-add-row").addEventListener("click", () => addPortfolioRow());
  $("#pf-save").addEventListener("click", savePortfolio);
  initSidebarSections();
  $("#config-form").addEventListener("submit", submitConfig);
  $("#batch-form").addEventListener("submit", submitBatch);
  $("#bf-select-all").addEventListener("click", () => toggleAllBatchTickers(true));
  $("#bf-clear").addEventListener("click", () => toggleAllBatchTickers(false));
  $("#bf-custom-add").addEventListener("click", addCustomTicker);
  $("#bf-custom-ticker").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addCustomTicker(); }
  });
  $("#m-close").addEventListener("click", closeAgentModal);
  $("#agent-modal").addEventListener("click", (e) => {
    if (e.target.id === "agent-modal") closeAgentModal();
  });
  $("#s-cancel").addEventListener("click", cancelActiveSession);
  $("#b-cancel").addEventListener("click", cancelActiveBatch);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeAgentModal();
      // Privacy / terms / upgrade modals are dismissable with Escape too.
      for (const id of ["privacy-modal", "terms-modal", "upgrade-modal"]) {
        const m = document.getElementById(id);
        if (m && !m.classList.contains("hidden")) m.classList.add("hidden");
      }
    }
  });
  initWelcomeFlow();
  initMobileNav();

  // Config is auth-free; sessions/batches require a logged-in user, so we
  // skip them at boot — `reloadOnAuthChange()` repopulates them when the
  // user signs in (or after the welcome modal completes).
  try { await loadConfig(); } catch (e) { console.error("loadConfig failed:", e); }
  setView("empty");
});

async function reloadOnAuthChange() {
  // Clear current selection so the user doesn't keep viewing someone else's
  // session by accident across a sign-out / switch.
  state.activeSessionId = null;
  state.activeBatchId = null;
  state.session = null;
  state.batch = null;
  closeWebsocket();
  closeBatchWebsocket();
  state.sessions = [];
  state.batches = [];
  renderSessionList();
  renderBatchList();
  if (!userState.current) {
    $("#usage-link")?.classList.add("hidden");
    setView("empty");
    return;
  }
  try { await loadSessions(); } catch (e) { console.error("loadSessions failed:", e); }
  try { await loadBatches();  } catch (e) { console.error("loadBatches failed:", e); }
  if (state.view === "empty" && state.sessions.length) {
    setView("session-or-empty");
  }
}

async function loadConfig() {
  const res = await fetch("/api/config");
  state.config = await res.json();
  populateProviderSelect();
  populateAnalystChips();
  populateLanguageSelect();
  syncProviderDependentFields();
  $("#f-provider").addEventListener("change", syncProviderDependentFields);

  $("#f-date").value = new Date().toISOString().slice(0, 10);
  $("#f-ticker").value = "SPY";
}

async function loadSessions() {
  const res = await fetch("/api/sessions");
  state.sessions = await res.json();
  renderSessionList();
}

async function loadBatches() {
  const res = await fetch("/api/batches");
  state.batches = await res.json();
  renderBatchList();
}

// ---------- views ----------

function setView(v) {
  if (v === "session-or-empty") {
    if (state.sessions.length) {
      openSession(state.sessions[0].id);
    } else {
      setView("empty");
    }
    return;
  }
  state.view = v;
  $("#view-empty").classList.toggle("hidden", v !== "empty");
  $("#view-config").classList.toggle("hidden", v !== "config");
  $("#view-session").classList.toggle("hidden", v !== "session");
  $("#view-batch-config").classList.toggle("hidden", v !== "batch-config");
  $("#view-batch").classList.toggle("hidden", v !== "batch");
  $("#view-portfolio").classList.toggle("hidden", v !== "portfolio");
}

function openConfigView() {
  state.activeSessionId = null;
  state.session = null;
  closeWebsocket();
  $$(".session-item").forEach((el) => el.classList.remove("active"));
  setView("config");
}

// ---------- sidebar ----------

function renderSessionList() {
  const ul = $("#session-list");
  ul.innerHTML = "";
  if (!state.sessions.length) {
    const empty = document.createElement("div");
    empty.className = "subtle";
    empty.style.padding = "12px 4px";
    empty.style.fontSize = "12px";
    empty.textContent = "No sessions yet. Start a new analysis above.";
    ul.appendChild(empty);
    return;
  }
  for (const s of state.sessions) {
    const li = document.createElement("li");
    li.className = "session-item";
    if (s.id === state.activeSessionId) li.classList.add("active");
    li.innerHTML = `
      <div class="session-ticker">
        <span>${escapeHTML(s.ticker)}</span>
        <span class="session-status ${s.status}">${statusLabel(s.status)}</span>
      </div>
      <div class="session-date">${s.analysis_date} · ${formatRelative(s.created_at)}</div>
    `;
    li.addEventListener("click", () => openSession(s.id));
    ul.appendChild(li);
  }
}

function statusLabel(s) {
  if (s === "running")   return "● live";
  if (s === "completed") return "✓ done";
  if (s === "failed")    return "✕ failed";
  if (s === "cancelled") return "⊘ cancelled";
  return "queued";
}

function formatRelative(ts) {
  if (!ts) return "";
  const diff = (Date.now() / 1000) - ts;
  if (diff < 60)    return "just now";
  if (diff < 3600)  return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

// ---------- config form ----------

// Pre-select an option whose value matches `value`, if it exists. No-op
// otherwise so the browser keeps the first option (its natural default).
function selectIfPresent(selectEl, value) {
  if (!selectEl || !value) return;
  if ([...selectEl.options].some((o) => o.value === value)) {
    selectEl.value = value;
  }
}

function populateProviderSelect() {
  const sel = $("#f-provider");
  sel.innerHTML = "";
  for (const p of state.config.providers) {
    const opt = document.createElement("option");
    opt.value = p.key;
    opt.textContent = p.label;
    sel.appendChild(opt);
  }
  selectIfPresent(sel, state.config.defaults?.provider);
}

function populateAnalystChips() {
  const wrap = $("#f-analysts");
  wrap.innerHTML = "";
  for (const a of state.config.analysts) {
    const c = document.createElement("div");
    c.className = "chip active";
    c.dataset.key = a.key;
    c.textContent = a.label;
    c.addEventListener("click", () => c.classList.toggle("active"));
    wrap.appendChild(c);
  }
}

function populateLanguageSelect() {
  const sel = $("#f-language");
  sel.innerHTML = "";
  for (const lang of state.config.languages) {
    const opt = document.createElement("option");
    opt.value = lang;
    opt.textContent = lang;
    sel.appendChild(opt);
  }
}

function syncProviderDependentFields() {
  const provider = $("#f-provider").value;
  const models = state.config.models[provider] || { quick: [], deep: [] };

  fillModelSelect("#f-quick", models.quick);
  fillModelSelect("#f-deep",  models.deep);

  // Apply env-driven default models (only meaningful when the provider also
  // matches the configured default — otherwise we'd be trying to select a
  // Gemini model id inside an OpenAI dropdown).
  const defaults = state.config.defaults || {};
  if (defaults.provider === provider) {
    selectIfPresent($("#f-quick"), defaults.quick_model);
    selectIfPresent($("#f-deep"),  defaults.deep_model);
  }

  // Provider-specific thinking field
  const wrap = $("#f-thinking-wrap");
  const label = $("#f-thinking-label");
  const sel = $("#f-thinking");
  sel.innerHTML = "";

  let opts = null;
  if (provider === "google") {
    label.textContent = "Thinking mode";
    opts = [
      ["high",    "Enable Thinking (recommended)"],
      ["minimal", "Minimal / Disable"],
    ];
  } else if (provider === "openai") {
    label.textContent = "Reasoning effort";
    opts = [
      ["medium", "Medium (default)"],
      ["high",   "High (more thorough)"],
      ["low",    "Low (faster)"],
    ];
  } else if (provider === "anthropic") {
    label.textContent = "Effort level";
    opts = [
      ["high",   "High (recommended)"],
      ["medium", "Medium"],
      ["low",    "Low (faster)"],
    ];
  }

  if (!opts) {
    wrap.classList.add("hidden");
    return;
  }
  wrap.classList.remove("hidden");
  for (const [v, lbl] of opts) {
    const o = document.createElement("option");
    o.value = v; o.textContent = lbl;
    sel.appendChild(o);
  }
}

function fillModelSelect(selector, options) {
  const sel = $(selector);
  sel.innerHTML = "";
  for (const [label, value] of options) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    sel.appendChild(o);
  }
}

async function submitConfig(e) {
  e.preventDefault();
  const btn = $("#go-btn");
  btn.disabled = true;
  btn.querySelector(".go-btn-label").textContent = "Spinning up…";

  const provider = $("#f-provider").value;
  const providerObj = state.config.providers.find((p) => p.key === provider);

  const analysts = $$("#f-analysts .chip.active").map((c) => c.dataset.key);
  if (!analysts.length) {
    alert("Pick at least one analyst.");
    btn.disabled = false;
    btn.querySelector(".go-btn-label").textContent = "Let's go";
    return;
  }

  const payload = {
    ticker: $("#f-ticker").value.trim(),
    analysis_date: $("#f-date").value,
    llm_provider: provider,
    backend_url: providerObj?.url || null,
    quick_think_llm: $("#f-quick").value,
    deep_think_llm: $("#f-deep").value,
    research_depth: parseInt($("#f-depth").value, 10),
    analysts,
    output_language: $("#f-language").value,
  };
  const thinking = $("#f-thinking").value;
  if (provider === "google")    payload.google_thinking_level = thinking;
  if (provider === "openai")    payload.openai_reasoning_effort = thinking;
  if (provider === "anthropic") payload.anthropic_effort = thinking;

  try {
    const res = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const summary = await res.json();
    await loadSessions();
    openSession(summary.id);
  } catch (err) {
    alert(`Failed to start analysis: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.querySelector(".go-btn-label").textContent = "Let's go";
  }
}

// ---------- session view ----------

async function openSession(id) {
  state.activeSessionId = id;
  closeWebsocket();
  $$(".session-item").forEach((el) => el.classList.remove("active"));

  setView("session");

  const res = await fetch(`/api/sessions/${id}`);
  if (!res.ok) {
    alert("Session not found");
    setView("empty");
    return;
  }
  state.session = await res.json();
  renderSession();
  renderSessionList();

  if (state.session.status === "running" || state.session.status === "pending") {
    openWebsocket(id);
  }
}

function renderSession() {
  const s = state.session;
  $("#s-title").textContent = `${s.ticker} · ${s.analysis_date}`;
  $("#s-meta").textContent = `${s.config.llm_provider} · deep=${s.config.deep_think_llm} · quick=${s.config.quick_think_llm} · depth=${s.config.research_depth}`;
  const pill = $("#s-status");
  pill.textContent = s.status;
  pill.className = `status-pill ${s.status}`;
  const cancelBtn = $("#s-cancel");
  const cancellable = s.status === "running" || s.status === "pending";
  cancelBtn.classList.toggle("hidden", !cancellable);
  cancelBtn.disabled = false;
  cancelBtn.textContent = "Cancel";
  renderSessionStats();
  renderFinal();
  renderAgents();
}

function renderSessionStats() {
  $("#s-stats").innerHTML = formatStatsLine(state.session?.stats);
  $("#s-team-timings").innerHTML = formatTeamTimings(state.session?.team_timings);
}

async function cancelActiveSession() {
  const id = state.activeSessionId;
  if (!id) return;
  if (!confirm("Cancel this analysis? Work done so far will be discarded.")) return;
  const btn = $("#s-cancel");
  btn.disabled = true;
  btn.textContent = "Cancelling…";
  try {
    const res = await fetch(`/api/sessions/${id}/cancel`, { method: "POST" });
    if (!res.ok) {
      const msg = await res.text();
      alert(`Cancel failed: ${msg || res.statusText}`);
      btn.disabled = false;
      btn.textContent = "Cancel";
      return;
    }
    state.session = await res.json();
    renderSession();
    renderSessionList();
  } catch (e) {
    console.error("cancel failed:", e);
    alert(`Cancel failed: ${e.message || e}`);
    btn.disabled = false;
    btn.textContent = "Cancel";
  }
}

async function cancelActiveBatch() {
  const id = state.activeBatchId;
  if (!id) return;
  if (!confirm("Cancel this basket run? In-flight tickers will stop and remaining tickers will be skipped.")) return;
  const btn = $("#b-cancel");
  btn.disabled = true;
  btn.textContent = "Cancelling…";
  try {
    const res = await fetch(`/api/batches/${id}/cancel`, { method: "POST" });
    if (!res.ok) {
      const msg = await res.text();
      alert(`Cancel failed: ${msg || res.statusText}`);
      btn.disabled = false;
      btn.textContent = "Cancel";
      return;
    }
    state.batch = await res.json();
    renderBatch();
    renderBatchList();
  } catch (e) {
    console.error("cancel failed:", e);
    alert(`Cancel failed: ${e.message || e}`);
    btn.disabled = false;
    btn.textContent = "Cancel";
  }
}

function formatTeamTimings(timings) {
  if (!timings || !Object.keys(timings).length) return "";
  // Render in canonical team order so the line stays stable across runs.
  const order = ["Analyst Team", "Research Team", "Trading Team", "Risk Management", "Portfolio Management"];
  const seen = new Set(order);
  const teams = order.filter((t) => t in timings).concat(
    Object.keys(timings).filter((t) => !seen.has(t))
  );
  const parts = [];
  for (const team of teams) {
    const t = timings[team] || {};
    if (t.duration_s !== null && t.duration_s !== undefined) {
      parts.push(`<span class="stat">${escapeHTML(team)} <strong>${fmtDuration(t.duration_s)}</strong></span>`);
    } else if (t.started_at) {
      const elapsed = Math.max(0, (Date.now() / 1000) - t.started_at);
      parts.push(`<span class="stat">${escapeHTML(team)} <strong>${fmtDuration(elapsed)}…</strong></span>`);
    }
  }
  return parts.join("");
}

function fmtDuration(secs) {
  const s = Number(secs) || 0;
  if (s < 1) return `${(s * 1000).toFixed(0)}ms`;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const r = Math.round(s - m * 60);
  return `${m}m ${r}s`;
}

function formatStatsLine(stats) {
  if (!stats) return "";
  const tin = Number(stats.tokens_in || 0);
  const tout = Number(stats.tokens_out || 0);
  const calls = Number(stats.llm_calls || 0);
  const tools = Number(stats.tool_calls || 0);
  if (!tin && !tout && !calls && !tools) return "";
  return [
    `<span class="stat">↓ in <strong>${fmtNum(tin)}</strong></span>`,
    `<span class="stat">↑ out <strong>${fmtNum(tout)}</strong></span>`,
    `<span class="stat">Σ <strong>${fmtNum(tin + tout)}</strong> tokens</span>`,
    `<span class="stat"><strong>${calls}</strong> LLM calls</span>`,
    `<span class="stat"><strong>${tools}</strong> tool calls</span>`,
  ].join("");
}

function fmtNum(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

function renderFinal() {
  const s = state.session;
  const final = s.report_sections?.final_trade_decision;
  const body = $("#s-final-body");
  const tag = $("#s-final-tag");

  if (!final) {
    body.classList.remove("markdown");
    body.classList.add("subtle");
    if (s.status === "failed") {
      body.textContent = `Analysis failed: ${s.error || "unknown error"}`;
      tag.textContent = "failed";
      tag.className = "final-card-tag";
    } else if (s.status === "cancelled") {
      body.textContent = "Analysis was cancelled before a recommendation was produced.";
      tag.textContent = "cancelled";
      tag.className = "final-card-tag";
    } else if (s.status === "running") {
      body.textContent = "Agents are still deliberating…";
      tag.textContent = "in progress";
      tag.className = "final-card-tag";
    } else {
      body.textContent = "The Portfolio Manager will weigh in once the debate concludes…";
      tag.textContent = "awaiting";
      tag.className = "final-card-tag";
    }
    return;
  }

  body.classList.add("markdown");
  body.classList.remove("subtle");
  body.innerHTML = renderMarkdown(final);

  const verdict = inferVerdict(final);
  tag.textContent = verdict || "decision";
  tag.className = `final-card-tag ${verdict?.toLowerCase() || ""}`;
}

function inferVerdict(text) {
  const m = text.match(/\b(BUY|SELL|HOLD)\b/i);
  return m ? m[1].toUpperCase() : null;
}

function renderAgents() {
  const s = state.session;
  const grid = $("#s-agents");
  grid.innerHTML = "";
  for (const team of state.config.teams) {
    const present = team.agents.filter((a) => a in s.agent_status);
    if (!present.length) continue;
    const label = document.createElement("div");
    label.className = "team-label";
    label.textContent = team.name;
    grid.appendChild(label);

    for (const agent of present) {
      grid.appendChild(buildAgentCard(agent, team.name));
    }
  }
}

function buildAgentCard(agent, teamName) {
  const s = state.session;
  const status = s.agent_status[agent] || "pending";
  const card = document.createElement("div");
  card.className = "agent-card";
  if (status === "in_progress") card.classList.add("active-status");
  if (status === "completed")   card.classList.add("completed-status");
  card.dataset.agent = agent;

  const section = sectionForAgent(agent);
  const content = section ? s.report_sections?.[section] : null;
  const preview = content
    ? stripMarkdown(content).slice(0, 140)
    : (status === "in_progress" ? "Thinking…" : "Awaiting their turn.");

  card.innerHTML = `
    <div class="agent-card-top">
      <span class="agent-dot ${status}"></span>
      <div class="agent-name">${escapeHTML(agent)}</div>
    </div>
    <div class="agent-meta">${escapeHTML(teamName)} · ${status.replace("_", " ")}</div>
    <div class="agent-preview">${escapeHTML(preview)}</div>
  `;
  card.addEventListener("click", () => openAgentModal(agent, teamName));
  return card;
}

function sectionForAgent(agent) {
  const map = state.config.section_agent || {};
  for (const [section, owner] of Object.entries(map)) {
    if (owner === agent) return section;
  }
  return null;
}

// ---------- modal ----------

function openAgentModal(agent, teamName) {
  const s = state.session;
  $("#m-title").textContent = agent;
  $("#m-team").textContent = `${teamName} · ${(s.agent_status[agent] || "pending").replace("_", " ")}`;
  const section = sectionForAgent(agent);
  const content = section ? s.report_sections?.[section] : null;
  const body = $("#m-body");
  if (content) {
    body.classList.add("markdown");
    body.innerHTML = renderMarkdown(content);
  } else {
    body.classList.remove("markdown");
    const status = s.agent_status[agent] || "pending";
    body.innerHTML = `<p class="subtle">${
      status === "in_progress"
        ? "This agent is still working. Output will stream in here as it's produced."
        : "No output yet — this agent hasn't started or doesn't produce a primary report."
    }</p>`;
  }
  $("#agent-modal").classList.remove("hidden");
}

function closeAgentModal() {
  $("#agent-modal").classList.add("hidden");
}

// ---------- websocket ----------

function openWebsocket(id) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/sessions/${id}/stream`);
  state.ws = ws;
  ws.onmessage = (e) => {
    if (state.activeSessionId !== id) return;
    const event = JSON.parse(e.data);
    handleEvent(event);
  };
  ws.onclose = () => {
    if (state.ws === ws) state.ws = null;
  };
}

function closeWebsocket() {
  if (state.ws) {
    try { state.ws.close(); } catch {}
    state.ws = null;
  }
}

function handleEvent(event) {
  const s = state.session;
  if (!s) return;

  if (event.type === "session") {
    state.session = event.session;
    renderSession();
    loadSessions();      // refresh sidebar status
    if (["completed", "failed", "cancelled"].includes(event.session.status)) {
      closeWebsocket();
    }
    return;
  }

  if (event.type === "agent_status") {
    s.agent_status[event.agent] = event.status;
    updateAgentCard(event.agent);
    return;
  }

  if (event.type === "report") {
    s.report_sections = s.report_sections || {};
    s.report_sections[event.section] = event.content;
    renderFinal();
    if (event.agent) updateAgentCard(event.agent);
    return;
  }

  if (event.type === "message") {
    s.messages = s.messages || [];
    s.messages.push(event.message);
    return;
  }

  if (event.type === "stats") {
    s.stats = event.stats;
    renderSessionStats();
    return;
  }

  if (event.type === "team_timing") {
    s.team_timings = s.team_timings || {};
    s.team_timings[event.team] = event.timing;
    renderSessionStats();
    return;
  }
}

function updateAgentCard(agent) {
  const card = document.querySelector(`.agent-card[data-agent="${cssEscape(agent)}"]`);
  if (!card) return;
  // Find the team for this agent.
  const team = (state.config.teams.find((t) => t.agents.includes(agent)) || {}).name || "";
  const fresh = buildAgentCard(agent, team);
  card.replaceWith(fresh);
}

// ---------- utils ----------

function escapeHTML(str) {
  return (str ?? "").toString().replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function cssEscape(s) {
  return s.replace(/"/g, '\\"');
}

function stripMarkdown(s) {
  return s.replace(/[#*_`>]/g, "").replace(/\s+/g, " ").trim();
}

function renderMarkdown(s) {
  if (typeof marked !== "undefined") {
    try { return marked.parse(s); } catch { /* fall through */ }
  }
  return `<pre>${escapeHTML(s)}</pre>`;
}

// ---------- batch flow ----------

function renderBatchList() {
  const ul = $("#batch-list");
  ul.innerHTML = "";
  if (!state.batches.length) {
    const empty = document.createElement("div");
    empty.className = "subtle";
    empty.style.padding = "8px 4px";
    empty.style.fontSize = "12px";
    empty.textContent = "No baskets yet.";
    ul.appendChild(empty);
    return;
  }
  for (const b of state.batches) {
    const li = document.createElement("li");
    li.className = "session-item";
    if (b.id === state.activeBatchId) li.classList.add("active");
    li.innerHTML = `
      <div class="session-ticker">
        <span>Basket · ${b.ticker_count} tk</span>
        <span class="session-status ${b.status}">${batchStatusLabel(b.status)}</span>
      </div>
      <div class="session-date">${b.analysis_date} · ${formatRelative(b.created_at)}</div>
    `;
    li.addEventListener("click", () => openBatch(b.id));
    ul.appendChild(li);
  }
}

function batchStatusLabel(s) {
  if (s === "running")           return "● live";
  if (s === "composing_report")  return "● writing";
  if (s === "completed")         return "✓ done";
  if (s === "failed")            return "✕ failed";
  if (s === "completed_no_report") return "✓ partial";
  if (s === "cancelled")         return "⊘ cancelled";
  return "queued";
}

function openBatchConfigView() {
  if (!state.config) {
    alert("Config didn't load — check the browser console (likely the server isn't running the new code; restart `python -m web` and hard-reload).");
    return;
  }
  state.activeSessionId = null;
  state.session = null;
  state.activeBatchId = null;
  state.batch = null;
  closeWebsocket();
  closeBatchWebsocket();
  $$(".session-item").forEach((el) => el.classList.remove("active"));

  // Initialize date if empty.
  if (!$("#bf-date").value) $("#bf-date").value = new Date().toISOString().slice(0, 10);

  populateBatchProviderSelect();
  populateBatchAnalystChips();
  populateBatchLanguageSelect();
  syncBatchProviderDependentFields();
  renderUniverse();

  setView("batch-config");
}

function populateBatchProviderSelect() {
  const sel = $("#bf-provider");
  if (sel.options.length) return;
  for (const p of state.config.providers) {
    const opt = document.createElement("option");
    opt.value = p.key;
    opt.textContent = p.label;
    sel.appendChild(opt);
  }
  selectIfPresent(sel, state.config.defaults?.provider);
}

function populateBatchAnalystChips() {
  const wrap = $("#bf-analysts");
  if (wrap.children.length) return;
  for (const a of state.config.analysts) {
    const c = document.createElement("div");
    c.className = "chip active";
    c.dataset.key = a.key;
    c.textContent = a.label;
    c.addEventListener("click", () => c.classList.toggle("active"));
    wrap.appendChild(c);
  }
}

function populateBatchLanguageSelect() {
  const sel = $("#bf-language");
  if (sel.options.length) return;
  for (const lang of state.config.languages) {
    const opt = document.createElement("option");
    opt.value = lang;
    opt.textContent = lang;
    sel.appendChild(opt);
  }
}

function syncBatchProviderDependentFields() {
  const provider = $("#bf-provider").value;
  const models = state.config.models[provider] || { quick: [], deep: [] };
  fillModelSelect("#bf-quick", models.quick);
  fillModelSelect("#bf-deep", models.deep);

  const defaults = state.config.defaults || {};
  if (defaults.provider === provider) {
    selectIfPresent($("#bf-quick"), defaults.quick_model);
    selectIfPresent($("#bf-deep"),  defaults.deep_model);
  }

  const wrap = $("#bf-thinking-wrap");
  const label = $("#bf-thinking-label");
  const sel = $("#bf-thinking");
  sel.innerHTML = "";

  let opts = null;
  if (provider === "google") {
    label.textContent = "Thinking mode";
    opts = [["high","Enable Thinking (recommended)"],["minimal","Minimal / Disable"]];
  } else if (provider === "openai") {
    label.textContent = "Reasoning effort";
    opts = [["medium","Medium (default)"],["high","High (more thorough)"],["low","Low (faster)"]];
  } else if (provider === "anthropic") {
    label.textContent = "Effort level";
    opts = [["high","High (recommended)"],["medium","Medium"],["low","Low (faster)"]];
  }
  if (!opts) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  for (const [v, lbl] of opts) {
    const o = document.createElement("option");
    o.value = v; o.textContent = lbl;
    sel.appendChild(o);
  }
}

function renderUniverse() {
  const wrap = $("#bf-universe");
  wrap.innerHTML = "";
  const universe = state.config.universe || [];
  if (!universe.length) {
    wrap.innerHTML = `<div class="subtle" style="padding:18px">No instruments returned by /api/config. Restart <code>python -m web</code> so the server picks up the new universe.</div>`;
    return;
  }
  for (const cat of universe) {
    const card = document.createElement("div");
    card.className = "universe-cat";
    card.innerHTML = `
      <div class="universe-cat-head">
        <span>${escapeHTML(cat.category)}</span>
        <button type="button" class="cat-toggle">all</button>
      </div>
      <div class="universe-tickers"></div>
    `;
    const tickerWrap = card.querySelector(".universe-tickers");
    for (const tk of cat.tickers) {
      const chip = document.createElement("div");
      chip.className = "ticker-chip";
      chip.dataset.symbol = tk.symbol;
      chip.innerHTML = `<span>${escapeHTML(tk.symbol)}</span><span class="tk-name">${escapeHTML(tk.name)}</span>`;
      chip.addEventListener("click", () => {
        chip.classList.toggle("active");
        updateBatchCount();
      });
      tickerWrap.appendChild(chip);
    }
    const toggle = card.querySelector(".cat-toggle");
    toggle.addEventListener("click", () => {
      const chips = tickerWrap.querySelectorAll(".ticker-chip");
      const allOn = Array.from(chips).every((c) => c.classList.contains("active"));
      chips.forEach((c) => c.classList.toggle("active", !allOn));
      updateBatchCount();
    });
    wrap.appendChild(card);
  }
  updateBatchCount();
}

function toggleAllBatchTickers(on) {
  $$("#bf-universe .ticker-chip, #bf-custom-list .ticker-chip").forEach((c) => c.classList.toggle("active", on));
  updateBatchCount();
}

function updateBatchCount() {
  const n = $$("#bf-universe .ticker-chip.active, #bf-custom-list .ticker-chip.active").length;
  $("#bf-count").textContent = `(${n} selected)`;
}

async function submitBatch(e) {
  e.preventDefault();
  const btn = $("#batch-go-btn");
  const tickers = $$("#bf-universe .ticker-chip.active, #bf-custom-list .ticker-chip.active").map((c) => c.dataset.symbol);
  if (!tickers.length) {
    alert("Select at least one instrument.");
    return;
  }
  if (tickers.length > 30 && !confirm(`You picked ${tickers.length} instruments. This will run ${tickers.length} full multi-agent analyses sequentially. Continue?`)) {
    return;
  }
  const analysts = $$("#bf-analysts .chip.active").map((c) => c.dataset.key);
  if (!analysts.length) {
    alert("Pick at least one analyst.");
    return;
  }

  const provider = $("#bf-provider").value;
  const providerObj = state.config.providers.find((p) => p.key === provider);
  const payload = {
    tickers,
    analysis_date: $("#bf-date").value,
    llm_provider: provider,
    backend_url: providerObj?.url || null,
    quick_think_llm: $("#bf-quick").value,
    deep_think_llm: $("#bf-deep").value,
    research_depth: parseInt($("#bf-depth").value, 10),
    analysts,
    output_language: $("#bf-language").value,
    max_concurrency: parseInt($("#bf-concurrency").value, 10),
  };
  const thinking = $("#bf-thinking").value;
  if (provider === "google")    payload.google_thinking_level = thinking;
  if (provider === "openai")    payload.openai_reasoning_effort = thinking;
  if (provider === "anthropic") payload.anthropic_effort = thinking;

  btn.disabled = true;
  btn.querySelector(".go-btn-label").textContent = "Spinning up…";
  try {
    const res = await fetch("/api/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const summary = await res.json();
    await loadBatches();
    openBatch(summary.id);
  } catch (err) {
    alert(`Failed to start batch: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.querySelector(".go-btn-label").textContent = "Run basket";
  }
}

async function openBatch(id) {
  state.activeBatchId = id;
  state.activeSessionId = null;
  state.session = null;
  closeWebsocket();
  closeBatchWebsocket();
  $$(".session-item").forEach((el) => el.classList.remove("active"));

  setView("batch");
  const res = await fetch(`/api/batches/${id}`);
  if (!res.ok) {
    alert("Batch not found");
    setView("empty");
    return;
  }
  state.batch = await res.json();
  renderBatch();
  renderBatchList();
  if (["pending", "running", "composing_report"].includes(state.batch.status)) {
    openBatchWebsocket(id);
  }
}

function renderBatch() {
  const b = state.batch;
  $("#b-title").textContent = `Basket · ${b.analysis_date}`;
  $("#b-meta").textContent = `${b.config.llm_provider} · deep=${b.config.deep_think_llm} · quick=${b.config.quick_think_llm} · depth=${b.config.research_depth} · ${b.items.length} instruments`;
  const pill = $("#b-status");
  pill.textContent = batchStatusLabel(b.status);
  pill.className = `status-pill ${b.status}`;
  const cancelBtn = $("#b-cancel");
  const cancellable = ["pending", "running", "composing_report"].includes(b.status);
  cancelBtn.classList.toggle("hidden", !cancellable);
  cancelBtn.disabled = false;
  cancelBtn.textContent = "Cancel";
  renderBatchTotals();
  renderBatchItems();
  renderBatchReport();
}

function renderBatchTotals() {
  $("#b-totals").innerHTML = formatStatsLine(state.batch?.totals);
  $("#b-team-totals").innerHTML = formatTeamTotals(state.batch?.team_totals);
}

function formatTeamTotals(team_totals) {
  if (!team_totals || !Object.keys(team_totals).length) return "";
  const order = ["Analyst Team", "Research Team", "Trading Team", "Risk Management", "Portfolio Management"];
  const seen = new Set(order);
  const teams = order.filter((t) => t in team_totals).concat(
    Object.keys(team_totals).filter((t) => !seen.has(t))
  );
  const parts = [];
  for (const team of teams) {
    const t = team_totals[team] || {};
    if (!t.count) continue;
    parts.push(
      `<span class="stat">${escapeHTML(team)} ` +
      `Σ <strong>${fmtDuration(t.total_s)}</strong> ` +
      `<span style="color:var(--text-faint);font-size:11px">(avg ${fmtDuration(t.avg_s)} · max ${fmtDuration(t.max_s)} · n=${t.count})</span>` +
      `</span>`
    );
  }
  return parts.join("");
}

function renderBatchItems() {
  const b = state.batch;
  const wrap = $("#b-items");
  wrap.innerHTML = "";
  const done = b.items.filter((it) => it.status === "completed" || it.status === "failed").length;
  $("#b-counter").textContent = `${done} / ${b.items.length}`;
  for (const it of b.items) {
    const div = document.createElement("div");
    div.className = `batch-item ${it.status}`;
    const tin = Number(it.stats?.tokens_in || 0);
    const tout = Number(it.stats?.tokens_out || 0);
    const tokenLabel = (tin || tout) ? `${fmtNum(tin + tout)} tok` : "";
    div.innerHTML = `
      <span class="bi-tk">${escapeHTML(it.ticker)}</span>
      ${tokenLabel ? `<span class="bi-tokens">${tokenLabel}</span>` : ""}
      <span class="bi-status">${escapeHTML(it.status)}</span>
    `;
    if (it.session_id) {
      div.addEventListener("click", () => openSession(it.session_id));
    }
    wrap.appendChild(div);
  }
}

function renderBatchReport() {
  const b = state.batch;
  const body = $("#b-report-body");
  const tag = $("#b-report-tag");
  if (b.report) {
    body.classList.add("markdown");
    body.classList.remove("subtle");
    body.innerHTML = renderMarkdown(b.report);
    tag.textContent = "ready";
    tag.className = "final-card-tag";
    return;
  }
  body.classList.remove("markdown");
  body.classList.add("subtle");
  if (b.status === "composing_report") {
    body.textContent = "All instruments done — composing the consolidated report…";
    tag.textContent = "writing";
  } else if (b.status === "completed_no_report") {
    body.textContent = `Analyses finished but report generation failed: ${b.report_error || "unknown error"}`;
    tag.textContent = "failed";
  } else if (b.status === "failed") {
    body.textContent = `Batch failed: ${b.error || "unknown error"}`;
    tag.textContent = "failed";
  } else if (b.status === "cancelled") {
    const done = b.items.filter((it) => it.status === "completed" || it.status === "failed").length;
    body.textContent = `Basket was cancelled. ${done} of ${b.items.length} instruments completed; no consolidated report was generated.`;
    tag.textContent = "cancelled";
  } else {
    const done = b.items.filter((it) => it.status === "completed" || it.status === "failed").length;
    body.textContent = `${done} of ${b.items.length} instruments analyzed. Report appears once all are done.`;
    tag.textContent = "awaiting";
  }
}

function openBatchWebsocket(id) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/batches/${id}/stream`);
  state.batchWs = ws;
  ws.onmessage = (e) => {
    if (state.activeBatchId !== id) return;
    const event = JSON.parse(e.data);
    handleBatchEvent(event);
  };
  ws.onclose = () => {
    if (state.batchWs === ws) state.batchWs = null;
  };
}

function closeBatchWebsocket() {
  if (state.batchWs) {
    try { state.batchWs.close(); } catch {}
    state.batchWs = null;
  }
}

// ---------- sidebar collapsible sections ----------

const SIDEBAR_PREFS_KEY = "ta-sidebar-collapsed";

function initSidebarSections() {
  let collapsed = {};
  try {
    collapsed = JSON.parse(localStorage.getItem(SIDEBAR_PREFS_KEY) || "{}") || {};
  } catch { collapsed = {}; }

  for (const section of $$(".sidebar-section")) {
    const key = section.dataset.section;
    if (collapsed[key]) section.classList.add("collapsed");
    const toggle = section.querySelector(".section-toggle");
    toggle.addEventListener("click", () => {
      section.classList.toggle("collapsed");
      collapsed[key] = section.classList.contains("collapsed");
      try { localStorage.setItem(SIDEBAR_PREFS_KEY, JSON.stringify(collapsed)); } catch {}
    });
  }
}

// ---------- portfolio editor ----------

async function openPortfolioView() {
  state.activeSessionId = null;
  state.session = null;
  state.activeBatchId = null;
  state.batch = null;
  closeWebsocket();
  closeBatchWebsocket();
  $$(".session-item").forEach((el) => el.classList.remove("active"));

  setView("portfolio");
  await loadPortfolio();
}

async function loadPortfolio() {
  const tbody = $("#pf-rows");
  tbody.innerHTML = "";
  let positions = {};
  try {
    const res = await fetch("/api/portfolio");
    if (res.ok) {
      const data = await res.json();
      positions = data.positions || {};
    }
  } catch (e) {
    console.error("loadPortfolio failed:", e);
  }
  const entries = Object.entries(positions);
  if (!entries.length) {
    addPortfolioRow();
  } else {
    for (const [sym, pos] of entries) {
      addPortfolioRow({ symbol: sym, qty: pos.qty, avg_cost: pos.avg_cost, notes: pos.notes });
    }
  }
  updatePortfolioCount();
}

function addPortfolioRow(seed = {}) {
  const tbody = $("#pf-rows");
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td><input class="pf-symbol" placeholder="AAPL" value="${escapeAttr(seed.symbol)}" /></td>
    <td><input class="pf-qty" type="number" step="any" placeholder="100" value="${escapeAttr(seed.qty)}" /></td>
    <td><input class="pf-cost" type="number" step="any" placeholder="180.50" value="${escapeAttr(seed.avg_cost)}" /></td>
    <td><input class="pf-notes" placeholder="core holding, long-term" value="${escapeAttr(seed.notes)}" /></td>
    <td><button type="button" class="pf-del" aria-label="Remove row">×</button></td>
  `;
  tr.querySelector(".pf-del").addEventListener("click", () => {
    tr.remove();
    updatePortfolioCount();
  });
  for (const inp of tr.querySelectorAll("input")) {
    inp.addEventListener("input", updatePortfolioCount);
  }
  tbody.appendChild(tr);
  updatePortfolioCount();
}

function escapeAttr(v) {
  if (v === null || v === undefined || v === "") return "";
  return String(v).replace(/"/g, "&quot;");
}

function updatePortfolioCount() {
  const rows = $$("#pf-rows tr");
  const filled = rows.filter((r) => {
    const sym = r.querySelector(".pf-symbol").value.trim();
    const qty = r.querySelector(".pf-qty").value.trim();
    return sym && qty && Number(qty) !== 0;
  });
  $("#pf-count").textContent = `${filled.length} position${filled.length === 1 ? "" : "s"}`;
}

async function savePortfolio() {
  const rows = $$("#pf-rows tr");
  const positions = {};
  for (const r of rows) {
    const sym = r.querySelector(".pf-symbol").value.trim().toUpperCase();
    const qtyRaw = r.querySelector(".pf-qty").value.trim();
    if (!sym || !qtyRaw) continue;
    const qty = Number(qtyRaw);
    if (!Number.isFinite(qty) || qty === 0) continue;
    const entry = { qty };
    const costRaw = r.querySelector(".pf-cost").value.trim();
    if (costRaw) {
      const c = Number(costRaw);
      if (Number.isFinite(c)) entry.avg_cost = c;
    }
    const notes = r.querySelector(".pf-notes").value.trim();
    if (notes) entry.notes = notes;
    positions[sym] = entry;
  }

  const btn = $("#pf-save");
  btn.disabled = true;
  btn.querySelector(".go-btn-label").textContent = "Saving…";
  try {
    const res = await fetch("/api/portfolio", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ positions }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    btn.querySelector(".go-btn-label").textContent = "Saved";
    setTimeout(() => { btn.querySelector(".go-btn-label").textContent = "Save"; btn.disabled = false; }, 1200);
  } catch (err) {
    alert(`Failed to save portfolio: ${err.message}`);
    btn.querySelector(".go-btn-label").textContent = "Save";
    btn.disabled = false;
  }
}

function handleBatchEvent(event) {
  if (!state.batch) return;
  if (event.type === "batch") {
    state.batch = event.batch;
    renderBatch();
    loadBatches();
    if (["completed", "failed", "completed_no_report", "cancelled"].includes(event.batch.status)) {
      closeBatchWebsocket();
    }
    return;
  }
  if (event.type === "item") {
    state.batch.items[event.index] = event.item;
    renderBatchItems();
    renderBatchReport();
    return;
  }
  if (event.type === "totals") {
    state.batch.totals = event.totals;
    if (event.team_totals) state.batch.team_totals = event.team_totals;
    renderBatchTotals();
    return;
  }
}

// =====================================================================
// Mobile nav drawer
// =====================================================================

function initMobileNav() {
  const toggle = $("#nav-toggle");
  const sidebar = $("#sidebar");
  const backdrop = $("#sidebar-backdrop");
  if (!toggle || !sidebar || !backdrop) return;

  function open() {
    sidebar.classList.add("open");
    backdrop.classList.add("visible");
    backdrop.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    toggle.setAttribute("aria-label", "Close navigation");
  }
  function close() {
    sidebar.classList.remove("open");
    backdrop.classList.remove("visible");
    // Wait for the fade-out so the layer doesn't disappear mid-transition.
    setTimeout(() => { backdrop.hidden = true; }, 220);
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "Open navigation");
  }

  toggle.addEventListener("click", () => {
    sidebar.classList.contains("open") ? close() : open();
  });
  backdrop.addEventListener("click", close);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && sidebar.classList.contains("open")) close();
  });

  // Auto-close after the user taps anything that switches the main view, so
  // they're not left looking at the drawer covering their just-opened content.
  ["#new-session-btn", "#new-batch-btn", "#portfolio-btn"].forEach((sel) => {
    const el = $(sel);
    if (el) el.addEventListener("click", close);
  });
  // Session/batch list items use delegation so newly-rendered rows still close.
  document.addEventListener("click", (e) => {
    if (!sidebar.classList.contains("open")) return;
    const item = e.target.closest?.(".session-item");
    if (item && sidebar.contains(item)) close();
  });

  // Resizing past the desktop breakpoint resets state so the drawer doesn't
  // get stuck closed via CSS while the JS still thinks it's open.
  const mq = window.matchMedia("(min-width: 901px)");
  const resync = () => {
    if (mq.matches) {
      backdrop.classList.remove("visible");
      backdrop.hidden = true;
    }
  };
  mq.addEventListener?.("change", resync);
  resync();
}

// =====================================================================
// Welcome flow, auth, tiers, daily quota gating, custom-ticker support.
// =====================================================================

const TIER_LABEL = { novice: "Novice", intermediate: "Intermediate", master: "Master" };

const userState = {
  current: null,        // { uid, displayName, email, photoURL, tier, isGuest }
  usageToday: 0,
  customTickers: [],    // [{ symbol, name }]
};

function getAuth() {
  return window.AgenticWhalesAuth || null;
}

// Compliance gate for /analyze. The accept modal lives on /fund, so a
// signed-in user who hasn't attested to the active disclaimer version is sent
// there to accept (which also lands them on the product surface). Checked once
// per page load.
let _complianceChecked = false;
async function ensureComplianceOrRedirect() {
  if (_complianceChecked) return;
  _complianceChecked = true;
  try {
    const r = await fetch("/api/audit/compliance-ack");
    if (!r.ok) return;
    const data = await r.json();
    if (data.needs_attestation) window.location.replace("/fund");
  } catch (e) {
    console.warn("compliance check failed:", e);
  }
}

// Loop guard for the /analyze ↔ / auth bounce — stop after 2 redirects in 10s
// so a transient null-user emission during session restore can't ping-pong.
function _allowAnalyzeRedirect(tag) {
  const key = `aw_redirect_${tag}`;
  const now = Date.now();
  let hist = [];
  try { hist = JSON.parse(sessionStorage.getItem(key) || "[]"); } catch (_) {}
  hist = hist.filter((t) => now - t < 10000);
  if (hist.length >= 2) {
    console.warn(`auth-redirect guard tripped for ${tag}; staying put.`);
    return false;
  }
  hist.push(now);
  try { sessionStorage.setItem(key, JSON.stringify(hist)); } catch (_) {}
  return true;
}

function initWelcomeFlow() {
  const start = () => {
    const auth = getAuth();
    if (!auth) {
      // Module hasn't loaded yet — wait for the ready event then retry once.
      window.addEventListener("agenticwhales-auth-ready", initWelcomeFlow, { once: true });
      return;
    }
    initHowItWorksCarousel();
    initWelcomeModalControls();
    initLegalModalControls();
    initSignOutButton();
    auth.onChange((u) => {
      const prev = userState.current;
      userState.current = u;
      reflectUserChip();
      if (u) {
        // Hide welcome if shown
        const w = $("#welcome-modal");
        if (w && !w.classList.contains("hidden")) w.classList.add("hidden");
        refreshUsageBadge();
        // Compliance gate: every signed-in user must hold a current
        // attestation. The accept UI is centralized on /fund, so an
        // un-attested user is sent there to accept (and lands on the product).
        // Guests (local dev, no Supabase) are exempt.
        if (auth.isConfigured && !u.isGuest) ensureComplianceOrRedirect();
      } else if (auth.isConfigured) {
        // Login required — bounce signed-out visitors to the / landing gate.
        if (_allowAnalyzeRedirect("analyze_to_root")) window.location.replace("/");
      } else {
        showWelcomeModal();
      }
      // Reload sidebar listings whenever the active user changes (sign-in,
      // sign-out, or switch). Each user only sees their own runs.
      if ((prev?.uid || null) !== (u?.uid || null)) {
        reloadOnAuthChange();
      }
    });
  };

  // Also wire the "How does AgenticWhales work?" link to reopen the carousel
  // section as a quick refresher (without forcing a re-sign-in).
  const showHow = $("#show-howitworks");
  if (showHow) showHow.addEventListener("click", () => showWelcomeModal({ refresher: true }));

  start();
}

// ---------- "How it works" rotating cards ----------
let howAutoTimer = null;

function initHowItWorksCarousel() {
  const track = $("#how-track");
  const dotsWrap = $("#how-dots");
  if (!track || !dotsWrap) return;
  const cards = Array.from(track.querySelectorAll(".how-card"));
  dotsWrap.innerHTML = "";
  cards.forEach((_, i) => {
    const dot = document.createElement("button");
    dot.type = "button";
    dot.className = "how-dot" + (i === 0 ? " active" : "");
    dot.dataset.idx = String(i);
    dot.addEventListener("click", () => goToCard(i, true));
    dotsWrap.appendChild(dot);
  });
  $("#how-prev").onclick = () => goToCard(currentCardIdx() - 1, true);
  $("#how-next").onclick = () => goToCard(currentCardIdx() + 1, true);
  goToCard(0, false);
  scheduleHowAutoplay();
}

function currentCardIdx() {
  const active = $("#how-track .how-card.active");
  if (!active) return 0;
  return Number(active.dataset.step || 1) - 1;
}

function goToCard(idx, manual) {
  const track = $("#how-track");
  if (!track) return;
  const cards = Array.from(track.querySelectorAll(".how-card"));
  const dots = Array.from($("#how-dots").querySelectorAll(".how-dot"));
  const n = cards.length;
  const i = ((idx % n) + n) % n;
  cards.forEach((c, k) => c.classList.toggle("active", k === i));
  dots.forEach((d, k) => d.classList.toggle("active", k === i));
  if (manual) scheduleHowAutoplay();
}

function scheduleHowAutoplay() {
  if (howAutoTimer) clearInterval(howAutoTimer);
  howAutoTimer = setInterval(() => goToCard(currentCardIdx() + 1, false), 5500);
}

function stopHowAutoplay() {
  if (howAutoTimer) clearInterval(howAutoTimer);
  howAutoTimer = null;
}

// ---------- Welcome modal logic ----------

function showWelcomeModal(opts = {}) {
  const auth = getAuth();
  const modal = $("#welcome-modal");
  if (!modal) return;

  // Surface the "Supabase not configured" notice when relevant, and disable
  // the Google button so users don't get a confusing error.
  const hint = $("#welcome-firebase-hint");
  const googleBtn = $("#welcome-google");
  if (auth && !auth.isConfigured) {
    if (hint) hint.hidden = false;
    if (googleBtn) googleBtn.disabled = true;
  } else if (auth) {
    if (hint) hint.hidden = true;
  }

  // Refresher mode: user is already signed in — let them dismiss easily.
  modal.classList.toggle("refresher", !!opts.refresher);

  modal.classList.remove("hidden");
  scheduleHowAutoplay();
  // Focus the consent checkbox so the user can tab straight into the buttons.
  setTimeout(() => $("#welcome-agree")?.focus(), 50);
}

function hideWelcomeModal() {
  const modal = $("#welcome-modal");
  if (modal) modal.classList.add("hidden");
  stopHowAutoplay();
}

function initWelcomeModalControls() {
  const agree = $("#welcome-agree");
  const googleBtn = $("#welcome-google");
  const auth = getAuth();

  function updateButtons() {
    const ok = !!agree.checked;
    if (auth?.isConfigured) googleBtn.disabled = !ok;
  }
  agree.addEventListener("change", updateButtons);
  updateButtons();

  googleBtn.addEventListener("click", async () => {
    if (!auth?.isConfigured) return;
    googleBtn.disabled = true;
    try {
      // Display name comes from the user's Google profile (full_name in the
      // auth user_metadata), so we don't need a local input.
      await auth.signInWithGoogle();
      hideWelcomeModal();
    } catch (err) {
      console.error("Google sign-in failed:", err);
      alert(`Sign-in failed: ${err.message || err}`);
      googleBtn.disabled = false;
    }
  });
}

function initLegalModalControls() {
  document.addEventListener("click", (e) => {
    const open = e.target.closest?.("[data-open]");
    if (open) {
      const which = open.dataset.open;
      const id = which === "privacy" ? "privacy-modal" : which === "terms" ? "terms-modal" : null;
      if (id) document.getElementById(id)?.classList.remove("hidden");
      return;
    }
    const close = e.target.closest?.("[data-close-modal]");
    if (close) {
      const id = close.dataset.closeModal;
      document.getElementById(id)?.classList.add("hidden");
      return;
    }
    // Click on backdrop dismisses
    for (const id of ["privacy-modal", "terms-modal", "upgrade-modal", "welcome-modal"]) {
      if (e.target.id === id) {
        const modal = document.getElementById(id);
        // Welcome modal can only close itself when the user is signed in
        // (otherwise the gate would be bypassable by clicking outside).
        if (id === "welcome-modal") {
          if (userState.current) modal.classList.add("hidden");
        } else {
          modal.classList.add("hidden");
        }
      }
    }
  });
}

function initSignOutButton() {
  const btn = $("#signout-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const auth = getAuth();
    if (!auth) return;
    try { await auth.signOut(); } catch (e) { console.error(e); }
  });
}

// ---------- User chip + usage badge ----------

function reflectUserChip() {
  const chip = $("#user-chip");
  const u = userState.current;
  if (!chip) return;
  if (!u) {
    chip.classList.add("hidden");
    return;
  }
  chip.classList.remove("hidden");
  $("#user-name").textContent = u.displayName || "Trader";
  const av = $("#user-avatar");
  // Always render initials first (covers slow loads, CORS-blocked Google
  // avatars, no-photo accounts, and guest users). If photoURL preloads
  // successfully, swap in the image background; the text falls through.
  const initials = (u.displayName || "?")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p.charAt(0).toUpperCase())
    .join("") || "?";
  av.textContent = initials;
  av.style.backgroundImage = "";
  if (u.photoURL) {
    const probe = new Image();
    probe.onload = () => {
      // Re-check: the user could have signed out before the image loaded.
      if (userState.current?.photoURL === u.photoURL) {
        av.style.backgroundImage = `url("${u.photoURL}")`;
        av.textContent = "";
      }
    };
    probe.src = u.photoURL;
  }
  const badge = $("#user-tier-badge");
  badge.textContent = TIER_LABEL[u.tier] || "Novice";
  badge.className = `tier-badge ${u.tier || "novice"}`;
  refreshUsageBadge();
  refreshAdminNav();
}

// Server-side gate is the security boundary (require_admin in web/auth.py).
// This is purely a UX call: hide the /usage link for everyone else so the
// dashboard isn't a discoverable surface.
async function refreshAdminNav() {
  const link = $("#usage-link");
  if (!link) return;
  const u = userState.current;
  if (!u || u.isGuest) {
    link.classList.add("hidden");
    return;
  }
  try {
    const res = await fetch("/api/usage/me");
    link.classList.toggle("hidden", !res.ok);
  } catch {
    link.classList.add("hidden");
  }
}

async function refreshUsageBadge() {
  const auth = getAuth();
  const u = userState.current;
  const el = $("#user-usage");
  if (!el || !u) return;
  let used = 0;
  try { used = await auth.getUsageToday(); } catch (e) { used = userState.usageToday; }
  userState.usageToday = used;
  const limit = auth.dailyLimitFor(u.tier || "novice");
  if (limit === Infinity) {
    el.textContent = `${used} today · unlimited`;
  } else {
    el.textContent = `${used} / ${limit} today`;
    el.classList.toggle("at-limit", used >= limit);
  }
}

// ---------- Quota gate around analysis-creation entry points ----------

function guardCreate(fn) {
  return async (...args) => {
    const auth = getAuth();
    const u = userState.current;
    if (!auth || !u) {
      // Not signed in yet — push the welcome modal forward.
      showWelcomeModal();
      return;
    }
    const limit = auth.dailyLimitFor(u.tier || "novice");
    let used;
    try { used = await auth.getUsageToday(); } catch { used = userState.usageToday; }
    userState.usageToday = used;
    if (used >= limit) {
      $("#upgrade-modal").classList.remove("hidden");
      refreshUsageBadge();
      return;
    }
    fn.apply(null, args);
  };
}

// Patched fetch: every /api/* call gets `Authorization: Bearer <jwt>`
// attached so the server can scope sessions/batches by user. We also keep
// the post-success hook for usage-counter increments.
const _origFetch = window.fetch.bind(window);
window.fetch = async function patchedFetch(input, init) {
  const url = typeof input === "string" ? input : (input?.url || "");
  const isApi = url.startsWith("/api/") || url.includes(`${location.host}/api/`);
  if (isApi) {
    const auth = getAuth();
    const token = auth?.getAccessToken?.();
    if (token) {
      init = init ? { ...init } : {};
      const h = new Headers(init.headers || (typeof input !== "string" ? input.headers : undefined));
      if (!h.has("Authorization")) h.set("Authorization", `Bearer ${token}`);
      init.headers = h;
    }
  }
  const res = await _origFetch(input, init);
  try {
    const method = (init?.method || (typeof input !== "string" ? input.method : "GET") || "GET").toUpperCase();
    if (res.ok && method === "POST" && (url.endsWith("/api/sessions") || url.endsWith("/api/batches"))) {
      // Clone so the original caller can still parse the body normally.
      let cached = false;
      try {
        const peek = await res.clone().json();
        cached = !!peek?.cached;
      } catch {}
      if (cached) {
        console.info("AgenticWhales: cache hit — reused recent analysis, quota unchanged");
      }
      const auth = getAuth();
      if (!cached && auth && userState.current) {
        try {
          // For batches, count each ticker in the basket toward the quota so
          // a Novice can't bypass the cap by submitting a 50-ticker basket.
          let increments = 1;
          if (url.endsWith("/api/batches") && init?.body) {
            try {
              const body = JSON.parse(init.body);
              if (Array.isArray(body.tickers)) increments = Math.max(1, body.tickers.length);
            } catch {}
          }
          for (let i = 0; i < increments; i++) {
            await auth.incrementUsage();
          }
          refreshUsageBadge();
        } catch (e) {
          console.warn("usage increment failed:", e);
        }
      }
    }
  } catch {}
  return res;
};

// Browsers can't set headers on WebSocket connects, so the server reads the
// token from a `?token=...` query param. Patch WebSocket to inject it.
const _OrigWebSocket = window.WebSocket;
window.WebSocket = function PatchedWebSocket(url, protocols) {
  try {
    const u = new URL(url, location.href);
    if (u.pathname.startsWith("/api/") && !u.searchParams.has("token")) {
      const auth = getAuth();
      const token = auth?.getAccessToken?.();
      if (token) u.searchParams.set("token", token);
      url = u.toString();
    }
  } catch {}
  return protocols !== undefined ? new _OrigWebSocket(url, protocols) : new _OrigWebSocket(url);
};
window.WebSocket.prototype = _OrigWebSocket.prototype;
window.WebSocket.CONNECTING = _OrigWebSocket.CONNECTING;
window.WebSocket.OPEN = _OrigWebSocket.OPEN;
window.WebSocket.CLOSING = _OrigWebSocket.CLOSING;
window.WebSocket.CLOSED = _OrigWebSocket.CLOSED;

// ---------- Custom ticker (basket analysis) ----------

function addCustomTicker() {
  const inp = $("#bf-custom-ticker");
  const msg = $("#bf-custom-msg");
  const raw = (inp.value || "").trim();
  if (!raw) return;
  // Lightweight validation: alnum, dot, dash, caret. Don't try to be too strict
  // — yfinance accepts a wide range of tickers (BRK-B, BTC-USD, 0700.HK, ^GSPC).
  if (!/^[A-Za-z0-9.\-^]{1,20}$/.test(raw)) {
    msg.textContent = "Use letters, digits, '.', '-' or '^' (max 20 chars).";
    msg.classList.add("error");
    return;
  }
  const symbol = raw.toUpperCase();
  // De-dupe against the universe AND existing custom tickers.
  const inUniverse = (state.config?.universe || []).some((cat) =>
    cat.tickers.some((t) => t.symbol.toUpperCase() === symbol)
  );
  if (inUniverse) {
    msg.textContent = `${symbol} is already in the basket — just click it below.`;
    msg.classList.remove("error");
    inp.value = "";
    return;
  }
  if (userState.customTickers.some((t) => t.symbol === symbol)) {
    msg.textContent = `${symbol} is already added.`;
    msg.classList.remove("error");
    return;
  }
  userState.customTickers.push({ symbol, name: "custom" });
  inp.value = "";
  msg.textContent = "";
  msg.classList.remove("error");
  renderCustomTickerList();
}

function renderCustomTickerList() {
  const wrap = $("#bf-custom-list");
  if (!wrap) return;
  wrap.innerHTML = "";
  if (!userState.customTickers.length) {
    wrap.classList.add("hidden");
    return;
  }
  wrap.classList.remove("hidden");
  for (const t of userState.customTickers) {
    const chip = document.createElement("div");
    chip.className = "ticker-chip custom-ticker active";
    chip.dataset.symbol = t.symbol;
    chip.innerHTML = `
      <span>${escapeHTML(t.symbol)}</span>
      <span class="tk-name">custom</span>
      <button type="button" class="custom-remove" aria-label="Remove ${escapeHTML(t.symbol)}">×</button>
    `;
    chip.addEventListener("click", (e) => {
      // Toggling the chip selects/deselects it for the basket, except when
      // the click landed on the remove button.
      if (e.target.closest(".custom-remove")) return;
      chip.classList.toggle("active");
      updateBatchCount();
    });
    chip.querySelector(".custom-remove").addEventListener("click", (e) => {
      e.stopPropagation();
      userState.customTickers = userState.customTickers.filter((x) => x.symbol !== t.symbol);
      renderCustomTickerList();
      updateBatchCount();
    });
    wrap.appendChild(chip);
  }
  updateBatchCount();
}
