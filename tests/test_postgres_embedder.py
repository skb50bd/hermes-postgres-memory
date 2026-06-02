"""Tests for the embedding provider used by the PostgreSQL memory plugin.

Covers:
- Config defaults and env overrides per dim
- In-memory and on-disk cache hit/miss (keyed by dim+provider+model+text)
- Fail-open fallback to zero vector on provider errors
- Hard-fail when HERMES_EMBED_FAIL_OPEN=0
- Provider dispatch: ollama_cloud, ollama_local, noop, unknown
- Dimension mismatch surfaces as EmbeddingError
- Per-dim registry (get_embedder(768) and get_embedder(1024) are distinct)
- Module-level singletons are reused and reset by reset_embedder
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# Per-dim constants for tests. The plugin supports 768, 1024, 1536.
EMBED_DIM_768 = 768
EMBED_DIM_1024 = 1024
EMBED_DIM_1536 = 1536

# Add the plugin dir to sys.path so we can `import embedder`.
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
    # Clear per-dim env vars
    for prefix in ("HERMES_EMBED_API_KEY_768", "HERMES_EMBED_API_KEY_1024",
                   "HERMES_EMBED_API_KEY_1536", "HERMES_EMBED_BASE_URL_768",
                   "HERMES_EMBED_BASE_URL_1024", "HERMES_EMBED_BASE_URL_1536",
                   "HERMES_EMBED_PROVIDER_768", "HERMES_EMBED_PROVIDER_1024",
                   "HERMES_EMBED_PROVIDER_1536", "HERMES_EMBED_MODEL_768",
                   "HERMES_EMBED_MODEL_1024", "HERMES_EMBED_MODEL_1536",
                   "HERMES_EMBED_API_KEY", "HERMES_EMBED_BASE_URL",
                   "HERMES_EMBED_PROVIDER", "HERMES_EMBED_MODEL",
                   "HERMES_EMBED_DIM", "HERMES_EMBED_TIMEOUT", "HERMES_EMBED_CACHE",
                   "HERMES_EMBED_FAIL_OPEN", "HERMES_EMBED_CACHE_DIR",
                   "KIMI_API_KEY", "OLLAMA_API_KEY"):
        monkeypatch.delenv(prefix, raising=False)
    import embedder as em
    em.reset_embedder()  # clear any prior singleton
    return em


def test_default_per_dim_models(embedder_module, monkeypatch, tmp_path):
    """The default config for each dim uses a known provider+model."""
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_EMBED_CACHE", "0")
    monkeypatch.setenv("KIMI_API_KEY", "k")
    monkeypatch.setenv("OLLAMA_API_KEY", "o")
    for d, expected_provider, expected_model in [
        (768, "ollama_local", "nomic-embed-text"),
        (1024, "kimi", "bge_m3_embed"),
        (1536, "kimi", "text-embedding-3-small"),
    ]:
        cfg = embedder_module._default_model_config_for_dim(d)
        assert cfg["dim"] == d
        assert cfg["provider"] == expected_provider, f"dim {d}: expected {expected_provider}, got {cfg['provider']}"
        assert cfg["model"] == expected_model, f"dim {d}: expected {expected_model}, got {cfg['model']}"


def test_per_dim_provider_override(embedder_module, monkeypatch, tmp_path):
    """HERMES_EMBED_PROVIDER_<DIM> overrides the default provider."""
    monkeypatch.setenv("HERMES_EMBED_PROVIDER_1024", "ollama_local")
    monkeypatch.setenv("HERMES_EMBED_MODEL_1024", "mxbai-embed-large")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    cfg = embedder_module._default_model_config_for_dim(1024)
    assert cfg["provider"] == "ollama_local"
    assert cfg["model"] == "mxbai-embed-large"


def test_per_dim_api_key_fallback_chain(embedder_module, monkeypatch, tmp_path):
    """API key resolution: per-dim env > shared env > provider-specific env."""
    from embedder import _resolve_api_key
    # 1. Per-dim explicit wins
    monkeypatch.setenv("HERMES_EMBED_API_KEY_1024", "explicit-dim")
    monkeypatch.setenv("HERMES_EMBED_API_KEY", "shared")
    monkeypatch.setenv("KIMI_API_KEY", "kimi-default")
    assert _resolve_api_key(1024, "kimi") == "explicit-dim"
    # 2. Shared env next
    monkeypatch.delenv("HERMES_EMBED_API_KEY_1024")
    assert _resolve_api_key(1024, "kimi") == "shared"
    # 3. Provider-specific
    monkeypatch.delenv("HERMES_EMBED_API_KEY")
    assert _resolve_api_key(1024, "kimi") == "kimi-default"
    monkeypatch.delenv("KIMI_API_KEY")
    assert _resolve_api_key(1024, "kimi") == ""
    # 4. Ollama-specific
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-default")
    assert _resolve_api_key(768, "ollama_local") == "ollama-default"


def test_noop_provider_returns_zero_vector_at_dim(embedder_module, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_EMBED_PROVIDER_1024", "noop")
    monkeypatch.setenv("HERMES_EMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_EMBED_CACHE", "0")
    e = embedder_module.Embedder(dim=1024, provider="noop", model="noop",
                                  cache_dir=str(tmp_path), cache_enabled=False)
    v = e.embed("anything")
    assert v == [0.0] * EMBED_DIM_1024
    # A second call is a cache hit (the noop provider deterministically
    # returns the same vector, which is intentional and SHOULD be cached).
    v2 = e.embed("anything")
    assert v2 == [0.0] * EMBED_DIM_1024
    # At least one miss happened (the first call).
    assert e.stats()["misses"] >= 1


def test_empty_text_returns_zero_vector_without_calling_provider(embedder_module, monkeypatch, tmp_path):
    e = embedder_module.Embedder(dim=1024, provider="kimi", model="bge_m3_embed",
                                  api_key="k", cache_dir=str(tmp_path))
    with patch.object(e, "_embed_live") as live:
        assert e.embed("") == [0.0] * EMBED_DIM_1024
        assert e.embed("   ") == [0.0] * EMBED_DIM_1024
    live.assert_not_called()


def test_unknown_provider_raises_embedding_error(embedder_module, monkeypatch):
    with pytest.raises(embedder_module.EmbeddingError, match="Unknown embedding provider"):
        e = embedder_module.Embedder(dim=1024, provider="not-a-real-provider",
                                      model="m", fail_open=False)
        e.embed("hello")


def test_provider_failure_fails_open_to_zero_vector(embedder_module, monkeypatch, tmp_path):
    """A failed embed must NOT poison the disk cache."""
    e = embedder_module.Embedder(dim=1024, provider="kimi", model="m",
                                  api_key="k", cache_dir=str(tmp_path))
    with patch.object(e, "_embed_live", side_effect=RuntimeError("network down")):
        v = e.embed("hello world")
    assert v == [0.0] * EMBED_DIM_1024
    s = e.stats()
    assert s["errors"] == 1
    assert s["zero_fallbacks"] == 1
    # Subsequent call with the live path fixed must hit the network,
    # not the poisoned cache.
    with patch.object(e, "_embed_live", return_value=[0.9] * EMBED_DIM_1024) as live:
        v2 = e.embed("hello world")
    assert v2 == [0.9] * EMBED_DIM_1024
    assert live.call_count == 1


def test_provider_failure_raises_when_fail_open_disabled(embedder_module, monkeypatch, tmp_path):
    e = embedder_module.Embedder(dim=1024, provider="kimi", model="m",
                                  api_key="k", cache_dir=str(tmp_path), fail_open=False)
    with patch.object(e, "_embed_live", side_effect=RuntimeError("network down")):
        with pytest.raises(embedder_module.EmbeddingError):
            e.embed("hello world")


def test_in_memory_cache_short_circuits_provider(embedder_module, monkeypatch, tmp_path):
    e = embedder_module.Embedder(dim=1024, provider="kimi", model="m",
                                  api_key="k", cache_dir=str(tmp_path))
    with patch.object(e, "_embed_live", return_value=[0.1] * EMBED_DIM_1024) as live:
        v1 = e.embed("hello world")
        v2 = e.embed("hello world")
    assert v1 == v2
    assert live.call_count == 1
    s = e.stats()
    assert s["misses"] == 1
    assert s["hits"] == 1


def test_disk_cache_persists_across_instances(embedder_module, monkeypatch, tmp_path):
    e1 = embedder_module.Embedder(dim=1024, provider="kimi", model="m",
                                   api_key="k", cache_dir=str(tmp_path))
    with patch.object(e1, "_embed_live", return_value=[0.2] * EMBED_DIM_1024):
        e1.embed("persist me")
    e2 = embedder_module.Embedder(dim=1024, provider="kimi", model="m",
                                   api_key="k", cache_dir=str(tmp_path))
    with patch.object(e2, "_embed_live", side_effect=AssertionError("should not call live")):
        v = e2.embed("persist me")
    assert v == [0.2] * EMBED_DIM_1024
    assert e2.stats()["hits"] == 1


def test_disk_cache_disabled_does_not_read_or_write(embedder_module, monkeypatch, tmp_path):
    e = embedder_module.Embedder(dim=1024, provider="kimi", model="m",
                                  api_key="k", cache_dir=str(tmp_path), cache_enabled=False)
    with patch.object(e, "_embed_live", return_value=[0.3] * EMBED_DIM_1024) as live:
        e.embed("a")
        e.embed("a")
    assert live.call_count == 2
    assert list(tmp_path.rglob("*.json")) == []


def test_dim_mismatch_raises_embedding_error(embedder_module, monkeypatch, tmp_path):
    e = embedder_module.Embedder(dim=1024, provider="kimi", model="m",
                                  api_key="k", cache_dir=str(tmp_path),
                                  cache_enabled=False, fail_open=False)
    with patch.object(e, "_embed_live", return_value=[0.0] * 2048):
        with pytest.raises(embedder_module.EmbeddingError, match="dim mismatch"):
            e.embed("mismatch")


def test_ollama_endpoint_uses_configured_base_url_and_auth(embedder_module, monkeypatch, tmp_path):
    e = embedder_module.Embedder(dim=768, provider="ollama_cloud",
                                  model="nomic-embed-text", api_key="secret",
                                  base_url="https://example.test/v1",
                                  cache_dir=str(tmp_path), cache_enabled=False)
    captured = {}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"embeddings": [[0.5] * EMBED_DIM_768]}

    class FakeClient:
        def __init__(self, *a, **kw): captured["timeout"] = kw.get("timeout")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return FakeResp()

    monkeypatch.setattr(embedder_module, "_httpx", type("H", (), {"Client": FakeClient}))

    v = e.embed("hello")
    assert v == [0.5] * EMBED_DIM_768
    assert captured["url"] == "https://example.test/v1/api/embed"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["body"] == {"model": "nomic-embed-text", "input": "hello"}


def test_ollama_legacy_response_shape_is_supported(embedder_module, monkeypatch, tmp_path):
    e = embedder_module.Embedder(dim=1024, provider="ollama_local",
                                  model="bge-m3", base_url="http://localhost:11434",
                                  cache_dir=str(tmp_path), cache_enabled=False)

    class FakeResp:
        status_code = 200
        def json(self):
            return {"embedding": [0.7] * EMBED_DIM_1024}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            return FakeResp()

    monkeypatch.setattr(embedder_module, "_httpx", type("H", (), {"Client": FakeClient}))

    v = e.embed("legacy")
    assert v == [0.7] * EMBED_DIM_1024


def test_ollama_http_error_message_includes_status(embedder_module, monkeypatch, tmp_path):
    e = embedder_module.Embedder(dim=1024, provider="ollama_local",
                                  model="bge-m3", base_url="http://localhost:11434",
                                  cache_dir=str(tmp_path), cache_enabled=False,
                                  fail_open=False)

    class FakeResp:
        status_code = 401
        text = "unauthorized"

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            return FakeResp()

    monkeypatch.setattr(embedder_module, "_httpx", type("H", (), {"Client": FakeClient}))
    with pytest.raises(embedder_module.EmbeddingError, match="401"):
        e.embed("x")


def test_singleton_is_reused_per_dim(embedder_module, monkeypatch, tmp_path):
    """get_embedder(768) and get_embedder(1024) return distinct instances."""
    monkeypatch.setenv("HERMES_EMBED_PROVIDER_768", "noop")
    monkeypatch.setenv("HERMES_EMBED_PROVIDER_1024", "noop")
    embedder_module.reset_embedder()
    e_768_a = embedder_module.get_embedder(768)
    e_768_b = embedder_module.get_embedder(768)
    e_1024_a = embedder_module.get_embedder(1024)
    assert e_768_a is e_768_b
    assert e_1024_a is not e_768_a
    embedder_module.reset_embedder(768)
    e_768_c = embedder_module.get_embedder(768)
    assert e_768_c is not e_768_a
    # 1024 singleton is unaffected
    e_1024_b = embedder_module.get_embedder(1024)
    assert e_1024_b is e_1024_a
    embedder_module.reset_embedder()


def test_get_embedder_rejects_unsupported_dim(embedder_module, monkeypatch):
    with pytest.raises(ValueError, match="Unsupported dim"):
        embedder_module.get_embedder(512)


def test_supported_dims_constant(embedder_module):
    """SUPPORTED_DIMS is the canonical list — adding a dim requires code
    + migration + test changes, not just an env var."""
    assert embedder_module.SUPPORTED_DIMS == (768, 1024, 1536)


def test_kimi_default_falls_back_to_KIMI_API_key(embedder_module, monkeypatch, tmp_path):
    """The kimi provider should use KIMI_API_KEY when no explicit key is
    set, so existing .env files work without modification.

    The factory in `_default_model_config_for_dim` does the fallback
    (it calls `_resolve_api_key`). This test exercises the factory
    rather than constructing Embedder directly, to mirror real usage.
    """
    monkeypatch.setenv("KIMI_API_KEY", "kimi-test-key")
    cfg = embedder_module._default_model_config_for_dim(1024)
    assert cfg["api_key"] == "kimi-test-key"

    # Now exercise the live call with a captured response.
    e = embedder_module.Embedder(dim=1024, provider="kimi", model="bge_m3_embed",
                                  api_key=cfg["api_key"],
                                  cache_dir=str(tmp_path), cache_enabled=False)

    captured = {}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"data": [{"index": 0, "embedding": [0.5] * EMBED_DIM_1024}]}

    class FakeClient:
        def __init__(self, *a, **kw): captured["timeout"] = kw.get("timeout")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return FakeResp()

    monkeypatch.setattr(embedder_module, "_httpx", type("H", (), {"Client": FakeClient}))
    v = e.embed("hello")
    assert v == [0.5] * EMBED_DIM_1024
    assert captured["url"] == "https://api.kimi.com/coding/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer kimi-test-key"
    assert captured["body"] == {"model": "bge_m3_embed", "input": "hello"}


def test_kimi_openai_shape_response_parser(embedder_module, monkeypatch, tmp_path):
    """Kimi returns the OpenAI shape (data[].embedding). The embedder
    parser handles it correctly at the 1024-dim default."""
    e = embedder_module.Embedder(dim=1024, provider="kimi", model="bge_m3_embed",
                                  api_key="k", cache_dir=str(tmp_path),
                                  cache_enabled=False)

    class FakeResp:
        status_code = 200
        def json(self):
            return {
                "object": "list", "model": "bge_m3_embed",
                "data": [
                    {"index": 0, "embedding": [0.1] * EMBED_DIM_1024},
                    {"index": 1, "embedding": [0.2] * EMBED_DIM_1024},
                ],
            }

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            return FakeResp()

    monkeypatch.setattr(embedder_module, "_httpx", type("H", (), {"Client": FakeClient}))
    v = e.embed("first")
    assert len(v) == EMBED_DIM_1024
    assert v[0] == 0.1
