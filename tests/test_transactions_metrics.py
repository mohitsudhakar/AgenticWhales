"""Unit tests for the deterministic transactions metrics engine.

These exhaustively exercise the FIFO realized-PnL math and the behavioral
flags ported from robinhood-analyzer/lib/metrics.ts. No LLM, no network —
``compute_metrics`` is a pure function.
"""

from __future__ import annotations

import pytest

from agenticwhales.transactions import Transaction, compute_metrics

pytestmark = pytest.mark.unit


def _t(date="2024-01-01", type="Buy", symbol="", description="", quantity=0.0, price=0.0, amount=0.0):
    return Transaction(
        date=date,
        type=type,
        symbol=symbol,
        description=description,
        quantity=quantity,
        price=price,
        amount=amount,
    )


class TestEmptyAndBasics:
    def test_empty(self):
        m = compute_metrics([])
        assert m.total_transactions == 0
        assert m.date_range.start == ""
        assert m.net_realized_pnl == 0.0
        assert m.unique_symbols == 0
        assert m.behavioral_flags == []

    def test_date_range_sorted_iso_only(self):
        txns = [
            _t(date="2024-03-15", type="Buy", symbol="AAPL", quantity=1, price=10, amount=-10),
            _t(date="not-a-date", type="Fee", amount=-1),
            _t(date="2024-01-02", type="Buy", symbol="AAPL", quantity=1, price=8, amount=-8),
        ]
        m = compute_metrics(txns)
        assert m.date_range.start == "2024-01-02"
        assert m.date_range.end == "2024-03-15"

    def test_totals_use_abs_amount(self):
        txns = [
            _t(type="Buy", symbol="AAPL", quantity=10, price=100, amount=-1000),
            _t(type="Sell", symbol="AAPL", quantity=5, price=120, amount=600),
            _t(type="Dividend", symbol="AAPL", amount=5),
            _t(type="Deposit", amount=2000),
            _t(type="Withdrawal", amount=-300),
            _t(type="Fee", amount=-2),
        ]
        m = compute_metrics(txns)
        assert m.total_invested == 1000.0
        assert m.total_proceeds == 600.0
        assert m.total_dividends == 5.0
        assert m.total_deposits == 2000.0
        assert m.total_withdrawals == 300.0
        assert m.total_fees == 2.0
        assert m.total_buys == 1
        assert m.total_sells == 1


class TestFifoRealizedPnl:
    def test_simple_profit(self):
        # Buy 10 @ 100, sell 10 @ 120 -> +200 realized.
        txns = [
            _t(type="Buy", symbol="AAPL", quantity=10, price=100, amount=-1000),
            _t(type="Sell", symbol="AAPL", quantity=10, price=120, amount=1200),
        ]
        m = compute_metrics(txns)
        assert m.net_realized_pnl == 200.0
        stat = next(s for s in m.symbol_stats if s.symbol == "AAPL")
        assert stat.realized_pnl == 200.0
        assert stat.net_shares == 0.0
        assert m.winning_symbols == 1
        assert m.losing_symbols == 0

    def test_fifo_orders_lots_oldest_first(self):
        # Buy 10@10, buy 10@20, sell 15@30.
        # FIFO cost: 10*10 + 5*20 = 200; proceeds 15*30 = 450 -> +250.
        txns = [
            _t(type="Buy", symbol="X", quantity=10, price=10, amount=-100),
            _t(type="Buy", symbol="X", quantity=10, price=20, amount=-200),
            _t(type="Sell", symbol="X", quantity=15, price=30, amount=450),
        ]
        m = compute_metrics(txns)
        assert m.net_realized_pnl == 250.0
        stat = next(s for s in m.symbol_stats if s.symbol == "X")
        assert stat.net_shares == 5.0  # 20 bought - 15 sold

    def test_partial_lot_then_remaining(self):
        # Buy 10@10, sell 4@15 (+20), sell 6@5 (-30) -> net -10.
        txns = [
            _t(type="Buy", symbol="Y", quantity=10, price=10, amount=-100),
            _t(type="Sell", symbol="Y", quantity=4, price=15, amount=60),
            _t(type="Sell", symbol="Y", quantity=6, price=5, amount=30),
        ]
        m = compute_metrics(txns)
        assert m.net_realized_pnl == -10.0
        assert m.losing_symbols == 1

    def test_sell_with_no_lots_yields_zero_realized(self):
        # Sell with no prior buy: matched == 0, no realized PnL added.
        txns = [_t(type="Sell", symbol="Z", quantity=5, price=10, amount=50)]
        m = compute_metrics(txns)
        assert m.net_realized_pnl == 0.0
        stat = next(s for s in m.symbol_stats if s.symbol == "Z")
        assert stat.realized_pnl == 0.0
        assert stat.proceeds == 50.0

    def test_price_derived_from_amount_when_missing(self):
        # No explicit price: derived as abs(amount)/qty. Buy 10 -> 100/10=10.
        txns = [
            _t(type="Buy", symbol="W", quantity=10, price=0, amount=-100),
            _t(type="Sell", symbol="W", quantity=10, price=0, amount=150),
        ]
        m = compute_metrics(txns)
        assert m.net_realized_pnl == 50.0


