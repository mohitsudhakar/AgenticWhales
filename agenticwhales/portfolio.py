"""User portfolio: stores per-ticker positions and formats them for agent prompts.

Persistence is a single JSON file at ``~/.agenticwhales/portfolio.json``.
Positions are a flat dict keyed by uppercase ticker symbol. Each entry holds:

- ``qty`` (float, required): signed quantity. Positive = long, negative = short, 0 = flat.
- ``avg_cost`` (float | None): average entry price per unit. Optional.
- ``notes`` (str | None): free-text notes shown to the agents. Optional.

All fields are optional from the user's perspective — a ticker with no entry
yields an empty prompt block (i.e. agents treat it as flat).
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# Futures contract month codes: F=Jan, G=Feb, H=Mar, J=Apr, K=May, M=Jun,
# N=Jul, Q=Aug, U=Sep, V=Oct, X=Nov, Z=Dec.
_FUTURES_MONTH_CODES = "FGHJKMNQUVXZ"
# A dated futures contract symbol: ROOT (1-3 alpha) + month code + 2-digit year.
_FUTURES_CONTRACT_RE = re.compile(rf"^([A-Z]{{1,3}})([{_FUTURES_MONTH_CODES}])(\d{{2}})$")

_LOCK = threading.RLock()
_PATH = Path(os.path.expanduser("~/.agenticwhales/portfolio.json"))


def _ensure_dir() -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)


def load_all() -> Dict[str, Dict[str, Any]]:
    """Return the entire portfolio as a dict keyed by ticker. Missing file → empty."""
    if not _PATH.exists():
        return {}
    try:
        data = json.loads(_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_all(positions: Dict[str, Dict[str, Any]]) -> None:
    """Overwrite the entire portfolio. Caller is responsible for shape."""
    _ensure_dir()
    cleaned: Dict[str, Dict[str, Any]] = {}
    for raw_sym, pos in (positions or {}).items():
        sym = (raw_sym or "").strip().upper()
        if not sym:
            continue
        if not isinstance(pos, dict):
            continue
        entry: Dict[str, Any] = {}
        if "qty" in pos and pos["qty"] not in (None, ""):
            try:
                qty_val = float(pos["qty"])
            except (TypeError, ValueError):
                continue
            if qty_val == 0:
                continue  # zero-qty positions are flat, drop them
            entry["qty"] = qty_val
        if "avg_cost" in pos and pos["avg_cost"] not in (None, ""):
            try:
                entry["avg_cost"] = float(pos["avg_cost"])
            except (TypeError, ValueError):
                pass
        notes = (pos.get("notes") or "").strip()
        if notes:
            entry["notes"] = notes
        if entry:
            cleaned[sym] = entry
    with _LOCK:
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cleaned, indent=2))
        tmp.replace(_PATH)


def get(symbol: str) -> Optional[Dict[str, Any]]:
    """Return the position dict for one ticker, or None when absent/flat."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    pos = load_all().get(sym)
    if not pos:
        return None
    qty = pos.get("qty")
    if qty is None or qty == 0:
        return None
    return pos


def _futures_root(symbol: str) -> Optional[str]:
    """Return the futures root for ``XX=F`` (continuous) or ``XXMyy`` (dated), else None."""
    s = (symbol or "").strip().upper()
    if s.endswith("=F"):
        root = s[:-2]
        return root if root.isalpha() else None
    m = _FUTURES_CONTRACT_RE.match(s)
    return m.group(1) if m else None


