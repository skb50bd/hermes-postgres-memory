# Embedding providers

The greenfield schema stores embeddings in per-dimension columns:

- `vector_768`
- `vector_1024`
- `vector_1536`

Each column must contain vectors from one embedding model family. Mixing models
inside the same column silently ruins similarity scores.

## Defaults

- 768: `ollama_local` / `nomic-embed-text`
- 1024: `kimi` / `bge_m3_embed`
- 1536: `minimax` / `embo-01`

## Configure

```bash
hermes postgres-memory model-list
hermes postgres-memory model-set --dim 1024 --provider kimi --model bge_m3_embed
```

Embedder keys are resolved from the SQL model registry's `api_key_env` value,
then per-dim env overrides, then generic fallbacks.

Typical `.env`:

```bash
PG_MEM_DB_CONN_STR='postgresql://hermes:***@host:5432/hermes'
KIMI_API_KEY='***'
MINIMAX_API_KEY='***'       # only needed for 1536 default
OLLAMA_API_KEY='***'        # only needed for Ollama Cloud/local auth setups
```

## Verify non-empty vectors

```sql
SELECT count(*) AS active FROM agent_memory WHERE is_active = TRUE;
SELECT count(*) AS embedded_1024
FROM agent_memory
WHERE is_active = TRUE
  AND vector_1024 IS NOT NULL
  AND vector_1024 <> array_fill(0, ARRAY[1024])::vector;
```

## Backfill

```bash
python plugins/memory/postgres/scripts/backfill_embeddings.py --dim 1024
python plugins/memory/postgres/scripts/backfill_embeddings.py
```
