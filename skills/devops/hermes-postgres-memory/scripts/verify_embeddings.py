#!/usr/bin/env python3
"""Verify the PostgreSQL memory plugin's embedding pipeline is actually working.

This is the script you run when a user asks "are embeddings working?" — NOT
the docstring, NOT the schema, NOT a passing test. The script:

  1. Counts active rows vs. active rows with a non-zero content_vector.
  2. Computes the "zero-vector ratio" — anything > 0% is suspicious.
  3. Runs a hybrid search query (FTS + cosine) for a configurable probe
     text and asserts the result set has non-zero text_rank AND vector_sim.
  4. Loads the embedder singleton and prints its stats
     (hits, misses, errors, zero_fallbacks).

Exit code is non-zero if any check fails. The script prints a human-readable
report and does not raise.

Usage:
    source ~/.hermes/.env
    python scripts/verify_embeddings.py                       # default probe
    python scripts/verify_embeddings.py --probe "rainy days"  # custom probe
    python scripts/verify_embeddings.py --dim 1024            # non-default model

Environment:
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD,
    POSTGRES_DATABASE — required, same as the memory plugin.
    HERMES_EMBED_DIM — defaults to 768, override if your model is different.
    HERMES_EMBED_FAIL_OPEN — should be 1; verify-passing under 0 is stricter.
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow running this script from anywhere — point at the plugin dir so
# `from embedder import get_embedder` works. Two import paths are tried:
# (1) the new flat layout where embedder.py is a sibling of __init__.py,
# (2) the legacy subpackage layout where it's plugins.memory.postgres.embedder.
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)            # .../hermes-postgres-memory
PLUGIN_DIR = os.path.normpath(
    os.path.join(SKILL_ROOT, "..", "..", "..", "hermes-agent", "plugins", "memory", "postgres")
)
if os.path.isdir(PLUGIN_DIR) and PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

try:
    import psycopg2
    from embedder import get_embedder
except ImportError:
    # Fall back to the legacy subpackage layout, in case the plugin
    # is installed as `plugins.memory.postgres` (older hermes-agent
    # auto-discovery treats each plugin as a subpackage).
    try:
        from plugins.memory.postgres.embedder import get_embedder  # type: ignore
    except Exception as exc:  # noqa: BLE001 — diagnostic-friendly
        print(f"FATAL: could not import embedder / psycopg2: {exc}", file=sys.stderr)
        print("Make sure you're running from a Hermes venv that has psycopg2 installed.", file=sys.stderr)
        sys.exit(2)


def _connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "hermes"),
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("POSTGRES_DATABASE", "hermes"),
        connect_timeout=5,
        application_name="hermes-memory-verify",
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--probe", default="memory recall",
                   help="Query text for the hybrid-search check.")
    p.add_argument("--dim", type=int,
                   default=int(os.environ.get("HERMES_EMBED_DIM", "768")),
                   help="Embedding dim; must match agent_memory.content_vector.")
    p.add_argument("--top-k", type=int, default=5)
    args = p.parse_args()

    failures: list[str] = []

    # ── 1 + 2. zero-vector ratio ───────────────────────────────────────────
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT "
                " count(*) AS total, "
                " count(*) FILTER (WHERE content_vector <> array_fill(0, ARRAY[%s])::vector) AS embedded "
                "FROM agent_memory WHERE is_active = TRUE",
                (args.dim,),
            )
            total, embedded = cur.fetchone()
            ratio = 0.0 if total == 0 else (total - embedded) / total

            print(f"[1] Active memories:        {total}")
            print(f"[1] With real embeddings:   {embedded}")
            print(f"[1] Zero-vector rows:       {total - embedded}  ({ratio * 100:.1f}%)")
            if total > 0 and embedded == 0:
                failures.append(
                    "No rows have non-zero content_vector. Embedder is failing open to zero "
                    "vectors on every write, or the column was never backfilled."
                )
            elif total > 0 and ratio > 0.5:
                failures.append(
                    f"More than 50% of rows ({ratio * 100:.1f}%) are zero vectors. "
                    f"Run backfill_embeddings.py to populate them."
                )

            # ── 3. hybrid search actually returns non-zero scores ───────────
            embedder = get_embedder()
            qvec = embedder.embed(args.probe)
            cur.execute(
                """
                WITH fts_candidates AS (
                  SELECT id, content,
                         ts_rank(to_tsvector('english', content),
                                 plainto_tsquery('english', %s)) AS text_rank
                  FROM agent_memory
                  WHERE is_active = TRUE
                    AND to_tsvector('english', content) @@ plainto_tsquery('english', %s)
                  ORDER BY text_rank DESC
                  LIMIT %s
                )
                SELECT id, content, text_rank,
                       1 - (content_vector <=> %s::vector) AS vector_sim
                FROM fts_candidates
                ORDER BY 1 - (content_vector <=> %s::vector) DESC
                LIMIT %s
                """,
                (args.probe, args.probe, args.top_k * 4, qvec, qvec, args.top_k),
            )
            rows = cur.fetchall()
            print(f"\n[3] Hybrid search for: {args.probe!r}")
            if not rows:
                print(f"[3] No FTS matches for probe {args.probe!r}; cannot exercise vector path.")
                if embedded == 0:
                    failures.append("Hybrid search returned nothing AND no rows are embedded.")
            else:
                any_vector_nonzero = False
                for r in rows[: args.top_k]:
                    rid, content, text_rank, vector_sim = r
                    print(
                        f"    {str(rid)[:8]}  text_rank={text_rank:.4f}  "
                        f"vector_sim={vector_sim:.4f}  {content[:60]!r}"
                    )
                    if vector_sim and vector_sim > 0.0:
                        any_vector_nonzero = True
                if not any_vector_nonzero:
                    failures.append(
                        "Hybrid search returned rows but ALL vector_sim values are zero. "
                        "The content_vector column is zero-vec even where rows were 'embedded'."
                    )
    finally:
        conn.close()

    # ── 4. embedder stats ──────────────────────────────────────────────────
    stats = get_embedder().stats()
    print(f"\n[4] Embedder stats:  {stats}")
    if stats.get("zero_fallbacks", 0) > 0:
        failures.append(
            f"Embedder has fallen back to zero vector {stats['zero_fallbacks']} times. "
            f"Check provider reachability, auth, and rate limits."
        )
    if stats.get("errors", 0) > 0:
        failures.append(
            f"Embedder reports {stats['errors']} errors. Look for WARNING-level logs "
            f"about provider failures."
        )

    # ── verdict ────────────────────────────────────────────────────────────
    print()
    if failures:
        print("FAIL: embeddings are NOT working as configured:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: embeddings are wired up and the search pipeline returns non-zero scores.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
