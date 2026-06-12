"""Behavioral trading coach — the product wedge for the decision-support + coach play.

Why this exists
---------------
The edge probe proved the system has no predictive/selection edge (IC ~= 0,
market-neutral Sharpe ~= 0). So we do **not** sell alpha. We sell *discipline*:
we take a trader's own history and quantify, in dollars, what their behavioral
leaks cost them — and the exact rule that fixes each one. No market-beating
required; the value is making a human less self-destructive, with a number
attached.

Design law (learned from the probe): **the dollar figures are deterministic and
defensible.** Counterfactuals are computed from the trade log (and, optionally,
price data) — never invented. An LLM may *narrate* the findings into coaching
language, but it never produces the numbers.

What it does
------------
1. Reconstruct round-trip trades (FIFO lot matching) from a brokerage history.
2. Detect behavioral leaks every retail trader has — disposition effect,
   overtrading, oversizing/tail risk, revenge trading, cutting winners / letting
   losers run — and **quantify each in dollars** via a concrete counterfactual.
3. (optional) A price-based stop-loss counterfactual: "a 15% stop would have
   saved you $X on your tail losses."
4. (optional) An LLM narrative that turns the deterministic findings into a
   personalized coaching message.
"""

from __future__ import annotations

import datetime as _dt
import statistics
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .transactions.models import Transaction


# --------------------------------------------------------------------------- #
# Round-trip reconstruction (FIFO)
# --------------------------------------------------------------------------- #

@dataclass
class RoundTrip:
    symbol: str
    entry_date: str
    exit_date: str
    qty: float
    entry_px: float
    exit_px: float
    pnl: float
    hold_days: int
    return_pct: float

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


def _d(s: str) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(s[:10])
    except Exception:
        return None


def _is_option(t: Transaction) -> bool:
    s = (t.type + " " + t.description).lower()
    return "option" in s or "call" in s or "put" in s


def reconstruct_round_trips(txns: Sequence[Transaction]) -> List[RoundTrip]:
    """Match buys to sells FIFO, per symbol, into closed long round-trips.

    Long-only for v1 (the retail majority); option rows and short excess are
    skipped. Requires per-row quantity + price; rows missing them are ignored.
    """
    by_symbol: Dict[str, List[Transaction]] = {}
    for t in txns:
        if not t.symbol or _is_option(t) or t.quantity <= 0 or t.price <= 0:
            continue
        if t.type.lower() not in ("buy", "sell"):
            continue
        by_symbol.setdefault(t.symbol.upper(), []).append(t)

    trips: List[RoundTrip] = []
    for symbol, rows in by_symbol.items():
        rows.sort(key=lambda r: r.date)
        lots: List[List[float]] = []  # [qty, price, date_str] open buy lots
        for t in rows:
            if t.type.lower() == "buy":
                lots.append([t.quantity, t.price, t.date])
            else:  # sell -> match FIFO
                qty_to_close = t.quantity
                while qty_to_close > 1e-9 and lots:
                    lot = lots[0]
                    matched = min(qty_to_close, lot[0])
                    ed, xd = _d(lot[2]), _d(t.date)
                    hold = (xd - ed).days if ed and xd else 0
                    pnl = matched * (t.price - lot[1])
                    trips.append(RoundTrip(
                        symbol=symbol, entry_date=lot[2], exit_date=t.date,
                        qty=matched, entry_px=lot[1], exit_px=t.price, pnl=pnl,
                        hold_days=hold,
                        return_pct=(t.price / lot[1] - 1.0) * 100 if lot[1] else 0.0,
                    ))
                    lot[0] -= matched
                    qty_to_close -= matched
                    if lot[0] <= 1e-9:
                        lots.pop(0)
    trips.sort(key=lambda r: r.exit_date)
    return trips


# --------------------------------------------------------------------------- #
# Leak detection — each leak carries a deterministic dollar estimate + a fix
# --------------------------------------------------------------------------- #

@dataclass
class Leak:
    name: str
    severity: str            # low | medium | high
    dollars: float           # estimated $ cost (positive = money lost)
    evidence: str            # the numbers behind it
    fix: str                 # the concrete rule that addresses it


