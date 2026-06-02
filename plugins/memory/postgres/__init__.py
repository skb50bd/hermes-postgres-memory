"""PostgreSQL memory plugin for Hermes Agent.

PostgreSQL + pgvector backed persistent memory. Uses the existing
agent_memory table with:

- Vector embeddings (1024 dims by default — BGE-M3 family) via pgvector
- HNSW index for fast similarity search
- Full-text search (GIN index on to_tsvector)
- Hybrid search combining vector + text relevance
- Categories, tags, JSONB metadata, TTL, soft deletes
- Non-destructive schema migration (sidecar v2 column)
- Auto-detects the live vector column at runtime — works whether you
  have a single 1024-dim content_vector, a sidecar content_vector_v2,
  or a legacy 1536-dim content_vector
- Pluggable embedder: kimi (free default), ollama_local, noop
- Content-addressable disk cache for embeddings (sha256 of provider|model|text)
- Fail-open embedder: provider errors fall back to zero vector; zero-fallback
  vectors are NOT cached (defense against cache poisoning)

Schema upgrade history
----------------------
- 1.0.x — agent_memory.content_vector vector(1536), zero-vector placeholder.
  Never actually used; the column was an unused scaffold.
- 1.1.0 — content_vector_v2 vector(1024) sidecar; content_vector retained
  for non-destructive upgrade. Run migrations 001..005 in order.
  After 005_drop_v1_column.sql, only content_vector_v2 remains.

Config via environment variables:
    POSTGRES_HOST      — Database host (default: localhost)
    POSTGRES_PORT      — Database port (default: 5432)
    POSTGRES_USER      — Database user (default: hermes)
    POSTGRES_PASSWORD  — Database password (required)
    POSTGRES_DATABASE  — Database name (default: hermes)
    HERMES_EMBED_PROVIDER — kimi, ollama_cloud, ollama_local, or noop
    HERMES_EMBED_MODEL    — model name (default: bge_m3_embed)
    HERMES_EMBED_DIM      — output dim (default: 1024)
    HERMES_EMBED_API_KEY  — API key (falls back to KIMI_API_KEY / OLLAMA_API_KEY)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import psycopg2
import psycopg2.pool
from psycopg2.extensions import make_dsn
from psycopg2.extras import register_uuid

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

# The embedder is a sibling of __init__.py inside the same plugin dir.
# When the plugin is installed under ~/.hermes/hermes-agent/plugins/memory/postgres/
# the standard `from embedder import ...` works because the plugin's
# parent package is on sys.path. When the plugin is loaded as a packaged
# module (e.g. by a test fixture) we add the plugin dir to sys.path first.
import sys as _sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
from embedder import Embedder, EmbeddingError, get_embedder  # noqa: E402

logger = logging.getLogger(__name__)

# Register UUID adapter for psycopg2
register_uuid()

# Module-level connection pool. Thread-safe via lock. We do NOT wrap each
# _PostgresClient in its own lock — the connection pool is already
# thread-safe, and a per-client lock would serialize all DB ops across
# the entire process, defeating the point of pooling.
_POOL = None
_POOL_LOCK = threading.Lock()

# How many rows the FTS pre-filter fetches before vector re-rank.
# Override at runtime with HERMES_POSTGRES_FTS_WINDOW if you have a
# very small or very large active row count.
_FTS_WINDOW_OVERFETCH = 4
_FTS_WINDOW_MIN = 40

# Hybrid-search blend: 0.5/0.5 (text vs vector). Override via
# HERMES_POSTGRES_HYBRID_TEXT_WEIGHT (0.0..1.0) if your workload
# is dominated by one or the other.
_HYBRID_TEXT_WEIGHT = 0.5


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    """Parse a float env setting, clamped to [minimum, maximum]."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default
    return max(minimum, min(maximum, v))


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default


