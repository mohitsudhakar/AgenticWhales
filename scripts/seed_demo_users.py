"""Seed 10k demo users + plausible sessions/usage into Supabase.

Built for a VC demo — populates `profiles`, `sessions`, and `usage_daily`
so the dashboard looks like a product at scale. All `created_at` values
land in the last 30 days, weighted slightly toward recent days so the
signup chart bends upward like a growing product.

Requires:
    AGENTICWHALES_SUPABASE_URL
    AGENTICWHALES_SUPABASE_SERVICE_KEY   (service_role; bypasses RLS)

Usage:
    python scripts/seed_demo_users.py                    # 10k users
    python scripts/seed_demo_users.py --users 1000       # fewer users
    python scripts/seed_demo_users.py --dry-run          # preview only
    python scripts/seed_demo_users.py --skip-auth        # skip Auth user creation
    python scripts/seed_demo_users.py --purge            # delete prior demo users

Each demo user is tagged via user_metadata.demo=true so --purge can find
them later without touching real users.
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


# ------------------------------------------------------------------
# config
# ------------------------------------------------------------------

DEMO_EMAIL_DOMAIN = "demo.agenticwhales.local"
DEMO_TAG = "demo_seed_v1"

TIERS = ["novice", "intermediate", "master"]
TIER_WEIGHTS = [0.65, 0.28, 0.07]

# Activity cohorts — what kind of user behaviour to model.
#   weight: share of population
#   sessions_range: (min, max) sessions in the 30-day window
#   tokens_per_session: (min, max) total tokens per session
#   llm_calls_range / tool_calls_range: per-session call counts
COHORTS: List[Dict[str, Any]] = [
    {
        "name": "power",
        "weight": 0.06,
        "sessions_range": (25, 90),
        "tokens_per_session": (180_000, 950_000),
        "llm_calls_range": (180, 520),
        "tool_calls_range": (90, 320),
        "tier_bias": "master",
    },
    {
        "name": "regular",
        "weight": 0.22,
        "sessions_range": (5, 22),
        "tokens_per_session": (45_000, 320_000),
        "llm_calls_range": (50, 180),
        "tool_calls_range": (20, 90),
        "tier_bias": "intermediate",
    },
    {
        "name": "occasional",
        "weight": 0.48,
        "sessions_range": (1, 4),
        "tokens_per_session": (8_000, 80_000),
        "llm_calls_range": (15, 60),
        "tool_calls_range": (4, 25),
        "tier_bias": None,
    },
    {
        "name": "signup_only",
        "weight": 0.24,
        "sessions_range": (0, 0),
        "tokens_per_session": (0, 0),
        "llm_calls_range": (0, 0),
        "tool_calls_range": (0, 0),
        "tier_bias": None,
    },
]

TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "AVGO",
    "JPM", "BAC", "WFC", "GS", "MS", "C",
    "XOM", "CVX", "COP",
    "JNJ", "PFE", "MRK", "LLY", "ABBV",
    "WMT", "TGT", "COST", "HD", "LOW",
    "DIS", "NFLX", "SPOT", "UBER", "ABNB",
    "BA", "CAT", "GE", "DE",
    "KO", "PEP", "MCD", "SBUX",
    "BTC-USD", "ETH-USD", "SPY", "QQQ", "IWM",
    "PLTR", "SHOP", "SQ", "COIN", "RBLX",
]

QUICK_MODELS = [
    "gpt-4o-mini", "gpt-4.1-mini", "claude-haiku-4.5",
    "gemini-2.0-flash", "deepseek-chat",
]
DEEP_MODELS = [
    "gpt-4o", "gpt-4.1", "claude-opus-4.7", "claude-sonnet-4.6",
    "gemini-2.5-pro", "deepseek-reasoner",
]
STATUSES = ["completed"] * 18 + ["failed"] * 1 + ["running"] * 1

FIRST_NAMES = [
    "alex", "jordan", "sam", "taylor", "morgan", "casey", "riley", "drew",
    "blake", "cameron", "jamie", "robin", "kai", "noah", "emma", "liam",
    "olivia", "lucas", "ava", "ethan", "sophia", "mason", "isabella", "logan",
    "mia", "elijah", "amelia", "james", "harper", "benjamin", "evelyn", "henry",
    "wei", "yuki", "ravi", "priya", "diego", "sofia", "matteo", "chloe",
    "arjun", "ananya", "hiro", "luca", "leila", "omar", "fatima", "kenji",
]
LAST_NAMES = [
    "smith", "jones", "garcia", "miller", "davis", "rodriguez", "martinez",
    "hernandez", "lopez", "gonzalez", "wilson", "anderson", "thomas", "taylor",
    "moore", "jackson", "martin", "lee", "perez", "thompson", "white", "harris",
    "clark", "lewis", "walker", "hall", "allen", "young", "king", "wright",
    "chen", "kumar", "patel", "khan", "nguyen", "kim", "park", "singh",
    "tanaka", "santos", "rossi", "schmidt", "ivanov", "okafor",
]


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        print(f"error: missing required env var {name}", file=sys.stderr)
        sys.exit(2)
    return val


def _service_headers(supabase_key: str, prefer: Optional[str] = None) -> Dict[str, str]:
    h = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _weighted_choice(weights: List[float]) -> int:
    r = random.random()
    cum = 0.0
    for i, w in enumerate(weights):
        cum += w
        if r <= cum:
            return i
    return len(weights) - 1


def _pick_cohort() -> Dict[str, Any]:
    weights = [c["weight"] for c in COHORTS]
    return COHORTS[_weighted_choice(weights)]


def _pick_tier(bias: Optional[str]) -> str:
    if bias and random.random() < 0.55:
        return bias
    return TIERS[_weighted_choice(TIER_WEIGHTS)]


def _gen_username(used: set[str]) -> str:
    for _ in range(20):
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        suffix = random.choice(["", str(random.randint(1, 999))])
        candidate = f"{first}_{last}{suffix}" if suffix else f"{first}_{last}"
        if candidate not in used:
            used.add(candidate)
            return candidate
    # last resort: append entropy
    candidate = f"trader_{''.join(random.choices(string.ascii_lowercase + string.digits, k=8))}"
    used.add(candidate)
    return candidate


def _gen_email(username: str, uid_short: str) -> str:
    return f"{username}.{uid_short}@{DEMO_EMAIL_DOMAIN}"


def _signup_offset_seconds(window_days: int) -> float:
    """Weight signups toward recent days (j-curve). Returns seconds-back in
    [0, window_days*86400] so the result, subtracted from `now`, stays strictly
    inside the window."""
    u = random.random()
    return window_days * 86400.0 * (1.0 - u ** 1.6)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ------------------------------------------------------------------
# user creation (Auth admin API)
# ------------------------------------------------------------------

def _create_auth_user(
    session: requests.Session,
    base_url: str,
    key: str,
    email: str,
    username: str,
) -> Optional[str]:
    """Create one auth.users entry. Returns the UUID, or None on failure."""
    payload = {
        "email": email,
        "password": uuid.uuid4().hex + "Aa1!",
        "email_confirm": True,
        "user_metadata": {
            "demo": True,
            "demo_tag": DEMO_TAG,
            "username": username,
        },
    }
    try:
        resp = session.post(
            f"{base_url}/auth/v1/admin/users",
            headers=_service_headers(key),
            json=payload,
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! admin create {email}: {e}", file=sys.stderr)
        return None
    if resp.status_code >= 300:
        # 422 typically = email already taken (re-running the script).
        if resp.status_code != 422:
            print(f"  ! admin create {email} -> {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return None
    return (resp.json() or {}).get("id")


def _create_users_parallel(
    base_url: str,
    key: str,
    user_specs: List[Dict[str, Any]],
    workers: int,
) -> List[Dict[str, Any]]:
    """Create auth.users in parallel. Mutates each spec with `user_id` on success.
    Returns only the specs that succeeded."""
    print(f"  creating {len(user_specs)} auth users with {workers} workers...")
    succeeded: List[Dict[str, Any]] = []
    started = time.time()
    last_log = started

    # One Session per worker for connection reuse.
    sessions = [requests.Session() for _ in range(workers)]

    def _do(idx_spec: Tuple[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        idx, spec = idx_spec
        sess = sessions[idx % workers]
        uid = _create_auth_user(sess, base_url, key, spec["email"], spec["username"])
        if uid:
            spec["user_id"] = uid
            return spec
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_do, (i, s)) for i, s in enumerate(user_specs)]
        done = 0
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None:
                succeeded.append(res)
            done += 1
            now = time.time()
            if now - last_log >= 5.0 or done == len(futures):
                rate = done / max(now - started, 0.001)
                print(f"    {done}/{len(futures)} ({rate:.0f}/s, {len(succeeded)} ok)")
                last_log = now

    return succeeded


# ------------------------------------------------------------------
# REST batch inserts
# ------------------------------------------------------------------

def _bulk_insert(
    session: requests.Session,
    base_url: str,
    key: str,
    table: str,
    rows: List[Dict[str, Any]],
    batch_size: int = 500,
    on_conflict: Optional[str] = None,
) -> int:
    """POST rows in chunks to PostgREST. Returns the count successfully sent."""
    if not rows:
        return 0
    inserted = 0
    prefer = "return=minimal"
    if on_conflict:
        prefer = f"resolution=merge-duplicates,{prefer}"
    url = f"{base_url}/rest/v1/{table}"
    if on_conflict:
        url = f"{url}?on_conflict={on_conflict}"
    headers = _service_headers(key, prefer)
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        try:
            resp = session.post(url, headers=headers, json=chunk, timeout=60)
        except requests.RequestException as e:
            print(f"  ! {table} batch {i//batch_size}: {e}", file=sys.stderr)
            continue
        if resp.status_code >= 300:
            print(
                f"  ! {table} batch {i//batch_size} -> {resp.status_code}: {resp.text[:300]}",
                file=sys.stderr,
            )
            continue
        inserted += len(chunk)
    return inserted


# ------------------------------------------------------------------
# data generation
# ------------------------------------------------------------------

def _build_user_specs(n: int, window_days: int, now: datetime) -> List[Dict[str, Any]]:
    used_usernames: set[str] = set()
    specs: List[Dict[str, Any]] = []
    for _ in range(n):
        username = _gen_username(used_usernames)
        uid_short = uuid.uuid4().hex[:8]
        cohort = _pick_cohort()
        tier = _pick_tier(cohort["tier_bias"])
        # Subtract seconds (not days) so the random fractional part already
        # gives a realistic spread of hours/minutes within the window.
        signup_dt = now - timedelta(seconds=_signup_offset_seconds(window_days))
        specs.append(
            {
                "username": username,
                "email": _gen_email(username, uid_short),
                "tier": tier,
                "cohort": cohort["name"],
                "cohort_def": cohort,
                "signup_at": signup_dt,
            }
        )
    return specs


def _build_sessions_for_user(spec: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    cohort = spec["cohort_def"]
    n_sessions = random.randint(*cohort["sessions_range"])
    if n_sessions == 0:
        return []
    signup_at: datetime = spec["signup_at"]
    user_window_seconds = max(int((now - signup_at).total_seconds()) - 60, 60)
    sessions: List[Dict[str, Any]] = []
    for _ in range(n_sessions):
        offset_seconds = random.randint(60, user_window_seconds)
        created_at = signup_at + timedelta(seconds=offset_seconds)
        duration_seconds = random.randint(45, 900)
        completed_at = created_at + timedelta(seconds=duration_seconds)
        if completed_at > now:
            completed_at = now

        status = random.choice(STATUSES)
        tokens_total = random.randint(*cohort["tokens_per_session"])
        # Inputs dominate (system prompts, tool results). 80/20 split-ish.
        tokens_in = int(tokens_total * random.uniform(0.72, 0.88))
        tokens_out = tokens_total - tokens_in
        llm_calls = random.randint(*cohort["llm_calls_range"])
        tool_calls = random.randint(*cohort["tool_calls_range"])

        if status != "completed":
            # Trim usage for runs that never finished.
            tokens_in = int(tokens_in * random.uniform(0.1, 0.6))
            tokens_out = int(tokens_out * random.uniform(0.1, 0.6))
            llm_calls = int(llm_calls * random.uniform(0.1, 0.6))
            tool_calls = int(tool_calls * random.uniform(0.1, 0.6))

        ticker = random.choice(TICKERS)
        analysis_date = (created_at - timedelta(days=random.randint(0, 2))).strftime("%Y-%m-%d")
        quick = random.choice(QUICK_MODELS)
        deep = random.choice(DEEP_MODELS)
        sid = f"sess-{uuid.uuid4().hex[:16]}"
        sessions.append(
            {
                "id": sid,
                "user_id": spec["user_id"],
                "ticker": ticker,
                "analysis_date": analysis_date,
                "status": status,
                "created_at": _iso(created_at),
                "completed_at": _iso(completed_at) if status == "completed" else None,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "llm_calls": llm_calls,
                "tool_calls": tool_calls,
                "quick_model": quick,
                "deep_model": deep,
                "data": {
                    "id": sid,
                    "user_id": spec["user_id"],
                    "ticker": ticker,
                    "analysis_date": analysis_date,
                    "status": status,
                    "demo_tag": DEMO_TAG,
                    "config": {"quick_think_llm": quick, "deep_think_llm": deep},
                    "stats": {
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "llm_calls": llm_calls,
                        "tool_calls": tool_calls,
                    },
                },
            }
        )
    return sessions


def _aggregate_usage_daily(sessions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Roll session counts up into the per-(user, day) shape `usage_daily` expects."""
    counts: Dict[Tuple[str, str], int] = {}
    for s in sessions:
        # day key = UTC date of created_at, matching increment_usage()'s logic.
        day = s["created_at"][:10]
        key = (s["user_id"], day)
        counts[key] = counts.get(key, 0) + 1
    rows = []
    for (uid, day), count in counts.items():
        rows.append(
            {
                "user_id": uid,
                "day": day,
                "count": count,
                "updated_at": f"{day}T23:59:00+00:00",
            }
        )
    return rows