def leak_key(name: str) -> str:
    """Stable identifier for a leak *kind*, independent of the human-facing
    wording. Findings persistence + forward validation key on this, so the
    copy can evolve without orphaning history."""
    import re
    base = name.split("(")[0].strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_")


# The product's central claim is "this bias cost you $X". That claim must stay
# falsifiable: each persisted finding is later re-tested on the trades that
# happened AFTER it was shown to the user — did the flagged behavior persist?
MIN_FORWARD_TRADES = 5


def resolve_finding_forward(finding: Dict, trips: List["RoundTrip"],
                            *, min_forward_trades: int = MIN_FORWARD_TRADES) -> Optional[Dict]:
    """Re-run the leak detectors on the round-trips closed AFTER a finding's
    window, and report whether that leak kind persisted.

    Returns None while there isn't enough forward data to say anything
    (fewer than `min_forward_trades` closed trades after `window_end`).
    Price-path leaks (`no_stop_loss`, `cutting_winners_early`) need price data
    and are not resolved here — they also return None.
    """
    key = finding.get("leak_key") or ""
    if key in ("no_stop_loss", "cutting_winners_early"):
        return None
    window_end = str(finding.get("window_end") or "")[:10]
    fwd = [t for t in trips if t.exit_date[:10] > window_end]
    if len(fwd) < min_forward_trades:
        return None
    match = [l for l in detect_leaks(fwd) if leak_key(l.name) == key]
    return {
        "persisted": bool(match),
        "forward_dollars": round(match[0].dollars, 2) if match else 0.0,
        "n_forward_trades": len(fwd),
    }


def dedupe_transactions(txns: Sequence[Transaction]) -> List[Transaction]:
    """Union helper for continuous history: drop exact-duplicate rows so the same
    trade uploaded in overlapping statements isn't double-counted."""
    seen = set()
    out: List[Transaction] = []
    for t in txns:
        key = (t.date[:10], t.type.lower(), t.symbol.upper(),
               round(float(t.quantity), 4), round(float(t.price), 4))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _period_label(days: int) -> str:
    if days <= 1:
        return "this day"
    if days <= 9:
        return "this week"
    if days <= 45:
        return "this month"
    if days <= 150:
        return "this quarter"
    if days <= 420:
        return "this year"
    return "this period"


def behavioral_insights(trips: List[RoundTrip], leaks: List[Leak]) -> Dict:
    """Period-aware, behaviour-changing cards computed from the round-trips.
    Adapts to whatever was uploaded (a day, a month, a year)."""
    if not trips:
        return {}
    dates = sorted(d for d in (_d(t.exit_date) for t in trips) if d)
    edates = sorted(d for d in (_d(t.entry_date) for t in trips) if d)
    start = (min(edates) if edates else dates[0]).isoformat()
    end = dates[-1].isoformat() if dates else start
    span_days = ((dates[-1] - (edates[0] if edates else dates[0])).days + 1) if dates else 1
    label = _period_label(span_days)

    wins = [t for t in trips if t.is_win]
    losses = [t for t in trips if not t.is_win]
    hw = statistics.mean([t.hold_days for t in wins]) if wins else 0.0
    hl = statistics.mean([t.hold_days for t in losses]) if losses else 0.0
    best = max(trips, key=lambda t: t.pnl)
    worst = min(trips, key=lambda t: t.pnl)
    top = leaks[0] if leaks else None

    if top:
        headline = f"Over {label}, {top.name.split('(')[0].strip().lower()} cost you about ${top.dollars:,.0f}."
    else:
        headline = f"Over {label} your trading was disciplined — keep doing what works."

    return {
        "headline": headline,
        "period": {"start": start, "end": end, "days": span_days, "label": label},
        "n_trades": len(trips),
        "win_rate": round(len(wins) / len(trips), 3) if trips else 0.0,
        "hold_winners_days": round(hw, 1),
        "hold_losers_days": round(hl, 1),
        "hold_ratio": round(hl / hw, 1) if hw > 0 else 0.0,
        "best_trade": {"symbol": best.symbol, "pnl": round(best.pnl, 2),
                       "return_pct": round(best.return_pct, 1)},
        "worst_trade": {"symbol": worst.symbol, "pnl": round(worst.pnl, 2),
                        "return_pct": round(worst.return_pct, 1)},
        "top_fix": ({"name": top.name, "dollars": top.dollars, "fix": top.fix} if top else None),
    }


