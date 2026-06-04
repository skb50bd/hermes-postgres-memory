"""PostgreSQL kanban plugin for Hermes Agent.

Replaces the SQLite kanban store (`~/.hermes/kanban/boards/*/kanban.db`)
with a Postgres-backed implementation in the `hermes_kanban` schema.

Design notes
------------
- One schema (`hermes_kanban`) per Hermes profile database. Schema is
  shipped in the `hermes_template` and cloned into every profile.
- Race-free claim uses `SELECT ... FOR UPDATE SKIP LOCKED` instead of
  the old `claim_lock`/`claim_expires` columns. No cron-based stale
  reaper needed — the lock auto-releases on transaction end.
- Tenant is a first-class FK (`hermes_kanban.tenants`) instead of a
  free-form `tasks.tenant TEXT`.
- Full-text search uses a generated `tsvector` column on `body`.
- All write paths return task IDs that are stable across migrations
  (`t_<26-char base32>`), so the SQLite → PG migration is idempotent.

Configuration
-------------
- PG_MEM_DB_CONN_STR — same DSN as the memory plugin. The kanban
  plugin reuses the existing connection pool when both plugins are
  loaded in the same process.
- HERMES_KANBAN_SCHEMA — override the default `hermes_kanban` schema
  (useful for per-profile isolation testing).

This module is the canonical storage layer. The thin wrapper in
`hermes_cli/kanban_db.py` re-exports the public surface as plain
functions so the dispatcher and CLI keep working unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import string
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2.extensions import make_dsn

logger = logging.getLogger(__name__)

# Schema name. Per-profile isolation comes from per-profile DBs cloned
# from `hermes_template`, NOT from schema names. Override only for tests.
DEFAULT_SCHEMA = "hermes_kanban"


def get_pg_kanban_schema() -> str:
    raw = os.environ.get("HERMES_KANBAN_SCHEMA", "").strip()
    return raw or DEFAULT_SCHEMA


# ── Connection pool (shared with memory plugin if both are loaded) ──

_KANBAN_POOL: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_KANBAN_POOL_LOCK = threading.Lock()


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        return default


def _normalize_pg_mem_dsn(dsn: str) -> str:
    """Same normalizer as the memory plugin — accept either libpq
    DSN or `Host=...;Port=...` style."""
    raw = dsn.strip()
    if ";" not in raw or "=" not in raw.split(";", 1)[0]:
        return raw
    mapping = {
        "host": "host", "server": "host", "port": "port",
        "database": "dbname", "dbname": "dbname",
        "user": "user", "username": "user", "userid": "user", "uid": "user",
        "password": "password", "pwd": "password",
        "sslmode": "sslmode",
        "application_name": "application_name", "applicationname": "application_name",
    }
    kwargs: Dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        normalized = mapping.get(key.strip().replace(" ", "").lower())
        if normalized and value.strip():
            kwargs[normalized] = value.strip()
    if not kwargs:
        return raw
    return make_dsn(**kwargs)


def get_pg_kanban_db_conn_str() -> str:
    """Reuse the memory plugin's connection string. Kanban and memory
    live in the same profile database."""
    dsn = os.environ.get("PG_MEM_DB_CONN_STR", "").strip()
    if dsn:
        return _normalize_pg_mem_dsn(dsn)
    raise RuntimeError(
        "No postgres connection configured. Set PG_MEM_DB_CONN_STR in "
        "~/.hermes/.env, e.g. "
        "PG_MEM_DB_CONN_STR='postgresql://hermes:***@10.0.0.1:5432/hermes'"
    )


def _kanban_dsn() -> str:
    base = get_pg_kanban_db_conn_str()
    connect_timeout = _env_int("HERMES_POSTGRES_CONNECT_TIMEOUT", 5, minimum=1)
    statement_timeout = _env_int("HERMES_POSTGRES_STATEMENT_TIMEOUT_MS", 10_000, minimum=100)
    idle_tx_timeout = _env_int("HERMES_POSTGRES_IDLE_TX_TIMEOUT_MS", 30_000, minimum=100)
    return make_dsn(
        dsn=base, sslmode="prefer", connect_timeout=connect_timeout,
        application_name="hermes-kanban-postgres",
        options=f"-c statement_timeout={statement_timeout} "
                f"-c idle_in_transaction_session_timeout={idle_tx_timeout}",
    )


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _KANBAN_POOL
    if _KANBAN_POOL is not None:
        return _KANBAN_POOL
    with _KANBAN_POOL_LOCK:
        if _KANBAN_POOL is None:
            minconn = _env_int("HERMES_POSTGRES_POOL_MIN", 0, minimum=0)
            maxconn = _env_int("HERMES_POSTGRES_POOL_MAX", 2, minimum=1)
            if minconn > maxconn:
                minconn = maxconn
            _KANBAN_POOL = psycopg2.pool.ThreadedConnectionPool(
                minconn, maxconn, _kanban_dsn()
            )
    return _KANBAN_POOL


# ── ID generation ──────────────────────────────────────────────────────
#
# SQLite used `t_<26 chars base32>`. We keep the same format so IDs
# round-trip cleanly through the migration. Use secrets.choice for
# cryptographic randomness; collisions are vanishingly unlikely
# (1 in 32^26 ≈ 1 in 10^39 for 1M tasks).

_BASE32_ALPHABET = "0123456789abcdefghijklmnopqrstuv"


def _new_task_id() -> str:
    return "t_" + "".join(secrets.choice(_BASE32_ALPHABET) for _ in range(26))


# ── Cursor / connection helpers ────────────────────────────────────────


class KanbanSchemaMissing(RuntimeError):
    """Raised when the hermes_kanban schema is absent."""


def _schema_table(schema: str) -> str:
    """Render a quoted schema-qualified identifier for safe interpolation."""
    # Validate the override — we never want SQL injection via the env var.
    if not schema.replace("_", "").isalnum():
        raise ValueError(f"Invalid schema name: {schema!r}")
    return schema


@contextmanager
def _cursor(*, commit: bool = False) -> Iterator[Any]:
    """Borrow a pooled connection. The pool is already thread-safe.

    `commit=True` is required for any mutating call (write, claim,
    heartbeat). Read-only paths leave it False for autocommit semantics.
    """
    pool = _get_pool()
    conn = pool.getconn()
    cur = None
    try:
        if commit:
            conn.autocommit = False
        else:
            conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        if commit:
            conn.commit()
    except Exception:
        if commit:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        pool.putconn(conn, close=False)


def _ensure_schema(cur) -> None:
    """Confirm the schema is present; raise KanbanSchemaMissing if not.

    This is called lazily on first use. If the user has the memory
    plugin but not the kanban plugin, we fail fast with a clear message
    pointing to the install path.
    """
    schema = get_pg_kanban_schema()
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.schemata "
        "WHERE schema_name = %s) AS present",
        (schema,),
    )
    row = cur.fetchone()
    present = row["present"] if isinstance(row, dict) else (row[0] if row else False)
    if not present:
        raise KanbanSchemaMissing(
            f"Schema {schema!r} is missing. Run the bootstrap script to "
            f"create it, or set HERMES_KANBAN_SCHEMA to the right name."
        )


# ── Domain objects (mirror kanban_db.Task/Run/Comment/Event/Attachment) ──


class Task(dict):
    """Dict subclass so `asdict(task)` works in the dashboard plugin
    and `task["id"]` works in the CLI. We DON'T use dataclasses because
    the dispatcher code accesses these as dicts already."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value


