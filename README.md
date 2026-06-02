# PostgreSQL Memory Provider for Hermes Agent

Vector memory backed by **PostgreSQL + pgvector**. Real embeddings, hybrid
search (FTS + cosine), categories, tags, JSONB metadata, TTL, soft deletes.
Free, self-hosted, no per-API-call cost.

Supports **3 embedding dims out of the box** — 768, 1024, 1536 — and lets
you switch between them at runtime via `hermes postgres-memory model-set`.

---

## 🚀 First-time install (5 minutes)

The one-shot installer handles everything: creates the database + role +
pgvector extension, installs the plugin + skill, configures `.env` and
`config.yaml`, runs the preflight. **Idempotent. Re-run any time.**

```bash
git clone https://github.com/skb50bd/hermes-postgres-memory.git /tmp/hpm
cd /tmp/hpm
./plugins/memory/postgres/scripts/bootstrap.sh
```

It will ask you for:
- Your **Postgres superuser** password (one-time, to create the role + DB)
- A password for the new `hermes` role (the plugin authenticates as this)

After it finishes, edit `~/.hermes/.env` to uncomment `KIMI_API_KEY=*** and
add your key (free at https://platform.moonshot.cn), then restart the
gateway:

```bash
hermes gateway restart
```

**If something fails** before any side effects, run the preflight:

```bash
./plugins/memory/postgres/scripts/diagnose.sh
```

It walks 16 prerequisites and tells you, in plain language, which are
missing. For the 5-command TL;DR see
[`bootstrap-message-short.txt`](bootstrap-message-short.txt). For the
full walkthrough, see [`bootstrap-message.txt`](bootstrap-message.txt).

---

## Switching embedding models

You have three dims. You can switch between them at any time.

### Switch the default dim (one command)
```bash
hermes postgres-memory model-set --dim 768
hermes postgres-memory model-set --dim 1024
hermes postgres-memory model-set --dim 1536
```

This updates `agent_memory_settings.default_dim`. New writes go to the new
column. Old rows that already have a vector at the new dim are immediately
queryable. Old rows that don't have the new dim yet need to be backfilled.

### Backfill a non-default dim for existing rows
```bash
# Backfill every dim (default)
hermes postgres-memory backfill

# Backfill just one dim
hermes postgres-memory backfill --dim 768
hermes postgres-memory backfill --dim 1536

# Dry-run to see what would happen
hermes postgres-memory backfill --dry-run

# Limit how many rows to process
hermes postgres-memory backfill --limit 1000

# Adjust batch size for rate-limited APIs
hermes postgres-memory backfill --batch 8
```

### Override the model for one dim
```bash
# Use mxbai-embed-large on ollama for 1024-dim
hermes postgres-memory model-set --dim 1024 --provider ollama_local --model mxbai-embed-large

# Use OpenAI for 1536-dim
hermes postgres-memory model-set --dim 1536 --provider openai --model text-embedding-3-small
```

Note: providers other than kimi/ollama_local/ollama_cloud/noop are not
implemented in `embedder.py` yet. Adding a new provider is ~50 lines: see
the dispatch in `Embedder._embed_live`.

---

## Schema layout

```
agent_memory
├── id (uuid)
├── category_id (smallint → memory_categories.id)
├── target (varchar)              — 'memory' or 'user'
├── content (text)                — the fact
├── vector_768   (vector(768))    — nullable, HNSW-indexed
├── vector_1024  (vector(1024))   — nullable, HNSW-indexed
├── vector_1536  (vector(1536))   — nullable, HNSW-indexed
├── content_vector (vector)       — LEGACY (pre-1.2.0). See migration 003.
├── source_session (uuid)
├── confidence (smallint, default 80)
├── is_active (boolean)           — soft delete
├── created_at, updated_at, expires_at (timestamptz)
├── tags (text[])
└── metadata (jsonb)

agent_memory_settings
├── key (text, PK)                — e.g. 'default_dim'
└── value (jsonb)                 — e.g. '"1024"' or '768' as JSON number-string

