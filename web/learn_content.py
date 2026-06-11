"""Programmatic SEO pages (/learn/<slug>) — data-driven from one dict.

High-intent, low-competition queries: broker CSV-export how-tos (the exact
moment someone has their history in hand) and leak explainers. Content rules:
educational, past-tense, no directives, every page CTAs to the coach with the
zero-friction path ("or just paste a screenshot"). Broker UI instructions
drift — each guide carries a visible "last reviewed" date and gets a quarterly
review pass (see docs/marketing/coach-gtm-playbook.md).
"""

from __future__ import annotations

from html import escape
from typing import Dict, List, Optional

LAST_REVIEWED = "2026-06-10"

# slug -> {title, meta, h1, intro, sections: [(heading, [paragraphs])], reviewed}
LEARN_PAGES: Dict[str, Dict] = {
    "robinhood-export-trade-history-csv": {
        "title": "How to export your Robinhood trade history (CSV)",
        "meta": "Step-by-step: download your full Robinhood trade history as a "
                "CSV or account statement, and what to do with it next.",
        "h1": "Export your Robinhood trade history",
        "intro": "Robinhood keeps your full activity history, but the export lives a few "
                 "menus deep. Here's the current path, plus a faster alternative if you "
                 "just want your trades analyzed.",
        "sections": [
            ("From the app or web", [
                "Open Account → Menu (☰) → History.",
                "Choose Reports and statements → Reports, then Generate new report.",
                "Pick the date range (go back as far as your account allows) and request the report.",
                "Robinhood emails you a CSV — usually within a few minutes for small accounts.",
            ]),
            ("If you only need a statement", [
                "Account → Statements lists monthly PDFs. They parse fine too — upload the PDF directly.",
            ]),
            ("The zero-export shortcut", [
                "Skip the export entirely: screenshot your History screen (a few screens "
                "are fine) and paste the images straight into the coach. The vision "
                "reader extracts the rows, and you can spot-check them before anything "
                "is computed.",
            ]),
        ],
    },
    "schwab-export-trade-history-csv": {
        "title": "How to export your Charles Schwab trade history (CSV)",
        "meta": "Download your Schwab transaction history as CSV in a few clicks, "
                "and what the export does and doesn't include.",
        "h1": "Export your Schwab trade history",
        "intro": "Schwab's export is one of the cleaner ones — a real CSV with dates, "
                 "actions, quantities and prices.",
        "sections": [
            ("On schwab.com", [
                "Log in and open Accounts → History.",
                "Set the date range (up to 4 years online; older needs statements).",
                "Click Export (top right of the table) and choose CSV.",
            ]),
            ("Watch for", [
                "Transfers and dividends appear alongside trades — that's fine; the "
                "coach ignores non-trade rows automatically.",
                "Options rows are recognized and excluded from stock round-trip math.",
            ]),
            ("No desktop handy?", [
                "Screenshot the History page from the app and paste the images into the "
                "coach — the vision reader handles the rest.",
            ]),
        ],
    },
    "fidelity-export-trade-history-csv": {
        "title": "How to export your Fidelity trade history (CSV)",
        "meta": "Get your Fidelity activity and orders out as a CSV download, "
                "including the 5-year history window.",
        "h1": "Export your Fidelity trade history",
        "intro": "Fidelity exposes downloads on the Activity & Orders page; history goes "
                 "back five years online.",
        "sections": [
            ("On fidelity.com", [
                "Open Accounts & Trade → Activity & Orders.",
                "Filter to the account and date range you want (custom ranges allowed).",
                "Click Download (the arrow icon above the table) and choose CSV.",
            ]),
            ("Statements work too", [
                "Monthly/quarterly PDF statements upload fine — the extractor reads them. "
                "Scanned paper statements work through the vision reader.",
            ]),
        ],
    },
    "ibkr-export-trade-history-csv": {
        "title": "How to export your IBKR trade history (Flex Query / CSV)",
        "meta": "Interactive Brokers trade exports: Activity Statements vs Flex "
                "Queries, and the quickest CSV path.",
        "h1": "Export your IBKR trade history",
        "intro": "IBKR gives you two paths: quick Activity Statements, or Flex Queries "
                 "for precise custom exports.",
        "sections": [
            ("Quick path — Activity Statement", [
                "In Client Portal: Performance & Reports → Statements.",
                "Run an Activity statement for your range and download as CSV.",
            ]),
            ("Power path — Flex Query", [
                "Performance & Reports → Flex Queries → create a Trade Confirmation "
                "Flex Query with the fields you want, then run it for any range.",
                "Flex Queries are reusable — set it up once, re-run monthly.",
            ]),
            ("Tip", [
                "Either export uploads directly; screenshots of the Trades screen also "
                "work via the vision reader.",
            ]),
        ],
    },
    "what-is-revenge-trading": {
        "title": "Revenge trading: what it is and how to spot it in your own history",
        "meta": "Revenge trading explained — the loss-chasing pattern, how it shows up "
                "in fill data, and the process rules traders use against it.",
        "h1": "Revenge trading, measured",
        "intro": "Revenge trading is re-entering the market quickly after a loss with "
                 "outsized size — chasing the loss back. It's one of the most common "
                 "and most measurable behavioral leaks in retail trading histories.",
        "sections": [
            ("What it looks like in fill data", [
                "A losing exit, then within a day or two a new entry at well above your "
                "typical position size. One occurrence is noise; a repeated pattern with "
                "negative P&L on those oversized entries is the signature.",
                "Behavioral-finance research has documented loss-chasing in retail "
                "trading for decades (e.g. Barber & Odean's work on overtrading).",
            ]),
            ("Process rules traders use", [
                "A cooldown: no new position for a fixed window after any loss.",
                "Size normalization: the next trade after a loss must be at or below "
                "median size — never above.",
                "These are process constraints a trader adopts for themselves, not "
                "trade signals.",
            ]),
            ("Measure yours", [
                "The coach detects the pattern in your own history and prices it: the "
                "realized P&L of oversized post-loss entries, in dollars, past tense. "
                "Upload a CSV — or just paste a screenshot of your trades.",
            ]),
        ],
    },
    "disposition-effect-cost": {
        "title": "The disposition effect: what holding losers actually costs",
        "meta": "The disposition effect — cutting winners early while holding losers — "
                "how it's measured from your own trade history, in dollars.",
        "h1": "The disposition effect, in dollars",
        "intro": "The disposition effect is the best-documented bias in retail trading: "
                 "winners get sold fast, losers get held and hoped on. The cost is "
                 "measurable from any trade history.",
        "sections": [
            ("The signature", [
                "Average holding time on losers far above winners, while the average "
                "loss exceeds the average win. Even a good hit-rate loses money under "
                "that payoff shape.",
                "Odean (1998) documented the pattern across tens of thousands of retail "
                "accounts; it has replicated ever since.",
            ]),
            ("How the cost is computed", [
                "A deterministic counterfactual on your own fills: what the losses "
                "would have been had each loser been cut at the size of your average "
                "winner. Arithmetic, not prediction — what happened, not what will.",
            ]),
            ("Check your own history", [
                "Upload a brokerage CSV or statement — or paste a screenshot — and the "
                "coach quantifies the pattern in your trades, with the exact process "
                "rule that addresses it.",
            ]),
        ],
    },
}