# ── CRUD: Tenants ─────────────────────────────────────────────────────


def list_tenants() -> List[Dict[str, Any]]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor() as cur:
        _ensure_schema(cur)
        cur.execute(
            f"SELECT id, slug, display_name, created_at "
            f"FROM {schema}.tenants ORDER BY slug"
        )
        return list(cur.fetchall())


def upsert_tenant(slug: str, display_name: Optional[str] = None) -> Dict[str, Any]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(
            f"INSERT INTO {schema}.tenants (slug, display_name) "
            f"VALUES (%s, %s) "
            f"ON CONFLICT (slug) DO UPDATE "
            f"  SET display_name = COALESCE(EXCLUDED.display_name, {schema}.tenants.display_name) "
            f"RETURNING id, slug, display_name, created_at",
            (slug, display_name),
        )
        return cur.fetchone()


# ── CRUD: Tasks ───────────────────────────────────────────────────────


def create_task(
    title: str,
    body: str = "",
    *,
    priority: int = 5,
    assignee: Optional[str] = None,
    tenant: Optional[str] = None,
    parent_ids: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    workspace: Optional[str] = None,
    status: str = "ready",
    created_by: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a task. Pass `task_id` to preserve an existing ID (used by
    the SQLite → PG migration to keep `t_...` IDs stable across runs)."""
    schema = _schema_table(get_pg_kanban_schema())
    task_id = task_id or _new_task_id()
    now = datetime.now(timezone.utc)
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        # Resolve tenant slug → id (insert if unknown).
        tenant_id = None
        if tenant:
            cur.execute(
                f"SELECT id FROM {schema}.tenants WHERE slug = %s", (tenant,)
            )
            row = cur.fetchone()
            if row:
                tenant_id = row["id"]
            else:
                cur.execute(
                    f"INSERT INTO {schema}.tenants (slug, display_name) "
                    f"VALUES (%s, %s) RETURNING id",
                    (tenant, tenant),
                )
                tenant_id = cur.fetchone()["id"]
        cur.execute(
            f"""
            INSERT INTO {schema}.tasks
                (id, title, body, status, priority, assignee, tenant_id,
                 metadata, workspace, created_by, created_at, updated_at,
                 consecutive_failures)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, 0)
            RETURNING *
            """,
            (task_id, title, body, status, priority, assignee, tenant_id,
             json.dumps(metadata or {}), workspace, created_by, now, now),
        )
        task = cur.fetchone()
        if parent_ids:
            for pid in parent_ids:
                cur.execute(
                    f"INSERT INTO {schema}.task_links (parent_id, child_id) "
                    f"VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (pid, task_id),
                )
        if tags:
            for tag in tags:
                cur.execute(
                    f"INSERT INTO {schema}.tags (name) VALUES (%s) "
                    f"ON CONFLICT (name) DO NOTHING",
                    (tag,),
                )
                cur.execute(
                    f"SELECT id FROM {schema}.tags WHERE name = %s", (tag,),
                )
                tag_id = cur.fetchone()["id"]
                cur.execute(
                    f"INSERT INTO {schema}.task_tags (task_id, tag_id) "
                    f"VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (task_id, tag_id),
                )
        # Append a 'created' event
        _append_event(cur, task_id, "created", created_by, {"title": title})
    return dict(task)


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor() as cur:
        _ensure_schema(cur)
        cur.execute(
            f"SELECT * FROM {schema}.tasks WHERE id = %s", (task_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_tasks(
    *,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    tenant: Optional[str] = None,
    parent_id: Optional[str] = None,
    child_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    order_by: str = "priority DESC, created_at",
) -> List[Dict[str, Any]]:
    schema = _schema_table(get_pg_kanban_schema())
    where: List[str] = []
    params: List[Any] = []
    if status:
        where.append("t.status = %s")
        params.append(status)
    if assignee is not None:
        where.append("t.assignee = %s")
        params.append(assignee)
    if tenant:
        where.append("ten.slug = %s")
        params.append(tenant)
    if search:
        where.append("t.body_tsv @@ plainto_tsquery('english', %s)")
        params.append(search)
    joins = [f"FROM {schema}.tasks t",
             f"LEFT JOIN {schema}.tenants ten ON t.tenant_id = ten.id"]
    if parent_id:
        joins.append(
            f"JOIN {schema}.task_links pl ON pl.child_id = t.id AND pl.parent_id = %s"
        )
        params.append(parent_id)
    if child_id:
        joins.append(
            f"JOIN {schema}.task_links cl ON cl.parent_id = t.id AND cl.child_id = %s"
        )
        params.append(child_id)
    if tags:
        joins.append(
            f"JOIN {schema}.task_tags tt ON tt.task_id = t.id "
            f"JOIN {schema}.tags tg ON tg.id = tt.tag_id "
            f"AND tg.name = ANY(%s)"
        )
        params.append(list(tags))
    sql = f"SELECT t.*, ten.slug AS tenant_slug {' '.join(joins)}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # order_by is whitelisted — never interpolate user input
    _SAFE_ORDERS = {
        "priority DESC, created_at", "priority ASC, created_at",
        "created_at DESC", "created_at ASC", "updated_at DESC",
    }
    if order_by not in _SAFE_ORDERS:
        order_by = "priority DESC, created_at"
    sql += f" ORDER BY {order_by} LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    with _cursor() as cur:
        _ensure_schema(cur)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def update_task(
    task_id: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[int] = None,
    assignee: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    workspace: Optional[str] = None,
    result: Optional[str] = None,
    expected_status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update a task. `expected_status` is a CAS guard."""
    schema = _schema_table(get_pg_kanban_schema())
    sets: List[str] = []
    params: List[Any] = []
    if title is not None:
        sets.append("title = %s")
        params.append(title)
    if body is not None:
        sets.append("body = %s")
        params.append(body)
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if priority is not None:
        sets.append("priority = %s")
        params.append(priority)
    if assignee is not None:
        sets.append("assignee = %s")
        params.append(assignee)
    if metadata is not None:
        sets.append("metadata = %s::jsonb")
        params.append(json.dumps(metadata))
    if workspace is not None:
        sets.append("workspace = %s")
        params.append(workspace)
    if result is not None:
        sets.append("result = %s")
        params.append(result)
    if not sets:
        return get_task(task_id)
    sets.append("updated_at = now()")
    sql = f"UPDATE {schema}.tasks SET {', '.join(sets)} WHERE id = %s"
    params.append(task_id)
    if expected_status is not None:
        sql += " AND status = %s"
        params.append(expected_status)
    sql += " RETURNING *"
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def delete_task(task_id: str) -> bool:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(
            f"DELETE FROM {schema}.tasks WHERE id = %s RETURNING id",
            (task_id,),
        )
        return cur.fetchone() is not None


