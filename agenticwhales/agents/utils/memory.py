"""Append-only markdown decision log for AgenticWhales.

The main ``.md`` log preserves a stable, human-readable shape (see test
assertions in ``tests/test_memory_log.py``). A sidecar ``.meta.json`` next
to it tracks FinMem-style layered metadata per entry (layer / importance /
access count) without polluting the markdown. The sidecar degrades
gracefully — missing keys default to ``layer="shallow"``, ``importance=60``,
``access_count=0`` — so existing logs and tests that bypass the API by
writing directly to the file keep working unchanged.

Layered design follows Yu et al. (2023) §3.2:
- Shallow (Q = 14 days): per-decision entries — daily news / market signals
- Intermediate (Q = 90 days): periodic quarterly-cadence reflections
- Deep (Q = 365 days): extended (M-day retrospective) reflections + entries
  promoted from shallow via the access counter.
"""

import json
import math
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agenticwhales.agents.utils.rating import parse_rating


# ---------------------------------------------------------------------------
# Layered-memory constants (Yu et al. 2023, FinMem)
# ---------------------------------------------------------------------------

# Recency decay constants per layer (Eq. 2): S_recency = exp(-δ / Q_l).
# Mirrors the FinMem paper's shallow/intermediate/deep schedule.
_LAYER_DECAY_DAYS: Dict[str, int] = {
    "shallow": 14,
    "intermediate": 90,
    "deep": 365,
}

# Default importance values per layer at write time. The actual importance
# is later updated in proportion to |alpha return| when the outcome is
# resolved — big surprises (in either direction) are the most informative.
_LAYER_DEFAULT_IMPORTANCE: Dict[str, int] = {
    "shallow": 60,
    "intermediate": 70,
    "deep": 80,
}

# Access-counter promotion threshold (Yu et al. 2023, end of §3.2.2).
# When a shallow entry is retrieved this many times, it gets promoted to
# the deep layer with its importance bumped and its recency reset.
_ACCESS_PROMOTION_THRESHOLD = 3

# Sentinel ticker used for extended-reflection entries written by the
# periodic retrospective pass. Distinguished from real tickers so the
# scored-retrieval pass can present them in their own section.
_EXTENDED_TICKER = "_EXTENDED_"


