"""Options lab — defined-risk premium-selling backtests for the sideways sleeve.

Companion to ``strategy_lab.py`` (which explicitly deferred derivatives until a
data source existed — that source is now ``dataflows/massive_options.py``).
This module tests the third leg of the regime triad: trend pays in moves,
buy&hold pays in bulls, and *selling volatility pays in chop* — IF the
variance-risk premium survives the tails (2008/2020/2022) where naive vol
sellers blow up. Same design commitments as strategy_lab:

1. **Structure over prediction.** No forecasting. At each monthly expiry we
   sell options at a fixed standard-deviation distance implied by the market's
   own vol quote (VIX), hold to expiry, settle at intrinsic. Strikes scale
   with IV, so position distance self-adjusts in panics.

2. **The enemy is OVERFITTING — and for short vol, HIDDEN TAIL RISK.** Every
   run reports regime slices, worst trade, premium-vs-payout decomposition,
   and realized-vs-implied vol (VRP capture). Hold-to-expiry P&L is computed
   at roll dates; intra-month mark-to-market drawdowns are *worse* — flagged,
   not hidden.

Pricing: Black-Scholes from first principles (``math.erf``; no scipy
dependency). Settlement at expiry is intrinsic value — model-free. The model
only sets *entry premiums*; real-chain entry premiums via the Massive adapter
are the calibration path (see ``tools/run_volsell_test.py``).

GATED RESEARCH — not wired into the product UI, agent toolkits, or
``dataflows/interface.py``. Promotion is gated on the findings memo in
``docs/reviews/`` showing acceptable tail behaviour.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

TRADING_DAYS = 252
PERIODS_PER_YEAR = 12  # monthly expiry cycle


# --------------------------------------------------------------------------- #
# Black-Scholes (no scipy: N(x) via erf)
# --------------------------------------------------------------------------- #

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(
    contract_type: str,
    spot: float,
    strike: float,
    t_years: float,
    sigma: float,
    r: float = 0.0,
    q: float = 0.0,
) -> float:
    """European Black-Scholes price; collapses to intrinsic at expiry."""
    cp = contract_type.strip().upper()[:1]
    if cp not in ("C", "P"):
        raise ValueError(f"contract_type must be C or P, got {contract_type!r}")
    if t_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return max(spot - strike, 0.0) if cp == "C" else max(strike - spot, 0.0)
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t_years) / sq
    d2 = d1 - sq
    if cp == "C":
        return spot * math.exp(-q * t_years) * norm_cdf(d1) - strike * math.exp(-r * t_years) * norm_cdf(d2)
    return strike * math.exp(-r * t_years) * norm_cdf(-d2) - spot * math.exp(-q * t_years) * norm_cdf(-d1)


def implied_vol(
    contract_type: str,
    price: float,
    spot: float,
    strike: float,
    t_years: float,
    r: float = 0.0,
    q: float = 0.0,
) -> Optional[float]:
    """Invert BS by bisection. Returns None when the price is outside
    no-arbitrage bounds (stale/garbage quotes happen in real chains)."""
    if t_years <= 0 or price <= 0:
        return None
    lo, hi = 1e-4, 5.0
    p_lo = bs_price(contract_type, spot, strike, t_years, lo, r, q)
    p_hi = bs_price(contract_type, spot, strike, t_years, hi, r, q)
    if not (p_lo <= price <= p_hi):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(contract_type, spot, strike, t_years, mid, r, q) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Monthly expiry cycle
# --------------------------------------------------------------------------- #

def third_friday(year: int, month: int) -> _dt.date:
    first_friday = 1 + (4 - _dt.date(year, month, 1).weekday()) % 7
    return _dt.date(year, month, first_friday + 14)


def roll_dates(index: pd.DatetimeIndex, start: _dt.date, end: _dt.date) -> List[pd.Timestamp]:
    """Monthly option roll dates: the last trading day on/before each third
    Friday (third Friday itself except holiday Fridays) within [start, end]."""
    out: List[pd.Timestamp] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        tf = pd.Timestamp(third_friday(y, m))
        eligible = index[(index <= tf) & (index >= tf - pd.Timedelta(days=6))]
        if len(eligible) and start <= eligible[-1].date() <= end:
            out.append(eligible[-1])
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


# --------------------------------------------------------------------------- #
# Spec / result
# --------------------------------------------------------------------------- #

@dataclass
class VolSellSpec:
    """A constrained, falsifiable premium-selling structure (cf. StrategySpec).

    structure:
      - ``putwrite``  — short put, fully cash-secured (CBOE PUT-style:
        contracts sized so collateral == equity; max loss = equity only if
        the underlying goes to zero). ``sd=0`` replicates the PUT index.
      - ``strangle``  — short put + short call at ±sd, sized small via
        ``notional_frac`` (NOT defined-risk on the call side; this is the
        naive structure we measure to see the blow-up).
      - ``condor``    — strangle plus long wings at ±wing_sd: genuinely
        defined-risk (max loss capped at wing distance minus credit).
    """
    name: str
    structure: str                 # putwrite | strangle | condor
    sd: float = 1.0                # short-strike distance in entry-IV std devs
    wing_sd: float = 2.0           # condor long-wing distance
    notional_frac: float = 0.25    # strangle/condor: short-leg notional / equity
    cost_frac: float = 0.05        # premium haircut per leg (half-spread + fees)
    div_yield: float = 0.017       # SPY dividend yield (underlying is the price series)

    def to_dict(self) -> Dict:
        return dict(self.__dict__)


@dataclass
class VolSellResult:
    spec: VolSellSpec
    equity: pd.Series              # equity at roll dates, starts at 1.0
    trades: pd.DataFrame           # one row per monthly trade
    max_loss_bound_pct: Optional[float] = None  # condor: structural cap, % equity

    @property
    def n_trades(self) -> int:
        return len(self.trades)


# --------------------------------------------------------------------------- #
# Backtest: sell at each monthly roll, hold to expiry, settle at intrinsic
# --------------------------------------------------------------------------- #

def backtest(
    spec: VolSellSpec,
    close: pd.Series,
    iv: pd.Series,
    rate: Optional[pd.Series] = None,
    start: Optional[_dt.date] = None,
    end: Optional[_dt.date] = None,
) -> VolSellResult:
    """Run the monthly premium-selling cycle.

    Args:
        close: underlying *price* series (unadjusted close — a put seller
            holds cash, not the stock, so dividends are correctly excluded).
        iv: annualized implied vol as a decimal (VIX/100 for SPY), used for
            strike placement and entry premium.
        rate: annualized risk-free rate as a decimal (e.g. ^IRX/100); the
            collateral earns it, as in the CBOE PUT methodology.

    Equity is marked at roll dates only (hold-to-expiry P&L). Intra-month
    drawdowns are strictly worse than reported — see module docstring.
    """
    if spec.structure not in ("putwrite", "strangle", "condor"):
        raise ValueError(f"unknown structure {spec.structure!r}")
    close = close.dropna()
    iv = iv.reindex(close.index).ffill()
    r_ser = rate.reindex(close.index).ffill() if rate is not None else None
    start = start or close.index[0].date()
    end = end or close.index[-1].date()
    rolls = roll_dates(close.index, start, end)

    equity = 1.0
    curve: List[Tuple[pd.Timestamp, float]] = [(rolls[0], equity)] if rolls else []
    trades: List[Dict] = []
    worst_bound = 0.0

    for t0, t1 in zip(rolls, rolls[1:]):
        s0, s1 = float(close[t0]), float(close[t1])
        sigma = float(iv[t0]) if not math.isnan(float(iv[t0])) else None
        r = float(r_ser[t0]) if r_ser is not None and not math.isnan(float(r_ser[t0])) else 0.0
        t_years = (t1 - t0).days / 365.0
        interest = equity * r * t_years
        if not sigma or sigma <= 0 or t_years <= 0:
            equity += interest  # no quote -> sit in bills that month
            curve.append((t1, equity))
            continue

        sq = sigma * math.sqrt(t_years)
        k_put = s0 * math.exp(-spec.sd * sq)
        k_call = s0 * math.exp(spec.sd * sq)
        q = spec.div_yield
        p_put = bs_price("P", s0, k_put, t_years, sigma, r, q)
        p_call = bs_price("C", s0, k_call, t_years, sigma, r, q)

        if spec.structure == "putwrite":
            units = equity / k_put          # fully cash-secured
            credit = p_put * (1 - spec.cost_frac)
            payoff = max(k_put - s1, 0.0)
        else:
            units = spec.notional_frac * equity / s0
            credit = (p_put + p_call) * (1 - spec.cost_frac)
            payoff = max(k_put - s1, 0.0) + max(s1 - k_call, 0.0)
            if spec.structure == "condor":
                k_pw = s0 * math.exp(-spec.wing_sd * sq)
                k_cw = s0 * math.exp(spec.wing_sd * sq)
                credit -= (bs_price("P", s0, k_pw, t_years, sigma, r, q)
                           + bs_price("C", s0, k_cw, t_years, sigma, r, q)) * (1 + spec.cost_frac)
                payoff -= max(k_pw - s1, 0.0) + max(s1 - k_cw, 0.0)
                # structural worst case: spot through a wing
                bound = units * (max(k_put - k_pw, k_cw - k_call) - credit) / equity
                worst_bound = max(worst_bound, bound)

        window = close[(close.index > t0) & (close.index <= t1)]
        rets = np.log(pd.concat([close[[t0]], window])).diff().dropna()
        rv = float(rets.std() * math.sqrt(TRADING_DAYS)) if len(rets) > 2 else float("nan")

        new_equity = equity + interest + units * (credit - payoff)
        trades.append(
            {
                "entry": t0,
                "expiry": t1,
                "spot": s0,
                "iv": sigma,
                "rv": rv,
                "vrp": sigma - rv if not math.isnan(rv) else float("nan"),
                "k_put": k_put,
                "k_call": k_call if spec.structure != "putwrite" else float("nan"),
                "premium_pct": 100.0 * units * credit / equity,
                "payoff_pct": 100.0 * units * payoff / equity,
                "pnl_pct": 100.0 * (new_equity / equity - 1.0),
            }
        )
        equity = new_equity
        curve.append((t1, equity))

    eq = pd.Series(dict(curve)).sort_index() if curve else pd.Series(dtype=float)
    return VolSellResult(
        spec=spec,
        equity=eq,
        trades=pd.DataFrame(trades),
        max_loss_bound_pct=100.0 * worst_bound if spec.structure == "condor" else None,
    )


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def curve_metrics(curve: pd.Series) -> Tuple[float, float, float]:
    """(annualized Sharpe, total return %, max drawdown %) on a roll-date curve."""
    if len(curve) < 3:
        return 0.0, 0.0, 0.0
    rets = curve.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * math.sqrt(PERIODS_PER_YEAR)) if rets.std() > 0 else 0.0
    peak, mdd = -math.inf, 0.0
    for v in curve.values:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
    total = (float(curve.iloc[-1]) / float(curve.iloc[0]) - 1.0) * 100.0
    return sharpe, total, mdd * 100.0


def year_stats(result: VolSellResult, year: int) -> Optional[Dict]:
    """Regime slice: that year's return/maxDD plus the worst single trade."""
    seg = result.equity[result.equity.index.year == year]
    if len(seg) < 3:
        return None
    sharpe, total, mdd = curve_metrics(seg)
    tr = result.trades[result.trades["expiry"].dt.year == year]
    return {
        "year": year,
        "sharpe": sharpe,
        "return_pct": total,
        "maxdd_pct": mdd,
        "worst_trade_pct": float(tr["pnl_pct"].min()) if len(tr) else float("nan"),
        "n_trades": len(tr),
    }


def vrp_summary(result: VolSellResult) -> Dict:
    """Variance-risk-premium capture: implied minus realized vol, and the
    premium-vs-payout decomposition that shows where the P&L actually came from."""
    t = result.trades.dropna(subset=["vrp"])
    if t.empty:
        return {}
    losers = t[t["pnl_pct"] < 0]
    return {
        "mean_vrp_volpts": float(t["vrp"].mean()) * 100.0,
        "pct_months_vrp_positive": 100.0 * float((t["vrp"] > 0).mean()),
        "mean_premium_pct": float(t["premium_pct"].mean()),
        "mean_payoff_pct": float(t["payoff_pct"].mean()),
        "win_rate_pct": 100.0 * float((t["pnl_pct"] > 0).mean()),
        "avg_loss_pct": float(losers["pnl_pct"].mean()) if len(losers) else 0.0,
        "worst_trade_pct": float(t["pnl_pct"].min()),
        "worst_trade_expiry": str(t.loc[t["pnl_pct"].idxmin(), "expiry"].date()),
    }
