// AgenticWhales — Fund SPA (Phase 1 surface, separate from /).
//
// This page is the autonomy + risk + paper-trade dashboard. The home page
// (/) remains the research / debate / batch surface and is untouched by
// Phase 1. Both pages share styles.css; this page also loads fund.css.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// Patched fetch: every /api/* call gets `Authorization: Bearer <jwt>` so the
// server can scope by user. Mirrors the interceptor in app.js — /fund needs
// its own because each page loads a different bundle.
// Also handles compliance 412 globally: when the server says the user needs
// to re-ack the disclaimer (because the version was bumped or they never
// acked at all), we pop the modal instead of letting the raw error bubble.
const _origFetch = window.fetch.bind(window);
window.fetch = async function fundPatchedFetch(input, init) {
  const url = typeof input === "string" ? input : (input?.url || "");
  const isApi = url.startsWith("/api/") || url.includes(`${location.host}/api/`);
  if (isApi) {
    const token = window.AgenticWhalesAuth?.getAccessToken?.();
    if (token) {
      init = init ? { ...init } : {};
      const h = new Headers(
        init.headers || (typeof input !== "string" ? input.headers : undefined)
      );
      if (!h.has("Authorization")) h.set("Authorization", `Bearer ${token}`);
      init.headers = h;
    }
  }
  const res = await _origFetch(input, init);
  if (isApi && res.status === 412) {
    // 412 = compliance precondition failed. Peek at the body without
    // consuming the response (caller still gets to read it normally).
    try {
      const peek = await res.clone().json();
      const code = peek?.detail?.code || peek?.code;
      if (code === "compliance_required") {
        if (typeof showComplianceModal === "function") {
          showComplianceModal({ reason: "412", payload: peek?.detail || peek });
        }
      }
    } catch (_) { /* not JSON; let caller handle */ }
  }
  return res;
};

const state = {
  config: null,
  paper: null,
  positions: [],
  orders: [],
  events: [],
  recipes: [],
  journal: [],
  sessions: [],
  limits: null,
  spendToday: 0,
  spendCap: 5.0,
  brier: null,
  activity: [],
  view: "overview",
  showDrafts: false,
};

// ------------------------------------------------------------------
// Bootstrap
// ------------------------------------------------------------------

window.addEventListener("DOMContentLoaded", async () => {
  // Sidebar nav.
  $$(".fund-nav-btn").forEach((b) => {
    b.addEventListener("click", () => switchView(b.dataset.target));
  });
  document.body.addEventListener("click", (e) => {
    const a = e.target.closest("[data-target]");
    if (a && a.tagName === "A") {
      e.preventDefault();
      switchView(a.dataset.target);
    }
  });
  // Subtab bar (Fund / Signals / Risk groups).
  $("#fund-subtabs")?.addEventListener("click", (e) => {
    const b = e.target.closest(".fund-subtab");
    if (b) {
      _lastLeaf[state.view] = b.dataset.leaf;
      showLeaf(b.dataset.leaf);
    }
  });

  // Refresh buttons.
  $("#ov-refresh").addEventListener("click", refreshAll);
  $("#ov-resolve").addEventListener("click", resolveOutcomes);
  $$("[data-refresh]").forEach((b) => b.addEventListener("click", refreshAll));

  // Recipe form.
  $("#recipe-form").addEventListener("submit", submitRecipe);
  $("#r-refresh").addEventListener("click", () => loadRecipes().then(render));
  $("#rf-provider").addEventListener("change", syncRecipeProviderFields);
  $("#rf-bull").addEventListener("change", maybeAutoPickBear);

  // Backtest (Phase 3).
  $("#backtest-form")?.addEventListener("submit", submitBacktest);

  // Strategy Lab — NL thesis → compiled rules → backtest.
  $("#strategy-form")?.addEventListener("submit", submitStrategy);

  // Streaming fires panel (Phase 3).
  $("#streaming-refresh")?.addEventListener("click", refreshStreamingEvents);

  // Thesis form Advanced disclosure (onboarding fix).
  $("#rf-advanced-toggle")?.addEventListener("click", toggleAdvancedThesisForm);
  initAdvancedThesisForm();

  // Overview hero — live multi-agent debate launcher.
  $("#hero-start-btn")?.addEventListener("click", startHeroSession);
  $("#hero-cancel-btn")?.addEventListener("click", cancelHeroSession);
  $("#hero-another-btn")?.addEventListener("click", resetHeroToIdle);
  $("#hero-schedule-btn")?.addEventListener("click", openScheduleModal);
  initScheduleModalControls();
  $("#hero-ticker")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); startHeroSession(); }
  });

  // Risk controls.
  $("#kill-toggle").addEventListener("change", toggleKillSwitch);
  $("#rl-save").addEventListener("click", saveRiskLimits);
  $("#bc-toggle")?.addEventListener("change", toggleBehavioralCooldown);

  // Behavioral findings re-scan button.
  $("#bf-rescan")?.addEventListener("click", rescanBehavioral);

  // Journal (Phase 2).
  $("#je-form").addEventListener("submit", submitJournalEntry);
  $("#je-refresh").addEventListener("click", () => loadJournal().then(renderJournal));
  $("#je-show-drafts").addEventListener("change", (e) => {
    state.showDrafts = e.target.checked;
    renderJournal();
  });

  // Ask the fund (Phase 2 deliverable #2). Two clones of the same widget —
  // one on Overview, one on Journal. Both populate from the same template
  // list and share the same render path.
  loadAskTemplates();
  $("#ask-clear")?.addEventListener("click", () => clearAskAnswer(""));
  document.body.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-clear-ask]");
    if (btn) clearAskAnswer("-" + btn.dataset.clearAsk);
  });

  // Sign-out (sidebar chip). After Supabase clears the session we send the
  // user back to / so the sign-in gate is the next thing they see.
  $("#signout-btn")?.addEventListener("click", async () => {
    try { await window.AgenticWhalesAuth?.signOut?.(); } catch (e) { console.error(e); }
    window.location.href = "/";
  });

  // Auth → load. supabase-client.js is a module with top-level await, so on
  // a cold load it may not have installed `window.AgenticWhalesAuth` by the
  // time DOMContentLoaded fires here. Wait for it before kicking off ANY
  // /api call — otherwise the patched fetch attaches no bearer token and the
  // server 401s every request (including the compliance gate's GET, which
  // used to fire before the await and 401 on every visit).
  await whenAuthReady();
  // Compliance attestation gate (paper-only acknowledgment + jurisdiction).
  initComplianceGate();
  await loadConfig();
  populateRecipeFormSelects();
  await refreshAll();
});

// Resolves once `window.AgenticWhalesAuth` is installed AND (if configured)
// the access token is available. Bounded at 3s so a hung auth boot can't
// block the page indefinitely; after that we proceed anonymously.
// Shared loop guard for the /fund ↔ / auth bounce. Returns true if a redirect
// is allowed; false if we've already redirected twice within 10s on this tag
// (which means the two pages are ping-ponging us due to a session race).
function _allowAuthRedirect(tag) {
  const key = `aw_redirect_${tag}`;
  const now = Date.now();
  let hist = [];
  try { hist = JSON.parse(sessionStorage.getItem(key) || "[]"); } catch (_) {}
  hist = hist.filter((t) => now - t < 10_000);
  if (hist.length >= 2) {
    console.warn(`auth-redirect guard tripped for ${tag} — bounced ${hist.length}× in 10s; staying put.`);
    return false;
  }
  hist.push(now);
  try { sessionStorage.setItem(key, JSON.stringify(hist)); } catch (_) {}
  return true;
}

function whenAuthReady() {
  return new Promise((resolve) => {
    const deadline = Date.now() + 3000;
    const settled = () => {
      const auth = window.AgenticWhalesAuth;
      if (!auth) return false;
      if (!auth.isConfigured) return true;          // guest mode — no token needed
      return !!auth.getAccessToken?.();             // signed in — token in hand
    };
    if (settled()) return resolve();
    let timer;
    const tick = () => {
      if (settled() || Date.now() >= deadline) {
        clearInterval(timer);
        resolve();
      }
    };
    // The auth client dispatches this once `window.AgenticWhalesAuth` is set.
    window.addEventListener("agenticwhales-auth-ready", tick, { once: false });
    // Belt-and-braces poll for the post-ready session-restore step where
    // AgenticWhalesAuth exists but the token hasn't been hydrated yet.
    timer = setInterval(tick, 50);
  });
}

// ------------------------------------------------------------------
// Compliance attestation — server-driven gate
// ------------------------------------------------------------------
//
// Behavior:
//   1. On boot, GET /api/audit/compliance-ack. The server tells us whether
//      the user has a valid attestation matching the currently-active
//      disclaimer version. If yes → modal stays hidden.
//   2. If no (new user OR the disclaimer version was bumped server-side) →
//      modal pops with the rendered legal-doc summaries and a checkbox.
//   3. The fetch interceptor at the top of this file also calls
//      `showComplianceModal()` if any API call returns 412 with code
//      `compliance_required` — so a user who somehow got past the gate
//      (race, stale tab) sees the popup the moment they take an action
//      that requires attestation.
//
// localStorage is NOT the source of truth — the server is. We do not cache
// "previously accepted" on the client because that's exactly the bug that
// kept the broken disclaimer flow alive: localStorage said "accepted" while
// the server said "no valid attestation for this version".

let _COMPLIANCE_DOCS = null;  // cached payload from GET; refreshed on open

async function initComplianceGate() {
  try {
    const r = await fetch("/api/audit/compliance-ack");
    if (!r.ok) return;  // unauthenticated GET in dev — fall through, no gate
    const data = await r.json();
    _COMPLIANCE_DOCS = data;
    if (data.needs_attestation) {
      showComplianceModal({ reason: "boot", payload: data });
    }
  } catch (e) {
    // Network errors don't block the user, but log so debug is possible.
    console.warn("compliance gate check failed:", e);
  }
}

function showComplianceModal({ reason, payload } = {}) {
  const modal = document.getElementById("compliance-modal");
  if (!modal) return;
  if (!modal.classList.contains("hidden")) return;  // already open

  // Refresh the inline content from whatever the server most recently sent.
  populateComplianceModalDocs(_COMPLIANCE_DOCS || payload || {});

  const check = document.getElementById("compliance-confirm-check");
  const accept = document.getElementById("compliance-accept");
  const decline = document.getElementById("compliance-decline");
  const errBox = document.getElementById("compliance-error");
  if (!check || !accept || !decline) return;

  // Reset state every time the modal re-opens.
  check.checked = false;
  accept.disabled = true;
  if (errBox) errBox.textContent = "";
  modal.classList.remove("hidden");

  const onChange = () => { accept.disabled = !check.checked; };
  check.addEventListener("change", onChange);

  const onAccept = async () => {
    accept.disabled = true;
    if (errBox) errBox.textContent = "";
    const version = (_COMPLIANCE_DOCS && _COMPLIANCE_DOCS.version) || "v1.0";
    try {
      const res = await fetch("/api/audit/compliance-ack", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          version,
          ack_paper_only: true,
          ack_not_advice: true,
          ack_jurisdiction: true,
        }),
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        const msg = (j && (j.detail?.message || j.detail || j.message)) || `HTTP ${res.status}`;
        if (errBox) errBox.textContent = `Could not record acknowledgement: ${msg}`;
        accept.disabled = false;
        return;
      }
      modal.classList.add("hidden");
      check.removeEventListener("change", onChange);
      accept.removeEventListener("click", onAccept);
      decline.removeEventListener("click", onDecline);
    } catch (e) {
      if (errBox) errBox.textContent = `Network error: ${e.message || e}`;
      accept.disabled = false;
    }
  };

  const onDecline = () => {
    // Compliance is mandatory — declining boots them back to the landing.
    window.location.href = "/";
  };

  accept.addEventListener("click", onAccept);
  decline.addEventListener("click", onDecline);
}

function populateComplianceModalDocs(data) {
  const docs = data?.docs || {};
  const target = document.getElementById("compliance-docs");
  if (!target) return;
  const entries = ["disclaimer", "privacy", "terms"];
  target.innerHTML = entries.map((k) => {
    const d = docs[k];
    if (!d) return "";
    return `
      <article class="compliance-doc">
        <h3>${escapeHTML(d.title)}</h3>
        <p>${escapeHTML(d.body)}</p>
      </article>`;
  }).join("");
  const ver = data?.version ? `v${data.version.replace(/^v/, "")}` : "";
  const verLabel = document.getElementById("compliance-version");
  if (verLabel && ver) verLabel.textContent = `Version ${ver}`;
}

// (Legacy ad-hoc form removed in the well-lit-path revamp — the hero on
//  Overview is the canonical session-launcher now. Power users still get
//  the full configurator at /analyze.)

window.addEventListener("agenticwhales-auth-ready", () => {
  setupAuth();
});
// TIER_LABEL must be initialized BEFORE setupAuth() runs — auth.onChange()
// fires its callback synchronously when a cached user is present, and that
// callback reads TIER_LABEL. If it's still in TDZ (decl below the call), the
// whole script aborts here, silently breaking every later feature.
const TIER_LABEL = { novice: "Novice", intermediate: "Intermediate", master: "Master" };

if (window.AgenticWhalesAuth) setupAuth();

function setupAuth() {
  const auth = window.AgenticWhalesAuth;
  if (!auth) return;
  // Sidebar sign-in button — always visible when signed out. When Supabase is
  // configured it runs the Google OAuth flow; in local/guest mode (no Supabase)
  // it falls back to a local guest session so the control is never dead and the
  // user chip always has a way to appear.
  const signinBtn = $("#fund-signin-btn");
  // Relabel for guest mode so the button is honest about what it does.
  if (signinBtn && !auth.isConfigured) {
    const lbl = signinBtn.querySelector("span:last-child");
    if (lbl) lbl.textContent = "Continue as guest";
  }
  signinBtn?.addEventListener("click", async () => {
    signinBtn.disabled = true;
    try {
      if (auth.isConfigured) {
        await auth.signInWithGoogle();
      } else {
        // Local dev / no Supabase — create a guest session inline.
        auth.signInAsGuest("Guest");
      }
    } catch (err) {
      console.error("Sign-in failed:", err);
      alert(`Sign-in failed: ${err.message || err}`);
      signinBtn.disabled = false;
    }
  });
  auth.onChange((user) => {
    const chip = $("#user-chip");
    if (!chip) return;
    if (!user) {
      chip.classList.add("hidden");
      // Login required: bounce signed-out visitors to the / landing gate
      // (Google sign-in + Privacy/Terms). Only when Supabase is configured —
      // in local/guest dev there's nothing to sign into, so we instead show
      // the inline guest button so the page is still usable.
      if (auth.isConfigured) {
        if (signinBtn) signinBtn.classList.add("hidden");
        if (_allowAuthRedirect("fund_to_root")) window.location.replace("/");
      } else if (signinBtn) {
        signinBtn.classList.remove("hidden");
      }
      return;
    }
    chip.classList.remove("hidden");
    if (signinBtn) signinBtn.classList.add("hidden");

    // Mirrors reflectUserChip() in app.js so /fund and /analyze render the
    // same Google profile (display name, avatar photo, tier badge).
    const name = user.displayName || user.email || "Trader";
    $("#user-name").textContent = name;

    const av = $("#user-avatar");
    // Show initials first — covers slow image loads, CORS-blocked avatars,
    // no-photo accounts, and guest users. The image swap below replaces the
    // text once the photoURL probe resolves.
    const initials = name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((p) => p.charAt(0).toUpperCase())
      .join("") || "?";
    av.textContent = initials;
    av.style.backgroundImage = "";
    if (user.photoURL) {
      const probe = new Image();
      probe.onload = () => {
        // Re-check the current user — they may have signed out before the
        // image finished loading.
        if (auth.getUser?.()?.photoURL === user.photoURL) {
          av.style.backgroundImage = `url("${user.photoURL}")`;
          av.textContent = "";
        }
      };
      probe.src = user.photoURL;
    }

    const badge = $("#user-tier-badge");
    if (badge) {
      badge.textContent = TIER_LABEL[user.tier] || "Novice";
      badge.className = `tier-badge ${user.tier || "novice"}`;
    }
  });
}

// ------------------------------------------------------------------
// View routing
// ------------------------------------------------------------------

// Two-level navigation: the sidebar shows 5 consolidated GROUPS; each group
// holds one or more leaf sections (the original data-section panels) surfaced
// as a subtab bar. Leaf ids are unchanged so every existing render path and
// deep-link keeps working.
const NAV_GROUPS = {
  overview: { leaves: ["overview"] },
  fund: {
    leaves: ["decisions", "analyses", "theses"],
    labels: { decisions: "Book", analyses: "Transcripts", theses: "Recipes" },
  },
  journal: { leaves: ["journal"] },
  signals: {
    leaves: ["x_recs", "congress", "trade_history"],
    labels: { x_recs: "X Recs", congress: "Congress", trade_history: "Trade History" },
  },
  risk: {
    leaves: ["risk", "insights", "backtest", "events"],
    labels: { risk: "Controls", insights: "Calibration", backtest: "Backtest", events: "Streaming" },
  },
};
const LEAF_TO_GROUP = {};
for (const [g, def] of Object.entries(NAV_GROUPS)) {
  for (const leaf of def.leaves) LEAF_TO_GROUP[leaf] = g;
}
const _lastLeaf = {}; // group -> last viewed leaf id (sticky subtab)