agent_memory_models
├── dim (smallint, PK)            — 768, 1024, or 1536
├── provider (text)               — 'kimi', 'ollama_local', 'ollama_cloud', 'noop'
├── model (text)                  — e.g. 'bge_m3_embed', 'nomic-embed-text'
├── base_url (text, nullable)
└── api_key_env (text, nullable)  — name of the env var holding the API key
```

A row can have any subset of the three vector columns populated. Switching
dims does NOT lose data — old vectors stay in their original columns, and
a `pg_search` always reads the column matching the configured default.

---

## Migrations

For users upgrading from 1.0.x / 1.1.0:

```bash
# 1. As a superuser (postgres role), transfer table ownership to hermes
PGPASSWORD="..." psql -h 10.49.0.33 -U postgres -d hermes \
  -f plugins/memory/postgres/migrations/000_grant_ddl_to_hermes.sql

# 2. As the hermes role, add per-dim columns + settings + models
PGPASSWORD="..." psql -h 10.49.0.33 -U hermes -d hermes \
  -f plugins/memory/postgres/migrations/001_add_per_dim_columns.sql

# 3. Build per-dim HNSW indexes (CONCURRENTLY — no downtime)
PGPASSWORD="..." psql -h 10.49.0.33 -U hermes -d hermes \
  -f plugins/memory/postgres/migrations/002_hnsw_per_dim.sql

# 4. Copy legacy content_vector data into the matching per-dim column
PGPASSWORD="..." psql -h 10.49.0.33 -U hermes -d hermes \
  -f plugins/memory/postgres/migrations/003_migrate_legacy_content_vector.sql

# 5. (Later) backfill the other dims
hermes postgres-memory backfill
# OR
python plugins/memory/postgres/scripts/backfill_embeddings.py

