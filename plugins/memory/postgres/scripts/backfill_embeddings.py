#!/usr/bin/env python3
"""Backfill agent_memory per-dim vector columns with real embeddings.

Idempotent. Skips rows whose target column is already non-zero. The
default behavior is to backfill ALL supported dims (768, 1024, 1536)
so a single run gives you coverage at every dim the user might switch
to. Pass --dim to backfill a specific dim only.

For each (row, dim) where the target column is null or all-zero, the
script calls the embedder for that dim and writes the result.

Order of operations (fresh install):
    1. psql ... -f sql/000_schema.sql                  # creates all per-dim columns + indexes
    2. python scripts/backfill_embeddings.py           # this file (default: all dims)
    3. Use the plugin. (No need for `model-set` to switch dims; all
       three columns are populated. To make a different dim the
       default, run `hermes postgres-memory model-set --dim <dim>`.)

Order of operations (upgrade from pre-1.2.0):
    1. psql ... -f migrations/000_grant_ddl_to_hermes.sql  # one-time, as superuser
    2. psql ... -f migrations/001_add_per_dim_columns.sql  # adds per-dim columns
    3. psql ... -f migrations/002_hnsw_per_dim.sql          # builds HNSW on each
    4. psql ... -f migrations/003_migrate_legacy_content_vector.sql  # copies legacy data
    5. python scripts/backfill_embeddings.py [--dim 1024]   # populate the rest
    6. (later) psql ... -f migrations/004_drop_legacy_column.sql    # drop legacy col

Environment:
    POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER / POSTGRES_PASSWORD /
    POSTGRES_DATABASE — connection
    HERMES_EMBED_PROVIDER_<DIM> / HERMES_EMBED_MODEL_<DIM> /
    HERMES_EMBED_BASE_URL_<DIM> / HERMES_EMBED_API_KEY_<DIM> — per-dim
    HERMES_EMBED_FAIL_OPEN — global fail-open flag (default: 1)

The script also auto-sources ~/.hermes/.env if KIMI_API_KEY /
OLLAMA_API_KEY are unset when it starts, so a plain
`python backfill_embeddings.py` works without `set -a; source ...`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterator, List, Tuple

import psycopg2

# Add the plugin dir to sys.path so the embedder imports work.
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from embedder import Embedder, EmbeddingError, SUPPORTED_DIMS  # noqa: E402

logger = logging.getLogger("backfill_embeddings")

DEFAULT_DIMS = list(SUPPORTED_DIMS)  # backfill all by default
ENV_FILES = [Path.home() / ".hermes" / ".env"]


def _maybe_source_env() -> None:
    if os.environ.get("KIMI_API_KEY") or os.environ.get("OLLAMA_API_KEY") \
            or os.environ.get("HERMES_EMBED_API_KEY"):
        return
    for path in ENV_FILES:
        if not path.exists():
            continue
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if v and v != "***":
                    os.environ.setdefault(k, v)
        except OSError as exc:
            logger.debug("Could not read %s: %s", path, exc)


def _connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "hermes"),
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("POSTGRES_DATABASE", "hermes"),
        connect_timeout=5,
        application_name="hermes-memory-backfill",
    )


def _column_for_dim(d: int) -> str:
    return f"vector_{d}"


def _row_needs_embedding(cur, column: str, dim: int, memory_id) -> bool:
    """A row 'needs embedding' if its target column is null, all-zero, or
    dim-mismatched."""
    cur.execute(
        f"SELECT {column} FROM agent_memory WHERE id = %s",
        (memory_id,),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        return True
    s = row[0]
    if isinstance(s, str):
        body = s.strip("[]")
        if not body:
            return True
        for tok in body.split(","):
            tok = tok.strip()
            if tok and tok not in ("0", "0.0", "-0", "-0.0", "0e0", "0.0e0"):
                return False
        return True
    try:
        return all(float(x) == 0.0 for x in s)
    except Exception:
        return True


def _iter_rows(conn, column: str, dim: int, batch_size: int) -> Iterator[List[Tuple]]:
    """Yield batches of (id, content) rows where the target column needs
    filling. Server-side cursor to avoid loading the whole table."""
    cur_name = f"backfill_cursor_{dim}"
    with conn.cursor(name=cur_name) as cur:
        cur.itersize = batch_size
        cur.execute(
            f"""
            SELECT id, content
            FROM agent_memory
            WHERE is_active = TRUE
              AND ({column} IS NULL OR {column} = array_fill(0, ARRAY[%s])::vector)
            ORDER BY created_at ASC
            """,
            (dim,),
        )
        batch: List[Tuple] = []
        for row in cur:
            batch.append(row)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows that would be embedded; no writes.")
    parser.add_argument("--batch", type=int, default=32,
                        help="Rows per embed batch (default: 32).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N rows per dim (0 = no limit).")
    parser.add_argument("--dim", type=int, choices=DEFAULT_DIMS,
                        help="Backfill a specific dim only (default: all dims).")
    args = parser.parse_args()

    dims_to_process = [args.dim] if args.dim else DEFAULT_DIMS

    _maybe_source_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Initialize one embedder per dim. The embedder reads the SQL registry
    # via _read_model_config_for_dim when available; falls back to env.
    embedders: dict = {}
    for d in dims_to_process:
        # We try the SQL registry first; if it fails, fall back to env defaults.
        try:
            from plugins.memory.postgres import _read_model_config_for_dim
            cfg = _read_model_config_for_dim(d)
        except Exception:
            from embedder import _default_model_config_for_dim
            cfg = _default_model_config_for_dim(d)
        embedders[d] = Embedder(**cfg)
        logger.info(
            "Backfill plan: dim=%d provider=%s model=%s",
            d, embedders[d].provider, embedders[d].model,
        )

    conn = _connect()
    conn.autocommit = False
    overall_total = 0
    overall_errors = 0
    try:
        for d in dims_to_process:
            column = _column_for_dim(d)
            embedder = embedders[d]
            logger.info("=== Backfilling dim=%d (column=%s) ===", d, column)
            t0 = time.monotonic()
            total = 0
            embedded = 0
            skipped = 0
            errors = 0
            for batch in _iter_rows(conn, column, d, args.batch):
                for memory_id, content in batch:
                    total += 1
                    if args.limit and total > args.limit:
                        break
                    with conn.cursor() as cur:
                        if not _row_needs_embedding(cur, column, d, memory_id):
                            skipped += 1
                            continue
                    if args.dry_run:
                        embedded += 1
                        continue
                    try:
                        vec = embedder.embed(content)
                    except EmbeddingError as exc:
                        errors += 1
                        logger.error("Embed failed (dim=%d, id=%s): %s", d, memory_id, exc)
                        continue
                    except Exception as exc:
                        errors += 1
                        logger.error("Embed error (dim=%d, id=%s): %s", d, memory_id, exc)
                        continue
                    if len(vec) != d:
                        errors += 1
                        logger.error(
                            "Skip id=%s: embedder returned dim=%s, expected %s",
                            memory_id, len(vec), d,
                        )
                        continue
                    with conn.cursor() as cur:
                        cur.execute(
                            f"UPDATE agent_memory SET {column} = %s::vector, "
                            f"updated_at = now() WHERE id = %s",
                            (vec, memory_id),
                        )
                    embedded += 1
                    if embedded % 50 == 0:
                        elapsed = time.monotonic() - t0
                        rate = embedded / elapsed if elapsed > 0 else 0
                        logger.info(
                            "  dim=%d progress: %s embedded, %s skipped, %s errors, %.1f rows/s",
                            d, embedded, skipped, errors, rate,
                        )
                if args.limit and total >= args.limit:
                    break
            if not args.dry_run:
                conn.commit()
            elapsed = time.monotonic() - t0
            logger.info(
                "Backfill %s for dim=%d: total=%s embedded=%s skipped=%s errors=%s elapsed=%.1fs",
                "preview" if args.dry_run else "complete",
                d, total, embedded, skipped, errors, elapsed,
            )
            overall_total += total
            overall_errors += errors
        return 0 if overall_errors == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
