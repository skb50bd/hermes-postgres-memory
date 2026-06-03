"""PostgreSQL memory plugin for Hermes Agent.

PostgreSQL + pgvector backed persistent memory. Uses the existing
agent_memory table with:

- Vector embeddings at 768, 1024, or 1536 dims (per-dim columns, all
  indexed with HNSW). The default dim is configurable at runtime.
- Pluggable per-dim embedder registry: agent_memory_models table maps
  each dim to a (provider, model, api_key_env) triple. Switch dims
  with `hermes postgres-memory model-set --dim <768|1024|1536>`.
- Non-destructive migration: three vector columns all nullable, plus
  a legacy `content_vector` for upgrade compatibility.
- Hybrid search: FTS pre-filter → cosine re-rank on the default-dim column.
- Categories, tags, JSONB metadata, TTL, soft deletes.
- Content-addressable embedding cache (sha256 of dim|provider|model|text).
- Fail-open embedder: provider errors fall back to zero vector; zero-
  fallback vectors are NOT cached (defense against cache poisoning).

Config via environment variables:
    PG_MEM_DB_CONN_STR — PostgreSQL libpq DSN (preferred), e.g.
                         postgresql://hermes:***@host:5432/hermes
    POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER / POSTGRES_PASSWORD /
    POSTGRES_DATABASE — legacy fallback accepted until v2.0
    HERMES_EMBED_DEFAULT_DIM — Override default dim if SQL is unavailable
    HERMES_EMBED_PROVIDER_<DIM> / HERMES_EMBED_MODEL_<DIM> /
    HERMES_EMBED_BASE_URL_<DIM> / HERMES_EMBED_API_KEY_<DIM> — per-dim
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

# Embedder is a sibling of __init__.py inside the plugin dir. Add the
# plugin dir to sys.path so the import resolves when the plugin is
# loaded as a leaf module (e.g. by tests).
import sys as _sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
from embedder import (  # noqa: E402
    Embedder, EmbeddingError, SUPPORTED_DIMS, DEFAULT_DIM,
    get_embedder, reset_embedder, get_all_embedders,
)

logger = logging.getLogger(__name__)

# Register UUID adapter for psycopg2
register_uuid()

# Module-level connection pool. Thread-safe via lock. We do NOT wrap
# each _PostgresClient in its own lock — the connection pool is already
# thread-safe, and a per-client lock would serialize all DB ops across
# the entire process, defeating the point of pooling.
_POOL = None
_POOL_LOCK = threading.Lock()

# How many rows the FTS pre-filter fetches before vector re-rank.
_FTS_WINDOW_OVERFETCH = 4
_FTS_WINDOW_MIN = 40

# Hybrid-search blend: 0.5/0.5 (text vs vector). Override via
# HERMES_POSTGRES_HYBRID_TEXT_WEIGHT (0.0..1.0) if your workload
# is dominated by one or the other.
_HYBRID_TEXT_WEIGHT = 0.5


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default
    return max(minimum, min(maximum, v))


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default


# ── Connection-string resolver ──────────────────────────────────────────
#
# Single source of truth for the postgres memory plugin's DB connection.
# The preferred env var is PG_MEM_DB_CONN_STR (a single DSN like
# "postgresql://user:pass@host:port/dbname"). The legacy POSTGRES_*
# vars are still accepted for backward compatibility — if they're set
# and PG_MEM_DB_CONN_STR is not, the plugin builds a DSN from them
# and emits a one-time deprecation warning. The POSTGRES_* support
# will be removed in v2.0.
_LEGACY_POSTGRES_VARS = (
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER",
    "POSTGRES_PASSWORD", "POSTGRES_DATABASE",
)
_DEPRECATION_LOGGED = False


def _build_dsn_from_legacy_vars() -> str:
    """Construct a postgresql:// DSN from the legacy POSTGRES_* env vars."""
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "hermes")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    database = os.environ.get("POSTGRES_DATABASE", "hermes")
    if not password:
        raise RuntimeError(
            "PG_MEM_DB_CONN_STR is not set and POSTGRES_PASSWORD is not set. "
            "Set PG_MEM_DB_CONN_STR='postgresql://user:pass@host:port/dbname' "
            "in ~/.hermes/.env."
        )
    # urllib.parse.quote so passwords with @, /, : etc. don't break the DSN.
    from urllib.parse import quote
    return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{database}"