# 6. (Later, irreversible) drop the legacy content_vector
hermes postgres-memory finalize-cutover --yes
```

The plugin auto-detects the existing column layout on init. If you skip
step 4, the plugin will still work — but only rows that have a vector
at the configured default_dim will be searchable.

---

## Configuration reference

### Environment variables

| Var | Default | Notes |
|---|---|---|
| `POSTGRES_HOST` | `localhost` | |
| `POSTGRES_PORT` | `5432` | |
| `POSTGRES_USER` | `hermes` | |
| `POSTGRES_PASSWORD` | (required) | |
| `POSTGRES_DATABASE` | `hermes` | |
| `KIMI_API_KEY` | (env) | Free 1024-dim embedder. https://platform.moonshot.cn |
| `OLLAMA_API_KEY` | (env) | Only needed for ollama_cloud |
| `HERMES_EMBED_PROVIDER_<dim>` | per-dim default | Override provider for a dim |
| `HERMES_EMBED_MODEL_<dim>` | per-dim default | Override model for a dim |
| `HERMES_EMBED_BASE_URL_<dim>` | per-dim default | Override API endpoint |
| `HERMES_EMBED_API_KEY_<dim>` | (none) | Direct per-dim API key |
| `HERMES_EMBED_API_KEY` | (none) | Shared API key for all dims |
| `HERMES_EMBED_DEFAULT_DIM` | `1024` | Fallback default when settings table is empty |
| `HERMES_EMBED_FAIL_OPEN` | `1` | If 0, embed errors raise EmbeddingError |
| `HERMES_POSTGRES_HYBRID_TEXT_WEIGHT` | `0.5` | 0..1. Weight of FTS rank vs cosine in hybrid score |
| `HERMES_POSTGRES_POOL_MIN` | `0` | Min idle connections in the pool |
| `HERMES_POSTGRES_POOL_MAX` | `2` | Max concurrent connections |

### Per-dim defaults (built-in)

| Dim | Provider | Model | Where to get the key |
|---|---|---|---|
| 768 | `ollama_local` | `nomic-embed-text` | `ollama pull nomic-embed-text` |
| 1024 | `kimi` | `bge_m3_embed` | `KIMI_API_KEY` (https://platform.moonshot.cn, free tier) |
| 1536 | `kimi` (default) or `openai` | `text-embedding-3-small` | `KIMI_API_KEY` or `OPENAI_API_KEY` |

To use OpenAI for 1536-dim, run:
```bash
hermes postgres-memory model-set --dim 1536 --provider openai --model text-embedding-3-small
```
(Note: the `openai` provider is not yet implemented in `embedder.py` —
use the Kimi 1536 default or open a PR.)

---

## CLI

```
hermes postgres-memory status
hermes postgres-memory model-list
hermes postgres-memory model-set --dim <768|1024|1536> [--provider X --model Y]
hermes postgres-memory backfill [--dim N] [--dry-run] [--batch N] [--limit N]
hermes postgres-memory preflight
hermes postgres-memory finalize-cutover --yes
hermes postgres-memory vector-column --set v1|v2       # DEPRECATED, mapped to --dim 1536/1024
```

## Helper scripts

The repo ships three first-class installer / uninstaller / preflight
scripts. They live under `plugins/memory/postgres/scripts/`:

| Script | What it does |
|---|---|
| `bootstrap.sh` | One-shot installer. Asks for the superuser password, creates the database + role + pgvector extension, installs the schema, copies plugin + skill files, patches `.env` and `config.yaml`, runs the preflight. Interactive by default, `--non-interactive` for scripted deploys. |
| `diagnose.sh` | Preflight checker. Walks 16 prerequisites (hermes-agent checkout, `.env`, psql on PATH, pgvector, role, schema, indexes, etc.) and prints a pass/fail table. Re-runnable. `--json` for automation. |
| `uninstall.sh` | Inverse of `bootstrap.sh`. Three modes: `--plugin` (files only), `--db` (drop tables), `--all` (both). Plus `--role` and `--database` to drop the role and DB. Asks before each destructive step. |

The database creation SQL that the bootstrap script invokes lives at
`plugins/memory/postgres/sql/000_create_database_and_role.sql`. It is
the **only** file in the plugin that requires superuser privileges, and
it accepts GUCs (`-v dbname=...`, `-v rolename=...`, `-v pw=...`) so
everything is customizable.

---

## Troubleshooting

### Search returns no results

Run `hermes postgres-memory status`. Check `per_dim_embedded` — if the dim
you configured is at zero, you need to either:
- (a) backfill that dim: `hermes postgres-memory backfill --dim <dim>`
- (b) switch the default dim: `hermes postgres-memory model-set --dim <dim>`

### Embedder returns zero vectors

The embedder fails open to `[0.0] * dim` on provider errors and refuses
to cache them. Check:
- `KIMI_API_KEY` is set in the same env Hermes Agent is running in
- For Ollama: `curl http://localhost:11434/api/tags` returns your pulled model
- For 1536-dim: only Kimi is implemented, and Kimi returns 1024-dim
  regardless of model name. So 1536-dim requires `OPENAI_API_KEY` and
  the `openai` provider (not yet implemented; use 1024 for now).

### Old `content_vector` column

Pre-1.2.0 had a single `content_vector` column. After upgrading, run
migration `003_migrate_legacy_content_vector.sql` to copy data into the
matching per-dim column. The legacy column is still readable (the plugin
auto-detects it) until you run `finalize-cutover`.

### 21/30 tests pass (or some are red)

Run `pytest tests/ -v`. The 3 cases most likely to break:
- `test_add_memory_writes_to_default_dim_column` — depends on
  `_read_model_config_for_dim` being patchable. If you refactor the
  embedder factory, ensure tests can still find the plugin's function
  via sys.modules.
- `test_search_memories_runs_hybrid_query_with_query_embedding` —
  the placeholder/param drift guard. If you add a new WHERE clause
  to `search_memories`, the param list MUST grow correspondingly or
  this test will catch the mismatch.

---

## License

MIT © Shakib Haris. See [LICENSE](LICENSE).
