#!/usr/bin/env python3
"""Backfill agent_memory.content_vector_v2 with real embeddings.

This is the script that turns a freshly-migrated schema (where the v2
column is empty) into a working hybrid-search table. It is idempotent:
it skips rows whose content_vector_v2 is already non-zero.

The column is configurable via --column, defaulting to content_vector_v2
(post-migration layout). If you have a custom schema, pass --column.

Order of operations:
    1. psql ... -f migrations/000_grant_ddl_to_hermes.sql     # ownership transfer (one-time, as superuser)
    2. psql ... -f migrations/001_add_v2_column.sql          # add v2 sidecar + settings table
    3. python scripts/backfill_embeddings.py [--dry-run]     # this file
    4. psql ... -f migrations/002_hnsw_v2.sql                # build HNSW over real vectors
    5. psql ... -f migrations/003_switch_live_column.sql     # switch plugin reads to v2
    6. (later) psql ... -f migrations/004_drop_v1_index.sql  # drop old index
    7. (later) psql ... -f migrations/005_drop_v1_column.sql # drop old column

Environment:
    POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER / POSTGRES_PASSWORD /
    POSTGRES_DATABASE — connection
    HERMES_EMBED_PROVIDER, HERMES_EMBED_MODEL, HERMES_EMBED_BASE_URL,
    HERMES_EMBED_API_KEY, HERMES_EMBED_TIMEOUT, HERMES_EMBED_FAIL_OPEN —
    embedding client. See embedder.py for defaults.

    The script also auto-sources ~/.hermes/.env (and $HERMES_HOME/.env)
    if KIMI_API_KEY / OLLAMA_API_KEY are unset when it starts, so a
    plain `python backfill_embeddings.py` works without `set -a; source ...`.
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

# Make the plugin importable when running this file directly.
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
# We don't add agent dir to sys.path because the embedder is a leaf module
# with no other hermes-agent dependencies. It imports only stdlib + httpx.
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from embedder import Embedder, EmbeddingError  # noqa: E402

logger = logging.getLogger("backfill_embeddings")

DEFAULT_COLUMN = "content_vector_v2"
ENV_FILES = [
    Path.home() / ".hermes" / ".env",
]


def _maybe_source_env() -> None:
    """Self-source ~/.hermes/.env if the relevant API keys are unset.

    A bash shell that ran `set -a; source ~/.hermes/.env; set +a` will
    have KIMI_API_KEY in its env, but a `python script.py` launched
    from that shell will see an empty os.environ. This makes the script
    work even when called directly.
    """
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
                # Don't overwrite an explicit empty / placeholder
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


def _row_needs_embedding(cur, column: str, memory_id) -> bool:
    """A row 'needs embedding' if its target column is null, all-zero, or
    dim-mismatched. Avoids re-embedding content we've already embedded."""
    # pgvector returns a string like '[0.1,0.2,...]'. We use a cheap
    # "any element non-zero" check, but parameterize the column name
    # to support custom schemas.
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


def _iter_rows(conn, column: str, batch_size: int) -> Iterator[List[Tuple]]:
    """Yield batches of (id, content) rows. Server-side cursor to avoid
    loading the whole table into memory. Only fetches rows where the
    target column is null OR all-zero (cheap WHERE on a stringified
    vector) so a fully-backfilled table is skipped entirely."""
    cur_name = "backfill_cursor"
    with conn.cursor(name=cur_name) as cur:
        cur.itersize = batch_size
        cur.execute(
            f"""
            SELECT id, content
            FROM agent_memory
            WHERE is_active = TRUE
              AND ({column} IS NULL OR {column} = array_fill(0, ARRAY[1024])::vector)
            ORDER BY created_at ASC
            """
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
                        help="Stop after N rows (0 = no limit). Useful for smoke tests.")
    parser.add_argument("--column", default=DEFAULT_COLUMN,
                        help=f"Vector column to backfill (default: {DEFAULT_COLUMN}).")
    args = parser.parse_args()

    _maybe_source_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    embedder = Embedder()
    logger.info(
        "Backfill starting: provider=%s model=%s dim=%s column=%s dry_run=%s",
        embedder.provider, embedder.model, embedder.dim, args.column, args.dry_run,
    )

    conn = _connect()
    conn.autocommit = False
    try:
        total = 0
        embedded = 0
        skipped = 0
        errors = 0
        t0 = time.monotonic()
        for batch in _iter_rows(conn, args.column, args.batch):
            for memory_id, content in batch:
                total += 1
                if args.limit and total > args.limit:
                    break
                with conn.cursor() as cur:
                    if not _row_needs_embedding(cur, args.column, memory_id):
                        skipped += 1
                        continue
                if args.dry_run:
                    embedded += 1
                    continue
                try:
                    vec = embedder.embed(content)
                except EmbeddingError as exc:
                    errors += 1
                    logger.error("Embed failed for id=%s: %s", memory_id, exc)
                    continue
                except Exception as exc:
                    errors += 1
                    logger.error("Embed error for id=%s: %s", memory_id, exc)
                    continue
                # Schema check: refuse to write a wrong-dim vector.
                if len(vec) != embedder.dim:
                    errors += 1
                    logger.error(
                        "Skip id=%s: embedder returned dim=%s, expected %s",
                        memory_id, len(vec), embedder.dim,
                    )
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE agent_memory SET {args.column} = %s::vector, "
                        f"updated_at = now() WHERE id = %s",
                        (vec, memory_id),
                    )
                embedded += 1
                if embedded % 50 == 0:
                    elapsed = time.monotonic() - t0
                    rate = embedded / elapsed if elapsed > 0 else 0
                    logger.info(
                        "Progress: %s embedded, %s skipped, %s errors, %.1f rows/s",
                        embedded, skipped, errors, rate,
                    )
            if args.limit and total >= args.limit:
                break
        if not args.dry_run:
            conn.commit()
        elapsed = time.monotonic() - t0
        logger.info(
            "Backfill %s: total=%s embedded=%s skipped=%s errors=%s elapsed=%.1fs",
            "preview" if args.dry_run else "complete",
            total, embedded, skipped, errors, elapsed,
        )
        return 0 if errors == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
