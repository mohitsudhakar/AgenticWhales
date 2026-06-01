"""Unit tests for the CSV parser and the LLM extraction/analysis paths.

The LLM paths use an injectable fake chat model (a ``FakeLLM`` returning a
canned ``.content``), so no network or API key is touched — mirroring the
project's existing monkeypatch/MagicMock test style.
"""

from __future__ import annotations

import pytest

from agenticwhales.transactions import (
    Transaction,
    analyze_transactions,
    compute_metrics,
    generate_analysis,
    parse_transactions_csv,
)
from agenticwhales.transactions.extract import extract_transactions, parse_json_loose
from agenticwhales.transactions.parser import _num, dedupe

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake LLM — duck-types a LangChain chat model: .invoke(messages) -> obj.content
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self, content):
        self._content = content
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return _Resp(self._content)


class FlakyLLM:
    """Fails ``fail_times`` then returns content — exercises retry logic."""

    def __init__(self, content, fail_times):
        self._content = content
        self._fail = fail_times
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls <= self._fail:
            raise RuntimeError("transient")
        return _Resp(self._content)


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------
class TestCsvParser:
    def test_basic_robinhood_style(self):
        csv = (
            "Activity Date,Trans Code,Instrument,Description,Quantity,Price,Amount\n"
            "2024-01-15,Buy,AAPL,Apple Inc,10,150.00,($1500.00)\n"
            "2024-02-20,Sell,AAPL,Apple Inc,5,170.00,$850.00\n"
            "2024-03-01,CDIV,AAPL,Dividend,0,0,$12.50\n"
        )
        txns = parse_transactions_csv(csv)
        assert len(txns) == 3
        buy, sell, div = txns
        assert buy.type == "Buy" and buy.symbol == "AAPL"
        assert buy.amount == -1500.0  # parenthesized -> negative
        assert sell.type == "Sell" and sell.amount == 850.0
        assert div.type == "Dividend" and div.amount == 12.5

    def test_date_normalization_us_format(self):
        csv = "Date,Type,Symbol,Amount\n01/15/2024,Buy,TSLA,-100\n"
        txns = parse_transactions_csv(csv)
        assert txns[0].date == "2024-01-15"

    def test_unparseable_date_kept_raw(self):
        csv = "Date,Type,Symbol,Amount\nQ1-2024,Buy,TSLA,-100\n"
        txns = parse_transactions_csv(csv)
        assert txns[0].date == "Q1-2024"

    def test_sign_normalization_buy_negative_sell_positive(self):
        # Source uses unsigned amounts; parser derives the sign from activity.
        csv = "Date,Type,Symbol,Amount\n2024-01-01,Buy,A,100\n2024-01-02,Sell,A,120\n"
        txns = parse_transactions_csv(csv)
        assert txns[0].amount == -100.0
        assert txns[1].amount == 120.0

    def test_option_classification(self):
        csv = (
            "Date,Type,Symbol,Description,Amount\n"
            "2024-01-01,Buy,AAPL,AAPL 1/19 Call Option,-200\n"
        )
        txns = parse_transactions_csv(csv)
        assert txns[0].type == "Option Buy"

    def test_symbol_uppercased(self):
        csv = "Date,Type,Symbol,Amount\n2024-01-01,Buy,aapl,-100\n"
        txns = parse_transactions_csv(csv)
        assert txns[0].symbol == "AAPL"

    def test_empty_rows_dropped(self):
        csv = "Date,Type,Symbol,Amount\n2024-01-01,,,0\n2024-01-02,Buy,A,-100\n"
        txns = parse_transactions_csv(csv)
        assert len(txns) == 1

    def test_parser_feeds_metrics(self):
        csv = (
            "Date,Type,Symbol,Quantity,Price,Amount\n"
            "2024-01-01,Buy,A,10,10,-100\n"
            "2024-02-01,Sell,A,10,12,120\n"
        )
        m = compute_metrics(parse_transactions_csv(csv))
        assert m.net_realized_pnl == 20.0


class TestNumHelper:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("$1,234.56", 1234.56),
            ("(500.00)", -500.0),
            ("", 0.0),
            ("garbage", 0.0),
            ("42", 42.0),
            (3.14, 3.14),
        ],
    )
    def test_num(self, raw, expected):
        assert _num(raw) == expected


class TestDedupe:
    def test_drops_exact_dupes(self):
        t = Transaction(date="2024-01-01", type="Buy", symbol="A", quantity=1, price=1, amount=-1)
        out = dedupe([t, t, t.model_copy()])
        assert len(out) == 1


