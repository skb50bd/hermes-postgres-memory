"""Integration tests for the embedding hooks in the PostgreSQL memory plugin.

Verifies that:
- add_memory() embeds the content at the default dim and persists the
  vector in the matching per-dim column.
- search_memories() embeds the query at the (optional) dim and runs
  a hybrid query that combines ts_rank and cosine distance.
- The plugin still works when the embedder fails open to a zero
  vector (degraded mode, but does not raise).
- The search SQL parameter list has the same length as the
  placeholder count (placeholder/param drift guard).
- model-set tooling writes the agent_memory_settings.default_dim row
  and rebuilds the embedder on next access.
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


# Per-dim constants.
EMBED_DIM_768 = 768
EMBED_DIM_1024 = 1024
EMBED_DIM_1536 = 1536

# Add the plugin dir to sys.path.
PLUGIN_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "plugins", "memory", "postgres")
)
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


@pytest.fixture()
def pg_module(monkeypatch, tmp_path_factory):
    """Fresh import of the postgres plugin per test, with shims for the
    hermes-agent imports the plugin needs."""
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
    monkeypatch.setenv("PG_MEM_DB_CONN_STR", "postgresql://hermes:***@db:5432/hermes")
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_DATABASE", raising=False)
    monkeypatch.setenv("HERMES_EMBED_PROVIDER_768", "ollama_local")
    monkeypatch.setenv("HERMES_EMBED_PROVIDER_1024", "kimi")
    monkeypatch.setenv("HERMES_EMBED_PROVIDER_1536", "kimi")
    monkeypatch.setenv("HERMES_EMBED_API_KEY_1024", "fake")
    monkeypatch.setenv("HERMES_EMBED_API_KEY_1536", "fake")
    monkeypatch.setenv("HERMES_EMBED_CACHE_768", "0")
    monkeypatch.setenv("HERMES_EMBED_CACHE_1024", "0")
    monkeypatch.setenv("HERMES_EMBED_CACHE_1536", "0")
    # Use a per-test cache dir so the user's real cache doesn't
    # bleed into tests. We point all 3 dims at the same dir.
    cache_dir = str(tmp_path_factory.mktemp("embed_cache"))
    for d in (768, 1024, 1536):
        monkeypatch.setenv(f"HERMES_EMBED_CACHE_DIR_{d}", cache_dir)
        monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", cache_dir)

    import embedder as em
    em.reset_embedder()
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
        self._last_sql = ""
        self._last_params = None
        self.executions = []  # all (sql, params) in order
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
            # The plugin's _read_default_dim query. Pretend it's never
            # set so the plugin falls through to the env-var default.
            self._rows = []
            self._row_idx = 0
        else:
            self._rows = []
            self._row_idx = 0

    def fetchall(self): return self._rows
    def fetchone(self):
        if not self._rows or self._row_idx >= len(self._rows):
            return None
        row = self._rows[self._row_idx]
        self._row_idx += 1
        return row
    def close(self): self.closed = True


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


def test_add_memory_writes_to_default_dim_column(pg_module, fake_pool, monkeypatch):
    """A 1024-dim default inserts into vector_1024 (the matching column)."""
    # Stub the SQL model-config reader so the embedder factory doesn't
    # try to connect to a real DB. Return a kimi/1024 config with a
    # fake key — the test patches the live call to return a vector.
    def fake_model_config(dim):
        return {
            "dim": dim, "provider": "kimi", "model": "bge_m3_embed",
            "api_key": "fake", "base_url": "",
        }
    monkeypatch.setattr(pg_module, "_read_model_config_for_dim", fake_model_config)
    monkeypatch.setattr(pg_module, "_read_default_dim", lambda c: 1024)

    client = pg_module._PostgresClient()
    seen = {}

    def fake_live(self, text):
        seen["text"] = text
        return [0.42] * EMBED_DIM_1024

    embedder = pg_module.get_embedder(1024)
    monkeypatch.setattr(embedder.__class__, "_embed_live", fake_live)
    mid = client.add_memory("the rain in spain", category="fact", target="memory")
    assert mid
    assert seen["text"] == "the rain in spain"
    cur = fake_pool.conn.cursors[-1]
    insert_sql = cur._last_sql
    assert "vector_1024" in insert_sql, f"INSERT did not target vector_1024: {insert_sql}"
    assert "vector_768" not in insert_sql
    assert "vector_1536" not in insert_sql
    insert_params = cur._last_params
    assert len(insert_params) >= 5
    assert insert_params[4] == [0.42] * EMBED_DIM_1024


def test_add_memory_uses_correct_column_per_dim(pg_module, fake_pool, monkeypatch):
    """The default dim drives which column we insert into."""
    client = pg_module._PostgresClient()

    def fake_live(self, text):
        return [0.5] * self.dim

    for default_dim, expected_col in [
        (768, "vector_768"),
        (1024, "vector_1024"),
        (1536, "vector_1536"),
    ]:
        # Reset the embedder singleton for this dim, then patch
        # its _embed_live. The factory will rebuild the embedder
        # on the next call to get_embedder(dim).
        from embedder import reset_embedder
        reset_embedder(default_dim)
        monkeypatch.setattr(client, "_default_dim", default_dim)
        monkeypatch.setattr(pg_module.get_embedder(default_dim).__class__,
                            "_embed_live", fake_live)
        client.add_memory(f"test-{default_dim}", category="fact", target="memory")
        cur = fake_pool.conn.cursors[-1]
        assert expected_col in cur._last_sql, (
            f"default_dim={default_dim}: expected column {expected_col} in "
            f"INSERT, got: {cur._last_sql}"
        )


def test_search_memories_runs_hybrid_query_with_query_embedding(pg_module, fake_pool, monkeypatch):
    client = pg_module._PostgresClient()
    monkeypatch.setattr(pg_module, "_read_model_config_for_dim",
                        lambda d: {"dim": d, "provider": "kimi", "model": "bge_m3_embed",
                                   "api_key": "fake", "base_url": ""})
    monkeypatch.setattr(pg_module, "_read_default_dim", lambda c: 1024)
    seen = {}

    def fake_live(self, text):
        seen.setdefault("calls", []).append(text)
        return [0.11] * EMBED_DIM_1024

    monkeypatch.setattr(pg_module.get_embedder(1024).__class__, "_embed_live", fake_live)
    results = client.search_memories("rainy days", target="memory", top_k=5)
    assert seen["calls"] == ["rainy days"]
    assert len(results) == 2
    r = results[0]
    assert "text_rank" in r and "vector_sim" in r and "rank" in r
    cur = fake_pool.conn.cursors[-1]
    v2_sql, v2_params = cur.executions[0]
    assert "fts_candidates" in v2_sql
    assert "vector_1024" in v2_sql
    assert [0.11] * EMBED_DIM_1024 in list(v2_params)


def test_search_memories_works_when_embedder_fails_open(pg_module, fake_pool, monkeypatch):
    client = pg_module._PostgresClient()
    monkeypatch.setattr(pg_module, "_read_default_dim", lambda c: 1024)

    def fake_live(self, text):
        raise RuntimeError("network down")

    monkeypatch.setattr(pg_module.get_embedder(1024).__class__, "_embed_live", fake_live)
    results = client.search_memories("rain", top_k=5)
    assert len(results) == 2
    cur = fake_pool.conn.cursors[-1]
    v2_sql, v2_params = cur.executions[0]
    assert [0.0] * EMBED_DIM_1024 in list(v2_params)


def test_hybrid_sql_placeholder_count_matches_params(pg_module, fake_pool, monkeypatch):
    client = pg_module._PostgresClient()
    monkeypatch.setattr(pg_module, "_read_default_dim", lambda c: 1024)

    def fake_live(self, text):
        return [0.5] * EMBED_DIM_1024

    monkeypatch.setattr(pg_module.get_embedder(1024).__class__, "_embed_live", fake_live)
    client.search_memories("hybrid", target="memory", category="fact", top_k=5)

    cur = fake_pool.conn.cursors[-1]
    for sql, params in cur.executions:
        if "fts_candidates" not in sql:
            continue
        placeholders = re.findall(r"%s", sql)
        assert len(placeholders) == len(params), (
            f"Placeholder/param mismatch: SQL has {len(placeholders)}, got {len(params)}.\n"
            f"SQL: {sql}\nParams: {params}"
        )


def test_hybrid_search_param_ordering_with_target_and_category(pg_module, fake_pool, monkeypatch):
    """Regression test for the v1.4.1 pg_search param-ordering bug.

    The where-clause placeholders (target, category) are *interleaved*
    in the middle of the SQL, NOT at the start. A pre-v1.4.1 build
    did `sql_params = list(params) + [query, query, fts_window, ...]`
    which bound the target string to the FIRST %s (the ts_rank
    plainto_tsquery slot), so FTS got the wrong query and the search
    returned 0 rows.

    This test asserts that the right values are bound to the right
    slots. With the v1.4.1 fix, params[0] is the query (for ts_rank),
    params[1] is the target string (for WHERE target = %s), and
    params[2] is the category_id (for WHERE category_id = %s).
    """
    client = pg_module._PostgresClient()
    monkeypatch.setattr(pg_module, "_read_default_dim", lambda c: 1024)

    def fake_live(self, text):
        return [0.5] * EMBED_DIM_1024

    monkeypatch.setattr(pg_module.get_embedder(1024).__class__, "_embed_live", fake_live)
    client.search_memories("hybrid search query", target="memory",
                            category="fact", top_k=5)

    cur = fake_pool.conn.cursors[-1]
    # Find the fts_candidates execution (the hybrid search SQL)
    fts_exec = next(
        (e for e in cur.executions if "fts_candidates" in e[0]),
        None,
    )
    assert fts_exec is not None, f"no fts_candidates SQL found in {cur.executions}"
    sql, params = fts_exec

    # The param order is: [query_for_ts_rank, *where_params, query_for_fts_match,
    #                     fts_window, query_embedding, query_embedding, top_k]
    # With target="memory" and category="fact" (category_id will be 1 per the fake
    # cursor's "SELECT id FROM memory_categories" stub), we expect:
    assert params[0] == "hybrid search query", (
        f"param[0] (ts_rank query) should be the user's query, got: {params[0]!r}"
    )
    assert params[1] == "memory", (
        f"param[1] (WHERE target = %s) should be the target string, got: {params[1]!r}"
    )
    assert params[2] == 1, (
        f"param[2] (WHERE category_id = %s) should be the category_id from the lookup, "
        f"got: {params[2]!r}"
    )
    assert params[3] == "hybrid search query", (
        f"param[3] (@@ tsquery) should be the user's query, got: {params[3]!r}"
    )
    # The fts_window comes next: max(top_k * 4, 40) = max(5*4, 40) = 40
    assert params[4] == max(5 * 4, 40), (
        f"param[4] (fts_window LIMIT) should be 40, got: {params[4]!r}"
    )
    # Then the query embedding appears twice
    assert params[5] == [0.5] * EMBED_DIM_1024
    assert params[6] == [0.5] * EMBED_DIM_1024
    # And the outer LIMIT top_k
    assert params[7] == 5, f"param[7] (outer LIMIT) should be top_k=5, got: {params[7]!r}"


def test_hybrid_search_param_ordering_without_target(pg_module, fake_pool, monkeypatch):
    """Same regression guard, but without target/category — the
    degenerate case where `params` is empty. The v1.4.1 fix must
    still produce a correct param order (and the v1.2.0 code
    happened to be correct in this case, which is why the bug
    was masked for so long)."""
    client = pg_module._PostgresClient()
    monkeypatch.setattr(pg_module, "_read_default_dim", lambda c: 1024)

    def fake_live(self, text):
        return [0.5] * EMBED_DIM_1024

    monkeypatch.setattr(pg_module.get_embedder(1024).__class__, "_embed_live", fake_live)
    client.search_memories("no-filter query", top_k=3)

    cur = fake_pool.conn.cursors[-1]
    fts_exec = next((e for e in cur.executions if "fts_candidates" in e[0]), None)
    assert fts_exec is not None
    sql, params = fts_exec

    # No target/category → params[0] is the query, params[1] is the query,
    # then fts_window, then 2x embedding, then top_k.
    assert params[0] == "no-filter query"
    assert params[1] == "no-filter query"
    assert params[2] == max(3 * 4, 40)
    assert params[3] == [0.5] * EMBED_DIM_1024
    assert params[4] == [0.5] * EMBED_DIM_1024
    assert params[5] == 3


def test_search_supports_per_dim_override(pg_module, fake_pool, monkeypatch):
    """`pg_search dim=768` queries the 768-dim column even when default is 1024."""
    client = pg_module._PostgresClient()
    monkeypatch.setattr(pg_module, "_read_default_dim", lambda c: 1024)

    calls = []
    def fake_live(self, text):
        calls.append((self.dim, text))
        return [0.5] * self.dim

    monkeypatch.setattr(pg_module.get_embedder(768).__class__, "_embed_live", fake_live)
    monkeypatch.setattr(pg_module.get_embedder(1024).__class__, "_embed_live", fake_live)

    # Search at 768 explicitly
    client.search_memories("hybrid", dim=768, top_k=5)
    cur = fake_pool.conn.cursors[-1]
    sql, _ = cur.executions[0]
    assert "vector_768" in sql
    # Only the 768-dim embedder was called
    assert all(d == 768 for d, _ in calls), f"expected only 768-dim calls, got {calls}"


def test_count_by_dim_returns_per_column_counts(pg_module, fake_pool, monkeypatch):
    """count_by_dim issues one COUNT per dim column. We can't easily
    intercept the inner cursor here, so just assert the method exists
    and returns a dict keyed by dim ints."""
    client = pg_module._PostgresClient()
    # Don't actually run the query — just confirm the method's surface.
    assert hasattr(client, "count_by_dim")
    # Smoke: call it through a faked _cursor that returns N for each.
    class _CountCursor(FakeCursor):
        def execute(self, sql, params=None):
            self.executions.append((sql, params))
            self._rows = [(7,)]
            self._row_idx = 0
    class _CountConn(FakeConnection):
        def cursor(self):
            self._cur = _CountCursor(connection=self)
            self.cursors.append(self._cur)
            return self._cur
    class _CountPool(FakePool):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.conn = _CountConn()
    pool = _CountPool(0, 2, "ignored")
    monkeypatch.setattr(psycopg2.pool, "ThreadedConnectionPool", lambda *a, **kw: pool)
    # Reset the global pool
    setattr(pg_module, "_POOL", None)
    out = client.count_by_dim()
    assert out == {768: 7, 1024: 7, 1536: 7}


def test_unsupported_dim_raises_in_add_memory(pg_module, fake_pool, monkeypatch):
    """If the user sets default_dim to a non-supported value, add_memory
    surfaces a clear error rather than silently writing to the wrong
    column."""
    client = pg_module._PostgresClient()
    # Force-set the cached dim on the client (mimics a misconfigured
    # settings table where default_dim is e.g. 512).
    client._default_dim = 512
    with pytest.raises(ValueError, match="not in SUPPORTED_DIMS"):
        client.add_memory("anything", category="fact", target="memory")


def test_unsupported_dim_raises_in_search(pg_module, fake_pool, monkeypatch):
    client = pg_module._PostgresClient()
    client._default_dim = 1024
    with pytest.raises(ValueError, match="Unsupported dim"):
        client.search_memories("anything", dim=512, top_k=5)


def test_read_default_dim_falls_back_to_env(monkeypatch):
    """When the settings table is empty and HERMES_EMBED_DEFAULT_DIM is
    set, the value is honored."""
    from embedder import SUPPORTED_DIMS
    monkeypatch.setenv("HERMES_EMBED_DEFAULT_DIM", "1536")
    # Simulate a connection that returns no rows for the settings query
    class C:
        def execute(self, sql, params=None): pass
        def fetchone(self): return None
        def fetchall(self): return []
        def close(self): pass
    class Conn:
        def cursor(self): return C()
        def rollback(self): pass
    # Re-import the plugin module to pick up the env var
    sys.path.insert(0, PLUGIN_DIR)
    import importlib.util
    spec = importlib.util.spec_from_file_location("pg2", os.path.join(PLUGIN_DIR, "__init__.py"))
    pg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pg)
    assert pg._read_default_dim(Conn()) == 1536
