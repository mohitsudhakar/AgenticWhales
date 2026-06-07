"""Cheap edge probe — a look-ahead-proof reduced-debate backtest.

Purpose
-------
The full M1a→M3 path is built to *prove* an edge rigorously. This module has a
humbler, faster job: a tripwire that can **kill the LLM-edge thesis cheaply** if
it's dead, and *justify* the expensive build only if it shows a pulse.

The thesis under test (deliberately reduced):

    Does an LLM debate over *point-in-time technical features* beat a
    turnover-matched random sizer and Classical-alone, net of cost,
    out-of-sample, across more than one market regime?

Why this is trustworthy (the look-ahead story)
----------------------------------------------
The production graph's analysts fetch live web/price data, and the `asof` guard
is **not currently wired into those accessors** — so driving the real graph at a
historical date would leak the future and manufacture a fake positive. This
probe sidesteps that entirely: the decision function is a **pure function of the
as-of-bounded price slice** we hand it. There is no tool access and no web
fetch, so there is no channel for look-ahead. A negative result is therefore
decisive.

Decisiveness via the model's knowledge boundary
-----------------------------------------------
DeepSeek's cutoff sits in 2024–2025, so the 2025 tariff-shock window (W1) is
partly *memorized* (a tailwind that biases toward false positives) while the
2025-08→2026 bull window (W2) is largely *post-cutoff* — the cleaner OOS read.
If the system shows no edge in W2 despite the W1 tailwind, that is an unusually
strong kill signal.

Structure
---------
* `build_features` — pure, look-ahead-free technical context from a price slice.
* `run_debate` — a minimal bull / bear / judge debate (LLM-agnostic; the two
  invoke callables are injected so tests run fully offline).
* `simulate` — a clean monthly-rebalance simulator. Every strategy (LLM /
  random / classical / buy-and-hold) runs on the **same schedule**, so the
  random baseline is turnover- and size-matched by construction.
* baselines, `equity_metrics`, `run_symbol`, `run_panel`, `evaluate_verdict`,
  `write_report`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import random as _random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .agents.schemas import PortfolioDecision, PortfolioRating
from .asof import as_of_date

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Panel + windows + costs (the researched defaults)
# ---------------------------------------------------------------------------

DEFAULT_PANEL: List[str] = [
    "AAPL", "MSFT", "NVDA",   # mega-cap tech / AI
    "JPM", "XOM", "JNJ", "WMT",  # financials, energy, healthcare, staples
    "GLD",                    # gold
    "SPY", "QQQ",             # S&P 500 + Nasdaq-100 indexes
]

# Per-leg transaction cost in basis points, applied identically to every
# strategy and baseline. Gold ETF is a touch tighter than single-name equity.
DEFAULT_COST_BPS = 10.0
COST_BPS_OVERRIDE: Dict[str, float] = {"GLD": 8.0, "SPY": 8.0, "QQQ": 8.0}


@dataclass(frozen=True)
class Window:
    name: str
    start: str   # ISO
    end: str     # ISO
    note: str = ""


# Chosen from regime research (see module docstring).
W1_DRAWDOWN = Window(
    "tariff-shock-drawdown", "2025-02-01", "2025-07-31",
    note="Apr-2025 'Liberation Day' tariff crash + V-recovery; high-vol stress; partly in-sample.",
)
W2_BULL_OOS = Window(
    "bull-grind-oos", "2025-08-01", "2026-05-31",
    note="Steady bull grind into 2026; largely post-cutoff -> cleanest out-of-sample read.",
)
DEFAULT_WINDOWS = [W1_DRAWDOWN, W2_BULL_OOS]

# Rating -> target portfolio weight. Shared by LLM and (magnitude-only) by the
# random sizer, so the random baseline differs from the LLM ONLY in sign.
RATING_WEIGHT: Dict[PortfolioRating, float] = {
    PortfolioRating.BUY: 1.0,
    PortfolioRating.OVERWEIGHT: 0.5,
    PortfolioRating.HOLD: 0.0,
    PortfolioRating.UNDERWEIGHT: -0.5,
    PortfolioRating.SELL: -1.0,
}

PROMPT_VERSION = "probe-v1"
TRADING_DAYS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# Features — pure, look-ahead-free (function of the as-of-bounded slice only)
# ---------------------------------------------------------------------------

def _rsi(close: pd.Series, n: int = 14) -> float:
    delta = close.diff().dropna()
    if len(delta) < n:
        return 50.0
    gain = delta.clip(lower=0).rolling(n).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(n).mean().iloc[-1]
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    rs = gain / loss
    return float(100.0 - 100.0 / (1.0 + rs))


def build_features(history: pd.DataFrame) -> Optional[Dict]:
    """Compute point-in-time technical features from a price slice.

    `history` must already be bounded to ``<= as_of`` (the caller's
    responsibility). This function only ever reads the tail of the series, so
    feeding it additional *future* rows cannot change the features for an
    earlier as-of date — that invariant is asserted in the test suite and is
    what makes the probe look-ahead-free.
    """
    if history is None or len(history) < 60:
        return None
    close = pd.to_numeric(history["Close"], errors="coerce").dropna()
    if len(close) < 60:
        return None
    last = float(close.iloc[-1])

    def _ret(n: int) -> Optional[float]:
        if len(close) <= n:
            return None
        return round((last / float(close.iloc[-n - 1]) - 1.0) * 100.0, 2)

    def _sma(n: int) -> Optional[float]:
        if len(close) < n:
            return None
        return float(close.tail(n).mean())

    daily_ret = close.pct_change().dropna()
    realized_vol = (
        float(daily_ret.tail(20).std() * math.sqrt(TRADING_DAYS_PER_YEAR) * 100)
        if len(daily_ret) > 2 else None
    )
    win = close.tail(TRADING_DAYS_PER_YEAR)
    hi, lo = float(win.max()), float(win.min())
    dd_from_high = round((last / hi - 1.0) * 100.0, 2) if hi > 0 else None
    range_pos = round((last - lo) / (hi - lo) * 100.0, 1) if hi > lo else None

    sma20, sma50, sma200 = _sma(20), _sma(50), _sma(200)

    def _vs(sma: Optional[float]) -> Optional[float]:
        return round((last / sma - 1.0) * 100.0, 2) if sma else None

    return {
        "last_close": round(last, 4),
        "ret_5d_pct": _ret(5),
        "ret_20d_pct": _ret(20),
        "ret_60d_pct": _ret(60),
        "ret_120d_pct": _ret(120),
        "price_vs_sma20_pct": _vs(sma20),
        "price_vs_sma50_pct": _vs(sma50),
        "price_vs_sma200_pct": _vs(sma200),
        "rsi_14": round(_rsi(close), 1),
        "realized_vol_ann_pct": round(realized_vol, 1) if realized_vol else None,
        "drawdown_from_252d_high_pct": dd_from_high,
        "range_pos_52w_pct": range_pos,
    }


def _features_block(symbol: str, as_of: _dt.date, f: Dict) -> str:
    lines = "\n".join(f"  {k}: {v}" for k, v in f.items())
    return (
        f"Instrument: {symbol}\n"
        f"As-of date: {as_of.isoformat()} (you have NO information after this date)\n"
        f"Point-in-time technical features:\n{lines}\n"
    )


# ---------------------------------------------------------------------------
# The reduced debate (LLM-agnostic; invokers injected for offline testing)
# ---------------------------------------------------------------------------

InvokeText = Callable[[List[Tuple[str, str]]], str]
InvokeStructured = Callable[[List[Tuple[str, str]]], PortfolioDecision]


def run_debate(
    symbol: str,
    as_of: _dt.date,
    features: Dict,
    *,
    invoke_text: InvokeText,
    invoke_structured: InvokeStructured,
) -> PortfolioDecision:
    """Bull (blind) + Bear (blind) + Judge -> structured PortfolioDecision.

    Blind first round: bull and bear do not see each other, preserving
    independence (mirrors the production graph's `blind_first_round`).
    """
    ctx = _features_block(symbol, as_of, features)
    bull = invoke_text([
        ("system",
         "You are a rigorous BULLISH equity analyst. Using ONLY the point-in-time "
         "technical features provided, make the strongest evidence-based case to be "
         "long. Cite specific numbers. Be honest about weak signals. <=120 words."),
        ("human", ctx),
    ])
    bear = invoke_text([
        ("system",
         "You are a rigorous BEARISH equity analyst. Using ONLY the point-in-time "
         "technical features provided, make the strongest evidence-based case to be "
         "short or flat. Cite specific numbers. Be honest about weak signals. <=120 words."),
        ("human", ctx),
    ])
    decision = invoke_structured([
        ("system",
         "You are a portfolio manager. Weigh the bull and bear cases against the "
         "point-in-time features and decide a position. Base your rating only on the "
         "evidence; a Hold is appropriate when signals conflict. Provide the structured "
         "decision fields, including expected_return_pct, expected_volatility_pct, "
         "prob_of_profit (0-1), and expected_hold_days (~21 for a monthly horizon)."),
        ("human", f"{ctx}\n\nBULL CASE:\n{bull}\n\nBEAR CASE:\n{bear}\n"),
    ])
    return decision


def default_invokers(
    provider: str = "deepseek",
    model: str = "deepseek-v4-pro",
    *,
    base_url: Optional[str] = None,
    temperature: float = 0.0,
) -> Tuple[InvokeText, InvokeStructured]:
    """Build (invoke_text, invoke_structured) backed by a real LLM client.

    Imported lazily so the module can be imported (and unit-tested) without any
    LLM SDK or API key present.
    """
    from .llm_clients import create_llm_client
    from .llm_clients.base_client import normalize_content

    llm = create_llm_client(
        provider=provider, model=model, base_url=base_url, temperature=temperature,
    ).get_llm()
    structured = llm.with_structured_output(PortfolioDecision)

    def invoke_text(msgs: List[Tuple[str, str]]) -> str:
        return normalize_content(llm.invoke(msgs))

    def invoke_structured(msgs: List[Tuple[str, str]]) -> PortfolioDecision:
        return structured.invoke(msgs)

    return invoke_text, invoke_structured


# ---------------------------------------------------------------------------
# Decision cache (skip paid calls on re-run; deterministic with temp=0)
# ---------------------------------------------------------------------------

class DecisionCache:
    """JSONL-backed cache of final decisions keyed by (symbol, date, features, model)."""

    def __init__(self, path: Optional[Path], model: str):
        self.path = Path(path) if path else None
        self.model = model
        self._mem: Dict[str, dict] = {}
        if self.path and self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._mem[rec["key"]] = rec["decision"]

    def _key(self, symbol: str, as_of: _dt.date, features: Dict) -> str:
        blob = json.dumps(
            {"s": symbol, "d": as_of.isoformat(), "f": features,
             "m": self.model, "v": PROMPT_VERSION},
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, symbol, as_of, features) -> Optional[PortfolioDecision]:
        rec = self._mem.get(self._key(symbol, as_of, features))
        return PortfolioDecision(**rec) if rec else None

    def put(self, symbol, as_of, features, decision: PortfolioDecision) -> None:
        key = self._key(symbol, as_of, features)
        payload = decision.model_dump(mode="json")
        self._mem[key] = payload
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(json.dumps({"key": key, "decision": payload}) + "\n")


# ---------------------------------------------------------------------------
# Schedule + decision generation (per symbol/window)
# ---------------------------------------------------------------------------

def monthly_schedule(history: pd.DataFrame, window: Window) -> List[_dt.date]:
    """First available trading day of each month within [start, end]."""
    start = _dt.date.fromisoformat(window.start)
    end = _dt.date.fromisoformat(window.end)
    seen: set = set()
    out: List[_dt.date] = []
    for ts in history.index:
        d = ts.date() if hasattr(ts, "date") else ts
        if d < start or d > end:
            continue
        ym = (d.year, d.month)
        if ym not in seen:
            seen.add(ym)
            out.append(d)
    return out


def generate_llm_decisions(
    symbol: str,
    history: pd.DataFrame,
    schedule: Sequence[_dt.date],
    *,
    invoke_text: InvokeText,
    invoke_structured: InvokeStructured,
    cache: Optional[DecisionCache] = None,
    strict: bool = True,
) -> Dict[_dt.date, PortfolioDecision]:
    """Run the debate on each schedule date using only data <= that date."""
    decisions: Dict[_dt.date, PortfolioDecision] = {}
    for d in schedule:
        sl = history.loc[history.index.date <= d]
        feats = build_features(sl)
        if feats is None:
            continue
        if cache:
            cached = cache.get(symbol, d, feats)
            if cached is not None:
                decisions[d] = cached
                continue
        # Defense-in-depth: strict as-of binding even though `feats` is already
        # a pure function of the bounded slice (no accessor is called here).
        with as_of_date(d, strict=strict):
            decision = run_debate(
                symbol, d, feats,
                invoke_text=invoke_text, invoke_structured=invoke_structured,
            )
        decisions[d] = decision
        if cache:
            cache.put(symbol, d, feats, decision)
    return decisions


# ---------------------------------------------------------------------------
# Baseline weight maps (all share the LLM's exact schedule -> matched turnover)
# ---------------------------------------------------------------------------

def weights_from_decisions(decisions: Dict[_dt.date, PortfolioDecision]) -> Dict[_dt.date, float]:
    return {d: RATING_WEIGHT.get(dec.rating, 0.0) for d, dec in decisions.items()}


def random_sign_weights(
    llm_weights: Dict[_dt.date, float], seed: int,
) -> Dict[_dt.date, float]:
    """Same magnitudes and same active dates as the LLM, sign chosen by a coin.

    Turnover- and size-matched by construction; only the *direction* is random.
    """
    rng = _random.Random(seed)
    out: Dict[_dt.date, float] = {}
    for d, w in llm_weights.items():
        if w == 0.0:
            out[d] = 0.0  # keep flat dates flat -> identical active-position count
        else:
            out[d] = abs(w) * (1 if rng.random() < 0.5 else -1)
    return out


def classical_weights(
    symbol: str, schedule: Sequence[_dt.date],
) -> Dict[_dt.date, float]:
    """Classical-alone baseline via the deterministic signal stack (date-filtered)."""
    from .classical import analyze_classical

    out: Dict[_dt.date, float] = {}
    for d in schedule:
        try:
            res = analyze_classical(symbol, d.isoformat())
        except Exception:
            res = None
        out[d] = RATING_WEIGHT.get(res.decision.rating, 0.0) if res else 0.0
    return out


# ---------------------------------------------------------------------------
# The monthly-rebalance simulator (shared by every strategy)
# ---------------------------------------------------------------------------

@dataclass
class SimResult:
    symbol: str
    window: str
    strategy: str
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)
    n_rebalances: int = 0
    n_active: int = 0  # rebalances with a non-zero target


def simulate(
    symbol: str,
    window: Window,
    history: pd.DataFrame,
    weights: Dict[_dt.date, float],
    *,
    strategy: str,
    starting_cash: float = 100_000.0,
    cost_bps: float = DEFAULT_COST_BPS,
    allow_short: bool = True,
) -> SimResult:
    """Walk the window daily; rebalance to target weight at the *next* open.

    Strict-causal: a target set on schedule date ``d`` (from data <= d) executes
    at the open of ``d+1``. Holdings are constant between rebalances. Cost is
    charged on the traded notional of each rebalance leg.
    """
    start = _dt.date.fromisoformat(window.start)
    end = _dt.date.fromisoformat(window.end)
    days = [ts for ts in history.index if start <= ts.date() <= end]

    cash = float(starting_cash)
    shares = 0.0
    pending_w: Optional[float] = None
    res = SimResult(symbol=symbol, window=window.name, strategy=strategy)

    for ts in days:
        bar = history.loc[ts]
        op = float(bar["Open"])
        cl = float(bar["Close"])

        # Execute a rebalance queued on the previous schedule date, at this open.
        if pending_w is not None and op > 0:
            nav_open = cash + shares * op
            target_shares = (pending_w * nav_open) / op
            delta = target_shares - shares
            cash -= delta * op + abs(delta) * op * cost_bps / 1e4
            shares = target_shares
            res.n_rebalances += 1
            if pending_w != 0.0:
                res.n_active += 1
            pending_w = None

        # Queue a new target if today is a schedule date with a decision.
        d = ts.date()
        if d in weights:
            w = weights[d]
            if not allow_short and w < 0:
                w = 0.0
            pending_w = w

        nav = cash + shares * cl
        res.equity_curve.append((d.isoformat(), round(nav, 2)))

    return res


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def equity_metrics(res: SimResult) -> Dict:
    navs = [nav for _, nav in res.equity_curve]
    if len(navs) < 2:
        return {"sharpe": 0.0, "total_return_pct": 0.0, "max_drawdown_pct": 0.0,
                "n_rebalances": res.n_rebalances, "n_active": res.n_active}
    rets = [navs[i] / navs[i - 1] - 1.0 for i in range(1, len(navs)) if navs[i - 1] > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    std = math.sqrt(var)
    sharpe = (mean / std * math.sqrt(TRADING_DAYS_PER_YEAR)) if std > 0 else 0.0
    peak = navs[0]
    max_dd = 0.0
    for nav in navs:
        peak = max(peak, nav)
        if peak > 0:
            max_dd = max(max_dd, (peak - nav) / peak)
    return {
        "sharpe": round(sharpe, 3),
        "total_return_pct": round((navs[-1] / navs[0] - 1.0) * 100.0, 2),
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "n_rebalances": res.n_rebalances,
        "n_active": res.n_active,
    }


def buy_and_hold_weights(schedule: Sequence[_dt.date]) -> Dict[_dt.date, float]:
    """Long 100% from the first schedule date onward (single rebalance)."""
    return {schedule[0]: 1.0} if schedule else {}


# ---------------------------------------------------------------------------
# Per-symbol + panel orchestration
# ---------------------------------------------------------------------------

def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def run_symbol(
    symbol: str,
    window: Window,
    history: pd.DataFrame,
    *,
    invoke_text: InvokeText,
    invoke_structured: InvokeStructured,
    cache: Optional[DecisionCache] = None,
    cost_bps: Optional[float] = None,
    random_seeds: Sequence[int] = tuple(range(20)),
) -> Dict:
    """Run LLM + all baselines for one (symbol, window). Returns per-strategy metrics."""
    cost = cost_bps if cost_bps is not None else COST_BPS_OVERRIDE.get(symbol, DEFAULT_COST_BPS)
    schedule = monthly_schedule(history, window)
    if not schedule:
        return {"symbol": symbol, "window": window.name, "skipped": "no schedule"}

    llm_decisions = generate_llm_decisions(
        symbol, history, schedule,
        invoke_text=invoke_text, invoke_structured=invoke_structured, cache=cache,
    )
    llm_w = weights_from_decisions(llm_decisions)

    def _m(weights, strat):
        return equity_metrics(simulate(symbol, window, history, weights,
                                       strategy=strat, cost_bps=cost))

    llm_m = _m(llm_w, "llm")
    classical_m = _m(classical_weights(symbol, schedule), "classical")
    bh_m = _m(buy_and_hold_weights(schedule), "buy_hold")

    # Random baseline: distribution over seeds -> report the median Sharpe.
    rand_sharpes = []
    rand_rets = []
    for seed in random_seeds:
        rm = _m(random_sign_weights(llm_w, seed), "random")
        rand_sharpes.append(rm["sharpe"])
        rand_rets.append(rm["total_return_pct"])
    random_m = {
        "sharpe": round(_median(rand_sharpes), 3),
        "total_return_pct": round(_median(rand_rets), 2),
        "sharpe_p90": round(sorted(rand_sharpes)[int(0.9 * (len(rand_sharpes) - 1))], 3),
    }

    return {
        "symbol": symbol,
        "window": window.name,
        "llm": llm_m,
        "random": random_m,
        "classical": classical_m,
        "buy_hold": bh_m,
        "n_decisions": len(llm_decisions),
    }


def run_panel(
    panel: Sequence[str],
    window: Window,
    load_history: Callable[[str, Window], pd.DataFrame],
    *,
    invoke_text: InvokeText,
    invoke_structured: InvokeStructured,
    cache: Optional[DecisionCache] = None,
) -> Dict:
    """Run every symbol for one window; aggregate the per-name Sharpe distribution."""
    per_symbol = []
    for symbol in panel:
        try:
            history = load_history(symbol, window)
        except Exception as exc:  # noqa: BLE001
            log.warning("edge_probe: skipping %s (%s): %s", symbol, window.name, exc)
            continue
        per_symbol.append(run_symbol(
            symbol, window, history,
            invoke_text=invoke_text, invoke_structured=invoke_structured, cache=cache,
        ))

    def med(strat: str, metric: str = "sharpe") -> float:
        return round(_median([r[strat][metric] for r in per_symbol if strat in r]), 3)

    return {
        "window": window.name,
        "window_note": window.note,
        "n_symbols": len(per_symbol),
        "median_sharpe": {
            "llm": med("llm"), "random": med("random"),
            "classical": med("classical"), "buy_hold": med("buy_hold"),
        },
        "median_total_return_pct": {
            "llm": med("llm", "total_return_pct"),
            "random": med("random", "total_return_pct"),
            "classical": med("classical", "total_return_pct"),
            "buy_hold": med("buy_hold", "total_return_pct"),
        },
        "per_symbol": per_symbol,
    }


# ---------------------------------------------------------------------------
# Pre-registered verdict (write this BEFORE running — it is the whole point)
# ---------------------------------------------------------------------------

def evaluate_verdict(window_results: Sequence[Dict]) -> Dict:
    """Apply the pre-registered decision rule across windows.

    - LLM loses to the turnover-matched random sizer in ANY window -> KILL.
    - LLM ~= Classical (no edge over the free baseline)             -> AMBER (rethink).
    - LLM beats BOTH random and Classical in BOTH windows           -> GREEN (build the rigorous substrate).
    """
    beats_random_all = True
    beats_classical_all = True
    loses_to_random_any = False
    details = []
    for wr in window_results:
        ms = wr["median_sharpe"]
        br = ms["llm"] > ms["random"]
        bc = ms["llm"] > ms["classical"]
        beats_random_all &= br
        beats_classical_all &= bc
        loses_to_random_any |= (ms["llm"] < ms["random"])
        details.append({
            "window": wr["window"], "llm": ms["llm"], "random": ms["random"],
            "classical": ms["classical"], "beats_random": br, "beats_classical": bc,
        })

    if loses_to_random_any:
        verdict = "KILL"
        reason = ("LLM debate failed to beat a turnover-matched random sizer "
                  "(same names, same trade count, same cost) in at least one regime "
                  "— no evidence of skill over luck.")
    elif beats_random_all and beats_classical_all:
        verdict = "GREEN"
        reason = ("LLM debate beat both the random sizer and Classical-alone, net of "
                  "cost, in every window. Justifies building the rigorous guarded "
                  "M1a/M1b/M2/M3 substrate to confirm with the full panel.")
    else:
        verdict = "AMBER"
        reason = ("LLM debate beat random but did not clearly beat Classical-alone in "
                  "every window — the LLM cost may not be justified. Rethink before "
                  "investing in the full substrate.")
    return {"verdict": verdict, "reason": reason, "by_window": details}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_report(window_results: Sequence[Dict], verdict: Dict, out_path: Path,
                 *, model: str, generated: str) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    lines.append("# Cheap edge probe — results\n")
    lines.append(f"> Generated: {generated} · model: `{model}` · prompt: `{PROMPT_VERSION}`\n")
    lines.append("Reduced thesis tested: *does an LLM debate over point-in-time "
                 "technical features beat a turnover-matched random sizer and "
                 "Classical-alone, net of cost, across regimes?* Look-ahead-proof by "
                 "construction (decisions are a pure function of the bounded price slice).\n")

    lines.append(f"\n## Verdict: **{verdict['verdict']}**\n\n{verdict['reason']}\n")
    lines.append("\n| window | LLM Sharpe | random | classical | beats random? | beats classical? |")
    lines.append("|---|---|---|---|---|---|")
    for d in verdict["by_window"]:
        lines.append(f"| {d['window']} | {d['llm']} | {d['random']} | {d['classical']} | "
                     f"{'✅' if d['beats_random'] else '❌'} | "
                     f"{'✅' if d['beats_classical'] else '❌'} |")

    for wr in window_results:
        lines.append(f"\n## Window: {wr['window']}\n\n_{wr['window_note']}_\n")
        lines.append(f"Symbols: {wr['n_symbols']}\n")
        ms, mr = wr["median_sharpe"], wr["median_total_return_pct"]
        lines.append("\n| strategy | median Sharpe | median total return % |")
        lines.append("|---|---|---|")
        for strat in ("llm", "random", "classical", "buy_hold"):
            lines.append(f"| {strat} | {ms[strat]} | {mr[strat]} |")
        lines.append("\n<details><summary>Per-symbol Sharpe</summary>\n")
        lines.append("\n| symbol | LLM | random | classical | buy&hold | #decisions |")
        lines.append("|---|---|---|---|---|---|")
        for r in wr["per_symbol"]:
            lines.append(f"| {r['symbol']} | {r['llm']['sharpe']} | {r['random']['sharpe']} | "
                         f"{r['classical']['sharpe']} | {r['buy_hold']['sharpe']} | {r['n_decisions']} |")
        lines.append("\n</details>\n")

    out_path.write_text("\n".join(lines) + "\n")
    return out_path