# ── Claim / heartbeat / complete / fail (the dispatcher hot path) ─────


def claim_next(
    *,
    assignee: Optional[str] = None,
    worker_pid: Optional[int] = None,
    claim_ttl_seconds: int = 300,
) -> Optional[Dict[str, Any]]:
    """Race-free claim using SELECT FOR UPDATE SKIP LOCKED.

    Returns the claimed task, or None if no task was available.
    `worker_pid` is recorded for the eventual stale-claim reaper
    (future work; not needed when running inside a single-transaction
    dispatcher).
    """
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        # The CTE selects the highest-priority ready task that's not
        # assigned to someone else, locks it, and the UPDATE flips it
        # to running. SKIP LOCKED means concurrent workers never
        # double-claim.
        if assignee:
            cur.execute(
                f"""
                WITH next AS (
                    SELECT id FROM {schema}.tasks
                    WHERE status = 'ready'
                      AND (assignee IS NULL OR assignee = %s)
                    ORDER BY priority DESC, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE {schema}.tasks t
                SET status = 'running',
                    assignee = COALESCE(t.assignee, %s),
                    worker_pid = %s,
                    started_at = now(),
                    claim_expires_at = now() + (%s || ' seconds')::interval,
                    updated_at = now()
                FROM next WHERE t.id = next.id
                RETURNING t.*
                """,
                (assignee, assignee, worker_pid, str(claim_ttl_seconds)),
            )
        else:
            cur.execute(
                f"""
                WITH next AS (
                    SELECT id FROM {schema}.tasks
                    WHERE status = 'ready'
                    ORDER BY priority DESC, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE {schema}.tasks t
                SET status = 'running',
                    worker_pid = %s,
                    started_at = now(),
                    claim_expires_at = now() + (%s || ' seconds')::interval,
                    updated_at = now()
                FROM next WHERE t.id = next.id
                RETURNING t.*
                """,
                (worker_pid, str(claim_ttl_seconds)),
            )
        row = cur.fetchone()
        if row:
            _append_event(
                cur, row["id"], "claimed",
                assignee, {"worker_pid": worker_pid},
            )
        return dict(row) if row else None