def open_tail(txns: Sequence[Transaction], trips: List[RoundTrip]) -> Optional[Dict]:
    """Eligible buys dated after the last closed round-trip — positions still
    open. They carry no realized P&L, so monthly_discipline can't bucket them;
    the UI uses this to say why the chart ends where it does instead of
    leaving the user to wonder whether a sync failed."""
    if not trips:
        return None
    last_exit = max((d for d in (_d(t.exit_date) for t in trips) if d), default=None)
    if not last_exit:
        return None
    tail_dates = sorted(
        d for t in txns
        if t.symbol and not _is_option(t) and t.quantity > 0 and t.price > 0
        and t.type.lower() == "buy"
        for d in [_d(t.date)] if d and d > last_exit)
    if not tail_dates:
        return None
    return {"last_close": last_exit.isoformat(),
            "n_open_buys": len(tail_dates),
            "first": tail_dates[0].isoformat(),
            "last": tail_dates[-1].isoformat()}


def monthly_discipline(trips: List[RoundTrip]) -> List[Dict]:
    """Discipline score per calendar month (by exit date) — the behaviour-over-time
    series that accumulates across uploads/syncs without re-uploading old data."""
    from collections import defaultdict
    buckets: Dict[str, List[RoundTrip]] = defaultdict(list)
    for t in trips:
        d = _d(t.exit_date)
        if d:
            buckets[f"{d.year}-{d.month:02d}"].append(t)
    out = []
    for month in sorted(buckets):
        bt = buckets[month]
        leaks = detect_leaks(bt)
        out.append({
            "month": month,
            "n_trades": len(bt),
            "pnl": round(sum(t.pnl for t in bt), 2),
            "score": _discipline_score(bt, leaks, sum(abs(t.pnl) for t in bt)),
        })
    return out


def quarterly_discipline(trips: List[RoundTrip]) -> List[Dict]:
    """Per-calendar-quarter summary (by exit date — round-trips spanning a
    boundary land in the exit quarter, same convention as monthly_discipline).
    The quarterly counterfactual uses the FULL-history median size cap so
    quarter figures stay consistent with the all-time headline; documented on
    /methodology."""
    from collections import defaultdict
    if not trips:
        return []
    buckets: Dict[str, List[RoundTrip]] = defaultdict(list)
    for t in trips:
        d = _d(t.exit_date)
        if d:
            buckets[f"{d.year}-Q{(d.month - 1) // 3 + 1}"].append(t)
    global_med = statistics.median(abs(t.qty * t.entry_px) for t in trips)
    out = []
    for quarter in sorted(buckets):
        qt = buckets[quarter]
        leaks = detect_leaks(qt)
        pnl = sum(t.pnl for t in qt)
        disciplined = counterfactual_disciplined(qt, med_notional=global_med)
        top = leaks[0] if leaks else None
        out.append({
            "quarter": quarter,
            "n_trades": len(qt),
            "pnl": round(pnl, 2),
            "score": _discipline_score(qt, leaks, sum(abs(t.pnl) for t in qt)),
            "quantified_leak": round(disciplined - pnl, 2),
            "top_leak": leak_key(top.name) if top else None,
            "top_leak_name": (top.name.split("(")[0].strip() if top else None),
        })
    return out


@dataclass
class CoachReport:
    n_trades: int
    hit_rate: float
    avg_win: float
    avg_loss: float
    payoff_ratio: float
    expectancy_per_trade: float
    total_pnl: float
    leaks: List[Leak] = field(default_factory=list)
    insights: Dict = field(default_factory=dict)
    monthly: List[Dict] = field(default_factory=list)
    open_tail: Optional[Dict] = None  # buys newer than the last closed trade
    quarterly: List[Dict] = field(default_factory=list)
    # Single coherent counterfactual: P&L under disciplined rules (size cap + stop),
    # and the swing vs. actual. This is the defensible headline number — NOT the sum
    # of the per-leak estimates, which overlap.
    disciplined_pnl: float = 0.0
    total_quantified_leak: float = 0.0
    discipline_score: int = 50
    narrative: str = ""

    def to_dict(self) -> Dict:
        d = self.__dict__.copy()
        d["leaks"] = [l.__dict__ for l in self.leaks]
        return d


