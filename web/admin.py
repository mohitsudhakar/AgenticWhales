"""Usage dashboard data assembly.

Pulls slim per-row data from Supabase (sessions, batches, profiles, users) via
the service-role helpers in web.auth, aggregates it in-process, and shapes the
result for the dashboard UI. Designed to stay correct even when Supabase
isn't configured — falls back to the in-memory store so local dev still
returns a sensible (empty / small) response.

Aggregation is in Python rather than SQL on purpose:
  * No new schema / migration required — works with the existing tables.
  * Volume is modest (a few thousand rows for a side project); a single
    PostgREST round-trip per table is fine.
If/when the dataset outgrows this, swap in a SQL view + GROUP BY query
behind the same module surface.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import auth

# Daily series window. Anything older is summed into the lifetime totals
# but not charted day-by-day. 30 fits cleanly on a desktop and is a sane
# default for "is the product growing".
DAILY_WINDOW_DAYS = 30


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # Postgres timestamps come back as "...+00:00" or "...Z" — normalise.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def _day_key(ts: Optional[str]) -> Optional[str]:
    dt = _parse_iso(ts)
    if not dt:
        return None
    return dt.astimezone(timezone.utc).date().isoformat()


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _empty_user_bucket(user_id: Optional[str]) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "email": None,
        "username": None,
        "tier": "novice",
        "created_at": None,
        "last_sign_in_at": None,
        "last_active": None,
        "analyses": 0,
        "batches": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_total": 0,
        "llm_calls": 0,
        "tool_calls": 0,
    }


def build_dashboard() -> Dict[str, Any]:
    sessions = auth.admin_list_sessions()
    batches = auth.admin_list_batches()
    profiles = auth.admin_list_profiles()
    users = auth.admin_list_users()

    by_user: Dict[str, Dict[str, Any]] = {}
    for u in users:
        uid = u.get("id")
        if not uid:
            continue
        bucket = _empty_user_bucket(uid)
        bucket["email"] = u.get("email")
        bucket["created_at"] = u.get("created_at")
        bucket["last_sign_in_at"] = u.get("last_sign_in_at")
        meta = u.get("user_metadata") or {}
        # Fallback display name from Google profile when no profile row exists.
        bucket["username"] = meta.get("full_name") or meta.get("name")
        by_user[uid] = bucket

    for p in profiles:
        pid = p.get("id")
        if not pid:
            continue
        bucket = by_user.setdefault(pid, _empty_user_bucket(pid))
        if p.get("username"):
            bucket["username"] = p["username"]
        if p.get("tier"):
            bucket["tier"] = p["tier"]
        if p.get("created_at") and not bucket["created_at"]:
            bucket["created_at"] = p["created_at"]

    # daily[day] = {date, active_users: set[uid], analyses, batches, tokens, tokens_in, tokens_out}
    daily: Dict[str, Dict[str, Any]] = {}

    def _day_bucket(day: str) -> Dict[str, Any]:
        return daily.setdefault(day, {
            "date": day,
            "active_users": set(),
            "analyses": 0,
            "batches": 0,
            "tokens": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "llm_calls": 0,
            "tool_calls": 0,
        })

    def _accumulate(row: Dict[str, Any], is_batch: bool) -> None:
        uid = row.get("user_id")
        ti = _i(row.get("tokens_in"))
        to_ = _i(row.get("tokens_out"))
        lc = _i(row.get("llm_calls"))
        tc = _i(row.get("tool_calls"))
        if uid:
            bucket = by_user.setdefault(uid, _empty_user_bucket(uid))
            if is_batch:
                bucket["batches"] += 1
            else:
                bucket["analyses"] += 1
            bucket["tokens_in"] += ti
            bucket["tokens_out"] += to_
            bucket["llm_calls"] += lc
            bucket["tool_calls"] += tc
            created = row.get("created_at")
            if created and (not bucket["last_active"] or created > bucket["last_active"]):
                bucket["last_active"] = created
        day = _day_key(row.get("created_at"))
        if day:
            d = _day_bucket(day)
            if is_batch:
                d["batches"] += 1
            else:
                d["analyses"] += 1
            d["tokens_in"] += ti
            d["tokens_out"] += to_
            d["tokens"] += ti + to_
            d["llm_calls"] += lc
            d["tool_calls"] += tc
            if uid:
                d["active_users"].add(uid)

    for s in sessions:
        _accumulate(s, is_batch=False)
    for b in batches:
        _accumulate(b, is_batch=True)

    for bucket in by_user.values():
        bucket["tokens_total"] = bucket["tokens_in"] + bucket["tokens_out"]

    per_user = sorted(
        by_user.values(),
        key=lambda u: (u["tokens_total"], u["analyses"] + u["batches"]),
        reverse=True,
    )

    # ---- Daily series: fill in zero rows for days with no activity so the
    # chart axis doesn't bunch up. Window ends today (UTC).
    today = datetime.now(timezone.utc).date()
    window_start = today - timedelta(days=DAILY_WINDOW_DAYS - 1)
    daily_series: List[Dict[str, Any]] = []
    for offset in range(DAILY_WINDOW_DAYS):
        day = (window_start + timedelta(days=offset)).isoformat()
        d = daily.get(day)
        daily_series.append({
            "date": day,
            "active_users": len(d["active_users"]) if d else 0,
            "analyses": d["analyses"] if d else 0,
            "batches": d["batches"] if d else 0,
            "tokens": d["tokens"] if d else 0,
            "tokens_in": d["tokens_in"] if d else 0,
            "tokens_out": d["tokens_out"] if d else 0,
            "llm_calls": d["llm_calls"] if d else 0,
            "tool_calls": d["tool_calls"] if d else 0,
        })

    today_iso = today.isoformat()
    week_start = today - timedelta(days=6)
    by_date = {d["date"]: d for d in daily_series}
    today_row = by_date.get(today_iso, {})

    last_7 = [d for d in daily_series if d["date"] >= week_start.isoformat()]
    dau_7d_avg = round(sum(d["active_users"] for d in last_7) / max(1, len(last_7)), 2)
    analyses_7d = sum(d["analyses"] for d in last_7)
    tokens_7d = sum(d["tokens"] for d in last_7)
    active_users_7d = len({
        uid
        for day in last_7
        if (d := daily.get(day["date"]))
        for uid in d["active_users"]
    })

    # Lifetime totals are summed across all sessions/batches, not just the
    # 30-day window — so the dashboard reflects historic usage too.
    overall = {
        "total_users": len(users),
        "users_with_activity": sum(1 for u in per_user if u["analyses"] + u["batches"] > 0),
        "total_analyses": sum(1 for s in sessions),
        "total_batches": sum(1 for _ in batches),
        "total_tokens_in": sum(u["tokens_in"] for u in per_user),
        "total_tokens_out": sum(u["tokens_out"] for u in per_user),
        "total_tokens": sum(u["tokens_total"] for u in per_user),
        "total_llm_calls": sum(u["llm_calls"] for u in per_user),
        "total_tool_calls": sum(u["tool_calls"] for u in per_user),
        "dau_today": today_row.get("active_users", 0),
        "dau_7d_avg": dau_7d_avg,
        "analyses_today": today_row.get("analyses", 0),
        "tokens_today": today_row.get("tokens", 0),
        "analyses_7d": analyses_7d,
        "tokens_7d": tokens_7d,
        "active_users_7d": active_users_7d,
    }

    return {
        "overall": overall,
        "per_user": per_user,
        "daily": daily_series,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "admin_email": auth.ADMIN_EMAIL,
        "supabase_configured": bool(auth._db_writable()),
    }
