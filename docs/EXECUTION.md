# Execution & Backtesting

The agent graph produces a `PortfolioDecision` (5-tier rating + executive
summary). The **execution layer** translates that into share-level orders
against a pluggable brokerage backend; the **backtest harness** replays the
same Executor against historical bars so the strategy can be evaluated
without spending real money first.

The same code runs in three modes — only the broker adapter swaps:

| Mode       | Broker                  | Creds needed                                |
|------------|-------------------------|---------------------------------------------|
| `backtest` | `SimulatedBroker`       | none                                         |
| `paper`    | `AlpacaBroker` (paper)  | `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`        |
| `live`     | `AlpacaBroker` (live)   | above + `BROKERAGE_ALLOW_LIVE=1`             |

## Layout

```
tradingagents/
  execution/
    schemas.py            # Account, BrokerPosition, Order, ExecutionResult, ...
    broker.py             # BrokerClient Protocol
    sizing.py             # SizingPolicy: rating -> target weight -> share qty
    executor.py           # Executor: PortfolioDecision -> Order against any BrokerClient
    portfolio_mirror.py   # broker positions -> ~/.tradingagents/portfolio.json
    pipeline.py           # LivePipeline: graph -> decision -> executor -> mirror
    translation.py        # final_state markdown -> PortfolioDecision
    factory.py            # build_broker(mode): env-driven adapter selection
    brokers/
      simulated.py        # in-memory ledger for backtest + tests
      alpaca.py           # alpaca-py adapter (paper + live)
  backtest/
    bars.py               # yfinance loader + synthetic helper for tests
    decision_source.py    # FixedRating, Replay, AgentGraph sources
    harness.py            # walk-forward replay against SimulatedBroker
    metrics.py            # CAGR, Sharpe, max drawdown, total return
    runner.py             # CLI: `python -m tradingagents.backtest.runner`
```

## Sizing

The PM's rating doesn't carry a quantity, so the `SizingPolicy` maps it
to a *target weight* (fraction of account equity):

| Rating       | Default weight | When `allow_short=False`         |
|--------------|---------------:|----------------------------------|
| Buy          | +10%           | unchanged                        |
| Overweight   | +5%            | unchanged                        |
| Hold         | (no change)    | unchanged                        |
| Underweight  | −2.5%          | collapses to 0% (exit to flat)   |
| Sell         | −5%            | collapses to 0% (exit to flat)   |

The Executor reads current qty from the **broker** (source of truth),
computes `delta = target_qty − current_qty`, and places a market order
for `abs(delta)`. Orders carry an idempotency key derived from
`(ticker, trade_date, rating)` so re-running the same decision is a no-op.

## Running a backtest

```bash
python -m tradingagents.backtest.runner \
  --ticker AAPL --start 2024-01-01 --end 2024-12-31 \
  --rating Buy --starting-cash 100000 --slippage-bps 5
```

For agent-driven backtests, swap `FixedRatingDecisionSource` for
`AgentGraphDecisionSource(graph)` and crank `--rebalance-every-n-bars` up
(e.g. 5 or 21) — each rebalance is one LLM-driven run.

## Executing a real (paper) trade

```bash
# Paper trade today's decision for AAPL through Alpaca
tradingagents execute --ticker AAPL --mode paper --date 2026-05-12

# Same thing but only print what *would* be ordered:
tradingagents execute --ticker AAPL --mode paper --dry-run
```

Via the web API:

```
POST /api/sessions/{sid}/execute   { "dry_run": false }
GET  /api/broker/account
GET  /api/broker/positions
POST /api/broker/sync              # pull broker truth -> portfolio.json
```

`AGENTICWHALES_RECONCILE_INTERVAL_SECONDS=60` enables a background task
that calls `PortfolioMirror.sync()` automatically.

## Going live

1. Test the full flow in paper mode for at least a week of trading days.
2. Set `BROKERAGE_MODE=live`, `BROKERAGE_ALLOW_LIVE=1`, and rotate to your
   live Alpaca keys. The opt-in env var is a deliberate safety check.
3. Start with a small `target_weights` cap (e.g. `max_position_weight=0.02`
   for 2%) until you trust the system.
