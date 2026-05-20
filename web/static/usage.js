// Standalone /usage dashboard. Loads supabase-client.js for auth, then
// either shows the dashboard (admin) or a gated landing card.
//
// State boundary:
//   - The server-side require_admin (web/auth.py) is the security gate.
//     The frontend logic here only controls what's rendered.
//
// The fetch wrapper at the bottom mirrors the one in app.js so /api/* calls
// pick up the Supabase access token. Kept inline instead of imported because
// app.js is a classic script and we don't want a build step.

const $ = (sel) => document.querySelector(sel);

const dashState = {
  data: null,
  loading: false,
  search: "",
  sortKey: "tokens_total",
  wired: false,
  currentUser: null,
};

function getAuth() {
  return window.AgenticWhalesAuth || null;
}

// ---------- Boot ----------

window.addEventListener("DOMContentLoaded", () => {
  $("#gate-google")?.addEventListener("click", signIn);
  $("#gate-signout")?.addEventListener("click", signOut);
  $("#dash-signout")?.addEventListener("click", signOut);

  const start = () => {
    const auth = getAuth();
    if (!auth) {
      window.addEventListener("agenticwhales-auth-ready", start, { once: true });
      return;
    }
    auth.onChange(async (u) => {
      dashState.currentUser = u;
      await syncAccessUI();
    });
  };
  start();
});

async function signIn() {
  const auth = getAuth();
  if (!auth?.isConfigured) return;
  const btn = $("#gate-google");
  btn.disabled = true;
  try { await auth.signInWithGoogle(); }
  catch (e) {
    console.error("sign-in failed:", e);
    alert(`Sign-in failed: ${e.message || e}`);
    btn.disabled = false;
  }
}

async function signOut() {
  const auth = getAuth();
  if (!auth) return;
  try { await auth.signOut(); } catch (e) { console.error(e); }
}

// ---------- Gate vs dashboard ----------

async function syncAccessUI() {
  const auth = getAuth();
  const u = dashState.currentUser;
  const gate = $("#gate");
  const dash = $("#dashboard");
  const gateTitle = $("#gate-title");
  const gateMsg = $("#gate-msg");
  const gateGoogle = $("#gate-google");
  const gateSignout = $("#gate-signout");

  // Not signed in -> prompt sign-in (or explain guest mode if Supabase
  // isn't configured at all).
  if (!u) {
    dash.classList.add("hidden");
    gate.classList.remove("hidden");
    if (!auth?.isConfigured) {
      gateTitle.textContent = "Supabase not configured";
      gateMsg.textContent = "The usage dashboard needs a Supabase project. Set AGENTICWHALES_SUPABASE_URL and AGENTICWHALES_SUPABASE_ANON_KEY and reload.";
      gateGoogle.classList.add("hidden");
      gateSignout.classList.add("hidden");
    } else {
      gateTitle.textContent = "Sign in to continue";
      gateMsg.textContent = "The usage dashboard is admin-only.";
      gateGoogle.classList.remove("hidden");
      gateGoogle.disabled = false;
      gateSignout.classList.add("hidden");
    }
    return;
  }

  // Signed in — probe the server gate before deciding which surface to show.
  try {
    const res = await fetch("/api/usage/me");
    if (res.ok) {
      gate.classList.add("hidden");
      dash.classList.remove("hidden");
      if (!dashState.wired) wireDashboardControls();
      await loadDashboard();
      return;
    }
    // 403 means signed in but not admin.
    dash.classList.add("hidden");
    gate.classList.remove("hidden");
    gateTitle.textContent = "Not authorised";
    gateMsg.textContent = `Signed in as ${u.email || u.displayName || "this account"}, but only the admin can view this dashboard.`;
    gateGoogle.classList.add("hidden");
    gateSignout.classList.remove("hidden");
  } catch (e) {
    console.error("usage/me probe failed:", e);
    gateTitle.textContent = "Could not reach the server";
    gateMsg.textContent = "Try again in a moment.";
    gateGoogle.classList.add("hidden");
    gateSignout.classList.remove("hidden");
  }
}

// ---------- Dashboard data ----------

function wireDashboardControls() {
  dashState.wired = true;
  $("#dash-refresh").addEventListener("click", () => loadDashboard({ force: true }));
  $("#dash-user-search").addEventListener("input", (e) => {
    dashState.search = e.target.value.trim().toLowerCase();
    renderDashboardUsers();
  });
  $("#dash-user-sort").addEventListener("change", (e) => {
    dashState.sortKey = e.target.value;
    renderDashboardUsers();
  });
}

