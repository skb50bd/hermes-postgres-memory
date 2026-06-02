"""Embedding provider for the PostgreSQL memory plugin.

Pluggable embedding client. The same model MUST be used for every row in the
agent_memory.content_vector column — pgvector's cosine/L2/inner-product
operators only produce meaningful similarity scores when vectors share the
same embedding space. If you switch models you must backfill the column.

Supported providers
-------------------
- ``kimi`` (default): Moonshot/Kimi's OpenAI-compatible embedding endpoint at
  https://api.kimi.com/coding/v1. Free with the KIMI_API_KEY already in
  .env. Model ``bge_m3_embed`` returns 1024-dim, L2-normalized vectors.
  Kimi's endpoint accepts 9+ model names as aliases (bge_m3_embed,
  bge-m3, bge-large, bge-large-en, bge-large-zh, nomic-embed-text,
  text-embedding-3-small, text-embedding-v1, embedding-2) but they all
  return 1024 dims, so the model is purely a quality/style choice.
- ``ollama_cloud``: Ollama Cloud's /api/embed endpoint. Free tier but the
  public model catalog is currently chat-only; embedding models require a
  self-hosted Ollama. Use ``ollama_local`` instead.
- ``ollama_local``: same contract as ``ollama_cloud`` but points at a
  self-hosted Ollama instance. Run ``ollama pull bge-m3`` (1024 dim) or
  ``ollama pull nomic-embed-text`` (768 dim) on the host.
- ``noop``: returns a zero vector. Used in tests and as a last-resort
  fail-safe if the configured provider is unreachable.

Configuration
-------------
HERMES_EMBED_PROVIDER       one of: kimi, ollama_cloud, ollama_local, noop
                            (default: kimi)
HERMES_EMBED_MODEL          model name passed to the provider
                            (default: bge_m3_embed)
HERMES_EMBED_DIM            output dimension, must match the model
                            (default: 1024 — enforced for bge_m3_embed)
HERMES_EMBED_BASE_URL       API base URL
                            (default: https://api.kimi.com/coding/v1 for kimi,
                             https://ollama.com for ollama_cloud,
                             http://localhost:11434 for ollama_local)
HERMES_EMBED_API_KEY        API key (falls back to KIMI_API_KEY for the kimi
                            provider, OLLAMA_API_KEY for ollama_cloud)
HERMES_EMBED_TIMEOUT        request timeout in seconds (default: 10)
HERMES_EMBED_CACHE_DIR      on-disk cache directory
                            (default: ~/.cache/hermes/embeddings)
HERMES_EMBED_CACHE          "1" to enable cache (default: 1), "0" disables
HERMES_EMBED_FAIL_OPEN      "1" to fall back to zero vector on provider
                            errors (default: 1). Set to "0" to raise.
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

# Default embedder settings.
# These match the agent_memory.content_vector schema after the 1536 -> 1024
# migration in migrations/001_embedding_dim.sql.
DEFAULT_PROVIDER = "kimi"
DEFAULT_MODEL = "bge_m3_embed"
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
    """Embedding client with disk cache and fail-safe fallback.

    Thread-safe. The disk cache is keyed by a SHA-256 of
    ``(provider, model, content)`` so identical content from the same
    configured model never hits the network twice.
    """

    def __init__(self, *, override: Optional[dict] = None) -> None:
        provider = os.environ.get("HERMES_EMBED_PROVIDER", DEFAULT_PROVIDER)
        # API key resolution: explicit HERMES_EMBED_API_KEY wins, else fall
        # back to the platform's primary env var (KIMI_API_KEY for kimi,
        # OLLAMA_API_KEY for ollama_*). This means existing .env files work
        # without modification.
        api_key = os.environ.get("HERMES_EMBED_API_KEY", "").strip()
        if not api_key:
            if provider == "kimi":
                api_key = os.environ.get("KIMI_API_KEY", "").strip()
            elif provider in ("ollama_cloud", "ollama_local"):
                api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
        cfg = {
            "provider": provider,
            "model": os.environ.get("HERMES_EMBED_MODEL", DEFAULT_MODEL),
            "dim": int(os.environ.get("HERMES_EMBED_DIM", str(DEFAULT_DIM))),
            "base_url": os.environ.get("HERMES_EMBED_BASE_URL", "").strip(),
            "api_key": api_key,
            "timeout": float(os.environ.get("HERMES_EMBED_TIMEOUT", "10")),
            "cache_dir": os.environ.get(
                "HERMES_EMBED_CACHE_DIR",
                str(Path.home() / ".cache" / "hermes" / "embeddings"),
            ),
            "cache_enabled": os.environ.get("HERMES_EMBED_CACHE", "1") != "0",
            "fail_open": os.environ.get("HERMES_EMBED_FAIL_OPEN", "1") == "1",
        }
        if override:
            cfg.update({k: v for k, v in override.items() if v is not None})
        self._cfg = cfg
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache: dict[str, List[float]] = {}
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
        except Exception as exc:  # network, auth, dimension mismatch, ...
            self._stats["errors"] += 1
            logger.warning("Embedding provider failed: %s", exc)
            if not self._cfg["fail_open"]:
                raise EmbeddingError(f"Embedding failed: {exc}") from exc
            self._stats["zero_fallbacks"] += 1
            used_fallback = True
            vector = [0.0] * self.dim
        # 3a) contract check: a successful embed that returns the wrong
        # dim silently corrupts the vector column. Refuse the result.
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
        # 3b) refuse to cache zero-fallback vectors. A zero vec stored in
        # the cache would short-circuit a future retry once the underlying
        # provider issue is fixed, leaving bad data in the DB. The correct
        # behavior on transient failure is to fall back for this call but
        # NOT poison the cache for next time. (The `noop` provider
        # deliberately returns zeros and DOES cache them; this guard only
        # blocks vectors produced by the fail-open safety net.)
        if used_fallback:
            logger.debug("Skipping cache write for zero-fallback vector")
            return vector
        # 4) cache
        if self._cfg["cache_enabled"]:
            with self._cache_lock:
                self._cache[key] = vector
            self._write_disk_cache(key, vector)
        return vector

    def embed_batch(self, texts: Iterable[str]) -> List[List[float]]:
        """Embed multiple texts. Falls back to per-item embed on batch errors."""
        items = list(texts)
        if not items:
            return []
        # Many providers support batch; we still call per-item for simplicity
        # and so the cache short-circuits each one.
        return [self.embed(t) for t in items]

    # ── Internals ──────────────────────────────────────────────────────────

    def _cache_key(self, text: str) -> str:
        payload = f"{self._cfg['provider']}|{self._cfg['model']}|{text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_path(self) -> Path:
        # Shard by first 2 hex chars of the key to avoid huge directories.
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
                    "provider": self._cfg["provider"],
                    "model": self._cfg["model"],
                    "dim": self.dim,
                    "vector": vector,
                    "ts": int(time.time()),
                }),
                "utf-8",
            )
        except Exception as exc:
            # Disk cache is best-effort.
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
            f"Set HERMES_EMBED_PROVIDER to one of: kimi, ollama_cloud, ollama_local, noop."
        )

    def _embed_openai_compat(
        self,
        *,
        default_base: str,
        path: str,
        text_payload_key: str,
        text_payload_value,
    ) -> List[float]:
        """Hit an OpenAI-compatible /v1/embeddings-style endpoint and parse
        the response. Used by the kimi provider today; reusable for any future
        OpenAI-shape endpoint (OpenRouter, Together, vLLM, etc.)."""
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
        # OpenAI shape: {"object":"list","data":[{"index":0,"embedding":[...]}]}
        if "data" in data and data["data"]:
            vec = data["data"][0].get("embedding")
            if vec is None:
                raise EmbeddingError(
                    f"Embed response missing 'embedding' in data[0]: {list(data.keys())}"
                )
            return [float(x) for x in vec]
        # Other shapes some gateways use:
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
        # Ollama /api/embed returns: {"model": "...", "embeddings": [[...]]}
        # Older /api/embeddings returns: {"embedding": [...]}
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


# ── Module-level singleton ────────────────────────────────────────────────
# Reused across the process. The schema and the model must agree; if the
# process is misconfigured, fail fast at first use.
_singleton: Optional[Embedder] = None
_singleton_lock = threading.Lock()


def get_embedder() -> Embedder:
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Embedder()
    return _singleton


def reset_embedder() -> None:
    """Drop the singleton. Used by tests."""
    global _singleton
    with _singleton_lock:
        _singleton = None