def _severity(dollars: float, total_pnl_abs: float) -> str:
    if total_pnl_abs <= 0:
        return "low"
    r = dollars / total_pnl_abs
    return "high" if r > 0.25 else ("medium" if r > 0.08 else "low")


def detect_leaks(trips: List[RoundTrip], *, fees_paid: float = 0.0,
                 slippage_bps: float = 5.0) -> List[Leak]:
    leaks: List[Leak] = []
    if not trips:
        return leaks
    wins = [t for t in trips if t.is_win]
    losses = [t for t in trips if not t.is_win]
    avg_win = statistics.mean([t.pnl for t in wins]) if wins else 0.0
    avg_loss = statistics.mean([abs(t.pnl) for t in losses]) if losses else 0.0
    gross_pnl_abs = sum(abs(t.pnl) for t in trips)

    # 1. Disposition effect: holding losers longer than winners + asymmetric size.
    if wins and losses:
        hw = statistics.mean([t.hold_days for t in wins]) or 0.001
        hl = statistics.mean([t.hold_days for t in losses])
        if hl > 1.3 * hw and avg_loss > avg_win:
            # Counterfactual: if losers were cut to the size of an average winner.
            cost = sum(max(0.0, abs(t.pnl) - avg_win) for t in losses)
            leaks.append(Leak(
                "Disposition effect (holding losers, cutting winners)",
                _severity(cost, gross_pnl_abs), round(cost, 2),
                f"You hold losers {hl:.0f}d vs winners {hw:.0f}d, and your average "
                f"loss (${avg_loss:,.0f}) is bigger than your average win (${avg_win:,.0f}).",
                "Set a hard stop at entry and a take-profit target; let the rules "
                "close trades, not your hope. Aim for avg win >= avg loss.",
            ))

    # 2. Overtrading: churn + cost drag.
    short_holds = [t for t in trips if t.hold_days <= 3]
    notional = sum(abs(t.qty * t.entry_px) for t in trips)
    slip = notional * 2 * slippage_bps / 1e4
    drag = fees_paid + slip
    if len(short_holds) / len(trips) > 0.35 or drag > 0.05 * gross_pnl_abs:
        leaks.append(Leak(
            "Overtrading / cost drag",
            _severity(drag, gross_pnl_abs), round(drag, 2),
            f"{len(short_holds)}/{len(trips)} trades held <=3 days; "
            f"~${drag:,.0f} paid in fees+slippage across ${notional:,.0f} traded.",
            "Trade less, size more deliberately. Require a written thesis before "
            "each entry; if you can't, don't trade.",
        ))

    # 3. Oversizing / tail risk: one trade dominating losses.
    if len(losses) >= 3:
        loss_amts = sorted([abs(t.pnl) for t in losses], reverse=True)
        med_loss = statistics.median(loss_amts)
        worst = loss_amts[0]
        if med_loss > 0 and worst > 3 * med_loss:
            excess = worst - 3 * med_loss
            leaks.append(Leak(
                "Oversizing / tail risk (one trade dominates losses)",
                _severity(excess, gross_pnl_abs), round(excess, 2),
                f"Your worst loss (${worst:,.0f}) is {worst/med_loss:.1f}x your median "
                f"loss (${med_loss:,.0f}) — position sizing is inconsistent.",
                "Cap risk per trade at a fixed % of equity (e.g. 1-2%). Size by "
                "volatility so each position risks the same dollar amount.",
            ))

    # 4. Revenge trading: larger-than-usual trades right after a loss.
    sizes = [abs(t.qty * t.entry_px) for t in trips]
    med_size = statistics.median(sizes) if sizes else 0.0
    revenge_pnl = 0.0
    revenge_n = 0
    for i in range(1, len(trips)):
        prev, cur = trips[i - 1], trips[i]
        ed_prev, ed_cur = _d(prev.exit_date), _d(cur.entry_date)
        gap = (ed_cur - ed_prev).days if ed_prev and ed_cur else 99
        if not prev.is_win and 0 <= gap <= 3 and abs(cur.qty * cur.entry_px) > 1.3 * med_size:
            revenge_pnl += cur.pnl
            revenge_n += 1
    if revenge_n >= 2 and revenge_pnl < 0:
        leaks.append(Leak(
            "Revenge trading (sizing up after losses)",
            _severity(-revenge_pnl, gross_pnl_abs), round(-revenge_pnl, 2),
            f"{revenge_n} oversized trades placed within 3 days of a loss, "
            f"netting ${revenge_pnl:,.0f}.",
            "After any loss, enforce a cooldown: no new position for 24h, and the "
            "next trade must be normal-sized. Automate the block.",
        ))

    # 5. Negative payoff structure (letting losers exceed winners).
    if wins and losses and avg_win > 0:
        payoff = avg_loss / avg_win if avg_win else 0.0
        if payoff > 1.15:
            # Counterfactual: bring avg loss down to avg win (symmetric payoff).
            cost = (avg_loss - avg_win) * len(losses)
            leaks.append(Leak(
                "Inverted payoff (losers bigger than winners)",
                _severity(cost, gross_pnl_abs), round(cost, 2),
                f"Average loss ${avg_loss:,.0f} is {payoff:.1f}x your average win "
                f"${avg_win:,.0f}. Even a 55% hit-rate loses money at this ratio.",
                "Define the exit before the entry: stop-loss no wider than your "
                "typical winner. Target a payoff ratio >= 1.5.",
            ))

    leaks.sort(key=lambda l: l.dollars, reverse=True)
    return leaks