def _tokenize(text: str) -> set:
    """Cheap relevance tokenization for Jaccard scoring.

    Drops short stop-like tokens (len <= 2) and lower-cases. Good enough
    for v1 — swap in real embeddings (e.g. text-embedding-004) later if
    Jaccard turns out to be the bottleneck.
    """
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class TradingMemoryLog:
    """Append-only markdown log of trading decisions and reflections."""

    # HTML comment: cannot appear in LLM prose output, safe as a hard delimiter
    _SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"
    # Precompiled patterns — avoids re-compilation on every load_entries() call
    _DECISION_RE = re.compile(r"DECISION:\n(.*?)(?=\nREFLECTION:|\Z)", re.DOTALL)
    _REFLECTION_RE = re.compile(r"REFLECTION:\n(.*?)$", re.DOTALL)

    def __init__(self, config: dict = None):
        cfg = config or {}
        self._log_path = None
        self._meta_path = None
        path = cfg.get("memory_log_path")
        if path:
            self._log_path = Path(path).expanduser()
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            # Sidecar metadata file sits next to the main log
            self._meta_path = self._log_path.with_suffix(self._log_path.suffix + ".meta.json")
        # Optional cap on resolved entries. None disables rotation.
        self._max_entries = cfg.get("memory_log_max_entries")
        # Layered-memory sidecar state (lazy-loaded; survives across runs).
        self._meta = self._load_meta()

    # --- Sidecar metadata (Phase B: layered memory) ---

    def _load_meta(self) -> dict:
        """Read the layered-memory sidecar, with a safe default schema."""
        empty = {
            "version": 1,
            "entries": {},
            "last_extended_reflection_date": None,
            "promotions": [],
        }
        if not self._meta_path or not self._meta_path.exists():
            return empty
        try:
            data = json.loads(self._meta_path.read_text(encoding="utf-8"))
            # Be tolerant of partial / older sidecars.
            for k, v in empty.items():
                data.setdefault(k, v)
            return data
        except (OSError, json.JSONDecodeError):
            return empty

    def _save_meta(self) -> None:
        """Atomic write of the sidecar to avoid mid-write corruption."""
        if not self._meta_path:
            return
        tmp = self._meta_path.with_suffix(self._meta_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._meta_path)

    @staticmethod
    def _entry_key(trade_date: str, ticker: str) -> str:
        return f"{trade_date}|{ticker}"

    def _entry_meta(self, trade_date: str, ticker: str) -> dict:
        """Return the metadata dict for an entry, or a sensible default."""
        return self._meta["entries"].get(
            self._entry_key(trade_date, ticker),
            {
                "layer": "shallow",
                "importance": _LAYER_DEFAULT_IMPORTANCE["shallow"],
                "access_count": 0,
                "boosted_date": None,
            },
        )

    def _set_entry_meta(self, trade_date: str, ticker: str, **fields) -> None:
        key = self._entry_key(trade_date, ticker)
        existing = self._meta["entries"].get(key, {
            "layer": "shallow",
            "importance": _LAYER_DEFAULT_IMPORTANCE["shallow"],
            "access_count": 0,
            "boosted_date": None,
        })
        existing.update(fields)
        self._meta["entries"][key] = existing
        self._save_meta()

    def _record_access(self, trade_date: str, ticker: str) -> bool:
        """Bump the access counter and promote shallow→deep at threshold.

        Returns True when a promotion was triggered. Yu et al. (2023) §3.2.2:
        frequently-retrieved memories should ascend to deeper layers for
        longer retention. We promote with importance += 5 and a recency
        reset (``boosted_date`` is honoured by ``_recency_score`` below).
        """
        meta = dict(self._entry_meta(trade_date, ticker))
        meta["access_count"] = meta.get("access_count", 0) + 1
        promoted = False
        if (
            meta.get("layer") == "shallow"
            and meta["access_count"] >= _ACCESS_PROMOTION_THRESHOLD
        ):
            meta["layer"] = "deep"
            meta["importance"] = min(80, meta.get("importance", 60) + 5)
            meta["boosted_date"] = datetime.utcnow().strftime("%Y-%m-%d")
            self._meta.setdefault("promotions", []).append({
                "key": self._entry_key(trade_date, ticker),
                "new_layer": "deep",
                "date": meta["boosted_date"],
            })
            promoted = True
        self._meta["entries"][self._entry_key(trade_date, ticker)] = meta
        self._save_meta()
        return promoted

    # --- Write path (Phase A) ---

    def store_decision(
        self,
        ticker: str,
        trade_date: str,
        final_trade_decision: str,
    ) -> None:
        """Append pending entry at end of propagate(). No LLM call."""
        if not self._log_path:
            return
        # Idempotency guard: fast raw-text scan instead of full parse
        if self._log_path.exists():
            raw = self._log_path.read_text(encoding="utf-8")
            for line in raw.splitlines():
                if line.startswith(f"[{trade_date} | {ticker} |") and line.endswith("| pending]"):
                    return
        rating = parse_rating(final_trade_decision)
        tag = f"[{trade_date} | {ticker} | {rating} | pending]"
        entry = f"{tag}\n\nDECISION:\n{final_trade_decision}{self._SEPARATOR}"
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(entry)
        # Initialize sidecar metadata: all decision entries start as shallow.
        # Importance gets refined later when the outcome is resolved.
        self._set_entry_meta(
            trade_date, ticker,
            layer="shallow",
            importance=_LAYER_DEFAULT_IMPORTANCE["shallow"],
            access_count=0,
            boosted_date=None,
        )

    # --- Read path (Phase A) ---

    def load_entries(self) -> List[dict]:
        """Parse all entries from log. Returns list of dicts."""
        if not self._log_path or not self._log_path.exists():
            return []
        text = self._log_path.read_text(encoding="utf-8")
        raw_entries = [e.strip() for e in text.split(self._SEPARATOR) if e.strip()]
        entries = []
        for raw in raw_entries:
            parsed = self._parse_entry(raw)
            if parsed:
                entries.append(parsed)
        return entries

    def get_pending_entries(self) -> List[dict]:
        """Return entries with outcome:pending (for Phase B)."""
        return [e for e in self.load_entries() if e.get("pending")]

    def get_recent_performance_block(
        self, ticker: str, lookback: int = 3
    ) -> str:
        """Pre-formatted block summarising recent same-ticker outcomes.

        Used by the Portfolio Manager for self-adaptive risk (FinMem-style
        character module — Yu et al. 2023, Section 3.1). When recent alpha
        is negative the synthesizer is nudged toward capital preservation;
        when positive, toward higher conviction. Returns an empty string
        when no resolved history exists, which collapses cleanly out of the
        prompt.
        """
        entries = [
            e for e in self.load_entries()
            if not e.get("pending") and e["ticker"] == ticker and e.get("alpha")
        ]
        if not entries:
            return ""
        recent = entries[-lookback:]
        total_alpha = 0.0
        total_raw = 0.0
        valid = 0
        for e in recent:
            try:
                a = float(str(e.get("alpha") or "").rstrip("%"))
                r = float(str(e.get("raw") or "").rstrip("%"))
                total_alpha += a
                total_raw += r
                valid += 1
            except (ValueError, AttributeError):
                continue
        if valid == 0:
            return ""
        return (
            f"RECENT PERFORMANCE ({valid} most recent trade{'s' if valid != 1 else ''} on "
            f"{ticker}): cumulative raw return {total_raw:+.1f}%, "
            f"cumulative alpha vs SPY {total_alpha:+.1f}%."
        )

    def get_past_context(self, ticker: str, n_same: int = 5, n_cross: int = 3) -> str:
        """Return formatted past context string for agent prompt injection."""
        entries = [e for e in self.load_entries() if not e.get("pending")]
        if not entries:
            return ""

        same, cross = [], []
        for e in reversed(entries):
            if len(same) >= n_same and len(cross) >= n_cross:
                break
            if e["ticker"] == ticker and len(same) < n_same:
                same.append(e)
            elif e["ticker"] != ticker and len(cross) < n_cross:
                cross.append(e)

        if not same and not cross:
            return ""

        parts = []
        if same:
            parts.append(f"Past analyses of {ticker} (most recent first):")
            parts.extend(self._format_full(e) for e in same)
        if cross:
            parts.append("Recent cross-ticker lessons:")
            parts.extend(self._format_reflection_only(e) for e in cross)
        return "\n\n".join(parts)

    # --- Update path (Phase B) ---

    def update_with_outcome(
        self,
        ticker: str,
        trade_date: str,
        raw_return: float,
        alpha_return: float,
        holding_days: int,
        reflection: str,
    ) -> None:
        """Replace pending tag and append REFLECTION section using atomic write.

        Finds the first pending entry matching (trade_date, ticker), updates
        its tag with return figures, and appends a REFLECTION section.  Uses
        a temp-file + os.replace() so a crash mid-write never corrupts the log.
        """
        if not self._log_path or not self._log_path.exists():
            return

        text = self._log_path.read_text(encoding="utf-8")
        blocks = text.split(self._SEPARATOR)

        pending_prefix = f"[{trade_date} | {ticker} |"
        raw_pct = f"{raw_return:+.1%}"
        alpha_pct = f"{alpha_return:+.1%}"

        updated = False
        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                new_blocks.append(block)
                continue

            lines = stripped.splitlines()
            tag_line = lines[0].strip()

            if (
                not updated
                and tag_line.startswith(pending_prefix)
                and tag_line.endswith("| pending]")
            ):
                # Parse rating from the existing pending tag
                fields = [f.strip() for f in tag_line[1:-1].split("|")]
                rating = fields[2]
                new_tag = (
                    f"[{trade_date} | {ticker} | {rating}"
                    f" | {raw_pct} | {alpha_pct} | {holding_days}d]"
                )
                rest = "\n".join(lines[1:])
                new_blocks.append(
                    f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{reflection}"
                )
                updated = True
            else:
                new_blocks.append(block)

        if not updated:
            return

        new_blocks = self._apply_rotation(new_blocks)
        new_text = self._SEPARATOR.join(new_blocks)
        tmp_path = self._log_path.with_suffix(".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._log_path)
        # Refine importance based on outcome magnitude. FinMem-style: big
        # alpha surprises (positive or negative) are the most informative
        # memories — they're the ones we want to weight heavily on retrieval.
        importance = self._importance_from_outcome(alpha_return)
        self._set_entry_meta(trade_date, ticker, importance=importance)

    def batch_update_with_outcomes(self, updates: List[dict]) -> None:
        """Apply multiple outcome updates in a single read + atomic write.

        Each element of updates must have keys: ticker, trade_date,
        raw_return, alpha_return, holding_days, reflection.
        """
        if not self._log_path or not self._log_path.exists() or not updates:
            return

        text = self._log_path.read_text(encoding="utf-8")
        blocks = text.split(self._SEPARATOR)

        # Build lookup keyed by (trade_date, ticker) for O(1) dispatch
        update_map = {(u["trade_date"], u["ticker"]): u for u in updates}

        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                new_blocks.append(block)
                continue

            lines = stripped.splitlines()
            tag_line = lines[0].strip()

            matched = False
            for (trade_date, ticker), upd in list(update_map.items()):
                pending_prefix = f"[{trade_date} | {ticker} |"
                if tag_line.startswith(pending_prefix) and tag_line.endswith("| pending]"):
                    fields = [f.strip() for f in tag_line[1:-1].split("|")]
                    rating = fields[2]
                    raw_pct = f"{upd['raw_return']:+.1%}"
                    alpha_pct = f"{upd['alpha_return']:+.1%}"
                    new_tag = (
                        f"[{trade_date} | {ticker} | {rating}"
                        f" | {raw_pct} | {alpha_pct} | {upd['holding_days']}d]"
                    )
                    rest = "\n".join(lines[1:])
                    new_blocks.append(
                        f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{upd['reflection']}"
                    )
                    del update_map[(trade_date, ticker)]
                    matched = True
                    break

            if not matched:
                new_blocks.append(block)

        new_blocks = self._apply_rotation(new_blocks)
        new_text = self._SEPARATOR.join(new_blocks)
        tmp_path = self._log_path.with_suffix(".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._log_path)
        # Refine importance for each resolved entry.
        for u in updates:
            self._set_entry_meta(
                u["trade_date"], u["ticker"],
                importance=self._importance_from_outcome(u["alpha_return"]),
            )

    # --- Scored retrieval (Phase B: layered memory) ---

    @staticmethod
    def _importance_from_outcome(alpha_return: float) -> int:
        """Map |alpha| → importance ∈ [40, 80].

        Yu et al. (2023) Eq. 4 samples importance discretely from {40,60,80}
        per layer. We use the same range but derive it from realized
        |alpha|: a flat / no-edge call scores 40, a 10%+ alpha call scores
        80. The premise is that *informative* memories — surprise wins or
        surprise losses — should outweigh routine no-ops on retrieval.
        """
        magnitude = abs(float(alpha_return))
        scaled = 40 + min(40, int(round(magnitude * 400)))  # 0% → 40, 10% → 80
        return max(40, min(80, scaled))

    @staticmethod
    def _recency_score(entry_date: str, layer: str, boosted_date: Optional[str]) -> float:
        """Eq. 2: S_recency = exp(-δ / Q_l).

        If the entry has been promoted (boosted_date is set), δ is measured
        from boosted_date rather than from the original entry_date — this
        is FinMem's "recency reset on promotion" mechanism.
        """
        anchor = boosted_date or entry_date
        try:
            anchor_dt = datetime.strptime(anchor, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return 0.0
        delta_days = max(0, (datetime.utcnow().date() - anchor_dt).days)
        q = _LAYER_DECAY_DAYS.get(layer, _LAYER_DECAY_DAYS["shallow"])
        return math.exp(-delta_days / q)

    def get_scored_context(
        self,
        ticker: str,
        top_k_per_layer: int = 5,
        current_summary: str = "",
    ) -> str:
        """FinMem-style scored retrieval (Yu et al. 2023, §3.2.2 + Eqs. 1-6).

        Returns a formatted string with up to ``top_k_per_layer`` entries
        per layer, ranked by γ = S_recency + S_relevancy + S_importance.

        - Recency: Ebbinghaus decay with layer-specific Q (14 / 90 / 365 days).
        - Relevancy: Jaccard token overlap (cheap, no embedding dep). Same-
          ticker entries get a +0.5 relevance boost so they surface even
          when token overlap with ``current_summary`` is sparse.
        - Importance: realized |alpha|-derived score, decayed by recency.

        Side effect: each returned entry's access_count is bumped, which
        can promote shallow → deep when the threshold is crossed.

        Empty string when no resolved entries exist (lets the prompt
        gracefully omit the section).
        """
        entries = [e for e in self.load_entries() if not e.get("pending")]
        if not entries:
            return ""

        query_text = f"{ticker} {current_summary}"
        query_tokens = _tokenize(query_text)

        by_layer: Dict[str, List[Tuple[float, dict, dict]]] = defaultdict(list)
        for e in entries:
            meta = self._entry_meta(e["date"], e["ticker"])
            layer = meta.get("layer", "shallow")
            importance = meta.get("importance", _LAYER_DEFAULT_IMPORTANCE[layer])
            boosted = meta.get("boosted_date")

            s_recency = self._recency_score(e["date"], layer, boosted)
            entry_text = f"{e.get('decision', '')} {e.get('reflection', '')}"
            s_relevance = _jaccard(query_tokens, _tokenize(entry_text))
            if e["ticker"] == ticker:
                s_relevance += 0.5  # same-ticker affinity
            # Normalise importance to 0..1 range so the three terms are
            # comparable. (Could be tuned per FinMem's Conformity Coefficient α.)
            s_importance = (importance / 100.0) * s_recency

            score = s_recency + s_relevance + s_importance
            by_layer[layer].append((score, e, meta))

        sections: List[str] = []
        for layer in ("shallow", "intermediate", "deep"):
            items = by_layer.get(layer) or []
            if not items:
                continue
            items.sort(key=lambda x: -x[0])
            top = items[:top_k_per_layer]

            sections.append(f"--- {layer.upper()} MEMORY LAYER (top {len(top)}) ---")
            for score, e, _meta in top:
                if e["ticker"] == _EXTENDED_TICKER:
                    sections.append(self._format_extended(e))
                elif e["ticker"] == ticker:
                    sections.append(self._format_full(e))
                else:
                    sections.append(self._format_reflection_only(e))
                # Record retrieval — may promote shallow → deep over time.
                self._record_access(e["date"], e["ticker"])

        return "\n\n".join(sections)

    # --- Extended reflection (Phase B.10) ---

    def store_extended_reflection(self, trade_date: str, content: str) -> None:
        """Append an extended (M-day retrospective) reflection as a deep entry.

        Uses a resolved-shape tag with the sentinel ticker ``_EXTENDED_``
        and rating ``extended`` so the existing parser handles it without
        modification. The sidecar metadata marks it as a deep-layer entry
        with high baseline importance.
        """
        if not self._log_path:
            return
        ticker = _EXTENDED_TICKER
        # Idempotency: skip if an extended entry for this date already exists.
        if self._log_path.exists():
            for line in self._log_path.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"[{trade_date} | {ticker} |"):
                    return
        tag = f"[{trade_date} | {ticker} | extended | n/a | n/a | n/a]"
        entry = (
            f"{tag}\n\n"
            f"DECISION:\n(M-day retrospective synthesis)\n\n"
            f"REFLECTION:\n{content}{self._SEPARATOR}"
        )
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(entry)

        self._set_entry_meta(
            trade_date, ticker,
            layer="deep",
            importance=_LAYER_DEFAULT_IMPORTANCE["deep"],
            access_count=0,
            boosted_date=None,
        )
        self._meta["last_extended_reflection_date"] = trade_date
        self._save_meta()

    def days_since_last_extended_reflection(self) -> Optional[int]:
        """Days since the last extended-reflection entry, or None if never run."""
        last = self._meta.get("last_extended_reflection_date")
        if not last:
            return None
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None
        return (datetime.utcnow().date() - last_dt).days

    def _format_extended(self, e: dict) -> str:
        tag = f"[{e['date']} | EXTENDED-REFLECTION]"
        return f"{tag}\n{e.get('reflection', '')}"

    # --- Helpers ---

    def _apply_rotation(self, blocks: List[str]) -> List[str]:
        """Drop oldest resolved blocks when their count exceeds max_entries.

        Pending blocks are always kept (they represent unprocessed work).
        Returns ``blocks`` unchanged when rotation is disabled or under cap.
        """
        if not self._max_entries or self._max_entries <= 0:
            return blocks

        # Tag each block with (kept, is_resolved) by parsing tag-line markers.
        # Extended-reflection entries (sentinel ticker _EXTENDED_) are
        # treated as non-rotatable — they are deep-layer summaries we want
        # to retain across the cap.
        decisions = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                decisions.append((block, False))
                continue
            tag_line = stripped.splitlines()[0].strip()
            is_resolved = (
                tag_line.startswith("[")
                and tag_line.endswith("]")
                and not tag_line.endswith("| pending]")
                and f"| {_EXTENDED_TICKER} |" not in tag_line
            )
            decisions.append((block, is_resolved))

        resolved_count = sum(1 for _, r in decisions if r)
        if resolved_count <= self._max_entries:
            return blocks

        to_drop = resolved_count - self._max_entries
        kept: List[str] = []
        for block, is_resolved in decisions:
            if is_resolved and to_drop > 0:
                to_drop -= 1
                continue
            kept.append(block)
        return kept

    def _parse_entry(self, raw: str) -> Optional[dict]:
        lines = raw.strip().splitlines()
        if not lines:
            return None
        tag_line = lines[0].strip()
        if not (tag_line.startswith("[") and tag_line.endswith("]")):
            return None
        fields = [f.strip() for f in tag_line[1:-1].split("|")]
        if len(fields) < 4:
            return None
        entry = {
            "date": fields[0],
            "ticker": fields[1],
            "rating": fields[2],
            "pending": fields[3] == "pending",
            "raw": fields[3] if fields[3] != "pending" else None,
            "alpha": fields[4] if len(fields) > 4 else None,
            "holding": fields[5] if len(fields) > 5 else None,
        }
        body = "\n".join(lines[1:]).strip()
        decision_match = self._DECISION_RE.search(body)
        reflection_match = self._REFLECTION_RE.search(body)
        entry["decision"] = decision_match.group(1).strip() if decision_match else ""
        entry["reflection"] = reflection_match.group(1).strip() if reflection_match else ""
        return entry

    def _format_full(self, e: dict) -> str:
        raw = e["raw"] or "n/a"
        alpha = e["alpha"] or "n/a"
        holding = e["holding"] or "n/a"
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {raw} | {alpha} | {holding}]"
        parts = [tag, f"DECISION:\n{e['decision']}"]
        if e["reflection"]:
            parts.append(f"REFLECTION:\n{e['reflection']}")
        return "\n\n".join(parts)

    def _format_reflection_only(self, e: dict) -> str:
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {e['raw'] or 'n/a'}]"
        if e["reflection"]:
            return f"{tag}\n{e['reflection']}"
        text = e["decision"][:300]
        suffix = "..." if len(e["decision"]) > 300 else ""
        return f"{tag}\n{text}{suffix}"