async function loadDashboard(opts = {}) {
  if (dashState.loading) return;
  const meta = $("#dash-meta");
  dashState.loading = true;
  meta.textContent = "Loading…";
  try {
    const res = await fetch("/api/usage/dashboard");
    if (res.status === 403) {
      meta.textContent = "Admin access only.";
      return;
    }
    if (!res.ok) {
      meta.textContent = `Failed to load dashboard (${res.status}).`;
      return;
    }
    dashState.data = await res.json();
    meta.textContent = "Lifetime totals across every signed-in user · 30-day daily activity below.";
    renderDashboardMetrics();
    renderDashboardChart();
    renderDashboardUsers();
    const gen = $("#dash-generated");
    if (gen && dashState.data.generated_at) {
      const d = new Date(dashState.data.generated_at);
      gen.textContent = `Generated ${d.toLocaleString()}`;
    }
  } catch (e) {
    console.error("loadDashboard failed:", e);
    meta.textContent = "Failed to load dashboard. Check the server log.";
  } finally {
    dashState.loading = false;
  }
}

// ---------- Formatting helpers ----------

function fmtInt(n) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString();
}

function fmtTokens(n) {
  if (!n) return "0";
  if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + "B";
  if (n >= 1_000_000)     return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000)         return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

function fmtRelativeIso(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "—";
  const diff = (Date.now() - t) / 1000;
  if (diff < 60)    return "just now";
  if (diff < 3600)  return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  if (diff < 86400 * 30) return `${Math.floor(diff/86400)}d ago`;
  return new Date(t).toLocaleDateString();
}

