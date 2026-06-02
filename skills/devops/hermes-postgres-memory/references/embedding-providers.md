# Embedding Provider Contracts (Kimi + Ollama + alternatives)

The Hermes PostgreSQL memory provider's `embedder.py` is built against the HTTP
contracts documented here. When `HERMES_EMBED_PROVIDER` changes, this file is
the source of truth. When the embedder behavior is wrong, the bug is almost
certainly here or in env var wiring.

**Default as of 2026-06**: `kimi` provider, model `bge_m3_embed`, **1024-dim**.
The previous default of `ollama_cloud` / `nomic-embed-text` / 768-dim was
retired because Ollama Cloud's public model catalog is chat-only and
`/api/embed` returns 401 even with valid keys (see "What does NOT work"
below). Live probe results are in `embedding-provider-landscape.md`; re-probe
before relying on anything here.

## Kimi — `https://api.kimi.com/coding/v1/embeddings` (current default)

Moonshot/Kimi's OpenAI-shape embedding endpoint. Free with the `KIMI_API_KEY`
already in `~/.hermes/.env`. The embedder's `_embed_openai_compat` helper
handles this — same path is reusable for OpenRouter, Together, vLLM, etc.

**Request:**
```http
POST https://api.kimi.com/coding/v1/embeddings
Content-Type: application/json
Authorization: Bearer <KIMI_API_KEY>

{
  "model": "bge_m3_embed",
  "input": "the text to embed"   # string OR list[str] for batch
}
```

**Response (200), normal:**
```json
{
  "object": "list",
  "model": "bge_m3_embed",
  "data": [
    {"index": 0, "embedding": [0.0403, 0.0370, -0.0289, ...]}
  ]
}
```

**Response (200), rate-limited** (NOT the same shape — the embedder's
catch-all parser handles this):
```json
{
  "vectors": null,
  "base_resp": {"status_code": 1002, "status_msg": "rate limit exceeded(RPM)"}
}
```

For batch input (`"input": ["a", "b"]`), `data` is a list of N objects.
The embedder calls per-item so each request benefits from the in-memory
cache; do not "optimize" to batched calls without re-running the cache tests.