def _postgres_dsn() -> str:
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "hermes")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    database = os.environ.get("POSTGRES_DATABASE", "hermes")

    if not password:
        raise RuntimeError("POSTGRES_PASSWORD is not set")

    connect_timeout = _env_int("HERMES_POSTGRES_CONNECT_TIMEOUT", 5, minimum=1)
    statement_timeout = _env_int("HERMES_POSTGRES_STATEMENT_TIMEOUT_MS", 10_000, minimum=100)
    idle_tx_timeout = _env_int("HERMES_POSTGRES_IDLE_TX_TIMEOUT_MS", 30_000, minimum=100)
    return make_dsn(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
        sslmode="prefer",
        connect_timeout=connect_timeout,
        application_name="hermes-memory-postgres",
        options=f"-c statement_timeout={statement_timeout} -c idle_in_transaction_session_timeout={idle_tx_timeout}",
    )


def _get_pool():
    """Return the process-local bounded connection pool."""
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            minconn = _env_int("HERMES_POSTGRES_POOL_MIN", 0, minimum=0)
            maxconn = _env_int("HERMES_POSTGRES_POOL_MAX", 2, minimum=1)
            if minconn > maxconn:
                logger.warning("HERMES_POSTGRES_POOL_MIN exceeds max; clamping min to %s", maxconn)
                minconn = maxconn
            _POOL = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, _postgres_dsn())
        return _POOL


def _close_pool() -> None:
    """Close all pooled PostgreSQL connections in this process."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            return
        _POOL.closeall()
        _POOL = None


# ---------------------------------------------------------------------------
# Vector column dispatch
# ---------------------------------------------------------------------------
#
# The plugin supports two layouts:
#
#   v1 (legacy 1.0.x): a single content_vector vector(1536) column. The
#       column held zero vectors; never actually used.
#   v2 (post-1.1.0 migration): a content_vector_v2 vector(1024) sidecar.
#       The original content_vector may still exist (pre-cutover) or may
#       have been dropped (post-cutover).
#
# At runtime the plugin reads agent_memory_settings.live_vector_column
# to determine which column to use. This is a per-process config knob
# that the migration can flip without restarting Hermes.
#
# The two columns are NEVER blended: a row is "in v2" if v2 is non-null,
# "in v1" otherwise. Hybrid search over a mixed table returns v2 rows
# first (vector re-rank on 1024-dim) and falls back to v1 rows (FTS only)
# in a separate pass.
#
# v1 layout returns hybrid search with vector_sim = NULL (the dim doesn't
# match the live embedder), so the v1 fallback uses text_rank only.

_V2_COLUMN = "content_vector_v2"
_VV1_COLUMN = "content_vector"  # legacy name when v1 dim happens to be 1024


def _live_column_name(live: str) -> str:
    """Resolve the runtime live_column to an actual column name on disk.

    Most users are in one of two states:
      - 'v1' (legacy 1536-dim)   → write/read content_vector
      - 'v2' (post-sidecar)      → write/read content_vector_v2

    A third state exists for users who already did a destructive 1024-dim
    migration (e.g. from the pre-1.1.0 code path) and have a single
    content_vector at 1024-dim. We treat them as 'v2' and write/read
    content_vector (the legacy name).
    """
    if live == "v2_named_v1":
        return _VV1_COLUMN
    if live == "v2":
        return _V2_COLUMN
    return _VV1_COLUMN


def _read_live_column(conn) -> str:
    """Determine which vector column the plugin should use.

    Returns one of: 'v1', 'v2'. Reads
    agent_memory_settings.live_vector_column; defaults to 'v1' if the
    settings table doesn't exist yet (fresh install) or the row is
    missing. Falls back to 'v1' for any error — we prefer a working
    plugin over a crashed one.

    If the settings table says 'v2' but the v2 column doesn't exist
    (e.g. the user is on the new layout with a single 1024-dim column
    named `content_vector`), auto-detect by inspecting which columns
    actually exist. This lets the plugin handle the three possible
    layouts without a forced migration:

      - legacy 1536-dim: only content_vector exists → 'v1'
      - sidecar v2:      both columns exist → 'v2' (per settings)
      - new layout:      only content_vector_v2 exists → 'v2'
      - mid-migration:   only content_vector exists at 1024-dim →
                         legacy destructive migration; treat as v2
                         (the column has the right dim, so cosine
                         similarity will work, but the name is v1)
    """
    # Step 1: ask the settings table what the user configured.
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM agent_memory_settings WHERE key = 'live_vector_column'"
            )
            row = cur.fetchone()
        configured = None
        if row and row[0] in ('"v1"', '"v2"'):
            configured = row[0].strip('"')
    except psycopg2.errors.UndefinedTable:
        # The settings table doesn't exist yet. New install or pre-1.1.0
        # upgrade. Need to clear the aborted-txn state before issuing
        # the next query on the same connection.
        try:
            conn.rollback()
        except Exception:
            pass
        configured = None
    except Exception as exc:
        logger.debug("live_column settings lookup failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        configured = None

    # Step 2: check which columns actually exist and their dims.
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name,
                       format_type(a.atttypid, a.atttypmod) AS vector_type
                FROM information_schema.columns c
                JOIN pg_attribute a
                  ON a.attrelid = (c.table_schema || '.' || c.table_name)::regclass
                 AND a.attname = c.column_name
                WHERE c.table_name = 'agent_memory'
                  AND c.column_name IN ('content_vector', 'content_vector_v2')
                """
            )
            rows = cur.fetchall()
        existing = {r[0] for r in rows}
        # Map column → dim (None for non-vector columns).
        dims: dict = {}
        for r in rows:
            col, vtype = r[0], r[1]
            if vtype and vtype.startswith("vector("):
                try:
                    dims[col] = int(vtype[7:-1])
                except ValueError:
                    pass
    except Exception as exc:
        logger.debug("live_column column inspection failed: %s", exc)
        return configured or "v1"

    # Step 3: reconcile.
    has_v2 = "content_vector_v2" in existing
    has_v1 = "content_vector" in existing
    v1_dim = dims.get("content_vector")

    if configured == "v2":
        if has_v2:
            return "v2"
        if has_v1 and v1_dim == 1024:
            # User said v2 but only v1 exists at 1024-dim. Probably
            # they did the destructive migration from the pre-1.1.0
            # code path. Treat as v2 with the legacy column name.
            logger.info(
                "live_vector_column='v2' but content_vector_v2 is missing; "
                "using content_vector (1024-dim) as the live column. "
                "Run the migration if you want to align with the new layout."
            )
            return "v2_named_v1"
    if configured == "v1" and not has_v1 and has_v2:
        return "v2"

    if has_v1 and not has_v2 and v1_dim == 1024:
        # Single content_vector at 1024-dim — this is the legacy
        # destructive migration. Auto-detect as v2.
        return "v2_named_v1"
    if has_v1 and not has_v2:
        return "v1"
    if has_v2 and not has_v1:
        return "v2"
    return configured or "v1"


