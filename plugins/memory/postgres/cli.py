"""CLI subcommands for the postgres memory provider.

Discovery convention: this file is auto-loaded by Hermes Agent's
plugin CLI discovery. The `register_cli(subparser)` function is the
entry point. Subcommands appear under `hermes postgres-memory <sub>`.

Subcommands
-----------
- status                — Show provider status
- model-list            — List per-dim model configs
- model-set             — Switch the default dim and/or model
- backfill              — Run the backfill script (per-dim, parallel)
- preflight             — Pre-migration checks (ownership, schema, dims)
- finalize-cutover      — Drop the legacy content_vector column (irreversible)
- vector-column         — Legacy: show/set the live vector column (deprecated)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import List, Optional

import psycopg2


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
    cur.execute(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = %s::regclass AND attname = %s",
        (table, column),
    )
    row = cur.fetchone()
    if not row or row[0] is None or row[0] < 0:
        return None
    return row[0]


# ── Subcommand handlers ──────────────────────────────────────────────────


def cmd_status(args, parser) -> int:
    """Print provider status as JSON."""
    from plugins.memory.postgres import (
        _PostgresClient, get_embedder, SUPPORTED_DIMS,
    )
    try:
        client = _PostgresClient()
        with client._cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            v = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM agent_memory WHERE is_active = TRUE")
            total = cur.fetchone()[0]
        per_dim = client.count_by_dim()
        embedders = {}
        for d in SUPPORTED_DIMS:
            try:
                e = get_embedder(d)
                embedders[str(d)] = {"provider": e.provider, "model": e.model, "stats": e.stats()}
            except Exception as exc:
                embedders[str(d)] = {"error": str(exc)}
        print(json.dumps({
            "status": "connected",
            "postgres_version": version,
            "pgvector_version": v[0] if v else "not installed",
            "total_memories": total,
            "default_dim": client.default_dim,
            "per_dim_embedded": per_dim,
            "embedders": embedders,
        }, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_model_list(args, parser) -> int:
    """List the per-dim model registry (agent_memory_models)."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            if not _table_exists(cur, "agent_memory_models"):
                print("agent_memory_models table does not exist; run sql/000_schema.sql first.",
                      file=sys.stderr)
                return 2
            cur.execute(
                "SELECT dim, provider, model, base_url, api_key_env, updated_at "
                "FROM agent_memory_models ORDER BY dim"
            )
            rows = cur.fetchall()
        # Pretty print as a table
        print(f"{'dim':<6} {'provider':<14} {'model':<32} {'api_key_env':<20}")
        print("-" * 78)
        for r in rows:
            print(f"{r[0]:<6} {r[1]:<14} {r[2]:<32} {r[4] or '':<20}")
        # Also print the current default_dim
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM agent_memory_settings WHERE key = 'default_dim'"
            )
            row = cur.fetchone()
        if row:
            print(f"\ndefault_dim: {row[0]}")
        return 0
    finally:
        conn.close()


def cmd_model_set(args, parser) -> int:
    """Switch the default dim and/or override the model for that dim.

    Examples:
        hermes postgres-memory model-set --dim 768
        hermes postgres-memory model-set --dim 1024 --provider kimi --model bge_m3_embed
        hermes postgres-memory model-set --dim 1536 --provider kimi --model text-embedding-3-small
    """
    if args.dim not in (768, 1024, 1536):
        print(f"Invalid --dim: {args.dim}. Use 768, 1024, or 1536.", file=sys.stderr)
        return 2
    conn = _conn()
    try:
        with conn.cursor() as cur:
            if not _table_exists(cur, "agent_memory_models"):
                print("agent_memory_models table does not exist; run sql/000_schema.sql first.",
                      file=sys.stderr)
                return 2
            # Update the model registry row if --provider or --model given
            if args.provider or args.model:
                cur.execute(
                    "UPDATE agent_memory_models SET "
                    "  provider = COALESCE(%s, provider), "
                    "  model = COALESCE(%s, model), "
                    "  updated_at = now() "
                    "WHERE dim = %s RETURNING provider, model",
                    (args.provider, args.model, args.dim),
                )
                row = cur.fetchone()
                if row:
                    new_provider, new_model = row
                else:
                    # Insert a new row for this dim
                    cur.execute(
                        "INSERT INTO agent_memory_models (dim, provider, model, api_key_env) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (dim) DO UPDATE SET "
                        "  provider = EXCLUDED.provider, model = EXCLUDED.model, "
                        "  updated_at = now() "
                        "RETURNING provider, model",
                        (args.dim, args.provider or "kimi", args.model or "bge_m3_embed",
                         "KIMI_API_KEY"),
                    )
                    new_provider, new_model = cur.fetchone()
            else:
                cur.execute(
                    "SELECT provider, model FROM agent_memory_models WHERE dim = %s",
                    (args.dim,),
                )
                row = cur.fetchone()
                if not row:
                    print(f"No model registered for dim {args.dim} and no overrides given.",
                          file=sys.stderr)
                    return 2
                new_provider, new_model = row
            # Update default_dim
            cur.execute(
                "UPDATE agent_memory_settings SET value = %s::jsonb, updated_at = now() "
                "WHERE key = 'default_dim' "
                "RETURNING value",
                (str(args.dim),),
            )
        conn.commit()
        # Drop the per-dim embedder singleton so the next call rebuilds from SQL
        from plugins.memory.postgres.embedder import reset_embedder
        reset_embedder(args.dim)
        print(f"✓ default_dim set to {args.dim}")
        print(f"  model: provider={new_provider!r}, model={new_model!r}")
        print()
        print("Next steps:")
        print(f"  1. New writes go to vector_{args.dim} automatically.")
        print(f"  2. Run `hermes postgres-memory backfill --dim {args.dim}` to populate")
        print(f"     the new dim for existing rows.")
        return 0
    finally:
        conn.close()