**Status codes:**
- `200` — success (even when rate-limited, the HTTP status is 200 and the
  rate-limit signal is in the body's `base_resp`)
- `401` — invalid/missing API key
- `4xx/5xx` — transient; the embedder fails open to zero vector

**Dim and norm:** 1024 floats per vector, L2-normalized (norm ≈ 1.0).
Verified live: `sum(x*x for x in emb)**0.5 == 1.0`.

**Base URLs / API keys:**
- Kimi: `https://api.kimi.com/coding/v1` (the embedder appends
  `/embeddings`; do not include the path in `HERMES_EMBED_BASE_URL`).
- API key: explicit `HERMES_EMBED_API_KEY` wins, else falls back to
  `KIMI_API_KEY` from the env. Don't re-paste the key.

**Model aliases on the Kimi endpoint** (all return 1024-dim, all served by
the same endpoint, all free; the alias is a quality/style choice only):
`bge_m3_embed`, `bge-m3`, `bge-large`, `bge-large-en`, `bge-large-zh`,
`nomic-embed-text`, `text-embedding-3-small`, `text-embedding-v1`, `embedding-2`.

## Model → dimension table

The agent_memory.content_vector dim is **whatever the configured model
returns**. After the 2026-06 migration it is `vector(1024)` (BGE-M3). The
embedder refuses to silently accept a wrong-dim response; the dim check
in `embed()` raises (when `HERMES_EMBED_FAIL_OPEN=0`) or falls back to
a zero vector (default) with a `WARNING` log.

| Model | Native dim | Quality (MTEB retrieval avg) | Self-hostable? | Free? |
|---|---|---|---|---|
| `bge_m3_embed` (Kimi / BGE-M3 weights) | 1024 | ~63 (multilingual) | ✅ via Ollama (`bge-m3`) | ✅ via Kimi |
| `nomic-embed-text` (v1.5) | 768 | 62.39 | ✅ via Ollama | ✅ via Ollama Cloud (not currently — see below) |
| `mxbai-embed-large` | 1024 | ~64 | ✅ via Ollama | ✅ via Ollama Cloud (not currently) |
| `bge-large-en-v1.5` | 1024 | ~64 | ✅ via Ollama | ✅ via Ollama Cloud (not currently) |
| `snowflake-arctic-embed` (335M) | 1024 | ~62 | ✅ via Ollama | ✅ via Ollama Cloud (not currently) |
| `text-embedding-3-small` (OpenAI) | 1536 native, 512/1536 settable | 62.3 | ❌ | ❌ ~$0.02 / 1M tokens |
| `text-embedding-3-large` (OpenAI) | 3072 native, 256-3072 settable | 64.6 | ❌ | ❌ ~$0.13 / 1M tokens |
| `voyage-3` | 1024 | 67 | ❌ | ❌ |
| `cohere-embed-english-v3.0` | 1024 | 64 | ❌ | ❌ free tier exists |

If you change the model, you must:
1. Update `HERMES_EMBED_MODEL` and `HERMES_EMBED_DIM` in `.env`.
2. Run `migrations/<NNN>_<model>.sql` to resize the column (see the
   `001_embedding_dim.sql` → `002_recreate_hnsw.sql` pattern in the
   plugin's `migrations/` directory).
3. Blow away the embedding cache: `rm -rf ~/.cache/hermes/embeddings/`.
4. Re-run `scripts/backfill_embeddings.py` against the new model.
5. Run `scripts/verify_embeddings.py` to confirm.

If you skip the migration and just swap `HERMES_EMBED_MODEL`, the embedder
will raise dim-mismatch on every save and fail-open to zero vectors. The
table will end up with a mix of old-dim and zero vectors, which is the
worst possible state — search results will be dominated by the zero rows.

## Ollama Cloud free tier limits (and the "doesn't work for embeddings" caveat)

Ollama Cloud's free tier is real for chat models (40+ on a typical account)
but **does not currently serve embedding models**. The `/api/tags` endpoint
lists 40 chat models and zero embed models. `/api/pull nomic-embed-text`
returns `404 model not found`. `/api/embed` returns `401 unauthorized`
even with a valid `OLLAMA_API_KEY`. This is a known general Ollama Cloud
limitation as of mid-2026; open GitHub issues reference this in
openclaw/ollama repos. Don't recommend `ollama_cloud` for embeddings.

For chat models (not what the embedder does), Ollama Cloud's free tier is
the same as before:
- 100 RPM per API key for chat models
- Rate-limit response: HTTP 429 with `{"error": "rate limit exceeded"}`

The embedder does NOT retry on 429. It fails open to a zero vector and
logs a WARNING. If you're pushing memory writes in a tight loop and
seeing `embedder.stats.errors` climb, slow down or switch to a working
provider (Kimi today; self-hosted Ollama tomorrow).

## Why no batch API call

`/api/embed` does support `"input": ["a", "b", "c"]` for batched calls.
We deliberately do not use it in the embedder because:

1. Per-item calls give us per-item cache hits. A batch that is 50% cache
   hits becomes 50 redundant embedding calls.
2. Batched failures fail the whole batch. Per-item calls let a single bad
   text fail open without losing the rest.
3. A batch of 100+ texts blows past the Ollama Cloud timeout.

If you ever need to bypass this for a large backfill, write a one-off
script that calls `/api/embed` directly with batched input and writes
results in one transaction. Don't add a batched code path to `embedder.py`.

## Debugging a "no embeddings are being stored" report

Run these in order. Each one isolates a different failure mode.

```bash
# 1. Is the embedder module loaded?
python -c "from plugins.memory.postgres.embedder import get_embedder; e = get_embedder(); print(e.provider, e.model, e.dim)"

# 2. Can it call the provider?
HERMES_EMBED_FAIL_OPEN=0 python -c "from plugins.memory.postgres.embedder import Embedder; print(Embedder().embed('hello world')[:4])"
# If this raises, you have a network / auth / model-name problem.

# 3. Are rows actually getting embedded? (Replace 1024 with your dim if you
#    re-migrated to a different model.)
psql ... -c "SELECT count(*) FILTER (WHERE content_vector <> array_fill(0, ARRAY[1024])::vector) AS embedded, count(*) AS total FROM agent_memory WHERE is_active = TRUE"
# embedded should equal total. Anything less means fail-open fired.

# 4. Is hybrid search actually combining the scores?
psql ... -c "SELECT text_rank, vector_sim, (0.5 * text_rank + 0.5 * vector_sim) AS hybrid FROM (SELECT ts_rank(to_tsvector('english', content), plainto_tsquery('english', 'rain')) AS text_rank, 1 - (content_vector <=> (SELECT content_vector FROM agent_memory LIMIT 1)) AS vector_sim FROM agent_memory WHERE to_tsvector('english', content) @@ plainto_tsquery('english', 'rain') LIMIT 5) sub"
# You want non-zero values in BOTH columns. If text_rank > 0 and vector_sim = 0, your content_vector is all zeros (fail-open in a loop).
```

When all four pass, answer "yes" to the user. When any one fails, report
the specific failure and the env var / SQL that proves it.
