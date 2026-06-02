"""Integration tests for the embedding hooks in the PostgreSQL memory plugin.

Verifies that:
- add_memory() embeds the content via the embedder (no real network).
- search_memories() embeds the query and runs a hybrid query that
  combines ts_rank and cosine distance.
- The plugin still works when the embedder fails open to a zero vector
  (degraded mode, but does not raise).
- The search SQL parameter list has the same length as the placeholder
  count in the SQL (placeholder/param drift guard).
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from unittest.mock import patch

import psycopg2
import psycopg2.pool
import pytest


# Centralized dim for all tests in this file. The shipped schema is
# vector(1024) (BGE-M3 family); tests that hardcode a different dim
# are a bug.
EMBED_DIM = 1024

# Add the plugin dir to sys.path so we can `import embedder` (the new
# flat layout) and `import __init__` (the plugin module).
PLUGIN_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "plugins", "memory", "postgres")
)
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


@pytest.fixture()
def pg_module(monkeypatch):
    """Fresh import of the postgres plugin per test.

    The plugin's __init__.py imports `agent.memory_provider` and
    `tools.registry` from the host hermes-agent. We shim those with
    fake modules so the test can import the plugin without a real
    hermes-agent install.
    """
    # Provide shims for the hermes-agent imports the plugin needs.
    if "agent.memory_provider" not in sys.modules:
        class _MemoryProvider:
            def __init__(self, *a, **kw): pass
        mp = type(sys)("agent.memory_provider")
        mp.MemoryProvider = _MemoryProvider
        sys.modules["agent.memory_provider"] = mp
    if "agent" not in sys.modules:
        sys.modules["agent"] = type(sys)("agent")
    if "tools.registry" not in sys.modules:
        tr = type(sys)("tools.registry")
        tr.tool_error = lambda msg: json.dumps({"error": msg})
        sys.modules["tools"] = type(sys)("tools")
        sys.modules["tools.registry"] = tr

    for m in list(sys.modules):
        if m == "embedder" or m.startswith("plugins.memory.postgres"):
            del sys.modules[m]
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_USER", "hermes")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("POSTGRES_DATABASE", "hermes")
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_cloud")
    monkeypatch.setenv("HERMES_EMBED_API_KEY", "fake")
    monkeypatch.setenv("HERMES_EMBED_CACHE", "0")

    import embedder as em
    em.reset_embedder()
    # Load the plugin module by file path (it has no subpackage prefix
    # in the new flat layout, but its filename is `__init__.py` so we
    # use importlib.util).
    import importlib.util
    init_path = os.path.join(PLUGIN_DIR, "__init__.py")
    spec = importlib.util.spec_from_file_location("pg_plugin", init_path)
    pg = importlib.util.module_from_spec(spec)
    sys.modules["pg_plugin"] = pg
    spec.loader.exec_module(pg)
    setattr(pg, "_POOL", None)
    yield pg
    setattr(pg, "_POOL", None)
    em.reset_embedder()


class FakeCursor:
    def __init__(self, connection=None):
        self.connection = connection
        self._rows = []
        self._row_idx = 0
        self._description = None
        self._last_sql = ""
        self._last_params = None
        # All execute() calls (sql, params) in order. Useful when the
        # same cursor issues multiple SQLs (e.g. v2 then v1 fallback).
        self.executions = []
        self.closed = False
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._last_sql = sql
        self._last_params = params
        self.executions.append((sql, params))
        if "INSERT INTO agent_memory" in sql:
            self.rowcount = 1
            self._rows = []
        elif "UPDATE agent_memory" in sql:
            self.rowcount = 1 if params and len(params) > 0 else 0
            self._rows = []
        elif "SELECT id FROM memory_categories" in sql:
            self._rows = [(1,)]
        elif "WITH fts_candidates" in sql or "ts_rank" in sql and "FROM agent_memory" in sql:
            # Simulate a hybrid result set. Reset the row pointer so
            # the next fetchone() starts at 0.
            from datetime import datetime, timezone
            self._rows = [
                ("uuid-1", "memory", "alpha content", datetime.now(timezone.utc),
                 [], {}, 0.8, 0.7, 0.75),
                ("uuid-2", "memory", "beta content", datetime.now(timezone.utc),
                 [], {}, 0.5, 0.9, 0.7),
            ]
            self._row_idx = 0
        elif "SELECT id, target, content, created_at" in sql:
            from datetime import datetime, timezone
            self._rows = [
                ("uuid-3", "memory", "gamma", datetime.now(timezone.utc), [], {}),
            ]
            self._row_idx = 0
        elif "COUNT(*)" in sql:
            self._rows = [(2,)]
            self._row_idx = 0
        elif "SELECT version()" in sql:
            self._rows = [("PostgreSQL 15.0",)]
            self._row_idx = 0
        elif "pg_extension" in sql and "extname = 'vector'" in sql:
            self._rows = [("0.8.2",)]
            self._row_idx = 0
        elif "name, COUNT(*)" in sql:
            self._rows = [("fact", 2)]
            self._row_idx = 0
        elif "agent_memory_settings" in sql:
            # The plugin's _read_live_column query. Pretend it's never
            # set so the plugin falls through to v1.
            self._rows = []
            self._row_idx = 0
        else:
            self._rows = []
            self._row_idx = 0

    def fetchall(self):
        return self._rows

    def fetchone(self):
        if not self._rows:
            return None
        if self._row_idx >= len(self._rows):
            return None
        row = self._rows[self._row_idx]
        self._row_idx += 1
        return row

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self._cur = None
        self.autocommit = False
        self.cursors = []

    def cursor(self):
        self._cur = FakeCursor(connection=self)
        self.cursors.append(self._cur)
        return self._cur


class FakePool:
    def __init__(self, minconn, maxconn, dsn):
        self.conn = FakeConnection()
        self.getconn_calls = 0
        self.putconn_calls = 0

    def getconn(self):
        self.getconn_calls += 1
        return self.conn

    def putconn(self, conn, close=False):
        self.putconn_calls += 1


@pytest.fixture()
def fake_pool(monkeypatch):
    pool = FakePool(0, 2, "ignored")
    monkeypatch.setattr(psycopg2.pool, "ThreadedConnectionPool", lambda *a, **kw: pool)
    return pool


def test_add_memory_calls_embedder_and_persists_vector(pg_module, fake_pool, monkeypatch):
    client = pg_module._PostgresClient()
    seen = {}

    def fake_live(self, text):
        seen["text"] = text
        return [0.42] * EMBED_DIM

    monkeypatch.setattr(pg_module.get_embedder().__class__, "_embed_live", fake_live)
    memory_id = client.add_memory("the rain in spain", category="fact", target="memory")
    assert memory_id  # uuid string
    assert seen["text"] == "the rain in spain"
    # The cursor should have received the EMBED_DIM-dim vector.
    cur = fake_pool.conn.cursors[-1]
    insert_params = cur._last_params
    # The vector is at index 4: (memory_id, category_id, target, content, embedding, ...)
    assert len(insert_params) >= 5
    assert insert_params[4] == [0.42] * EMBED_DIM


def test_search_memories_runs_hybrid_query_with_query_embedding(pg_module, fake_pool, monkeypatch):
    client = pg_module._PostgresClient()
    seen = {}

    def fake_live(self, text):
        seen.setdefault("calls", []).append(text)
        return [0.11] * EMBED_DIM

    monkeypatch.setattr(pg_module.get_embedder().__class__, "_embed_live", fake_live)
    results = client.search_memories("rainy days", target="memory", top_k=5)
    # Embedder was called for the query.
    assert seen["calls"] == ["rainy days"]
    # Two result rows from the fake cursor.
    assert len(results) == 2
    r = results[0]
    assert "text_rank" in r and "vector_sim" in r and "rank" in r
    # The v2 hybrid SQL was issued first; it carries the query embedding.
    # (The same cursor issues both v2 and v1 SQLs; the v2 is executions[0].)
    cur = fake_pool.conn.cursors[-1]
    assert len(cur.executions) >= 2, "expected both v2 and v1 SQLs on the cursor"
    v2_sql, v2_params = cur.executions[0]
    assert "fts_candidates" in v2_sql
    assert [0.11] * EMBED_DIM in list(v2_params), (
        f"v2 hybrid SQL params missing the query embedding; got {v2_params}"
    )


def test_search_memories_works_when_embedder_fails_open(pg_module, fake_pool, monkeypatch):
    client = pg_module._PostgresClient()

    def fake_live(self, text):
        # Provider blew up — fail_open defaults to True, so embed() should
        # swallow the error and return a zero vector.
        raise RuntimeError("network down")

    monkeypatch.setattr(pg_module.get_embedder().__class__, "_embed_live", fake_live)
    results = client.search_memories("rain", top_k=5)
    # Should still return rows (the fake cursor returns them regardless).
    assert len(results) == 2
    # All-zero vector is what was passed in for the query (v2 SQL).
    cur = fake_pool.conn.cursors[-1]
    v2_sql, v2_params = cur.executions[0]
    assert "fts_candidates" in v2_sql
    assert [0.0] * EMBED_DIM in list(v2_params)


def test_hybrid_sql_placeholder_count_matches_params(pg_module, fake_pool, monkeypatch):
    """Placeholder/param drift guard.

    Count the %s placeholders in the generated hybrid SQL and assert
    that it equals len(params) at execute time. If a future WHERE
    clause is added without updating the params list, this test fails
    before production sees the bug.
    """
    client = pg_module._PostgresClient()

    def fake_live(self, text):
        return [0.5] * EMBED_DIM

    monkeypatch.setattr(pg_module.get_embedder().__class__, "_embed_live", fake_live)

    # Call with target + category to exercise the WHERE-clause path.
    client.search_memories("hybrid", target="memory", category="fact", top_k=5)

    cur = fake_pool.conn.cursors[-1]
    # Both v2 (hybrid) and v1 (FTS-only fallback) SQLs ran. Each must
    # have placeholder count == len(params).
    for sql, params in cur.executions:
        if "fts_candidates" not in sql:
            continue
        placeholders = re.findall(r"%s", sql)
        assert len(placeholders) == len(params), (
            f"Placeholder/param mismatch: SQL has {len(placeholders)} placeholders, "
            f"got {len(params)} params.\nSQL:\n{sql}\nParams: {params}"
        )


# Note: _read_live_column's auto-detection across the three layouts
# (legacy 1536, sidecar v2, post-migration 1024-named-v1) is verified
# by the live-DB end-to-end test in CHANGELOG.md / the README's
# "Verifying the install" section. The unit test infrastructure for
# this would require either real Postgres or a heavy mock of psycopg2;
# the end-to-end path is more valuable.