class TestOptionsAndLargest:
    def test_option_detected_by_type(self):
        txns = [
            _t(type="Option Buy", symbol="AAPL", quantity=1, price=2, amount=-200),
            _t(type="Buy", symbol="AAPL", quantity=1, price=100, amount=-100),
        ]
        m = compute_metrics(txns)
        assert m.option_trades == 1

    def test_option_detected_by_description(self):
        txns = [_t(type="Other", symbol="AAPL", description="AAPL Call Option", amount=-50)]
        m = compute_metrics(txns)
        assert m.option_trades == 1

    def test_largest_trade_by_abs_amount(self):
        txns = [
            _t(type="Buy", symbol="A", quantity=1, price=100, amount=-100),
            _t(type="Sell", symbol="B", quantity=1, price=5000, amount=5000),
            _t(type="Buy", symbol="C", quantity=1, price=200, amount=-200),
        ]
        m = compute_metrics(txns)
        assert m.largest_trade.symbol == "B"
        assert m.largest_trade.amount == 5000.0


class TestConcentrationAndType:
    def test_top_concentration_pct(self):
        txns = [
            _t(type="Buy", symbol="BIG", quantity=1, price=900, amount=-900),
            _t(type="Buy", symbol="SMALL", quantity=1, price=100, amount=-100),
        ]
        m = compute_metrics(txns)
        assert m.top_concentration.symbol == "BIG"
        assert m.top_concentration.pct_of_invested == 90.0

    def test_type_breakdown_sorted_by_count(self):
        txns = [
            _t(type="Buy", symbol="A", quantity=1, price=1, amount=-1),
            _t(type="Buy", symbol="B", quantity=1, price=1, amount=-1),
            _t(type="Sell", symbol="A", quantity=1, price=2, amount=2),
        ]
        m = compute_metrics(txns)
        assert m.type_breakdown[0].type == "Buy"
        assert m.type_breakdown[0].count == 2


class TestBehavioralFlags:
    def test_overtrading_flag(self):
        # 25 buys within the same month -> >20/month.
        txns = [
            _t(date="2024-01-01", type="Buy", symbol=f"S{i}", quantity=1, price=1, amount=-1)
            for i in range(25)
        ]
        m = compute_metrics(txns)
        assert any("overtrading" in f for f in m.behavioral_flags)

    def test_concentration_flag(self):
        txns = [
            _t(type="Buy", symbol="BIG", quantity=1, price=600, amount=-600),
            _t(type="Buy", symbol="SMALL", quantity=1, price=400, amount=-400),
        ]
        m = compute_metrics(txns)
        assert any("High concentration" in f for f in m.behavioral_flags)

    def test_low_diversification_flag(self):
        txns = [
            _t(type="Buy", symbol="A", quantity=1, price=1, amount=-1),
            _t(type="Buy", symbol="B", quantity=1, price=1, amount=-1),
        ]
        m = compute_metrics(txns)
        assert any("Low diversification" in f for f in m.behavioral_flags)

    def test_options_activity_flag(self):
        txns = [
            _t(type="Option Buy", symbol="A", quantity=1, price=1, amount=-1),
            _t(type="Buy", symbol="B", quantity=1, price=1, amount=-1),
        ]
        m = compute_metrics(txns)
        # 1 option trade, trade_count=1 buy -> 1/1 > 0.25.
        assert any("options activity" in f for f in m.behavioral_flags)

    def test_churning_flag(self):
        txns = []
        for sym in ("A", "B", "C"):
            txns += [
                _t(type="Buy", symbol=sym, quantity=1, price=10, amount=-10),
                _t(type="Buy", symbol=sym, quantity=1, price=10, amount=-10),
                _t(type="Sell", symbol=sym, quantity=1, price=11, amount=11),
                _t(type="Sell", symbol=sym, quantity=1, price=11, amount=11),
            ]
        m = compute_metrics(txns)
        assert any("churning" in f for f in m.behavioral_flags)

    def test_more_losers_flag(self):
        txns = []
        for sym in ("A", "B", "C"):
            txns += [
                _t(type="Buy", symbol=sym, quantity=1, price=100, amount=-100),
                _t(type="Sell", symbol=sym, quantity=1, price=50, amount=50),  # loss
            ]
        m = compute_metrics(txns)
        assert m.losing_symbols == 3
        assert any("losing than winning" in f for f in m.behavioral_flags)

    def test_no_flags_for_clean_portfolio(self):
        # Well-diversified, profitable, low frequency, no options.
        txns = []
        for i, sym in enumerate(("A", "B", "C", "D", "E", "F")):
            txns += [
                _t(date="2024-01-01", type="Buy", symbol=sym, quantity=1, price=100, amount=-100),
                _t(date="2024-06-01", type="Sell", symbol=sym, quantity=1, price=120, amount=120),
            ]
        m = compute_metrics(txns)
        assert m.behavioral_flags == []


class TestDeterminism:
    def test_same_input_same_output(self):
        txns = [
            _t(type="Buy", symbol="A", quantity=10, price=10, amount=-100),
            _t(type="Sell", symbol="A", quantity=5, price=12, amount=60),
        ]
        m1 = compute_metrics(txns)
        m2 = compute_metrics(txns)
        assert m1.model_dump() == m2.model_dump()
