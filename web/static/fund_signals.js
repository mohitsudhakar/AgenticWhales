// /fund Signals tabs — X Recs, Congress, Trade History.
//
// Each handler is independent. The Trade History tab is a vanilla-JS port of
// robinhood-analyzer/components/Dashboard.tsx (recharts replaced with inline
// SVG so we don't pull in a charting bundle).

(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const escapeHtml = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const money = (n) => {
    const v = Number(n) || 0;
    const sign = v < 0 ? "-" : "";
    return `${sign}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  };

  function busy(btn, on, idleLabel, busyLabel) {
    if (!btn) return;
    btn.disabled = !!on;
    const label = btn.querySelector(".go-btn-label");
    if (label) label.textContent = on ? busyLabel : idleLabel;
  }

  function setStatus(el, msg, level) {
    if (!el) return;
    el.textContent = msg || "";
    el.className = "signals-status" + (level ? ` ${level}` : "");
  }

  window.addEventListener("DOMContentLoaded", () => {
    wireXRecs();
    wireCongress();
    wireTradeHistory();
  });

  // Auto-load saved trade history the first time the Trade History tab opens.
  let _thHistoryLoaded = false;
  window.addEventListener("aw-leaf-shown", (e) => {
    if (e.detail && e.detail.leaf === "trade_history" && !_thHistoryLoaded) {
      _thHistoryLoaded = true;
      loadSavedTradeHistory();
    }
  });

  async function loadSavedTradeHistory() {
    const dash = $("#th-dashboard");
    const status = $("#th-status");
    if (!dash) return;
    try {
      const res = await fetch("/api/transactions/metrics");
      if (!res.ok) return;            // signed out / no history → silent
      const data = await res.json();
      if (!data.count) return;        // nothing saved yet
      renderTradeHistory({ ...data, persisted: true, saved_count: data.count }, { dash, status, runLlm: false });
      setStatus(status, `Loaded ${data.count} saved transactions from your history. Upload a new CSV to add more.`, "ok");
    } catch (_) { /* best-effort */ }
  }

  // ====================================================================
  // X Recs
  // ====================================================================
  function wireXRecs() {
    const form = $("#xrecs-form");
    if (!form) return;
    const btn = $("#xrecs-go");
    const status = $("#xrecs-status");
    const summary = $("#xrecs-summary");
    const results = $("#xrecs-results");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const handle = $("#xrecs-handle").value.trim().replace(/^@/, "");
      const maxResults = Number($("#xrecs-max").value) || 30;
      if (!handle) {
        setStatus(status, "Enter an X handle.", "error");
        return;
      }
      busy(btn, true, "Extract recs", "Working…");
      setStatus(status, `Fetching tweets for @${handle}…`);
      summary.classList.add("hidden");
      results.innerHTML = "";
      try {
        const res = await fetch("/api/signals/x-recs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ handle, max_results: maxResults }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${res.status}`);
        }
        const data = await res.json();
        renderXRecs(data, { summary, results, status });
      } catch (err) {
        setStatus(status, `Failed: ${err.message || err}`, "error");
      } finally {
        busy(btn, false, "Extract recs", "Working…");
      }
    });
  }

  function renderXRecs(data, { summary, results, status }) {
    const recs = data.recommendations || [];
    const tweets = data.tweets || [];
    if (!tweets.length) {
      setStatus(status, `No recent tweets found for @${data.handle}.`, "warn");
      return;
    }
    if (!recs.length) {
      setStatus(
        status,
        `Scanned ${tweets.length} recent posts for @${data.handle}; no explicit trade recommendations detected.`,
        "warn",
      );
      return;
    }
    setStatus(status, `${recs.length} recommendation(s) from ${tweets.length} posts.`, "ok");
    const buys = recs.filter((r) => r.action === "buy").length;
    const sells = recs.filter((r) => r.action === "sell").length;
    const holds = recs.filter((r) => r.action === "hold").length;
    const avg = recs.reduce((s, r) => s + (r.conviction || 0), 0) / recs.length;
    summary.classList.remove("hidden");
    summary.innerHTML = `
      <div class="signals-summary-grid">
        <div><span>@${escapeHtml(data.handle)}</span><strong>${recs.length} recs</strong></div>
        <div><span>Buy / Sell / Hold</span><strong>${buys} · ${sells} · ${holds}</strong></div>
        <div><span>Avg conviction</span><strong>${avg.toFixed(2)}</strong></div>
        <div><span>Posts scanned</span><strong>${tweets.length}</strong></div>
      </div>
      <p class="signals-caveat subtle">Caveat: social-media posts are self-reported opinion, not advice, and may be promotional or manipulative.</p>
    `;
    const sorted = [...recs].sort((a, b) => (b.conviction || 0) - (a.conviction || 0));
    results.innerHTML = `
      <div class="signals-card">
        <table class="signals-tbl">
          <thead>
            <tr><th>Ticker</th><th>Action</th><th>Conviction</th><th>Timeframe</th><th>Rationale</th></tr>
          </thead>
          <tbody>
            ${sorted
              .map(
                (r) => `
              <tr>
                <td class="ticker-cell">${escapeHtml(r.ticker)}</td>
                <td><span class="action-pill action-${escapeHtml(r.action)}">${escapeHtml(r.action)}</span></td>
                <td>${convictionBar(r.conviction)}</td>
                <td>${escapeHtml(r.timeframe || "unspecified")}</td>
                <td>${escapeHtml(r.rationale || "")}</td>
              </tr>`,
              )
              .join("")}
          </tbody>
        </table>
      </div>
    `;
  }

  function convictionBar(c) {
    const v = Math.max(0, Math.min(1, Number(c) || 0));
    const pct = (v * 100).toFixed(0);
    return `
      <div class="conv-bar"><div class="conv-bar-fill" style="width:${pct}%"></div></div>
      <span class="conv-bar-val">${v.toFixed(2)}</span>
    `;
  }

  // ====================================================================
  // Congress
  // ====================================================================
  function wireCongress() {
    const form = $("#congress-form");
    if (!form) return;
    const btn = $("#congress-go");
    const status = $("#congress-status");
    const summary = $("#congress-summary");
    const results = $("#congress-results");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const ticker = $("#congress-ticker").value.trim().toUpperCase();
      const limit = Number($("#congress-limit").value) || 50;
      if (!ticker) {
        setStatus(status, "Enter a ticker.", "error");
        return;
      }
      busy(btn, true, "Fetch trades", "Working…");
      setStatus(status, `Fetching disclosed trades for ${ticker}…`);
      summary.classList.add("hidden");
      results.innerHTML = "";
      try {
        const res = await fetch("/api/signals/congress", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ticker, limit }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${res.status}`);
        }
        renderCongress(await res.json(), { summary, results, status });
      } catch (err) {
        setStatus(status, `Failed: ${err.message || err}`, "error");
      } finally {
        busy(btn, false, "Fetch trades", "Working…");
      }
    });
  }

  function renderCongress(data, { summary, results, status }) {
    const trades = data.trades || [];
    if (!trades.length) {
      setStatus(
        status,
        `No disclosed congressional transactions found for ${data.ticker}.`,
        "warn",
      );
      return;
    }
    setStatus(status, `${trades.length} disclosed transactions for ${data.ticker}.`, "ok");
    summary.classList.remove("hidden");
    summary.innerHTML = `
      <div class="signals-summary-grid">
        <div><span>Ticker</span><strong>${escapeHtml(data.ticker)}</strong></div>
        <div><span>Transactions</span><strong>${trades.length}</strong></div>
        <div><span>Purchases</span><strong class="pos">${data.buys || 0}</strong></div>
        <div><span>Sales</span><strong class="neg">${data.sells || 0}</strong></div>
      </div>
    `;
    results.innerHTML = `
      <div class="signals-card">
        <table class="signals-tbl">
          <thead>
            <tr><th>Date</th><th>Representative</th><th>Chamber</th><th>Party</th><th>Transaction</th><th>Amount</th></tr>
          </thead>
          <tbody>
            ${trades
              .map((r) => {
                const tx = (r.transaction || "").toLowerCase();
                const cls = tx.includes("buy") || tx.includes("purchase") ? "pos" : tx.includes("sell") || tx.includes("sale") ? "neg" : "";
                return `
              <tr>
                <td>${escapeHtml(r.transaction_date || "?")}</td>
                <td>${escapeHtml(r.representative || "?")}</td>
                <td>${escapeHtml(r.chamber || "?")}</td>
                <td>${escapeHtml(r.party || "?")}</td>
                <td class="${cls}">${escapeHtml(r.transaction || "?")}</td>
                <td>${escapeHtml(r.amount || "?")}</td>
              </tr>`;
              })
              .join("")}
          </tbody>
        </table>
      </div>
    `;
  }

  // ====================================================================
  // Trade History (port of robinhood-analyzer Dashboard.tsx)
  // ====================================================================
  function wireTradeHistory() {
    const form = $("#th-form");
    if (!form) return;
    const btn = $("#th-go");
    const status = $("#th-status");
    const dash = $("#th-dashboard");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fileInput = $("#th-file");
      const file = fileInput.files?.[0];
      if (!file) {
        setStatus(status, "Choose a CSV file.", "error");
        return;
      }
      const runLlm = $("#th-llm").checked;
      busy(btn, true, "Analyze", runLlm ? "Running LLM review…" : "Crunching…");
      setStatus(status, `Parsing ${file.name}…`);
      dash.classList.add("hidden");
      dash.innerHTML = "";
      try {
        const fd = new FormData();
        fd.append("file", file);
        fd.append("run_llm", runLlm ? "true" : "false");
        const res = await fetch("/api/signals/transactions", { method: "POST", body: fd });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${res.status}`);
        }
        const result = await res.json();
        renderTradeHistory(result, { dash, status, runLlm });
      } catch (err) {
        setStatus(status, `Failed: ${err.message || err}`, "error");
      } finally {
        busy(btn, false, "Analyze", runLlm ? "Running LLM review…" : "Crunching…");
      }
    });
  }

  function renderTradeHistory(result, { dash, status, runLlm }) {
    const m = result.metrics || {};
    const a = result.analysis || {};
    const warnings = result.warnings || [];
    const flags = m.behavioral_flags || [];
    const range = `${(m.date_range && m.date_range.start) || "?"} → ${(m.date_range && m.date_range.end) || "?"}`;
    const savedNote = result.persisted
      ? ` ✓ Saved ${result.saved_count} to your history.`
      : " (sign in to save this to your trade history)";
    setStatus(
      status,
      `Analyzed ${m.total_transactions || 0} transactions (${range}).${savedNote}`,
      "ok",
    );
    dash.classList.remove("hidden");

    const warnHtml = warnings.length
      ? `<div class="th-section">${warnings.map((w) => `<div class="th-flag">⚠ ${escapeHtml(w)}</div>`).join("")}</div>`
      : "";

    const headlineHtml = runLlm
      ? `
      <div class="th-section th-card">
        <div class="th-headline">${escapeHtml(a.headline || "Analysis complete.")}</div>
        <div><span class="th-archetype">${escapeHtml(a.investor_archetype || "Investor")}</span></div>
        <div class="th-scores">
          ${scoreCard("Risk-Taking", a.risk_score, true)}
          ${scoreCard("Discipline", a.discipline_score, false)}
          ${scoreCard("Diversification", a.diversification_score, false)}
        </div>
      </div>`
      : `
      <div class="th-section th-card">
        <div class="th-headline">Deterministic snapshot.</div>
        <p class="subtle">Re-run with the LLM review toggle on to add archetype, risk/discipline/diversification scores, and the 4-lens behavioral analysis.</p>
      </div>`;

    const statHtml = `
      <div class="th-section">
        <h2>Portfolio Snapshot</h2>
        <div class="th-stat-grid">
          ${stat("Transactions", m.total_transactions || 0)}
          ${stat("Date Range", `${(m.date_range && m.date_range.start) || "?"} → ${(m.date_range && m.date_range.end) || "?"}`)}
          ${stat("Total Invested", money(m.total_invested))}
          ${stat("Total Proceeds", money(m.total_proceeds))}
          ${stat("Realized P&L (FIFO est.)", money(m.net_realized_pnl), (m.net_realized_pnl || 0) >= 0 ? "pos" : "neg")}
          ${stat("Dividends", money(m.total_dividends), "pos")}
          ${stat("Fees", money(m.total_fees), (m.total_fees || 0) > 0 ? "neg" : "")}
          ${stat("Unique Symbols", m.unique_symbols || 0)}
          ${stat("Trades / Month", (m.trade_frequency_per_month || 0).toFixed ? m.trade_frequency_per_month.toFixed(1) : m.trade_frequency_per_month)}
          ${stat("Avg Trade Size", money(m.avg_trade_size))}
          ${stat("Top Concentration", `${(m.top_concentration && m.top_concentration.symbol) || "?"} ${(m.top_concentration && m.top_concentration.pct_of_invested) || 0}%`)}
          ${stat("Option Trades", m.option_trades || 0)}
        </div>
      </div>`;

    const chartsHtml = `
      <div class="th-section th-two-col">
        <div class="th-card">
          <h3>Monthly Buy / Sell Activity</h3>
          ${monthlySvg(m.monthly_activity || [])}
        </div>
        <div class="th-card">
          <h3>Activity by Type</h3>
          ${typePieSvg(m.type_breakdown || [])}
        </div>
      </div>
      ${
        (m.symbol_stats || []).length
          ? `<div class="th-section th-card">
        <h3>Capital Deployed by Symbol (top 8)</h3>
        ${symbolBarsSvg((m.symbol_stats || []).slice(0, 8))}
      </div>`
          : ""
      }`;

    const flagsHtml = flags.length
      ? `<div class="th-section"><h2>Behavioral Signals</h2>${flags.map((f) => `<div class="th-flag">⚠ ${escapeHtml(f)}</div>`).join("")}</div>`
      : "";

    const sectionsHtml =
      a.sections && a.sections.length
        ? `<div class="th-section">
        <h2>Expert Analysis</h2>
        <div class="th-persp">
          ${a.sections
            .map(
              (s) => `<div class="th-card">
            <h3>${escapeHtml(s.title || "")}</h3>
            <p>${escapeHtml(s.summary || "")}</p>
            <ul>${(s.points || []).map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>
          </div>`,
            )
            .join("")}
        </div>
      </div>`
        : "";

    const suggestionsHtml =
      a.suggestions && a.suggestions.length
        ? `<div class="th-section th-card">
        <h2 style="margin-top:0">Personalized Suggestions</h2>
        <ol class="th-list">${a.suggestions.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ol>
      </div>`
        : "";

    const habitsHtml =
      (a.habits_to_keep && a.habits_to_keep.length) ||
      (a.habits_to_change && a.habits_to_change.length)
        ? `<div class="th-section th-two-col">
        <div class="th-card">
          <h3 style="margin-top:0;color:var(--chart-2)">Habits to Keep</h3>
          <ul class="th-list">${(a.habits_to_keep || []).map((h) => `<li>${escapeHtml(h)}</li>`).join("")}</ul>
        </div>
        <div class="th-card">
          <h3 style="margin-top:0;color:#ffc857">Habits to Rethink</h3>
          <ul class="th-list">${(a.habits_to_change || []).map((h) => `<li>${escapeHtml(h)}</li>`).join("")}</ul>
        </div>
      </div>`
        : "";

    const symbolTableHtml =
      (m.symbol_stats || []).length
        ? `<div class="th-section th-card">
        <h3 style="margin-top:0">Per-Symbol Detail</h3>
        <div style="overflow-x:auto">
          <table class="signals-tbl">
            <thead><tr><th>Symbol</th><th>Buys</th><th>Sells</th><th>Invested</th><th>Proceeds</th><th>Realized P&L</th><th>Dividends</th></tr></thead>
            <tbody>
              ${(m.symbol_stats || [])
                .slice(0, 25)
                .map(
                  (s) => `<tr>
                <td class="ticker-cell">${escapeHtml(s.symbol)}</td>
                <td>${s.buys}</td>
                <td>${s.sells}</td>
                <td>${money(s.invested)}</td>
                <td>${money(s.proceeds)}</td>
                <td class="${(s.realized_pnl || 0) >= 0 ? "pos" : "neg"}">${money(s.realized_pnl)}</td>
                <td>${money(s.dividends)}</td>
              </tr>`,
                )
                .join("")}
            </tbody>
          </table>
        </div>
      </div>`
        : "";

    const reflectionHtml =
      a.closing_reflection
        ? `<div class="th-section th-card"><p class="th-reflection">${escapeHtml(a.closing_reflection)}</p></div>`
        : "";

    dash.innerHTML = `
      ${warnHtml}
      ${headlineHtml}
      ${statHtml}
      ${chartsHtml}
      ${flagsHtml}
      ${sectionsHtml}
      ${suggestionsHtml}
      ${habitsHtml}
      ${symbolTableHtml}
      ${reflectionHtml}
      <div class="th-disclaimer">Realized P&L is a FIFO estimate from the uploaded transactions and may differ from official tax statements. Educational analysis only — not financial advice.</div>
    `;
  }

  function stat(k, v, cls) {
    return `<div class="th-stat"><div class="th-stat-k">${escapeHtml(k)}</div><div class="th-stat-v ${cls || ""}">${typeof v === "string" ? escapeHtml(v) : v}</div></div>`;
  }

  function scoreCard(label, value, invert) {
    const n = Math.max(0, Math.min(100, Number(value) || 0));
    const good = invert ? n < 40 : n >= 60;
    const mid = n >= 40 && n < 60;
    const color = good ? "var(--chart-2)" : mid ? "#ffc857" : "var(--neg)";
    return `
      <div class="th-score">
        <div class="th-score-label">${escapeHtml(label)}</div>
        <div class="th-score-val" style="color:${color}">${n}<span class="th-score-unit">/100</span></div>
        <div class="th-score-bar"><div style="width:${n}%;background:${color}"></div></div>
      </div>`;
  }

  // ---------- inline SVG charts ----------
  function monthlySvg(rows) {
    if (!rows.length) return `<div class="th-empty subtle">No monthly activity.</div>`;
    const W = 560, H = 220, PAD = { l: 36, r: 12, t: 12, b: 28 };
    const innerW = W - PAD.l - PAD.r;
    const innerH = H - PAD.t - PAD.b;
    const maxV = Math.max(1, ...rows.flatMap((r) => [r.buys || 0, r.sells || 0]));
    const groupW = innerW / rows.length;
    const barW = Math.min(14, (groupW - 4) / 2);
    const gridYs = [0, 0.25, 0.5, 0.75, 1].map((f) => PAD.t + innerH * (1 - f));
    return `
      <svg viewBox="0 0 ${W} ${H}" class="th-chart">
        ${gridYs
          .map(
            (y, i) => `<line x1="${PAD.l}" x2="${W - PAD.r}" y1="${y}" y2="${y}" stroke="var(--chart-grid)" stroke-dasharray="3 3"/>
              <text x="${PAD.l - 6}" y="${y + 4}" text-anchor="end" fill="var(--chart-axis)" font-size="10">${Math.round((maxV * (4 - i)) / 4)}</text>`,
          )
          .join("")}
        ${rows
          .map((r, i) => {
            const xCenter = PAD.l + groupW * (i + 0.5);
            const buyH = ((r.buys || 0) / maxV) * innerH;
            const sellH = ((r.sells || 0) / maxV) * innerH;
            return `
            <rect x="${xCenter - barW - 1}" y="${PAD.t + innerH - buyH}" width="${barW}" height="${buyH}" fill="var(--chart-1)">
              <title>${escapeHtml(r.month)} buys: ${r.buys}</title>
            </rect>
            <rect x="${xCenter + 1}" y="${PAD.t + innerH - sellH}" width="${barW}" height="${sellH}" fill="var(--chart-2)">
              <title>${escapeHtml(r.month)} sells: ${r.sells}</title>
            </rect>
            ${
              i % Math.max(1, Math.ceil(rows.length / 8)) === 0
                ? `<text x="${xCenter}" y="${H - 8}" text-anchor="middle" fill="var(--chart-axis)" font-size="10">${escapeHtml(r.month)}</text>`
                : ""
            }
          `;
          })
          .join("")}
        <g transform="translate(${PAD.l}, 6)">
          <rect width="10" height="10" fill="var(--chart-1)"/><text x="14" y="9" font-size="10" fill="var(--chart-axis)">Buys</text>
          <rect x="56" width="10" height="10" fill="var(--chart-2)"/><text x="70" y="9" font-size="10" fill="var(--chart-axis)">Sells</text>
        </g>
      </svg>`;
  }

  function typePieSvg(rows) {
    if (!rows.length) return `<div class="th-empty subtle">No activity to break down.</div>`;
    const colors = ["var(--chart-2)", "var(--chart-1)", "#ffc857", "var(--neg)", "#b794f6", "#4fd1c5", "#f6ad55"];
    const total = rows.reduce((s, r) => s + (r.count || 0), 0) || 1;
    const cx = 110, cy = 110, R = 85;
    let a0 = -Math.PI / 2;
    const slices = rows.map((r, i) => {
      const frac = (r.count || 0) / total;
      const a1 = a0 + frac * Math.PI * 2;
      const large = frac > 0.5 ? 1 : 0;
      const x0 = cx + R * Math.cos(a0);
      const y0 = cy + R * Math.sin(a0);
      const x1 = cx + R * Math.cos(a1);
      const y1 = cy + R * Math.sin(a1);
      const path = `M${cx},${cy} L${x0},${y0} A${R},${R} 0 ${large} 1 ${x1},${y1} Z`;
      const labelAng = (a0 + a1) / 2;
      const lx = cx + (R + 12) * Math.cos(labelAng);
      const ly = cy + (R + 12) * Math.sin(labelAng);
      const anchor = lx < cx ? "end" : "start";
      a0 = a1;
      return { path, color: colors[i % colors.length], name: r.type, count: r.count, lx, ly, anchor, frac };
    });
    return `
      <svg viewBox="0 0 360 230" class="th-chart">
        ${slices
          .map(
            (s) => `<path d="${s.path}" fill="${s.color}" stroke="var(--bg)" stroke-width="1"><title>${escapeHtml(s.name)}: ${s.count}</title></path>`,
          )
          .join("")}
        ${slices
          .filter((s) => s.frac >= 0.05)
          .map(
            (s) => `<text x="${s.lx}" y="${s.ly}" text-anchor="${s.anchor}" fill="var(--text)" font-size="11">${escapeHtml(s.name)}</text>`,
          )
          .join("")}
      </svg>`;
  }

  function symbolBarsSvg(rows) {
    if (!rows.length) return `<div class="th-empty subtle">No symbols to plot.</div>`;
    const W = 660, H = 240, PAD = { l: 44, r: 12, t: 12, b: 32 };
    const innerW = W - PAD.l - PAD.r;
    const innerH = H - PAD.t - PAD.b;
    const maxV = Math.max(1, ...rows.flatMap((r) => [r.invested || 0, Math.abs(r.realized_pnl || 0)]));
    const groupW = innerW / rows.length;
    const barW = Math.min(18, (groupW - 6) / 2);
    return `
      <svg viewBox="0 0 ${W} ${H}" class="th-chart">
        ${[0, 0.25, 0.5, 0.75, 1]
          .map((f) => {
            const y = PAD.t + innerH * (1 - f);
            return `<line x1="${PAD.l}" x2="${W - PAD.r}" y1="${y}" y2="${y}" stroke="var(--chart-grid)" stroke-dasharray="3 3"/>
              <text x="${PAD.l - 6}" y="${y + 4}" text-anchor="end" fill="var(--chart-axis)" font-size="10">${money(maxV * f)}</text>`;
          })
          .join("")}
        ${rows
          .map((r, i) => {
            const xCenter = PAD.l + groupW * (i + 0.5);
            const invH = ((r.invested || 0) / maxV) * innerH;
            const realH = (Math.abs(r.realized_pnl || 0) / maxV) * innerH;
            const realColor = (r.realized_pnl || 0) >= 0 ? "var(--chart-2)" : "var(--neg)";
            return `
            <rect x="${xCenter - barW - 1}" y="${PAD.t + innerH - invH}" width="${barW}" height="${invH}" fill="var(--chart-1)">
              <title>${escapeHtml(r.symbol)} invested: ${money(r.invested)}</title>
            </rect>
            <rect x="${xCenter + 1}" y="${PAD.t + innerH - realH}" width="${barW}" height="${realH}" fill="${realColor}">
              <title>${escapeHtml(r.symbol)} realized: ${money(r.realized_pnl)}</title>
            </rect>
            <text x="${xCenter}" y="${H - 10}" text-anchor="middle" fill="var(--text)" font-size="11">${escapeHtml(r.symbol)}</text>
          `;
          })
          .join("")}
        <g transform="translate(${PAD.l}, 6)">
          <rect width="10" height="10" fill="var(--chart-1)"/><text x="14" y="9" font-size="10" fill="var(--chart-axis)">Invested</text>
          <rect x="78" width="10" height="10" fill="var(--chart-2)"/><text x="92" y="9" font-size="10" fill="var(--chart-axis)">Realized P&amp;L</text>
        </g>
      </svg>`;
  }
})();
