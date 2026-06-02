---
name: hermes-postgres-memory
description: "Install, configure, troubleshoot, and harden the PostgreSQL/pgvector memory provider for Hermes Agent — onboarding, hybrid search, multi-dim embeddings, non-destructive migration, ownership transfer."
version: 1.6.0
author: Shakib Haris
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes-agent, memory, postgres, pgvector, embeddings, onboarding, bootstrap, preflight, multi-dim, troubleshooting, connection-pooling, migration-privileges, ownership-transfer, sidecar-column, cutover]
    related_skills: [hermes-agent, hermes-gateway-troubleshooting, systematic-debugging]
---

# Hermes Postgres Memory Provider

A drop-in PostgreSQL + pgvector memory backend for Hermes Agent. Real
embeddings (Kimi BGE-M3, Ollama nomic-embed-text, or OpenAI), hybrid
FTS + cosine search, three embedding dims (768 / 1024 / 1536) with
runtime switching, non-destructive schema migration.

This skill has two halves:

1. **Onboarding** (top of file) — what to do when the user asks you to
   install the plugin for the first time, or upgrade an existing
   1.0/1.1 install. Pre-flight checks, the canonical install path, and
   the failure modes the bootstrap script handles.
2. **Operations** (bottom of file) — diagnosing connection problems,
   tuning connection limits, embedding provider details, migration
   ownership transfer, the sidecar cutover. Load these when something
   is already broken.

If you only have time to read one section, read **"Onboarding: first-
time install"** below. Everything else is reference material.

## When to Use

- The user says "install the postgres memory plugin" or
  "switch my memory to postgres" or "use pgvector for memory"
- The user gives you Postgres connection details and asks you to
  configure a memory provider
- `hermes memory status` shows the provider as installed but
  unavailable
- `pg_search` returns nothing even though `pg_remember` worked
- `hermes postgres-memory preflight` reports failures
- A migration fails with "must be owner of table agent_memory"
- The user wants to switch between 768 / 1024 / 1536 embedding dims
- The user wants to A/B test embedding models
- You're seeing "too many connections for role 'hermes'" in the
  gateway log
- The user wants to remove the plugin (use `uninstall.sh`)

## Onboarding: first-time install

This is the canonical 5-step flow. Every first-time install should
follow this sequence. Each step is a no-op if the prerequisite is
already in place, so re-running is safe.

### Step 1 — Pre-flight: load this skill + run `diagnose.sh`

Before you install anything, load this skill and run the preflight:

```
/skill hermes-postgres-memory
```

The skill is also auto-installed by the bootstrap script into
`~/.hermes/hermes-agent/skills/devops/hermes-postgres-memory/`, so if
you haven't loaded it yet, run the bootstrap once first (it will copy
the skill into place, then you can `/skill` it on the next session).

The `diagnose.sh` script walks every prerequisite the plugin needs.
Clone the repo and run it:

```bash
git clone https://github.com/skb50bd/hermes-postgres-memory.git /tmp/hpm
/tmp/hpm/plugins/memory/postgres/scripts/diagnose.sh
```

Read the output. It tells you, in plain language, which of these are
missing:

- [ ] `psql` on `$PATH` (install `postgresql-client`)
- [ ] A reachable PostgreSQL 13+ server
- [ ] A database and application role the plugin can use
      (defaults: `hermes`/`hermes`)
- [ ] The `vector` (pgvector) extension installed in the target DB
- [ ] The application role owns the `public` schema
- [ ] `KIMI_API_KEY` (or another embedder key) in `~/.hermes/.env`
- [ ] `psycopg2` installed in the python that will run the plugin

**Do not skip this step.** A green diagnose means the bootstrap script
will succeed end-to-end. A red diagnose tells you exactly which
prerequisite to fix first.

For a deeper walk-through of each prerequisite (and the rationale for
why each is needed), see `references/onboarding-checklist.md`. For
the database-side specifics (extensions, role grants, the password-
piping caveat for `psql`), see `references/database-bootstrap.md`.

### Step 2 — Ask the user for the details you don't have

The diagnose script can probe what's already configured. The things it
*can't* probe are the user's secrets. Ask once, upfront, for:

- **Postgres superuser credentials** (host, port, role name, password)
  — needed once, to create the role + database + extension
