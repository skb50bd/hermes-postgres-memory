"""Tests for the embedding provider used by the PostgreSQL memory plugin.

Covers:
- Config defaults and env overrides
- In-memory and on-disk cache hit/miss
- Fail-open fallback to zero vector on provider errors
- Hard-fail when HERMES_EMBED_FAIL_OPEN=0
- Provider dispatch: ollama_cloud, ollama_local, noop, unknown
- Dimension mismatch surfaces as EmbeddingError
- Batch embedding delegates to per-item (cache-friendly)
- Module-level singleton is reused (and reset by reset_embedder)
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# Centralized dim for all tests in this file. The shipped schema is
# vector(1024) (BGE-M3 family); tests that hardcode a different dim
# are a bug.
EMBED_DIM = 1024

# Add the plugin dir to sys.path so we can `import embedder` (the new
# flat layout used by the standalone repo). The plugin is also
# importable as `plugins.memory.postgres.embedder` once installed into
# a hermes-agent checkout; tests should work either way.
PLUGIN_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "plugins", "memory", "postgres")
)
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


@pytest.fixture()
def embedder_module(monkeypatch):
    """Re-import the embedder module per test so module-level state resets."""
    sys.modules.pop("embedder", None)
    sys.modules.pop("plugins.memory.postgres.embedder", None)
    monkeypatch.delenv("HERMES_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_EMBED_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_EMBED_MODEL", raising=False)
    monkeypatch.delenv("HERMES_EMBED_DIM", raising=False)
    monkeypatch.delenv("HERMES_EMBED_TIMEOUT", raising=False)
    monkeypatch.delenv("HERMES_EMBED_CACHE", raising=False)
    monkeypatch.delenv("HERMES_EMBED_FAIL_OPEN", raising=False)
    monkeypatch.delenv("HERMES_EMBED_CACHE_DIR", raising=False)
    import embedder as em
    return em


def test_default_provider_model_and_dim(embedder_module, monkeypatch):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_cloud")
    monkeypatch.setenv("HERMES_EMBED_MODEL", "nomic-embed-text")
    e = embedder_module.Embedder()
    assert e.provider == "ollama_cloud"
    assert e.model == "nomic-embed-text"
    assert e.dim == EMBED_DIM


def test_env_overrides_take_effect(embedder_module, monkeypatch):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_local")
    monkeypatch.setenv("HERMES_EMBED_MODEL", "mxbai-embed-large")
    monkeypatch.setenv("HERMES_EMBED_DIM", str(EMBED_DIM))
    monkeypatch.setenv("HERMES_EMBED_API_KEY", "k")
    e = embedder_module.Embedder()
    assert e.provider == "ollama_local"
    assert e.model == "mxbai-embed-large"
    assert e.dim == EMBED_DIM


def test_noop_provider_returns_zero_vector(embedder_module, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "noop")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_EMBED_CACHE", "0")
    e = embedder_module.Embedder()
    v = e.embed("anything")
    assert v == [0.0] * EMBED_DIM
    assert e.stats()["misses"] == 1


def test_empty_text_returns_zero_vector_without_calling_provider(embedder_module, monkeypatch):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_cloud")
    e = embedder_module.Embedder()
    with patch.object(e, "_embed_live") as live:
        assert e.embed("") == [0.0] * EMBED_DIM
        assert e.embed("   ") == [0.0] * EMBED_DIM
    live.assert_not_called()


def test_unknown_provider_raises_embedding_error(embedder_module, monkeypatch):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "definitely-not-a-real-provider")
    monkeypatch.setenv("HERMES_EMBED_FAIL_OPEN", "0")
    e = embedder_module.Embedder()
    with pytest.raises(embedder_module.EmbeddingError, match="Unknown embedding provider"):
        e.embed("hello")


def test_provider_failure_fails_open_to_zero_vector(embedder_module, monkeypatch, tmp_path):
    """A failed embed must NOT poison the disk cache. The second call
    should hit the network, not the cached zero vector."""
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_cloud")
    monkeypatch.setenv("HERMES_EMBED_FAIL_OPEN", "1")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    e = embedder_module.Embedder()
    with patch.object(e, "_embed_live", side_effect=RuntimeError("network down")):
        v = e.embed("hello world")
    assert v == [0.0] * EMBED_DIM
    s = e.stats()
    assert s["errors"] == 1
    assert s["zero_fallbacks"] == 1
    # The crucial assertion: a subsequent call with the live path fixed
    # must hit the network, not the poisoned cache. If this test ever
    # fails, someone has removed the `used_fallback` cache guard.
    with patch.object(e, "_embed_live", return_value=[0.9] * EMBED_DIM) as live:
        v2 = e.embed("hello world")
    assert v2 == [0.9] * EMBED_DIM
    assert live.call_count == 1
    # And the second call's good vector IS cached, so a third call is a hit.
    with patch.object(e, "_embed_live", side_effect=AssertionError("must hit cache")):
        v3 = e.embed("hello world")
    assert v3 == [0.9] * EMBED_DIM


def test_provider_failure_raises_when_fail_open_disabled(embedder_module, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_cloud")
    monkeypatch.setenv("HERMES_EMBED_FAIL_OPEN", "0")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    e = embedder_module.Embedder()
    with patch.object(e, "_embed_live", side_effect=RuntimeError("network down")):
        with pytest.raises(embedder_module.EmbeddingError):
            e.embed("hello world")


def test_in_memory_cache_short_circuits_provider(embedder_module, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_cloud")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    e = embedder_module.Embedder()
    with patch.object(e, "_embed_live", return_value=[0.1] * EMBED_DIM) as live:
        v1 = e.embed("hello world")
        v2 = e.embed("hello world")
    assert v1 == v2
    assert live.call_count == 1
    s = e.stats()
    assert s["misses"] == 1
    assert s["hits"] == 1


def test_disk_cache_persists_across_instances(embedder_module, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_cloud")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    e1 = embedder_module.Embedder()
    with patch.object(e1, "_embed_live", return_value=[0.2] * EMBED_DIM):
        e1.embed("persist me")
    # Second instance, no live mock — must hit disk.
    e2 = embedder_module.Embedder()
    with patch.object(e2, "_embed_live", side_effect=AssertionError("should not call live")):
        v = e2.embed("persist me")
    assert v == [0.2] * EMBED_DIM
    assert e2.stats()["hits"] == 1


def test_disk_cache_disabled_does_not_read_or_write(embedder_module, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_cloud")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_EMBED_CACHE", "0")
    e = embedder_module.Embedder()
    with patch.object(e, "_embed_live", return_value=[0.3] * EMBED_DIM) as live:
        e.embed("a")
        e.embed("a")
    # Two live calls — cache disabled.
    assert live.call_count == 2
    # No files on disk.
    assert list(tmp_path.rglob("*.json")) == []


def test_dim_mismatch_raises_embedding_error(embedder_module, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_cloud")
    monkeypatch.setenv("HERMES_EMBED_FAIL_OPEN", "0")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_EMBED_CACHE", "0")
    e = embedder_module.Embedder()
    with patch.object(e, "_embed_live", return_value=[0.0] * 2048):
        with pytest.raises(embedder_module.EmbeddingError, match="dim mismatch"):
            e.embed("mismatch")


def test_ollama_endpoint_uses_configured_base_url_and_auth(embedder_module, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_cloud")
    monkeypatch.setenv("HERMES_EMBED_MODEL", "nomic-embed-text")
    monkeypatch.setenv("HERMES_EMBED_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("HERMES_EMBED_API_KEY", "secret")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    e = embedder_module.Embedder()

    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"embeddings": [[0.5] * EMBED_DIM]}

    class FakeClient:
        def __init__(self, *a, **kw):
            captured["timeout"] = kw.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return FakeResp()

    fake_httpx = type("H", (), {"Client": FakeClient})
    monkeypatch.setattr(embedder_module, "_httpx", fake_httpx)

    v = e.embed("hello")
    assert v == [0.5] * EMBED_DIM
    assert captured["url"] == "https://example.test/v1/api/embed"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    # Ollama uses /api/embed with "input" key (not OpenAI /v1/embeddings shape).
    assert captured["body"] == {"model": "nomic-embed-text", "input": "hello"}


def test_ollama_legacy_response_shape_is_supported(embedder_module, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_local")
    monkeypatch.setenv("HERMES_EMBED_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    e = embedder_module.Embedder()

    class FakeResp:
        status_code = 200

        def json(self):
            # Legacy /api/embeddings shape
            return {"embedding": [0.7] * EMBED_DIM}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            return FakeResp()

    monkeypatch.setattr(embedder_module, "_httpx", type("H", (), {"Client": FakeClient}))

    v = e.embed("legacy")
    assert v == [0.7] * EMBED_DIM


def test_ollama_http_error_message_includes_status(embedder_module, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "ollama_local")
    monkeypatch.setenv("HERMES_EMBED_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_EMBED_FAIL_OPEN", "0")
    e = embedder_module.Embedder()

    class FakeResp:
        status_code = 401
        text = "unauthorized"

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            return FakeResp()

    monkeypatch.setattr(embedder_module, "_httpx", type("H", (), {"Client": FakeClient}))
    with pytest.raises(embedder_module.EmbeddingError, match="401"):
        e.embed("x")


def test_singleton_is_reused(embedder_module, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "noop")
    embedder_module.reset_embedder()
    a = embedder_module.get_embedder()
    b = embedder_module.get_embedder()
    assert a is b
    embedder_module.reset_embedder()
    c = embedder_module.get_embedder()
    assert c is not a


def test_kimi_default_falls_back_to_KIMI_API_KEY(embedder_module, monkeypatch, tmp_path):
    """The kimi provider should use KIMI_API_KEY when HERMES_EMBED_API_KEY
    is unset, so existing .env files work without modification."""
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "kimi")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_EMBED_CACHE", "0")
    monkeypatch.delenv("HERMES_EMBED_API_KEY", raising=False)
    monkeypatch.setenv("KIMI_API_KEY", "kimi-test-key")
    e = embedder_module.Embedder()
    assert e.provider == "kimi"
    assert e._cfg["api_key"] == "kimi-test-key"

    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.5] * EMBED_DIM}]}

    class FakeClient:
        def __init__(self, *a, **kw):
            captured["timeout"] = kw.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return FakeResp()

    monkeypatch.setattr(embedder_module, "_httpx", type("H", (), {"Client": FakeClient}))
    v = e.embed("hello")
    assert v == [0.5] * EMBED_DIM
    # Default base URL is the Kimi coding endpoint, not Ollama.
    assert captured["url"] == "https://api.kimi.com/coding/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer kimi-test-key"
    # OpenAI-shape request body.
    assert captured["body"] == {"model": "bge_m3_embed", "input": "hello"}


def test_kimi_batch_input_uses_data_response_shape(embedder_module, monkeypatch, tmp_path):
    """Verify the OpenAI-shape parser handles the data[].embedding format
    that Kimi returns (not embeddings[])."""
    monkeypatch.setenv("HERMES_EMBED_PROVIDER", "kimi")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_EMBED_CACHE", "0")
    monkeypatch.setenv("KIMI_API_KEY", "k")
    e = embedder_module.Embedder()

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "object": "list",
                "model": "bge_m3_embed",
                "data": [
                    {"index": 0, "embedding": [0.1] * EMBED_DIM},
                    {"index": 1, "embedding": [0.2] * EMBED_DIM},
                ],
            }

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            return FakeResp()

    monkeypatch.setattr(embedder_module, "_httpx", type("H", (), {"Client": FakeClient}))
    # embed_batch iterates per-item; the first call returns the first vec.
    v = e.embed("first")
    assert len(v) == EMBED_DIM
    assert v[0] == 0.1