# ---------------------------------------------------------------------------
# PostgreSQL client
# ---------------------------------------------------------------------------

class _PostgresClient:
    """Thin wrapper around psycopg2 for the agent_memory table.

    NOTE: this client is intentionally lock-free. The connection pool
    is already thread-safe; a per-client lock would serialize all DB
    ops and defeat the point of pooling. If you find yourself wanting
    a per-client lock, the right answer is probably a finer-grained
    lock around a specific shared state, not a blanket lock here.
    """

    def __init__(self):
        _get_pool()  # warm the pool at construction
        # Live column is per-process state. It changes rarely (only at
        # migration cutover), so we cache it on the client instance.
        with self._cursor() as cur:
            self._live_column = _read_live_column(cur.connection)
        logger.info("postgres-memory plugin using live_vector_column=%s", self._live_column)

    @property
    def live_column(self) -> str:
        return self._live_column

    def refresh_live_column(self) -> str:
        """Re-read the live column from the settings table. Called after
        migrations or from the cutover CLI."""
        with self._cursor() as cur:
            self._live_column = _read_live_column(cur.connection)
        return self._live_column

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        """Yield a fresh pooled cursor and always return the connection."""
        pool = _get_pool()
        conn = pool.getconn()
        cur = None
        try:
            conn.autocommit = True
            cur = conn.cursor()
            yield cur
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
            pool.putconn(conn, close=False)

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def add_memory(
        self,
        content: str,
        category: str = "fact",
        target: str = "memory",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
        confidence: int = 80,
        expires_at: Optional[datetime] = None,
    ) -> str:
        """Insert a memory. Returns the new memory ID.

        Writes to the live vector column (v1 or v2 depending on the
        runtime config). Embeds the content via the configured embedder;
        fail-open returns a zero vector if the provider is down.
        """
        column = _live_column_name(self._live_column)
        embedding = get_embedder().embed(content)

        with self._cursor() as cur:
            cur.execute("SELECT id FROM memory_categories WHERE name = %s", (category,))
            row = cur.fetchone()
            if row:
                category_id = row[0]
            else:
                cur.execute("SELECT id FROM memory_categories WHERE name = 'fact'")
                category_id = cur.fetchone()[0]

            memory_id = uuid.uuid4()
            now = datetime.now(timezone.utc)

            cur.execute(
                f"""
                INSERT INTO agent_memory
                (id, category_id, target, content, {column}, confidence, is_active,
                 created_at, updated_at, expires_at, tags, metadata, source_session)
                VALUES (%s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    memory_id,
                    category_id,
                    target,
                    content,
                    embedding,
                    confidence,
                    True,
                    now,
                    now,
                    expires_at,
                    tags or [],
                    json.dumps(metadata or {}),
                    None,
                ),
            )
            return str(memory_id)

    def search_memories(
        self,
        query: str,
        target: Optional[str] = None,
        category: Optional[str] = None,
        top_k: int = 10,
    ) -> List[Dict]:
        """Hybrid search: combines full-text search with vector similarity.

        Returns rows ordered by a weighted blend of ts_rank (text) and
        1 - cosine distance (vector). The live column is read at startup
        and cached on the client.

        For pre-cutover mixed tables (some rows in v2, some in v1), we
        run two passes: v2 with full hybrid scoring, then v1 with FTS
        only. Results are merged with v2 taking priority.
        """
        where_clauses = ["is_active = TRUE"]
        params: List[Any] = []
        if target:
            where_clauses.append("target = %s")
            params.append(target)
        if category:
            with self._cursor() as cur:
                cur.execute("SELECT id FROM memory_categories WHERE name = %s", (category,))
                row = cur.fetchone()
                if row:
                    where_clauses.append("category_id = %s")
                    params.append(row[0])
        where_sql = " AND ".join(where_clauses)

        text_weight = _env_float(
            "HERMES_POSTGRES_HYBRID_TEXT_WEIGHT", _HYBRID_TEXT_WEIGHT,
        )
        vector_weight = 1.0 - text_weight
        fts_window = max(top_k * _FTS_WINDOW_OVERFETCH, _FTS_WINDOW_MIN)

        with self._cursor() as cur:
            # If the live column is v1 at 1024-dim (the legacy destructive
            # migration case), use hybrid on that column directly. Otherwise
            # run the v2-hybrid + v1-FTS-fallback pair.
            live_col_name = _live_column_name(self._live_column)
            live_is_v1_named = (live_col_name == _VV1_COLUMN
                                and self._live_column == "v2_named_v1")

            if live_is_v1_named:
                # The legacy column is at 1024-dim, so we can run hybrid on it.
                embedding = get_embedder().embed(query)
                sql, sql_params = self._build_hybrid_sql(
                    column=_VV1_COLUMN,
                    query=query,
                    where_sql=where_sql,
                    where_params=list(params),
                    query_embedding=embedding,
                    fts_window=fts_window,
                    top_k=top_k,
                    text_weight=text_weight,
                    vector_weight=vector_weight,
                )
                cur.execute(sql, sql_params)
                return self._rows_to_dicts(cur.fetchall())[:top_k]

            results: List[Dict] = []

            # Pass 1: v2 (1024-dim) — full hybrid if the live column is v2.
            # Even if the live column is v1, we still query v2 because
            # some rows may have been backfilled.
            v2_query_embedding = get_embedder().embed(query)
            v2_sql, v2_params = self._build_hybrid_sql(
                column=_V2_COLUMN,
                query=query,
                where_sql=where_sql,
                where_params=list(params),
                query_embedding=v2_query_embedding,
                fts_window=fts_window,
                top_k=top_k,
                text_weight=text_weight,
                vector_weight=vector_weight,
            )
            cur.execute(v2_sql, v2_params)
            v2_rows = cur.fetchall()
            results.extend(self._rows_to_dicts(v2_rows))

            # If we're in v2-only mode, we're done.
            if self._live_column == "v2":
                return results[:top_k]

            # Pass 2: v1 (1536-dim). FTS only, no vector similarity
            # (the live embedder returns 1024-dim, which is dim-mismatched
            # against v1 and would corrupt the cosine score).
            v1_sql, v1_params = self._build_fts_only_sql(
                column=_VV1_COLUMN,
                query=query,
                where_sql=where_sql,
                where_params=list(params),
                fts_window=fts_window,
                top_k=top_k,
            )
            cur.execute(v1_sql, v1_params)
            v1_rows = cur.fetchall()
            v1_dicts = self._rows_to_dicts(v1_rows)
            # Merge: v2 first (already ranked), then v1 deduped by id.
            seen_ids = {r["id"] for r in results}
            for r in v1_dicts:
                if r["id"] not in seen_ids:
                    results.append(r)
            return results[:top_k]

    def _build_hybrid_sql(
        self,
        *,
        column: str,
        query: str,
        where_sql: str,
        where_params: list,
        query_embedding: list,
        fts_window: int,
        top_k: int,
        text_weight: float,
        vector_weight: float,
    ) -> tuple[str, list]:
        """Build the hybrid-search SQL for a given column. Returns
        (sql, params). The params list is the exact ordering the SQL
        expects; tests should assert on the length of this list to
        catch placeholder/param drift."""
        sql = f"""
            WITH fts_candidates AS (
                SELECT
                    m.id, m.target, m.content, m.created_at, m.tags, m.metadata,
                    m.{column} AS content_vector,
                    ts_rank(to_tsvector('english', m.content),
                            plainto_tsquery('english', %s)) AS text_rank
                FROM agent_memory m
                WHERE {where_sql}
                  AND m.{column} IS NOT NULL
                  AND to_tsvector('english', m.content) @@
                      plainto_tsquery('english', %s)
                ORDER BY text_rank DESC
                LIMIT %s
            )
            SELECT
                id, target, content, created_at, tags, metadata,
                text_rank,
                (1 - (content_vector <=> %s::vector)) AS vector_sim,
                ({text_weight} * COALESCE(text_rank, 0)
                 + {vector_weight} * COALESCE((1 - (content_vector <=> %s::vector)), 0)
                ) AS hybrid_score
            FROM fts_candidates
            ORDER BY hybrid_score DESC
            LIMIT %s
        """
        # Param ordering: where_params (target/category), then ts_rank,
        # then tsquery, then LIMIT, then vector_sim, then hybrid, then
        # outer LIMIT. The order MUST match the placeholder order above.
        params = list(where_params) + [
            query, query, fts_window,
            query_embedding, query_embedding, top_k,
        ]
        return sql, params

    def _build_fts_only_sql(
        self,
        *,
        column: str,
        query: str,
        where_sql: str,
        where_params: list,
        fts_window: int,
        top_k: int,
    ) -> tuple[str, list]:
        """Build an FTS-only SQL for the v1 column. Used during the
        pre-cutover period when some rows are still in v1 and we don't
        have a matching 1536-dim embedder to compute cosine similarity."""
        sql = f"""
            WITH fts_candidates AS (
                SELECT
                    m.id, m.target, m.content, m.created_at, m.tags, m.metadata,
                    m.{column} AS content_vector,
                    ts_rank(to_tsvector('english', m.content),
                            plainto_tsquery('english', %s)) AS text_rank
                FROM agent_memory m
                WHERE {where_sql}
                  AND m.{column} IS NOT NULL
                  AND to_tsvector('english', m.content) @@
                      plainto_tsquery('english', %s)
                ORDER BY text_rank DESC
                LIMIT %s
            )
            SELECT
                id, target, content, created_at, tags, metadata,
                text_rank,
                NULL::float AS vector_sim,
                text_rank AS hybrid_score
            FROM fts_candidates
            ORDER BY hybrid_score DESC
            LIMIT %s
        """
        params = list(where_params) + [query, query, fts_window, top_k]
        return sql, params

    def _rows_to_dicts(self, rows) -> List[Dict]:
        out = []
        for r in rows:
            out.append({
                "id": str(r[0]),
                "target": r[1],
                "content": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
                "tags": r[4],
                "metadata": r[5],
                "text_rank": float(r[6]) if r[6] is not None else 0.0,
                "vector_sim": float(r[7]) if r[7] is not None else None,
                "rank": float(r[8]) if r[8] is not None else 0.0,
            })
        return out

    def get_recent_memories(self, target: Optional[str] = None, limit: int = 20) -> List[Dict]:
        with self._cursor() as cur:
            if target:
                cur.execute(
                    """
                    SELECT id, target, content, created_at, tags, metadata
                    FROM agent_memory
                    WHERE is_active = TRUE AND target = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (target, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, target, content, created_at, tags, metadata
                    FROM agent_memory
                    WHERE is_active = TRUE
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            rows = cur.fetchall()

        return [
            {
                "id": str(r[0]),
                "target": r[1],
                "content": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
                "tags": r[4],
                "metadata": r[5],
            }
            for r in rows
        ]

    def remove_memory(self, memory_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE agent_memory SET is_active = FALSE, updated_at = %s WHERE id = %s",
                (datetime.now(timezone.utc), memory_id),
            )
            return cur.rowcount > 0

    def update_memory(self, memory_id: str, content: str) -> bool:
        column = _live_column_name(self._live_column)
        embedding = get_embedder().embed(content)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE agent_memory SET content = %s, {column} = %s::vector, "
                f"updated_at = %s WHERE id = %s",
                (content, embedding, datetime.now(timezone.utc), memory_id),
            )
            return cur.rowcount > 0

    def count_memories(self, target: Optional[str] = None) -> int:
        with self._cursor() as cur:
            if target:
                cur.execute(
                    "SELECT COUNT(*) FROM agent_memory WHERE is_active = TRUE AND target = %s",
                    (target,),
                )
            else:
                cur.execute("SELECT COUNT(*) FROM agent_memory WHERE is_active = TRUE")
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

REMEMBER_SCHEMA = {
    "name": "pg_remember",
    "description": (
        "Persist a fact, preference, or observation to the PostgreSQL vector memory store. "
        "Use for anything worth recalling across sessions: user preferences, environment facts, "
        "project conventions, lessons learned, workflow patterns."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to remember."},
            "category": {
                "type": "string",
                "enum": ["user_preference", "user_profile", "environment", "project_convention",
                         "tool_quirk", "lesson_learned", "workflow", "fact"],
                "description": "Category (default: fact).",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Target store (default: memory).",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for filtering.",
            },
        },
        "required": ["content"],
    },
}

