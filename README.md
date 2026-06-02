# PostgreSQL Memory Provider for Hermes Agent

Vector memory backed by **PostgreSQL + pgvector**. Real embeddings, hybrid
search (FTS + cosine), categories, tags, JSONB metadata, TTL, soft deletes.
Free default embedder, non-destructive schema migration, content-addressable
embedding cache.

> Author: Shakib Haris · License: MIT · Tested with PostgreSQL 14–18,
> pgvector 0.5–0.8, Hermes Agent 1.x.

## Why

Hermes Agent ships with built-in memory (`MEMORY.md` / `USER.md`) and
pluggable memory providers. The catalog has hosted-service providers
(Honcho, Mem0, Supermemory, OpenViking, ByteRover, RetainDB) and one
local research prototype (Holographic). **There is no first-class
database-backed memory provider.**

This plugin fills that gap. It is built around three principles:

1. **One model per table.** pgvector's similarity operators only produce
   meaningful scores when both vectors share the same embedding space.
   We pick a model, configure it, and stick with it.
2. **Non-destructive upgrades.** Switching to a new model adds a sidecar
   column rather than dropping data. The old column is queryable until
   the user explicitly runs `finalize-cutover`.
3. **Free by default.** The default embedder (Kimi's `bge_m3_embed` at
   `api.kimi.com/coding/v1`) is free with the `KIMI_API_KEY` already in
   your `.env`. The pluggable embedder also supports self-hosted Ollama.

## Features

- **Vector embeddings** (1024-dim BGE-M3 by default) with HNSW index
- **Full-text search** via GIN index on `to_tsvector`
- **Hybrid search** combining vector + text relevance (50/50 default, configurable)
- **Categories** — 8 built-in: `user_preference`, `user_profile`,
  `environment`, `project_convention`, `tool_quirk`, `lesson_learned`,
  `workflow`, `fact`
- **Tags** — array of strings for filtering
- **JSONB metadata** — structured provenance
- **TTL** — `expires_at` for auto-expiring memories
- **Soft deletes** — `is_active` flag
- **Content-addressable embedding cache** — sha256(provider|model|text),
  in-memory + on-disk, refuses to cache fail-open zero vectors
- **Pluggable embedder** — `kimi` (free default), `ollama_local`,
  `ollama_cloud`, `noop`
- **Fail-open** — provider errors fall back to a zero vector and log a
  warning; the memory is still stored. The disk cache is guarded
  against poisoning (a transient 401/429 does not corrupt the cache).
- **Non-destructive migration** — sidecar v2 column, manual cutover

## Tools

The plugin exposes 5 tools to the agent:

| Tool | Purpose |
|---|---|
| `pg_remember` | Store a memory |
| `pg_search` | Hybrid search (FTS + cosine) |
| `pg_recent` | List recent memories |
| `pg_forget` | Soft-delete a memory by ID |
| `pg_status` | Connection stats, live column, embedder health |

## Install

### 1. Drop the plugin into your Hermes Agent checkout

```bash
cd ~/.hermes/hermes-agent
git clone https://github.com/skb50bd/hermes-postgres-memory.git /tmp/hermes-postgres-memory
cp -r /tmp/hermes-postgres-memory/plugins/memory/postgres plugins/memory/postgres
cp -r /tmp/hermes-postgres-memory/skills/devops/hermes-postgres-memory skills/devops/hermes-postgres-memory
```

Or use the bundled `install.sh`:

```bash
curl -fsSL https://raw.githubusercontent.com/skb50bd/hermes-postgres-memory/main/install.sh | bash
```

### 2. Configure `.env`

Add to `~/.hermes/.env`:

```bash
# Required: PostgreSQL connection
POSTGRES_HOST=10.49.0.33
POSTGRES_PORT=5432
POSTGRES_USER=hermes
POSTGRES_PASSWORD=change_me
POSTGRES_DATABASE=hermes

# Embedder — defaults are kimi, free, 1024-dim. Override only if you want.
HERMES_EMBED_PROVIDER=kimi
HERMES_EMBED_MODEL=bge_m3_embed
HERMES_EMBED_DIM=1024
# HERMES_EMBED_API_KEY=...  # falls back to KIMI_API_KEY

# Embedding cache (optional)
HERMES_EMBED_CACHE_DIR=~/.cache/hermes/embeddings
HERMES_EMBED_CACHE=1
HERMES_EMBED_FAIL_OPEN=1
```