def find_related(symbol: str) -> Dict[str, Dict[str, Any]]:
    """Return every portfolio entry whose key relates to the given symbol.

    Match rules (all case-insensitive, applied to non-zero positions):
    1. Exact match on the full key.
    2. First whitespace-delimited token of the key equals the symbol
       (catches options like ``NVDA $215 5/11/2026 CALL`` for symbol ``NVDA``).
    3. Futures: when the analyzed symbol or the portfolio key encodes a
       futures contract with the same root, they match. So ``CL=F`` finds
       ``CLM26``, and ``CLM26`` also finds ``CL=F``. Different roots
       (``GC`` vs ``CL``) never cross.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {}

    sym_futures_root = _futures_root(sym)
    out: Dict[str, Dict[str, Any]] = {}
    for raw_key, pos in load_all().items():
        if not pos:
            continue
        qty = pos.get("qty")
        if qty is None or qty == 0:
            continue
        key_upper = raw_key.upper()
        if key_upper == sym:
            out[raw_key] = pos
            continue
        first_token = key_upper.split(None, 1)[0] if key_upper else ""
        if first_token == sym:
            out[raw_key] = pos
            continue
        # Futures cross-match by root.
        if sym_futures_root:
            key_root = _futures_root(first_token or key_upper)
            if key_root and key_root == sym_futures_root:
                out[raw_key] = pos
    return out


def _describe_one(label: str, pos: Dict[str, Any]) -> str:
    qty = float(pos.get("qty", 0) or 0)
    if qty == 0:
        return ""
    side = "LONG" if qty > 0 else "SHORT"
    abs_qty = abs(qty)
    head = f"{side} {abs_qty:g} units of {label}"
    if pos.get("avg_cost") is not None:
        head += f" @ avg cost {float(pos['avg_cost']):g}"
    notes = (pos.get("notes") or "").strip()
    if notes:
        head += f" — notes: {notes}"
    return head


def _net_side(positions: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Return 'LONG', 'SHORT', or 'MIXED' (or None when flat)."""
    sides = set()
    for pos in positions.values():
        qty = float(pos.get("qty", 0) or 0)
        if qty > 0:
            sides.add("LONG")
        elif qty < 0:
            sides.add("SHORT")
    if not sides:
        return None
    if len(sides) == 1:
        return next(iter(sides))
    return "MIXED"


_VOCAB_LONG = (
    "The user is **LONG**. Express recommendations in terms of this LONG position:\n"
    "- 'Add' / 'Increase' = buy more (grow the long).\n"
    "- 'Hold' / 'Maintain' = keep the long as-is.\n"
    "- 'Trim' / 'Reduce' = sell some (shrink the long).\n"
    "- 'Exit' / 'Sell' = close the long fully.\n"
    "- 'Reverse' = close the long and open a short.\n"
    "DO NOT recommend reducing or covering short exposure — the user has none."
)
_VOCAB_SHORT = (
    "The user is **SHORT**. Express recommendations in terms of this SHORT position:\n"
    "- 'Add' / 'Increase' = sell more (grow the short).\n"
    "- 'Hold' / 'Maintain' = keep the short as-is.\n"
    "- 'Reduce' / 'Trim' = buy back some (shrink the short).\n"
    "- 'Cover' / 'Close' = buy back the entire short (= flat).\n"
    "- 'Reverse' = cover the short and open a long.\n"
    "DO NOT recommend reducing or trimming long exposure — the user has no long position. "
    "DO NOT phrase advice as 'sell' to mean exit; for a short, selling means *adding* to the position."
)
_VOCAB_MIXED = (
    "The user has **MIXED** exposure (both long and short legs related to this name). "
    "Address each leg explicitly by its label and side. Do not collapse them into a single 'reduce/add' instruction."
)


def format_for_prompt(symbol: str, position: Optional[Dict[str, Any]] = None) -> str:
    """Return a directive prompt block for every related position, or '' when flat.

    The block is meant to be placed at the TOP of an agent prompt. It states
    the position(s) in unambiguous terms and provides side-specific
    vocabulary so the model does not default to long-biased language like
    "reduce long exposure" when the user is actually short.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return ""

    # Single explicit position passed in (legacy path).
    if position is not None:
        qty = float(position.get("qty", 0) or 0)
        if qty == 0:
            return ""
        positions = {sym: position}
    else:
        positions = find_related(sym)
        if not positions:
            return ""

    net = _net_side(positions)
    if net is None:
        return ""

    lines = [_describe_one(label, pos) for label, pos in sorted(positions.items())]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    bullets = "\n".join(f"  • {line}" for line in lines)
    if net == "LONG":
        vocab = _VOCAB_LONG
    elif net == "SHORT":
        vocab = _VOCAB_SHORT
    else:
        vocab = _VOCAB_MIXED

    return (
        "════════ USER'S CURRENT POSITION ════════\n"
        f"The user already holds the following {sym}-related position(s):\n"
        f"{bullets}\n\n"
        f"{vocab}\n"
        "Every recommendation must be a *delta* to the position above. "
        "When you can, name a concrete size (e.g. 'cover 50%', 'add 0.5x current size') "
        "and concrete entry / stop / target levels.\n"
        "════════════════════════════════════════"
    )
