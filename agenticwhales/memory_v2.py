"""Memory v2 — outcome-predictive retrieval (Phase 2 deliverable #4).

The existing `TradingMemoryLog` uses Jaccard token overlap for retrieval.
Demis-review D5: that retrieves *topically* similar entries, not
*outcome-predictive* ones. A reflection that said "I was wrong on NVDA
when AI sentiment shifted on Twitter" should surface when the user is
trading any AI-exposed name AND sentiment is shifting — not when they're
trading NVDA again on different drivers.

Memory v2 ships three pieces:

  1. **Embeddings.** Each journal entry / paper-order rationale gets a
     dense vector. Default: hashing-trick TF (1024-dim, deterministic, no
     external API). When `text-embedding-3-small` is configured the fetch
     swaps. We persist the vector blob in `memory_embeddings`.

  2. **Cosine retrieval.** Top-K by `dot(q, e) / (|q|·|e|)`. Linear scan
     over the user's vectors — fine up to ~10k entries, which we won't hit
     in v1.

  3. **Outcome-predictive re-ranker.** Each entry has a `predictiveness`
     score = `1 - mean_brier_at_resolution` for the entry's owning paper
     order (or 0.5 if the entry isn't linked to an outcome). Final score
     = cosine × predictiveness. Entries whose past decisions ended up
     *more correct than average* float to the top of the retrieval.

The Portfolio Manager's prompt context substrate can then swap from
Jaccard to this without touching the agent code: the public API is
`retrieve_relevant(user_id, query_text, k)` returning a list of
`{entry_id, body, score}` dicts.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)


# --- tunables --------------------------------------------------------------

EMBED_DIM = 1024                     # hashing-trick dimensionality (default fallback)
TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")

# Brier-to-predictiveness mapping: an entry tied to a paper order with
# `brier_component` in [0, 1] gets predictiveness = 1 - brier. Capped at
# [0.1, 1.0] so entries with bad past calls don't drop to zero — they still
# carry information.
PREDICTIVENESS_FLOOR = 0.1
PREDICTIVENESS_DEFAULT = 0.5         # entries with no resolved outcome

# Gemini embedding model ids. text-embedding-004 is the cost-effective default
# (768 dims). gemini-embedding-001 supports configurable output dims (768 /
# 1536 / 3072). We pin to text-embedding-004 by default to keep storage size
# predictable; users can override via env.
GEMINI_DEFAULT_MODEL = "text-embedding-004"
GEMINI_DIM_BY_MODEL = {
    "text-embedding-004":      768,
    "gemini-embedding-001":    3072,
    "gemini-embedding-exp-03-07": 3072,
}

# Resolved-once at first call. Picks the user's configured default embedding
# model: env var > Gemini if google-key configured > hashing-trick.
_DEFAULT_MODEL_CACHE: Optional[str] = None


# ---------------------------------------------------------------------------
# Embeddings — hashing-trick default + extensible API
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in TOKEN_PATTERN.findall(text or "")]


def _hash_idx(token: str) -> int:
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(h, "big") % EMBED_DIM


def embed_hashing_trick(text: str) -> List[float]:
    """Sparse-dense TF vector via hashing. Deterministic, no external API.
    Same trick we use in disagreement.py for cosine; centralized here to
    keep the memory-side surface independent of the disagreement-side."""
    vec = [0.0] * EMBED_DIM
    for tok in _tokenize(text):
        vec[_hash_idx(tok)] += 1.0
    return vec


def _default_embedding_model() -> str:
    """Resolve the embedding model to use when the caller doesn't specify.

    Priority order:
      1. AGENTICWHALES_EMBEDDING_MODEL env var (explicit operator choice).
      2. Gemini text-embedding-004 when GOOGLE_API_KEY is configured (the
         path that meaningfully improves retrieval quality over Jaccard).
      3. hashing-trick fallback (deterministic, zero-API).

    Cached after first resolution so a single deploy stays on one model
    (mixing embeddings across model_ids in the same corpus is a footgun —
    cosine isn't meaningful across spaces).
    """
    global _DEFAULT_MODEL_CACHE
    if _DEFAULT_MODEL_CACHE is not None:
        return _DEFAULT_MODEL_CACHE
    env = os.getenv("AGENTICWHALES_EMBEDDING_MODEL", "").strip()
    if env:
        _DEFAULT_MODEL_CACHE = env
    elif os.getenv("GOOGLE_API_KEY"):
        _DEFAULT_MODEL_CACHE = GEMINI_DEFAULT_MODEL
    else:
        _DEFAULT_MODEL_CACHE = "hashing-trick"
    log.debug("memory_v2 default embedding model: %s", _DEFAULT_MODEL_CACHE)
    return _DEFAULT_MODEL_CACHE


def reset_default_model_cache() -> None:
    """Test helper — re-resolve the default on next `embed()` call."""
    global _DEFAULT_MODEL_CACHE
    _DEFAULT_MODEL_CACHE = None


def embed_gemini(text: str, *, model: str = GEMINI_DEFAULT_MODEL) -> List[float]:
    """Real embedding via Google's Gemini embedding API.

    Uses `langchain_google_genai.GoogleGenerativeAIEmbeddings` which is
    already a project dependency (the chat models use the same package).
    Raises ImportError if the package isn't available; raises whatever
    the provider raises on auth / quota issues (caller's responsibility
    to catch + fall back).

    The model id we pass to the provider needs the `models/` prefix per
    Google's SDK. We strip/add as needed so callers can use the bare
    `text-embedding-004` form.
    """
    try:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
    except ImportError as exc:
        raise ImportError(
            "Gemini embeddings require langchain-google-genai; install or "
            "fall back to model='hashing-trick'."
        ) from exc

    provider_model = model if model.startswith("models/") else f"models/{model}"
    client = GoogleGenerativeAIEmbeddings(
        model=provider_model,
        google_api_key=os.getenv("GOOGLE_API_KEY"),
    )
    vec = client.embed_query(text)
    if not isinstance(vec, list):
        vec = list(vec)
    return [float(x) for x in vec]


def embed(text: str, *, model: Optional[str] = None) -> List[float]:
    """Public embedding entry point. Dispatches on model_id:

      - `'hashing-trick'`  → deterministic hashing-trick TF (1024 dims, no API)
      - `'text-embedding-004'` / `'gemini-embedding-001'` etc → Gemini API

    With `model=None` we use `_default_embedding_model()`. Failures from
    the Gemini path fall back to hashing-trick so a missing key / quota
    issue doesn't take retrieval offline entirely.
    """
    resolved = model or _default_embedding_model()
    if resolved == "hashing-trick":
        return embed_hashing_trick(text)
    if resolved in GEMINI_DIM_BY_MODEL or resolved.startswith("models/"):
        try:
            return embed_gemini(text, model=resolved)
        except Exception as exc:
            log.warning("Gemini embed failed (%s); falling back to hashing-trick", exc)
            return embed_hashing_trick(text)
    raise ValueError(f"unsupported embedding model: {resolved}")


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (math.sqrt(na) * math.sqrt(nb))))


# ---------------------------------------------------------------------------
# Vector storage: bytea-compatible float32 blob
# ---------------------------------------------------------------------------

def _pack_vector(vec: Sequence[float]) -> bytes:
    """Pack a vector as float32 bytes for the `memory_embeddings.vector_bytes`
    bytea column. Survives PostgREST encoding round-trips."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack_vector(blob: Any) -> List[float]:
    """Inverse of `_pack_vector`. Tolerates either bytes or hex-string
    (PostgREST sometimes returns bytea as `\\x...`). Dim is inferred from
    blob length — supports any embedding-model size."""
    if blob is None:
        return []
    if isinstance(blob, str):
        if blob.startswith("\\x"):
            blob = bytes.fromhex(blob[2:])
        else:
            try:
                blob = bytes.fromhex(blob)
            except ValueError:
                return []
    if not isinstance(blob, (bytes, bytearray)):
        return []
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StoredEmbedding:
    entry_id: str
    user_id: str
    source: str           # 'journal' | 'paper_order' | 'memory_log'
    vector: List[float]


def upsert_embedding(
    *, entry_id: str, user_id: str, source: str, text: str,
    model: Optional[str] = None,
) -> StoredEmbedding:
    """Compute + persist an embedding row. Idempotent on entry_id.

    `model=None` resolves via `_default_embedding_model()` (env >
    Gemini-if-keyed > hashing-trick). The actual resolved model_id is
    persisted so retrieval can compare like-with-like even when the deploy
    changes models mid-corpus.
    """
    from web import auth
    resolved = model or _default_embedding_model()
    vec = embed(text, model=resolved)
    row = {
        "entry_id": entry_id,
        "user_id": user_id,
        "source": source,
        "model_id": resolved,
        "vector_bytes": _pack_vector(vec).hex(),   # store as hex string for memstore
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    auth._memstore[("memory_embeddings", entry_id)] = row
    try:
        auth._upsert_columns("memory_embeddings", row, on_conflict="entry_id")
    except Exception:
        pass
    return StoredEmbedding(entry_id=entry_id, user_id=user_id, source=source, vector=vec)


def list_embeddings(user_id: str, *, source: Optional[str] = None) -> List[Dict[str, Any]]:
    from web import auth
    if auth._db_writable():
        filters: Dict[str, Any] = {"user_id": user_id}
        if source:
            filters["source"] = source
        try:
            return auth._select_columns("memory_embeddings", filters=filters) or []
        except Exception:
            pass
    return [
        r for (t, _), r in auth._memstore.items()
        if t == "memory_embeddings" and r.get("user_id") == user_id
        and (source is None or r.get("source") == source)
    ]


# ---------------------------------------------------------------------------
# Outcome-predictiveness scoring
# ---------------------------------------------------------------------------

def _predictiveness_for(entry_row: Dict[str, Any]) -> float:
    """Return a predictiveness score in [PREDICTIVENESS_FLOOR, 1].

    Currently linked via paper_order_id → decision_outcomes. Future
    extensions may also consult `journal_entries.kind == 'reflection'`
    quality scoring. For v1: an entry tied to a paper-order whose outcome
    has a low Brier component (high predictiveness) gets a higher score;
    entries without a linked outcome get the default 0.5.
    """
    from web import auth
    poid = entry_row.get("paper_order_id")
    if not poid:
        return PREDICTIVENESS_DEFAULT
    outcome = auth._memstore.get(("decision_outcomes", poid))
    if not outcome:
        return PREDICTIVENESS_DEFAULT
    brier = outcome.get("brier_component")
    if brier is None:
        return PREDICTIVENESS_DEFAULT
    try:
        return max(PREDICTIVENESS_FLOOR, min(1.0, 1.0 - float(brier)))
    except (TypeError, ValueError):
        return PREDICTIVENESS_DEFAULT


# ---------------------------------------------------------------------------
# Public retrieval
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetrievedEntry:
    entry_id: str
    body: str
    source: str
    cosine: float
    predictiveness: float
    score: float           # composite — cosine × predictiveness


def retrieve_relevant(
    user_id: str,
    query_text: str,
    *,
    k: int = 5,
    source_filter: Optional[str] = None,
) -> List[RetrievedEntry]:
    """Top-K outcome-predictive retrieval over the user's journal entries.

    1. Embed the query.
    2. Cosine-score every persisted embedding for this user.
    3. Multiply by predictiveness (derived from linked outcome's Brier).
    4. Return top-K with the underlying entry body, joined back from
       `journal_entries`.

    Source filter is optional; default returns across all sources.
    """
    if not query_text:
        return []
    from web import auth

    # Embed the query with the currently-configured default. Persisted rows
    # may have been written under a different model_id — we only compare
    # against rows whose model_id matches, since cosine across different
    # embedding spaces is meaningless.
    query_model = _default_embedding_model()
    q_vec = embed(query_text, model=query_model)
    embed_rows = list_embeddings(user_id, source=source_filter)
    if not embed_rows:
        return []

    scored: List[Tuple[float, float, float, Dict[str, Any]]] = []
    for emb in embed_rows:
        if emb.get("model_id") and emb.get("model_id") != query_model:
            continue   # different embedding space → skip
        vec = _unpack_vector(emb.get("vector_bytes"))
        if not vec or len(vec) != len(q_vec):
            continue
        cos = cosine(q_vec, vec)
        if cos <= 0:
            continue
        # Look up source row for predictiveness + body.
        if emb.get("source") == "journal":
            entry_row = auth.load_journal_entry(emb["entry_id"]) or {}
        else:
            entry_row = auth._memstore.get((emb.get("source") or "", emb["entry_id"])) or {}
        pred = _predictiveness_for(entry_row)
        scored.append((cos * pred, cos, pred, {**emb, **entry_row}))

    scored.sort(key=lambda t: t[0], reverse=True)
    out: List[RetrievedEntry] = []
    for composite, cos, pred, joined in scored[:k]:
        out.append(RetrievedEntry(
            entry_id=joined.get("entry_id") or joined.get("id") or "",
            body=joined.get("body") or "",
            source=joined.get("source") or "",
            cosine=cos, predictiveness=pred, score=composite,
        ))
    return out


# ---------------------------------------------------------------------------
# Auto-index hook — embed journal entries on save
# ---------------------------------------------------------------------------

def index_journal_entry(entry: Dict[str, Any]) -> None:
    """Compute + persist an embedding for a journal entry. Called from the
    runner's auto-draft hook (and could be called by /api/journal CRUD if
    we want sync indexing). Best-effort; failures swallowed.

    Drafts are skipped — until the user commits the entry it's not signal."""
    if not entry or entry.get("is_draft"):
        return
    try:
        upsert_embedding(
            entry_id=entry["id"],
            user_id=entry["user_id"],
            source="journal",
            text=entry.get("body") or "",
        )
    except Exception as exc:
        log.debug("memory_v2 index failed for %s: %s", entry.get("id"), exc)
