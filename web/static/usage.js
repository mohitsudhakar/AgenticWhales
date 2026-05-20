// Standalone /usage dashboard. Loads supabase-client.js for auth, then
// either shows the dashboard (admin) or a gated landing card.
//
// Security boundary: web/auth.py::require_admin gates the data. This file
// only controls what the browser renders.

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

  // Re-render the SVG charts on resize so bar widths track the viewport.
  let rAF = null;
  window.addEventListener("resize", () => {
    if (rAF) cancelAnimationFrame(rAF);
    rAF = requestAnimationFrame(() => {
      if (dashState.data && !$("#dashboard").classList.contains("hidden")) {
        renderDashboardChart();
        renderMiniChart();
      }
    });
  });
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

  try {
    const res = await fetch("/api/usage/me");
    if (res.ok) {
      gate.classList.add("hidden");
      dash.classList.remove("hidden");
      if (!dashState.wired) wireDashboardControls();
      await loadDashboard();
      return;
    }
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
  dashState.loading = true;
  const refreshBtn = $("#dash-refresh");
  refreshBtn?.classList.add("spinning");
  try {
    const res = await fetch("/api/usage/dashboard");
    if (res.status === 403) {
      console.warn("admin gate rejected request");
      return;
    }
    if (!res.ok) {
      console.warn("dashboard fetch failed:", res.status);
      return;
    }
    dashState.data = await res.json();
    renderHero();
    renderDashboardChart();
    renderMiniChart();
    renderTopUsers();
    renderDashboardUsers();
    const gen = $("#dash-generated");
    if (gen && dashState.data.generated_at) {
      const d = new Date(dashState.data.generated_at);
      gen.textContent = `Updated ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    }
  } catch (e) {
    console.error("loadDashboard failed:", e);
  } finally {
    dashState.loading = false;
    refreshBtn?.classList.remove("spinning");
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

// Round n up to a nice 1/2/5 × 10^k boundary so the chart's Y axis shows
// human-friendly labels (10k, 20k, 50k, 100k, ...) instead of jagged numbers.
function niceCeil(n) {
  if (n <= 0) return 1;
  const exp = Math.floor(Math.log10(n));
  const base = Math.pow(10, exp);
  const mult = n / base;
  if (mult <= 1) return 1 * base;
  if (mult <= 2) return 2 * base;
  if (mult <= 5) return 5 * base;
  return 10 * base;
}

// ---------- Hero metric strip ----------

function renderHero() {
  const o = dashState.data.overall;
  const cards = [
    {
      key: "tokens",
      eyebrow: "Tokens",
      value: fmtTokens(o.total_tokens),
      caption: "lifetime",
      rows: [
        { label: "Today",       value: fmtTokens(o.tokens_today) },
        { label: "Last 7 days", value: fmtTokens(o.tokens_7d) },
        { label: "Split",       value: `${fmtTokens(o.total_tokens_in)} in · ${fmtTokens(o.total_tokens_out)} out` },
      ],
    },
    {
      key: "analyses",
      eyebrow: "Analyses",
      value: fmtInt(o.total_analyses),
      caption: `+ ${fmtInt(o.total_batches)} basket runs`,
      rows: [
        { label: "Today",       value: fmtInt(o.analyses_today) },
        { label: "Last 7 days", value: fmtInt(o.analyses_7d) },
        { label: "LLM calls",   value: `${fmtInt(o.total_llm_calls)} · ${fmtInt(o.total_tool_calls)} tool` },
      ],
    },
    {
      key: "users",
      eyebrow: "Users",
      value: fmtInt(o.total_users),
      caption: `${fmtInt(o.users_with_activity)} ever active`,
      rows: [
        { label: "Active today",  value: fmtInt(o.dau_today) },
        { label: "Active 7 days", value: fmtInt(o.active_users_7d) },
        { label: "7-day DAU avg", value: o.dau_7d_avg },
      ],
    },
  ];
  $("#usage-hero").innerHTML = cards.map((c) => `
    <article class="hero-card hero-${c.key}">
      <div class="hero-eyebrow">${escapeHTML(c.eyebrow)}</div>
      <div class="hero-value">${escapeHTML(String(c.value))}</div>
      <div class="hero-caption">${escapeHTML(c.caption)}</div>
      <dl class="hero-rows">
        ${c.rows.map((r) => `
          <div class="hero-row">
            <dt>${escapeHTML(r.label)}</dt>
            <dd>${escapeHTML(String(r.value))}</dd>
          </div>
        `).join("")}
      </dl>
    </article>
  `).join("");
}

// ---------- Daily tokens chart (SVG, stacked) ----------

function renderDashboardChart() {
  const host = $("#usage-chart");
  if (!host || !dashState.data) return;
  const days = dashState.data.daily;
  const max = Math.max(1, ...days.map((d) => d.tokens));
  const niceMax = niceCeil(max);

  // Pick width from the host so we hit pixel-perfect bar widths instead of
  // relying on SVG's preserveAspectRatio scaling (which makes thin bars look
  // blurry at high pixel ratios).
  const W = Math.max(360, host.clientWidth || 800);
  const H = 240;
  const padL = 56, padR = 12, padT = 16, padB = 30;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const barGap = 3;
  const barW = Math.max(2, (innerW - barGap * (days.length - 1)) / days.length);

  const tickFrac = [0, 0.25, 0.5, 0.75, 1];
  const ticks = tickFrac.map((f) => Math.round(f * niceMax));

  let svg = `<svg viewBox="0 0 ${W} ${H}" class="usage-svg" role="img" aria-label="Daily token usage chart">`;

  // Y-axis grid + labels
  for (const t of ticks) {
    const y = padT + innerH - (t / niceMax) * innerH;
    svg += `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" class="usage-grid-line"/>`;
    svg += `<text x="${padL - 8}" y="${(y + 3).toFixed(1)}" class="usage-yaxis" text-anchor="end">${fmtTokens(t)}</text>`;
  }

  // Stacked bars: tokens_out on bottom (teal), tokens_in on top (blue).
  days.forEach((d, i) => {
    const x = padL + i * (barW + barGap);
    if (!d.tokens) return;
    const outH = (d.tokens_out / niceMax) * innerH;
    const inH  = (d.tokens_in  / niceMax) * innerH;
    const tip = `${d.date}\n${fmtTokens(d.tokens)} tokens\n${fmtTokens(d.tokens_in)} in · ${fmtTokens(d.tokens_out)} out\n${d.active_users} active · ${d.analyses} analyses`;
    svg += `<g class="usage-bar-group"><title>${escapeHTML(tip)}</title>`;
    svg += `<rect x="${x.toFixed(1)}" y="${(padT + innerH - outH).toFixed(1)}" width="${barW.toFixed(2)}" height="${outH.toFixed(1)}" rx="1.5" class="usage-bar-out"/>`;
    svg += `<rect x="${x.toFixed(1)}" y="${(padT + innerH - outH - inH).toFixed(1)}" width="${barW.toFixed(2)}" height="${inH.toFixed(1)}" rx="1.5" class="usage-bar-in"/>`;
    svg += `</g>`;
  });

  // X-axis labels: first, last, every 5th day
  days.forEach((d, i) => {
    if (i !== 0 && i !== days.length - 1 && i % 5 !== 0) return;
    const x = padL + i * (barW + barGap) + barW / 2;
    svg += `<text x="${x.toFixed(1)}" y="${(H - padB + 16).toFixed(1)}" class="usage-xaxis" text-anchor="middle">${escapeHTML(d.date.slice(5))}</text>`;
  });

  // X-axis baseline
  svg += `<line x1="${padL}" y1="${(padT + innerH).toFixed(1)}" x2="${W - padR}" y2="${(padT + innerH).toFixed(1)}" class="usage-grid-baseline"/>`;
  svg += `</svg>`;
  host.innerHTML = svg;

  $("#usage-chart-legend").innerHTML = `
    <span class="legend-item"><span class="legend-swatch swatch-in"></span>Tokens in</span>
    <span class="legend-item"><span class="legend-swatch swatch-out"></span>Tokens out</span>
  `;

  // Quick footer stats: peak day + average
  const peak = days.reduce((acc, d) => (d.tokens > (acc?.tokens || 0) ? d : acc), null);
  const activeDays = days.filter((d) => d.tokens > 0);
  const avg = activeDays.length ? activeDays.reduce((s, d) => s + d.tokens, 0) / activeDays.length : 0;
  $("#usage-chart-stats").innerHTML = `
    <span><strong>Peak:</strong> ${peak && peak.tokens ? `${fmtTokens(peak.tokens)} on ${peak.date}` : "—"}</span>
    <span><strong>Avg / active day:</strong> ${fmtTokens(Math.round(avg))}</span>
    <span><strong>Active days:</strong> ${activeDays.length} of ${days.length}</span>
  `;
}

// ---------- Mini chart: daily analyses ----------

function renderMiniChart() {
  const host = $("#usage-mini-chart");
  if (!host || !dashState.data) return;
  const days = dashState.data.daily;
  const max = Math.max(1, ...days.map((d) => d.analyses));
  const W = Math.max(280, host.clientWidth || 360);
  const H = 160;
  const padL = 32, padR = 8, padT = 10, padB = 22;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const barGap = 2;
  const barW = Math.max(2, (innerW - barGap * (days.length - 1)) / days.length);
  const niceMax = niceCeil(max);

  let svg = `<svg viewBox="0 0 ${W} ${H}" class="usage-svg" role="img" aria-label="Daily analyses chart">`;
  // Just 2 Y ticks (0 + max) — keeps the mini chart uncluttered.
  for (const t of [0, niceMax]) {
    const y = padT + innerH - (t / niceMax) * innerH;
    svg += `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" class="usage-grid-line"/>`;
    svg += `<text x="${padL - 6}" y="${(y + 3).toFixed(1)}" class="usage-yaxis" text-anchor="end">${t}</text>`;
  }
  days.forEach((d, i) => {
    const x = padL + i * (barW + barGap);
    if (!d.analyses) return;
    const h = (d.analyses / niceMax) * innerH;
    svg += `<rect x="${x.toFixed(1)}" y="${(padT + innerH - h).toFixed(1)}" width="${barW.toFixed(2)}" height="${h.toFixed(1)}" rx="1.5" class="usage-bar-accent"><title>${escapeHTML(`${d.date}: ${d.analyses} analyses`)}</title></rect>`;
  });
  // X labels: first, mid, last
  [0, Math.floor(days.length / 2), days.length - 1].forEach((i) => {
    const d = days[i];
    if (!d) return;
    const x = padL + i * (barW + barGap) + barW / 2;
    svg += `<text x="${x.toFixed(1)}" y="${(H - padB + 14).toFixed(1)}" class="usage-xaxis" text-anchor="middle">${escapeHTML(d.date.slice(5))}</text>`;
  });
  svg += `<line x1="${padL}" y1="${(padT + innerH).toFixed(1)}" x2="${W - padR}" y2="${(padT + innerH).toFixed(1)}" class="usage-grid-baseline"/>`;
  svg += `</svg>`;
  host.innerHTML = svg;
}

// ---------- Top users panel ----------

function renderTopUsers() {
  const host = $("#usage-top-list");
  if (!host || !dashState.data) return;
  const users = [...dashState.data.per_user]
    .filter((u) => u.tokens_total > 0)
    .sort((a, b) => b.tokens_total - a.tokens_total)
    .slice(0, 8);
  if (!users.length) {
    host.innerHTML = `<li class="usage-empty">No usage yet.</li>`;
    return;
  }
  const peak = users[0].tokens_total || 1;
  const me = (dashState.currentUser?.email || "").toLowerCase();
  host.innerHTML = users.map((u, i) => {
    const pct = Math.max(2, (u.tokens_total / peak) * 100);
    const isMe = (u.email || "").toLowerCase() === me;
    const tier = (u.tier || "novice").toLowerCase();
    const name = u.username || (u.email ? u.email.split("@")[0] : "(unknown)");
    return `
      <li class="usage-top-row ${isMe ? "is-me" : ""}">
        <span class="usage-top-rank">${i + 1}</span>
        <div class="usage-top-meta">
          <div class="usage-top-name">${escapeHTML(name)}${isMe ? `<span class="usage-top-you">you</span>` : ""}</div>
          <div class="usage-top-sub subtle">${escapeHTML(u.email || "—")} · <span class="dash-tier-badge ${tier}">${escapeHTML(tier)}</span> · ${fmtInt(u.analyses)} analyses</div>
        </div>
        <div class="usage-top-bar-wrap" aria-hidden="true">
          <div class="usage-top-bar" style="width:${pct.toFixed(1)}%"></div>
        </div>
        <div class="usage-top-value">${fmtTokens(u.tokens_total)}</div>
      </li>
    `;
  }).join("");
}

// ---------- All-users table ----------

function renderDashboardUsers() {
  const body = $("#dash-users-body");
  const meta = $("#usage-users-meta");
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
  if (meta) {
    meta.textContent = dashState.search
      ? `${rows.length} of ${dashState.data.per_user.length} users match "${dashState.search}"`
      : `${dashState.data.per_user.length} users · sorted by ${key.replace("_", " ")}`;
  }
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="10" class="usage-empty">No users match.</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((u) => {
    const isMe = (u.email || "").toLowerCase() === me;
    const tier = (u.tier || "novice").toLowerCase();
    const name = u.username || (u.email ? u.email.split("@")[0] : "(unknown)");
    return `
      <tr class="${isMe ? "admin-row" : ""}">
        <td>
          <div class="usage-user-cell">
            <span class="usage-user-name">${escapeHTML(name)}${isMe ? `<span class="usage-user-you">you</span>` : ""}</span>
            <span class="usage-user-email">${escapeHTML(u.email || "—")}</span>
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

// ---------- Fetch wrapper — JWT injection mirrors app.js ----------

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
