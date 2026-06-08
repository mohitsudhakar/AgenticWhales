"""Pre-trade decision support — a fast, honest read on a symbol's setup.

This is the "decision-support" half of the product. It is deliberately NOT the
heavy multi-agent debate (which the edge probe showed has no predictive edge):
it's the deterministic Classical signal stack (momentum / trend / Bollinger /
vol-regime) translated into a plain-English read, with an OPTIONAL short LLM
note. No buy/sell recommendation, no price target — context for the human, who
still decides.
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, Optional


def analyze_symbol(symbol: str, *, as_of: Optional[str] = None, llm_note: bool = False,
                   provider: str = "deepseek", model: str = "deepseek-chat") -> Dict:
    from . import classical

    date = as_of or _dt.date.today().isoformat()
    try:
        res = classical.analyze_classical(symbol, date)
    except Exception:  # noqa: BLE001
        res = None
    if res is None:
        return {"available": False, "symbol": symbol.upper()}

    out = {
        "available": True,
        "symbol": symbol.upper(),
        "rating": getattr(res.decision.rating, "value", str(res.decision.rating)),
        "score": round(float(res.aggregate_score), 3),
        "last_price": round(float(res.last_price), 2),
        "signals": [
            {"name": s.name, "direction": s.direction,
             "strength": round(float(s.strength), 2), "notes": s.notes}
            for s in res.signals
        ],
        "summary": (getattr(res.decision, "executive_summary", "") or "")[:400],
    }
    if llm_note:
        try:
            out["ai_note"] = _ai_note(out, provider, model)
        except Exception:  # noqa: BLE001 — the note is a bonus, never fatal
            out["ai_note"] = ""
    return out


def _ai_note(read: Dict, provider: str, model: str) -> str:
    from .llm_clients import create_llm_client
    from .llm_clients.base_client import normalize_content

    llm = create_llm_client(provider=provider, model=model).get_llm()
    sig = "; ".join(
        f"{s['name']} {'+' if s['direction'] > 0 else ('-' if s['direction'] < 0 else 'flat')} "
        f"(strength {s['strength']}){' — ' + s['notes'] if s['notes'] else ''}"
        for s in read["signals"]
    )
    msgs = [
        ("system",
         "You are a concise trading-desk analyst. In <=60 words give a plain-English read "
         "of this technical setup and name ONE risk to watch. Do NOT give buy/sell advice "
         "or price targets — just context for a human who will decide."),
        ("human",
         f"{read['symbol']} at ${read['last_price']}. Classical signal score {read['score']} "
         f"(maps to {read['rating']}). Signals: {sig}."),
    ]
    return str(normalize_content(llm.invoke(msgs)).content).strip()
