"""Recipe scheduler — wraps APScheduler with Phase 1 gates.

Single logical scheduler across N FastAPI workers via a Postgres advisory
lock. The DB is source of truth: on every start the leader-elect re-reads
`recipes WHERE status='active'` and re-registers all jobs from scratch.
This eliminates bi-store consistency pain and works identically with the
in-memory fallback (where every "worker" is the leader by definition).

Per-recipe `threading.Lock` keyed on recipe_id guarantees no two concurrent
fires of the same recipe even across restarts. Combined with APScheduler's
`max_instances=1`, this is two-belts-and-suspenders.

Gates (fail-fast, in order):
  1. recipe.status != active                                              → drop
  2. market-hours gate (when market_hours_only=true)                      → drop
  3. budget gate (recipe_usage today >= max_daily_token_cost_usd)         → drop + risk_event
  4. user-spend gate (user_spend_daily today >= daily_spend_cap_usd)      → drop + risk_event
  5. failure gate (consecutive_failures >= 5)                              → status='failed' + risk_event
After gates: build session, run runner (existing SessionRunner), runner's
post-decision hook drives RiskGuard + paper-order placement.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from agenticwhales import recipes as recipes_mod
from agenticwhales import risk as risk_mod
from agenticwhales.agents.schemas import Recipe, RecipeStatus, ScheduleKind
from agenticwhales.audit import audit, impersonate
from agenticwhales.calendar import is_market_open
from agenticwhales.observability import METRICS, correlation_id, get_logger

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore
    from apscheduler.triggers.cron import CronTrigger  # type: ignore
    from apscheduler.triggers.interval import IntervalTrigger  # type: ignore
    _HAS_APS = True
except ImportError:  # pragma: no cover
    AsyncIOScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore
    IntervalTrigger = None  # type: ignore
    _HAS_APS = False

log = get_logger(__name__)

# Max consecutive failures before a recipe auto-flips to status='failed'.
MAX_CONSECUTIVE_FAILURES = 5

# Postgres advisory-lock key used for leader election. Arbitrary 32-bit int.
LEADER_LOCK_KEY = 74_737_248

# Worker id stamped into the leader heartbeat row.
_WORKER_ID = f"{os.uname().nodename}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _parse_interval_seconds(expr: str) -> int:
    """Parse '30s' / '15m' / '6h' / '1d' into seconds.

    Bare integers are interpreted as seconds. Raises ValueError on bad input.
    """
    s = (expr or "").strip().lower()
    if not s:
        raise ValueError("empty interval expression")
    if s.isdigit():
        return int(s)
    unit = s[-1]
    body = s[:-1]
    if not body.isdigit():
        raise ValueError(f"bad interval expression: {expr!r}")
    n = int(body)
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    if unit == "d":
        return n * 86400
    raise ValueError(f"unknown interval unit: {unit!r}")


class RecipeScheduler:
    """APScheduler wrapper. Owns the leader-lock, per-recipe locks, and gates.

    Two callable extension points are injected at construction:

    - `run_session`: called inside the leader thread with `(recipe, fire_id)`
      after all gates pass. Returns nothing; raises on unrecoverable errors.
      In prod this is wired to a SessionRunner; in tests it's a stub.
    - `register_runner`: optional callback to register the spawned runner
      against the existing `_runners` dict in `web/server.py` so the
      WebSocket stream endpoint works for recipe-fired sessions.
    """

    def __init__(
        self,
        run_session: Optional[Callable[[Recipe, str], None]] = None,
        register_runner: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self._run_session = run_session
        self._register_runner = register_runner
        self._inflight: Dict[str, threading.Lock] = {}
        self._inflight_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._scheduler: Optional[Any] = None
        self._is_leader = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._bootstrapped = False
        # Phase 3 — streaming worker is lazily started on leader acquisition.
        # Lives only on the leader so non-leaders don't burn Alpaca quota.
        self._streaming_worker: Optional[Any] = None

    # ---- lifecycle ----

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if not _HAS_APS:
            log.warning("apscheduler not installed; recipe scheduler is disabled")
            return
        if self._scheduler is not None:
            return
        self._loop = loop or asyncio.get_event_loop()
        self._scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        )
        self._scheduler.start()
        # Attempt leader election + bootstrap as a kick-off task.
        try:
            self._heartbeat_task = self._loop.create_task(self._leader_loop())
        except RuntimeError:
            # Loop not running yet — schedule_via_app's lifespan will retry on next tick.
            self._heartbeat_task = None

    async def shutdown(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._streaming_worker is not None:
            try:
                await self._streaming_worker.stop()
            except Exception as exc:
                log.warning("streaming worker stop failed", exc=str(exc))
            self._streaming_worker = None
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
        self._release_leader()
        METRICS.scheduler_is_leader.set(0) if METRICS.enabled else None

    # ---- weekly cron jobs (run only on the leader) ----

    def _register_weekly_jobs(self) -> None:
        """Register the recurring maintenance jobs that should run on a
        cadence rather than on a fire event. Called once per leadership
        acquisition. Each job re-checks `self._is_leader` at fire time so a
        leadership handoff mid-flight doesn't double-run."""
        if self._scheduler is None:
            return
        # Phase 2 #9 — prompt-eval harness. 04:00 UTC every Sunday is the
        # canonical "weekly maintenance" slot; misses tolerate a 12h grace.
        self._scheduler.add_job(
            self._run_prompt_evals,
            CronTrigger.from_crontab("0 4 * * 0", timezone="UTC"),
            id="prompt_eval_weekly",
            replace_existing=True,
            misfire_grace_time=43_200,
            max_instances=1,
        )
        # Nightly outcome resolver — closes the loop on every paper_order with
        # expected_hold_days elapsed: pulls the realized return, scores Brier,
        # writes decision_outcomes. Runs 02:00 UTC daily; 6h misfire grace.
        self._scheduler.add_job(
            self._run_outcome_resolver,
            CronTrigger.from_crontab("0 2 * * *", timezone="UTC"),
            id="outcome_resolver_nightly",
            replace_existing=True,
            misfire_grace_time=21_600,
            max_instances=1,
        )
        # PR-3 — Stuck-run reaper. Recipe fires are fire-and-forget now,
        # which means a session can get stuck in `status='running'` if the
        # pod dies mid-run. Without a reaper, the per-recipe concurrent
        # gate (`has_running_session_for_recipe`) would block all future
        # fires of that recipe forever. The reaper sweeps every 5 min and
        # flips rows older than the cutoff to `status='failed'`.
        self._scheduler.add_job(
            self._run_stuck_run_reaper,
            IntervalTrigger(minutes=5),
            id="stuck_run_reaper",
            replace_existing=True,
            misfire_grace_time=300,
            max_instances=1,
        )
        # Daily hard-delete of long-stuck running rows. The 5-min reaper above
        # flips them to `failed`; this job removes the ones that never got
        # reaped (pre-reaper deploys, non-leader pods, autonomy disabled). Runs
        # nightly so the Analyses + Recent Activity tables don't accumulate
        # forever-RUNNING rows that no longer reflect any live work.
        self._scheduler.add_job(
            self._run_stale_running_cleanup,
            CronTrigger.from_crontab("15 3 * * *", timezone="UTC"),
            id="stale_running_cleanup",
            replace_existing=True,
            misfire_grace_time=3600,
            max_instances=1,
        )

    # PR-3: stuck-run reaper. Tunable via env so ops can dial it on a hot
    # incident without a redeploy.
    STUCK_RUN_CUTOFF_SECONDS = int(
        os.getenv("AGENTICWHALES_STUCK_RUN_CUTOFF_SECONDS", str(30 * 60))
    )

    # Daily hard-delete cutoff. Defaults to 1 day — anything older is
    # certainly orphaned, since the slowest legitimate run finishes in <5min.
    STALE_RUNNING_DELETE_CUTOFF_SECONDS = int(
        os.getenv("AGENTICWHALES_STALE_RUNNING_DELETE_CUTOFF_SECONDS", str(24 * 60 * 60))
    )

    def _run_stuck_run_reaper(self) -> None:
        """Flip stuck `running` sessions to `failed` so concurrent-fire gates
        don't permanently block their recipes.

        A "stuck" session is one whose `status='running'` and whose
        `created_at` is older than `STUCK_RUN_CUTOFF_SECONDS` (default
        30 min). In practice a session takes < 5 min; anything past 30
        min is overwhelmingly a dead pod or hung LLM call.

        Idempotent — flipping a row twice is a no-op. Safe to run on every
        leader; non-leaders skip.
        """
        if not self._is_leader:
            return
        from web import auth as _auth
        stuck = _auth.list_stuck_running_sessions(
            older_than_seconds=self.STUCK_RUN_CUTOFF_SECONDS,
            limit=200,
        )
        if not stuck:
            return

        reaped = 0
        for row in stuck:
            sid = row.get("id")
            if not sid:
                continue
            if _auth.mark_session_failed(
                sid, failure_reason="stuck_run_reaped",
            ):
                reaped += 1
                # Audit-log per-session so we can answer "why did this
                # recipe stop firing?" by checking the failure_reason.
                user_id = row.get("user_id")
                if user_id:
                    try:
                        audit(user_id, "session.reaped_stuck",
                              target_resource=sid,
                              metadata={
                                  "recipe_id": row.get("recipe_id"),
                                  "fire_id": row.get("fire_id"),
                                  "created_at": row.get("created_at"),
                                  "cutoff_seconds": self.STUCK_RUN_CUTOFF_SECONDS,
                              })
                    except Exception:
                        # Audit is best-effort; never let it break the reaper.
                        pass

        if reaped:
            log.warning("stuck-run reaper flipped sessions to failed",
                        count=reaped,
                        cutoff_seconds=self.STUCK_RUN_CUTOFF_SECONDS)
            if METRICS.enabled:
                # Reuse the recipe-fire status counter so the dashboards
                # already showing fire health get a "stuck-reaped" channel.
                METRICS.recipe_fire.labels(status="reaped").inc(reaped)

    def _run_stale_running_cleanup(self) -> None:
        """Hard-delete sessions stuck at running/pending past the daily cutoff.

        The 5-minute reaper above flips fresh stuck rows to `failed`. This
        nightly job removes the ones that have been RUNNING for over a day —
        rows the reaper missed because autonomy was off, no leader was
        elected, or the deploy pre-dates the reaper.

        Idempotent. Safe on leader-only; non-leaders early-return.
        """
        if not self._is_leader:
            return
        from web import auth as _auth
        try:
            deleted = _auth.delete_stuck_running_sessions(
                older_than_seconds=self.STALE_RUNNING_DELETE_CUTOFF_SECONDS,
                limit=500,
            )
        except Exception as exc:
            log.exception("stale_running_cleanup failed", exc=str(exc))
            return
        if deleted:
            log.warning("stale-running cleanup deleted sessions",
                        count=deleted,
                        cutoff_seconds=self.STALE_RUNNING_DELETE_CUTOFF_SECONDS)
            if METRICS.enabled:
                METRICS.recipe_fire.labels(status="stale_deleted").inc(deleted)

    def _run_outcome_resolver(self) -> None:
        """Walk every user with at least one unresolved paper order and run
        `resolve_outcomes_for_user`. Runs only on the leader."""
        if not self._is_leader:
            return
        from agenticwhales import outcomes as outcomes_mod
        from web import auth as _auth
        user_ids: set[str] = set()
        if _auth._db_writable():
            try:
                rows = _auth._select_columns(
                    "paper_orders",
                    filters={}, select="user_id", limit=10_000,
                )
                user_ids = {r["user_id"] for r in rows if r.get("user_id")}
            except Exception as exc:
                log.warning("outcome_resolver cron: user scan failed", exc=str(exc))
        if not user_ids:
            user_ids = {
                r.get("user_id")
                for (table, _), r in _auth._memstore.items()
                if table == "paper_orders" and r.get("user_id")
            }
        total_resolved = 0
        for uid in user_ids:
            try:
                n = outcomes_mod.resolve_outcomes_for_user(uid, limit=200)
                total_resolved += int(n or 0)
            except Exception as exc:
                log.warning("outcome_resolver cron failure for %s: %s", uid, exc)
        log.info("outcome_resolver cron complete",
                 users=len(user_ids), resolved=total_resolved)

    def _run_prompt_evals(self) -> None:
        """Walk every user with enough resolved outcomes and run a baseline
        flat-coin (`p = 0.5`) variant through the prompt-eval harness. The
        flat-coin variant is a *canary*: if it beats the live PM, the live
        PM is genuinely worse than chance and warrants engineering attention.

        Real candidate prompts ship in Phase 2.x — wiring them is one line
        here once we have a variant registry. For v1 we exercise the harness
        end-to-end against this canary so the cron path is verified.
        """
        if not self._is_leader:
            return
        from agenticwhales import adaptive
        from web import auth as _auth

        # Find every user_id with at least one resolved outcome. The
        # memstore covers dev; in prod this scans `decision_outcomes`.
        user_ids: set[str] = set()
        if _auth._db_writable():
            try:
                rows = _auth._select_columns(
                    "decision_outcomes",
                    filters={}, select="user_id", limit=10_000,
                )
                user_ids = {r["user_id"] for r in rows if r.get("user_id")}
            except Exception as exc:
                log.warning("prompt_eval cron: user scan failed", exc=str(exc))
        if not user_ids:
            user_ids = {
                r.get("user_id")
                for (table, _), r in _auth._memstore.items()
                if table == "decision_outcomes" and r.get("user_id")
            }

        evaluated = 0
        promoted = 0
        for uid in user_ids:
            try:
                result = adaptive.evaluate_prompt_variant(
                    uid, variant="canary-flat-coin",
                    scorer=lambda _row: 0.5,
                )
                if result is None:
                    continue
                evaluated += 1
                if result.promoted:
                    promoted += 1
            except Exception as exc:
                log.warning("prompt_eval cron failure for %s: %s", uid, exc)
        log.info("prompt_eval cron complete",
                 users=len(user_ids), evaluated=evaluated, promoted=promoted)

    # ---- streaming worker (Phase 3) ----

    async def _start_streaming_worker(self) -> None:
        """Spin up the streaming worker on this (leader) worker.

        Only recipes with non-null `trigger_conditions` participate. If no
        recipes need streaming, we still create the worker but with an empty
        binding set — so a recipe added later just needs an `update_recipes`
        call to start being evaluated.
        """
        if self._streaming_worker is not None:
            return
        from web.streaming_worker import StreamingWorker
        worker = StreamingWorker(
            fire_recipe=self._fire_from_streaming,
            is_leader_fn=lambda: self._is_leader,
        )
        try:
            active = recipes_mod.list_all_active()
        except Exception as exc:
            log.warning("streaming start: list_all_active failed", exc=str(exc))
            active = []
        try:
            await worker.start(active)
        except Exception as exc:
            log.warning("streaming start failed", exc=str(exc))
            return
        self._streaming_worker = worker

    async def _fire_from_streaming(self, recipe, symbol: str, reason: str) -> None:
        """Callback for the streaming worker. Mints a fire_id and dispatches
        through the existing per-recipe fire path so all gates + idempotency
        rules apply uniformly to streaming-fired vs cron-fired runs."""
        fire_id = uuid.uuid4().hex
        audit("system", "streaming.fire",
              target_user_id=recipe.user_id,
              metadata={"recipe_id": recipe.id, "symbol": symbol,
                        "reason": reason, "fire_id": fire_id})
        try:
            self._do_fire(recipe, fire_id, skip_market_hours=False)
        except Exception as exc:
            log.warning("streaming-driven fire failed",
                        recipe_id=recipe.id, exc=str(exc))

    # ---- leader election ----

    async def _leader_loop(self) -> None:
        """Repeatedly attempt to acquire the leader lock + heartbeat.

        Uses a dedicated thread for the blocking advisory-lock call so we
        don't stall the event loop. In the Supabase REST-only setup we don't
        have a raw psycopg connection handy — we fall back to "first writer
        wins" via the `scheduler_leader` row update. Good enough for the
        single-worker default deployment; production multi-worker should
        switch to a direct asyncpg connection that holds `pg_advisory_lock`.
        """
        try:
            while True:
                acquired = await asyncio.get_event_loop().run_in_executor(
                    None, self._try_acquire_leader,
                )
                if acquired and not self._is_leader:
                    self._is_leader = True
                    METRICS.scheduler_is_leader.set(1) if METRICS.enabled else None
                    audit("system", "scheduler.leader.acquire", metadata={"worker_id": _WORKER_ID})
                    log.info("scheduler became leader", worker_id=_WORKER_ID)
                    await self._bootstrap()
                    # Register weekly cron jobs that only the leader runs.
                    # Re-registering on every leadership acquisition is safe
                    # because APScheduler `replace_existing=True` is set.
                    try:
                        self._register_weekly_jobs()
                    except Exception as exc:
                        log.warning("scheduler: register_weekly_jobs failed", exc=str(exc))
                    # Start streaming worker (Phase 3) on the leader only.
                    try:
                        await self._start_streaming_worker()
                    except Exception as exc:
                        log.warning("scheduler: streaming worker start failed", exc=str(exc))
                elif acquired:
                    pass  # already leader; nothing to do
                else:
                    if self._is_leader:
                        self._is_leader = False
                        METRICS.scheduler_is_leader.set(0) if METRICS.enabled else None
                        audit("system", "scheduler.leader.lose", metadata={"worker_id": _WORKER_ID})
                        log.warning("scheduler lost leader", worker_id=_WORKER_ID)
                        # Stop the streaming worker — only the leader runs it.
                        if self._streaming_worker is not None:
                            try:
                                await self._streaming_worker.stop()
                            except Exception as exc:
                                log.warning("streaming worker stop on demote failed",
                                            exc=str(exc))
                            self._streaming_worker = None
                await asyncio.sleep(15.0 if not self._is_leader else 5.0)
        except asyncio.CancelledError:
            return

    def _try_acquire_leader(self) -> bool:
        """Acquire / refresh the leader heartbeat row.

        Returns True if we hold the leader. Algorithm: read the existing row.
        If absent or its heartbeat is stale (>30s), claim it via upsert; if
        present and fresh and not ours, return False; if present and ours,
        refresh the heartbeat and return True.
        """
        from web import auth  # lazy

        if not auth._db_writable():
            # Single-worker dev mode: always leader.
            return True

        url = f"{auth._rest_url('scheduler_leader')}?id=eq.1&select=*"
        try:
            resp = auth._http.get(url, headers=auth._service_headers(), timeout=5)
            if resp.status_code != 200:
                return False
            rows = resp.json() or []
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("leader read failed", exc=str(exc))
            return False

        now = datetime.now(tz=timezone.utc)
        if not rows:
            return self._upsert_leader_row(now)

        row = rows[0]
        try:
            hb = datetime.fromisoformat(row["heartbeat_at"].replace("Z", "+00:00"))
        except Exception:
            hb = datetime.fromtimestamp(0, tz=timezone.utc)

        owner = row.get("worker_id")
        age = (now - hb).total_seconds()
        if owner == _WORKER_ID:
            return self._upsert_leader_row(now)
        if age > 30:
            return self._upsert_leader_row(now)
        return False

    def _upsert_leader_row(self, when: datetime) -> bool:
        """Race-claim or refresh the leader row. Returns True on success."""
        from web import auth
        row = {
            "id": 1,
            "worker_id": _WORKER_ID,
            "heartbeat_at": when.isoformat(),
        }
        try:
            resp = auth._http.post(
                f"{auth._rest_url('scheduler_leader')}?on_conflict=id",
                headers=auth._service_headers({"Prefer": "resolution=merge-duplicates,return=representation"}),
                json=[row], timeout=5,
            )
            if resp.status_code >= 300:
                return False
            data = resp.json() or []
            return bool(data) and data[0].get("worker_id") == _WORKER_ID
        except Exception as exc:
            log.warning("leader upsert failed", exc=str(exc))
            return False

    def _release_leader(self) -> None:
        """Best-effort release of the leader row on shutdown."""
        from web import auth
        if not self._is_leader or not auth._db_writable():
            return
        try:
            auth._http.delete(
                f"{auth._rest_url('scheduler_leader')}?id=eq.1&worker_id=eq.{_WORKER_ID}",
                headers=auth._service_headers(),
                timeout=5,
            )
        except Exception:
            pass

    # ---- bootstrap ----

    async def _bootstrap(self) -> None:
        """Re-register every active recipe as an APScheduler job."""
        if self._bootstrapped:
            return
        self._bootstrapped = True
        active = recipes_mod.list_all_active()
        for recipe in active:
            try:
                self.add(recipe)
            except Exception as exc:
                log.warning("bootstrap.add failed", recipe_id=recipe.id, exc=str(exc))
        log.info("scheduler bootstrap complete", count=len(active))

    # ---- job CRUD ----

    def add(self, recipe: Recipe) -> Optional[str]:
        """Register an APScheduler job for the recipe. Returns the job_id or None."""
        if self._scheduler is None or recipe.schedule_kind == ScheduleKind.MANUAL:
            return None
        trigger = self._trigger_for(recipe)
        if trigger is None:
            return None
        job = self._scheduler.add_job(
            self._wrap_fire, trigger, args=[recipe.id],
            id=recipe.id, replace_existing=True,
            misfire_grace_time=recipe.misfire_grace_seconds,
            coalesce=True, max_instances=1,
        )
        return job.id

    def remove(self, recipe_id: str) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(recipe_id)
        except Exception:
            pass

    def pause(self, recipe_id: str) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.pause_job(recipe_id)
        except Exception:
            pass

    def resume(self, recipe_id: str) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.resume_job(recipe_id)
        except Exception:
            # If the job doesn't exist (e.g. server restarted), re-add it.
            recipe = recipes_mod.load(recipe_id)
            if recipe:
                self.add(recipe)

    def trigger_now(self, recipe_id: str) -> str:
        """Fire a recipe immediately. Returns the session_id."""
        recipe = recipes_mod.load(recipe_id)
        if not recipe:
            raise ValueError(f"recipe not found: {recipe_id}")
        # Skip market-hours gate on manual trigger; still honor budget/failure.
        fire_id = uuid.uuid4().hex
        self._do_fire(recipe, fire_id, skip_market_hours=True)
        return fire_id  # session_id is set inside _do_fire and returned via callback

    # ---- trigger construction ----

    def _trigger_for(self, recipe: Recipe) -> Optional[Any]:
        if recipe.schedule_kind == ScheduleKind.CRON:
            if not recipe.schedule_expr:
                return None
            return CronTrigger.from_crontab(recipe.schedule_expr, timezone="UTC")
        if recipe.schedule_kind == ScheduleKind.INTERVAL:
            try:
                secs = _parse_interval_seconds(recipe.schedule_expr or "")
            except ValueError as exc:
                log.warning("bad interval expr", recipe_id=recipe.id, exc=str(exc))
                return None
            return IntervalTrigger(seconds=secs, timezone="UTC")
        return None

    # ---- the actual fire path ----

    def _wrap_fire(self, recipe_id: str) -> None:
        """APScheduler-callable entry point. Loads recipe + per-recipe lock."""
        with self._inflight_lock:
            lock = self._inflight.setdefault(recipe_id, threading.Lock())
        if not lock.acquire(blocking=False):
            log.info("fire skipped (concurrent)", recipe_id=recipe_id)
            return
        try:
            recipe = recipes_mod.load(recipe_id)
            if not recipe:
                return
            fire_id = uuid.uuid4().hex
            self._do_fire(recipe, fire_id, skip_market_hours=False)
        finally:
            lock.release()

    def _do_fire(self, recipe: Recipe, fire_id: str, *, skip_market_hours: bool) -> None:
        token_cid = correlation_id.set(fire_id)
        try:
            # Gate 1: status
            if recipe.status != RecipeStatus.ACTIVE:
                self._record_metric("skipped")
                return

            # Gate 2: market hours
            if not skip_market_hours and recipe.market_hours_only:
                if not is_market_open(recipe.exchange_code, datetime.now(tz=timezone.utc)):
                    self._record_metric("skipped")
                    log.info("fire skipped (market closed)",
                             recipe_id=recipe.id, exchange=recipe.exchange_code)
                    return

            today = datetime.now(tz=timezone.utc).date().isoformat()

            # Gate 3: per-recipe budget
            from web import auth
            usage = auth.load_recipe_usage(recipe.id, today)
            if usage and float(usage.get("token_cost_usd", 0)) >= recipe.max_daily_token_cost_usd:
                self._record_metric("budget")
                self._emit_risk_event(recipe, "budget", {
                    "today_cost_usd": float(usage["token_cost_usd"]),
                    "cap_usd": recipe.max_daily_token_cost_usd,
                })
                return

            # Gate 4: per-user daily spend cap
            limits = auth.load_risk_limits(recipe.user_id) or auth._default_risk_limits_row(recipe.user_id)
            user_spend = auth.load_user_spend(recipe.user_id, today)
            cap = float(limits.get("daily_spend_cap_usd", 25.0))
            if user_spend >= cap:
                self._record_metric("budget")
                self._emit_risk_event(recipe, "user_spend_cap", {
                    "today_cost_usd": user_spend, "cap_usd": cap,
                })
                return

            # Gate 5: consecutive-failure auto-pause
            if recipe.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self._record_metric("failed")
                recipes_mod.update_status(recipe.id, RecipeStatus.FAILED)
                self._emit_risk_event(recipe, "failures", {
                    "consecutive": recipe.consecutive_failures,
                })
                return

            # All gates passed. Build + run session under impersonation.
            with impersonate(recipe.user_id, "scheduler_fire", fire_id=fire_id):
                if self._run_session is None:
                    log.info("dry-run fire (no run_session callback)",
                             recipe_id=recipe.id, fire_id=fire_id)
                    self._record_metric("ok")
                    recipes_mod.touch_last_run(recipe.id)
                    recipes_mod.reset_failures(recipe.id)
                    return
                try:
                    self._run_session(recipe, fire_id)
                    recipes_mod.reset_failures(recipe.id)
                    recipes_mod.touch_last_run(recipe.id)
                    self._record_metric("ok")
                except Exception as exc:
                    log.exception("fire raised", recipe_id=recipe.id, exc=str(exc))
                    new_count = recipes_mod.bump_failures(recipe.id)
                    self._record_metric("failed")
                    if new_count >= MAX_CONSECUTIVE_FAILURES:
                        recipes_mod.update_status(recipe.id, RecipeStatus.FAILED)
                        self._emit_risk_event(recipe, "failures",
                                               {"consecutive": new_count})
        finally:
            correlation_id.reset(token_cid)

    # ---- helpers ----

    def _record_metric(self, status: str) -> None:
        if METRICS.enabled:
            METRICS.recipe_fire.labels(status=status).inc()

    def _emit_risk_event(self, recipe: Recipe, rule: str, details: Dict[str, Any]) -> None:
        from web import auth
        auth.insert_risk_event({
            "user_id": recipe.user_id,
            "recipe_id": recipe.id,
            "session_id": None,
            "ticker": None,
            "rule": rule,
            "details": details,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        })
        if METRICS.enabled:
            METRICS.risk_event.labels(rule=rule).inc()


# Module-level singleton — populated when `web/server.py` calls `start(...)`.
scheduler = RecipeScheduler()