def learn_slugs() -> List[str]:
    return list(LEARN_PAGES.keys())


_PAGE_TMPL = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} — AgenticWhales</title>
  <meta name="description" content="{meta}" />
  <link rel="canonical" href="{canonical}" />
  <meta property="og:title" content="{title}" />
  <meta property="og:description" content="{meta}" />
  <meta property="og:type" content="article" />
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {{ theme: {{ extend: {{
      colors: {{ cream: '#FAF6EE', ink: '#1A1A17', muted: '#6B6657', line: '#E7E0D2', emerald: '#1F6F54' }},
      fontFamily: {{ sans: ['Inter','sans-serif'], display: ['Instrument Serif','Georgia','serif'] }},
    }}}}}}
  </script>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Instrument+Serif:ital@0;1&family=Lora:wght@600&display=swap" rel="stylesheet">
  <style>
    body {{ font-family: 'Inter', sans-serif; background: #FAF6EE; color: #1A1A17; }}
    .font-display {{ font-family: 'Instrument Serif', Georgia, serif; letter-spacing: -0.015em; }}
    .font-logo {{ font-family: 'Lora', Georgia, serif; font-weight: 600; }}
  </style>
</head>
<body>
  <nav class="border-b border-line">
    <div class="max-w-3xl mx-auto px-6 h-16 flex items-center justify-between">
      <a href="/" class="flex items-center gap-2.5"><span class="text-xl">🐋</span><span class="font-logo text-xl">AgenticWhales</span></a>
      <div class="flex items-center gap-5 text-sm text-muted">
        <a href="/learn" class="hover:text-ink transition">Learn</a>
        <a href="/coach" class="hover:text-ink transition">Coach</a>
      </div>
    </div>
  </nav>
  <main class="max-w-3xl mx-auto px-6 py-14">
    <h1 class="font-display text-4xl sm:text-5xl mb-4">{h1}</h1>
    <p class="text-muted text-lg mb-8 leading-relaxed">{intro}</p>
    <div class="space-y-8 text-[15px] leading-relaxed">{sections}</div>
    <div class="mt-12 border border-line rounded-2xl p-6 bg-white/60">
      <p class="font-display text-2xl mb-2">See what your own habits cost you</p>
      <p class="text-muted mb-4">Upload a CSV or statement — or just paste a screenshot of your trades.
      A dollar-quantified discipline audit, free, no signup needed to try it.</p>
      <a href="/coach" class="inline-block bg-ink text-cream px-6 py-3 rounded-full text-sm font-medium hover:opacity-90 transition">Run your audit</a>
    </div>
    <p class="text-xs text-muted mt-8">Last reviewed {reviewed}. Broker interfaces change —
    if a step looks different, the broker's own help center has the latest path.</p>
  </main>
  <footer class="max-w-3xl mx-auto px-6 py-10 border-t border-line text-sm text-muted">
    <p><span class="text-ink font-medium">Educational, not investment advice.</span>
    Figures discussed are historical attribution from a trader's own history — never predictions or recommendations.
    <a href="/methodology" class="underline underline-offset-2">Methodology</a> ·
    <a href="/security" class="underline underline-offset-2">Your data</a></p>
  </footer>
</body>
</html>"""


def render_learn_page(slug: str, base_url: str = "") -> Optional[str]:
    page = LEARN_PAGES.get(slug)
    if page is None:
        return None
    sections_html = "".join(
        '<section><h2 class="font-display text-2xl mb-2">{h}</h2>'.format(h=escape(h))
        + '<ul class="list-disc pl-5 space-y-2 text-muted">'
        + "".join(f"<li>{escape(p)}</li>" for p in paras)
        + "</ul></section>"
        for h, paras in page["sections"]
    )
    return _PAGE_TMPL.format(
        title=escape(page["title"]), meta=escape(page["meta"]),
        h1=escape(page["h1"]), intro=escape(page["intro"]),
        sections=sections_html,
        canonical=f"{base_url.rstrip('/')}/learn/{slug}" if base_url else f"/learn/{slug}",
        reviewed=page.get("reviewed", LAST_REVIEWED),
    )


def render_learn_index(base_url: str = "") -> str:
    items = "".join(
        f'<a href="/learn/{slug}" class="block border border-line rounded-2xl p-6 bg-white/60 hover:bg-white transition">'
        f'<p class="font-display text-2xl mb-1">{escape(p["title"])}</p>'
        f'<p class="text-muted text-sm">{escape(p["meta"])}</p></a>'
        for slug, p in LEARN_PAGES.items()
    )
    return _PAGE_TMPL.format(
        title="Learn — trading discipline, measured", h1="Learn",
        meta="Broker export guides and behavioral-leak explainers — educational, "
             "past-tense, no predictions.",
        intro="Export guides for the moment you have your history in hand, and "
              "plain-English explainers for the leaks the coach measures.",
        sections=f'<div class="grid gap-4">{items}</div>',
        canonical=f"{base_url.rstrip('/')}/learn" if base_url else "/learn",
        reviewed=LAST_REVIEWED,
    )