def counterfactual_disciplined(trips: List[RoundTrip], *, stop_pct: float = 0.15,
                               med_notional: Optional[float] = None) -> float:
    """P&L if two simple rules had been enforced, applied together (no double-count):

      * size discipline — cap each position's notional at the median notional;
      * stop-loss — cap each trade's loss at `stop_pct` of its notional.

    Wins are scaled down by the same size cap (discipline costs some upside too —
    an honest teaching point). Returns the disciplined total P&L.

    `med_notional` lets period slices (quarters) use the FULL-history median so
    period figures reconcile with the all-time headline.
    """
    if not trips:
        return 0.0
    notionals = [abs(t.qty * t.entry_px) for t in trips]
    med = med_notional or statistics.median(notionals) or 1.0
    disc = 0.0
    for t in trips:
        notional = abs(t.qty * t.entry_px) or 1.0
        scale = min(1.0, med / notional)             # shrink oversized positions
        ret_pct = t.pnl / notional                   # signed return on notional
        disciplined_ret = max(ret_pct, -stop_pct)    # stop caps the downside
        disc += disciplined_ret * notional * scale
    return disc


def price_based_leaks(trips: List[RoundTrip], fetch_ohlc, *,
                      stop_pct: float = 0.15, lookahead_days: int = 20) -> List[Leak]:
    """Two leaks that need real price paths (so they're precise, not modeled):

      * No stop-loss — for each loser, did a `stop_pct` stop ever trigger between
        entry and exit? If so, how much would exiting there have saved?
      * Cutting winners early — for each winner, how far did it run in the
        `lookahead_days` after you sold?

    `fetch_ohlc(symbol, start, end)` returns a date-indexed OHLC frame (or None).
    """
    from collections import defaultdict
    from concurrent.futures import ThreadPoolExecutor
    by_sym: Dict[str, List[RoundTrip]] = defaultdict(list)
    for t in trips:
        by_sym[t.symbol].append(t)

    # One price window per symbol. Fetch them concurrently — a long history
    # spans 100+ symbols, and sequential network fetches turn a page load
    # into multiple seconds. Failures degrade to "no data for that symbol".
    windows: Dict[str, Tuple[str, str]] = {}
    for sym, ts in by_sym.items():
        eds = [_d(t.entry_date) for t in ts if _d(t.entry_date)]
        xds = [_d(t.exit_date) for t in ts if _d(t.exit_date)]
        if eds and xds:
            windows[sym] = (min(eds).isoformat(),
                            (max(xds) + _dt.timedelta(days=lookahead_days + 5)).isoformat())

    def _safe_fetch(sym: str):
        start, end = windows[sym]
        try:
            return sym, fetch_ohlc(sym, start, end)
        except Exception:  # noqa: BLE001
            return sym, None

    frames: Dict[str, object] = {}
    if windows:
        with ThreadPoolExecutor(max_workers=min(8, len(windows))) as pool:
            frames = dict(pool.map(_safe_fetch, windows))

    stop_saved = 0.0
    left_on_table = 0.0
    n_stop = n_left = 0
    for sym, ts in by_sym.items():
        df = frames.get(sym)
        if df is None or len(df) == 0:
            continue
        dates = df.index.date
        for t in ts:
            ed, xd = _d(t.entry_date), _d(t.exit_date)
            if not ed or not xd:
                continue
            held = df[(dates > ed) & (dates <= xd)]
            if not t.is_win and len(held):
                stop_px = t.entry_px * (1 - stop_pct)            # long stop
                if float(held["Low"].min()) <= stop_px:
                    stopped_loss = t.qty * stop_pct * t.entry_px
                    saved = abs(t.pnl) - stopped_loss
                    if saved > 0:
                        stop_saved += saved
                        n_stop += 1
            if t.is_win:
                after = df[dates > xd].head(lookahead_days)
                if len(after):
                    hi = float(after["High"].max())
                    if hi > t.exit_px:
                        left_on_table += (hi - t.exit_px) * t.qty
                        n_left += 1

    gross = sum(abs(t.pnl) for t in trips) or 1.0
    out: List[Leak] = []
    if stop_saved > 0:
        out.append(Leak(
            "No stop-loss (measured on real prices)", _severity(stop_saved, gross),
            round(stop_saved, 2),
            f"On {n_stop} losing trades, a {stop_pct:.0%} stop would have exited earlier "
            f"and saved you ${stop_saved:,.0f}.",
            "Place the stop at entry and let it execute — never widen it to 'give it room'.",
        ))
    if left_on_table > 0.02 * gross:
        out.append(Leak(
            "Cutting winners early", _severity(left_on_table, gross),
            round(left_on_table, 2),
            f"On {n_left} winners, the stock ran a further ${left_on_table:,.0f} in the "
            f"{lookahead_days} days after you sold.",
            "Use a trailing stop to stay in winners instead of selling on the first pop.",
        ))
    return out


