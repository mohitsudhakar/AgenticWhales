"""Disagreement index + Classical auto-inject — Phase 2 deliverable #7.

After every PM decision the runner hands the Bull and Bear histories to
`record_disagreement(...)`. We compute:

  1. **Text similarity** between the Bull and Bear histories via cosine
     similarity over a hashing-trick TF vector (no embedding API call;
     deterministic; ~10ms for two ~1000-word texts).
  2. **Rating agreement**: did Bull and Bear converge on the same
     directional view? (Inferred from the histories' leading sentence or
     a regex on common patterns like "I lean toward Buy / Sell / Hold".)
  3. Persist a row to `disagreement_log` keyed by session_id.

When a Thesis has `auto_inject_classical = true` AND the agreement is high
(similarity > 0.55) the runner ALSO runs the deterministic Classical
Analyst as a third voice. Classical's decision is stashed on the session
under `classical_decision`; the UI surfaces it as an explicit "Classical
say" card. We don't *replace* the LLM decision — we surface the contrast.

Why hashing-trick TF instead of real embeddings: zero dependencies, zero
provider cost, runs in milliseconds, perfectly deterministic. The
*relative* ordering of similarity scores is what we need; absolute values
don't matter for downstream consumers.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Hashing-trick dimensionality. 1024 is plenty for ~1000-word texts; bigger
# costs more memory without changing the ranking.
HASH_DIMS = 1024

# Cutoff for "high agreement" → the Classical Analyst auto-injects.
AGREEMENT_THRESHOLD = 0.55


# ---------------------------------------------------------------------------
# Cosine sim via hashing-trick TF vectors
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z][a-z0-9'-]+")


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


def _hash_idx(tok: str) -> int:
    h = hashlib.blake2b(tok.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(h, "big") % HASH_DIMS


def _hash_vector(text: str) -> List[float]:
    """Sparse-dense TF vector: token count at hash(token) % DIMS."""
    vec = [0.0] * HASH_DIMS
    for tok in _tokenize(text):
        vec[_hash_idx(tok)] += 1.0
    return vec


def cosine_similarity(a: str, b: str) -> float:
    """Cosine similarity of two texts via hashing-trick TF. Returns 0 when
    either text is empty; clipped to [0, 1] (TF vectors are non-negative so
    cosine is naturally in [0, 1])."""
    if not a or not b:
        return 0.0
    va = _hash_vector(a)
    vb = _hash_vector(b)
    dot = sum(x * y for x, y in zip(va, vb))
    norm_a = math.sqrt(sum(x * x for x in va))
    norm_b = math.sqrt(sum(x * x for x in vb))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


# ---------------------------------------------------------------------------
# Rating agreement inference
# ---------------------------------------------------------------------------

# Regex anchors that signal each side's final lean. We look at the LAST
# occurrence in each history since debates often start neutral and resolve.
_LEAN_PATTERNS = {
    "long":    re.compile(r"\b(buy|overweight|long|bullish|add|accumulate|enter long)\b", re.I),
    "short":   re.compile(r"\b(sell|underweight|short|bearish|exit|cover|enter short)\b", re.I),
    "neutral": re.compile(r"\b(hold|maintain|neutral|stay flat|range[- ]?bound)\b", re.I),
}


def infer_lean(text: str) -> Optional[str]:
    """Best-effort 'long' / 'short' / 'neutral' / None classification."""
    if not text:
        return None
    last_pos = {}
    for label, pat in _LEAN_PATTERNS.items():
        matches = list(pat.finditer(text))
        if matches:
            last_pos[label] = matches[-1].start()
    if not last_pos:
        return None
    return max(last_pos.items(), key=lambda kv: kv[1])[0]


def ratings_agree(bull_text: str, bear_text: str) -> bool:
    """True iff Bull and Bear lean the same direction."""
    bull_lean = infer_lean(bull_text)
    bear_lean = infer_lean(bear_text)
    if bull_lean is None or bear_lean is None:
        return False
    return bull_lean == bear_lean


# ---------------------------------------------------------------------------
# Persistence + recording API
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DisagreementSnapshot:
    user_id: str
    recipe_id: Optional[str]
    session_id: str
    bull_model: Optional[str]
    bear_model: Optional[str]
    similarity: float
    rating_agreement: bool


def record_disagreement(
    *,
    user_id: str,
    session_id: str,
    bull_history: str,
    bear_history: str,
    bull_model: Optional[str] = None,
    bear_model: Optional[str] = None,
    recipe_id: Optional[str] = None,
) -> DisagreementSnapshot:
    """Compute + persist a row in `disagreement_log`. Idempotent on session_id
    (re-recording overwrites the prior row for the same session)."""
    sim = cosine_similarity(bull_history or "", bear_history or "")
    agree = ratings_agree(bull_history or "", bear_history or "")
    snapshot = DisagreementSnapshot(
        user_id=user_id, recipe_id=recipe_id, session_id=session_id,
        bull_model=bull_model, bear_model=bear_model,
        similarity=sim, rating_agreement=agree,
    )
    _persist(snapshot)
    return snapshot


def _persist(s: DisagreementSnapshot) -> None:
    from web import auth
    row = {
        "user_id": s.user_id,
        "recipe_id": s.recipe_id,
        "session_id": s.session_id,
        "bull_model": s.bull_model,
        "bear_model": s.bear_model,
        "similarity": s.similarity,
        "rating_agreement": s.rating_agreement,
        "recorded_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    # Memstore PK: session_id (we want at most one row per session even if
    # the runner mistakenly calls us twice).
    auth._memstore[("disagreement_log", s.session_id)] = row
    try:
        auth._upsert_columns("disagreement_log", row)
    except Exception:
        pass


def list_for_user(user_id: str, *, limit: int = 200) -> List[Dict[str, Any]]:
    from web import auth
    if auth._db_writable():
        try:
            rows = auth._select_columns(
                "disagreement_log",
                filters={"user_id": user_id},
                order="recorded_at.desc",
                limit=limit,
            ) or []
        except Exception:
            rows = []
    else:
        rows = []
    if not rows:
        rows = [
            r for (t, _), r in auth._memstore.items()
            if t == "disagreement_log" and r.get("user_id") == user_id
        ]
    rows.sort(key=lambda r: r.get("recorded_at") or "", reverse=True)
    return rows[:limit]


def should_auto_inject(recipe_row: Dict[str, Any], similarity: float) -> bool:
    """Decision: should we run Classical as a third voice on this fire?

    Yes when both:
      - the Thesis has `auto_inject_classical = true`
      - Bull/Bear cosine ≥ threshold (high consensus = suspicious uniformity)
    """
    if not recipe_row:
        return False
    if not recipe_row.get("auto_inject_classical"):
        return False
    return similarity >= AGREEMENT_THRESHOLD