### 3. Activate the provider

In `~/.hermes/config.yaml`:

```yaml
memory:
  provider: postgres
```

### 4. Initialize the schema

If you're starting fresh, run the schema in `plugins/memory/postgres/sql/000_schema.sql`
(see "Schema" below). If you have an existing `agent_memory` table, run
the migration:

```bash
# Pre-flight: confirm we have DDL rights and the table looks right.
hermes postgres-memory preflight

# 1. One-time: transfer ownership of agent_memory to hermes.
psql -h $POSTGRES_HOST -U postgres -d $POSTGRES_DATABASE \
  -f plugins/memory/postgres/migrations/000_grant_ddl_to_hermes.sql

# 2. Add the v2 sidecar column + settings table.
psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DATABASE \
  -f plugins/memory/postgres/migrations/001_add_v2_column.sql

# 3. Backfill the v2 column with real embeddings (~30s for 50 rows).
hermes postgres-memory backfill

# 4. Build the HNSW index on the v2 column. (CONCURRENTLY — runs outside a txn.)
psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DATABASE \
  -f plugins/memory/postgres/migrations/002_hnsw_v2.sql

# 5. Switch the plugin's live column to v2.
psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DATABASE \
  -f plugins/memory/postgres/migrations/003_switch_live_column.sql

# (Optional, later) Drop the v1 column. IRREVERSIBLE.
hermes postgres-memory finalize-cutover --yes
```

You can roll back at any time before `finalize-cutover` by setting
`live_vector_column` back to `v1`:

```bash
hermes postgres-memory vector-column --set v1
```

The plugin immediately reads from the new column on the next request.

## Configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `POSTGRES_HOST` | `localhost` | DB host |
| `POSTGRES_PORT` | `5432` | DB port |
| `POSTGRES_USER` | `hermes` | DB user |
| `POSTGRES_PASSWORD` | (required) | DB password |
| `POSTGRES_DATABASE` | `hermes` | DB name |
| `HERMES_EMBED_PROVIDER` | `kimi` | `kimi` / `ollama_local` / `ollama_cloud` / `noop` |
| `HERMES_EMBED_MODEL` | `bge_m3_embed` | Model name |
| `HERMES_EMBED_DIM` | `1024` | Must match the live vector column |
| `HERMES_EMBED_BASE_URL` | (provider default) | Override the API base |
| `HERMES_EMBED_API_KEY` | unset | Falls back to `KIMI_API_KEY` / `OLLAMA_API_KEY` |
| `HERMES_EMBED_TIMEOUT` | `10` | HTTP timeout, seconds |
| `HERMES_EMBED_CACHE_DIR` | `~/.cache/hermes/embeddings` | Disk cache root |
| `HERMES_EMBED_CACHE` | `1` | `0` disables disk cache |
| `HERMES_EMBED_FAIL_OPEN` | `1` | `0` raises on provider error |
| `HERMES_POSTGRES_POOL_MIN` | `0` | Min connections in pool |
| `HERMES_POSTGRES_POOL_MAX` | `2` | Max connections in pool |
| `HERMES_POSTGRES_CONNECT_TIMEOUT` | `5` | TCP connect timeout, seconds |
| `HERMES_POSTGRES_STATEMENT_TIMEOUT_MS` | `10000` | Per-statement timeout |
| `HERMES_POSTGRES_IDLE_TX_TIMEOUT_MS` | `30000` | Idle-in-transaction timeout |
| `HERMES_POSTGRES_FTS_WINDOW_MIN` | `40` | Min candidate window for FTS |
| `HERMES_POSTGRES_HYBRID_TEXT_WEIGHT` | `0.5` | Hybrid blend (0.0..1.0) |

## CLI subcommands

`hermes postgres-memory <subcommand>`:

- `status` — Print provider status as JSON
- `vector-column [--set v1|v2]` — Show or set the live vector column
- `backfill [--dry-run] [--batch N] [--limit N] [--column NAME]` — Backfill the v2 column
- `preflight` — Pre-migration checks (ownership, schema, dim)
- `finalize-cutover --yes` — Drop the v1 (1536-dim) column. **IRREVERSIBLE.**

## Schema

The plugin expects these tables:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memory_categories (
    id smallint PRIMARY KEY,
    name varchar(50) UNIQUE NOT NULL
);

INSERT INTO memory_categories (id, name) VALUES
(1, 'user_preference'),
(2, 'user_profile'),
(3, 'environment'),
(4, 'project_convention'),
(5, 'tool_quirk'),
(6, 'lesson_learned'),
(7, 'workflow'),
(8, 'fact');

CREATE TABLE IF NOT EXISTS agent_memory (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    category_id smallint REFERENCES memory_categories(id),
    target varchar(20) DEFAULT 'memory',
    content text NOT NULL,
    content_vector vector(1024),          -- legacy 1.0.x column (1536-dim in older deployments)
    content_vector_v2 vector(1024),         -- 1.1.0+ sidecar; will replace content_vector after cutover
    source_session uuid,
    confidence smallint DEFAULT 80,
    is_active boolean DEFAULT true,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    expires_at timestamptz,
    tags text[] DEFAULT '{}',
    metadata jsonb DEFAULT '{}'
);

CREATE INDEX idx_memory_vector_hnsw_v2
    ON agent_memory USING hnsw (content_vector_v2 vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_memory_fts
    ON agent_memory USING gin (to_tsvector('english', content));
CREATE INDEX idx_memory_target ON agent_memory (target);
CREATE INDEX idx_memory_category ON agent_memory (category_id);
CREATE INDEX idx_memory_active ON agent_memory (is_active) WHERE is_active = true;
CREATE INDEX idx_memory_tags ON agent_memory USING gin (tags);
CREATE INDEX idx_memory_metadata ON agent_memory USING gin (metadata jsonb_path_ops);
CREATE INDEX idx_memory_created ON agent_memory (created_at DESC);

CREATE TABLE IF NOT EXISTS agent_memory_settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

The bundled `sql/000_schema.sql` creates this for fresh installs. The
migrations in `migrations/` upgrade existing schemas non-destructively.

## Embedder providers

| Provider | Free? | Dim | Quality | When to use |
|---|---|---|---|---|
| `kimi` (default) | ✅ | 1024 | Top-tier MTEB, multilingual | Recommended default. `KIMI_API_KEY` in `.env` already works. |
| `ollama_local` | ✅ | model-dependent | matches Kimi | Self-hosted Ollama. Same model `bge-m3` works for free. |
| `ollama_cloud` | ❌ | n/a | n/a | **Ollama Cloud's public catalog is chat-only.** Don't use this provider. |
| `noop` | ✅ | any | n/a | Test/fallback only. Always returns a zero vector. |

Why Kimi? As of June 2026, Kimi's `https://api.kimi.com/coding/v1/embeddings`
is the only free, working embedding endpoint among the providers with
keys in our `.env`. Ollama Cloud's free tier does not serve embedding
models. Moonshot's `api.moonshot.cn` rejects the same key. OpenAI's
`text-embedding-3-*` is paid. The Kimi endpoint is OpenAI-compatible;
the same code path works for OpenRouter, Together, vLLM, etc.

## Verifying the install

After install, run the verification probe:

```bash
set -a; source ~/.hermes/.env; set +a
python skills/devops/hermes-postgres-memory/scripts/verify_embeddings.py
```

Expected output: 0 zero-vec rows, non-zero `vector_sim` in search
results, embedder stats with non-zero `misses`. Exit code 0 means
"yes, embeddings are actually working."

## License

MIT. See `LICENSE`.

## See also

- `skills/devops/hermes-postgres-memory/SKILL.md` — operational
  troubleshooting, embedding-provider landscape, migration playbook.
- `plugins/memory/postgres/CHANGELOG.md` — version history.