def _discipline_score(trips: List[RoundTrip], leaks: List[Leak], total_pnl_abs: float) -> int:
    score = 85
    for l in leaks:
        hit = {"high": 22, "medium": 12, "low": 5}[l.severity]
        score -= hit
    return max(5, min(100, score))


def audit_trades(txns: Sequence[Transaction], *, fees_paid: float = 0.0,
                 narrate: Optional[Callable[[CoachReport], str]] = None,
                 price_fetcher=None) -> CoachReport:
    """Full behavioral audit of a trade history. Deterministic; `narrate` and
    `price_fetcher` optional. When `price_fetcher` is supplied, two precise
    price-path leaks (real-stop savings, winners-left-on-table) are added."""
    trips = reconstruct_round_trips(txns)
    wins = [t for t in trips if t.is_win]
    losses = [t for t in trips if not t.is_win]
    avg_win = statistics.mean([t.pnl for t in wins]) if wins else 0.0
    avg_loss = statistics.mean([abs(t.pnl) for t in losses]) if losses else 0.0
    hit = len(wins) / len(trips) if trips else 0.0
    payoff = (avg_win / avg_loss) if avg_loss else 0.0
    expectancy = hit * avg_win - (1 - hit) * avg_loss
    total_pnl = sum(t.pnl for t in trips)

    leaks = detect_leaks(trips, fees_paid=fees_paid)
    if price_fetcher is not None and trips:
        try:
            leaks = leaks + price_based_leaks(trips, price_fetcher)
            leaks.sort(key=lambda l: l.dollars, reverse=True)
        except Exception:  # noqa: BLE001 — price data is a bonus, never fatal
            pass
    disciplined = counterfactual_disciplined(trips)
    report = CoachReport(
        n_trades=len(trips), hit_rate=round(hit, 3),
        avg_win=round(avg_win, 2), avg_loss=round(avg_loss, 2),
        payoff_ratio=round(payoff, 2), expectancy_per_trade=round(expectancy, 2),
        total_pnl=round(total_pnl, 2), leaks=leaks,
        disciplined_pnl=round(disciplined, 2),
        total_quantified_leak=round(disciplined - total_pnl, 2),
    )
    report.discipline_score = _discipline_score(trips, leaks, abs(total_pnl))
    report.insights = behavioral_insights(trips, leaks)
    report.monthly = monthly_discipline(trips)
    report.open_tail = open_tail(txns, trips)
    report.quarterly = quarterly_discipline(trips)
    if narrate:
        try:
            report.narrative = narrate(report)
        except Exception:
            report.narrative = ""
    return report