def heartbeat_claim(
    task_id: str,
    *,
    worker_pid: Optional[int] = None,
    extend_seconds: int = 300,
) -> bool:
    """Extend a claim's TTL. Returns True if the heartbeat landed."""
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(
            f"""
            UPDATE {schema}.tasks
            SET claim_expires_at = now() + (%s || ' seconds')::interval,
                updated_at = now()
            WHERE id = %s AND status = 'running'
              AND (%s::int IS NULL OR worker_pid = %s)
            RETURNING id
            """,
            (str(extend_seconds), task_id, worker_pid, worker_pid),
        )
        return cur.fetchone() is not None


def complete_task(
    task_id: str,
    result: str,
    *,
    worker_pid: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        if worker_pid is not None:
            cur.execute(
                f"""
                UPDATE {schema}.tasks
                SET status = 'done', result = %s, completed_at = now(),
                    updated_at = now(), claim_expires_at = NULL,
                    worker_pid = NULL, consecutive_failures = 0
                WHERE id = %s AND status = 'running' AND worker_pid = %s
                RETURNING *
                """,
                (result, task_id, worker_pid),
            )
        else:
            cur.execute(
                f"""
                UPDATE {schema}.tasks
                SET status = 'done', result = %s, completed_at = now(),
                    updated_at = now(), claim_expires_at = NULL,
                    worker_pid = NULL, consecutive_failures = 0
                WHERE id = %s AND status = 'running'
                RETURNING *
                """,
                (result, task_id),
            )
        row = cur.fetchone()
        if row:
            _append_event(cur, task_id, "completed", None, {"result": result})
        return dict(row) if row else None


def fail_task(
    task_id: str,
    error: str,
    *,
    worker_pid: Optional[int] = None,
    requeue: bool = True,
) -> Optional[Dict[str, Any]]:
    """Mark a task as failed. If `requeue`, the task goes back to
    `ready` and `consecutive_failures` increments; the dispatcher's
    failure_limit decides whether to permanently cancel."""
    schema = _schema_table(get_pg_kanban_schema())
    target_status = "ready" if requeue else "failed"
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        if worker_pid is not None:
            cur.execute(
                f"""
                UPDATE {schema}.tasks
                SET status = %s,
                    result = %s,
                    consecutive_failures = consecutive_failures + 1,
                    claim_expires_at = NULL,
                    worker_pid = NULL,
                    updated_at = now()
                WHERE id = %s AND status = 'running' AND worker_pid = %s
                RETURNING *
                """,
                (target_status, error, task_id, worker_pid),
            )
        else:
            cur.execute(
                f"""
                UPDATE {schema}.tasks
                SET status = %s,
                    result = %s,
                    consecutive_failures = consecutive_failures + 1,
                    claim_expires_at = NULL,
                    worker_pid = NULL,
                    updated_at = now()
                WHERE id = %s AND status = 'running'
                RETURNING *
                """,
                (target_status, error, task_id),
            )
        row = cur.fetchone()
        if row:
            _append_event(cur, task_id, "failed", None,
                          {"error": error, "requeue": requeue})
        return dict(row) if row else None


# ── Comments, attachments, events, runs ──────────────────────────────


def list_comments(task_id: str) -> List[Dict[str, Any]]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor() as cur:
        _ensure_schema(cur)
        cur.execute(
            f"SELECT * FROM {schema}.task_comments "
            f"WHERE task_id = %s ORDER BY created_at",
            (task_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def add_comment(task_id: str, body: str, author: Optional[str] = None) -> Dict[str, Any]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(
            f"INSERT INTO {schema}.task_comments (task_id, body, author) "
            f"VALUES (%s, %s, %s) RETURNING *",
            (task_id, body, author),
        )
        return dict(cur.fetchone())


def list_events(task_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor() as cur:
        _ensure_schema(cur)
        cur.execute(
            f"SELECT * FROM {schema}.task_events "
            f"WHERE task_id = %s ORDER BY created_at DESC LIMIT %s",
            (task_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def list_runs(task_id: str) -> List[Dict[str, Any]]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor() as cur:
        _ensure_schema(cur)
        cur.execute(
            f"SELECT * FROM {schema}.task_runs "
            f"WHERE task_id = %s ORDER BY started_at DESC",
            (task_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def start_run(task_id: str, worker_pid: int, worker_label: Optional[str] = None) -> Dict[str, Any]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(
            f"INSERT INTO {schema}.task_runs "
            f"(task_id, worker_pid, worker_label, started_at) "
            f"VALUES (%s, %s, %s, now()) RETURNING *",
            (task_id, worker_pid, worker_label),
        )
        return dict(cur.fetchone())


def end_run(run_id: int, *, status: str, exit_code: Optional[int] = None) -> Optional[Dict[str, Any]]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(
            f"UPDATE {schema}.task_runs "
            f"SET ended_at = now(), status = %s, exit_code = %s "
            f"WHERE id = %s RETURNING *",
            (status, exit_code, run_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _append_event(cur, task_id: str, kind: str, actor: Optional[str], payload: Dict[str, Any]) -> None:
    schema = _schema_table(get_pg_kanban_schema())
    cur.execute(
        f"INSERT INTO {schema}.task_events (task_id, kind, actor, payload) "
        f"VALUES (%s, %s, %s, %s::jsonb)",
        (task_id, kind, actor, json.dumps(payload)),
    )
    # Also fire a NOTIFY so the dashboard WebSocket can broadcast.
    try:
        cur.execute(
            f"NOTIFY hermes_kanban_event, %s",
            (json.dumps({"task_id": task_id, "kind": kind, "actor": actor}),),
        )
    except Exception:
        # NOTIFY is best-effort; failure shouldn't break the write.
        pass


# ── Task links (parent/child) ───────────────────────────────────────


def list_children(parent_id: str) -> List[Dict[str, Any]]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor() as cur:
        _ensure_schema(cur)
        cur.execute(
            f"SELECT t.* FROM {schema}.task_links l "
            f"JOIN {schema}.tasks t ON t.id = l.child_id "
            f"WHERE l.parent_id = %s ORDER BY t.priority DESC, t.created_at",
            (parent_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def list_parents(child_id: str) -> List[Dict[str, Any]]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor() as cur:
        _ensure_schema(cur)
        cur.execute(
            f"SELECT t.* FROM {schema}.task_links l "
            f"JOIN {schema}.tasks t ON t.id = l.parent_id "
            f"WHERE l.child_id = %s ORDER BY t.priority DESC, t.created_at",
            (child_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def link_tasks(parent_id: str, child_id: str) -> bool:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(
            f"INSERT INTO {schema}.task_links (parent_id, child_id) "
            f"VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING parent_id",
            (parent_id, child_id),
        )
        return cur.fetchone() is not None


def unlink_tasks(parent_id: str, child_id: str) -> bool:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(
            f"DELETE FROM {schema}.task_links "
            f"WHERE parent_id = %s AND child_id = %s RETURNING parent_id",
            (parent_id, child_id),
        )
        return cur.fetchone() is not None


# ── Attachments (metadata only; file blobs go in hermes_memory blobs) ──


def add_attachment(
    task_id: str, filename: str, mime: str, size: int, path: str,
    *, author: Optional[str] = None,
) -> Dict[str, Any]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(
            f"INSERT INTO {schema}.task_attachments "
            f"(task_id, filename, mime, size, path, author) "
            f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (task_id, filename, mime, size, path, author),
        )
        return dict(cur.fetchone())


def list_attachments(task_id: str) -> List[Dict[str, Any]]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor() as cur:
        _ensure_schema(cur)
        cur.execute(
            f"SELECT * FROM {schema}.task_attachments "
            f"WHERE task_id = %s ORDER BY created_at DESC",
            (task_id,),
        )
        return [dict(r) for r in cur.fetchall()]


# ── Notify subs (cross-process pub/sub for the dashboard) ──────────


def subscribe(task_id: str, channel: str, *, filter_kind: Optional[str] = None) -> Dict[str, Any]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(
            f"INSERT INTO {schema}.notify_subs (task_id, channel, filter_kind) "
            f"VALUES (%s, %s, %s) "
            f"ON CONFLICT (task_id, channel) DO UPDATE "
            f"  SET filter_kind = EXCLUDED.filter_kind "
            f"RETURNING *",
            (task_id, channel, filter_kind),
        )
        return dict(cur.fetchone())


def unsubscribe(task_id: str, channel: str) -> bool:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        cur.execute(
            f"DELETE FROM {schema}.notify_subs "
            f"WHERE task_id = %s AND channel = %s RETURNING task_id",
            (task_id, channel),
        )
        return cur.fetchone() is not None


# ── Stats ────────────────────────────────────────────────────────────


def stats() -> Dict[str, Any]:
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor() as cur:
        _ensure_schema(cur)
        cur.execute(
            f"SELECT status, COUNT(*) AS n FROM {schema}.tasks GROUP BY status"
        )
        by_status: Dict[str, int] = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute(f"SELECT COUNT(*) AS n FROM {schema}.tasks")
        total = cur.fetchone()["n"]
        cur.execute(f"SELECT COUNT(*) AS n FROM {schema}.tenants")
        tenants = cur.fetchone()["n"]
    return {"total": total, "tenants": tenants, "by_status": by_status}


# ── Recompute 'ready' (the dispatcher's gatekeeper) ──────────────────


def recompute_ready() -> int:
    """A task with parents is `ready` only if all parents are `done`.

    Returns the number of tasks whose status changed.
    """
    schema = _schema_table(get_pg_kanban_schema())
    with _cursor(commit=True) as cur:
        _ensure_schema(cur)
        # Unblock: all parents done → ready
        cur.execute(
            f"""
            UPDATE {schema}.tasks t
            SET status = 'ready', updated_at = now()
            FROM {schema}.task_links l
            WHERE l.child_id = t.id AND t.status = 'blocked'
              AND NOT EXISTS (
                SELECT 1 FROM {schema}.task_links l2
                JOIN {schema}.tasks p ON p.id = l2.parent_id
                WHERE l2.child_id = t.id AND p.status <> 'done'
              )
            RETURNING t.id
            """
        )
        unblocked = len(cur.fetchall())
        # Re-block: a parent moved away from 'done' → blocked
        cur.execute(
            f"""
            UPDATE {schema}.tasks t
            SET status = 'blocked', updated_at = now()
            FROM {schema}.task_links l
            JOIN {schema}.tasks p ON p.id = l.parent_id
            WHERE l.child_id = t.id AND t.status = 'ready'
              AND p.status <> 'done'
            RETURNING t.id
            """
        )
        reblocked = len(cur.fetchall())
    return unblocked + reblocked
