"""CLI subcommands for the postgres memory provider.

Discovery convention: this file is auto-loaded by Hermes Agent's
plugin CLI discovery. The `register_cli(subparser)` function is the
entry point. Subcommands appear under `hermes postgres-memory <sub>`.

Subcommands
-----------
- status              — Show provider status (connection, table stats, live column)
- vector-column       — Show or set the live vector column
- backfill            — Run the backfill script (delegates to scripts/backfill_embeddings.py)
- finalize-cutover    — Drop the v1 column (irreversible; requires --yes)
- preflight           — Run pre-migration checks: ownership, schema, dim
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import List, Optional

import psycopg2
from psycopg2.extensions import make_dsn


def _conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "hermes"),
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("POSTGRES_DATABASE", "hermes"),
        connect_timeout=5,
        application_name="hermes-postgres-memory-cli",
    )


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)",
        (table,),
    )
    return cur.fetchone()[0]


def _column_dim(cur, table: str, column: str) -> Optional[int]:
    """Return the dim of a vector column, or None if missing."""
    cur.execute(
        """
        SELECT atttypmod
        FROM pg_attribute
        WHERE attrelid = %s::regclass AND attname = %s
        """,
        (table, column),
    )
    row = cur.fetchone()
    if not row or row[0] is None or row[0] < 0:
        return None
    return row[0]


# ── Subcommand handlers ──────────────────────────────────────────────────


def cmd_status(args, parser) -> int:
    """Print the provider status as JSON."""
    from plugins.memory.postgres import _PostgresClient, get_embedder
    try:
        client = _PostgresClient()
        # Trigger tool_status by calling the public path.
        # We re-implement a minimal version here so this CLI works
        # without an active provider instance.
        with client._cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            cur.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            v = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM agent_memory WHERE is_active = TRUE")
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT value FROM agent_memory_settings
                WHERE key = 'live_vector_column'
                """
            )
            live_row = cur.fetchone()
            live = live_row[0].strip('"') if live_row else "v1"
        embedder = get_embedder()
        print(json.dumps({
            "status": "connected",
            "postgres_version": version,
            "pgvector_version": v[0] if v else "not installed",
            "total_memories": total,
            "live_vector_column": live,
            "embedder": {
                "provider": embedder.provider,
                "model": embedder.model,
                "dim": embedder.dim,
                "stats": embedder.stats(),
            },
        }, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_vector_column(args, parser) -> int:
    """Show or set the live vector column."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            if not _table_exists(cur, "agent_memory_settings"):
                print("agent_memory_settings table does not exist; run migrations/001_add_v2_column.sql first.",
                      file=sys.stderr)
                return 2
            if args.set:
                if args.set not in ("v1", "v2"):
                    print(f"Invalid value: {args.set!r}. Use 'v1' or 'v2'.", file=sys.stderr)
                    return 2
                cur.execute(
                    """
                    INSERT INTO agent_memory_settings (key, value, updated_at)
                    VALUES ('live_vector_column', %s::jsonb, now())
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = now()
                    """,
                    (f'"{args.set}"',),
                )
                print(f"live_vector_column set to {args.set!r}")
            cur.execute(
                "SELECT value FROM agent_memory_settings WHERE key = 'live_vector_column'"
            )
            row = cur.fetchone()
            print(f"current: {row[0] if row else '(unset)'}")
        return 0
    finally:
        conn.close()


def cmd_backfill(args, parser) -> int:
    """Delegate to scripts/backfill_embeddings.py."""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "..", "scripts", "backfill_embeddings.py")
    script = os.path.normpath(script)
    cmd = [sys.executable, script]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.batch:
        cmd += ["--batch", str(args.batch)]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.column:
        cmd += ["--column", args.column]
    print(f"running: {' '.join(cmd)}", file=sys.stderr)
    return subprocess.call(cmd)


def cmd_finalize_cutover(args, parser) -> int:
    """Drop the v1 column. IRREVERSIBLE. Requires --yes."""
    if not args.yes:
        print("This will DROP content_vector (1536-dim) from agent_memory.", file=sys.stderr)
        print("This is IRREVERSIBLE. Re-run with --yes to confirm.", file=sys.stderr)
        return 2

    conn = _conn()
    try:
        with conn.cursor() as cur:
            if not _table_exists(cur, "agent_memory_settings"):
                print("agent_memory_settings table does not exist; nothing to finalize.",
                      file=sys.stderr)
                return 2
            cur.execute(
                "SELECT value FROM agent_memory_settings WHERE key = 'live_vector_column'"
            )
            row = cur.fetchone()
            live = row[0].strip('"') if row else None
            if live != "v2":
                print(f"live_vector_column is {live!r}, expected 'v2'.", file=sys.stderr)
                print("Run `hermes postgres-memory vector-column --set v2` first,",
                      file=sys.stderr)
                print("or run migrations/003_switch_live_column.sql.", file=sys.stderr)
                return 2

            # Pre-flight: confirm v2 has real embeddings for at least 99% of active rows.
            cur.execute(
                """
                SELECT
                    count(*) FILTER (WHERE content_vector_v2 IS NOT NULL
                                     AND content_vector_v2 <> array_fill(0, ARRAY[1024])::vector) AS embedded,
                    count(*) AS total
                FROM agent_memory WHERE is_active = TRUE
                """
            )
            embedded, total = cur.fetchone()
            if total == 0:
                print("No active rows; safe to drop v1 column.", file=sys.stderr)
            elif embedded / total < 0.99:
                print(
                    f"Refusing: only {embedded}/{total} ({100*embedded/total:.1f}%) rows "
                    f"have real v2 embeddings. Run backfill first.",
                    file=sys.stderr,
                )
                return 3

            print("Dropping idx_memory_vector_hnsw (if any)...")
            cur.execute("DROP INDEX IF EXISTS idx_memory_vector_hnsw")
            print("Dropping content_vector (1536-dim)...")
            cur.execute("ALTER TABLE agent_memory DROP COLUMN IF EXISTS content_vector")
        conn.commit()
        print("Cutover complete. Only content_vector_v2 (1024-dim) remains.")
        return 0
    finally:
        conn.close()


def cmd_preflight(args, parser) -> int:
    """Run pre-migration checks: ownership, schema, dim."""
    conn = _conn()
    errors: List[str] = []
    try:
        with conn.cursor() as cur:
            # 1. Table exists?
            if not _table_exists(cur, "agent_memory"):
                errors.append("agent_memory table does not exist; create it first.")
                # No point continuing.
                print(json.dumps({"errors": errors}, indent=2))
                return 1
            # 2. Ownership
            cur.execute(
                """
                SELECT pg_get_userbyid(c.relowner) AS owner,
                       current_user AS me
                FROM pg_class c WHERE c.relname = 'agent_memory'
                """
            )
            owner, me = cur.fetchone()
            if owner != me:
                errors.append(
                    f"agent_memory is owned by {owner!r}, not {me!r}. "
                    f"Run migrations/000_grant_ddl_to_hermes.sql as a superuser."
                )
            # 3. Vector column dims
            v1_dim = _column_dim(cur, "agent_memory", "content_vector")
            v2_dim = _column_dim(cur, "agent_memory", "content_vector_v2")
            # 4. Settings table
            has_settings = _table_exists(cur, "agent_memory_settings")
            # 5. Live column
            live = None
            if has_settings:
                cur.execute(
                    "SELECT value FROM agent_memory_settings WHERE key = 'live_vector_column'"
                )
                row = cur.fetchone()
                if row:
                    live = row[0].strip('"')
            # 6. Embedder
            from plugins.memory.postgres import get_embedder
            embedder = get_embedder()
        print(json.dumps({
            "ok": len(errors) == 0,
            "owner": owner,
            "current_user": me,
            "v1_dim": v1_dim,
            "v2_dim": v2_dim,
            "settings_table": has_settings,
            "live_column": live,
            "embedder_dim": embedder.dim,
            "errors": errors,
        }, indent=2))
        return 0 if not errors else 1
    finally:
        conn.close()


# ── Argparse wiring ──────────────────────────────────────────────────────


def register_cli(subparser) -> None:
    """Build the `hermes postgres-memory` argparse tree.

    Called by discover_plugin_cli_commands() at argparse setup time.
    """
    p = subparser.add_parser(
        "postgres-memory",
        help="PostgreSQL memory provider commands",
    )
    subs = p.add_subparsers(dest="postgres_memory_command")

    s_status = subs.add_parser("status", help="Show provider status")
    s_status.set_defaults(func=cmd_status)

    s_vc = subs.add_parser("vector-column", help="Show or set the live vector column")
    s_vc.add_argument("--set", choices=["v1", "v2"], help="Set the live column")
    s_vc.set_defaults(func=cmd_vector_column)

    s_bf = subs.add_parser("backfill", help="Run the backfill script")
    s_bf.add_argument("--dry-run", action="store_true",
                      help="Count rows that would be embedded; no writes.")
    s_bf.add_argument("--batch", type=int, help="Rows per embed batch (default: 32).")
    s_bf.add_argument("--limit", type=int, help="Stop after N rows (0 = no limit).")
    s_bf.add_argument("--column", help="Vector column to backfill (default: content_vector_v2).")
    s_bf.set_defaults(func=cmd_backfill)

    s_cut = subs.add_parser(
        "finalize-cutover",
        help="Drop the v1 (1536-dim) column. IRREVERSIBLE. Requires --yes.",
    )
    s_cut.add_argument("--yes", action="store_true", required=True,
                       help="Confirm you understand this is irreversible.")
    s_cut.set_defaults(func=cmd_finalize_cutover)

    s_pre = subs.add_parser("preflight", help="Pre-migration checks")
    s_pre.set_defaults(func=cmd_preflight)

    p.set_defaults(func=lambda args, parser: p.print_help() or 1)
