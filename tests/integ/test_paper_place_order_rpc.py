"""Integration test for the Phase 1.5 `paper_place_order` Postgres RPC.

These tests run against a real Postgres container (testcontainers). The
unit-test suite (`pytest`) skips them automatically when integration deps
or Docker aren't available. To run explicitly:

    pip install -e ".[integration]"
    pytest -m integration tests/integ/

Covers:
  - RPC commits insert + position + cash + realized PnL atomically.
  - Idempotency: same (user_id, fire_id, ticker, side) returns the prior
    order without double-writing.
  - Crash-safety equivalent: a synthetic error mid-RPC rolls everything
    back (verified by checking the books haven't desynced).
  - RLS shape: the policies compile against vanilla Postgres + the
    `service_role` and `authenticated` role shells we stub.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(pg_url):
    import psycopg
    with psycopg.connect(pg_url, autocommit=True) as c:
        yield c


def _call_rpc(conn, *, user_id, fire_id, ticker="AAPL", side="buy",
              qty=10.0, fill_price=100.0, recipe_id=None, session_id=None,
              pm_rating="Buy", conviction=7,
              expected_return_pct=10.0, expected_volatility_pct=20.0,
              prob_of_profit=0.6, expected_hold_days=30,
              kelly_fraction=0.05, status="filled"):
    """Thin wrapper around `paper_place_order(...)` matching the wrapper in
    `web/auth.py::call_paper_place_order_rpc`. Returns the JSONB result."""
    with conn.cursor() as cur:
        cur.execute("""
            select paper_place_order(
                %s, %s, %s, %s, %s, %s,
                %s::numeric, %s::numeric, %s::int, %s, %s::int,
                %s::numeric, %s::numeric, %s::numeric, %s::int, %s::numeric, %s
            )
        """, (
            user_id, fire_id, recipe_id, session_id, ticker, side,
            qty, fill_price, 0, pm_rating, conviction,
            expected_return_pct, expected_volatility_pct, prob_of_profit,
            expected_hold_days, kelly_fraction, status,
        ))
        return cur.fetchone()[0]


def _read_position(conn, user_id, ticker):
    with conn.cursor() as cur:
        cur.execute(
            "select qty, avg_cost from paper_positions where user_id=%s and ticker=%s",
            (user_id, ticker.upper()),
        )
        return cur.fetchone()


def _read_account(conn, user_id):
    with conn.cursor() as cur:
        cur.execute(
            "select cash, realized_pnl, short_collateral_reserved "
            "from paper_accounts where user_id=%s",
            (user_id,),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_buy_creates_position_debits_cash_atomically(self, conn, test_user_id):
        fire_id = uuid.uuid4().hex
        result = _call_rpc(conn, user_id=test_user_id, fire_id=fire_id,
                           qty=10, fill_price=100)
        assert result["idempotent"] is False
        assert result["status"] == "filled"
        qty, avg = _read_position(conn, test_user_id, "AAPL")
        assert float(qty) == 10.0
        assert float(avg) == 100.0
        cash, realized, short_coll = _read_account(conn, test_user_id)
        # Starting cash 100k − 10 × 100 = 99,000.
        assert float(cash) == pytest.approx(99_000.0)
        assert float(realized) == pytest.approx(0.0)

    def test_sell_realizes_pnl(self, conn, test_user_id):
        _call_rpc(conn, user_id=test_user_id, fire_id="open",
                  qty=10, fill_price=100, side="buy")
        _call_rpc(conn, user_id=test_user_id, fire_id="close",
                  qty=10, fill_price=120, side="sell")
        # All sold → position row deleted.
        assert _read_position(conn, test_user_id, "AAPL") is None
        cash, realized, _ = _read_account(conn, test_user_id)
        # PnL = (120-100) * 10 = +200. Cash: 100k - 1000 + 1200 = 100,200.
        assert float(realized) == pytest.approx(200.0)
        assert float(cash) == pytest.approx(100_200.0)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_same_fire_returns_existing_order(self, conn, test_user_id):
        fid = "fire-idem"
        first = _call_rpc(conn, user_id=test_user_id, fire_id=fid, qty=5)
        second = _call_rpc(conn, user_id=test_user_id, fire_id=fid, qty=5)
        assert second["idempotent"] is True
        assert second["order_id"] == first["order_id"]
        # Position is the first call's, not doubled.
        qty, _ = _read_position(conn, test_user_id, "AAPL")
        assert float(qty) == 5.0


# ---------------------------------------------------------------------------
# Atomicity — synthetic mid-RPC failure rolls back
# ---------------------------------------------------------------------------

class TestAtomicity:
    def test_invalid_side_raises_and_rolls_back(self, conn, test_user_id):
        """Pass an unknown `side` value. The function should `raise
        exception` which Postgres rolls back inside the transaction — so
        no paper_orders row and no account mutation should remain."""
        import psycopg
        with pytest.raises(psycopg.errors.RaiseException):
            _call_rpc(conn, user_id=test_user_id, fire_id="fail-1",
                      side="invalid_side", qty=10, fill_price=100)
        # No order, no position, account untouched.
        with conn.cursor() as cur:
            cur.execute("select count(*) from paper_orders where user_id=%s",
                        (test_user_id,))
            assert cur.fetchone()[0] == 0
        assert _read_position(conn, test_user_id, "AAPL") is None
        assert _read_account(conn, test_user_id) is None


# ---------------------------------------------------------------------------
# RLS shape — policies compile against vanilla Postgres + role shells
# ---------------------------------------------------------------------------

class TestRLSShape:
    def test_rls_policies_present_on_user_tables(self, conn):
        """Smoke check that every RLS policy named in the schema actually
        landed. If a `create policy` clause silently failed (e.g. role
        doesn't exist) the policy count would drop."""
        with conn.cursor() as cur:
            cur.execute("""
                select tablename, count(*) as n
                from pg_policies
                where schemaname='public'
                group by tablename
            """)
            counts = dict(cur.fetchall())
        # Spot-check the tables we know enforce ownership.
        for table in ("sessions", "paper_orders", "paper_positions",
                      "risk_limits", "recipes", "journal_entries"):
            assert table in counts, f"missing RLS policies on {table}"
            assert counts[table] >= 1, f"{table} has no RLS policies"

    def test_rls_table_rls_enabled(self, conn):
        """Every user-scoped table must have RLS enabled — `enable row
        level security` is what makes the policies actually filter."""
        with conn.cursor() as cur:
            cur.execute("""
                select tablename
                from pg_tables
                where schemaname='public' and rowsecurity=true
            """)
            rls_on = {row[0] for row in cur.fetchall()}
        for table in ("sessions", "paper_orders", "paper_positions",
                      "journal_entries"):
            assert table in rls_on, f"{table} doesn't have RLS enabled"
