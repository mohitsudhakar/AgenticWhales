"""Tests for the behavioral coach — FIFO correctness + leak quantification."""

from agenticwhales import coach
from agenticwhales.transactions.models import Transaction


def _t(date, typ, sym, qty, px):
    return Transaction(date=date, type=typ, symbol=sym, quantity=qty, price=px,
                       amount=(qty * px) * (-1 if typ == "Buy" else 1))


def test_fifo_round_trips():
    txns = [
        _t("2025-01-01", "Buy", "AAPL", 10, 100),
        _t("2025-01-05", "Buy", "AAPL", 10, 110),
        _t("2025-01-10", "Sell", "AAPL", 15, 120),
    ]
    trips = coach.reconstruct_round_trips(txns)
    assert len(trips) == 2
    # First lot fully closed: 10 @ 100 -> 120.
    assert trips[0].qty == 10 and trips[0].entry_px == 100 and trips[0].pnl == 200
    # Second lot partially closed: 5 @ 110 -> 120.
    assert trips[1].qty == 5 and trips[1].entry_px == 110 and trips[1].pnl == 50
    assert trips[0].hold_days == 9


def test_options_and_bad_rows_skipped():
    txns = [
        _t("2025-01-01", "Buy", "AAPL", 0, 0),                 # no qty/px
        Transaction(date="2025-01-02", type="Buy Option", symbol="AAPL",
                    quantity=1, price=5, amount=-500, description="AAPL Call"),
        _t("2025-01-03", "Dividend", "AAPL", 0, 0),
    ]
    assert coach.reconstruct_round_trips(txns) == []


def test_disposition_and_payoff_leaks_quantified():
    # Small quick wins + big slow losses -> disposition + inverted payoff.
    txns = []
    for i in range(5):  # quick small winners
        txns += [_t(f"2025-01-0{i+1}", "Buy", f"W{i}", 10, 100),
                 _t(f"2025-01-0{i+2}", "Sell", f"W{i}", 10, 103)]
    for i in range(3):  # big slow losers
        txns += [_t(f"2025-01-1{i}", "Buy", f"L{i}", 10, 100),
                 _t(f"2025-03-1{i}", "Sell", f"L{i}", 10, 70)]
    report = coach.audit_trades(txns)
    assert report.n_trades == 8
    names = " ".join(l.name for l in report.leaks).lower()
    assert "disposition" in names or "payoff" in names
    assert report.total_quantified_leak > 0
    assert report.discipline_score < 85


def test_narrate_uses_provided_numbers_only(monkeypatch):
    seen = {}

    def fake_invoke(msgs):
        seen["msgs"] = msgs
        return "Cut your losers. Start a 2% risk rule this week."

    txns = []
    for i in range(4):
        txns += [_t(f"2025-02-0{i+1}", "Buy", f"L{i}", 10, 100),
                 _t(f"2025-04-0{i+1}", "Sell", f"L{i}", 10, 60)]
        txns += [_t(f"2025-02-1{i}", "Buy", f"W{i}", 10, 100),
                 _t(f"2025-02-1{i}", "Sell", f"W{i}", 10, 101)]
    report = coach.audit_trades(txns, narrate=coach.make_narrator(fake_invoke))
    assert "2% risk rule" in report.narrative
    # The model is handed the deterministic numbers, not asked to compute them.
    human = [m for m in seen["msgs"] if m[0] == "human"][0][1]
    assert "expectancy" in human.lower()


def test_price_based_leaks_with_fake_fetcher():
    import pandas as pd
    from agenticwhales.coach import RoundTrip, price_based_leaks

    def fetch(sym, start, end):
        idx = pd.bdate_range("2025-01-01", periods=60)
        n = len(idx)
        if sym == "LOSE":   # held-window low dips to 80 (below a 15% stop at 85)
            return pd.DataFrame({"Open": [100]*n, "High": [101]*n, "Low": [80]*n, "Close": [90]*n}, index=idx)
        return pd.DataFrame({"Open": [110]*n, "High": [130]*n, "Low": [109]*n, "Close": [115]*n}, index=idx)

    trips = [
        RoundTrip("LOSE", "2025-01-02", "2025-01-20", 10, 100, 70, -300, 18, -30.0),  # big loss, stop would've helped
        RoundTrip("WIN", "2025-01-02", "2025-01-20", 10, 100, 110, 100, 18, 10.0),    # ran to 130 after exit
    ]
    leaks = price_based_leaks(trips, fetch)
    names = " ".join(l.name for l in leaks).lower()
    assert "stop-loss" in names and "cutting winners" in names
    assert all(l.dollars > 0 for l in leaks)


def test_price_based_leaks_graceful_when_no_data():
    from agenticwhales.coach import RoundTrip, price_based_leaks
    trips = [RoundTrip("X", "2025-01-02", "2025-01-20", 10, 100, 90, -100, 18, -10.0)]
    assert price_based_leaks(trips, lambda *a, **k: None) == []


def test_clean_trader_low_leak():
    # Symmetric, disciplined: similar win/loss size, no churn.
    txns = []
    for i in range(6):
        win = i % 2 == 0
        px_out = 110 if win else 95
        txns += [_t(f"2025-0{i+1}-01", "Buy", f"S{i}", 10, 100),
                 _t(f"2025-0{i+1}-20", "Sell", f"S{i}", 10, px_out)]
    report = coach.audit_trades(txns)
    assert report.discipline_score >= 60