def cmd_backfill(args, parser) -> int:
    """Delegate to scripts/backfill_embeddings.py."""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.normpath(os.path.join(here, "..", "scripts", "backfill_embeddings.py"))
    cmd = [sys.executable, script]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.batch:
        cmd += ["--batch", str(args.batch)]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.dim:
        cmd += ["--dim", str(args.dim)]
    print(f"running: {' '.join(cmd)}", file=sys.stderr)
    return subprocess.call(cmd)


def cmd_finalize_cutover(args, parser) -> int:
    """Drop the legacy content_vector column. IRREVERSIBLE. Requires --yes."""
    if not args.yes:
        print("This will DROP content_vector (legacy dim) from agent_memory.", file=sys.stderr)
        print("This is IRREVERSIBLE. Re-run with --yes to confirm.", file=sys.stderr)
        return 2
    conn = _conn()
    try:
        with conn.cursor() as cur:
            if not _table_exists(cur, "agent_memory"):
                print("agent_memory table does not exist.", file=sys.stderr)
                return 2
            # Confirm at least one per-dim column has data
            for d in (768, 1024, 1536):
                col = f"vector_{d}"
                if not _table_exists(cur, col) and False:  # always false; we check column not table
                    pass
                cur.execute(
                    f"SELECT COUNT(*) FROM information_schema.columns "
                    f"WHERE table_name = 'agent_memory' AND column_name = %s",
                    (col,),
                )
                if cur.fetchone()[0]:
                    cur.execute(
                        f"SELECT COUNT(*) FROM agent_memory "
                        f"WHERE is_active = TRUE AND {col} IS NOT NULL "
                        f"AND {col} <> array_fill(0, ARRAY[%s])::vector",
                        (d,),
                    )
                    non_zero = cur.fetchone()[0]
                    if non_zero > 0:
                        cur.execute("SELECT COUNT(*) FROM agent_memory WHERE is_active = TRUE")
                        total = cur.fetchone()[0]
                        if total > 0 and non_zero / total < 0.5:
                            print(
                                f"Refusing: only {non_zero}/{total} rows have non-zero {col}.",
                                file=sys.stderr,
                            )
                            print(
                                f"Run `hermes postgres-memory backfill --dim {d}` first.",
                                file=sys.stderr,
                            )
                            return 3
                        break  # found a populated per-dim column
            else:
                print("No populated per-dim column found; refusing to drop legacy data.",
                      file=sys.stderr)
                return 3
            print("Dropping idx_memory_vector_hnsw (legacy)...")
            cur.execute("DROP INDEX IF EXISTS idx_memory_vector_hnsw")
            print("Dropping content_vector...")
            cur.execute("ALTER TABLE agent_memory DROP COLUMN IF EXISTS content_vector")
        conn.commit()
        print("Cutover complete. Only the per-dim vector columns remain.")
        return 0
    finally:
        conn.close()