# ------------------------------------------------------------------
# purge (cleanup of prior demo data)
# ------------------------------------------------------------------

def _purge(base_url: str, key: str) -> None:
    """Delete every auth user tagged demo_tag=DEMO_TAG. profiles/sessions/usage_daily
    cascade automatically because of the FK to auth.users."""
    print(f"purging users tagged demo_tag={DEMO_TAG}...")
    session = requests.Session()
    page = 1
    per_page = 200
    total_deleted = 0
    while True:
        try:
            resp = session.get(
                f"{base_url}/auth/v1/admin/users?per_page={per_page}&page={page}",
                headers=_service_headers(key),
                timeout=30,
            )
        except requests.RequestException as e:
            print(f"  ! list page {page}: {e}", file=sys.stderr)
            break
        if resp.status_code >= 300:
            print(f"  ! list page {page} -> {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            break
        users = (resp.json() or {}).get("users", [])
        if not users:
            break
        demo_users = [
            u for u in users
            if (u.get("user_metadata") or {}).get("demo_tag") == DEMO_TAG
        ]
        print(f"  page {page}: {len(users)} total, {len(demo_users)} demo")
        for u in demo_users:
            try:
                d = session.delete(
                    f"{base_url}/auth/v1/admin/users/{u['id']}",
                    headers=_service_headers(key),
                    timeout=15,
                )
                if d.status_code < 300:
                    total_deleted += 1
            except requests.RequestException as e:
                print(f"  ! delete {u['id']}: {e}", file=sys.stderr)
        if len(users) < per_page:
            break
        page += 1
    print(f"purged {total_deleted} demo users (profiles/sessions cascaded).")


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--users", type=int, default=10_000, help="number of users to create")
    p.add_argument("--window-days", type=int, default=30, help="days back to spread signups")
    p.add_argument("--workers", type=int, default=20, help="parallel admin-API workers")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")
    p.add_argument("--dry-run", action="store_true", help="generate data but skip API calls")
    p.add_argument("--skip-auth", action="store_true",
                   help="skip auth.users creation (re-seed sessions only — implies pre-existing user IDs aren't available; mostly useful for dev)")
    p.add_argument("--purge", action="store_true",
                   help="delete all users tagged demo_seed_v1 and exit")
    args = p.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    base_url = _env("AGENTICWHALES_SUPABASE_URL").rstrip("/")
    service_key = _env("AGENTICWHALES_SUPABASE_SERVICE_KEY")

    if args.purge:
        _purge(base_url, service_key)
        return 0

    now = datetime.now(tz=timezone.utc)
    print(f"seeding {args.users} users over {args.window_days} days ending {now.isoformat()}")
    print(f"target: {base_url}")
    if args.dry_run:
        print("DRY-RUN: no API calls will be made.")

    # 1. Build user specs (no API yet).
    specs = _build_user_specs(args.users, args.window_days, now)
    cohort_counts: Dict[str, int] = {}
    for s in specs:
        cohort_counts[s["cohort"]] = cohort_counts.get(s["cohort"], 0) + 1
    print(f"  cohort distribution: {cohort_counts}")

    if args.dry_run:
        # Show a sample for sanity-checking.
        for s in specs[:3]:
            print(f"    sample: {s['username']} / {s['tier']} / cohort={s['cohort']} / signup={s['signup_at'].isoformat()}")
        return 0

    # 2. Create auth.users (Admin API).
    if args.skip_auth:
        # Without auth users we can't FK to profiles/sessions, so fabricate UUIDs
        # and skip the FK by leaving profile/session writes to the user.
        print("  --skip-auth: assigning random UUIDs (downstream inserts will fail FK).")
        for s in specs:
            s["user_id"] = str(uuid.uuid4())
        live_specs = specs
    else:
        live_specs = _create_users_parallel(base_url, service_key, specs, args.workers)
        if not live_specs:
            print("no users created. aborting.", file=sys.stderr)
            return 1

    rest = requests.Session()

    # 3. Profiles — one per user, with backdated created_at.
    profile_rows = [
        {
            "id": s["user_id"],
            "username": s["username"],
            "tier": s["tier"],
            "created_at": _iso(s["signup_at"]),
        }
        for s in live_specs
    ]
    print(f"  inserting {len(profile_rows)} profiles...")
    n_prof = _bulk_insert(rest, base_url, service_key, "profiles", profile_rows,
                          on_conflict="id")
    print(f"    inserted {n_prof} profile rows")

    # 4. Sessions — generate per user, then bulk insert in chunks.
    print("  generating sessions...")
    all_sessions: List[Dict[str, Any]] = []
    for s in live_specs:
        all_sessions.extend(_build_sessions_for_user(s, now))
    print(f"  inserting {len(all_sessions)} sessions...")
    n_sess = _bulk_insert(rest, base_url, service_key, "sessions", all_sessions,
                          batch_size=400)
    print(f"    inserted {n_sess} session rows")

    # 5. usage_daily — derived from session timestamps.
    usage_rows = _aggregate_usage_daily(all_sessions)
    print(f"  inserting {len(usage_rows)} usage_daily rows...")
    n_usage = _bulk_insert(rest, base_url, service_key, "usage_daily", usage_rows,
                           batch_size=1000, on_conflict="user_id,day")
    print(f"    inserted {n_usage} usage_daily rows")

    # 6. Summary.
    total_tokens = sum(r["tokens_in"] + r["tokens_out"] for r in all_sessions)
    completed = sum(1 for r in all_sessions if r["status"] == "completed")
    print()
    print("=" * 60)
    print(f"users created:       {len(live_specs):>10,}")
    print(f"profiles inserted:   {n_prof:>10,}")
    print(f"sessions inserted:   {n_sess:>10,} ({completed:,} completed)")
    print(f"usage_daily rows:    {n_usage:>10,}")
    print(f"total tokens (demo): {total_tokens:>10,}")
    print("=" * 60)
    print(f"cleanup: python {sys.argv[0]} --purge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
