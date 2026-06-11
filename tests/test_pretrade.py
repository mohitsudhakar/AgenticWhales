"""Tests for the pre-trade decision-support check."""

from agenticwhales import pretrade as pt
from agenticwhales.coach import RoundTrip


def test_clean_trade_passes():
    t = pt.ProposedTrade("AAPL", "long", qty=50, entry_price=200,
                         stop_price=196, target_price=210)
    v = pt.check_trade(t, equity=100_000)
    assert v.verdict == "GO"
    assert v.risk_pct_equity <= 0.02


def test_oversized_risk_blocks_and_resizes():
    t = pt.ProposedTrade("TSLA", "long", qty=1000, entry_price=250, stop_price=225)
    v = pt.check_trade(t, equity=100_000)
    assert v.verdict == "BLOCK"
    risk = next(c for c in v.checks if c.name == "Risk per trade")
    assert risk.status == "fail"
    # Disciplined resize must bring risk to the cap (2% of 100k = $2k / $25 per-share).
    assert v.disciplined["qty"] == 80


def test_missing_stop_warns_and_suggests():
    t = pt.ProposedTrade("MSFT", "long", qty=10, entry_price=400)
    v = pt.check_trade(t, equity=100_000)
    assert v.verdict in ("CAUTION", "BLOCK")
    assert any(c.name == "Stop-loss" and c.status == "warn" for c in v.checks)
    assert v.disciplined["stop"] < 400


def test_poor_payoff_warns():
    t = pt.ProposedTrade("NVDA", "long", qty=10, entry_price=130,
                         stop_price=124, target_price=133)
    v = pt.check_trade(t, equity=100_000)
    payoff = next(c for c in v.checks if c.name == "Payoff ratio")
    assert payoff.status == "warn" and v.payoff_ratio is not None and v.payoff_ratio < 1.5


def test_revenge_pattern_blocks():
    # RoundTrip(symbol, entry_date, exit_date, qty, entry_px, exit_px, pnl, hold_days, return_pct)
    recent = [
        RoundTrip("AAPL", "2025-01-01", "2025-01-05", 10, 100, 104, 40, 4, 4.0),
        RoundTrip("COIN", "2025-01-06", "2025-01-10", 10, 100, 80, -200, 4, -20.0),  # a loss, last
    ]
    # A trade much bigger than the ~1000 median notional, right after the loss.
    t = pt.ProposedTrade("NVDA", "long", qty=100, entry_price=130, stop_price=125)
    v = pt.check_trade(t, equity=500_000, recent_trades=recent)
    assert any(c.name == "Revenge-trade pattern" and c.status == "fail" for c in v.checks)
    assert v.verdict == "BLOCK"


def test_short_side_stop_and_target_geometry():
    t = pt.ProposedTrade("XOM", "short", qty=10, entry_price=100)
    v = pt.check_trade(t, equity=100_000)
    # Short: default stop is ABOVE entry, suggested target BELOW.
    assert v.disciplined["stop"] > 100
    assert v.disciplined["target"] < 100


def test_directive_detector():
    assert pt.contains_directive("You should buy this dip")
    assert pt.contains_directive("Sell it all")
    assert pt.contains_directive("we recommend entering")
    assert not pt.contains_directive("Cut size to ~80 shares to risk 2%")
    assert not pt.contains_directive("Trade this at normal size or not today.")


def test_directive_detector_soft_directives():
    """Hardened forms the original regex missed (2026-06-10 compliance review)."""
    assert pt.contains_directive("consider selling into strength")
    assert pt.contains_directive("it might be worth adding here")
    assert pt.contains_directive("you should exit before earnings")
    assert pt.contains_directive("exit your position by Friday")
    assert pt.contains_directive("add to the position on weakness")
    assert pt.contains_directive("should hold through the dip")
    # Process/descriptive copy must still pass:
    assert not pt.contains_directive("instead of selling on the first pop")
    assert not pt.contains_directive("Define the exit before the entry")
    assert not pt.contains_directive("you hold losers 60d vs winners 2d")
    assert not pt.contains_directive("a trailing stop keeps you in winners")


def test_coach_generated_strings_pass_tripwire():
    """Every string the coach generates (leak names/evidence/fixes, insight
    headline) must survive the hardened directive tripwire."""
    from agenticwhales import coach

    report = coach.audit_trades(coach.sample_history())
    strings = [report.insights.get("headline", "")]
    for l in report.leaks:
        strings += [l.name, l.evidence, l.fix]
    for s in strings:
        assert not pt.contains_directive(s), f"directive leaked: {s!r}"


def test_no_directive_leaks_into_user_facing_strings():
    """S2 advice posture: every check scenario must emit process guidance only —
    no buy/sell directive in any user-facing string."""
    from agenticwhales import coach

    profile = coach.audit_trades(coach.sample_history())
    recent = coach.reconstruct_round_trips(coach.sample_history())
    scenarios = [
        (pt.ProposedTrade("AAPL", "long", qty=50, entry_price=200,
                          stop_price=196, target_price=210), {}),
        (pt.ProposedTrade("TSLA", "long", qty=1000, entry_price=250,
                          stop_price=225), {}),                       # oversized
        (pt.ProposedTrade("MSFT", "long", qty=10, entry_price=400), {}),  # no stop
        (pt.ProposedTrade("NVDA", "long", qty=10, entry_price=130,
                          stop_price=124, target_price=133), {}),     # poor payoff
        (pt.ProposedTrade("XOM", "short", qty=10, entry_price=100), {}),
        (pt.ProposedTrade("NVDA", "long", qty=900, entry_price=130, stop_price=125),
         {"recent_trades": recent, "leak_profile": profile}),         # behavioral
    ]
    for trade, kw in scenarios:
        v = pt.check_trade(trade, equity=100_000, **kw)
        strings = [v.verdict]
        for c in v.checks:
            strings += [c.name, c.message, c.suggestion]
        for s in strings:
            assert not pt.contains_directive(s), f"directive leaked: {s!r}"