def cmd_preflight(args, parser) -> int:
    """Run pre-migration checks."""
    from plugins.memory.postgres import SUPPORTED_DIMS
    conn = _conn()
    errors: List[str] = []
    try:
        with conn.cursor() as cur:
            if not _table_exists(cur, "agent_memory"):
                errors.append("agent_memory table does not exist; create it first.")
                print(json.dumps({"errors": errors}, indent=2))
                return 1
            # Ownership
            cur.execute(
                "SELECT pg_get_userbyid(c.relowner) AS owner, current_user AS me "
                "FROM pg_class c WHERE c.relname = 'agent_memory'"
            )
            owner, me = cur.fetchone()
            if owner != me:
                errors.append(
                    f"agent_memory is owned by {owner!r}, not {me!r}. "
                    f"Run migrations/000_grant_ddl_to_hermes.sql as a superuser."
                )
            # Per-dim columns
            dims_present: dict = {}
            for d in SUPPORTED_DIMS:
                col = f"vector_{d}"
                cur.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'agent_memory' AND column_name = %s",
                    (col,),
                )
                dims_present[d] = cur.fetchone() is not None
            # Legacy column
            legacy_dim = _column_dim(cur, "agent_memory", "content_vector")
            # Settings + models tables
            has_settings = _table_exists(cur, "agent_memory_settings")
            has_models = _table_exists(cur, "agent_memory_models")
            # default_dim
            default_dim = None
            if has_settings:
                cur.execute(
                    "SELECT value FROM agent_memory_settings WHERE key = 'default_dim'"
                )
                row = cur.fetchone()
                if row:
                    try:
                        default_dim = int(row[0])
                    except (TypeError, ValueError):
                        pass
            # Per-dim row counts
            per_dim_counts: dict = {}
            for d in SUPPORTED_DIMS:
                col = f"vector_{d}"
                if not dims_present[d]:
                    per_dim_counts[d] = None
                    continue
                cur.execute(
                    f"SELECT COUNT(*) FROM agent_memory "
                    f"WHERE is_active = TRUE AND {col} IS NOT NULL "
                    f"AND {col} <> array_fill(0, ARRAY[%s])::vector",
                    (d,),
                )
                per_dim_counts[d] = cur.fetchone()[0]
        print(json.dumps({
            "ok": len(errors) == 0,
            "owner": owner,
            "current_user": me,
            "default_dim": default_dim,
            "dims_present": dims_present,
            "per_dim_embedded": per_dim_counts,
            "legacy_content_vector_dim": legacy_dim,
            "settings_table": has_settings,
            "models_table": has_models,
            "errors": errors,
        }, indent=2))
        return 0 if not errors else 1
    finally:
        conn.close()


def cmd_vector_column(args, parser) -> int:
    """DEPRECATED in 1.2.0. Kept for backward compat — proxies to model-set."""
    if args.set:
        # Map v1/v2 to dim values
        if args.set == "v1":
            # Old v1 was 1536-dim. Map accordingly.
            print("DEPRECATED: --set v1 mapped to --dim 1536 (legacy 1536-dim).",
                  file=sys.stderr)
            args.dim = 1536
        elif args.set == "v2":
            # Old v2 was 1024-dim.
            print("DEPRECATED: --set v2 mapped to --dim 1024 (Kimi BGE-M3).",
                  file=sys.stderr)
            args.dim = 1024
        cmd_model_set(args, parser)
        return 0
    # Show mode
    conn = _conn()
    try:
        with conn.cursor() as cur:
            if not _table_exists(cur, "agent_memory_settings"):
                print("(no settings table)", file=sys.stderr)
                return 0
            cur.execute(
                "SELECT value FROM agent_memory_settings WHERE key = 'default_dim'"
            )
            row = cur.fetchone()
            print(f"default_dim: {row[0] if row else '(unset)'}")
        return 0
    finally:
        conn.close()


# ── Argparse wiring ──────────────────────────────────────────────────────


def register_cli(subparser) -> None:
    p = subparser.add_parser(
        "postgres-memory",
        help="PostgreSQL memory provider commands",
    )
    subs = p.add_subparsers(dest="postgres_memory_command")

    s_status = subs.add_parser("status", help="Show provider status")
    s_status.set_defaults(func=cmd_status)

    s_ml = subs.add_parser("model-list", help="List per-dim model configs")
    s_ml.set_defaults(func=cmd_model_list)

    s_ms = subs.add_parser(
        "model-set",
        help="Switch the default dim and/or override the model for that dim",
    )
    s_ms.add_argument("--dim", type=int, required=True, choices=[768, 1024, 1536],
                      help="New default dim")
    s_ms.add_argument("--provider", help="Override the embedder provider for this dim")
    s_ms.add_argument("--model", help="Override the model name for this dim")
    s_ms.set_defaults(func=cmd_model_set)

    s_bf = subs.add_parser("backfill", help="Run the backfill script")
    s_bf.add_argument("--dry-run", action="store_true",
                      help="Count rows that would be embedded; no writes.")
    s_bf.add_argument("--batch", type=int, help="Rows per embed batch (default: 32).")
    s_bf.add_argument("--limit", type=int, help="Stop after N rows (0 = no limit).")
    s_bf.add_argument("--dim", type=int, choices=[768, 1024, 1536],
                      help="Backfill a specific dim only (default: all dims).")
    s_bf.set_defaults(func=cmd_backfill)

    s_pre = subs.add_parser("preflight", help="Pre-migration checks")
    s_pre.set_defaults(func=cmd_preflight)

    s_cut = subs.add_parser(
        "finalize-cutover",
        help="Drop the legacy content_vector column. IRREVERSIBLE. Requires --yes.",
    )
    s_cut.add_argument("--yes", action="store_true", required=True,
                       help="Confirm you understand this is irreversible.")
    s_cut.set_defaults(func=cmd_finalize_cutover)

    # Deprecated: kept for backward compat with 1.1.0-era scripts.
    s_vc = subs.add_parser(
        "vector-column",
        help="DEPRECATED in 1.2.0. Use `model-set` instead.",
    )
    s_vc.add_argument("--set", choices=["v1", "v2"],
                      help="Mapped to --dim 1536 (v1) or --dim 1024 (v2)")
    s_vc.set_defaults(func=cmd_vector_column)

    p.set_defaults(func=lambda args, parser: p.print_help() or 1)
