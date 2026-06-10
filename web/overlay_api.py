"""API for the defensive trend-overlay product surface.

Read-only and deterministic: the endpoint reports the mechanical state of the
walk-forward-validated blend (agenticwhales/overlay.py) — target weights,
per-sleeve trend/vol status, and the committed evidence. It never places
orders and never makes a recommendation; the UI frames it as educational.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Tuple

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from agenticwhales import overlay

router = APIRouter()
log = logging.getLogger(__name__)

# yfinance pulls ~2y of history for 7 ETFs — cache the computed status for a
# few hours; the 12-month signal moves daily at most.
_CACHE: Dict[Tuple[float], Dict] = {}
_CACHE_AT: Dict[Tuple[float], float] = {}
_CACHE_TTL = 4 * 3600.0
_CACHE_LOCK = threading.Lock()


@router.get("/api/overlay/status")
def overlay_status(leverage: float = Query(1.0, ge=0.0, le=2.0)):
    """Current target weights for the blend. Sync route on purpose — FastAPI
    runs it in the threadpool, keeping the slow yfinance fetch off the loop."""
    key = (round(leverage, 2),)
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and now - _CACHE_AT.get(key, 0) < _CACHE_TTL:
            return cached
    try:
        status = overlay.fetch_overlay_status(leverage=leverage)
    except Exception as exc:  # noqa: BLE001 — market data can be flaky
        log.warning("overlay status failed: %s", exc)
        return JSONResponse(
            {"error": "Market data is unavailable right now — try again shortly."},
            status_code=503)
    out = status.to_dict()
    out["disclaimer"] = (
        "Educational only — the mechanical state of a published rule set, "
        "computed from public price history; it predicts nothing. Not "
        "investment advice; no orders are placed.")
    with _CACHE_LOCK:
        _CACHE[key] = out
        _CACHE_AT[key] = now
    return out