SEARCH_SCHEMA = {
    "name": "pg_search",
    "description": (
        "Search the PostgreSQL memory store using full-text + semantic hybrid search. "
        "Returns ranked memories with relevance scores."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Filter by target store (optional).",
            },
            "category": {
                "type": "string",
                "enum": ["user_preference", "user_profile", "environment", "project_convention",
                         "tool_quirk", "lesson_learned", "workflow", "fact"],
                "description": "Filter by category (optional).",
            },
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
        },
        "required": ["query"],
    },
}

RECENT_SCHEMA = {
    "name": "pg_recent",
    "description": "List the most recently added memories from the PostgreSQL store.",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Filter by target store (optional).",
            },
            "limit": {"type": "integer", "description": "Max results (default: 20, max: 100)."},
        },
        "required": [],
    },
}

FORGET_SCHEMA = {
    "name": "pg_forget",
    "description": "Soft-delete a memory from the PostgreSQL store by its ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "The UUID of the memory to delete."},
        },
        "required": ["memory_id"],
    },
}

STATUS_SCHEMA = {
    "name": "pg_status",
    "description": "Check PostgreSQL memory store status — connection, table stats, pgvector version, live column, embedder health.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class PostgresMemoryProvider(MemoryProvider):
    """PostgreSQL vector memory via pgvector."""

    def __init__(self):
        self._client: Optional[_PostgresClient] = None
        self._session_id = ""
        # Per-process lock only around _client init (which warms the
        # connection pool and reads live column). All other operations
        # go through the pool's own thread-safety.

    @property
    def name(self) -> str:
        return "postgres"

    def is_available(self) -> bool:
        """Check if PostgreSQL is reachable and pgvector is installed."""
        try:
            client = _PostgresClient()
            with client._cursor() as cur:
                cur.execute("SELECT EXISTS (SELECT FROM pg_extension WHERE extname = 'vector')")
                has_vector = cur.fetchone()[0]
                cur.execute("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'agent_memory')")
                has_table = cur.fetchone()[0]
            return has_vector and has_table
        except Exception:
            return False

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "host",
                "description": "PostgreSQL host",
                "default": "localhost",
                "env_var": "POSTGRES_HOST",
            },
            {
                "key": "port",
                "description": "PostgreSQL port",
                "default": "5432",
                "env_var": "POSTGRES_PORT",
            },
            {
                "key": "user",
                "description": "PostgreSQL user",
                "default": "hermes",
                "env_var": "POSTGRES_USER",
            },
            {
                "key": "password",
                "description": "PostgreSQL password",
                "secret": True,
                "required": True,
                "env_var": "POSTGRES_PASSWORD",
            },
            {
                "key": "database",
                "description": "PostgreSQL database name",
                "default": "hermes",
                "env_var": "POSTGRES_DATABASE",
            },
            {
                "key": "embed_provider",
                "description": "Embedding provider: kimi, ollama_cloud, ollama_local, or noop",
                "default": "kimi",
                "env_var": "HERMES_EMBED_PROVIDER",
            },
            {
                "key": "embed_model",
                "description": "Embedding model name (default: bge_m3_embed for kimi)",
                "default": "bge_m3_embed",
                "env_var": "HERMES_EMBED_MODEL",
            },
            {
                "key": "embed_dim",
                "description": "Embedding output dim; must match the live vector column",
                "default": "1024",
                "env_var": "HERMES_EMBED_DIM",
            },
            {
                "key": "embed_base_url",
                "description": "Embedding provider API base URL (leave blank for provider default)",
                "default": "",
                "env_var": "HERMES_EMBED_BASE_URL",
            },
            {
                "key": "embed_api_key",
                "description": "Embedding provider API key (falls back to KIMI_API_KEY / OLLAMA_API_KEY)",
                "secret": True,
                "env_var": "HERMES_EMBED_API_KEY",
            },
            {
                "key": "embed_fail_open",
                "description": "Fall back to zero vector on provider errors (1=yes, 0=raise)",
                "default": "1",
                "env_var": "HERMES_EMBED_FAIL_OPEN",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        if self._client is None:
            self._client = _PostgresClient()

    def system_prompt_block(self) -> str:
        if not self._client:
            return ""
        try:
            count = self._client.count_memories()
            live = self._client.live_column
            return (
                f"# PostgreSQL Vector Memory\n"
                f"Active. {count} memories stored. pgvector with HNSW index, full-text search, hybrid retrieval.\n"
                f"Live vector column: {live} (1024-dim BGE-M3 family).\n"
                f"Use pg_remember to store facts, pg_search to recall, pg_recent to browse, pg_forget to remove, pg_status for diagnostics."
            )
        except Exception as e:
            logger.warning("Postgres system_prompt_block failed: %s", e)
            return "# PostgreSQL Vector Memory\nActive. Use pg_remember, pg_search, pg_recent, pg_forget, pg_status."

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant memories before each turn.

        This makes a live embed call on every turn. The content-
        addressable cache makes repeat queries free after the first
        call, but the first call has ~500ms latency on Kimi. Hermes
        may rate-limit short queries; we set a 5-character minimum
        to skip empty / short inputs.
        """
        if not self._client or not query or len(query.strip()) < 5:
            return ""
        try:
            results = self._client.search_memories(query.strip()[:500], top_k=5)
            if not results:
                return ""
            lines = ["## PostgreSQL Memory Context"]
            for r in results:
                tag_str = f" [{', '.join(r['tags'])}]" if r.get("tags") else ""
                lines.append(f"- [{r['target']}]{tag_str} {r['content'][:200]}")
            return "\n".join(lines)
        except Exception as e:
            logger.debug("Postgres prefetch failed: %s", e)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """No automatic turn sync — explicit pg_remember only."""
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [REMEMBER_SCHEMA, SEARCH_SCHEMA, RECENT_SCHEMA, FORGET_SCHEMA, STATUS_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._client:
            return tool_error("PostgreSQL memory provider not initialized")

        try:
            if tool_name == "pg_remember":
                return self._tool_remember(args)
            elif tool_name == "pg_search":
                return self._tool_search(args)
            elif tool_name == "pg_recent":
                return self._tool_recent(args)
            elif tool_name == "pg_forget":
                return self._tool_forget(args)
            elif tool_name == "pg_status":
                return self._tool_status()
            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            logger.error("Postgres tool %s failed: %s", tool_name, e)
            return tool_error(f"PostgreSQL tool '{tool_name}' failed: {e}")

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict] = None) -> None:
        """Mirror built-in memory writes to PostgreSQL."""
        if action not in {"add", "replace"} or not content or not self._client:
            return
        try:
            category = "user_profile" if target == "user" else "fact"
            self._client.add_memory(
                content=content,
                category=category,
                target=target,
                tags=["mirrored", "builtin"],
                metadata={"source": "builtin_memory_tool", "action": action, **(metadata or {})},
            )
        except Exception as e:
            logger.debug("Postgres memory mirror failed: %s", e)

    def shutdown(self) -> None:
        _close_pool()

    # -- Tool implementations ------------------------------------------------

    def _tool_remember(self, args: Dict[str, Any]) -> str:
        content = args.get("content", "").strip()
        if not content:
            return tool_error("content is required")

        category = args.get("category", "fact")
        target = args.get("target", "memory")
        tags = args.get("tags", [])

        memory_id = self._client.add_memory(
            content=content,
            category=category,
            target=target,
            tags=tags,
        )
        return json.dumps({
            "success": True,
            "memory_id": memory_id,
            "message": "Memory stored in PostgreSQL.",
        })

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "").strip()
        if not query:
            return tool_error("query is required")

        target = args.get("target")
        category = args.get("category")
        top_k = min(args.get("top_k", 10), 50)

        results = self._client.search_memories(query, target=target, category=category, top_k=top_k)
        if not results:
            return json.dumps({"results": [], "message": "No matching memories found."})

        return json.dumps({
            "results": results,
            "count": len(results),
        })

    def _tool_recent(self, args: Dict[str, Any]) -> str:
        target = args.get("target")
        limit = min(args.get("limit", 20), 100)

        results = self._client.get_recent_memories(target=target, limit=limit)
        return json.dumps({
            "results": results,
            "count": len(results),
        })

    def _tool_forget(self, args: Dict[str, Any]) -> str:
        memory_id = args.get("memory_id", "").strip()
        if not memory_id:
            return tool_error("memory_id is required")

        success = self._client.remove_memory(memory_id)
        return json.dumps({
            "success": success,
            "message": "Memory deleted." if success else "Memory not found.",
        })

    def _tool_status(self) -> str:
        host = os.environ.get("POSTGRES_HOST", "localhost")
        port = os.environ.get("POSTGRES_PORT", "5432")
        database = os.environ.get("POSTGRES_DATABASE", "hermes")

        with self._client._cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            vector_ver = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM agent_memory WHERE is_active = TRUE")
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT name, COUNT(*) FROM agent_memory JOIN memory_categories ON agent_memory.category_id = memory_categories.id WHERE is_active = TRUE GROUP BY name"
            )
            by_category = {r[0]: r[1] for r in cur.fetchall()}

            # Zero-vec count is per the live column, with fallback to v1.
            live = self._client.live_column
            zero_col = _live_column_name(live)
            cur.execute(
                "SELECT count(*) FROM agent_memory "
                "WHERE is_active = TRUE "
                f"AND {zero_col} = array_fill(0, ARRAY[1024])::vector",
            )
            zero_vec_count = cur.fetchone()[0]

        try:
            embedder = get_embedder()
            embedder_info = {
                "provider": embedder.provider,
                "model": embedder.model,
                "dim": embedder.dim,
                "stats": embedder.stats(),
            }
        except Exception as exc:
            embedder_info = {"error": str(exc)}

        return json.dumps({
            "status": "connected",
            "host": f"{host}:{port}/{database}",
            "postgres_version": version,
            "pgvector_version": vector_ver[0] if vector_ver else "not installed",
            "total_memories": total,
            "by_category": by_category,
            "live_vector_column": live,
            "zero_vector_memories": zero_vec_count,
            "embedder": embedder_info,
        })


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register PostgreSQL as a memory provider plugin."""
    ctx.register_memory_provider(PostgresMemoryProvider())