// Accepts either a group key (overview/fund/journal/signals/risk) or a leaf
// section id (decisions, analyses, x_recs, …) — the latter lets banner links
// and #analyses/{id} deep-links target a specific subtab.
function switchView(target) {
  if (!target) return;
  let group, leaf;
  if (NAV_GROUPS[target]) {
    group = target;
    leaf = _lastLeaf[group] || NAV_GROUPS[group].leaves[0];
  } else if (LEAF_TO_GROUP[target]) {
    group = LEAF_TO_GROUP[target];
    leaf = target;
  } else {
    return;
  }
  state.view = group;
  _lastLeaf[group] = leaf;
  $$(".fund-nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.target === group));
  renderSubtabs(group, leaf);
  showLeaf(leaf);
}

// Build the subtab bar for a group (hidden when the group has a single leaf).
function renderSubtabs(group, activeLeaf) {
  const bar = $("#fund-subtabs");
  if (!bar) return;
  const def = NAV_GROUPS[group];
  if (!def || def.leaves.length < 2) {
    bar.innerHTML = "";
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  bar.innerHTML = def.leaves
    .map((leaf) => {
      const label = (def.labels && def.labels[leaf]) || leaf;
      const on = leaf === activeLeaf ? " active" : "";
      return `<button type="button" class="fund-subtab${on}" data-leaf="${leaf}">${label}</button>`;
    })
    .join("");
}

function showLeaf(leaf) {
  $$(".fund-section").forEach((s) => (s.hidden = s.dataset.section !== leaf));
  $$("#fund-subtabs .fund-subtab").forEach((b) => b.classList.toggle("active", b.dataset.leaf === leaf));
  // Let independent modules (fund_signals.js) react to a leaf becoming visible.
  window.dispatchEvent(new CustomEvent("aw-leaf-shown", { detail: { leaf } }));
  // Lazy-refresh on every view switch so the user always sees current data
  // without an explicit click. Cheap (a handful of small GETs).
  scheduleRefresh(leaf);
}

let _refreshTimer = null;
function scheduleRefresh(target) {
  // Debounce so rapid tab-clicks don't N+1 the API.
  clearTimeout(_refreshTimer);
  _refreshTimer = setTimeout(async () => {
    // Per-tab targeted loads keep it cheap.
    switch (target) {
      case "overview":  await refreshAll(); break;
      case "theses":    await loadRecipes(); renderRecipes(); break;
      case "analyses":  await loadRecentSessions(); renderAnalysesList(); applyAnalysesHash(); break;
      case "decisions": await Promise.all([loadPositions(), loadOrders(), loadConvictionSeries()]); renderPositions(); renderOrders(); renderConvictionChart(); break;
      case "journal":   await loadJournal(); renderJournal(); break;
      case "events":    await loadEvents(); renderEvents(); break;
      case "insights":  await loadInsights(); renderInsights(); break;
      case "risk":      await loadLimits(); renderLimits(); break;
    }
  }, 50);
}

async function loadInsights() {
  // Three independent fetches, all best-effort.
  state.behavioralFindings = [];
  state.disagreementLog = [];
  state.promptEvals = [];
  await Promise.all([
    fetch("/api/behavioral/findings?limit=50").then(r => r.ok ? r.json() : []).then(d => state.behavioralFindings = d).catch(() => null),
    fetch("/api/disagreement?limit=50").then(r => r.ok ? r.json() : []).then(d => state.disagreementLog = d).catch(() => null),
    fetch("/api/prompt-evals?limit=50").then(r => r.ok ? r.json() : []).then(d => state.promptEvals = d).catch(() => null),
  ]);
}

const _PATTERN_ICON_INS = { tilt: "🔥", revenge: "⚔️", anchoring: "⚓", overconfidence: "📈" };

function renderInsights() {
  // Behavioral history.
  const bhBody = $("#ins-bh-rows");
  const findings = state.behavioralFindings || [];
  $("#ins-bh-count").textContent = `(${findings.length})`;
  if (!findings.length) {
    bhBody.innerHTML = `<tr class="empty-row"><td colspan="5" class="subtle">No behavioral findings yet.</td></tr>`;
  } else {
    bhBody.innerHTML = "";
    for (const f of findings) {
      const sev = Number(f.severity || 0);
      const stateLabel =
        f.dismissed ? `<span class="subtle">⊘ dismissed</span>` :
        f.acknowledged ? `<span class="pnl-pos">✓ acknowledged</span>` :
        `<span class="pnl-neg">⚠ open</span>`;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="subtle">${String(f.created_at || "").slice(0,19).replace("T"," ")}</td>
        <td>${_PATTERN_ICON_INS[f.pattern] || "•"} ${escapeHTML(f.pattern)}</td>
        <td style="text-align:right">${(sev*100).toFixed(0)}%</td>
        <td class="subtle">${escapeHTML((f.evidence?.summary || "").slice(0, 140))}</td>
        <td>${stateLabel}</td>
      `;
      bhBody.appendChild(tr);
    }
  }

  // Disagreement log.
  const dsBody = $("#ins-dis-rows");
  const dis = state.disagreementLog || [];
  $("#ins-dis-count").textContent = `(${dis.length})`;
  if (!dis.length) {
    dsBody.innerHTML = `<tr class="empty-row"><td colspan="4" class="subtle">No fires yet. Trigger a thesis to populate.</td></tr>`;
  } else {
    dsBody.innerHTML = "";
    for (const d of dis) {
      const sim = parseFloat(d.similarity || 0);
      const cls = sim > 0.7 ? "pnl-neg" : sim > 0.4 ? "" : "pnl-pos";
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="subtle">${String(d.recorded_at || "").slice(0,19).replace("T"," ")}</td>
        <td class="subtle">${escapeHTML(d.bull_model || "?")} → ${escapeHTML(d.bear_model || "?")}</td>
        <td style="text-align:right" class="${cls}">${(sim*100).toFixed(0)}%</td>
        <td>${d.rating_agreement ? "✓ yes" : "✗ no"}</td>
      `;
      dsBody.appendChild(tr);
    }
  }

  // Prompt evals.
  const peBody = $("#ins-pe-rows");
  const evals = state.promptEvals || [];
  $("#ins-pe-count").textContent = `(${evals.length})`;
  if (!evals.length) {
    peBody.innerHTML = `<tr class="empty-row"><td colspan="6" class="subtle">No prompt evaluations yet.</td></tr>`;
  } else {
    peBody.innerHTML = "";
    for (const e of evals) {
      const promoted = e.promoted ? `<span class="pnl-pos">✓ promoted</span>` : `<span class="subtle">held back</span>`;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="subtle">${String(e.evaluated_at || "").slice(0,19).replace("T"," ")}</td>
        <td>${escapeHTML(e.variant)}</td>
        <td style="text-align:right">${e.n_samples}</td>
        <td style="text-align:right">${parseFloat(e.baseline_brier || 0).toFixed(4)}</td>
        <td style="text-align:right">${parseFloat(e.variant_brier || 0).toFixed(4)}</td>
        <td>${promoted}</td>
      `;
      peBody.appendChild(tr);
    }
  }
}

// ------------------------------------------------------------------
// Data loaders
// ------------------------------------------------------------------

async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    state.config = await r.json();
  } catch (e) {
    console.error("loadConfig", e);
  }
}

async function refreshAll() {
  await Promise.all([
    loadPaperAccount(),
    loadPositions(),
    loadOrders(),
    loadEvents(),
    loadLimits(),
    loadCalibration(),
    loadRecipes(),
    loadSpendSnapshot(),
    loadJournal(),
    loadBehavioralFindings(),
    loadStreamingEvents(),
    loadRecentSessions(),
  ]);
  buildActivity();
  render();
  renderStreamingEvents();
}

// Fetch recent + in-flight analyses so the Overview activity feed can show
// "AAPL — debate in progress" as it happens, not just completed orders.
async function loadRecentSessions() {
  try {
    const r = await fetch("/api/sessions");
    if (!r.ok) { state.sessions = []; return; }
    const all = await r.json();
    state.sessions = (Array.isArray(all) ? all : []).slice(0, 10);
  } catch (e) {
    console.error("loadRecentSessions", e);
    state.sessions = [];
  }
}

async function loadBehavioralFindings() {
  try {
    const r = await fetch("/api/behavioral/findings?limit=20");
    if (r.ok) state.behavioralFindings = await r.json();
  } catch (e) { console.error(e); }
}

async function loadJournal() {
  try {
    const r = await fetch("/api/journal/entries?limit=100&include_drafts=true");
    if (r.ok) state.journal = await r.json();
  } catch (e) { console.error(e); }
}

async function loadPaperAccount() {
  try {
    const r = await fetch("/api/paper/account");
    if (r.ok) state.paper = await r.json();
  } catch (e) { console.error(e); }
}

async function loadPositions() {
  try {
    const r = await fetch("/api/paper/positions");
    if (r.ok) state.positions = await r.json();
  } catch (e) { console.error(e); }
}

async function loadOrders() {
  try {
    const r = await fetch("/api/paper/orders?limit=50");
    if (r.ok) state.orders = await r.json();
  } catch (e) { console.error(e); }
}

async function loadEvents() {
  try {
    const r = await fetch("/api/risk/events?limit=50");
    if (r.ok) state.events = await r.json();
  } catch (e) { console.error(e); }
}

async function loadLimits() {
  try {
    const r = await fetch("/api/risk/limits");
    if (r.ok) state.limits = await r.json();
  } catch (e) { console.error(e); }
}

async function loadCalibration() {
  try {
    const r = await fetch("/api/paper/calibration");
    if (r.ok) state.brier = await r.json();
  } catch (e) { console.error(e); }
  // Phase 2 #3: also load the calibration head suggestion.
  try {
    const r2 = await fetch("/api/calibration");
    if (r2.ok) state.calSuggest = await r2.json();
  } catch (e) { console.error(e); }
}

async function loadRecipes() {
  try {
    const r = await fetch("/api/recipes");
    if (r.ok) state.recipes = await r.json();
  } catch (e) { console.error(e); }
}

// Snapshot today's spend by reading user_spend_daily indirectly via the
// risk_events budget rows + recipe_usage. For Phase 1 we approximate from
// `risk_limits.daily_spend_cap_usd` + a synthesized total from the recipe_usage
// API we expose at /api/recipes/{rid}/usage. The cheaper path: a dedicated
// endpoint. For now we just use limits.daily_spend_cap_usd and let the user
// know what their cap is. Wire a real reader in Phase 2 when the dashboard
// gets per-day breakdowns.
async function loadSpendSnapshot() {
  state.spendCap = parseFloat(state.limits?.daily_spend_cap_usd ?? 5.0);
  // Crude rollup from recipe_usage via the per-recipe-usage shim.
  let total = 0;
  for (const rcp of state.recipes) {
    try {
      const r = await fetch(`/api/recipes/${rcp.id}/usage`);
      if (r.ok) {
        const u = await r.json();
        total += parseFloat(u?.token_cost_usd ?? 0);
      }
    } catch { /* ignore — endpoint may not exist in some builds */ }
  }
  state.spendToday = total;
}

// ------------------------------------------------------------------
// Rendering
// ------------------------------------------------------------------

function render() {
  renderZeroState();
  renderHero();
  renderSpendBar();
  renderCalibration();
  renderPositions();
  renderOrders();
  renderEvents();
  renderRecipes();
  renderLimits();
  renderActivity();
  renderJournal();
  renderBehavioralFindings();
}

// Show the zero-state hero only when the user genuinely has nothing yet.
// Hide the empty "Ask the fund" / "Calibration" / "Recent activity" cards on
// the Overview tab in first-run mode so the user has a single focal action.
function renderZeroState() {
  // Hide the empty-data cards on Overview when the user has nothing yet, so
  // the hero stays the focal point. The hero itself is always visible.
  const hasRecipes = (state.recipes || []).length > 0;
  const hasOrders = (state.orders || []).length > 0;
  const firstRun = !hasRecipes && !hasOrders;
  const overview = document.querySelector('[data-section="overview"]');
  if (!overview) return;
  const hideOnFirstRun = [
    "#streaming-card",
    "#ask-card",
    overview.querySelector(".phase1-card:has(#ov-activity)"),
    overview.querySelector(".phase1-card:has(#spend-bar)"),
    overview.querySelector(".phase1-card:has(#calibration-brier)"),
  ];
  hideOnFirstRun.forEach((sel) => {
    const el = typeof sel === "string" ? overview.querySelector(sel) : sel;
    if (el) el.hidden = firstRun;
  });
}

const PATTERN_ICON = { tilt: "🔥", revenge: "⚔️", anchoring: "⚓", overconfidence: "📈" };

function renderBehavioralFindings() {
  const card = document.getElementById("bf-card");
  const list = document.getElementById("bf-list");
  const countLabel = document.getElementById("bf-count");
  if (!card || !list) return;
  // Filter out dismissed findings — keep them in state for future "show all"
  // but hide from the default card. Acknowledged stays visible to the user.
  const findings = (state.behavioralFindings || []).filter((f) => !f.dismissed);
  if (!findings.length) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  countLabel.textContent = `(${findings.length})`;
  list.innerHTML = "";
  for (const f of findings) {
    const icon = PATTERN_ICON[f.pattern] || "•";
    const sev = Number(f.severity || 0);
    const sevBar = Math.round(sev * 100);
    const sevColor = sev > 0.6 ? "var(--bad)" : sev > 0.3 ? "var(--warn)" : "var(--accent-2)";
    const summary = (f.evidence && f.evidence.summary) || "(no summary)";
    const div = document.createElement("article");
    div.className = "bf-card-row";
    if (f.acknowledged) div.classList.add("ack");
    div.innerHTML = `
      <div class="bf-icon">${icon}</div>
      <div class="bf-body">
        <div class="bf-head">
          <strong class="bf-pattern">${escapeHTML(f.pattern)}</strong>
          <span class="bf-meta subtle">severity ${(sev * 100).toFixed(0)}% · ${formatRelative(f.created_at)}</span>
        </div>
        <div class="bf-summary">${escapeHTML(summary)}</div>
        <div class="bf-sev-bar"><div class="bf-sev-fill" style="width:${sevBar}%;background:${sevColor}"></div></div>
      </div>
      <div class="bf-actions">
        ${f.acknowledged ? "" : `<button class="ghost-btn" data-bf-act="acknowledge">✓ Got it</button>`}
        <button class="ghost-btn danger" data-bf-act="dismiss">⊘ Dismiss</button>
      </div>
    `;
    div.querySelectorAll("[data-bf-act]").forEach((btn) => {
      btn.addEventListener("click", () => updateFindingState(f, btn.dataset.bfAct));
    });
    list.appendChild(div);
  }
}

async function updateFindingState(finding, action) {
  try {
    const r = await fetch("/api/behavioral/findings/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pattern: finding.pattern,
        created_at: finding.created_at,
        action,
      }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    await loadBehavioralFindings();
    renderBehavioralFindings();
    flash(action === "dismiss" ? "Dismissed." : "Acknowledged.");
  } catch (e) { alert(`Failed: ${e.message || e}`); }
}

async function rescanBehavioral() {
  try {
    const r = await fetch("/api/behavioral/scan", { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const result = await r.json();
    await loadBehavioralFindings();
    renderBehavioralFindings();
    flash(`Scan complete — ${result.new_findings} new finding(s).`);
  } catch (e) { alert(`Failed: ${e.message || e}`); }
}

function renderJournal() {
  const wrap = $("#je-entries");
  const entries = (state.journal || []).filter(
    (e) => state.showDrafts || !e.is_draft,
  );
  $("#je-count").textContent = `(${entries.length}${state.showDrafts ? "" : ", drafts hidden"})`;
  if (!entries.length) {
    wrap.innerHTML = `<div class="empty-row subtle" style="padding:18px;text-align:center">No entries yet. The fund will auto-draft one the next time a thesis fires.</div>`;
    return;
  }
  wrap.innerHTML = "";
  for (const e of entries) {
    const card = document.createElement("article");
    card.className = "je-card";
    if (e.is_draft) card.classList.add("draft");
    const kindBadge = `<span class="je-kind kind-${e.kind}">${e.kind}</span>`;
    const draftBadge = e.is_draft ? `<span class="je-draft-badge">DRAFT</span>` : "";
    const sentiment = e.sentiment_score != null
      ? `<span class="subtle" style="font-size:11px;">sentiment ${e.sentiment_score}</span>`
      : "";
    const sessionRef = e.session_id
      ? `<a href="/#session/${escapeHTML(e.session_id)}" target="_blank" class="subtle" style="font-size:11px">session ${e.session_id.slice(0, 8)}</a>`
      : "";
    card.innerHTML = `
      <header class="je-card-head">
        <div class="je-meta">
          ${kindBadge}
          ${draftBadge}
          <span class="subtle">${formatRelative(e.created_at)}</span>
          ${sessionRef}
          ${sentiment}
        </div>
        <div class="je-actions">
          <button class="ghost-btn" data-act="edit">Edit</button>
          <button class="ghost-btn danger" data-act="delete">Delete</button>
        </div>
      </header>
      <div class="je-body" data-md="1"></div>
    `;
    card.querySelector(".je-body").textContent = e.body;
    card.querySelector('[data-act="edit"]').addEventListener("click", () => editJournalEntry(e));
    card.querySelector('[data-act="delete"]').addEventListener("click", () => deleteJournalEntry(e.id));
    wrap.appendChild(card);
  }
}

async function submitJournalEntry(e) {
  e.preventDefault();
  $("#je-error").textContent = "";
  const editingId = $("#je-form").dataset.editingId || null;
  const payload = {
    body: $("#je-body").value.trim(),
    kind: $("#je-kind").value,
    session_id: $("#je-session").value.trim() || null,
    is_draft: false,
  };
  if (!payload.body) { $("#je-error").textContent = "Body required."; return; }
  try {
    const url = editingId ? `/api/journal/entries/${editingId}` : "/api/journal/entries";
    const method = editingId ? "PUT" : "POST";
    const res = await fetch(url, {
      method, headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    $("#je-form").reset();
    delete $("#je-form").dataset.editingId;
    $("#je-attach-hint").textContent = "";
    await loadJournal();
    renderJournal();
    flash(editingId ? "Entry updated." : "Entry saved.");
  } catch (err) {
    $("#je-error").textContent = String(err.message || err);
  }
}

function editJournalEntry(entry) {
  $("#je-body").value = entry.body || "";
  $("#je-kind").value = entry.kind || "note";
  $("#je-session").value = entry.session_id || "";
  $("#je-form").dataset.editingId = entry.id;
  $("#je-attach-hint").textContent = ` — editing ${entry.kind} from ${formatRelative(entry.created_at)}`;
  switchView("journal");
  $("#je-body").focus();
}

async function deleteJournalEntry(eid) {
  if (!confirm("Delete this journal entry?")) return;
  try {
    const res = await fetch(`/api/journal/entries/${eid}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadJournal();
    renderJournal();
    flash("Entry deleted.");
  } catch (err) { alert(`Failed: ${err.message || err}`); }
}

function renderHero() {
  const p = state.paper || {};
  $("#ov-nav").textContent       = fmtUSD(p.nav);
  $("#ov-realized").textContent  = fmtUSD(p.realized_pnl);
  $("#ov-unrealized").textContent = fmtUSD(p.unrealized_pnl);
  $("#ov-cash").textContent      = fmtUSD(p.cash);
  if (p.starting_cash && p.nav != null) {
    const pct = ((p.nav - p.starting_cash) / p.starting_cash) * 100;
    const cls = pct >= 0 ? "pnl-pos" : "pnl-neg";
    $("#ov-nav-delta").innerHTML = `<span class="${cls}">${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%</span> vs starting`;
  }
}

function renderSpendBar() {
  const used = state.spendToday || 0;
  const cap = state.spendCap || 5.0;
  const pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0;
  $("#ov-spend-fill").style.width = `${pct.toFixed(1)}%`;
  $("#ov-spend-fill").className = "fund-spend-bar-fill " + (pct > 80 ? "danger" : pct > 50 ? "warn" : "ok");
  $("#ov-spend-used").textContent = fmtUSD(used);
  $("#ov-spend-cap").textContent  = `/ ${fmtUSD(cap)} cap`;
  $("#ov-spend-tag").textContent  = `${pct.toFixed(0)}% used`;
}

function renderCalibration() {
  const c = state.brier || {};
  if (c.brier_score == null) {
    $("#ov-brier").textContent = "—";
    $("#ov-cal-state").textContent = "no data";
    $("#ov-cal-interp").textContent = "Resolve outcomes to populate. Brier score = mean squared error between PM probabilities and realized hits. Lower is better.";
  } else {
    $("#ov-brier").textContent = c.brier_score.toFixed(4);
    $("#ov-cal-state").textContent = c.interpretation || "";
    const cls = c.brier_score < 0.20 ? "pnl-pos" : c.brier_score < 0.30 ? "" : "pnl-neg";
    $("#ov-brier").className = "fund-stat " + cls;
  }
  renderCalibrationSuggestion();
}

function renderCalibrationSuggestion() {
  const wrap = $("#cal-suggest");
  if (!wrap) return;
  const s = state.calSuggest || {};
  wrap.classList.remove("hidden");
  let html = "";
  switch (s.status) {
    case "no_fit": {
      const pct = Math.min(100, ((s.n || 0) / (s.unlock_n || 30)) * 100);
      html = `
        <div class="cal-suggest-body">
          <div class="cal-suggest-title">Calibration head — learning your edges</div>
          <p class="subtle" style="margin:4px 0">
            The fund will fit a personalized probability-calibration map once you have
            <strong>${s.unlock_n || 30}</strong> resolved outcomes. You're at <strong>${s.n || 0}</strong>.
          </p>
          <div class="cal-progress"><div class="cal-progress-fill" style="width:${pct.toFixed(0)}%"></div></div>
        </div>`;
      break;
    }
    case "available": {
      const improvement = ((s.improvement || 0) * 100).toFixed(2);
      html = `
        <div class="cal-suggest-body cal-suggest-available">
          <div class="cal-suggest-title">📈 Your fund has learned your calibration</div>
          <p class="subtle" style="margin:4px 0">
            Brier ${s.brier_before?.toFixed(4)} → <strong>${s.brier_after?.toFixed(4)}</strong>
            over ${s.n} outcomes (improvement: ${improvement} Brier points).
            Apply it to your sizing?
          </p>
          <div class="cal-suggest-actions">
            <button type="button" class="go-btn small-go" id="cal-apply-btn">
              <span class="go-btn-label">Apply calibration</span>
              <span class="go-btn-spark">✓</span>
            </button>
          </div>
        </div>`;
      break;
    }
    case "applied": {
      html = `
        <div class="cal-suggest-body cal-suggest-applied">
          <div class="cal-suggest-title">✅ Calibration applied</div>
          <p class="subtle" style="margin:4px 0">
            <code>p_calibrated = sigmoid(${s.a?.toFixed(3)} * logit(p_raw) + ${s.b?.toFixed(3)})</code> —
            applied to every paper-trade fire (${s.n} outcomes fit).
          </p>
          <div class="cal-suggest-actions">
            <button type="button" class="ghost-btn" id="cal-revoke-btn">Revoke</button>
            <button type="button" class="ghost-btn" id="cal-refit-btn">↻ Refit on latest outcomes</button>
          </div>
        </div>`;
      break;
    }
    case "no_improvement": {
      html = `
        <div class="cal-suggest-body">
          <div class="cal-suggest-title">Calibration available — doesn't beat raw yet</div>
          <p class="subtle" style="margin:4px 0">
            Fit on ${s.n} outcomes but Brier ${s.brier_before?.toFixed(4)} → ${s.brier_after?.toFixed(4)} —
            no measurable improvement. More data or a better PM prompt should help.
          </p>
        </div>`;
      break;
    }
    default:
      wrap.classList.add("hidden");
      return;
  }
  wrap.innerHTML = html;
  $("#cal-apply-btn")?.addEventListener("click", () => optInCalibration(true));
  $("#cal-revoke-btn")?.addEventListener("click", () => optInCalibration(false));
  $("#cal-refit-btn")?.addEventListener("click", refitCalibration);
}

async function optInCalibration(apply) {
  try {
    const r = await fetch("/api/calibration/opt-in", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ apply, regime: "all" }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    await loadCalibration();
    renderCalibration();
    flash(apply ? "✅ Calibration applied to sizing." : "Calibration revoked.");
  } catch (e) { alert(`Failed: ${e.message || e}`); }
}

async function refitCalibration() {
  try {
    const r = await fetch("/api/calibration/fit", { method: "POST" });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    await loadCalibration();
    renderCalibration();
    flash("Calibration refit on latest outcomes.");
  } catch (e) { alert(`Failed: ${e.message || e}`); }
}

function renderPositions() {
  const tbody = $("#positions-rows");
  $("#pos-count").textContent = `(${state.positions.length})`;
  if (!state.positions.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="5" class="subtle">No positions.</td></tr>`;
    return;
  }
  tbody.innerHTML = "";
  for (const p of state.positions) {
    const last = p.last_price;
    const mtm  = last != null
      ? (p.qty > 0 ? (last - p.avg_cost) * p.qty : (p.avg_cost - last) * Math.abs(p.qty))
      : null;
    const mtmClass = mtm == null ? "" : (mtm >= 0 ? "pnl-pos" : "pnl-neg");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHTML(p.ticker)}</strong> ${p.qty < 0 ? `<span class="subtle">(short)</span>` : ""}</td>
      <td style="text-align:right">${fmtNum(p.qty)}</td>
      <td style="text-align:right">${fmtUSD(p.avg_cost)}</td>
      <td style="text-align:right">${last != null ? fmtUSD(last) : "—"}</td>
      <td style="text-align:right" class="${mtmClass}">${mtm != null ? fmtUSD(mtm) : "—"}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderOrders() {
  const tbody = $("#orders-rows");
  $("#orders-count").textContent = `(${state.orders.length})`;
  if (!state.orders.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="10" class="subtle">No orders yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = "";
  for (const o of state.orders) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="subtle">${String(o.created_at || "").slice(0, 19).replace("T", " ")}</td>
      <td><strong>${escapeHTML(o.ticker)}</strong></td>
      <td><span class="order-side ${o.side}">${o.side}</span></td>
      <td style="text-align:right">${fmtNum(o.qty)}</td>
      <td style="text-align:right">${fmtUSD(o.fill_price)}</td>
      <td>${escapeHTML(o.pm_rating || "—")}</td>
      <td style="text-align:right">${o.conviction_score ?? "—"}</td>
      <td style="text-align:right" class="subtle">${o.kelly_fraction != null ? (parseFloat(o.kelly_fraction) * 100).toFixed(2) + "%" : "—"}</td>
      <td><span class="order-status status-${o.status}">${o.status}</span></td>
      <td style="text-align:right">
        ${o.session_id ? `<button class="ghost-btn" data-explain="${escapeHTML(o.session_id)}">Why?</button>` : "—"}
      </td>
    `;
    const btn = tr.querySelector("[data-explain]");
    if (btn) btn.addEventListener("click", () => toggleExplain(tr, o.session_id));
    tbody.appendChild(tr);
  }
}

// ------------------------------------------------------------------
// Per-decision tear-down: ablation report + classical voice + disagreement
// ------------------------------------------------------------------

const _explainCache = new Map();   // session_id → fetched payload

async function toggleExplain(row, sessionId) {
  // Toggle off if already open.
  const next = row.nextSibling;
  if (next && next.classList && next.classList.contains("decision-explain-row")) {
    next.remove();
    return;
  }
  const drawer = document.createElement("tr");
  drawer.className = "decision-explain-row";
  drawer.innerHTML = `<td colspan="10"><div class="explain-pane">Loading…</div></td>`;
  row.parentNode.insertBefore(drawer, row.nextSibling);

  let payload = _explainCache.get(sessionId);
  if (!payload) {
    payload = await fetchExplainBundle(sessionId);
    _explainCache.set(sessionId, payload);
  }
  drawer.querySelector(".explain-pane").innerHTML = renderExplain(payload);
}

async function fetchExplainBundle(sessionId) {
  // Three independent lookups; tolerate partial failures.
  const out = { ablation: null, session: null, disagreement: null };
  try {
    const r = await fetch(`/api/sessions/${sessionId}/ablation`);
    if (r.ok) out.ablation = await r.json();
  } catch (e) { console.error(e); }
  try {
    const r = await fetch(`/api/sessions/${sessionId}`);
    if (r.ok) out.session = await r.json();
  } catch (e) { console.error(e); }
  try {
    // The disagreement_log endpoint returns recent rows; find this session's.
    const r = await fetch("/api/disagreement?limit=200");
    if (r.ok) {
      const rows = await r.json();
      out.disagreement = rows.find((d) => d.session_id === sessionId) || null;
    }
  } catch (e) { console.error(e); }
  return out;
}

function renderExplain(p) {
  const blocks = [];

  // 1. Disagreement index.
  if (p.disagreement) {
    const sim = parseFloat(p.disagreement.similarity || 0);
    const agree = p.disagreement.rating_agreement;
    const cls = sim > 0.7 ? "high" : sim > 0.4 ? "mid" : "low";
    const verdict = sim > 0.7
      ? "⚠️ Bull and Bear were highly aligned — possible groupthink"
      : sim > 0.4
      ? "Bull and Bear shared some vocabulary"
      : "Bull and Bear took genuinely different angles";
    blocks.push(`
      <section class="explain-block">
        <h4>🤝 Bull / Bear disagreement</h4>
        <div class="explain-row">
          <div>Similarity (cosine):</div>
          <div class="explain-bar"><div class="explain-bar-fill ${cls}" style="width:${(sim*100).toFixed(0)}%"></div></div>
          <div class="subtle">${(sim*100).toFixed(0)}%</div>
        </div>
        <div class="subtle">Rating agreement: <strong>${agree ? "yes" : "no"}</strong> · ${escapeHTML(p.disagreement.bull_model || "?")} vs ${escapeHTML(p.disagreement.bear_model || "?")}</div>
        <p class="subtle" style="font-style:italic">${verdict}</p>
      </section>`);
  }

  // 2. Classical voice (if auto-injected).
  const classical = p.session?.classical_decision;
  if (classical) {
    const pmRating = p.session?.report_sections?.final_trade_decision || "";
    blocks.push(`
      <section class="explain-block">
        <h4>📐 Classical Analyst said: <strong>${escapeHTML(classical.rating)}</strong></h4>
        <div class="subtle">Deterministic rules-based voice, auto-injected because Bull/Bear consensus was high.</div>
        <div class="subtle" style="margin-top:8px">${escapeHTML(classical.executive_summary || "")}</div>
        ${p.session?.classical_score != null ? `<div class="subtle" style="margin-top:6px">aggregate score: <code>${parseFloat(p.session.classical_score).toFixed(3)}</code></div>` : ""}
      </section>`);
  }

  // 3. Ablation contributions.
  if (p.ablation?.contributions?.length) {
    const top = p.ablation.top_section;
    const silent = p.ablation.silent_sections || [];
    const rowsHTML = p.ablation.contributions.map((c) => `
      <div class="explain-row">
        <div>${escapeHTML(c.section)}${c.section === top ? " 🏆" : ""}</div>
        <div class="explain-bar"><div class="explain-bar-fill" style="width:${(c.score*100).toFixed(0)}%"></div></div>
        <div class="subtle">${(c.score*100).toFixed(0)}%</div>
      </div>`).join("");
    blocks.push(`
      <section class="explain-block">
        <h4>🔍 Which analyst was load-bearing?</h4>
        ${rowsHTML}
        ${silent.length ? `<p class="subtle" style="margin-top:8px">⊘ <strong>Silently ignored:</strong> ${silent.map(escapeHTML).join(", ")}</p>` : ""}
        <p class="subtle" style="font-style:italic;font-size:11px">${escapeHTML(p.ablation.method || "")}</p>
      </section>`);
  }

  if (!blocks.length) {
    return `<div class="subtle">No explanation data for this session yet.</div>`;
  }
  return blocks.join("");
}

function renderEvents() {
  const tbody = $("#events-rows");
  $("#events-count").textContent = `(${state.events.length})`;
  if (!state.events.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="4" class="subtle">No risk events.</td></tr>`;
    return;
  }
  tbody.innerHTML = "";
  for (const e of state.events) {
    const tr = document.createElement("tr");
    const detail = e.details?.reason || JSON.stringify(e.details || {}).slice(0, 140);
    tr.innerHTML = `
      <td class="subtle">${String(e.created_at || "").slice(0, 19).replace("T", " ")}</td>
      <td><span class="risk-rule rule-${e.rule}">${e.rule}</span></td>
      <td>${escapeHTML(e.ticker || "—")}</td>
      <td class="subtle">${escapeHTML(detail)}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderLimits() {
  const l = state.limits || {};
  $("#rl-max-pos").value      = l.max_position_pct ?? 0.10;
  $("#rl-max-dd").value       = l.max_daily_drawdown_pct ?? 0.03;
  $("#rl-kelly").value        = l.kelly_fraction_cap ?? 0.10;
  $("#rl-slip").value         = l.max_slippage_bps ?? 10;
  $("#rl-daily-spend").value  = l.daily_spend_cap_usd ?? 5.0;
  $("#rl-monthly-spend").value = l.monthly_spend_cap_usd ?? 100.0;
  $("#kill-toggle").checked   = !!l.global_kill_switch;
  $("#kill-state").textContent = l.global_kill_switch
    ? "🔴 Engaged — no new paper orders will land."
    : "🟢 Inactive — orders flow through risk guards as usual.";
  // Phase 2 #5 cooldown.
  const bc = $("#bc-toggle");
  if (bc) {
    bc.checked = !!l.behavioral_cooldown;
    $("#bc-state").textContent = l.behavioral_cooldown
      ? "🛡️ Armed — next order blocked for 60min after a tilt / revenge finding."
      : "💤 Off — detection runs but doesn't block orders.";
  }
}

async function toggleBehavioralCooldown() {
  const enabled = $("#bc-toggle").checked;
  try {
    const r = await fetch("/api/risk/limits", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ behavioral_cooldown: enabled }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await loadLimits();
    renderLimits();
    flash(enabled ? "🛡️ Behavioral cooldown armed." : "💤 Behavioral cooldown off.");
  } catch (e) {
    $("#bc-toggle").checked = !enabled;
    alert(`Failed: ${e.message || e}`);
  }
}

function renderRecipes() {
  const tbody = $("#r-rows");
  $("#r-count").textContent = `(${state.recipes.length})`;
  if (!state.recipes.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="7" class="subtle">No recipes yet. Create one above.</td></tr>`;
    return;
  }
  tbody.innerHTML = "";
  for (const r of state.recipes) {
    const tr = document.createElement("tr");
    const isActive = r.status === "active";
    const isPaused = r.status === "paused";
    const lastRunCell = r.last_run_at
      ? `<button class="link-btn" data-act="open-last" data-id="${escapeHTML(r.id)}">${formatRelative(r.last_run_at)}</button>`
      : `<span class="subtle">—</span>`;
    tr.innerHTML = `
      <td><strong>${escapeHTML(r.name)}</strong></td>
      <td>${(r.tickers || []).map(escapeHTML).join(", ")}</td>
      <td>${escapeHTML(r.schedule_kind)}${r.schedule_expr ? `: ${escapeHTML(r.schedule_expr)}` : ""}</td>
      <td>${escapeHTML(r.output_policy)}</td>
      <td><span class="recipe-status status-${r.status}">${r.status}</span></td>
      <td>${lastRunCell}</td>
      <td class="recipe-actions">
        <button class="ghost-btn" data-act="trigger">▶ Fire</button>
        ${isActive ? `<button class="ghost-btn" data-act="pause">⏸</button>` : ""}
        ${isPaused ? `<button class="ghost-btn" data-act="resume">▶</button>` : ""}
        <button class="ghost-btn" data-act="kill">⛔</button>
        <button class="ghost-btn danger" data-act="delete">✕</button>
      </td>
    `;
    for (const btn of tr.querySelectorAll("button")) {
      if (btn.dataset.act === "open-last") {
        btn.addEventListener("click", () => openLastSession(r.id));
      } else if (btn.dataset.act) {
        btn.addEventListener("click", () => recipeAction(r.id, btn.dataset.act));
      }
    }
    tbody.appendChild(tr);
  }
}

function buildActivity() {
  // Overview activity is the *user's own* action log: debates they kicked
  // off and orders the system placed on their behalf. System-generated risk
  // events live in Lab → Risk events, where they belong.
  const acts = [];
  for (const s of (state.sessions || []).slice(0, 8)) {
    const status = s.status || "pending";
    const type = (status === "running" || status === "pending" || status === "composing_report")
      ? "analysis-running"
      : (status === "completed" ? "analysis" : `analysis-${status}`);
    const kp = pmKpis(s.pm_decision || s.portfolio_decision);
    acts.push({
      ts: s.completed_at || s.created_at,
      type,
      ticker: s.ticker || "—",
      verdict: kp.verdict,
      target: kp.target,
      er: kp.er,
      pop: kp.pop,
      conv: kp.conv,
      hold: kp.hold,
      status,
    });
  }
  for (const o of (state.orders || []).slice(0, 6)) {
    // Orders carry the rating but not the full PM scalars. Show what we have;
    // the rest are em-dashes.
    acts.push({
      ts: o.created_at,
      type: o.status === "blocked" ? "order-blocked" : "order",
      ticker: o.ticker || "—",
      verdict: (o.pm_rating || "").toUpperCase() || "—",
      target: "—",
      er: "—",
      pop: "—",
      conv: o.conviction != null ? `${o.conviction}/10` : "—",
      hold: "—",
      status: `${o.side} ${fmtNum(o.qty)} @ ${fmtUSD(o.fill_price)}`,
    });
  }
  // Normalize to epoch millis so ISO strings and Unix-epoch floats can sort
  // against each other. (Server returns sessions with float-seconds epoch and
  // orders/events with ISO strings.)
  acts.sort((a, b) => tsToMillis(b.ts) - tsToMillis(a.ts));
  state.activity = acts.slice(0, 10);
}

// Extract + format the PM-decision KPIs for table rendering. Mirrors the
// server-side `paper.score_from_decision()` formula for conviction so we
// have a value even when the conviction_scores table isn't surfaced yet on
// the session payload. Every field returns a printable string (em-dash for
// missing values) so call sites don't need null checks.
function pmKpis(pm) {
  const empty = { verdict: "—", target: "—", er: "—", pop: "—", conv: "—", hold: "—" };
  if (!pm || typeof pm !== "object") return empty;
  const verdict = (pm.rating || "").toString().toUpperCase() || "—";
  const target  = pm.price_target != null ? fmtUSD(pm.price_target) : "—";
  const er = pm.expected_return_pct != null
    ? `${pm.expected_return_pct > 0 ? "+" : ""}${Number(pm.expected_return_pct).toFixed(1)}%`
    : "—";
  const pop = pm.prob_of_profit != null
    ? `${Math.round(Number(pm.prob_of_profit) * 100)}%`
    : "—";
  const hold = pm.expected_hold_days != null ? `${pm.expected_hold_days}d` : "—";
  // Conviction: prefer an explicit field if the server happens to attach one;
  // else fall back to the Sharpe-flavored derivation that paper.py uses.
  let convInt = null;
  if (pm.conviction_score != null) convInt = Number(pm.conviction_score);
  else {
    const p = pm.prob_of_profit, e = pm.expected_return_pct, v = pm.expected_volatility_pct;
    if (p != null && e != null && v != null && v > 0) {
      const signal = Number(p) * (Math.abs(Number(e)) / Number(v));
      const norm = Math.max(0, Math.min(1, signal / 0.6));
      convInt = Math.max(1, Math.min(10, Math.round(1 + norm * 9)));
    } else if (pm.rating) {
      const fallback = { BUY: 9, OVERWEIGHT: 7, HOLD: 3, UNDERWEIGHT: 6, SELL: 8 };
      convInt = fallback[(pm.rating || "").toUpperCase()] ?? null;
    }
  }
  const conv = convInt != null && Number.isFinite(convInt) ? `${convInt}/10` : "—";
  return { verdict, target, er, pop, conv, hold };
}

function tsToMillis(ts) {
  if (ts == null) return 0;
  if (ts instanceof Date) return ts.getTime();
  if (typeof ts === "number") return ts < 1e12 ? ts * 1000 : ts;
  const n = Number(ts);
  if (Number.isFinite(n) && String(ts).match(/^[-0-9.eE+]+$/)) {
    return n < 1e12 ? n * 1000 : n;
  }
  const d = new Date(ts).getTime();
  return Number.isFinite(d) ? d : 0;
}

function renderActivity() {
  const tbody = $("#ov-activity");
  if (!state.activity.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="5" class="subtle">No activity yet. Create a recipe and trigger it.</td></tr>`;
    return;
  }
  tbody.innerHTML = "";
  for (const a of state.activity) {
    const tr = document.createElement("tr");
    // Normalize first so numeric-string timestamps (the float-seconds Supabase
    // sends back) feed cleanly into formatRelative, which expects an
    // ISO string or an epoch-seconds number.
    const ms = tsToMillis(a.ts);
    const rel = ms ? formatRelative(ms / 1000) : "";
    const abs = ms ? new Date(ms).toLocaleString() : "";
    const verdictClass = (a.verdict || "").toLowerCase();
    const verdictCell = a.verdict && a.verdict !== "—"
      ? `<span class="verdict-tag ${escapeHTML(verdictClass)}">${escapeHTML(a.verdict)}</span>`
      : `<span class="subtle">—</span>`;
    tr.innerHTML = `
      <td class="subtle" title="${escapeHTML(abs)}">${escapeHTML(rel)}</td>
      <td><span class="activity-badge type-${a.type}">${a.type}</span></td>
      <td><strong>${escapeHTML(a.ticker)}</strong></td>
      <td>${verdictCell}</td>
      <td style="text-align:right" class="subtle">${escapeHTML(a.target)}</td>
    `;
    tbody.appendChild(tr);
  }
}

function formatAbsoluteTime(ts) {
  const ms = tsToMillis(ts);
  if (!ms) return ts ? String(ts) : "";
  return new Date(ms).toLocaleString();
}

// ------------------------------------------------------------------
// Recipe form
// ------------------------------------------------------------------

function populateRecipeFormSelects() {
  if (!state.config) return;
  const provSel = $("#rf-provider");
  provSel.innerHTML = "";
  for (const p of state.config.providers || []) {
    const opt = document.createElement("option");
    opt.value = p.key;
    opt.textContent = p.label;
    provSel.appendChild(opt);
  }
  provSel.value = state.config.defaults?.provider || provSel.value;

  // Analyst chips.
  const aWrap = $("#rf-analysts");
  aWrap.innerHTML = "";
  for (const a of state.config.analysts || []) {
    const c = document.createElement("button");
    c.type = "button";
    c.className = "chip active";
    c.dataset.key = a.key;
    c.textContent = a.label;
    c.addEventListener("click", () => c.classList.toggle("active"));
    aWrap.appendChild(c);
  }

  syncRecipeProviderFields();
}

function syncRecipeProviderFields() {
  const provider = $("#rf-provider").value;
  if (!provider || !state.config) return;
  const models = state.config.models?.[provider] || {};
  fillModelSelect("#rf-quick", models.quick || models.quick_models || []);
  fillModelSelect("#rf-deep",  models.deep  || models.deep_models  || []);

  // Bull/Bear span every provider's models.
  const opts = [];
  const seen = new Set();
  for (const [prov, ms] of Object.entries(state.config.models || {})) {
    for (const tup of [...(ms.quick || ms.quick_models || []), ...(ms.deep || ms.deep_models || [])]) {
      const [label, id] = Array.isArray(tup) ? tup : [String(tup), String(tup)];
      if (seen.has(id)) continue;
      seen.add(id);
      opts.push({ value: id, label: `${id} — ${label.split(" - ")[0]} (${prov})`, provider: prov });
    }
  }
  fillTaggedSelect("#rf-bull", opts);
  fillTaggedSelect("#rf-bear", opts);
  $("#rf-bull").value = opts[0]?.value || "";
  const bearOpt = opts.find((o) => o.provider !== opts[0]?.provider);
  if (bearOpt) $("#rf-bear").value = bearOpt.value;
  updateBearHint();
}

function maybeAutoPickBear() {
  // When the user picks a Bull, auto-pick the first option from a different
  // provider as Bear (only if the current Bear shares Bull's family).
  const bullSel = $("#rf-bull");
  const bearSel = $("#rf-bear");
  const bullProvider = bullSel.options[bullSel.selectedIndex]?.dataset.provider;
  const bearProvider = bearSel.options[bearSel.selectedIndex]?.dataset.provider;
  if (bullProvider && bearProvider === bullProvider) {
    for (const opt of bearSel.options) {
      if (opt.dataset.provider !== bullProvider) {
        bearSel.value = opt.value;
        break;
      }
    }
  }
  updateBearHint();
}

function updateBearHint() {
  const bullProv = $("#rf-bull").options[$("#rf-bull").selectedIndex]?.dataset.provider;
  const bearProv = $("#rf-bear").options[$("#rf-bear").selectedIndex]?.dataset.provider;
  const hint = $("#rf-bear-hint");
  if (bullProv && bearProv && bullProv === bearProv) {
    hint.textContent = `— ⚠️ same family as Bull (${bullProv}); the heterogeneity check will reject this`;
    hint.style.color = "var(--bad)";
  } else {
    hint.textContent = `— ${bearProv || "?"} ≠ ${bullProv || "?"} ✓`;
    hint.style.color = "var(--text-dim)";
  }
}

function fillModelSelect(selector, tuples) {
  const el = $(selector);
  el.innerHTML = "";
  for (const tup of tuples) {
    const [label, id] = Array.isArray(tup) ? tup : [String(tup), String(tup)];
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = label;
    el.appendChild(opt);
  }
}

function fillTaggedSelect(selector, opts) {
  const el = $(selector);
  el.innerHTML = "";
  for (const o of opts) {
    const opt = document.createElement("option");
    opt.value = o.value;
    opt.textContent = o.label;
    opt.dataset.provider = o.provider;
    el.appendChild(opt);
  }
}

// ------------------------------------------------------------------
// Mutations
// ------------------------------------------------------------------

async function submitRecipe(e) {
  e.preventDefault();
  $("#rf-error").textContent = "";
  const analystKeys = $$("#rf-analysts .chip.active").map((c) => c.dataset.key);
  const payload = {
    name: $("#rf-name").value.trim(),
    tickers: $("#rf-tickers").value.split(",").map((s) => s.trim()).filter(Boolean),
    analysts: analystKeys,
    llm_provider: $("#rf-provider").value,
    quick_model: $("#rf-quick").value,
    deep_model: $("#rf-deep").value,
    bull_model: $("#rf-bull").value,
    bear_model: $("#rf-bear").value,
    schedule_kind: $("#rf-schedule-kind").value,
    schedule_expr: $("#rf-schedule-expr").value.trim() || null,
    output_policy: $("#rf-policy").value,
    conviction_threshold: Number($("#rf-conviction").value) || 7,
    max_daily_token_cost_usd: Number($("#rf-budget").value) || 5.0,
    market_hours_only: $("#rf-mho").value === "true",
  };
  try {
    const res = await fetch("/api/recipes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    $("#recipe-form").reset();
    populateRecipeFormSelects();
    await loadRecipes();
    renderRecipes();
    flash("Recipe created.");
  } catch (err) {
    $("#rf-error").textContent = String(err.message || err);
  }
}

async function recipeAction(rid, action) {
  const map = {
    trigger: { url: `/api/recipes/${rid}/trigger-now`, method: "POST" },
    pause:   { url: `/api/recipes/${rid}/pause`,       method: "POST" },
    resume:  { url: `/api/recipes/${rid}/resume`,      method: "POST" },
    kill:    { url: `/api/recipes/${rid}/kill`,        method: "POST" },
    delete:  { url: `/api/recipes/${rid}`,             method: "DELETE" },
  };
  const spec = map[action];
  if (!spec) return;
  if (action === "delete" && !confirm("Delete this recipe?")) return;
  try {
    const res = await fetch(spec.url, { method: spec.method });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    await loadRecipes();
    renderRecipes();
    if (action === "trigger") {
      flash("Recipe fired. Refresh in a few seconds to see the new session.");
      // Poll the orders + events tables in 3s to surface the post-decision hook
      // output once the runner thread completes.
      setTimeout(refreshAll, 3000);
    }
  } catch (err) {
    alert(`Failed: ${err.message || err}`);
  }
}

async function openLastSession(rid) {
  try {
    const r = await fetch(`/api/recipes/${rid}/sessions?limit=1`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const rows = await r.json();
    if (!rows.length) { flash("No sessions yet for that recipe."); return; }
    const sid = rows[0].id;
    // The session detail view lives on the home page. Open it there.
    window.open(`/#session/${sid}`, "_blank");
  } catch (err) {
    alert(`Failed: ${err.message || err}`);
  }
}

async function toggleKillSwitch() {
  const enabled = $("#kill-toggle").checked;
  try {
    const res = await fetch("/api/risk/kill-switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadLimits();
    renderLimits();
    flash(enabled ? "🔴 Kill switch engaged." : "🟢 Kill switch disengaged.");
  } catch (e) {
    $("#kill-toggle").checked = !enabled;
    alert(`Failed: ${e.message || e}`);
  }
}

async function saveRiskLimits() {
  const payload = {
    max_position_pct:        Number($("#rl-max-pos").value),
    max_daily_drawdown_pct:  Number($("#rl-max-dd").value),
    kelly_fraction_cap:      Number($("#rl-kelly").value),
    max_slippage_bps:        Number($("#rl-slip").value),
    daily_spend_cap_usd:     Number($("#rl-daily-spend").value),
    monthly_spend_cap_usd:   Number($("#rl-monthly-spend").value),
    behavioral_cooldown:     document.getElementById("bc-toggle")?.checked || false,
  };
  try {
    const res = await fetch("/api/risk/limits", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadLimits();
    renderLimits();
    flash("Risk limits saved.");
  } catch (e) { alert(`Failed: ${e.message || e}`); }
}

async function resolveOutcomes() {
  try {
    const res = await fetch("/api/paper/outcomes/resolve?limit=200", { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const r = await res.json();
    flash(`Resolved ${r.resolved} outcome(s).`);
    await loadCalibration();
    renderCalibration();
  } catch (e) { alert(`Failed: ${e.message || e}`); }
}

// ------------------------------------------------------------------
// Tiny helpers
// ------------------------------------------------------------------

function escapeHTML(s) {
  return String(s ?? "").replace(/[&<>"']/g, (ch) => (
    {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]
  ));
}

function fmtUSD(v) {
  if (v == null || isNaN(v)) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtNum(v) {
  if (v == null || isNaN(v)) return "—";
  return Number(v).toLocaleString("en-US", { maximumFractionDigits: 4 });
}

function formatRelative(ts) {
  if (!ts) return "";
  if (typeof ts === "string") ts = new Date(ts).getTime() / 1000;
  const diff = (Date.now() / 1000) - ts;
  if (diff < 60)    return "just now";
  if (diff < 3600)  return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

function flash(msg) {
  const bar = $("#phase1-flash");
  bar.textContent = msg;
  bar.classList.remove("hidden");
  setTimeout(() => bar.classList.add("hidden"), 3500);
}

// ============================================================================
// Ask the fund — Phase 2 deliverable #2
// ============================================================================

// Icon hint per template id. Decorative only — server returns the canonical
// question text + slug; the icon is a UI affordance.
const ASK_ICONS = {
  1: "📅", 2: "🥇", 3: "🎯", 4: "🤖", 5: "⏳",
  6: "🤝", 7: "💀", 8: "📝", 9: "🌡️", 10: "✨",
};

async function loadAskTemplates() {
  try {
    const r = await fetch("/api/journal/ask/templates");
    if (!r.ok) return;
    const templates = await r.json();
    renderAskButtons(templates, "ask-buttons");
    renderAskButtons(templates, "ask-buttons-journal");
  } catch (e) { console.error("loadAskTemplates", e); }
}

function renderAskButtons(templates, mountId) {
  const mount = document.getElementById(mountId);
  if (!mount) return;
  mount.innerHTML = "";
  for (const t of templates) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ask-btn";
    btn.dataset.templateId = t.template_id;
    btn.innerHTML = `<span class="ask-icon">${ASK_ICONS[t.template_id] || "•"}</span><span class="ask-q">${escapeHTML(t.question)}</span>`;
    btn.addEventListener("click", () => askFund(t.template_id, mountId.endsWith("journal") ? "-journal" : ""));
    mount.appendChild(btn);
  }
}

async function askFund(templateId, suffix = "") {
  const card = document.getElementById(`ask-answer${suffix}`);
  const q = document.getElementById(`ask-answer-question${suffix}`);
  const meta = document.getElementById(`ask-answer-meta${suffix}`);
  const body = document.getElementById(`ask-answer-body${suffix}`);
  const table = document.getElementById(`ask-answer-table${suffix}`);
  if (!card) return;
  // Loading state.
  card.classList.remove("hidden");
  q.textContent = "Asking…";
  meta.textContent = "";
  body.innerHTML = `<div class="subtle">Computing answer from your corpus…</div>`;
  table.innerHTML = "";

  try {
    const r = await fetch("/api/journal/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ template_id: templateId }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const result = await r.json();
    q.textContent = result.question;
    const confLabel = {
      ok: "deterministic",
      low: "heuristic",
      no_data: "no data yet",
    }[result.confidence] || result.confidence;
    const cta = result.cta ? ` · ${escapeHTML(result.cta)}` : "";
    meta.textContent = `${confLabel} · ${result.data_points} data point(s)${cta}`;
    body.innerHTML = renderMarkdown(result.markdown || "");
    if (result.table && result.table.length) {
      table.innerHTML = renderTable(result.table);
    }
  } catch (err) {
    q.textContent = "Couldn't answer";
    body.innerHTML = `<div class="phase1-error">${escapeHTML(String(err.message || err))}</div>`;
  }
}

function clearAskAnswer(suffix = "") {
  const card = document.getElementById(`ask-answer${suffix}`);
  if (card) card.classList.add("hidden");
}

// Minimal markdown rendering. We only need: paragraphs, lists, h-tags,
// strong, em, blockquotes, code. No HTML escaping required because we
// pre-escape any user content the server inlined; the server only emits
// markdown that came from internal templating, never raw user-text.
function renderMarkdown(md) {
  if (!md) return "";
  const escaped = escapeHTML(md);
  const lines = escaped.split("\n");
  const out = [];
  let inList = false;
  for (const raw of lines) {
    const line = raw.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
                    .replace(/`([^`]+?)`/g, "<code>$1</code>")
                    .replace(/_([^_]+?)_/g, "<em>$1</em>");
    if (line.startsWith("### ")) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(`<h4>${line.slice(4)}</h4>`);
    } else if (line.startsWith("## ")) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(`<h3>${line.slice(3)}</h3>`);
    } else if (line.startsWith("> ")) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(`<blockquote>${line.slice(2)}</blockquote>`);
    } else if (line.startsWith("- ")) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${line.slice(2)}</li>`);
    } else if (line.trim() === "") {
      if (inList) { out.push("</ul>"); inList = false; }
    } else {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(`<p>${line}</p>`);
    }
  }
  if (inList) out.push("</ul>");
  return out.join("\n");
}

function renderTable(rows) {
  if (!rows || !rows.length) return "";
  const cols = Object.keys(rows[0]);
  const th = cols.map((c) => `<th>${escapeHTML(prettyCol(c))}</th>`).join("");
  const tr = rows.map(
    (r) => "<tr>" + cols.map((c) => `<td>${escapeHTML(formatCell(r[c]))}</td>`).join("") + "</tr>"
  ).join("");
  return `<table class="phase1-table ask-table"><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
}

function prettyCol(name) {
  return String(name).replace(/_/g, " ");
}

function formatCell(v) {
  if (v == null) return "—";
  if (typeof v === "number") return v.toLocaleString("en-US", { maximumFractionDigits: 4 });
  return String(v);
}

// =========================================================================
// Backtest (Phase 3)
// =========================================================================

async function submitBacktest(ev) {
  ev.preventDefault();
  const btn = $("#bt-run-btn");
  const status = $("#bt-status");
  btn.disabled = true;
  status.textContent = "Running…";
  const payload = {
    ticker: $("#bt-ticker").value.trim().toUpperCase(),
    from_date: $("#bt-from").value,
    to_date: $("#bt-to").value,
    starting_cash: parseFloat($("#bt-cash").value || "100000"),
    kelly_cap: parseFloat($("#bt-kelly").value || "0.10"),
  };
  try {
    const r = await fetch("/api/backtest/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      const err = await r.text();
      status.textContent = `Failed: ${err.slice(0, 200)}`;
      return;
    }
    const data = await r.json();
    status.textContent = `Done. ${data.total_decisions} decisions, ${data.closed_trades} trades.`;
    renderBacktestResult(data);
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

function renderBacktestResult(data) {
  const card = $("#bt-result-card");
  card.hidden = false;
  $("#bt-result-symbol").textContent = `${data.symbol}: ${data.from_date} → ${data.to_date}`;

  const growth = ((data.final_nav - data.starting_cash) / data.starting_cash) * 100;
  const kpis = [
    { k: "Final NAV", v: `$${formatNumber(data.final_nav, 2)}` },
    { k: "Growth", v: `${growth >= 0 ? "+" : ""}${growth.toFixed(2)}%` },
    { k: "Trades", v: String(data.closed_trades) },
    { k: "Hit rate", v: `${(data.hit_rate * 100).toFixed(1)}%` },
    { k: "Brier", v: data.brier.toFixed(4) },
    { k: "Max DD", v: `${(data.max_drawdown_pct * 100).toFixed(1)}%` },
  ];
  $("#bt-kpis").innerHTML = kpis.map(
    (kv) => `<div class="kpi"><div class="kpi-label">${kv.k}</div><div class="kpi-value">${kv.v}</div></div>`
  ).join("");

  drawEquityCurve(data.equity_curve || []);

  const trades = (data.trades || []).slice(-10);
  $("#bt-trades-body").innerHTML = trades.length === 0
    ? `<tr><td colspan="7" class="subtle">No closed trades in window.</td></tr>`
    : trades.map((t) => `
        <tr>
          <td>${escapeHTML(t.entry_date)}</td>
          <td>${escapeHTML(t.exit_date)}</td>
          <td>${formatNumber(t.qty, 2)}</td>
          <td>${formatNumber(t.entry_price, 2)}</td>
          <td>${formatNumber(t.exit_price, 2)}</td>
          <td>${t.realized_return_pct >= 0 ? "+" : ""}${t.realized_return_pct.toFixed(2)}%</td>
          <td>${escapeHTML(t.reason)}</td>
        </tr>`).join("");
}

function drawEquityCurve(curve) {
  const svg = $("#bt-equity-svg");
  svg.innerHTML = "";
  if (!curve.length) {
    svg.innerHTML = `<text x="300" y="90" text-anchor="middle" fill="#666">no data</text>`;
    return;
  }
  const W = 600, H = 180, padX = 24, padY = 16;
  const navs = curve.map((p) => p.nav);
  const lo = Math.min(...navs);
  const hi = Math.max(...navs);
  const span = (hi - lo) || 1;
  const step = (W - 2 * padX) / Math.max(curve.length - 1, 1);
  let d = "";
  curve.forEach((p, i) => {
    const x = padX + i * step;
    const y = padY + (H - 2 * padY) * (1 - (p.nav - lo) / span);
    d += (i === 0 ? `M${x.toFixed(1)},${y.toFixed(1)}` : ` L${x.toFixed(1)},${y.toFixed(1)}`);
  });
  const first = navs[0], last = navs[navs.length - 1];
  const color = last >= first ? "#5fd75f" : "#ff6b6b";
  svg.innerHTML = `
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.8"/>
    <text x="${padX}" y="${padY - 2}" font-size="10" fill="#888">$${formatNumber(hi, 0)}</text>
    <text x="${padX}" y="${H - 2}" font-size="10" fill="#888">$${formatNumber(lo, 0)}</text>
  `;
}

function formatNumber(v, dp = 2) {
  if (v == null || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString("en-US", {
    minimumFractionDigits: dp, maximumFractionDigits: dp,
  });
}

// =========================================================================
// Streaming fires panel (Phase 3)
// =========================================================================

async function loadStreamingEvents() {
  try {
    const r = await fetch("/api/streaming/events?limit=20");
    if (r.ok) {
      const data = await r.json();
      state.streamingEvents = data.events || [];
    }
  } catch (e) { console.error(e); }
}

async function refreshStreamingEvents() {
  await loadStreamingEvents();
  renderStreamingEvents();
}

function renderStreamingEvents() {
  const rows = state.streamingEvents || [];
  const tbody = $("#streaming-rows");
  const count = $("#streaming-count");
  if (count) count.textContent = `(${rows.length})`;
  if (!tbody) return;
  if (rows.length === 0) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="4" class="subtle">No streaming fires yet.</td></tr>`;
    return;
  }
  const recipeName = (rid) => {
    const r = (state.recipes || []).find((x) => x.id === rid);
    return r ? r.name : (rid ? rid.slice(0, 8) + "…" : "—");
  };
  tbody.innerHTML = rows.map((ev) => `
    <tr>
      <td>${escapeHTML(formatTime(ev.created_at))}</td>
      <td>${escapeHTML(ev.symbol || "—")}</td>
      <td>${escapeHTML(recipeName(ev.recipe_id))}</td>
      <td class="subtle">${escapeHTML(ev.reason || "")}</td>
    </tr>`).join("");
}

function formatTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    // Compact "MM-DD HH:MM:SS" in UTC
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  } catch { return String(iso); }
}

// =========================================================================
// Onboarding — Advanced disclosure + zero-state first-thesis
// =========================================================================

const ADVANCED_PREF_KEY = "aw.thesis.advancedOpen";

function initAdvancedThesisForm() {
  const open = localStorage.getItem(ADVANCED_PREF_KEY) === "1";
  setAdvancedThesisFormState(open);
}

function toggleAdvancedThesisForm() {
  const open = $("#rf-advanced")?.hasAttribute("hidden");
  setAdvancedThesisFormState(open);
  localStorage.setItem(ADVANCED_PREF_KEY, open ? "1" : "0");
}

function setAdvancedThesisFormState(open) {
  const group = $("#rf-advanced");
  const btn = $("#rf-advanced-toggle");
  if (!group || !btn) return;
  group.hidden = !open;
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  const arrow = btn.querySelector(".advanced-arrow");
  if (arrow) arrow.textContent = open ? "▾" : "▸";
}

// =========================================================================
// Overview hero — live multi-agent session launcher (the well-lit path)
// =========================================================================
//
// State machine:
//   idle      — input + Start
//   running   — WS-streamed activity feed
//   complete  — final decision + Save-as-recipe / Analyze-another
//
// Always visible on Overview. Same primitive (`POST /api/sessions`) that the
// legacy /analyze page uses, but rendered inline so the user never has to
// navigate or go hunting for the result.

const hero = {
  state: "idle",
  ws: null,
  session: null,
  agents: {},           // agent → {status, preview}
  reportSections: {},
  ticker: null,
  sessionId: null,
};

function setHeroState(next) {
  hero.state = next;
  for (const id of ["hero-idle", "hero-running", "hero-complete"]) {
    const el = document.getElementById(id);
    if (el) el.hidden = el.id !== `hero-${next}`;
  }
}

function resetHeroToIdle() {
  closeHeroWS();
  hero.session = null;
  hero.agents = {};
  hero.reportSections = {};
  hero.sessionId = null;
  const feed = $("#hero-feed");
  if (feed) feed.innerHTML = "";
  const err = $("#hero-error");
  if (err) err.textContent = "";
  const save = $("#hero-save-status");
  if (save) save.textContent = "";
  setHeroState("idle");
}

function closeHeroWS() {
  if (hero.ws) { try { hero.ws.close(); } catch (_) {} hero.ws = null; }
}

async function startHeroSession() {
  const ticker = ($("#hero-ticker")?.value || "").trim().toUpperCase();
  const err = $("#hero-error");
  if (!ticker) {
    if (err) err.textContent = "Please enter a ticker.";
    return;
  }
  // Guard: only block when Supabase is actually configured AND the user hasn't
  // signed in. In dev / guest mode (`isConfigured === false`) the server
  // accepts anonymous calls, so we fall through.
  const auth = window.AgenticWhalesAuth;
  if (auth?.isConfigured && !auth.getAccessToken?.()) {
    if (err) {
      err.innerHTML = `Sign in to run an analysis. <button type="button" id="hero-signin" class="ghost-btn" style="margin-left:0.5rem">Sign in with Google →</button>`;
      $("#hero-signin")?.addEventListener("click", () => auth.signInWithGoogle?.());
    }
    return;
  }
  if (err) err.textContent = "";

  if (!state.config) { try { await loadConfig(); } catch (_) {} }
  const defaults = state.config?.defaults || {};
  const provider = defaults.provider || "google";
  const payload = {
    ticker,
    analysis_date: new Date().toISOString().slice(0, 10),
    llm_provider: provider,
    quick_think_llm: defaults.quick_model || "gemini-3-flash-preview",
    deep_think_llm: defaults.deep_model || "gemini-3.1-pro-preview",
    research_depth: 1,
    analysts: ["market", "quant", "news"],
    output_language: "English",
  };

  hero.ticker = ticker;
  setHeroState("running");
  $("#hero-running-ticker").textContent = ticker;
  $("#hero-progress-text").textContent = "Spinning up agents…";
  $("#hero-feed").innerHTML = "";
  hero.agents = {};
  hero.reportSections = {};

  try {
    const res = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      // FastAPI HTTPException renders `detail` either as a string or as the
      // structured dict the route handed in. Compliance gating uses the dict
      // shape; the fetch interceptor at the top of the file already pops the
      // attestation modal for 412 + code=compliance_required, so we just go
      // quietly back to idle and let the modal lead the recovery.
      const det = errBody.detail;
      if (res.status === 412 && det && typeof det === "object" && det.code === "compliance_required") {
        setHeroState("idle");
        return;
      }
      const msg = (typeof det === "string" && det)
        || det?.message
        || errBody.message
        || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    const summary = await res.json();
    hero.sessionId = summary.id;
    // Cache hit? Skip straight to completed state by fetching the full session.
    if (summary.cached || ["completed", "failed", "cancelled"].includes(summary.status)) {
      try {
        const full = await fetch(`/api/sessions/${summary.id}`);
        const session = full.ok ? await full.json() : { id: summary.id, status: summary.status };
        hero.session = session;
        renderHeroComplete();
        return;
      } catch (_) { /* fall through to WS */ }
    }
    connectHeroWS(summary.id);
  } catch (e) {
    if (err) err.textContent = String(e.message || e);
    setHeroState("idle");
  }
}

function connectHeroWS(id) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const token = window.AgenticWhalesAuth?.getAccessToken?.() || "";
  const tokenQS = token ? `?token=${encodeURIComponent(token)}` : "";
  const ws = new WebSocket(`${proto}//${location.host}/api/sessions/${id}/stream${tokenQS}`);
  hero.ws = ws;
  ws.onmessage = (e) => {
    let event;
    try { event = JSON.parse(e.data); } catch (_) { return; }
    handleHeroEvent(event);
  };
  ws.onerror = () => { /* swallow; onclose follows */ };
  ws.onclose = () => { if (hero.ws === ws) hero.ws = null; };
}

function handleHeroEvent(event) {
  if (event.type === "session") {
    hero.session = event.session;
    // Seed agent + report state from snapshot.
    Object.assign(hero.agents, hero.agents, mapAgentStatus(event.session.agent_status || {}));
    Object.assign(hero.reportSections, event.session.report_sections || {});
    renderHeroFeed();
    if (["completed", "failed", "cancelled"].includes(event.session.status)) {
      closeHeroWS();
      renderHeroComplete();
    }
    return;
  }
  if (event.type === "agent_status") {
    hero.agents[event.agent] = { ...(hero.agents[event.agent] || {}), status: event.status };
    renderHeroFeed();
    return;
  }
  if (event.type === "report") {
    hero.reportSections[event.section] = event.content;
    if (event.agent) {
      hero.agents[event.agent] = { ...(hero.agents[event.agent] || {}), preview: previewMarkdown(event.content) };
    }
    renderHeroFeed();
    return;
  }
}

function mapAgentStatus(byAgent) {
  const out = {};
  for (const [agent, status] of Object.entries(byAgent)) {
    out[agent] = { status };
  }
  return out;
}

function renderHeroFeed() {
  const feed = $("#hero-feed");
  if (!feed) return;
  // Order: Analysts → Bull/Bear/Research Manager → Trader → Risk team → Portfolio Manager.
  const order = [
    "Market Analyst", "Quant Analyst", "Social Analyst", "News Analyst", "Fundamentals Analyst",
    "Bull Researcher", "Bear Researcher", "Research Manager",
    "Trader",
    "Aggressive Analyst", "Neutral Analyst", "Conservative Analyst",
    "Portfolio Manager",
  ];
  const rows = [];
  const counted = new Set();
  for (const agent of order) {
    const data = hero.agents[agent];
    if (!data) continue;
    counted.add(agent);
    rows.push(heroFeedRow(agent, data));
  }
  // Any agent the server reports that we don't have a slot for, append.
  for (const [agent, data] of Object.entries(hero.agents)) {
    if (counted.has(agent)) continue;
    rows.push(heroFeedRow(agent, data));
  }
  feed.innerHTML = rows.join("");

  // Progress text — running count + completed count.
  const total = Object.keys(hero.agents).length || 1;
  const done = Object.values(hero.agents).filter((a) => a.status === "completed").length;
  const running = Object.values(hero.agents).filter((a) => a.status === "running").length;
  const txt = $("#hero-progress-text");
  if (txt) {
    if (running) txt.textContent = `${done}/${total} agents done · ${running} working…`;
    else if (done < total) txt.textContent = `${done}/${total} agents done…`;
    else txt.textContent = `Finalizing decision…`;
  }
}

function heroFeedRow(agent, data) {
  const status = data.status || "pending";
  const icon = status === "completed" ? "✓"
              : status === "running"   ? "●"
              : status === "failed"    ? "✕"
                                       : "○";
  const cls = `hero-feed-row status-${status}`;
  const preview = data.preview
    ? `<div class="hero-feed-preview">${escapeHTML(data.preview)}</div>`
    : "";
  return `
    <li class="${cls}">
      <div class="hero-feed-head">
        <span class="hero-feed-icon">${icon}</span>
        <span class="hero-feed-agent">${escapeHTML(agent)}</span>
        <span class="hero-feed-status subtle">${escapeHTML(status)}</span>
      </div>
      ${preview}
    </li>`;
}

function previewMarkdown(md) {
  if (!md) return "";
  const stripped = String(md).replace(/[#*_`>]/g, "").replace(/\s+/g, " ").trim();
  return stripped.length > 220 ? stripped.slice(0, 220) + "…" : stripped;
}

function renderHeroComplete() {
  setHeroState("complete");
  const s = hero.session || {};
  $("#hero-complete-ticker").textContent = hero.ticker || s.ticker || "";
  $("#hero-complete-status").textContent = s.status || "completed";

  // Extract the Portfolio Manager decision from the final_trade_decision section.
  // Server stores structured PM output on the session as `pm_decision` (JSON).
  const pm = s.pm_decision || s.portfolio_decision || {};
  const rating = pm.rating || "—";
  $("#hero-rating").textContent = rating;
  $("#hero-rating").dataset.rating = (rating || "").toLowerCase();

  const fmtPct = (v) => (v == null ? "—" : (Number(v) > 0 ? "+" : "") + Number(v).toFixed(2) + "%");
  const fmtProb = (v) => (v == null ? "—" : (Number(v) * 100).toFixed(0) + "%");

  $("#hero-er").textContent = fmtPct(pm.expected_return_pct);
  $("#hero-pop").textContent = fmtProb(pm.prob_of_profit);
  $("#hero-hold").textContent = pm.expected_hold_days != null ? `${pm.expected_hold_days}d` : "—";
  $("#hero-conviction").textContent = pm.conviction_score != null ? `${pm.conviction_score}/10` : (pm.confidence != null ? `${pm.confidence}/10` : "—");

  const thesisMd = pm.investment_thesis || pm.executive_summary || hero.reportSections.final_trade_decision || "(no narrative emitted)";
  const thesisEl = $("#hero-thesis");
  // Render markdown if marked is on the page (loaded via CDN in fund.html);
  // fall back to plain text + pre-wrap when offline / blocked. The CSS pane
  // is styled for either path.
  if (typeof marked !== "undefined") {
    try {
      thesisEl.classList.add("markdown");
      thesisEl.innerHTML = marked.parse(thesisMd.slice(0, 4000));
    } catch (_) {
      thesisEl.classList.remove("markdown");
      thesisEl.textContent = thesisMd.slice(0, 4000);
    }
  } else {
    thesisEl.classList.remove("markdown");
    thesisEl.textContent = thesisMd.slice(0, 4000);
  }
  // The "See full analysis in Analyses" button uses hash navigation rather
  // than an href so leave it alone here — the click handler reads hero.sessionId.
  const saveStatus = $("#hero-save-status");
  if (saveStatus) saveStatus.textContent = "";
}

async function cancelHeroSession() {
  if (!hero.sessionId) {
    resetHeroToIdle();
    return;
  }
  try {
    await fetch(`/api/sessions/${hero.sessionId}/cancel`, { method: "POST" });
  } catch (_) { /* ignore */ }
  closeHeroWS();
  resetHeroToIdle();
}

async function saveHeroAsRecipe() {
  const btn = $("#hero-save-btn");
  const status = $("#hero-save-status");
  if (!hero.session) {
    if (status) status.textContent = "No session to save.";
    return;
  }
  btn.disabled = true;
  if (status) status.textContent = "Saving…";
  const cfg = hero.session.config || {};
  const ticker = hero.ticker || hero.session.ticker || "";
  // Pick a heterogeneous Bull/Bear pair. Bull always = deep_model of the
  // session's primary provider. Bear must come from a different family or
  // the recipe-create endpoint rejects it (validate_heterogeneity).
  const bull = cfg.deep_think_llm || cfg.deep_model;
  let bear = null;
  const ALT_FAMILY_FALLBACK = "deepseek-v4-pro";
  try {
    const providers = state.config?.providers || [];
    const other = providers.find((p) => p.key !== (cfg.llm_provider || "google"));
    if (other) {
      const m = state.config?.models?.[other.key] || {};
      const deepList = m.deep || m.deep_models || [];
      // model entries are [label, id] tuples; sometimes strings; sometimes {id}.
      const pick = (entry) => {
        if (!entry) return null;
        if (typeof entry === "string") return entry;
        if (Array.isArray(entry)) return entry[1];
        if (typeof entry === "object" && entry.id) return entry.id;
        return null;
      };
      bear = pick(deepList[0]);
    }
  } catch (_) {}
  if (!bear) bear = ALT_FAMILY_FALLBACK;

  const payload = {
    name: `${ticker} — saved from analysis`,
    tickers: [ticker],
    analysts: cfg.analysts || ["market", "quant", "news"],
    llm_provider: cfg.llm_provider || "google",
    quick_model: cfg.quick_think_llm || cfg.quick_model || "gemini-3-flash-preview",
    deep_model: cfg.deep_think_llm || cfg.deep_model || "gemini-3.1-pro-preview",
    bull_model: bull,
    bear_model: bear,
    schedule_kind: "manual",
    output_policy: "notify",
    conviction_threshold: 7,
    max_daily_token_cost_usd: 5.0,
    market_hours_only: true,
  };
  try {
    const res = await fetch("/api/recipes", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.detail || `HTTP ${res.status}`);
    }
    const recipe = await res.json();
    if (status) status.textContent = `Saved as recurring thesis ✓ (id: ${(recipe.id || "").slice(0, 8)}…)`;
  } catch (e) {
    if (status) status.textContent = `Save failed: ${e.message || e}`;
  } finally {
    btn.disabled = false;
  }
}

// =========================================================================
// Schedule modal — opens from the verdict-card Schedule button. Lets the
// user persist the current ticker + agent lineup as a recurring recipe
// with one of four cadences: daily / weekly / monthly / custom cron.
// POSTs the resulting Recipe to /api/recipes.
// =========================================================================

function initScheduleModalControls() {
  // Cancel buttons (cross + ghost) just hide the modal.
  document.getElementById("schedule-cancel-x")?.addEventListener("click", closeScheduleModal);
  document.getElementById("schedule-cancel")?.addEventListener("click", closeScheduleModal);
  // Backdrop dismiss.
  document.getElementById("schedule-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "schedule-modal") closeScheduleModal();
  });
  // Radio change → swap which "detail" sub-panel is visible.
  document.querySelectorAll('input[name="schedule-kind"]').forEach((r) => {
    r.addEventListener("change", () => syncScheduleDetail(r.value));
  });
  document.getElementById("schedule-submit")?.addEventListener("click", submitSchedule);
}

function openScheduleModal() {
  if (!hero.session) {
    const status = $("#hero-save-status");
    if (status) status.textContent = "No session to schedule.";
    return;
  }
  const ticker = hero.ticker || hero.session.ticker || "—";
  const tickerLabel = document.getElementById("schedule-ticker-label");
  if (tickerLabel) tickerLabel.textContent = ticker;
  const nameInput = document.getElementById("schedule-name");
  if (nameInput && !nameInput.value) nameInput.value = `${ticker} — scheduled debate`;
  const errEl = document.getElementById("schedule-error");
  if (errEl) errEl.textContent = "";
  // Reset to daily by default.
  const dailyRadio = document.querySelector('input[name="schedule-kind"][value="daily"]');
  if (dailyRadio) dailyRadio.checked = true;
  syncScheduleDetail("daily");
  document.getElementById("schedule-modal")?.classList.remove("hidden");
}

function closeScheduleModal() {
  document.getElementById("schedule-modal")?.classList.add("hidden");
}

function syncScheduleDetail(kind) {
  for (const k of ["daily", "weekly", "monthly", "cron"]) {
    const el = document.getElementById(`schedule-detail-${k}`);
    if (el) el.hidden = k !== kind;
  }
}

// Translate the modal inputs into the scheduler's (kind, expression) pair.
// daily/weekly/monthly map to cron expressions under the hood — the backend
// only needs to understand "cron" + a valid 5-field expression.
function buildScheduleSpec() {
  const kind = (document.querySelector('input[name="schedule-kind"]:checked')?.value) || "daily";
  if (kind === "daily") {
    const t = (document.getElementById("schedule-time-daily")?.value || "13:30").split(":");
    const h = Number(t[0] ?? "13");
    const m = Number(t[1] ?? "30");
    return { kind: "cron", expr: `${m} ${h} * * 1-5`, label: `daily ${pad2(h)}:${pad2(m)} UTC (weekdays)` };
  }
  if (kind === "weekly") {
    const dow = document.getElementById("schedule-weekday")?.value || "1";
    const t = (document.getElementById("schedule-time-weekly")?.value || "13:30").split(":");
    const h = Number(t[0] ?? "13");
    const m = Number(t[1] ?? "30");
    return { kind: "cron", expr: `${m} ${h} * * ${dow}`, label: `weekly on day-of-week ${dow} at ${pad2(h)}:${pad2(m)} UTC` };
  }
  if (kind === "monthly") {
    const dom = Math.max(1, Math.min(28, Number(document.getElementById("schedule-day-monthly")?.value || 1)));
    const t = (document.getElementById("schedule-time-monthly")?.value || "13:30").split(":");
    const h = Number(t[0] ?? "13");
    const m = Number(t[1] ?? "30");
    return { kind: "cron", expr: `${m} ${h} ${dom} * *`, label: `monthly on day ${dom} at ${pad2(h)}:${pad2(m)} UTC` };
  }
  // cron
  const expr = (document.getElementById("schedule-cron-expr")?.value || "").trim();
  return { kind: "cron", expr, label: expr ? `cron: ${expr}` : "" };
}

function pad2(n) { return String(n).padStart(2, "0"); }

// Cron sanity check — five whitespace-separated fields, no other validation.
// Backend does the full parse via croniter, this is just a UX guard.
function looksLikeCron(s) {
  return /^\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s*$/.test(s || "");
}

async function submitSchedule() {
  const errEl = document.getElementById("schedule-error");
  const submitBtn = document.getElementById("schedule-submit");
  if (errEl) errEl.textContent = "";
  if (!hero.session) {
    if (errEl) errEl.textContent = "No session to schedule.";
    return;
  }
  const spec = buildScheduleSpec();
  if (!spec.expr || !looksLikeCron(spec.expr)) {
    if (errEl) errEl.textContent = "Cron expression must have 5 whitespace-separated fields (e.g. \"0 13 * * 1-5\").";
    return;
  }
  const cfg = hero.session.config || {};
  const ticker = hero.ticker || hero.session.ticker || "";
  const name = (document.getElementById("schedule-name")?.value || "").trim()
    || `${ticker} — scheduled`;
  // Heterogeneous Bull/Bear pair (same fallback as the old save flow).
  const bull = cfg.deep_think_llm || cfg.deep_model;
  let bear = null;
  const ALT_FAMILY_FALLBACK = "deepseek-v4-pro";
  try {
    const providers = state.config?.providers || [];
    const other = providers.find((p) => p.key !== (cfg.llm_provider || "google"));
    if (other) {
      const m = state.config?.models?.[other.key] || {};
      const deepList = m.deep || m.deep_models || [];
      const pick = (entry) => {
        if (!entry) return null;
        if (typeof entry === "string") return entry;
        if (Array.isArray(entry)) return entry[1];
        if (typeof entry === "object" && entry.id) return entry.id;
        return null;
      };
      bear = pick(deepList[0]);
    }
  } catch (_) {}
  if (!bear) bear = ALT_FAMILY_FALLBACK;

  const payload = {
    name,
    tickers: [ticker],
    analysts: cfg.analysts || ["market", "quant", "news"],
    llm_provider: cfg.llm_provider || "google",
    quick_model: cfg.quick_think_llm || cfg.quick_model || "gemini-3-flash-preview",
    deep_model: cfg.deep_think_llm || cfg.deep_model || "gemini-3.1-pro-preview",
    bull_model: bull,
    bear_model: bear,
    schedule_kind: spec.kind,
    schedule_expr: spec.expr,
    output_policy: "notify",
    conviction_threshold: 7,
    max_daily_token_cost_usd: 5.0,
    market_hours_only: true,
  };
  if (submitBtn) submitBtn.disabled = true;
  try {
    const res = await fetch("/api/recipes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      const det = j.detail;
      const msg = (typeof det === "string" && det) || det?.message || j.message || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    const recipe = await res.json();
    closeScheduleModal();
    // Echo confirmation on the verdict card so the user knows it landed.
    const status = $("#hero-save-status");
    if (status) status.textContent = `Scheduled ✓ ${spec.label} (id ${(recipe.id || "").slice(0, 8)}…)`;
  } catch (e) {
    if (errEl) errEl.textContent = `Couldn't schedule: ${e.message || e}`;
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

// =========================================================================
// Phase 3 #4 — Conviction-decay timeseries (charted on Decisions tab)
// =========================================================================

function _ensureConvictionState() {
  // Defensive: init lazily so a syntax/runtime error elsewhere in the module
  // doesn't prevent the chart from working. Idempotent.
  if (!state.conviction) {
    state.conviction = { series: [], selectedTicker: null, halfLifeDays: 5 };
  }
}

async function loadConvictionSeries() {
  _ensureConvictionState();
  const halfLife = Number(document.querySelector("#conv-half-life")?.value || 5);
  const ticker = state.conviction.selectedTicker;
  const qs = new URLSearchParams({ half_life_days: String(halfLife), limit: "200" });
  if (ticker) qs.set("ticker", ticker);
  try {
    const r = await fetch(`/api/paper/conviction/timeseries?${qs}`);
    if (!r.ok) return;
    const data = await r.json();
    state.conviction.series = data.points || [];
    state.conviction.halfLifeDays = halfLife;
  } catch (e) {
    console.error("loadConvictionSeries", e);
  }
}

function renderConvictionChart() {
  _ensureConvictionState();
  // Ticker chip row — built from the orders we already have.
  const tickerSet = new Set();
  for (const o of (state.orders || [])) if (o.ticker) tickerSet.add(o.ticker);
  const tickerRow = document.querySelector("#conviction-tickers");
  if (tickerRow) {
    const tickers = ["All", ...Array.from(tickerSet).sort()];
    tickerRow.innerHTML = tickers.map((t) => {
      const active = (t === "All" && !state.conviction.selectedTicker)
        || t === state.conviction.selectedTicker;
      return `<button type="button" class="chip ${active ? "active" : ""}" data-conv-ticker="${escapeHTML(t)}">${escapeHTML(t)}</button>`;
    }).join("");
    tickerRow.querySelectorAll("[data-conv-ticker]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.conviction.selectedTicker = btn.dataset.convTicker === "All" ? null : btn.dataset.convTicker;
        loadConvictionSeries().then(renderConvictionChart);
      });
    });
  }

  const svg = document.querySelector("#conviction-svg");
  const hint = document.querySelector("#conviction-hint");
  if (!svg) return;
  const pts = state.conviction.series || [];
  if (!pts.length) {
    svg.innerHTML = "";
    if (hint) hint.style.display = "";
    return;
  }
  if (hint) hint.style.display = "none";

  const W = 720, H = 200, padX = 28, padY = 16;
  const xs = pts.map((p) => new Date(p.ts).getTime());
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const xSpan = (xMax - xMin) || 1;
  const yMin = 0, yMax = 10; // conviction scale
  const scaleX = (ts) => padX + ((ts - xMin) / xSpan) * (W - 2 * padX);
  const scaleY = (v) => padY + (H - 2 * padY) * (1 - (v - yMin) / (yMax - yMin));

  const rawD = pts.map((p, i) => {
    const x = scaleX(new Date(p.ts).getTime()).toFixed(1);
    const y = scaleY(p.raw_score).toFixed(1);
    return (i === 0 ? "M" : "L") + x + "," + y;
  }).join(" ");
  const decayedD = pts.map((p, i) => {
    const x = scaleX(new Date(p.ts).getTime()).toFixed(1);
    const y = scaleY(p.decayed_score).toFixed(1);
    return (i === 0 ? "M" : "L") + x + "," + y;
  }).join(" ");

  // Axis labels: 0, 5, 10 on Y; oldest + newest dates on X.
  const yLabels = [0, 5, 10].map((v) => {
    const y = scaleY(v).toFixed(1);
    return `<text x="4" y="${y}" font-size="10" fill="#666" alignment-baseline="middle">${v}</text>`;
  }).join("");
  const xMinLabel = new Date(xMin).toISOString().slice(0, 10);
  const xMaxLabel = new Date(xMax).toISOString().slice(0, 10);

  svg.innerHTML = `
    ${yLabels}
    <line x1="${padX}" y1="${scaleY(5).toFixed(1)}" x2="${W - padX}" y2="${scaleY(5).toFixed(1)}" stroke="#1e2632" stroke-dasharray="3,4" />
    <path d="${rawD}" fill="none" stroke="#6ec3ff" stroke-width="1" stroke-dasharray="3,3" opacity="0.55" />
    <path d="${decayedD}" fill="none" stroke="#5fd75f" stroke-width="1.8" />
    <text x="${padX}" y="${H - 2}" font-size="10" fill="#888">${xMinLabel}</text>
    <text x="${W - padX}" y="${H - 2}" font-size="10" fill="#888" text-anchor="end">${xMaxLabel}</text>
  `;
}

// Hook up the half-life input + initial paint after refreshAll() runs.
document.addEventListener("DOMContentLoaded", () => {
  const hl = document.querySelector("#conv-half-life");
  if (hl) hl.addEventListener("change", () => loadConvictionSeries().then(renderConvictionChart));
});

// =========================================================================
// Battle arena — anime character roster + live fight visuals
// =========================================================================
//
// The arena re-uses the WebSocket pipeline that already feeds renderHeroFeed.
// On each event we:
//   1. Refresh the legacy feed list (paper-trail, hidden) — unchanged.
//   2. Diff hero.agents against hero.lastSeen to detect *transitions*
//      (pending → running, new preview chunk arrived) and replay them
//      as character strikes / speech-bubble pops.
//   3. Rewire the Bull/Bear fighter portraits, the supporting bench, the
//      tug-of-war marker and the round indicator.

const ANIMA_ROSTER = {
  // Bull/Bear are the headliners — large portraits, drive the tug.
  "Bull Researcher":      { side: "bull", pose: "bull",        role: "Charging bull"   },
  "Bear Researcher":      { side: "bear", pose: "bear",        role: "Shadow stalker"  },
  // Analysts: feed Bull (left bench).
  "Market Analyst":       { side: "left", pose: "market",      role: "Chart hawk"      },
  "Quant Analyst":        { side: "left", pose: "quant",       role: "Number witch"    },
  "Social Analyst":       { side: "left", pose: "social",      role: "Vibe scout"      },
  "News Analyst":         { side: "left", pose: "news",        role: "Field reporter"  },
  "Fundamentals Analyst": { side: "left", pose: "fundamentals",role: "Ledger monk"     },
  "Research Manager":     { side: "left", pose: "research",    role: "Sensei"          },
  // Risk team + Trader: right bench.
  "Aggressive Analyst":   { side: "right", pose: "aggressive", role: "Blade champion"  },
  "Neutral Analyst":      { side: "right", pose: "neutral",    role: "Scale keeper"    },
  "Conservative Analyst": { side: "right", pose: "conservative",role: "Shield warden"  },
  "Trader":               { side: "right", pose: "trader",     role: "Floor runner"    },
  // The closer.
  "Portfolio Manager":    { side: "pm",   pose: "pm",          role: "The verdict"     },
};

const ANIMA_PALETTE = {
  bull:         { aura: "#34d399", skin: "#fde4c0", hair: "#fef3c7", body: "#f5f5f5", trim: "#10b981" },
  bear:         { aura: "#f87171", skin: "#dfe5f0", hair: "#1f2937", body: "#374151", trim: "#dc2626" },
  market:       { aura: "#60a5fa", skin: "#fde4c0", hair: "#0ea5e9", body: "#1e293b", trim: "#38bdf8" },
  quant:        { aura: "#a78bfa", skin: "#fde4c0", hair: "#6d28d9", body: "#312e81", trim: "#c4b5fd" },
  social:       { aura: "#f472b6", skin: "#fde4c0", hair: "#ec4899", body: "#4c1d95", trim: "#fb7185" },
  news:         { aura: "#fbbf24", skin: "#fde4c0", hair: "#92400e", body: "#1f2937", trim: "#f59e0b" },
  fundamentals: { aura: "#84cc16", skin: "#fde4c0", hair: "#3f6212", body: "#1f2937", trim: "#65a30d" },
  research:     { aura: "#fcd34d", skin: "#fde4c0", hair: "#fff",    body: "#1e1b4b", trim: "#fbbf24" },
  aggressive:   { aura: "#ef4444", skin: "#fde4c0", hair: "#7f1d1d", body: "#450a0a", trim: "#dc2626" },
  neutral:      { aura: "#60a5fa", skin: "#fde4c0", hair: "#1e3a8a", body: "#1e293b", trim: "#3b82f6" },
  conservative: { aura: "#22c55e", skin: "#fde4c0", hair: "#14532d", body: "#052e16", trim: "#16a34a" },
  trader:       { aura: "#fb923c", skin: "#fde4c0", hair: "#9a3412", body: "#1f2937", trim: "#f97316" },
  pm:           { aura: "#a78bfa", skin: "#fde4c0", hair: "#3b0764", body: "#1e1b4b", trim: "#c084fc" },
};

// Per-pose accessory rendering — keeps each character visually distinct.
const ANIMA_ACCESSORY = {
  bull:         (p) => `<path d="M28 36 Q22 22 30 22 Q34 28 40 32 Z" fill="${p.body}" stroke="${p.trim}" stroke-width="1.5"/><path d="M92 36 Q98 22 90 22 Q86 28 80 32 Z" fill="${p.body}" stroke="${p.trim}" stroke-width="1.5"/>`,  // horns
  bear:         (p) => `<path d="M30 48 Q34 18 60 16 Q86 18 90 48 Q90 36 60 32 Q30 36 30 48" fill="${p.hair}" stroke="${p.trim}" stroke-width="1.5"/>`,  // hood
  market:       (p) => `<rect x="44" y="56" width="32" height="10" rx="4" fill="${p.trim}" opacity="0.85"/><rect x="48" y="58" width="6" height="6" fill="#fff"/><rect x="66" y="58" width="6" height="6" fill="#fff"/>`,  // VR visor
  quant:        (p) => `<circle cx="50" cy="60" r="7" fill="none" stroke="${p.trim}" stroke-width="2"/><circle cx="70" cy="60" r="7" fill="none" stroke="${p.trim}" stroke-width="2"/><path d="M57 60 L63 60" stroke="${p.trim}" stroke-width="2"/>`,  // round glasses
  social:       (p) => `<rect x="34" y="100" width="20" height="32" rx="3" fill="#0f172a" stroke="${p.trim}" stroke-width="1.5"/><rect x="36" y="104" width="16" height="22" fill="${p.aura}" opacity="0.5"/>`,  // phone
  news:         (p) => `<rect x="80" y="80" width="18" height="6" fill="${p.trim}"/><circle cx="100" cy="83" r="6" fill="${p.body}" stroke="${p.trim}" stroke-width="1.5"/>`,  // microphone
  fundamentals: (p) => `<rect x="44" y="98" width="32" height="22" rx="2" fill="${p.body}" stroke="${p.trim}" stroke-width="1.5"/><line x1="50" y1="106" x2="70" y2="106" stroke="${p.trim}" stroke-width="1"/><line x1="50" y1="112" x2="68" y2="112" stroke="${p.trim}" stroke-width="1"/>`,  // ledger
  research:     (p) => `<path d="M38 32 L60 16 L82 32 Z" fill="${p.trim}" stroke="${p.hair}" stroke-width="1.5"/><circle cx="60" cy="24" r="3" fill="${p.hair}"/>`,  // mortarboard
  aggressive:   (p) => `<rect x="86" y="62" width="4" height="56" fill="${p.trim}"/><polygon points="83,62 93,62 88,46" fill="${p.trim}"/><rect x="80" y="118" width="16" height="4" fill="${p.hair}"/>`,  // sword
  neutral:      (p) => `<line x1="38" y1="70" x2="82" y2="70" stroke="${p.trim}" stroke-width="2"/><circle cx="38" cy="78" r="6" fill="${p.trim}" opacity="0.7"/><circle cx="82" cy="78" r="6" fill="${p.trim}" opacity="0.7"/>`,  // scales
  conservative: (p) => `<path d="M86 64 Q94 70 94 88 Q94 110 86 118 Q92 110 92 88 Q92 72 86 64" fill="${p.trim}" opacity="0.85"/>`,  // shield
  trader:       (p) => `<path d="M40 38 Q60 30 80 38 L80 44 Q60 38 40 44 Z" fill="${p.trim}"/>`,  // visor
  pm:           (p) => `<polygon points="56,86 64,86 66,108 54,108" fill="${p.trim}"/><rect x="58" y="86" width="4" height="22" fill="${p.hair}"/>`,  // necktie
};

const ANIMA_EXPRESSION = {
  bull:         (p) => `<path d="M52 70 Q60 76 68 70" stroke="${p.trim}" stroke-width="2" fill="none" stroke-linecap="round"/>`,  // grin
  bear:         (p) => `<path d="M52 72 Q60 68 68 72" stroke="${p.trim}" stroke-width="2" fill="none" stroke-linecap="round"/>`,  // smug frown
  market:       (p) => `<line x1="52" y1="72" x2="68" y2="72" stroke="${p.trim}" stroke-width="2" stroke-linecap="round"/>`,
  quant:        (p) => `<line x1="52" y1="72" x2="68" y2="72" stroke="${p.trim}" stroke-width="2" stroke-linecap="round"/>`,
  social:       (p) => `<path d="M52 70 Q60 76 68 70" stroke="${p.trim}" stroke-width="2" fill="none" stroke-linecap="round"/>`,
  news:         (p) => `<path d="M52 72 Q60 70 68 72" stroke="${p.trim}" stroke-width="2" fill="none" stroke-linecap="round"/>`,
  fundamentals: (p) => `<line x1="52" y1="72" x2="68" y2="72" stroke="${p.trim}" stroke-width="2" stroke-linecap="round"/>`,
  research:     (p) => `<path d="M52 70 Q60 74 68 70" stroke="${p.trim}" stroke-width="2" fill="none" stroke-linecap="round"/>`,
  aggressive:   (p) => `<path d="M52 73 Q60 67 68 73" stroke="${p.trim}" stroke-width="2.5" fill="none" stroke-linecap="round"/>`,  // fierce
  neutral:      (p) => `<line x1="52" y1="72" x2="68" y2="72" stroke="${p.trim}" stroke-width="2" stroke-linecap="round"/>`,
  conservative: (p) => `<path d="M52 70 Q60 74 68 70" stroke="${p.trim}" stroke-width="2" fill="none" stroke-linecap="round"/>`,
  trader:       (p) => `<path d="M52 70 Q60 75 68 70" stroke="${p.trim}" stroke-width="2" fill="none" stroke-linecap="round"/>`,
  pm:           (p) => `<path d="M52 71 Q60 75 68 71" stroke="${p.trim}" stroke-width="2" fill="none" stroke-linecap="round"/>`,
};

function animaSvg(pose) {
  const p = ANIMA_PALETTE[pose] || ANIMA_PALETTE.bull;
  const uid = "a" + Math.random().toString(36).slice(2, 8);
  const accessory = (ANIMA_ACCESSORY[pose] || (() => ""))(p);
  const mouth     = (ANIMA_EXPRESSION[pose] || ANIMA_EXPRESSION.bull)(p);
  // 120x140 viewbox: aura (full), torso, head, hair, eyes, mouth, accessory.
  return `
    <svg viewBox="0 0 120 140" xmlns="http://www.w3.org/2000/svg" class="anime-svg" aria-hidden="true">
      <defs>
        <radialGradient id="aura-${uid}" cx="50%" cy="50%" r="55%">
          <stop offset="0%"  stop-color="${p.aura}" stop-opacity="0.55"/>
          <stop offset="60%" stop-color="${p.aura}" stop-opacity="0.18"/>
          <stop offset="100%" stop-color="${p.aura}" stop-opacity="0"/>
        </radialGradient>
        <linearGradient id="body-${uid}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${p.body}" stop-opacity="1"/>
          <stop offset="100%" stop-color="${p.body}" stop-opacity="0.7"/>
        </linearGradient>
      </defs>
      <circle cx="60" cy="70" r="58" fill="url(#aura-${uid})" class="anime-aura"/>
      <!-- torso -->
      <path d="M36 132 Q36 96 60 92 Q84 96 84 132 Z" fill="url(#body-${uid})" stroke="${p.trim}" stroke-width="1.6"/>
      <!-- neck -->
      <rect x="54" y="78" width="12" height="10" fill="${p.skin}"/>
      <!-- head -->
      <ellipse cx="60" cy="58" rx="22" ry="24" fill="${p.skin}" stroke="${p.trim}" stroke-width="1.6"/>
      <!-- hair / hood -->
      <path d="M40 50 Q42 30 60 28 Q78 30 80 50 Q78 38 60 38 Q42 38 40 50 Z" fill="${p.hair}"/>
      <!-- eyes (large anime style) -->
      <ellipse cx="52" cy="60" rx="3.6" ry="5" fill="#1f2937"/>
      <ellipse cx="68" cy="60" rx="3.6" ry="5" fill="#1f2937"/>
      <circle  cx="53" cy="58" r="1.2" fill="#fff"/>
      <circle  cx="69" cy="58" r="1.2" fill="#fff"/>
      <!-- subtle blush -->
      <ellipse cx="46" cy="66" rx="3" ry="1.6" fill="${p.aura}" opacity="0.35"/>
      <ellipse cx="74" cy="66" rx="3" ry="1.6" fill="${p.aura}" opacity="0.35"/>
      <!-- mouth -->
      ${mouth}
      <!-- accessory -->
      ${accessory}
    </svg>
  `;
}

// One-time injection of the IDLE roster silhouettes.
function paintIdleRoster() {
  $$(".hero-roster-slot").forEach((el) => {
    const pose = el.dataset.pose;
    if (pose) el.innerHTML = animaSvg(pose);
  });
}

// Build the running-state DOM scaffolding. Idempotent — safe to call on every
// render; we only inject SVG into slots that don't yet have one.
function ensureArenaPortraits() {
  const sel = [
    ".hero-fighter-portrait",
    ".hero-verdict-pm-portrait",
    ".hero-matador-portrait",
    ".hero-trader-portrait",
    ".hero-pm-portrait",
    ".hero-risk-portrait",
  ].join(", ");
  $$(sel).forEach((el) => {
    const pose = el.dataset.pose;
    if (pose && !el.dataset.posed) {
      el.innerHTML = animaSvg(pose);
      el.dataset.posed = "1";
    }
  });
}

function arenaReset() {
  hero.tug = { bull: 0, bear: 0 };
  hero.lastSeen = {};
  hero.lastSpoke = null;
  const grid = document.getElementById("hero-analyst-grid");        if (grid) grid.innerHTML = "";
  const strip = document.getElementById("hero-team-strip");         if (strip) strip.innerHTML = "";
  for (const id of ["hero-fighter-bull", "hero-fighter-bear"]) {
    const f = document.getElementById(id);
    if (f) { f.dataset.state = "idle"; f.classList.remove("strike"); }
  }
  for (const f of document.querySelectorAll(".hero-risk-fighter")) {
    f.dataset.state = "idle";
    const s = f.querySelector(".hero-risk-status");
    if (s) s.textContent = "waiting";
  }
  for (const id of ["hero-hp-bull", "hero-hp-bear"]) {
    const el = document.getElementById(id);
    if (el) el.style.width = "0%";
  }
  for (const id of ["hero-status-bull", "hero-status-bear"]) {
    const el = document.getElementById(id);
    if (el) el.textContent = "waiting";
  }
  for (const id of ["hero-tug-bull-score", "hero-tug-bear-score"]) {
    const el = document.getElementById(id);
    if (el) el.textContent = "0";
  }
  const m = document.getElementById("hero-tug-marker");        if (m) m.style.left = "50%";
  const fb = document.getElementById("hero-tug-fill-bull");    if (fb) fb.style.width = "0%";
  const fr = document.getElementById("hero-tug-fill-bear");    if (fr) fr.style.width = "0%";
  const sp = document.getElementById("hero-speech");           if (sp) sp.hidden = true;
  const tStatus = document.getElementById("hero-trader-status"); if (tStatus) tStatus.textContent = "waiting";
  const pmStatus = document.getElementById("hero-pm-status");    if (pmStatus) pmStatus.textContent = "deliberating…";
  const running = document.getElementById("hero-running");
  if (running) running.dataset.phase = "recon";
}

// Build the per-phase rendering targets:
//   - Act I  : .hero-analyst-grid cards (one per analyst present)
//   - Act IV : .hero-risk-fighter slots are static in the HTML — we just
//              flip data-state on them.
//   - Always : .hero-team-strip — every agent gets a pip.
// Idempotent: only touches DOM when an agent has appeared since the last call.
function syncBench() {
  syncAnalystGrid();
  syncTeamStrip();
}

const ANALYST_KEYS = [
  "Market Analyst", "Quant Analyst", "Social Analyst", "News Analyst", "Fundamentals Analyst",
];

function syncAnalystGrid() {
  const grid = document.getElementById("hero-analyst-grid");
  if (!grid) return;
  for (const agent of ANALYST_KEYS) {
    if (!hero.agents[agent]) continue;
    if (grid.querySelector(`.hero-analyst-card[data-agent="${CSS.escape(agent)}"]`)) continue;
    const spec = ANIMA_ROSTER[agent];
    if (!spec) continue;
    const card = document.createElement("div");
    card.className = "hero-analyst-card";
    card.dataset.agent = agent;
    card.dataset.state = "idle";
    card.innerHTML = `
      <div class="hero-analyst-card-portrait">${animaSvg(spec.pose)}</div>
      <div class="hero-analyst-card-name">${escapeHTML(agent.replace(/ (Analyst|Researcher|Manager)$/, ""))}</div>
      <div class="hero-analyst-card-role">${escapeHTML(spec.role)}</div>
      <div class="hero-analyst-card-status">pending</div>
    `;
    grid.appendChild(card);
  }
}

// Team strip: tiny portrait + name + status pip for every agent involved.
// Rebuilt on every render — small DOM and gives a single-glance view.
const TEAM_ORDER = [
  "Market Analyst", "Quant Analyst", "Social Analyst", "News Analyst", "Fundamentals Analyst",
  "Bull Researcher", "Bear Researcher", "Research Manager",
  "Trader",
  "Aggressive Analyst", "Neutral Analyst", "Conservative Analyst",
  "Portfolio Manager",
];

function syncTeamStrip() {
  const strip = document.getElementById("hero-team-strip");
  if (!strip) return;
  const rows = [];
  for (const agent of TEAM_ORDER) {
    const data = hero.agents[agent];
    if (!data) continue;
    const spec = ANIMA_ROSTER[agent];
    if (!spec) continue;
    const status = data.status || "pending";
    const stateAttr = status === "running" ? "active"
                    : status === "completed" || status === "failed" ? "done"
                    : "idle";
    const short = agent.replace(/ (Analyst|Researcher|Manager)$/, "");
    rows.push(`
      <span class="hero-team-pip" data-agent="${escapeHTML(agent)}" data-state="${stateAttr}">
        <span class="hero-team-pip-mini">${animaSvg(spec.pose)}</span>
        <span class="hero-team-pip-label">${escapeHTML(short)}</span>
        <span class="hero-team-pip-status subtle">${escapeHTML(status)}</span>
      </span>
    `);
  }
  strip.innerHTML = rows.join("");
}

// Update each analyst card's state from hero.agents.
function syncAnalystCardStates() {
  const grid = document.getElementById("hero-analyst-grid");
  if (!grid) return;
  for (const card of grid.querySelectorAll(".hero-analyst-card")) {
    const agent = card.dataset.agent;
    const data = hero.agents[agent];
    if (!data) continue;
    const status = data.status || "pending";
    card.dataset.state = status === "running" ? "active"
                       : (status === "completed" || status === "failed") ? "done"
                       : "idle";
    const s = card.querySelector(".hero-analyst-card-status");
    if (s) s.textContent = status === "running" ? "researching…" : status;
  }
}

function syncRiskTrioStates() {
  for (const fighter of document.querySelectorAll(".hero-risk-fighter")) {
    const agent = fighter.dataset.agent;
    const data = hero.agents[agent];
    if (!data) continue;
    const status = data.status || "pending";
    fighter.dataset.state = status === "running" ? "active"
                          : (status === "completed" || status === "failed") ? "done"
                          : "idle";
    const s = fighter.querySelector(".hero-risk-status");
    if (s) s.textContent = status === "running" ? "arguing…" : status;
  }
}

function syncTraderStatus() {
  const t = hero.agents["Trader"];
  const el = document.getElementById("hero-trader-status");
  if (!el || !t) return;
  el.textContent = t.status === "running" ? "drafting trade…"
                 : t.status === "completed" ? "trade plan ready"
                 : t.status || "pending";
}

function syncPMStatus() {
  const pm = hero.agents["Portfolio Manager"];
  const el = document.getElementById("hero-pm-status");
  if (!el || !pm) return;
  el.textContent = pm.status === "running" ? "writing the verdict…"
                 : pm.status === "completed" ? "verdict ready"
                 : pm.status || "deliberating…";
}

// Decide which phase to display. Hierarchy: the latest-stage agent that's
// active or done wins; if a later agent is active, jump straight to its phase.
const PHASE_AGENTS = {
  recon:   ["Market Analyst", "Quant Analyst", "Social Analyst", "News Analyst", "Fundamentals Analyst"],
  debate:  ["Bull Researcher", "Bear Researcher", "Research Manager"],
  trader:  ["Trader"],
  risk:    ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
  verdict: ["Portfolio Manager"],
};
const PHASE_ORDER = ["recon", "debate", "trader", "risk", "verdict"];

function currentPhase() {
  // Pick the latest phase that has any non-pending agent. If all are pending,
  // default to recon.
  let cur = "recon";
  for (const phase of PHASE_ORDER) {
    const live = PHASE_AGENTS[phase].some((a) => {
      const st = hero.agents[a]?.status;
      return st === "running" || st === "completed" || st === "failed";
    });
    if (live) cur = phase;
  }
  return cur;
}

function syncPhaseRail(phase) {
  const reachedIdx = PHASE_ORDER.indexOf(phase);
  for (const pip of document.querySelectorAll(".hero-phase-pip")) {
    const p = pip.dataset.phasePip;
    const idx = PHASE_ORDER.indexOf(p);
    const state = idx < reachedIdx ? "done"
                : idx === reachedIdx ? "active"
                : "upcoming";
    pip.dataset.state = state;
  }
  const running = document.getElementById("hero-running");
  if (running) running.dataset.phase = phase;
}

function setFighterState(side, status) {
  const f = document.getElementById(`hero-fighter-${side}`);
  if (!f) return;
  let dataState = "idle";
  let hpPct = 5;
  let label = "waiting";
  if (status === "running")   { dataState = "active"; hpPct = 55; label = "fighting"; }
  if (status === "completed") { dataState = "done";   hpPct = 100; label = "rested"; }
  if (status === "failed")    { dataState = "done";   hpPct = 0;  label = "KO"; }
  f.dataset.state = dataState;
  const hp = document.getElementById(`hero-hp-${side}`);
  if (hp) hp.style.width = `${hpPct}%`;
  const s = document.getElementById(`hero-status-${side}`);
  if (s) s.textContent = label;
}

function triggerStrike(side, agent, preview) {
  const f = document.getElementById(`hero-fighter-${side}`);
  const spark = document.getElementById("hero-fight-spark");
  if (f) {
    f.classList.remove("strike");
    // Force reflow to restart animation if it's repeated.
    void f.offsetWidth;
    f.classList.add("strike");
    setTimeout(() => f.classList.remove("strike"), 700);
  }
  if (spark) {
    spark.classList.remove("bull-flash", "bear-flash");
    void spark.offsetWidth;
    spark.classList.add(`${side}-flash`);
    setTimeout(() => spark.classList.remove(`${side}-flash`), 700);
  }
  // Tug-of-war: each strike pulls 1 point toward the striker's side.
  hero.tug = hero.tug || { bull: 0, bear: 0 };
  hero.tug[side] = (hero.tug[side] || 0) + 1;
  updateTug();
  if (preview) updateSpeech(side, agent, preview);
}

function updateTug() {
  const { bull = 0, bear = 0 } = hero.tug || {};
  const total = Math.max(bull + bear, 1);
  const bullPct = (bull / total) * 100;
  const bearPct = (bear / total) * 100;
  const bullEl = document.getElementById("hero-tug-bull-score");
  const bearEl = document.getElementById("hero-tug-bear-score");
  if (bullEl) bullEl.textContent = String(bull);
  if (bearEl) bearEl.textContent = String(bear);
  const fillBull = document.getElementById("hero-tug-fill-bull");
  const fillBear = document.getElementById("hero-tug-fill-bear");
  if (fillBull) fillBull.style.width = `${bullPct / 2}%`;
  if (fillBear) fillBear.style.width = `${bearPct / 2}%`;
  const marker = document.getElementById("hero-tug-marker");
  if (marker) {
    // Marker slides from 50% toward the stronger side. Max swing 90% ↔ 10%.
    const lean = (bull - bear) / total;          // -1 .. 1
    const left = 50 + Math.max(-40, Math.min(40, lean * 40));
    marker.style.left = `${left}%`;
  }
}

function updateSpeech(side, agent, preview) {
  const bubble = document.getElementById("hero-speech");
  const who    = document.getElementById("hero-speech-who");
  const body   = document.getElementById("hero-speech-body");
  const avatar = document.getElementById("hero-speech-avatar");
  if (!bubble || !who || !body) return;
  bubble.hidden = false;
  bubble.dataset.side = side;
  who.textContent = `${agent} · speaking`;
  // Reset/inject the speaker's portrait so the bubble shows WHO is talking.
  if (avatar) {
    const spec = ANIMA_ROSTER[agent];
    avatar.innerHTML = spec ? animaSvg(spec.pose) : "";
  }
  const txt = (preview || "").slice(0, 240);
  body.textContent = txt + (preview && preview.length > 240 ? "…" : "");
  bubble.style.animation = "none";
  void bubble.offsetWidth;
  bubble.style.animation = "";
  hero.lastSpoke = side;
}

// No-op shim kept so older callers in this file don't NPE. The actual
// per-phase state-syncing happens in renderArena via syncAnalystCardStates,
// syncRiskTrioStates, syncTraderStatus, syncPMStatus and syncTeamStrip.
function updateBenchCard() { /* deprecated — superseded by per-phase sync */ }

const ROUND_NAMES = [
  { num: 1, name: "Recon",   agents: ["Market Analyst", "Quant Analyst", "Social Analyst", "News Analyst", "Fundamentals Analyst"] },
  { num: 2, name: "Debate",  agents: ["Bull Researcher", "Bear Researcher", "Research Manager"] },
  { num: 3, name: "Risk",    agents: ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst", "Trader"] },
  { num: 4, name: "Verdict", agents: ["Portfolio Manager"] },
];

function updateRound() {
  // Current round = highest-numbered round that has any non-pending member.
  let cur = ROUND_NAMES[0];
  for (const r of ROUND_NAMES) {
    const liveInThisRound = r.agents.some((a) => {
      const st = hero.agents[a]?.status;
      return st === "running" || st === "completed" || st === "failed";
    });
    if (liveInThisRound) cur = r;
  }
  const numEl  = document.getElementById("hero-round-num");
  const nameEl = document.getElementById("hero-round-name");
  if (numEl)  numEl.textContent  = String(cur.num);
  if (nameEl) nameEl.textContent = cur.name;
}

// Main arena render — invoked from renderHeroFeed on every event.
function renderArena() {
  if (hero.state !== "running") return;
  ensureArenaPortraits();
  syncBench();

  // 1. Diff against lastSeen to find transitions.
  hero.lastSeen = hero.lastSeen || {};
  for (const [agent, data] of Object.entries(hero.agents)) {
    const spec = ANIMA_ROSTER[agent];
    if (!spec) continue;
    const prev = hero.lastSeen[agent] || {};
    const status = data.status || "pending";
    const preview = data.preview || "";
    const previewChanged = preview && preview !== (prev.preview || "");
    const statusChanged  = status !== prev.status;

    if (spec.side === "bull" || spec.side === "bear") {
      setFighterState(spec.side, status);
      if ((statusChanged && status === "running") || previewChanged) {
        triggerStrike(spec.side, agent, preview);
      }
    } else if (previewChanged) {
      const side = spec.side === "pm" ? "pm" : (spec.side === "right" ? "risk" : "left");
      updateSpeech(side, agent, preview);
    }
    hero.lastSeen[agent] = { status, preview };
  }

  // 2. Per-phase state syncs (cheap; only touch attrs).
  syncAnalystCardStates();
  syncRiskTrioStates();
  syncTraderStatus();
  syncPMStatus();
  syncTeamStrip();

  // 3. Phase rail + active phase swap.
  syncPhaseRail(currentPhase());
}

// =========================================================================
// Lab nav highlighting — Lab button is active whenever any of the 4 Lab
// sub-sections is active. Sub-nav buttons inside each section also stay
// synced via switchView's data-target.
// =========================================================================
const LAB_VIEWS = new Set(["insights", "events", "backtest", "risk"]);

function syncLabNavActive(view) {
  const labBtn = document.querySelector(".fund-nav-btn-lab");
  if (!labBtn) return;
  const inLab = LAB_VIEWS.has(view);
  // Clear .active from all top-level nav (already done by switchView), then
  // re-mark Lab as active if we're in any Lab sub-view.
  $$(".fund-nav-btn").forEach((b) => b.classList.remove("active"));
  if (inLab) {
    labBtn.classList.add("active");
  } else {
    const match = document.querySelector(`.fund-nav-btn[data-target="${view}"]`);
    if (match) match.classList.add("active");
  }
  // Sync the Lab sub-tab nav inside each section.
  $$(".lab-subnav-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.target === view);
  });
}

// Wrap renderHeroFeed so the arena re-renders on every WS event without
// touching the original function body. (Monkey-patch at module load.)
(function wrapRenderHeroFeed() {
  const orig = renderHeroFeed;
  window._origRenderHeroFeed = orig;
  // Reassign the binding so callers see the wrapped version.
  // eslint-disable-next-line no-func-assign
  renderHeroFeed = function () {
    try { orig.apply(this, arguments); } catch (e) { console.error(e); }
    try { renderArena(); } catch (e) { console.error("renderArena", e); }
  };
})();

// Wrap setHeroState so we can paint the idle roster + reset arena on
// transitions, AND keep Recent Activity in sync with the live session. Same
// pattern as above — minimal blast radius.
(function wrapSetHeroState() {
  const orig = setHeroState;
  // eslint-disable-next-line no-func-assign
  setHeroState = function (next) {
    orig.call(this, next);
    if (next === "idle")    { paintIdleRoster(); }
    if (next === "running") { arenaReset(); ensureArenaPortraits(); }
    if (next === "complete"){ ensureArenaPortraits(); }
    if (next === "running" || next === "complete") {
      // Refresh recent sessions so the activity feed reflects this debate
      // both at kickoff (shows "in progress") and at finish (flips to
      // "decision published").
      loadRecentSessions().then(() => { buildActivity(); renderActivity(); }).catch(() => {});
    }
  };
})();

// Wrap switchView for Lab nav sync.
(function wrapSwitchView() {
  const orig = switchView;
  // eslint-disable-next-line no-func-assign
  switchView = function (target) {
    orig.call(this, target);
    syncLabNavActive(target);
  };
})();

// Initial paint on DOM ready (some early callers run before hero.idle ever
// fires setHeroState, so seed the roster eagerly). Also installs a click
// delegator for the Lab sub-nav buttons — they live inside .fund-section
// containers that toggle visibility, so a body-level delegator survives
// any re-render.
document.addEventListener("DOMContentLoaded", () => {
  paintIdleRoster();
  ensureArenaPortraits();
  syncLabNavActive(state.view || "overview");

  document.body.addEventListener("click", (e) => {
    const btn = e.target.closest(".lab-subnav-btn[data-target]");
    if (btn) switchView(btn.dataset.target);
  });

  document.getElementById("analyses-refresh")?.addEventListener("click", async () => {
    await loadRecentSessions();
    renderAnalysesList();
  });
  document.getElementById("analysis-back-btn")?.addEventListener("click", () => {
    closeAnalysisDetail();
    if (location.hash.startsWith("#analyses/")) history.replaceState(null, "", "#analyses");
  });
  document.getElementById("hero-full-analysis-btn")?.addEventListener("click", () => {
    const sid = hero.sessionId || hero.session?.id;
    if (!sid) return;
    location.hash = `#analyses/${sid}`;
    switchView("analyses");
  });

  window.addEventListener("hashchange", applyAnalysesHash);
  applyAnalysesHash();
});

// =========================================================================
// Analyses tab — list of every past debate + per-session detail view.
// The list is a thin wrapper over /api/sessions (already loaded by
// loadRecentSessions). The detail view fetches /api/sessions/{id} for the
// full snapshot (report_sections + agent_status + config) and renders one
// expandable card per agent report, grouped by team — same data shape the
// legacy /analyze page consumes.
// =========================================================================

function renderAnalysesList() {
  const tbody = document.getElementById("analyses-rows");
  const count = document.getElementById("analyses-count");
  if (!tbody) return;
  const sessions = (state.sessions || []).slice(0, 200);
  if (count) count.textContent = `(${sessions.length})`;
  if (!sessions.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="6" class="subtle">No analyses yet. Start a debate from the Floor tab.</td></tr>`;
    return;
  }
  tbody.innerHTML = sessions.map((s) => {
    const ms = tsToMillis(s.created_at || s.completed_at);
    const dateCell = ms ? new Date(ms).toLocaleString() : "—";
    const kp = pmKpis(s.pm_decision || s.portfolio_decision);
    const verdictClass = (kp.verdict || "").toLowerCase();
    const verdictHtml = kp.verdict && kp.verdict !== "—"
      ? `<span class="verdict-tag ${escapeHTML(verdictClass)}">${escapeHTML(kp.verdict)}</span>`
      : `<span class="subtle">—</span>`;
    return `
      <tr class="analyses-row" data-session-id="${escapeHTML(s.id)}">
        <td class="subtle">${escapeHTML(dateCell)}</td>
        <td><strong>${escapeHTML(s.ticker || "—")}</strong></td>
        <td><span class="status-pill ${escapeHTML(s.status || "")}">${escapeHTML(s.status || "—")}</span></td>
        <td>${verdictHtml}</td>
        <td style="text-align:right" class="subtle">${escapeHTML(kp.target)}</td>
        <td style="text-align:right"><button type="button" class="ghost-btn" data-open-analysis="${escapeHTML(s.id)}">Open ↗</button></td>
      </tr>`;
  }).join("");
  // Delegate row clicks. Buttons inside also work via the same handler.
  tbody.querySelectorAll("[data-open-analysis], .analyses-row").forEach((el) => {
    el.addEventListener("click", (e) => {
      const sid = e.target.closest("[data-open-analysis]")?.dataset.openAnalysis
        || e.currentTarget.dataset.sessionId;
      if (sid) {
        location.hash = `#analyses/${sid}`;
        openAnalysisDetail(sid);
      }
    });
  });
}

function applyAnalysesHash() {
  const m = (location.hash || "").match(/^#analyses\/([A-Za-z0-9_-]+)$/);
  if (m) {
    // Hash points to a specific analysis — ensure we're on the Analyses tab
    // and open the detail.
    if (state.view !== "analyses") switchView("analyses");
    openAnalysisDetail(m[1]);
  } else if (location.hash === "#analyses") {
    if (state.view !== "analyses") switchView("analyses");
    closeAnalysisDetail();
  }
}

async function openAnalysisDetail(sessionId) {
  const list = document.getElementById("analyses-list-card");
  const detail = document.getElementById("analysis-detail-card");
  const body = document.getElementById("analysis-detail-body");
  const title = document.getElementById("analysis-detail-title");
  const statusEl = document.getElementById("analysis-detail-status");
  const tagEl = document.getElementById("analysis-detail-tag");
  if (!detail || !body) return;
  list?.setAttribute("hidden", "");
  detail.hidden = false;
  body.innerHTML = `<div class="subtle" style="padding:18px">Loading…</div>`;
  if (title) title.textContent = "Loading…";
  if (statusEl) statusEl.textContent = "";
  if (tagEl) tagEl.textContent = "";
  try {
    const r = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const session = await r.json();
    renderAnalysisDetail(session);
  } catch (e) {
    body.innerHTML = `<div class="subtle" style="padding:18px">Couldn't load this analysis: ${escapeHTML(String(e.message || e))}</div>`;
  }
}

function closeAnalysisDetail() {
  const list = document.getElementById("analyses-list-card");
  const detail = document.getElementById("analysis-detail-card");
  if (list) list.hidden = false;
  if (detail) detail.hidden = true;
}

// Section grouping for the detail panel: matches the team layout the
// legacy /analyze page uses but keyed by section name so we render whatever
// the session actually emitted (some sections may be missing for analyses
// that didn't run all analysts).
const ANALYSIS_TEAM_GROUPS = [
  { team: "Analyst Team",      sections: ["market_report", "sentiment_report", "news_report", "fundamentals_report"] },
  { team: "Research Team",     sections: ["bull_history", "bear_history", "investment_plan"] },
  { team: "Trading Team",      sections: ["trader_investment_plan"] },
  { team: "Risk Management",   sections: ["aggressive_history", "neutral_history", "conservative_history"] },
  { team: "Portfolio Manager", sections: ["final_trade_decision"] },
];

const SECTION_LABEL = {
  market_report:           "Market Analyst",
  sentiment_report:        "Social Analyst",
  news_report:             "News Analyst",
  fundamentals_report:     "Fundamentals Analyst",
  bull_history:            "Bull Researcher",
  bear_history:            "Bear Researcher",
  investment_plan:         "Research Manager (judgment)",
  trader_investment_plan:  "Trader (plan)",
  aggressive_history:      "Aggressive Analyst",
  neutral_history:         "Neutral Analyst",
  conservative_history:    "Conservative Analyst",
  final_trade_decision:    "Portfolio Manager · final call",
};
const SECTION_POSE = {
  market_report: "market",         sentiment_report: "social",
  news_report: "news",             fundamentals_report: "fundamentals",
  bull_history: "bull",            bear_history: "bear",
  investment_plan: "research",     trader_investment_plan: "trader",
  aggressive_history: "aggressive",neutral_history: "neutral",
  conservative_history: "conservative",
  final_trade_decision: "pm",
};

function renderAnalysisDetail(session) {
  const body = document.getElementById("analysis-detail-body");
  const title = document.getElementById("analysis-detail-title");
  const statusEl = document.getElementById("analysis-detail-status");
  const tagEl = document.getElementById("analysis-detail-tag");
  if (!body || !session) return;
  const date = (session.analysis_date || session.created_at || "").slice(0, 10);
  const ticker = session.ticker || "—";
  if (title) title.textContent = `${ticker} · ${date}`;
  if (statusEl) {
    statusEl.textContent = session.status || "";
    statusEl.className = `status-pill ${session.status || ""}`;
  }
  const verdict = session.pm_decision?.rating || inferVerdictFromText(session.report_sections?.final_trade_decision) || "";
  if (tagEl) {
    tagEl.textContent = verdict || "";
    tagEl.className = `final-card-tag ${verdict ? verdict.toLowerCase() : ""}`;
  }
  const sections = session.report_sections || {};
  const groups = ANALYSIS_TEAM_GROUPS.map((g) => {
    const present = g.sections.filter((sec) => sections[sec]);
    return { ...g, present };
  }).filter((g) => g.present.length);

  if (!groups.length) {
    body.innerHTML = `<div class="subtle" style="padding:18px">This analysis didn't emit any reports — it may have failed or been cancelled early.</div>`;
    return;
  }

  body.innerHTML = groups.map((g, gi) => `
    <div class="analysis-team-group">
      <div class="analysis-team-head">${escapeHTML(g.team)}</div>
      ${g.present.map((sec, si) => {
        const open = gi === groups.length - 1 && si === 0;  // final_trade_decision opens by default
        const pose = SECTION_POSE[sec] || "pm";
        const label = SECTION_LABEL[sec] || sec;
        const content = sections[sec] || "";
        return `
          <details class="analysis-section" ${open ? "open" : ""}>
            <summary>
              <span class="analysis-section-portrait">${animaSvg(pose)}</span>
              <span>${escapeHTML(label)}</span>
              <span class="analysis-section-meta">${escapeHTML(sec)}</span>
            </summary>
            <div class="analysis-section-body markdown" data-section="${escapeHTML(sec)}">${renderMd(content) || `<div class="analysis-empty">(no content)</div>`}</div>
          </details>`;
      }).join("")}
    </div>
  `).join("");
}

function renderMd(s) {
  if (typeof marked !== "undefined" && s) {
    try { return marked.parse(s); } catch (_) { /* fall through */ }
  }
  return s ? `<pre>${escapeHTML(s)}</pre>` : "";
}

function inferVerdictFromText(s) {
  if (!s) return "";
  const m = String(s).match(/\b(BUY|SELL|HOLD|OVERWEIGHT|UNDERWEIGHT)\b/i);
  return m ? m[1].toUpperCase() : "";
}