function escapeHTML(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// ---------- Renderers ----------

function renderDashboardMetrics() {
  const grid = $("#dash-metrics");
  if (!grid || !dashState.data) return;
  const o = dashState.data.overall;
  const tiles = [
    { label: "Active users today",  value: fmtInt(o.dau_today),         sub: `${o.dau_7d_avg} avg / day (7d)` },
    { label: "Active users (7d)",   value: fmtInt(o.active_users_7d),   sub: `of ${o.users_with_activity} ever active` },
    { label: "Total users",         value: fmtInt(o.total_users),       sub: "signed up with Supabase" },
    { label: "Analyses today",      value: fmtInt(o.analyses_today),    sub: `${fmtInt(o.analyses_7d)} in last 7 days` },
    { label: "Analyses (lifetime)", value: fmtInt(o.total_analyses),    sub: `+ ${fmtInt(o.total_batches)} basket runs` },
    { label: "Tokens today",        value: fmtTokens(o.tokens_today),   sub: `${fmtTokens(o.tokens_7d)} last 7d` },
    { label: "Tokens (lifetime)",   value: fmtTokens(o.total_tokens),   sub: `${fmtTokens(o.total_tokens_in)} in · ${fmtTokens(o.total_tokens_out)} out` },
    { label: "LLM calls (lifetime)",value: fmtInt(o.total_llm_calls),   sub: `${fmtInt(o.total_tool_calls)} tool calls` },
  ];
  grid.innerHTML = tiles.map((t) => `
    <div class="dash-metric">
      <div class="dash-metric-label">${escapeHTML(t.label)}</div>
      <div class="dash-metric-value">${escapeHTML(String(t.value))}</div>
      <div class="dash-metric-sub">${escapeHTML(t.sub)}</div>
    </div>
  `).join("");
}

function renderDashboardChart() {
  const chart = $("#dash-chart");
  const legend = $("#dash-chart-legend");
  if (!chart || !dashState.data) return;
  const days = dashState.data.daily;
  const max = Math.max(1, ...days.map((d) => d.tokens));
  chart.innerHTML = days.map((d) => {
    const inH  = d.tokens ? Math.max(2, (d.tokens_in  / max) * 100) : 0;
    const outH = d.tokens ? Math.max(2, (d.tokens_out / max) * 100) : 0;
    const tip = `
      <strong>${d.date}</strong><br>
      ${fmtInt(d.active_users)} active · ${fmtInt(d.analyses)} analyses${d.batches ? ` · ${fmtInt(d.batches)} baskets` : ""}<br>
      ${fmtTokens(d.tokens)} tokens (${fmtTokens(d.tokens_in)} in / ${fmtTokens(d.tokens_out)} out)
    `;
    return `
      <div class="dash-bar-col">
        ${d.tokens
          ? `<div class="dash-bar tokens-in"  style="height:${inH}%"></div>
             <div class="dash-bar tokens-out" style="height:${outH}%"></div>`
          : `<div class="dash-bar empty" style="height:2%"></div>`}
        <div class="dash-bar-tip">${tip}</div>
      </div>
    `;
  }).join("");

  // Sparse x-axis labels — first, last, every 5th day. Replace any previous
  // axis to avoid stacking on re-render.
  chart.parentElement.querySelectorAll(".dash-chart-xaxis").forEach((el) => el.remove());
  const xs = document.createElement("div");
  xs.className = "dash-chart-xaxis";
  xs.innerHTML = days.map((d, i) => {
    const show = i === 0 || i === days.length - 1 || i % 5 === 0;
    return `<span>${show ? d.date.slice(5) : ""}</span>`;
  }).join("");
  chart.after(xs);

  legend.innerHTML = `
    <span class="legend-item"><span class="legend-swatch" style="background:linear-gradient(180deg,var(--accent-2),#3b82f6)"></span>Tokens in</span>
    <span class="legend-item"><span class="legend-swatch" style="background:linear-gradient(180deg,var(--accent),#14b8a6)"></span>Tokens out</span>
    <span class="legend-item subtle">Hover a bar for daily totals</span>
  `;
}

function renderDashboardUsers() {
  const body = $("#dash-users-body");
  if (!body || !dashState.data) return;
  const me = (dashState.currentUser?.email || "").toLowerCase();
  let rows = [...dashState.data.per_user];
  if (dashState.search) {
    rows = rows.filter((u) => {
      const hay = `${u.email || ""} ${u.username || ""}`.toLowerCase();
      return hay.includes(dashState.search);
    });
  }
  const key = dashState.sortKey;
  rows.sort((a, b) => {
    if (key === "last_active" || key === "created_at") {
      const av = a[key] ? Date.parse(a[key]) : -Infinity;
      const bv = b[key] ? Date.parse(b[key]) : -Infinity;
      return bv - av;
    }
    return (b[key] || 0) - (a[key] || 0);
  });
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="10" class="dash-empty">No users match.</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((u) => {
    const isMe = (u.email || "").toLowerCase() === me;
    const tier = (u.tier || "novice").toLowerCase();
    const name = u.username || (u.email ? u.email.split("@")[0] : "(unknown)");
    return `
      <tr class="${isMe ? "admin-row" : ""}">
        <td>
          <div class="dash-user-cell">
            <span class="dash-user-name">${escapeHTML(name)}${isMe ? `<span class="dash-user-you-flag">you</span>` : ""}</span>
            <span class="dash-user-email">${escapeHTML(u.email || "—")}</span>
          </div>
        </td>
        <td><span class="dash-tier-badge ${tier}">${escapeHTML(tier)}</span></td>
        <td class="num">${fmtInt(u.analyses)}</td>
        <td class="num">${fmtInt(u.batches)}</td>
        <td class="num">${fmtTokens(u.tokens_in)}</td>
        <td class="num">${fmtTokens(u.tokens_out)}</td>
        <td class="num"><strong>${fmtTokens(u.tokens_total)}</strong></td>
        <td class="num">${fmtInt(u.llm_calls)}</td>
        <td class="num">${fmtInt(u.tool_calls)}</td>
        <td>${escapeHTML(fmtRelativeIso(u.last_active))}</td>
      </tr>
    `;
  }).join("");
}

// ---------- Fetch wrapper — same JWT injection as the main app ----------

const _origFetch = window.fetch.bind(window);
window.fetch = async function patchedFetch(input, init) {
  const url = typeof input === "string" ? input : (input?.url || "");
  if (url.startsWith("/api/") || url.includes(`${location.host}/api/`)) {
    const token = getAuth()?.getAccessToken?.();
    if (token) {
      init = init ? { ...init } : {};
      const h = new Headers(init.headers || (typeof input !== "string" ? input.headers : undefined));
      if (!h.has("Authorization")) h.set("Authorization", `Bearer ${token}`);
      init.headers = h;
    }
  }
  return _origFetch(input, init);
};
