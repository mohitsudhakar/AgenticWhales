#!/usr/bin/env python
"""Behavioral trading coach — CLI.

    python tools/run_coach.py --demo              # synthetic trader, deterministic
    python tools/run_coach.py --demo --narrate    # + LLM coaching voice (DeepSeek)
    python tools/run_coach.py --csv my_history.csv

Reads a brokerage history, reconstructs round-trip trades, and prints a
dollar-quantified behavioral-leak audit + coaching. No orders, ever.
"""
from __future__ import annotations

import argparse
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from agenticwhales import coach
from agenticwhales.transactions.models import Transaction


def demo_history():
    return coach.sample_history()


def fmt(report: coach.CoachReport) -> str:
    L = []
    L.append("=" * 64)
    L.append("  BEHAVIORAL TRADING COACH — your discipline audit")
    L.append("=" * 64)
    L.append(f"  round-trip trades : {report.n_trades}")
    L.append(f"  hit-rate          : {report.hit_rate:.0%}")
    L.append(f"  avg win / avg loss: ${report.avg_win:,.0f} / ${report.avg_loss:,.0f}"
             f"   (payoff {report.payoff_ratio})")
    L.append(f"  expectancy/trade  : ${report.expectancy_per_trade:,.0f}")
    L.append(f"  net P&L           : ${report.total_pnl:,.0f}")
    L.append(f"  DISCIPLINE SCORE  : {report.discipline_score}/100")
    L.append("")
    L.append(f"  💸 Disciplined rules (size cap + 15% stop) would have changed your")
    L.append(f"     P&L from ${report.total_pnl:,.0f} to ${report.disciplined_pnl:,.0f}")
    L.append(f"     — a swing of ${report.total_quantified_leak:,.0f} from behavior alone.")
    L.append("")
    L.append("  Leak breakdown (estimated impact per pattern; patterns overlap, so")
    L.append("  these do NOT sum to the figure above):")
    L.append("")
    for i, lk in enumerate(report.leaks, 1):
        L.append(f"  {i}. [{lk.severity.upper()}] {lk.name}  →  ~${lk.dollars:,.0f}")
        L.append(f"     evidence: {lk.evidence}")
        L.append(f"     fix:      {lk.fix}")
        L.append("")
    if report.narrative:
        L.append("-" * 64)
        L.append("  COACH:")
        L.append("")
        for line in report.narrative.splitlines():
            L.append("  " + line)
    return "\n".join(L)


def fmt_verdict(v) -> str:
    icon = {"GO": "🟢 GO", "CAUTION": "🟡 CAUTION", "BLOCK": "🔴 BLOCK"}[v.verdict]
    L = ["", "=" * 64, f"  PRE-TRADE CHECK  →  {icon}", "=" * 64,
         f"  risk ${v.risk_dollars:,.0f} ({v.risk_pct_equity:.1%} of equity) · "
         f"notional ${v.notional:,.0f} · payoff {v.payoff_ratio}"]
    for c in v.checks:
        mark = {"pass": "✓", "warn": "!", "fail": "✗"}[c.status]
        L.append(f"  [{mark}] {c.name}: {c.message}")
        if c.suggestion:
            L.append(f"      → {c.suggestion}")
    d = v.disciplined
    L.append(f"\n  Disciplined version: {d['qty']:.0f} sh, stop ${d['stop']:,.2f}, "
             f"target ${d['target']:,.2f}  (risk ${d['risk_dollars']:,.0f})")
    return "\n".join(L)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--demo", action="store_true")
    p.add_argument("--csv", default=None)
    p.add_argument("--narrate", action="store_true", help="add an LLM coaching voice (DeepSeek)")
    p.add_argument("--fees", type=float, default=0.0, help="total fees paid (if not in CSV)")
    p.add_argument("--pretrade", action="store_true",
                   help="also run a sample pre-trade check against this history's profile")
    args = p.parse_args(argv)

    if args.demo:
        txns = demo_history()
    elif args.csv:
        from agenticwhales.transactions.parser import parse_transactions_csv_file
        txns = parse_transactions_csv_file(args.csv)
    else:
        p.error("pass --demo or --csv <file>")

    narrate = None
    if args.narrate:
        from agenticwhales.edge_probe import default_invokers
        invoke_text, _ = default_invokers(provider="deepseek", model="deepseek-v4-pro")
        narrate = coach.make_narrator(invoke_text)

    report = coach.audit_trades(txns, fees_paid=args.fees, narrate=narrate)
    print(fmt(report))

    if args.pretrade:
        from agenticwhales import pretrade as pt
        trips = coach.reconstruct_round_trips(txns)
        # A tempting oversized trade right after a loss — exactly this trader's leak.
        proposed = pt.ProposedTrade("NVDA", "long", qty=300, entry_price=120, stop_price=108)
        verdict = pt.check_trade(proposed, equity=200_000,
                                 recent_trades=trips, leak_profile=report)
        print(fmt_verdict(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
