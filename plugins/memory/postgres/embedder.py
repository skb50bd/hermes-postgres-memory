"""Embedding provider for the PostgreSQL memory plugin.

Pluggable embedding client with per-dim model dispatch. The plugin
needs vectors at 768, 1024, or 1536 dims depending on the user's
configured model. Each dim gets its own embedder instance, configured
via the `agent_memory_models` SQL table (override via the CLI) or
environment variables.

Why per-dim
-----------
Different providers serve different dims. Kimi's free endpoint
serves 1024-dim vectors regardless of model alias. Ollama local can
serve 768 (nomic-embed-text) or 1024 (bge-m3). OpenAI serves 1536
(text-embedding-3-small) or 3072. The plugin handles three dims
(768, 1024, 1536) out of the box and lets the user switch.

Configuration
-------------
Per-dim model:    agent_memory_models table (override via CLI or SQL)
Default dim:      agent_memory_settings.default_dim (override via CLI)
Provider-specific env vars (HERMES_EMBED_*): see below.

Env var fallback chain
----------------------
1. HERMES_EMBED_API_KEY_<DIM>      (e.g. HERMES_EMBED_API_KEY_1024)
2. HERMES_EMBED_API_KEY            (shared)
3. <api_key_env from SQL registry> (e.g. KIMI_API_KEY for kimi provider)
4. KIMI_API_KEY or OLLAMA_API_KEY  (provider-specific fallback)

Supported providers
-------------------
- ``kimi`` (default for 1024-dim): Moonshot/Kimi's OpenAI-compatible
  embedding endpoint at https://api.kimi.com/coding/v1. Free with the
  KIMI_API_KEY already in .env. Returns 1024-dim, L2-normalized.
- ``ollama_local``: self-hosted Ollama. Run `ollama pull bge-m3` for
  1024-dim or `ollama pull nomic-embed-text` for 768-dim.
- ``ollama_cloud``: Ollama Cloud's /api/embed. Free tier is currently
  chat-only; do not use this provider for embeddings.
- ``noop``: returns a zero vector. Used in tests and as a last-resort
  fail-safe if the configured provider is unreachable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

# Three dims supported out of the box. Adding a new dim requires:
#   1. ALTER TABLE agent_memory ADD COLUMN vector_<dim> vector(<dim>);
#   2. CREATE INDEX ... ON agent_memory USING hnsw (vector_<dim> ...);
#   3. INSERT INTO agent_memory_models VALUES (<dim>, ...);
#   4. Update SUPPORTED_DIMS below.
SUPPORTED_DIMS = (768, 1024, 1536)

# Default dim the plugin uses when no setting is configured.
DEFAULT_DIM = 1024

# Lazy-imported httpx keeps import-time cost off the hot path.
_httpx = None


def _get_httpx():
    global _httpx
    if _httpx is None:
        import httpx  # type: ignore
        _httpx = httpx
    return _httpx


class EmbeddingError(RuntimeError):
    """Raised when an embedding request fails and fail-open is disabled."""


class Embedder:
    """Embedding client for a single dim. Per-dim cache, fail-safe fallback.

    The cache key includes the dim so two embedders at different dims
    don't share cache entries. The cache is content-addressable:
    sha256(provider|model|dim|text) means identical content from the
    same configured model is a hit.

    Multiple Embedder instances (one per dim) can coexist. The plugin
    gets the right one via the module-level `get_embedder(dim=N)`
    factory.
    """

    def __init__(self, *, dim: int, provider: str, model: str,
                 api_key: str = "", base_url: str = "",
                 cache_dir: Optional[str] = None,
                 cache_enabled: bool = True,
                 fail_open: bool = True,
                 timeout: float = 10.0) -> None:
        if dim not in SUPPORTED_DIMS:
            raise ValueError(
                f"Unsupported dim: {dim}. Supported: {SUPPORTED_DIMS}. "
                f"Add an ALTER TABLE migration before using a new dim."
            )
        # Resolve cache_dir from env if not explicitly given. This
        # makes the factory's _read_model_config_for_dim able to
        # pass through "the user wants a per-test cache dir" via
        # env vars without the SQL path needing to know about them.
        if not cache_dir:
            cache_dir = (
                os.environ.get(f"HERMES_EMBED_CACHE_DIR_{dim}")
                or os.environ.get("HERMES_EMBED_CACHE_DIR")
                or ""
            )
        self._cfg = {
            "dim": dim,
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "timeout": timeout,
            "cache_dir": cache_dir or str(
                Path.home() / ".cache" / "hermes" / "embeddings" / str(dim)
            ),
            "cache_enabled": cache_enabled,
            "fail_open": fail_open,
        }
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache: dict = {}
        self._cache_loaded = False
        self._stats = {"hits": 0, "misses": 0, "errors": 0, "zero_fallbacks": 0}

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def dim(self) -> int:
        return self._cfg["dim"]

    @property
    def model(self) -> str:
        return self._cfg["model"]

    @property
    def provider(self) -> str:
        return self._cfg["provider"]

    def stats(self) -> dict:
        return dict(self._stats)

    def embed(self, text: str) -> List[float]:
        """Return a vector for ``text``. Never raises unless fail_open=False."""
        if not text or not text.strip():
            return [0.0] * self.dim
        # 1) in-memory cache
        key = self._cache_key(text)
        with self._cache_lock:
            if key in self._cache:
                self._stats["hits"] += 1
                return list(self._cache[key])
        # 2) disk cache
        if self._cfg["cache_enabled"]:
            cached = self._read_disk_cache(key)
            if cached is not None:
                with self._cache_lock:
                    self._cache[key] = cached
                self._stats["hits"] += 1
                return cached
        # 3) live embed
        self._stats["misses"] += 1
        used_fallback = False
        try:
            vector = self._embed_live(text)
        except Exception as exc:  # network, auth, dim mismatch, ...
            self._stats["errors"] += 1
            logger.warning(
                "Embedding provider failed (dim=%d, provider=%s, model=%s): %s",
                self.dim, self.provider, self.model, exc,
            )
            if not self._cfg["fail_open"]:
                raise EmbeddingError(f"Embedding failed: {exc}") from exc
            self._stats["zero_fallbacks"] += 1
            used_fallback = True
            vector = [0.0] * self.dim
        # 3a) contract check
        if len(vector) != self.dim:
            self._stats["errors"] += 1
            logger.warning(
                "Embedding provider returned dim=%d, expected %d",
                len(vector), self.dim,
            )
            if not self._cfg["fail_open"]:
                raise EmbeddingError(
                    f"Embedding dim mismatch: got {len(vector)}, expected {self.dim}"
                )
            self._stats["zero_fallbacks"] += 1
            used_fallback = True
            vector = [0.0] * self.dim
        # 3b) refuse to cache zero-fallback vectors
        if used_fallback:
            logger.debug("Skipping cache write for zero-fallback vector (dim=%d)", self.dim)
            return vector
        # 4) cache
        if self._cfg["cache_enabled"]:
            with self._cache_lock:
                self._cache[key] = vector
            self._write_disk_cache(key, vector)
        return vector

    def embed_batch(self, texts: Iterable[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]

    # ── Internals ──────────────────────────────────────────────────────────

    def _cache_key(self, text: str) -> str:
        payload = f"{self._cfg['dim']}|{self._cfg['provider']}|{self._cfg['model']}|{text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_path(self) -> Path:
        p = Path(self._cfg["cache_dir"])
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _read_disk_cache(self, key: str) -> Optional[List[float]]:
        path = self._cache_path() / f"{key[:2]}" / f"{key}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
            vec = data.get("vector")
            if not isinstance(vec, list) or len(vec) != self.dim:
                return None
            return [float(x) for x in vec]
        except Exception:
            return None

    def _write_disk_cache(self, key: str, vector: List[float]) -> None:
        path = self._cache_path() / f"{key[:2]}"
        try:
            path.mkdir(parents=True, exist_ok=True)
            (path / f"{key}.json").write_text(
                json.dumps({
                    "dim": self.dim,
                    "provider": self._cfg["provider"],
                    "model": self._cfg["model"],
                    "vector": vector,
                    "ts": int(time.time()),
                }),
                "utf-8",
            )
        except Exception as exc:
            logger.debug("Embedding disk cache write failed: %s", exc)

    def _embed_live(self, text: str) -> List[float]:
        provider = self._cfg["provider"]
        if provider == "noop":
            return [0.0] * self.dim
        if provider == "kimi":
            return self._embed_openai_compat(
                default_base="https://api.kimi.com/coding/v1",
                path="/embeddings",
                text_payload_key="input",
                text_payload_value=text,
            )
        if provider in ("ollama_cloud", "ollama_local"):
            return self._embed_ollama(text)
        raise EmbeddingError(
            f"Unknown embedding provider: {provider!r}. "
            f"Set HERMES_EMBED_PROVIDER to one of: kimi, ollama_local, ollama_cloud, noop."
        )

    def _embed_openai_compat(
        self,
        *,
        default_base: str,
        path: str,
        text_payload_key: str,
        text_payload_value,
    ) -> List[float]:
        base = self._cfg["base_url"] or default_base
        base = base.rstrip("/")
        url = f"{base}{path}"
        body = {"model": self._cfg["model"], text_payload_key: text_payload_value}
        headers = {"Content-Type": "application/json"}
        if self._cfg["api_key"]:
            headers["Authorization"] = f"Bearer {self._cfg['api_key']}"
        httpx = _get_httpx()
        with httpx.Client(timeout=self._cfg["timeout"]) as client:
            resp = client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            raise EmbeddingError(
                f"Embed endpoint returned {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        if "data" in data and data["data"]:
            vec = data["data"][0].get("embedding")
            if vec is None:
                raise EmbeddingError(
                    f"Embed response missing 'embedding' in data[0]: {list(data.keys())}"
                )
            return [float(x) for x in vec]
        if "embedding" in data:
            return [float(x) for x in data["embedding"]]
        if "embeddings" in data and data["embeddings"]:
            vec = data["embeddings"][0]
            return [float(x) for x in vec]
        if "vectors" in data and data["vectors"]:
            return [float(x) for x in data["vectors"][0]]
        raise EmbeddingError(
            f"Unexpected embed response shape: {list(data.keys())}"
        )

    def _embed_ollama(self, text: str) -> List[float]:
        base = self._cfg["base_url"]
        if not base:
            base = "https://ollama.com" if self._cfg["provider"] == "ollama_cloud" else "http://localhost:11434"
        base = base.rstrip("/")
        url = f"{base}/api/embed"
        body = {"model": self._cfg["model"], "input": text}
        headers = {"Content-Type": "application/json"}
        if self._cfg["provider"] == "ollama_cloud" and self._cfg["api_key"]:
            headers["Authorization"] = f"Bearer {self._cfg['api_key']}"
        httpx = _get_httpx()
        with httpx.Client(timeout=self._cfg["timeout"]) as client:
            resp = client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            raise EmbeddingError(
                f"Ollama embed returned {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        if "embeddings" in data and data["embeddings"]:
            vec = data["embeddings"][0]
        elif "embedding" in data:
            vec = data["embedding"]
        else:
            raise EmbeddingError(f"Unexpected Ollama embed response shape: {list(data.keys())}")
        if len(vec) != self.dim:
            raise EmbeddingError(
                f"Embedding dim mismatch: model returned {len(vec)}, "
                f"configured dim is {self.dim}. Adjust HERMES_EMBED_DIM or "
                f"switch HERMES_EMBED_MODEL."
            )
        return [float(x) for x in vec]


# ── Module-level per-dim singleton registry ──────────────────────────────

_singletons: dict = {}  # dim → Embedder
_singletons_lock = threading.Lock()


def get_embedder(dim: int) -> Embedder:
    """Get (or create) the Embedder singleton for the given dim.

    The factory looks up the model config in this order:
      1. In-process Python overrides (if any were set via set_override)
      2. The `agent_memory_models` SQL table (the live source of truth)
      3. Hard-coded defaults per dim
    """
    if dim not in SUPPORTED_DIMS:
        raise ValueError(
            f"Unsupported dim: {dim}. Supported: {SUPPORTED_DIMS}."
        )
    if dim in _singletons:
        return _singletons[dim]
    with _singletons_lock:
        if dim in _singletons:
            return _singletons[dim]
        # Try to load config from SQL via the plugin's _read_model_config_for_dim.
        # If unavailable (e.g. tests), fall back to env-based config.
        #
        # We resolve the function through sys.modules rather than a
        # hardcoded `from plugins.memory.postgres import ...` so that
        # tests which load the plugin under a different module name
        # (e.g. via importlib.util.spec_from_file_location) can still
        # monkeypatch the function.
        cfg = _resolve_model_config(dim)
        embedder = Embedder(**cfg)
        _singletons[dim] = embedder
        return embedder


def _resolve_model_config(dim: int) -> dict:
    """Find the plugin's _read_model_config_for_dim via sys.modules, then
    fall back to env-based defaults if no plugin module is loaded yet.

    We walk sys.modules looking for a module that defines
    _read_model_config_for_dim and is NOT this embedder module itself.
    The first match wins. If none found (or it raises), fall back to
    the env-based defaults baked into _default_model_config_for_dim.
    """
    import sys as _sys
    for mod_name, mod in list(_sys.modules.items()):
        if mod is None or mod_name == __name__:
            continue
        # The plugin module is anything under plugins/memory/postgres/,
        # or anything that has _read_model_config_for_dim as a top-level
        # callable (not inherited).
        if mod is not None and "_read_model_config_for_dim" in dir(mod):
            attr = getattr(mod, "_read_model_config_for_dim", None)
            if callable(attr) and getattr(attr, "__module__", "") != __name__:
                try:
                    return attr(dim)
                except Exception:
                    break
    return _default_model_config_for_dim(dim)


def _default_model_config_for_dim(dim: int) -> dict:
    """Hard-coded defaults for the per-dim model config.

    Used when the SQL registry is unavailable (tests, fresh boot
    before the settings table is created). The live plugin always
    uses the SQL registry.
    """
    cache_dir = (
        os.environ.get(f"HERMES_EMBED_CACHE_DIR_{dim}")
        or os.environ.get("HERMES_EMBED_CACHE_DIR")
        or ""
    )
    if dim == 768:
        return {
            "dim": 768,
            "provider": os.environ.get("HERMES_EMBED_PROVIDER_768", "ollama_local"),
            "model": os.environ.get("HERMES_EMBED_MODEL_768", "nomic-embed-text"),
            "api_key": _resolve_api_key(768, "ollama_local"),
            "base_url": os.environ.get("HERMES_EMBED_BASE_URL_768", ""),
            "cache_dir": cache_dir,
        }
    if dim == 1024:
        return {
            "dim": 1024,
            "provider": os.environ.get("HERMES_EMBED_PROVIDER_1024", "kimi"),
            "model": os.environ.get("HERMES_EMBED_MODEL_1024", "bge_m3_embed"),
            "api_key": _resolve_api_key(1024, "kimi"),
            "base_url": os.environ.get("HERMES_EMBED_BASE_URL_1024", ""),
            "cache_dir": cache_dir,
        }
    if dim == 1536:
        return {
            "dim": 1536,
            "provider": os.environ.get("HERMES_EMBED_PROVIDER_1536", "kimi"),
            "model": os.environ.get("HERMES_EMBED_MODEL_1536", "text-embedding-3-small"),
            "api_key": _resolve_api_key(1536, "kimi"),
            "base_url": os.environ.get("HERMES_EMBED_BASE_URL_1536", ""),
            "cache_dir": cache_dir,
        }
    raise ValueError(dim)


def _resolve_api_key(dim: int, provider: str) -> str:
    """Resolve the API key for a given dim + provider, in priority order."""
    explicit = os.environ.get(f"HERMES_EMBED_API_KEY_{dim}", "").strip()
    if explicit:
        return explicit
    shared = os.environ.get("HERMES_EMBED_API_KEY", "").strip()
    if shared:
        return shared
    if provider == "kimi":
        return os.environ.get("KIMI_API_KEY", "").strip()
    if provider in ("ollama_cloud", "ollama_local"):
        return os.environ.get("OLLAMA_API_KEY", "").strip()
    return ""


def reset_embedder(dim: Optional[int] = None) -> None:
    """Drop the cached embedder(s). Used by tests and the model-set CLI."""
    global _singletons
    with _singletons_lock:
        if dim is None:
            _singletons = {}
        elif dim in _singletons:
            del _singletons[dim]


def get_all_embedders() -> List[Embedder]:
    """Return one Embedder per supported dim (used by the backfill script)."""
    return [get_embedder(d) for d in SUPPORTED_DIMS]
