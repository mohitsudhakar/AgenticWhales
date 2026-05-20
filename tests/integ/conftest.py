"""Pytest fixtures for integration tests that need real Postgres.

Each test that asks for `pg_url` gets a session-scoped Postgres container
running for the test session. Schema is applied once, idempotently. Each
test should clean up after itself (TRUNCATE its tables) or use a fresh
test-user-id so it doesn't see another test's rows.

We deliberately substitute a `auth` schema stub for the Supabase
`auth.users` table the real schema references — testcontainers Postgres
doesn't include Supabase's auth schema, and we don't need real
authentication, just the foreign-key target shape.

The whole module is import-time-safe: if testcontainers / psycopg are
missing OR Docker isn't running, the `pg_url` fixture skips the test
rather than erroring. So you can run `pytest` without the integration
extras installed and these tests silently sit out.
"""

from __future__ import annotations

import os
import re
import textwrap
from pathlib import Path

import pytest


def _has_integration_deps() -> tuple[bool, str]:
    try:
        import psycopg  # noqa: F401
        from testcontainers.postgres import PostgresContainer  # noqa: F401
    except ImportError as exc:
        return False, f"integration deps missing ({exc})"
    return True, ""


def _docker_available() -> tuple[bool, str]:
    """Cheap probe — try to talk to the Docker socket without spawning a
    container. We accept that a more thorough check would actually start
    a tiny container, but the docker module's client constructor already
    validates the daemon connection."""
    try:
        import docker
    except ImportError:
        return False, "python `docker` package not installed"
    try:
        client = docker.from_env()
        client.ping()
        return True, ""
    except Exception as exc:   # docker not running, perm denied, etc.
        return False, f"docker daemon not reachable ({exc})"


@pytest.fixture(scope="session")
def pg_url():
    """Session-scoped Postgres container with our schema applied.

    Yields a DSN string usable by `psycopg.connect(...)`. Skips the whole
    test when integration deps are missing or Docker isn't reachable so
    local runs without Docker stay clean.
    """
    ok, msg = _has_integration_deps()
    if not ok:
        pytest.skip(msg)
    ok, msg = _docker_available()
    if not ok:
        pytest.skip(msg)

    from testcontainers.postgres import PostgresContainer

    # Postgres 16 matches what Supabase currently ships. `pgcrypto` is
    # needed for `gen_random_bytes` used by the paper_place_order RPC.
    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        _apply_schema(dsn)
        yield dsn


def _apply_schema(dsn: str) -> None:
    """Apply our `docs/supabase-schema.sql` to the container, after stubbing
    out Supabase-only bits the file references."""
    import psycopg

    schema_path = Path(__file__).resolve().parent.parent.parent / "docs" / "supabase-schema.sql"
    sql = schema_path.read_text()

    # Stub `auth` schema + `auth.uid()` so the schema's FK + RLS clauses
    # compile against vanilla Postgres. We also drop `security definer set
    # search_path = public` — works on vanilla Postgres but keep the same
    # functional behaviour.
    bootstrap = textwrap.dedent("""
        create extension if not exists pgcrypto;
        create schema if not exists auth;
        create table if not exists auth.users (
            id uuid primary key default gen_random_uuid()
        );
        -- Stub auth.uid() so RLS expressions don't fail at compile.
        create or replace function auth.uid() returns uuid
            language sql stable as $$ select null::uuid $$;
        grant usage on schema auth to public;
        grant select on auth.users to public;
        -- Vanilla Postgres has no `authenticated` / `service_role` roles —
        -- create them as no-op shells so the `grant execute ... to ...`
        -- statements in the schema don't fail.
        do $$ begin
            create role authenticated;
        exception when duplicate_object then null; end $$;
        do $$ begin
            create role service_role;
        exception when duplicate_object then null; end $$;
    """)

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(bootstrap)
        cur.execute(sql)


@pytest.fixture
def test_user_id(pg_url):
    """Seed a fresh auth.users row per test so each test has its own user.
    Returns the UUID string."""
    import psycopg
    with psycopg.connect(pg_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("insert into auth.users default values returning id")
        return str(cur.fetchone()[0])