# --------------------------------------------------------------------------- #
# LLM narration (optional) — turns deterministic findings into a coaching voice.
# The LLM is given the NUMBERS and told to explain, never to invent figures.
# --------------------------------------------------------------------------- #

def sample_history() -> List[Transaction]:
    """A believable retail trader for demos/tests: cuts winners fast, holds losers,
    oversizes after a loss, overtrades. Surfaces the leaks the coach detects."""
    t: List[Transaction] = []

    def trade(sym, qty, buy_px, sell_px, buy_date, sell_date):
        t.append(Transaction(date=buy_date, type="Buy", symbol=sym, quantity=qty,
                             price=buy_px, amount=-qty * buy_px))
        t.append(Transaction(date=sell_date, type="Sell", symbol=sym, quantity=qty,
                             price=sell_px, amount=qty * sell_px))

    for a in [("AAPL", 50, 180, 184, "2025-01-06", "2025-01-08"),
              ("MSFT", 30, 410, 418, "2025-01-09", "2025-01-10"),
              ("NVDA", 40, 130, 134, "2025-01-13", "2025-01-15"),
              ("AMD", 60, 120, 123, "2025-01-16", "2025-01-17"),
              ("TSLA", 20, 240, 248, "2025-01-21", "2025-01-23"),
              ("META", 25, 600, 612, "2025-01-27", "2025-01-28"),
              ("GOOG", 30, 195, 199, "2025-02-03", "2025-02-04")]:
        trade(*a)
    for a in [("PLTR", 200, 85, 68, "2025-01-10", "2025-03-20"),
              ("COIN", 60, 280, 210, "2025-01-14", "2025-03-10"),
              ("SOFI", 400, 18, 13, "2025-01-22", "2025-04-01")]:
        trade(*a)
    trade("NVDA", 300, 120, 101, "2025-03-11", "2025-03-25")     # oversized revenge
    trade("MSTR", 100, 1800, 1250, "2025-02-01", "2025-04-15")   # catastrophic, no stop
    return t


def make_narrator(invoke_text: Callable[[list], str]) -> Callable[[CoachReport], str]:
    def narrate(r: CoachReport) -> str:
        leaks = "\n".join(
            f"- {l.name}: cost ~${l.dollars:,.0f} ({l.severity}). {l.evidence} FIX: {l.fix}"
            for l in r.leaks) or "- No major leaks detected."
        return invoke_text([
            ("system",
             "You are a sharp, supportive trading coach. You are given DETERMINISTIC, "
             "already-computed findings about a trader's behavior. Explain them in a "
             "direct, encouraging voice and prioritize the 2-3 highest-impact fixes. "
             "NEVER invent or change any number — use only the figures provided. "
             "End with one concrete habit to start this week. <=200 words."),
            ("human",
             f"Trades: {r.n_trades}, hit-rate {r.hit_rate:.0%}, payoff {r.payoff_ratio}, "
             f"expectancy ${r.expectancy_per_trade:,.0f}/trade, net P&L ${r.total_pnl:,.0f}, "
             f"discipline score {r.discipline_score}/100.\n"
             f"Total quantified behavioral leak: ${r.total_quantified_leak:,.0f}.\n"
             f"Leaks:\n{leaks}"),
        ])
    return narrate