# ---------------------------------------------------------------------------
# parse_json_loose
# ---------------------------------------------------------------------------
class TestParseJsonLoose:
    def test_plain_json(self):
        assert parse_json_loose('{"a": 1}') == {"a": 1}

    def test_code_fence(self):
        assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}

    def test_prose_wrapped(self):
        assert parse_json_loose('Here you go: {"a": 1} thanks') == {"a": 1}

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            parse_json_loose("no json here")


# ---------------------------------------------------------------------------
# LLM extraction (fake LLM)
# ---------------------------------------------------------------------------
class TestExtraction:
    def test_extracts_and_normalizes(self):
        content = (
            '{"transactions": [{"date": "2024-01-01", "type": "buy", '
            '"symbol": "aapl", "quantity": "10", "price": "150", "amount": "-1500"}]}'
        )
        llm = FakeLLM(content)
        txns = extract_transactions("some raw text", llm=llm)
        assert len(txns) == 1
        assert txns[0].symbol == "AAPL"  # uppercased
        assert txns[0].quantity == 10.0  # string coerced
        assert txns[0].amount == -1500.0

    def test_empty_transactions(self):
        llm = FakeLLM('{"transactions": []}')
        assert extract_transactions("text", llm=llm) == []

    def test_retry_then_success(self):
        content = '{"transactions": [{"type": "Buy", "symbol": "A", "amount": -1}]}'
        llm = FlakyLLM(content, fail_times=2)
        txns = extract_transactions("text", llm=llm)
        assert len(txns) == 1
        assert llm.calls == 3  # 2 failures + 1 success

    def test_failed_chunk_warns_and_skips(self):
        llm = FlakyLLM("ok", fail_times=99)  # always fails (and content invalid anyway)
        warnings = []
        txns = extract_transactions("text", llm=llm, on_warn=warnings.append)
        assert txns == []
        assert any("failed to parse" in w for w in warnings)


# ---------------------------------------------------------------------------
# LLM analysis (fake LLM)
# ---------------------------------------------------------------------------
class TestAnalysis:
    def _good_content(self):
        return (
            '{"headline": "Active trader", "investorArchetype": "Momentum Chaser", '
            '"riskScore": 80, "disciplineScore": 40, "diversificationScore": 30, '
            '"sections": [{"title": "Financial Perspective", "summary": "s", '
            '"points": ["p1", "p2"]}], "suggestions": ["s1"], '
            '"habitsToKeep": ["k1"], "habitsToChange": ["c1"], '
            '"closingReflection": "Keep going."}'
        )

    def test_normalizes_camelcase(self):
        llm = FakeLLM(self._good_content())
        m = compute_metrics([Transaction(type="Buy", symbol="A", quantity=1, price=1, amount=-1)])
        a = generate_analysis(m, [], llm=llm)
        assert a.investor_archetype == "Momentum Chaser"
        assert a.risk_score == 80
        assert a.sections[0].title == "Financial Perspective"
        assert a.suggestions == ["s1"]
        assert a.closing_reflection == "Keep going."

    def test_score_clamped(self):
        content = (
            '{"headline": "h", "riskScore": 200, "disciplineScore": -5, '
            '"diversificationScore": "abc", "sections": []}'
        )
        llm = FakeLLM(content)
        a = generate_analysis(compute_metrics([]), [], llm=llm)
        assert a.risk_score == 100
        assert a.discipline_score == 0
        assert a.diversification_score == 50  # non-numeric -> default

    def test_analyze_transactions_without_llm_skips_analysis(self):
        txns = [Transaction(type="Buy", symbol="A", quantity=1, price=1, amount=-1)]
        result = analyze_transactions(txns)  # run_llm defaults False
        assert result.metrics.total_buys == 1
        assert result.analysis.headline == "Analysis complete."  # default, untouched
        assert result.warnings == []

    def test_analyze_transactions_with_llm(self):
        txns = [Transaction(type="Buy", symbol="A", quantity=1, price=1, amount=-1)]
        result = analyze_transactions(txns, llm=FakeLLM(self._good_content()))
        assert result.analysis.investor_archetype == "Momentum Chaser"

    def test_analyze_transactions_llm_failure_captured_in_warnings(self):
        txns = [Transaction(type="Buy", symbol="A", quantity=1, price=1, amount=-1)]
        result = analyze_transactions(txns, llm=FlakyLLM("bad", fail_times=99))
        assert result.warnings
        assert "LLM analysis failed" in result.warnings[0]