def get_pg_mem_db_conn_str() -> str:
    """Return the postgres memory connection string.

    Resolution order:
      1. PG_MEM_DB_CONN_STR (preferred) — a postgresql:// DSN
      2. POSTGRES_HOST/POSTGRES_PORT/POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DATABASE
         (legacy) — built into a DSN at first use, with a one-time
         deprecation warning. Will be removed in v2.0.

    The returned string is a libpq-style DSN that psycopg2 can
    consume directly via `psycopg2.connect(dsn)`.
    """
    global _DEPRECATION_LOGGED
    explicit = os.environ.get("PG_MEM_DB_CONN_STR", "").strip()
    if explicit:
        return explicit
    # Legacy fallback
    if any(os.environ.get(v) for v in _LEGACY_POSTGRES_VARS):
        if not _DEPRECATION_LOGGED:
            logger.warning(
                "POSTGRES_HOST/POSTGRES_PORT/POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DATABASE "
                "are deprecated; set PG_MEM_DB_CONN_STR='postgresql://user:pass@host:port/dbname' "
                "instead. Legacy support will be removed in v2.0."
            )
            _DEPRECATION_LOGGED = True
        return _build_dsn_from_legacy_vars()
    raise RuntimeError(
        "No postgres connection configured. Set PG_MEM_DB_CONN_STR in "
        "~/.hermes/.env, e.g. "
        "PG_MEM_DB_CONN_STR='postgresql://hermes:*** @10.0.0.1:5432/hermes'"
    )


def _postgres_dsn() -> str:
    """Build the full psycopg2 DSN, including timeouts and
    application_name. Uses get_pg_mem_db_conn_str() for the base
    connection so legacy POSTGRES_* vars still work."""
    base = get_pg_mem_db_conn_str()
    connect_timeout = _env_int("HERMES_POSTGRES_CONNECT_TIMEOUT", 5, minimum=1)
    statement_timeout = _env_int("HERMES_POSTGRES_STATEMENT_TIMEOUT_MS", 10_000, minimum=100)
    idle_tx_timeout = _env_int("HERMES_POSTGRES_IDLE_TX_TIMEOUT_MS", 30_000, minimum=100)
    return make_dsn(
        dsn=base,
        sslmode="prefer", connect_timeout=connect_timeout,
        application_name="hermes-memory-postgres",
        options=f"-c statement_timeout={statement_timeout} -c idle_in_transaction_session_timeout={idle_tx_timeout}",
    )


def _get_pool():
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
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            return
        _POOL.closeall()
        _POOL = None


# ---------------------------------------------------------------------------
# Column / dim resolution
# ---------------------------------------------------------------------------

def _vector_column_for_dim(dim: int) -> str:
    """Map a dim to the canonical column name in agent_memory."""
    if dim == 768:
        return "vector_768"
    if dim == 1024:
        return "vector_1024"
    if dim == 1536:
        return "vector_1536"
    raise ValueError(
        f"Unsupported dim {dim}. Supported: {SUPPORTED_DIMS}. "
        f"Run a migration to add a vector_<dim> column first."
    )


