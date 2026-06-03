#!/usr/bin/env python3
"""Verify that the PostgreSQL memory embedding pipeline is working.

Greenfield schema only. Uses PG_MEM_DB_CONN_STR and vector_<dim> columns.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parents[3] / "plugins" / "memory" / "postgres"
if PLUGIN_DIR.exists() and str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

try:
    from embedder import get_embedder
except Exception:  # pragma: no cover - alternate installed layout
    from plugins.memory.postgres.embedder import get_embedder

import psycopg2
from psycopg2.extensions import make_dsn

SUPPORTED_DIMS = {768, 1024, 1536}


def _source_env() -> None:
    env = Path.home() / ".hermes" / ".env"
    if not env.exists():
        return
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _normalize_dsn(raw: str) -> str:
    raw = raw.strip()
    if ";" not in raw or "=" not in raw.split(";", 1)[0]:
        return raw
    mapping = {
        "host": "host", "server": "host", "port": "port",
        "database": "dbname", "dbname": "dbname",
        "user": "user", "username": "user", "userid": "user", "uid": "user",
        "password": "password", "pwd": "password", "sslmode": "sslmode",
    }
    kwargs = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        normalized = mapping.get(key.strip().replace(" ", "").lower())
        if normalized and value.strip():
            kwargs[normalized] = value.strip()
    return make_dsn(**kwargs) if kwargs else raw


def _connect():
    _source_env()
    dsn = os.environ.get("PG_MEM_DB_CONN_STR", "").strip()
    if not dsn:
        raise RuntimeError("PG_MEM_DB_CONN_STR is required")
    return psycopg2.connect(
        make_dsn(dsn=_normalize_dsn(dsn), connect_timeout=5, application_name="hermes-memory-verify")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", default="memory recall")
    parser.add_argument("--dim", type=int, default=int(os.environ.get("HERMES_EMBED_DEFAULT_DIM", "1024")))
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    if args.dim not in SUPPORTED_DIMS:
        print(f"FAIL: unsupported dim {args.dim}; expected one of {sorted(SUPPORTED_DIMS)}")
        return 2
    column = f"vector_{args.dim}"
    failures: list[str] = []

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  count(*) AS total,
                  count(*) FILTER (
                    WHERE {column} IS NOT NULL
                      AND {column} <> array_fill(0, ARRAY[%s])::vector
                  ) AS embedded
                FROM agent_memory
                WHERE is_active = TRUE
                """,
                (args.dim,),
            )
            total, embedded = cur.fetchone()
            ratio = 0.0 if total == 0 else (total - embedded) / total

            print(f"[1] Active memories:      {total}")
            print(f"[1] Embedded in {column}: {embedded}")
            print(f"[1] Missing/zero vectors: {total - embedded} ({ratio * 100:.1f}%)")
            if total > 0 and embedded == 0:
                failures.append(f"No active rows have non-zero {column} vectors.")
            elif total > 0 and ratio > 0.5:
                failures.append(f"More than 50% of active rows are missing/zero in {column}.")

            embedder = get_embedder(args.dim)
            qvec = embedder.embed(args.probe)
            cur.execute(
                f"""
                WITH fts_candidates AS (
                  SELECT id, content, {column} AS embedding_vector,
                         ts_rank(to_tsvector('english', content),
                                 plainto_tsquery('english', %s)) AS text_rank
                  FROM agent_memory
                  WHERE is_active = TRUE
                    AND {column} IS NOT NULL
                    AND to_tsvector('english', content) @@ plainto_tsquery('english', %s)
                  ORDER BY text_rank DESC
                  LIMIT %s
                )
                SELECT id, content, text_rank,
                       1 - (embedding_vector <=> %s::vector) AS vector_sim
                FROM fts_candidates
                ORDER BY 1 - (embedding_vector <=> %s::vector) DESC
                LIMIT %s
                """,
                (args.probe, args.probe, args.top_k * 4, qvec, qvec, args.top_k),
            )
            rows = cur.fetchall()
            print(f"\n[2] Hybrid search for {args.probe!r}")
            if not rows:
                print("[2] No FTS matches; vector path could not be exercised.")
                if embedded == 0:
                    failures.append("Hybrid search returned nothing and no rows are embedded.")
            else:
                any_vector = False
                for rid, content, text_rank, vector_sim in rows:
                    print(
                        f"    {str(rid)[:8]} text_rank={float(text_rank):.4f} "
                        f"vector_sim={float(vector_sim):.4f} {content[:60]!r}"
                    )
                    if vector_sim and vector_sim > 0.0:
                        any_vector = True
                if not any_vector:
                    failures.append("Hybrid search rows all had zero vector similarity.")
    finally:
        conn.close()

    stats = get_embedder(args.dim).stats()
    print(f"\n[3] Embedder stats: {stats}")
    if stats.get("zero_fallbacks", 0) > 0:
        failures.append(f"Embedder fell back to zero vectors {stats['zero_fallbacks']} times.")
    if stats.get("errors", 0) > 0:
        failures.append(f"Embedder reports {stats['errors']} errors.")

    print()
    if failures:
        print("FAIL: embeddings are not verified:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("OK: embeddings are wired up and search returns non-zero scores.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
