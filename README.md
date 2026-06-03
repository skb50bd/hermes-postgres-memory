# Hermes Postgres Memory

Greenfield PostgreSQL + pgvector memory backend for Hermes Agent.

It provides:

- Hybrid full-text + vector search
- Runtime-selectable embedding dimensions: 768, 1024, 1536
- Per-dimension model registry
- HNSW indexes for every supported vector column
- Tags, categories, soft deletes, TTL metadata, and status tooling

This project intentionally documents only the current greenfield install path.
Git already stores history; users need the current install path, not archaeology.

## Required environment

Set a single libpq connection string in `~/.hermes/.env`:

```bash
PG_MEM_DB_CONN_STR='postgresql://hermes:***@10.0.0.1:5432/hermes'
KIMI_API_KEY='***'
```

`PG_MEM_DB_CONN_STR` is the only supported application database connection setting.
It may be a normal URI DSN or a semicolon connection string such as
`Host=...;Port=5432;Database=hermes;Username=hermes;Password=...`.

## Install

```bash
git clone https://github.com/skb50bd/hermes-postgres-memory.git /tmp/hpm
cd /tmp/hpm
./plugins/memory/postgres/scripts/bootstrap.sh
```

If the database already exists and `~/.hermes/.env` already contains
`PG_MEM_DB_CONN_STR`, install only the plugin files:

```bash
./install.sh
```

Then set Hermes to use the provider:

```yaml
memory:
  memory_enabled: true
  provider: postgres
```

Restart Hermes after changing `.env` or `config.yaml`.

## Database schema

Run `plugins/memory/postgres/sql/000_schema.sql` against the database in
`PG_MEM_DB_CONN_STR`.

The greenfield schema creates:

- `memory_categories`
- `agent_memory`
  - `vector_768 vector(768)`
  - `vector_1024 vector(1024)`
  - `vector_1536 vector(1536)`
- `agent_memory_settings`
- `agent_memory_models`
- HNSW indexes on all three vector columns
- full-text, category, target, tag, metadata, and created-at indexes

## CLI

```bash
hermes postgres-memory preflight
hermes postgres-memory status
hermes postgres-memory model-list
hermes postgres-memory model-set --dim 1024
hermes postgres-memory backfill --dim 1024
```

## Smoke test

```bash
hermes postgres-memory preflight
hermes postgres-memory status
```

Then in a fresh Hermes session:

```text
pg_remember(content="postgres memory is live", category="fact")
pg_search(query="postgres memory")
```

## Embedding defaults

- 768: `ollama_local` / `nomic-embed-text`
- 1024: `kimi` / `bge_m3_embed` (default)
- 1536: `minimax` / `embo-01`

Override the default dimension or model through:

```bash
hermes postgres-memory model-set --dim 768
hermes postgres-memory model-set --dim 1024 --provider kimi --model bge_m3_embed
```

## Backfill

Backfill fills missing vectors for existing rows. It is idempotent.

```bash
python plugins/memory/postgres/scripts/backfill_embeddings.py --dim 1024
python plugins/memory/postgres/scripts/backfill_embeddings.py
```

Without `--dim`, all supported dimensions are filled.

## Uninstall

```bash
plugins/memory/postgres/scripts/uninstall.sh --plugin
plugins/memory/postgres/scripts/uninstall.sh --db --yes
plugins/memory/postgres/scripts/uninstall.sh --all --yes
```

The database step drops plugin tables. It does not remove the PostgreSQL role,
database, or `vector` extension unless explicitly requested.
