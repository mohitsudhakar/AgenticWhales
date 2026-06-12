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


def test_dedupe_transactions():
    from agenticwhales.coach import dedupe_transactions
    t = lambda d, ty, s, q, p: Transaction(date=d, type=ty, symbol=s, quantity=q, price=p, amount=0)
    txns = [t("2025-01-01", "Buy", "AAPL", 10, 100), t("2025-01-01", "Buy", "AAPL", 10, 100),  # dup
            t("2025-01-02", "Sell", "AAPL", 10, 110)]
    assert len(dedupe_transactions(txns)) == 2


def test_behavioral_insights_and_monthly():
    txns = []
    # quick small winners (held 1d) in Jan; big slow losers (held ~60d) Jan->Mar
    for i in range(4):
        txns += [_t(f"2025-01-0{i+1}", "Buy", f"W{i}", 10, 100),
                 _t(f"2025-01-0{i+2}", "Sell", f"W{i}", 10, 103)]
    for i in range(2):
        txns += [_t(f"2025-01-1{i}", "Buy", f"L{i}", 10, 100),
                 _t(f"2025-03-1{i}", "Sell", f"L{i}", 10, 70)]
    rep = coach.audit_trades(txns)
    ins = rep.insights
    assert ins["n_trades"] == 6 and ins["period"]["label"] in ("this quarter", "this year")
    assert ins["hold_losers_days"] > ins["hold_winners_days"]
    assert ins["top_fix"] is not None and "headline" in ins
    assert ins["best_trade"]["pnl"] > 0 and ins["worst_trade"]["pnl"] < 0
    # monthly buckets present (Jan + Mar)
    months = {m["month"] for m in rep.monthly}
    assert "2025-01" in months and "2025-03" in months


def test_insights_period_label_adapts():
    txns = [_t("2025-01-06", "Buy", "AAPL", 10, 100), _t("2025-01-06", "Sell", "AAPL", 10, 102)]
    rep = coach.audit_trades(txns)
    assert rep.insights["period"]["label"] == "this day"


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


def test_open_tail_explains_chart_gap():
    """Buys newer than the last closed trade are reported so the UI can say
    why the discipline chart ends earlier than the synced history does."""
    txns = [
        _t("2026-03-01", "Buy", "AAPL", 10, 100),
        _t("2026-04-10", "Sell", "AAPL", 10, 110),   # last close: April
        _t("2026-05-05", "Buy", "MSFT", 5, 400),     # still open
        _t("2026-06-02", "Buy", "NVDA", 8, 130),     # still open
        # Noise that must NOT count as open tail:
        _t("2026-05-20", "Dividend", "AAPL", 0, 0),
        Transaction(date="2026-05-21", type="Buy Option", symbol="SPY",
                    quantity=1, price=5, amount=-500, description="SPY Call"),
    ]
    rep = coach.audit_trades(txns)
    assert [m["month"] for m in rep.monthly] == ["2026-04"]
    tail = rep.open_tail
    assert tail["n_open_buys"] == 2
    assert tail["last_close"] == "2026-04-10"
    assert tail["first"] == "2026-05-05" and tail["last"] == "2026-06-02"
    assert "open_tail" in rep.to_dict()


def test_open_tail_absent_when_nothing_open_after_last_close():
    txns = [
        _t("2026-03-01", "Buy", "AAPL", 10, 100),
        _t("2026-04-10", "Sell", "AAPL", 10, 110),
    ]
    assert coach.audit_trades(txns).open_tail is None
    assert coach.audit_trades([]).open_tail is None


def test_transaction_dates_normalize_to_iso():
    """US broker CSVs carry MM/DD/YYYY; unparsed dates silently drop trades
    out of the FIFO builder and the discipline chart, so the model boundary
    normalizes them."""
    assert _t("02/20/2026", "Buy", "AMD", 1, 100).date == "2026-02-20"
    assert _t("25/12/2025", "Buy", "AMD", 1, 100).date == "2025-12-25"   # unambiguous DD/MM
    assert _t("2026/02/20", "Buy", "AMD", 1, 100).date == "2026-02-20"   # YYYY/MM/DD
    assert _t("2025-01-06T00:00:00Z", "Buy", "A", 1, 1).date == "2025-01-06T00:00:00Z"
    assert _t("13/13/2025", "Buy", "A", 1, 1).date == "13/13/2025"       # invalid: untouched
    assert _t("not a date", "Buy", "A", 1, 1).date == "not a date"


def test_dedupe_collapses_same_trade_across_date_formats():
    """The same fill arriving via CSV ('02/20/2026') and via brokerage sync
    ('2026-02-20') must count once, not twice."""
    a = _t("02/20/2026", "Buy", "AAPL", 10, 150)
    b = _t("2026-02-20", "Buy", "AAPL", 10, 150)
    assert len(coach.dedupe_transactions([a, b])) == 1


def test_slash_dated_trades_are_scored():
    txns = [_t("01/06/2026", "Buy", "AAPL", 10, 100),
            _t("01/20/2026", "Sell", "AAPL", 10, 110)]
    rep = coach.audit_trades(txns)
    assert rep.n_trades == 1
    assert rep.monthly[0]["month"] == "2026-01"
