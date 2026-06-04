"""CLI subcommands for the postgres kanban provider.

Discovery convention: this file is auto-loaded by Hermes Agent's
plugin CLI discovery. The `register_cli(subparser)` function is the
entry point. Subcommands appear under `hermes postgres-kanban <sub>`.

Subcommands
-----------
- status     — Show provider stats (task counts by status, tenants)
- migrate    — Read SQLite boards, write to hermes_kanban in PG
- preflight  — Schema/readiness checks (same shape as memory plugin)
"""

from __future__ import annotations

import json
import os
import sys
from typing import List

import psycopg2
from psycopg2.extensions import make_dsn


def _conn():
    """Build a psycopg2 connection from required PG_MEM_DB_CONN_STR."""
    from plugins.kanban.postgres import (
        get_pg_kanban_db_conn_str, _normalize_pg_mem_dsn,
    )
    return psycopg2.connect(
        make_dsn(
            dsn=get_pg_kanban_db_conn_str(),
            connect_timeout=5,
            application_name="hermes-postgres-kanban-cli",
        )
    )


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s)",
        (schema, table),
    )
    return cur.fetchone()[0]


def cmd_status(args, parser) -> int:
    """Print provider status as JSON."""
    from plugins.kanban.postgres import (
        stats as kanban_stats, get_pg_kanban_schema,
    )
    try:
        s = kanban_stats()
        print(json.dumps({
            "status": "connected",
            "schema": get_pg_kanban_schema(),
            **s,
        }, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_preflight(args, parser) -> int:
    """Run schema/readiness checks."""
    from plugins.kanban.postgres import get_pg_kanban_schema
    schema = get_pg_kanban_schema()
    conn = _conn()
    errors: List[str] = []
    try:
        with conn.cursor() as cur:
            # Schema exists
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.schemata "
                "WHERE schema_name = %s)",
                (schema,),
            )
            row = cur.fetchone()
            schema_exists = row[0] if row else False
            if not schema_exists:
                errors.append(f"Schema {schema!r} does not exist.")
                print(json.dumps({"errors": errors}, indent=2))
                return 1
            # All 8 tables present
            required = [
                "tenants", "tasks", "task_runs", "task_events",
                "task_links", "task_comments", "task_attachments",
                "notify_subs",
            ]
            present = {t: _table_exists(cur, schema, t) for t in required}
            missing = [t for t, ok in present.items() if not ok]
            if missing:
                errors.append(f"Missing tables in {schema}: {missing}")
            # Task counts by status
            cur.execute(
                f"SELECT status, COUNT(*) FROM {schema}.tasks GROUP BY status"
            )
            by_status = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute(f"SELECT COUNT(*) FROM {schema}.tenants")
            row = cur.fetchone()
            tenants = row[0] if row else 0
        print(json.dumps({
            "ok": len(errors) == 0,
            "schema": schema,
            "tables": present,
            "tasks_by_status": by_status,
            "tenants": tenants,
            "errors": errors,
        }, indent=2))
        return 0 if not errors else 1
    finally:
        conn.close()


# Re-export the migrate command so `hermes postgres-kanban migrate` works
from plugins.kanban.postgres.migrate import cmd_migrate  # noqa: E402


def register_cli(subparser) -> None:
    p = subparser.add_parser(
        "postgres-kanban",
        help="PostgreSQL kanban provider commands",
    )
    subs = p.add_subparsers(dest="postgres_kanban_command")

    s_status = subs.add_parser("status", help="Show provider status")
    s_status.set_defaults(func=cmd_status)

    s_mig = subs.add_parser(
        "migrate", help="Migrate SQLite kanban boards to Postgres",
    )
    s_mig.add_argument(
        "--boards-root", default="~/.hermes/kanban/boards",
        help="Path to the boards directory",
    )
    s_mig.add_argument(
        "--dry-run", action="store_true",
        help="Count rows without writing",
    )
    s_mig.add_argument(
        "--no-archive", action="store_true",
        help="Don't copy SQLite files to kanban.db.migrated.<ts>",
    )
    s_mig.set_defaults(func=cmd_migrate)

    s_pre = subs.add_parser("preflight", help="Schema/readiness checks")
    s_pre.set_defaults(func=cmd_preflight)

    p.set_defaults(func=lambda args, parser: p.print_help() or 1)