def _read_default_dim(conn) -> int:
    """Read the default dim from agent_memory_settings.

    Returns one of: 768, 1024, 1536. Falls back to env or DEFAULT_DIM
    if the settings table doesn't exist yet (fresh install) or the
    row is missing. The fallback chain is:
      1. agent_memory_settings.default_dim  (live source of truth)
      2. HERMES_EMBED_DEFAULT_DIM env var
      3. 1024 (DEFAULT_DIM)
    """
    configured = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM agent_memory_settings WHERE key = 'default_dim'"
            )
            row = cur.fetchone()
        if row:
            # value is JSONB, e.g. '1024' (a JSON number-as-string).
            try:
                configured = int(row[0])
            except (TypeError, ValueError):
                pass
    except psycopg2.errors.UndefinedTable:
        try:
            conn.rollback()
        except Exception:
            pass
    except Exception as exc:
        logger.debug("default_dim lookup failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass

    if configured in SUPPORTED_DIMS:
        return configured
    env_val = os.environ.get("HERMES_EMBED_DEFAULT_DIM", "").strip()
    if env_val:
        try:
            v = int(env_val)
            if v in SUPPORTED_DIMS:
                return v
        except ValueError:
            pass
    return DEFAULT_DIM


def _read_model_config_for_dim(dim: int) -> dict:
    """Read per-dim embedder config from the agent_memory_models table.

    Returns a dict suitable for `Embedder(**dict)`. Falls back to the
    embedder's hard-coded defaults if the table is unavailable.
    """
    try:
        import psycopg2 as _psy
        # Reuse the plugin's connection-string resolver (handles both
        # PG_MEM_DB_CONN_STR and legacy POSTGRES_* vars). Override the
        # default 5s connect_timeout here with a short 5s — same as
        # the legacy version used to — to keep behavior unchanged.
        dsn = make_dsn(
            dsn=get_pg_mem_db_conn_str(),
            connect_timeout=5,
        )
        conn = _psy.connect(dsn)
    except Exception as exc:
        logger.debug("model config read failed: %s", exc)
        # Fall back to defaults
        from embedder import _default_model_config_for_dim
        return _default_model_config_for_dim(dim)

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, model, base_url, api_key_env "
                "FROM agent_memory_models WHERE dim = %s",
                (dim,),
            )
            row = cur.fetchone()
        if not row:
            from embedder import _default_model_config_for_dim
            return _default_model_config_for_dim(dim)
        provider, model, base_url, api_key_env = row
        # Resolve the API key from the named env var, with a few fallbacks.
        api_key = ""
        if api_key_env:
            api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            api_key = os.environ.get(f"HERMES_EMBED_API_KEY_{dim}", "").strip()
        if not api_key:
            api_key = os.environ.get("HERMES_EMBED_API_KEY", "").strip()
        if not api_key and provider == "kimi":
            api_key = os.environ.get("KIMI_API_KEY", "").strip()
        if not api_key and provider == "minimax":
            api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
        if not api_key and provider in ("ollama_local", "ollama_cloud"):
            api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
        return {
            "dim": dim,
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "base_url": base_url or os.environ.get(f"HERMES_EMBED_BASE_URL_{dim}", ""),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PostgreSQL client
# ---------------------------------------------------------------------------

class _PostgresClient:
    """Thin wrapper around psycopg2 for the agent_memory table.

    NOTE: this client is intentionally lock-free. The connection pool
    is already thread-safe; a per-client lock would serialize all DB
    ops and defeat the point of pooling.
    """

    def __init__(self):
        _get_pool()  # warm the pool
        # Read default_dim from the settings table once at init.
        with self._cursor() as cur:
            self._default_dim = _read_default_dim(cur.connection)
        logger.info("postgres-memory plugin default_dim=%d", self._default_dim)

    @property
    def default_dim(self) -> int:
        return self._default_dim

    def refresh_default_dim(self) -> int:
        """Re-read the default dim. Called after `model-set`."""
        with self._cursor() as cur:
            self._default_dim = _read_default_dim(cur.connection)
        # The per-dim embedder singleton may have stale config; drop it
        # so the next call rebuilds from SQL.
        reset_embedder(self._default_dim)
        return self._default_dim

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
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
        """Insert a memory. Embeds at the default dim and writes to
        the matching column. Other dim columns are left null (you can
        backfill them later via the backfill script)."""
        if self._default_dim not in SUPPORTED_DIMS:
            raise ValueError(
                f"Configured default_dim {self._default_dim} is not in "
                f"SUPPORTED_DIMS {list(SUPPORTED_DIMS)}. Update "
                f"agent_memory_settings.default_dim to 768, 1024, or 1536."
            )
        column = _vector_column_for_dim(self._default_dim)
        embedding = get_embedder(self._default_dim).embed(content)

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
                    memory_id, category_id, target, content, embedding,
                    confidence, True, now, now, expires_at,
                    tags or [], json.dumps(metadata or {}), None,
                ),
            )
            return str(memory_id)

    def search_memories(
        self,
        query: str,
        target: Optional[str] = None,
        category: Optional[str] = None,
        top_k: int = 10,
        dim: Optional[int] = None,
    ) -> List[Dict]:
        """Hybrid search: FTS pre-filter + cosine re-rank on the dim-
        matching column. The `dim` parameter overrides default_dim
        (use it to search at a non-default dim). Passing a non-supported
        dim raises ValueError — explicit > implicit."""
        if dim is not None and dim not in SUPPORTED_DIMS:
            raise ValueError(
                f"Unsupported dim {dim}. Supported: {list(SUPPORTED_DIMS)}."
            )
        search_dim = dim if dim in SUPPORTED_DIMS else self._default_dim
        column = _vector_column_for_dim(search_dim)
        query_embedding = get_embedder(search_dim).embed(query)

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

        text_weight = _env_float("HERMES_POSTGRES_HYBRID_TEXT_WEIGHT", _HYBRID_TEXT_WEIGHT)
        vector_weight = 1.0 - text_weight
        fts_window = max(top_k * _FTS_WINDOW_OVERFETCH, _FTS_WINDOW_MIN)

        # Build the param list in the exact order the %s placeholders
        # appear in the rendered SQL. The where-clause slots are
        # *interleaved* in the middle of the SQL (after the first
        # ts_rank %s, before the @@ %s), NOT at the start. Building
        # them in the wrong order silently mis-binds target/category
        # to a tsquery slot and returns empty results — a bug
        # introduced in v1.2.0 and fixed in v1.4.1.
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
        # Order: [query_for_ts_rank, *where_params, query_for_fts_match,
        #         fts_window, query_embedding, query_embedding, top_k]
        sql_params = [query] + list(params) + [query, fts_window,
                                              query_embedding, query_embedding, top_k]

        with self._cursor() as cur:
            cur.execute(sql, sql_params)
            rows = cur.fetchall()

        return [
            {
                "id": str(r[0]),
                "target": r[1],
                "content": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
                "tags": r[4],
                "metadata": r[5],
                "text_rank": float(r[6]) if r[6] is not None else 0.0,
                "vector_sim": float(r[7]) if r[7] is not None else None,
                "rank": float(r[8]) if r[8] is not None else 0.0,
            }
            for r in rows
        ]

    def get_recent_memories(self, target: Optional[str] = None, limit: int = 20) -> List[Dict]:
        with self._cursor() as cur:
            if target:
                cur.execute(
                    """
                    SELECT id, target, content, created_at, tags, metadata
                    FROM agent_memory
                    WHERE is_active = TRUE AND target = %s
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    (target, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, target, content, created_at, tags, metadata
                    FROM agent_memory WHERE is_active = TRUE
                    ORDER BY created_at DESC LIMIT %s
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
        column = _vector_column_for_dim(self._default_dim)
        embedding = get_embedder(self._default_dim).embed(content)
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

    def count_by_dim(self) -> Dict[int, int]:
        """Count non-null rows per dim column. Useful for status / preflight."""
        out: Dict[int, int] = {}
        with self._cursor() as cur:
            for d in SUPPORTED_DIMS:
                col = _vector_column_for_dim(d)
                cur.execute(
                    f"SELECT COUNT(*) FROM agent_memory "
                    f"WHERE is_active = TRUE AND {col} IS NOT NULL "
                    f"AND {col} <> array_fill(0, ARRAY[%s])::vector",
                    (d,),
                )
                out[d] = cur.fetchone()[0]
        return out


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
            "dim": {
                "type": "integer",
                "enum": [768, 1024, 1536],
                "description": "Override the default embedding dim for this search (default: configured default).",
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
            "target": {"type": "string", "enum": ["memory", "user"],
                       "description": "Filter by target store (optional)."},
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
    "description": "Check PostgreSQL memory store status — connection, table stats, default dim, per-dim embedding coverage, embedder health.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

MODEL_SET_SCHEMA = {
    "name": "pg_model_set",
    "description": "Switch the default embedding dim and/or model. After this, new writes go to the configured dim, and the embedder is reconfigured. Existing rows are untouched; run `hermes postgres-memory backfill --dim <dim>` to populate the new dim for them.",
    "parameters": {
        "type": "object",
        "properties": {
            "dim": {"type": "integer", "enum": [768, 1024, 1536],
                    "description": "The new default dim. 768=nomic-embed-text (Ollama), 1024=bge_m3_embed (Kimi), 1536=embo-01 (MiniMax)."},
            "provider": {"type": "string",
                         "description": "Override the provider for this dim. Defaults to the SQL-registered value."},
            "model": {"type": "string",
                      "description": "Override the model name for this dim."},
        },
        "required": ["dim"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class PostgresMemoryProvider(MemoryProvider):
    """PostgreSQL vector memory via pgvector."""

    def __init__(self):
        self._client: Optional[_PostgresClient] = None
        self._session_id = ""

    @property
    def name(self) -> str:
        return "postgres"

    def is_available(self) -> bool:
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
            {"key": "conn_str",
             "description": ("PostgreSQL connection string (libpq-style DSN, e.g. "
                            "'postgresql://hermes:***@10.0.0.1:5432/hermes'). "
                            "The legacy POSTGRES_HOST/PORT/USER/PASSWORD/DATABASE vars "
                            "are still accepted but deprecated as of v1.5.0."),
             "env_var": "PG_MEM_DB_CONN_STR"},
            {"key": "default_dim", "description": "Default embedding dim (768/1024/1536)",
             "default": "1024", "env_var": "HERMES_EMBED_DEFAULT_DIM"},
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
            d = self._client.default_dim
            return (
                f"# PostgreSQL Vector Memory\n"
                f"Active. {count} memories stored. pgvector with HNSW index, full-text search, hybrid retrieval.\n"
                f"Default embedding dim: {d}. Per-dim vector columns supported: 768, 1024, 1536.\n"
                f"Use pg_remember to store facts, pg_search to recall, pg_recent to browse, "
                f"pg_forget to remove, pg_status for diagnostics, pg_model_set to switch dim."
            )
        except Exception as e:
            logger.warning("Postgres system_prompt_block failed: %s", e)
            return "# PostgreSQL Vector Memory\nActive. Use pg_remember, pg_search, pg_recent, pg_forget, pg_status, pg_model_set."

    def prefetch(self, query: str, *, session_id: str = "") -> str:
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
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [REMEMBER_SCHEMA, SEARCH_SCHEMA, RECENT_SCHEMA, FORGET_SCHEMA,
                STATUS_SCHEMA, MODEL_SET_SCHEMA]

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
            elif tool_name == "pg_model_set":
                return self._tool_model_set(args)
            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            logger.error("Postgres tool %s failed: %s", tool_name, e)
            return tool_error(f"PostgreSQL tool '{tool_name}' failed: {e}")

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict] = None) -> None:
        if action not in {"add", "replace"} or not content or not self._client:
            return
        try:
            category = "user_profile" if target == "user" else "fact"
            self._client.add_memory(
                content=content, category=category, target=target,
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
        memory_id = self._client.add_memory(
            content=content,
            category=args.get("category", "fact"),
            target=args.get("target", "memory"),
            tags=args.get("tags", []),
        )
        return json.dumps({"success": True, "memory_id": memory_id,
                           "message": "Memory stored in PostgreSQL."})

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "").strip()
        if not query:
            return tool_error("query is required")
        results = self._client.search_memories(
            query,
            target=args.get("target"),
            category=args.get("category"),
            dim=args.get("dim"),
            top_k=min(args.get("top_k", 10), 50),
        )
        if not results:
            return json.dumps({"results": [], "message": "No matching memories found."})
        return json.dumps({"results": results, "count": len(results)})

    def _tool_recent(self, args: Dict[str, Any]) -> str:
        results = self._client.get_recent_memories(
            target=args.get("target"),
            limit=min(args.get("limit", 20), 100),
        )
        return json.dumps({"results": results, "count": len(results)})

    def _tool_forget(self, args: Dict[str, Any]) -> str:
        memory_id = args.get("memory_id", "").strip()
        if not memory_id:
            return tool_error("memory_id is required")
        success = self._client.remove_memory(memory_id)
        return json.dumps({"success": success,
                           "message": "Memory deleted." if success else "Memory not found."})

    def _tool_status(self) -> str:
        # Parse the connection string for display purposes (no password
        # in the output — we strip the userinfo down to user only).
        dsn = get_pg_mem_db_conn_str()
        # `make_dsn` can parse libpq DSNs into a dict. Fall back to
        # the raw string if the parse fails (e.g. someone passed a
        # raw key=value string).
        try:
            from psycopg2.extensions import parse_dsn
            parsed = parse_dsn(dsn)
            host = parsed.get("host", "?")
            port = parsed.get("port", "5432")
            database = parsed.get("dbname", "?")
            user = parsed.get("user", "?")
            display = f"{host}:{port}/{database} (user={user})"
        except Exception:
            # Mask the password if the parse failed
            import re as _re
            display = _re.sub(r"://[^@/]+@", "://*** @", dsn)
        with self._client._cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            vector_ver = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM agent_memory WHERE is_active = TRUE")
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT name, COUNT(*) FROM agent_memory JOIN memory_categories "
                "ON agent_memory.category_id = memory_categories.id "
                "WHERE is_active = TRUE GROUP BY name"
            )
            by_category = {r[0]: r[1] for r in cur.fetchall()}
        per_dim = self._client.count_by_dim()
        embedder_info: Dict[str, Any] = {}
        for d in SUPPORTED_DIMS:
            try:
                e = get_embedder(d)
                embedder_info[str(d)] = {
                    "provider": e.provider, "model": e.model, "stats": e.stats(),
                }
            except Exception as exc:
                embedder_info[str(d)] = {"error": str(exc)}
        return json.dumps({
            "status": "connected",
            "host": display,
            "postgres_version": version,
            "pgvector_version": vector_ver[0] if vector_ver else "not installed",
            "total_memories": total,
            "by_category": by_category,
            "default_dim": self._client.default_dim,
            "per_dim_embedded": per_dim,
            "embedders": embedder_info,
        })

    def _tool_model_set(self, args: Dict[str, Any]) -> str:
        """Switch the default dim, optionally override the model for that dim."""
        dim = args.get("dim")
        if dim not in SUPPORTED_DIMS:
            return tool_error(f"dim must be one of {list(SUPPORTED_DIMS)}")
        provider = args.get("provider")
        model = args.get("model")
        # 1) Update the model registry row for this dim
        with self._client._cursor() as cur:
            if provider or model:
                cur.execute(
                    "UPDATE agent_memory_models SET "
                    "  provider = COALESCE(%s, provider), "
                    "  model = COALESCE(%s, model), "
                    "  updated_at = now() "
                    "WHERE dim = %s RETURNING provider, model",
                    (provider, model, dim),
                )
                row = cur.fetchone()
                if not row:
                    # No row for this dim — insert with the overrides or defaults
                    cur.execute(
                        "INSERT INTO agent_memory_models (dim, provider, model, api_key_env) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (dim) DO UPDATE SET "
                        "  provider = EXCLUDED.provider, model = EXCLUDED.model, "
                        "  updated_at = now() "
                        "RETURNING provider, model",
                        (dim, provider or "kimi", model or "bge_m3_embed", "KIMI_API_KEY"),
                    )
                    row = cur.fetchone()
                provider, model = row
            # 2) Update default_dim
            cur.execute(
                "UPDATE agent_memory_settings SET value = %s::jsonb, updated_at = now() "
                "WHERE key = 'default_dim' "
                "RETURNING value",
                (str(dim),),
            )
            row = cur.fetchone()
        # 3) Reset the per-dim embedder singleton so the next call picks up new config
        reset_embedder(dim)
        new_dim = self._client.refresh_default_dim()
        return json.dumps({
            "success": True,
            "new_default_dim": new_dim,
            "model_for_dim": {"dim": dim, "provider": provider, "model": model},
            "message": (
                f"Default dim is now {new_dim}. "
                f"New writes go to vector_{new_dim}. "
                f"Run `hermes postgres-memory backfill --dim {new_dim}` "
                f"to populate the new dim for existing rows."
            ),
        })


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register PostgreSQL as a memory provider plugin."""
    ctx.register_memory_provider(PostgresMemoryProvider())