- **Embedder API key** (default: `KIMI_API_KEY` from
  https://platform.moonshot.cn, free)
- (Optional) custom database / role names if they don't want the
  defaults of `hermes` / `hermes`

If the user says "use the same database I already have", ask them to
confirm:

- The database already has the `vector` extension installed
- The plugin role has DML on the `agent_memory` table
  (or the table doesn't exist yet)

If both are yes, the bootstrap script can skip the database creation
step and just install the schema into the existing database.

### Step 3 — Run `bootstrap.sh`

The bootstrap script is the one command that does everything end-to-
end. It's idempotent (every SQL uses `IF NOT EXISTS`, every file copy
is a plain `cp -R`):

```bash
git clone https://github.com/skb50bd/hermes-postgres-memory.git /tmp/hpm
cd /tmp/hpm
./plugins/memory/postgres/scripts/bootstrap.sh
```

Or non-interactive (for scripted onboarding):

```bash
export POSTGRES_HOST=10.49.0.33
export POSTGRES_PORT=5432
export POSTGRES_SUPERUSER=postgres
export POSTGRES_SUPERUSER_PASSWORD=*** NEW_DB_NAME=hermes
export NEW_ROLE_NAME=hermes
export NEW_ROLE_PASSWORD=***
....p.sh --non-interactive
```

What it does, in order:

1. Checks `psql` is installed and the hermes-agent checkout exists
2. Connects as the postgres superuser (test the connection first,
   refuse to continue if it fails)
3. Runs `sql/000_create_database_and_role.sql` — creates the role,
   the database, the `vector` extension, transfers ownership of the
   `public` schema
4. Runs `sql/000_schema.sql` — creates `agent_memory`,
   `agent_memory_settings`, `agent_memory_models`, `memory_categories`,
   the HNSW indexes, the FTS index, the per-dim B-tree indexes
5. Installs the plugin + skill into `~/.hermes/hermes-agent/`
6. Appends the `POSTGRES_*` block to `~/.hermes/.env` (with
   `KIMI_API_KEY=` left as a comment for the user to fill in)
7. Sets `memory.provider: postgres` in `~/.hermes/config.yaml` (if
   the `memory:` block exists; otherwise leaves a notice)
8. Re-runs `diagnose.sh` and prints the final report

**If any step fails, the script exits with a clear error.** Read the
error, fix the underlying problem (the diagnose script will help),
re-run `bootstrap.sh`. Don't try to fix a half-installed plugin by
hand.

### Step 4 — User adds the embedder API key + restarts the gateway

The bootstrap script leaves a placeholder in `~/.hermes/.env`:

```bash
# KIMI_API_KEY=***   # required for the embedder — get one at https://platform.moonshot.cn
```

**Ask the user** to uncomment that line and paste their key, then
restart the gateway:

```bash
hermes gateway restart
# or, if it's not a managed service:
pkill -f "hermes gateway" ; hermes gateway run &
```

The new `.env` and the new plugin only load on a fresh process. If
the user edited `.env` while the gateway was running, the gateway
won't see the change until restart.

### Step 5 — Verify + smoke test

```bash
hermes postgres-memory preflight
hermes postgres-memory status
hermes postgres-memory model-list
```

Expected output:

- `preflight` → every check passes
- `status` → `default_dim: 1024`,
  `per_dim_embedded: {768: 0, 1024: 0, 1536: 0}` (zero is correct on
  a fresh install; the rows have no content yet)
- `model-list` → 3 rows showing
  (768/1024/1536) → (provider/model/api-key-env)

Then in a fresh Hermes session (the `pg_remember` / `pg_search` tools
are session-scoped, so a restart is required to pick them up):

```
pg_remember(content="postgres plugin is live", category="fact")
pg_search(query="postgres plugin")
```

If `pg_search` returns the test memory, you're done. If not, the
diagnose flow at the bottom of this skill will help.

## Onboarding: upgrading from 1.0 / 1.1

If the user already has the legacy single-`content_vector` schema
(pre-1.2.0), the bootstrap script will detect the existing
`agent_memory` table and **refuse to overwrite it**. You have two
options:

**Option A: sidecar migration (non-destructive, recommended)**

Run the migration set in order:

```bash
# 0. As superuser — transfer ownership so the hermes role can do DDL
PGPASSWORD=*** psql -h <host> -U postgres -d hermes \
  -f ~/.hermes/hermes-agent/plugins/memory/postgres/migrations/000_grant_ddl_to_hermes.sql

# 1-4. As the hermes role — add per-dim columns, indexes, copy legacy data
for f in 001_add_per_dim_columns.sql 002_hnsw_per_dim.sql 003_migrate_legacy_content_vector.sql; do
    PGPASSWORD=*** psql -h <host> -U hermes -d hermes \
      -f ~/.hermes/hermes-agent/plugins/memory/postgres/migrations/$f
done

# 5. (later) backfill the other dims
hermes postgres-memory backfill

# 6. (later, irreversible) drop the legacy column
hermes postgres-memory finalize-cutover --yes
```

The plugin auto-detects the legacy column and uses it for searches
until `finalize-cutover --yes` runs. See `references/migration-
privileges.md` for why the ownership transfer is mandatory (and why
no `GRANT` will fix it).

**Option B: nuke and re-seed (destructive, only for fresh installs)**

If the user is OK losing all stored memories (e.g. they're testing
in a throwaway DB), drop the existing schema first and let the
bootstrap script start clean:

```bash
plugins/memory/postgres/scripts/uninstall.sh --db --yes
plugins/memory/postgres/scripts/bootstrap.sh
```

## Onboarding: uninstall

The `uninstall.sh` script is the inverse of `bootstrap.sh`. It has
three modes:

```bash
# Remove the plugin + skill files only (DB untouched)
plugins/memory/postgres/scripts/uninstall.sh --plugin

# Drop the agent_memory schema from the DB
plugins/memory/postgres/scripts/uninstall.sh --db --yes

# Both, plus clean up .env entries
plugins/memory/postgres/scripts/uninstall.sh --all --yes

# Nuclear: also drop the role and database
plugins/memory/postgres/scripts/uninstall.sh --all --role --database --yes
```

Each destructive step asks for confirmation by default. `--yes` skips
the prompts. The script does **not** remove the `vector` extension
by default — that's a server-wide change, do it by hand only after
every plugin table is gone.

## For the agent: pre-validation workflow

When the user says "install the postgres memory plugin", follow this
sequence **before touching any files**:

```bash
# 1. Pre-flight: what's already there?
git clone https://github.com/skb50bd/hermes-postgres-memory.git /tmp/hpm
/tmp/hpm/plugins/memory/postgres/scripts/diagnose.sh

# 2. Read the output. For every FAIL, ask the user the
#    matching question from references/onboarding-checklist.md.
#    Do NOT proceed until diagnose shows zero failures.
```

If the user already has a hermes-agent install and just wants the
plugin added, and the diagnose is green, the full sequence is:

```bash
# 3. Run the bootstrap (interactive — asks for superuser pw)
...p.sh

# 4. User adds KIMI_API_KEY to ~/.hermes/.env

# 5. User restarts the gateway
hermes gateway restart

# 6. Verify
hermes postgres-memory preflight
hermes postgres-memory status

# 7. Smoke test in a fresh session
pg_remember(content="postgres plugin is live", category="fact")
pg_search(query="postgres plugin")
```

If the user has an existing 1.0/1.1 install (single `content_vector`
column), use the sidecar migration flow above instead of the fresh-
install bootstrap.

If the user has a different memory plugin installed (honcho, mem0,
retaindb, etc.), the bootstrap will detect the `memory.provider` is
not `postgres` and will offer to switch it. The other plugin stays
installed but is unused.

## For the agent: what to tell the user upfront

Before you run any commands, give the user a one-screen summary so
they know what's about to happen. Suggested text:

> I'm going to install the postgres memory provider. The setup will:
> 1. Create a database (`hermes`) and a role (`hermes`) on your
>    Postgres server — I need the postgres superuser password for
>    this.
> 2. Install the `pgvector` extension in that database.
> 3. Install the plugin and create the `agent_memory` schema.
> 4. Add the `POSTGRES_*` env vars to `~/.hermes/.env`.
> 5. Set `memory.provider: postgres` in `~/.hermes/config.yaml`.
>
> I'll need you to add a `KIMI_API_KEY` (free, get one at
> https://platform.moonshot.cn) and restart the gateway after I'm
> done. Total time: ~2 minutes if Postgres is already up.

If the user balks at the superuser requirement, point out that it's
one-time — the plugin never needs DDL after the initial install, and
you can `ALTER TABLE ... OWNER TO postgres;` afterward to downscope
the role back to DML-only.

## Reference: full onboarding docs

The repo ships with extensive onboarding documentation the agent can
load when needed:

- `references/onboarding-checklist.md` — the canonical pre-flight
  checklist, with one-liner probes for every prerequisite
- `references/database-bootstrap.md` — what the database needs, why
  each requirement exists, the password-piping caveat for `psql`,
  the GUCs the SQL script accepts
- `references/migration-privileges.md` — the ownership-transfer
  rationale, the SQL pattern, the security tradeoffs
- `references/pgvector-connectivity-probe.md` — the gold-standard
  direct psycopg2 probe for connection debugging
- `bootstrap-message.txt` — the full copy-pasteable instruction
  manual to send to another agent instance
- `bootstrap-message-short.txt` — the 5-command TL;DR for users who
  just want it done
- `plugins/memory/postgres/scripts/diagnose.sh` — the preflight
  script (re-runnable, JSON output for automation)
- `plugins/memory/postgres/scripts/bootstrap.sh` — the one-shot
  installer
- `plugins/memory/postgres/scripts/uninstall.sh` — the inverse
- `plugins/memory/postgres/sql/000_create_database_and_role.sql` —
  the only file that requires superuser privileges
- `plugins/memory/postgres/sql/000_schema.sql` — the plugin's own
  schema (idempotent)

---

# Operations: diagnostics + troubleshooting

The rest of this skill covers operations and troubleshooting. Load
this half when the plugin is already installed but something is
broken, or when the user is asking operational questions (connection
limits, embedding provider selection, dim migration).

## Diagnostic Flow

1. Load `hermes-agent` first for current Hermes CLI/config conventions.
2. Check provider configuration:
   ```bash
   hermes memory status
   hermes config path
   hermes config env-path
   ```
3. Verify runtime environment separately from CLI status output.
   **Important**: `os.environ` may be stale from startup — if the user
   updated `.env` mid-session, re-read it directly (see step 5 probe).
   ```bash
   for v in POSTGRES_HOST POSTGRES_PORT POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DATABASE; do
     [ -n "${!v}" ] && echo "$v=set" || echo "$v=unset"
   done
   ```
4. For PostgreSQL, test reachability:
   ```bash
   pg_isready -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE"
   ```
5. **The gold-standard probe**: `hermes memory status` can report
   "available ✓" on a dead connection, and it can also miss post-
   restore permission gaps. Always run the direct psycopg2 probe
   that re-reads `.env` directly and checks permissions — see
   `references/pgvector-connectivity-probe.md`.
6. Check Postgres role/database connection usage:
   ```sql
   SELECT usename, state, count(*)
   FROM pg_stat_activity
   WHERE usename = 'hermes'
   GROUP BY usename, state;
   ```
7. Inspect provider plugin lifecycle: persistent connection per
   provider instance, per-process pools, shutdown hooks, retry paths,
   and availability checks.

## PostgreSQL Role Connection Limits

Check current role limit and server capacity:

```sql
SELECT rolname, rolconnlimit
FROM pg_roles
WHERE rolname = 'hermes';

SHOW max_connections;

SELECT usename, state, count(*)
FROM pg_stat_activity
WHERE usename = 'hermes'
GROUP BY usename, state;
```

Free stale idle sessions only when safe:

```sql
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE usename = 'hermes'
  AND state = 'idle'
  AND now() - state_change > interval '10 minutes';
```

Increase the role limit as a database admin/superuser:

```sql
ALTER ROLE hermes CONNECTION LIMIT 20;
```

Use `30` only if the deployment regularly runs many concurrent gateway workers, cron jobs, profiles, and subagents. Avoid unlimited (`-1`) for app roles unless there is external pooling and monitoring.

## Recommended Safe Limits

Default recommendation for a single-user Hermes deployment with gateway + occasional subagents:

- PostgreSQL role `hermes`: connection limit 20
- Memory provider process pool max: 2 connections per Hermes process
- Pool min: 0 or 1
- Connect timeout: 3–5 seconds
- Statement timeout: 10 seconds
- Idle-in-transaction timeout: 30 seconds
- Add `application_name=hermes-memory-postgres` to connections
- Prefer short checked-out connections returned to a pool over a long-lived per-provider connection

Reasoning:

- Gateway process: up to 2
- Main CLI/API session: up to 2
- Three parallel subagents: up to 6
- Cron/background workers: up to 4
- Operational headroom: roughly 6

Total: 20 gives enough room without letting memory infrastructure starve the database.

## Plugin Hardening Pattern

For psycopg2-backed memory providers:

1. Use a module-level `ThreadedConnectionPool` or equivalent, keyed by DSN if multiple profiles/databases may coexist in one process.
2. Default max pool size to 2 per process.
3. Expose conservative env overrides, for example:
   - `HERMES_POSTGRES_POOL_MIN=0`
   - `HERMES_POSTGRES_POOL_MAX=2`
   - `HERMES_POSTGRES_CONNECT_TIMEOUT=5`
   - `HERMES_POSTGRES_STATEMENT_TIMEOUT_MS=10000`
4. Do not hold a checked-out connection for the provider lifetime.
5. For each operation:
   - get connection
   - open cursor
   - execute
   - close cursor
   - return connection in `finally`
6. On broken connection, discard/close it rather than returning poison to the pool.
7. Ensure `is_available()` does not leak connections and does not create a persistent provider connection as a side effect.
8. Add `shutdown()` / close-all behavior for gateway/process exit.
9. Import `psycopg2.pool` explicitly before patching or using `ThreadedConnectionPool`; `import psycopg2` alone may not expose the `pool` submodule.
10. Add tests for repeated status checks, exception paths, pool cap enforcement, shutdown, and “no direct `psycopg2.connect` in normal operation.”

## Embedding Generation (PostgreSQL/pgvector)

The PostgreSQL memory provider computes real embeddings for every memory at
write time, and re-embeds the query at search time. Before 1.1.0 the column
held a 1536-dim zero vector; the schema has since been migrated to **1024
dims** to match Kimi's `bge_m3_embed`.

### Decision rule: free first, fall back to paid only if recall is bad

When picking an embedding provider, the user's preference is to use a
free endpoint first (current winner: `kimi` on `api.kimi.com/coding/v1`,
1024-dim, BGE-M3 quality) and only switch to a paid provider (e.g.
OpenAI's `text-embedding-3-*`) if the free tier's recall quality is
demonstrably inadequate for the workload. Don't propose a paid
embedding service until the free one has been benchmarked and shown
to lose.

See `references/embedding-provider-landscape.md` for the live
provider matrix, exact probe commands, and the Kimi/OpenAI/Ollama
endpoint details.

### Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `HERMES_EMBED_PROVIDER` | `kimi` | One of `kimi`, `ollama_cloud`, `ollama_local`, `noop` |
| `HERMES_EMBED_MODEL` | `bge_m3_embed` | Model passed to the provider |
| `HERMES_EMBED_DIM` | `1024` | Must match `agent_memory.content_vector` dim |
| `HERMES_EMBED_BASE_URL` | `https://api.kimi.com/coding/v1` (kimi) / `https://ollama.com` (cloud) / `http://localhost:11434` (local) | Provider API base |
| `HERMES_EMBED_API_KEY` | unset | Explicit key. If unset, falls back to `KIMI_API_KEY` for kimi, `OLLAMA_API_KEY` for ollama_* |
| `HERMES_EMBED_TIMEOUT` | `10` | HTTP timeout, seconds |
| `HERMES_EMBED_CACHE_DIR` | `~/.cache/hermes/embeddings` | Disk cache root (sharded by first 2 hex chars of content hash) |
| `HERMES_EMBED_CACHE` | `1` | Set to `0` to disable disk cache (in-memory always on per process) |
| `HERMES_EMBED_FAIL_OPEN` | `1` | On provider error, fall back to zero vector and continue. Set to `0` to raise `EmbeddingError` |

The `noop` provider is the fail-safe: it returns a zero vector without
network calls. Useful for tests, smoke checks, and as a last-resort fallback
if the configured provider is misbehaving.

### Why ONE model per table

pgvector's similarity operators (`<=>` cosine, `<->` L2, `<#>` inner product)
only produce *meaningful* scores when both vectors share the same embedding
space. A 1024-dim vector from `bge_m3_embed` and a 1024-dim vector from
`mxbai-embed-large` are both 1024 numbers, but the geometry is different —
the cosine similarity is essentially noise.

**Rule:** every row in `agent_memory.content_vector` must come from the same
model. Switching models requires a full backfill (see "Migrating the dim"
below).

### Embedding providers

| Provider | Free? | Dim | Quality | Notes |
|---|---|---|---|---|
| `kimi` (`bge_m3_embed`) | ✅ | 1024 | Top-tier MTEB, multilingual, BAAI flagship | Recommended default. Moonshot/Kimi serves it at `api.kimi.com/coding/v1`. Same model as Ollama's `bge-m3` for self-host. |
| `kimi` (`bge-large`, `bge-large-en`, `nomic-embed-text`, `text-embedding-3-small`, …) | ✅ | 1024 | varies | Kimi's endpoint accepts 9+ model-name aliases but all return 1024-dim; the alias is a quality/style choice only |
| `ollama_local` (`bge-m3` or `nomic-embed-text`) | ✅ | 1024 / 768 | matches Kimi | Self-host: `ollama pull bge-m3` (or `nomic-embed-text`). Same HTTP contract as `kimi` but different path. |
| `ollama_cloud` | ❌ | n/a | n/a | **Ollama Cloud's public model catalog is currently chat-only.** No embedding models are provisioned, and `/api/embed` returns 401. Don't use this provider. |
| `noop` | ✅ | any | n/a | Test/fallback only |

**Why Kimi is the default**: as of June 2026, Kimi's
`https://api.kimi.com/coding/v1/embeddings` is the only free, working
embedding endpoint among the providers with keys in our `.env`. Ollama
Cloud's free tier does not serve embedding models, Moonshot's
`api.moonshot.cn/v1` rejects the same key, and OpenAI's `text-embedding-3-*`
is paid.

The Kimi endpoint is OpenAI-shape: `POST /v1/embeddings` with
`{"model": ..., "input": ...}` returning `{"data": [{"embedding": [...]}]}`.
The embedder's `_embed_openai_compat` helper handles this and is also
reusable for OpenRouter, Together, vLLM, and any other OpenAI-compatible
embedding service.

### Migrating the dim (1536 → 1024 worked example)

The shipped migration in `plugins/memory/postgres/migrations/` does this.
Order of operations is load-bearing; do not reorder.

```bash
# 1. Drop the old HNSW index, drop the column, recreate at the new dim.
psql ... -f plugins/memory/postgres/migrations/001_embedding_dim.sql

# 2. Backfill the new column with real embeddings.
source .env
python plugins/memory/postgres/scripts/backfill_embeddings.py
# Optional flags: --dry-run, --batch 64, --limit 100

# 3. Rebuild the HNSW index over real vectors. CONCURRENTLY — must run
#    outside a transaction block.
psql ... -f plugins/memory/postgres/migrations/002_recreate_hnsw.sql
```

**Why no HNSW rebuild before backfill?** Building the index over mostly-zero
rows produces a low-quality graph (zero vectors are all equidistant), and
wastes the build work.

**Why `CONCURRENTLY`?** Building a 1024-dim HNSW index over tens of thousands
of rows locks writes on the table for the build duration. CONCURRENTLY lets
the gateway keep writing during the build.

### Privilege prerequisites

The dim migration requires DDL rights on `agent_memory`: `ALTER`, `DROP`,
and the ability to drop/recreate the HNSW index. In many deployments the
`hermes` role only has DML on the table — the table is owned by a
`postgres` superuser and the application role is intentionally downscoped.

**The only way to give `hermes` DDL on a table is to transfer ownership.**
PostgreSQL does NOT support `GRANT ... ALTER, DROP ON TABLE` — those
privileges are ownership-gated, not ACL-gated. The standard table-level
privileges you can GRANT are limited to:
`SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, MAINTAIN`.
This was verified on PG 18.4 via `aclexplode(pg_class.relacl)`.

If you see `ERROR: must be owner of table agent_memory`, run
`migrations/000_grant_ddl_to_hermes.sql` as a superuser (or the current
table owner) FIRST, then re-run `001_embedding_dim.sql`:

```sql
-- Run as a superuser (e.g. psql -U postgres -d hermes -f ...)
ALTER TABLE agent_memory OWNER TO hermes;
```

This is a one-time transfer. After the migration, hermes owns the table
and can perform DDL. The DML grants survive the ownership change. You
can `ALTER TABLE ... OWNER TO postgres;` later to revert if you prefer
a tight role separation; the embedding runtime (pg_remember / pg_search /
backfill script) does not need DDL, only DML + ownership is only needed
for the migration itself.

For production, consider a separate migration role (a role that owns
schema objects and runs migrations, distinct from the application role).

### Self-hosting later

The Kimi model `bge_m3_embed` is the same `bge-m3` served by Ollama.
To migrate to a self-hosted Ollama:

```bash
# On the host
docker run -d --name ollama -p 11434:11434 ollama/ollama
docker exec ollama ollama pull bge-m3
```

Then in your `.env`:

```
HERMES_EMBED_PROVIDER=ollama_local
HERMES_EMBED_BASE_URL=http://ollama-host:11434
# HERMES_EMBED_API_KEY=  # not needed for local
```

No backfill is needed if you stay on the same BGE-M3 model — vectors are
bit-compatible across the two endpoints (same model weights, same dim).

### Disk cache semantics

Embeddings are content-addressable: cache key is `sha256(provider|model|text)`.
A memory with the same content as a previous one (in this or any prior
process) is a cache hit and never hits the network. On disk, entries are
sharded under `<cache_dir>/<first-2-hex>/<full-hash>.json`. To invalidate
the cache (e.g. after switching models), `rm -rf ~/.cache/hermes/embeddings/`.

The cache is best-effort: a failed write is logged at debug and does not
raise. The in-memory cache is per-process; the disk cache is shared.

### Common embedding pitfalls

- **Embedding during a gateway outage**: fail-open means `pg_remember` still
  succeeds; the row is stored with a zero vector. Re-running the backfill
  script is idempotent and will fill in real vectors on the next pass.
- **Stale cache after a model switch**: the cache key includes the model
  name, so different models don't poison each other. But within a model, a
  changed embedding API can produce vectors that no longer match the cached
  ones. If that happens, blow away the cache.
- **Dim mismatch silently corrupts the index**: the embedder has a dim
  contract check that fails open to zero vector when the live result has the
  wrong dim. If you see logs of "Embedding provider returned dim=N, expected
  M" with `fail_open=1`, your config is wrong. Either fix `HERMES_EMBED_DIM`
  or switch `HERMES_EMBED_MODEL`.
- **Kimi rate limits**: the free tier has a per-key RPM limit. If you hit
  it, embed returns `{"vectors": null, "base_resp": {"status_code": 1002,
  "status_msg": "rate limit exceeded(RPM)"}}`. fail-open means memories
  still get stored but with zero vectors. Set `HERMES_EMBED_FAIL_OPEN=0` to
  surface the failure loudly during development.
- **Cache poisoning across users**: the cache is shared across all users
  on the host. If you run multi-tenant, the same `content` from different
  users is a cache hit. This is fine for embeddings (they're content-only)
  but worth knowing if you ever add user-scoped context to the embedder.
- **Pre-1.1.0 zero-vector rows dominate HNSW search results**: cosine
  distance is undefined for the zero vector (all components 0), and pgvector
  treats it as "equidistant from everything." Run the backfill after the dim
  migration or all your old memories will be top-k every time.

### Verifying the embedder is wired up

After deploying, three checks:

1. `pg_status` should report the embedder is reachable. Add a debug print if
   needed — the embedder exposes `get_embedder().stats()` with
   `{hits, misses, errors, zero_fallbacks}`.
2. `pg_remember` a unique sentence, then `pg_search` for it. The new memory
   should appear with a non-zero `vector_sim` field.
3. `psql ... -c "SELECT count(*) FROM agent_memory WHERE content_vector = array_fill(0, ARRAY[1024])::vector"`
   should be `0` after the backfill. Anything non-zero means real embeddings
   are not being computed or stored.

## Common Pitfalls

- **`hermes memory status` false positive/negative**: The `is_available()` check queries only catalog tables (`pg_extension`, `information_schema.tables`) — it does NOT verify that the connecting role can actually SELECT from `agent_memory`. After a `pg_restore` where the table owner and the Hermes role differ, the table exists but grants may be missing. `is_available()` returns `True` while every CRUD operation fails with `InsufficientPrivilege`. Conversely, a transient TCP failure during pool creation makes it return `False` even though the database is fine. Always verify with the direct probe in `references/pgvector-connectivity-probe.md`, which now includes a permissions check.
- **Stale session environment variables**: `os.environ` reflects the process's startup state. If the user updated `~/.hermes/.env` mid-session (e.g., changed `POSTGRES_HOST` after an outage), the session env vars are still the old values. For diagnostics, re-read `.env` directly rather than relying on `os.environ` or `hermes memory status` output.
- **Multiple venvs in checkout**: The Hermes checkout may have both `.venv/` and `venv/`. `psycopg2` is typically only in one. For diagnostics, locate the right one with `find ~/.hermes -path "*/site-packages/psycopg2/__init__.py"` and activate that venv.
- `hermes memory status` can say environment variables are missing when the active failure is actually a connection error. Verify with a direct connection probe before trusting display text. Computers: famously confident, occasionally decorative.
- A single persistent connection per provider instance sounds harmless until gateway sessions, profiles, cron jobs, and subagents multiply it.
- Availability checks that open their own connections can worsen an outage if run repeatedly under a tight role limit.
- Raising the database role limit masks symptoms; plugin pooling prevents recurrence.
- Do not store session-specific database hostnames, credentials, or transient outage facts as long-term memory. Store the diagnostic method in this skill instead.
- **DO NOT claim "embeddings are stored" from schema alone.** A `vector(N)` column plus an HNSW index plus a docstring saying "hybrid search" does not mean embeddings are actually computed. Grep the column for non-zero vectors before making the claim: `SELECT count(*) FROM agent_memory WHERE content_vector <> array_fill(0, ARRAY[<dim>])::vector`. If that's 0 and you said embeddings are stored, you lied. The shipped `pg_status` exposes `zero_vector_memories` and `embedder.stats` precisely so the next agent can stop lying. See "Trust but verify" below.
- **DO NOT patch `embed()` in tests when you mean to patch `_embed_live()`.** `embed()` wraps `_embed_live()` in cache lookup, fail-open, dim check, and cache write. Patching the wrong level either bypasses the production code (tests don't exercise fail-open or dim-mismatch paths) or breaks fail-open entirely. Patch at the boundary that matches the test's intent. Dim-mismatch failures must be raised by `_embed_live` (or by the post-`_embed_live` dim check in `embed`) — never assert on the wrong level and call the test green.
- **Every row in `agent_memory.content_vector` must come from the same embedding model.** pgvector's `<=>` cosine, `<->` L2, and `<#>` inner product operators only produce *meaningful* similarity when both vectors share the same embedding space. A 768-dim vector from `nomic-embed-text` and a 768-dim vector from `bge-small-en` are both 768 numbers, but the geometry is different — cosine similarity is noise. Store the model name somewhere queryable (e.g. a column or a separate audit table) so future you can detect a mix. Mixing models silently breaks hybrid search; you won't see an error, you'll just see irrelevant results.
- **Fail-open zero-vectors poison the disk cache.** If `HERMES_EMBED_FAIL_OPEN=1` (the default) and the provider returns an error, the embedder substitutes a zero vector and returns successfully. **The cache must refuse to store that zero vector** — otherwise a transient 401 / 429 / DNS hiccup writes a cache entry, and every subsequent `embed(same_text)` short-circuits to that zero entry, including after the provider recovers. The fix lives in the embedder: a `used_fallback` flag set inside the exception path gates the cache write. The `noop` provider deliberately returns zeros and *should* cache (it's deterministic and intentional); the guard only blocks vectors produced by the fail-open safety net. When writing tests, assert that the second call after a fail-open hits the network, not the cache.
- **Subprocesses don't auto-source `~/.hermes/.env`.** A bash shell that ran `set -a; source ~/.hermes/.env; set +a` will have `KIMI_API_KEY` in its env, but a `python script.py` launched from that shell will see an empty `os.environ` for the embedding key. The `python` interpreter does not re-read `.env` on startup. The backfill script (and any other embedder-calling script) must either (a) be launched with `set -a; source ~/.hermes/.env; set +a && python script.py` in bash, or (b) re-read the `.env` itself in Python before constructing the embedder. Symptom: `embedder.stats()["zero_fallbacks"]` increments while the table fills with zero vectors. Fix: also wire `scripts/backfill_embeddings.py` to read `~/.hermes/.env` itself if the relevant env var is missing, not assume the caller did it.
- **Hybrid search's FTS pre-filter kills pure-semantic queries.** The shipped `search_memories()` runs FTS first to pull a candidate window, then reranks with cosine similarity. If the query has no token overlap with any memory, the FTS filter produces zero candidates and the result is empty — even if a memory is semantically a perfect match. This is by design (the FTS window is the index that makes hybrid search fast at scale), but it means `"How does the SportsVerse storefront handle mobile navigation?"` returns empty if no memory contains the literal words "mobile" or "navigation". Mitigations for a future task: drop the FTS pre-filter and rely on HNSW alone for small tables; add a query rewriter that synthesizes keyword variants from a semantic query; widen the FTS window with a fallback. Don't try to "fix" this in the FTS query — it's a design choice, not a bug.
- **Tests against the embedder leak disk cache state across runs unless `HERMES_EMBED_CACHE_DIR` is set to a per-test tmpdir.** The default cache root is `~/.cache/hermes/embeddings`; the test fixture must override it (and set `HERMES_EMBED_CACHE=0` for tests that should never write to disk). A test that calls `e.embed("anything")` then asserts `stats["misses"] == 1` will flake if a prior run on the same machine happened to embed "anything" — the in-memory cache miss check is correct, but a disk-cache hit from yesterday still leaves the in-memory `self._cache` populated, depending on fixture isolation. Always monkey-patch `HERMES_EMBED_CACHE_DIR` to `tmp_path` and `HERMES_EMBED_CACHE=0` in embedder test fixtures.
- **When the password is in an env var, shell `***` substitution breaks command quoting.** Commands like `psql "postgresql://$POSTGRES_USER:***@$HOST/..."` will choke on the `***` token because it's neither a valid shell substitution nor a literal. Use a Python wrapper that reads `os.environ` directly (no shell interpolation of the secret) when you need to compose a command that includes credentials. Same applies to heredocs, command-substitution, and `eval`.
- **Do not "fix" missing `.env` env vars by hardcoding a different endpoint in the code.** When a script fails because `KIMI_API_KEY` is unset, the temptation is to fall back to a different provider that doesn't need a key. That silently changes the embedding model and dimensions, which silently breaks similarity search. The right fix is to surface the missing env var loudly and stop, not to degrade to a different model. The `noop` provider is the one legitimate fallback (it's the documented test/fail-safe).
- **FTS candidate CTEs must include every column the outer SELECT references.** A common hybrid-search shape is `WITH fts_candidates AS (SELECT id, content, ts_rank(...) FROM agent_memory WHERE ... LIMIT N) SELECT id, content, text_rank, 1 - (content_vector <=> $q::vector) AS vector_sim FROM fts_candidates ORDER BY ...`. If the CTE only selects `id, content, text_rank` and the outer SELECT also references `content_vector` for the cosine call, the query fails at runtime with `column content_vector does not exist` — confusing because the column clearly exists in the base table. Symptom: search returns `column "content_vector" does not exist` despite a working schema. Fix: add `m.content_vector` to the inner CTE's select list, or move the cosine calculation into a JOIN against the base table. Add a test that exercises a known-good query and asserts the result has a non-null `vector_sim` — a smoke test catches this in 1 second.
- **The embedder's `dim` is the single source of truth for vector size.** If the plugin's `__init__.py` also has a hardcoded `_EMBED_DIM = 1024` constant for "compatibility," that's duplicated state. A user with a 768-dim schema would have to edit source instead of just changing `HERMES_EMBED_DIM`. Use `get_embedder().dim` everywhere — and if a SQL query needs the literal dim (e.g. `array_fill(0, ARRAY[<dim>])::vector`), either pass it as a parameter or `SELECT vector_dims(content_vector) FROM agent_memory LIMIT 1` at query time. Don't hardcode.

## Trust but verify

When a user asks "are embeddings working" or "is hybrid search wired up", do not answer from the schema, the docstring, or the test pass count. Run the verification script `scripts/verify_embeddings.py`. It:

1. Counts non-zero vectors in the `content_vector` column.
2. Counts rows vs. rows-with-real-embeddings. A gap means the embedder is failing open to zero vectors.
3. Runs a real `pg_search`-equivalent hybrid query and asserts the result set has both `text_rank > 0` and `vector_sim > 0` columns populated.
4. Reports embedder stats (`hits`, `misses`, `errors`, `zero_fallbacks`) so a fail-open configuration is visible.

If any of those fail, the answer to the user is "no, embeddings are not working — here's the evidence," not "the schema has a vector column so yes."

## Verification After Fixes

- `hermes memory status` shows provider available.
- Direct psycopg2 probe can connect and check `pg_extension.vector` + `agent_memory`.
- `pg_stat_activity` for `usename='hermes'` stays below the proposed role limit during normal use.
- Repeated provider status checks do not increase active/idle connection count monotonically.
- Gateway restart does not leave old idle Hermes memory connections behind.

## Post-Restore / Migration Verification

When the Hermes database has been restored to a new host (or reloaded from a dump):

1. **Check table ownership**: `pg_restore` preserves the original owner. If the dump was taken as `postgres`, the restored `agent_memory` is owned by `postgres`, not `hermes`. Fix with:
   ```sql
   ALTER TABLE agent_memory OWNER TO hermes;
   ```
   Or keep ownership and grant:
   ```sql
   GRANT SELECT, INSERT, UPDATE, DELETE ON agent_memory TO hermes;
   ```

2. **Check role permissions**: The `is_available()` method in the PostgreSQL memory provider only queries catalog tables — it does NOT verify that the Hermes role can SELECT from `agent_memory`. After a restore, the table exists but grants may be missing, producing a misleading "available ✓" while every CRUD operation fails with `InsufficientPrivilege`.

3. **Run the full diagnostic probe** from `references/pgvector-connectivity-probe.md` — it now includes a three-phase check: catalog tables → role grants → actual data access. A green Phase 1 with a red Phase 2 is the signature of a post-restore permission gap.

4. **Verify sequences**: If `agent_memory` uses a `SERIAL` or `IDENTITY` column, the sequence may need resetting:
   ```sql
   SELECT setval('agent_memory_id_seq', (SELECT max(id) FROM agent_memory));
   ```

## Packaging a memory plugin for sharing

When you've built a memory plugin (or a new version of one) and want to
share it — upstream PR, PyPI package, or standalone repo — a structured
review catches the issues that block adoption. Pattern captured from
the June 2026 postgres plugin packaging review.

### Packaging destinations, in order of reach

| Destination | Reach | Effort | Discovery | Trade-off |
|---|---|---|---|---|
| **Upstream PR into `NousResearch/hermes-agent`** | Highest | 3–5 days (incl. review) | Built-in via `hermes plugins list` | Subject to maintainer review; bakes in schema decisions |
| Standalone PyPI package (e.g. `hermes-postgres-memory`) | Medium | ~2 days | Manual install | Version-skew with hermes-agent's `MemoryProvider` ABC; no built-in discovery |
| Standalone GitHub repo + setup script | Lowest | ~1 day | Manual git clone | Hardest to discover; no version management |

**Default to upstream.** The hermes-agent catalog has memory plugins
that are thin clients to hosted services (honcho, mem0, supermemory,
openviking, byterover, retaindb) plus one local research prototype
(holographic). **A database-backed memory plugin fills a real gap
that no other plugin covers.** Ask the user before assuming, but the
default answer is upstream PR.

### The 11-issue code review checklist

Before opening a PR or publishing, walk this list. Every item is a
real issue that has blocked past reviews.

1. **`plugin.yaml` must use `pip_dependencies:`, not `dependencies:`.**
   Upstream's loader looks for the former; the latter is silently
   ignored. Verify against `plugins/memory/honcho/plugin.yaml` or any
   other shipped plugin. Also declare `requires_env:` for every env
   var the plugin reads, and `hooks:` for every hook it implements.
2. **`psycopg2-binary` is a base Hermes dependency** — don't
   re-declare it in `pip_dependencies`. Only declare the deps that
   are *additions* to the base set (e.g. `httpx` for the embedder).
3. **Hardcoded constants that duplicate state from another module.**
   Example: `_EMBED_DIM = 1024` in the plugin's `__init__.py` while
   the embedder module exposes `dim` as a property. The constant
   forces users on different dims to edit source. Remove the
   duplicate; use the embedder's `dim` (or query the schema) as the
   single source of truth.
4. **Stale README.** If the README still references an old schema
   version, an old env var, or a missing migration, downstream users
   will follow it into a broken state. The README must mirror what
   the code does *today*, not what it did at the initial commit.
5. **Missing `CHANGELOG.md`.** Even a one-line "1.1.0 — replaced
   zero-vector placeholder with real embeddings, breaking: 1536→1024
   schema requires migration" is enough to warn users. Without it,
   they have no signal that upgrading is destructive.
6. **Author/license/required_env completeness.** Match the
   convention of sibling plugins in the same directory. For
   hermes-agent, `author: "Hermes Agent"`, `license: MIT`, and a
   `pip_dependencies:` list of *only* the additions.
7. **Per-method `self._lock` that defeats the connection pool.**
   The pool exists to enable concurrency; serializing on `self._lock`
   makes the entire client single-threaded. Either drop the lock
   (the pool is already thread-safe) or document why it's there.
8. **No integration test against a real database.** All-mocked unit
   tests pass even when the SQL is broken. Add at least one test
   that hits a dockerized Postgres + pgvector, exercises add → search
   → backfill, and asserts non-zero `vector_sim` on a known-good
   query. Upstream reviewers will ask for this.
9. **Fragile parameter lists in raw SQL.** `params = [query] + params + [q, ..., top_k]` — fine today, silent breakage tomorrow if a WHERE clause is added. Add a test that asserts the `%s` placeholder count in the SQL matches the params list length. (`%s` count = `len(params)` in the executed statement; the Python driver will catch mismatches at execute time, but a test asserts the *intent*.)
10. **`prefetch()` does live network work on every turn.** Embedding
    the query adds ~500ms latency on a cold cache. Other providers
    gate prefetch on minimum query length and short-circuit on
    cache hits. Document the cost in the PR; consider gating.
11. **Migration file names must match their content.** A file named
    `000_grant_ddl_to_hermes.sql` that actually contains
    `ALTER TABLE ... OWNER TO` will confuse every reader. Rename to
    match what's in the file (`000_ownership_transfer.sql`).

### Three decisions to lock before starting the PR

Ask the user explicitly. Don't assume.

1. **Packaging destination** (A: upstream / B: PyPI / C: standalone repo).
2. **Author name in `plugin.yaml`** (the personal brand, the company, or upstream's "Hermes Agent").
3. **Migration policy for users on the old schema** (destructive: DROP+ADD column; non-destructive: sidecar column with union-at-search; or a hard upgrade gate).

### Skill and reference file placement

If the plugin ships with a skill, the skill must move with it:

- The skill is currently at `~/.hermes/skills/<cat>/<name>/SKILL.md`
  (user-local). For sharing, move it to
  `~/.hermes/hermes-agent/skills/<cat>/<name>/SKILL.md` so it ships
  in the same PR.
- Reference doc paths inside the skill (e.g.
  `scripts/verify_embeddings.py`) become stable once the skill is
  in the repo, since they resolve relative to the skill dir.
- After the move, search the references for any
  `~/.hermes/hermes-agent/...` path and confirm it still resolves
  in the new layout (it will, because the skill is now in that
  tree).

## References

- `references/pgvector-connectivity-probe.md` — Direct psycopg2 probe that bypasses Hermes provider layer; re-reads `.env` directly to avoid stale session env vars. Use when `hermes memory status` lies.
- `references/postgres-memory-connection-limits.md` — Session-specific details for diagnosing Postgres role connection exhaustion and hardening the Hermes PostgreSQL memory provider.
- `references/embedding-provider-landscape.md` — Live probe results and exact HTTP contracts for free/paid embedding providers (Kimi, Ollama, OpenAI). Decision tree, probe recipes, and what does NOT work (Ollama Cloud's chat-only catalog, Moonshot China auth, etc.).
- `references/embedding-rollout-playbook.md` — End-to-end workflow for adding embeddings to a new table: probe providers → pick a model → migrate the dim → backfill → verify. Captures the rollout gotchas that have cost real time (env loading, cache poisoning, ownership transfer, FTS pre-filter limitation).
- `references/migration-privileges.md` — How to handle the `hermes` role's DML-only posture when migrations need DDL. Diagnose → one-time GRANT as privileged role → run migration → optional REVOKE.
- `scripts/run_embedding_migration.sh` — End-to-end runbook for the dim migration (preflight → migration 001 → backfill → migration 002 → verify). Handles `--dry-run`, `--skip-migration`, `--skip-backfill`. Idempotent.
- `references/embedding-providers.md` — Ollama `/api/embed` vs `/api/embeddings` response shapes, auth headers, base URLs for cloud vs local, free-tier rate limits, and the canonical nomic-embed-text vs mxbai-embed-large vs bge-large-en-vs text-embedding-3-small trade-off. The exact HTTP contract the embedder is built against.
- `scripts/verify_embeddings.py` — The "are embeddings actually working" probe. Counts zero-vec rows, runs a hybrid query, reports embedder stats. Run this before answering any "is the vector store live?" question.
- `references/memory-plugin-packaging.md` — Distilled playbook for "I built a memory plugin, how do I share it?" Covers the 11-issue code-review checklist, packaging destinations (upstream PR / PyPI / standalone repo), the three decisions to lock before starting, and a 5-phase plan. The full session plan with postgres specifics is in `~/.hermes/plans/2026-06-02-postgres-memory-packaging/plan.md`.
