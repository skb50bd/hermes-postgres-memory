---
name: hermes-postgres-memory
description: "Install, configure, troubleshoot, and harden the greenfield PostgreSQL/pgvector memory provider for Hermes Agent."
version: 1.6.1
author: Shakib Haris
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes-agent, memory, postgres, pgvector, embeddings, onboarding, preflight, multi-dim, troubleshooting, connection-pooling]
    related_skills: [hermes-agent, hermes-gateway-troubleshooting, systematic-debugging]
---

# Hermes Postgres Memory Provider

Greenfield PostgreSQL + pgvector memory backend for Hermes Agent.

The project intentionally supports only the current schema and current runtime
configuration. Git is the version history. Migration docs, compatibility shims,
and old connection forms are not part of the user-facing workflow.

## Current contract

- Required runtime DB config: `PG_MEM_DB_CONN_STR` (URI DSN or semicolon connection string)
- Schema: `agent_memory` with `vector_768`, `vector_1024`, and `vector_1536`
- Default dim: `1024`
- Default 1024 provider/model: `kimi` / `bge_m3_embed`

## When to use

Load this skill when the user asks to:

- Install or configure the Postgres memory plugin
- Diagnose `pg_remember`, `pg_search`, or `pg_status`
- Verify that embeddings are stored and searchable
- Tune connection limits or embedder provider settings
- Uninstall the plugin

If the user asks about Hermes Agent CLI/config/gateway itself, also load
`hermes-agent` before answering.

## Greenfield install

The agent/runtime should not have PostgreSQL superuser access. Treat role/database
creation, `CREATE EXTENSION vector`, and schema ownership grants as DBA
prerequisites. The agent verifies them before installing or using the plugin.

```bash
git clone https://github.com/skb50bd/hermes-postgres-memory.git /tmp/hpm
cd /tmp/hpm
PG_MEM_DB_CONN_STR='postgresql://hermes:***@host:5432/hermes' \
  ./plugins/memory/postgres/scripts/bootstrap.sh
```

The bootstrap script:

1. Checks `psql`, Python, psycopg2, and the Hermes checkout.
2. Reads `PG_MEM_DB_CONN_STR` for the dedicated non-superuser runtime role.
3. Verifies DBA prerequisites: runtime role is not superuser, pgvector exists, public schema is owned by the runtime role, and object creation works.
4. Writes a single `PG_MEM_DB_CONN_STR` entry to `~/.hermes/.env` if missing.
5. Installs the plugin and this skill into the Hermes checkout.
6. Creates the greenfield schema via `sql/000_schema.sql` using the runtime role.
7. Runs `diagnose.sh`.

After bootstrap, the user must add an embedder key and restart Hermes:

```bash
KIMI_API_KEY=sk-...
hermes gateway restart
```

## Existing database install

If `~/.hermes/.env` already has the DSN and DBA prerequisites are complete:

```bash
cd ~/repos/hermes-postgres-memory
./install.sh
psql "$PG_MEM_DB_CONN_STR" -f plugins/memory/postgres/sql/000_schema.sql
hermes postgres-memory preflight
```

Set Hermes config:

```yaml
memory:
  memory_enabled: true
  provider: postgres
```

Restart after changing config or `.env`.

## Required environment

```bash
PG_MEM_DB_CONN_STR='postgresql://hermes:***@host:5432/hermes'
KIMI_API_KEY='***'
```

Optional embedder/pool env vars:

- `HERMES_EMBED_DEFAULT_DIM=1024`
- `HERMES_EMBED_FAIL_OPEN=1`
- `HERMES_POSTGRES_POOL_MIN=0`
- `HERMES_POSTGRES_POOL_MAX=2`
- `HERMES_POSTGRES_CONNECT_TIMEOUT=5`
- `HERMES_POSTGRES_STATEMENT_TIMEOUT_MS=10000`

Do not diagnose runtime connection issues with the old five-variable DB env
form. If `PG_MEM_DB_CONN_STR` is missing, the plugin should fail loudly.

## Verification workflow

Run these before telling the user it works:

```bash
hermes postgres-memory preflight
hermes postgres-memory status
hermes postgres-memory model-list
```

Then smoke test from a fresh Hermes session:

```text
pg_remember(content="postgres plugin is live", category="fact")
pg_search(query="postgres plugin")
```

For direct DB verification:

```sql
SELECT count(*) FROM agent_memory WHERE is_active = true;
SELECT count(*) FROM agent_memory WHERE vector_1024 IS NOT NULL;
```

## CLI commands

```bash
hermes postgres-memory status
hermes postgres-memory model-list
hermes postgres-memory model-set --dim 1024
hermes postgres-memory backfill --dim 1024
hermes postgres-memory preflight
```

## Backfill

Backfill is for populating missing vectors in existing rows. It is idempotent.

```bash
python plugins/memory/postgres/scripts/backfill_embeddings.py --dim 1024
python plugins/memory/postgres/scripts/backfill_embeddings.py
```

Without `--dim`, it attempts all supported dimensions.

## Troubleshooting

### Provider unavailable

1. Confirm `.env` contains `PG_MEM_DB_CONN_STR`.
2. Re-read `.env` directly; do not trust stale process env after an edit.
3. Run:
   ```bash
   pg_isready -d "$PG_MEM_DB_CONN_STR"
   psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT 1;"
   psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT 1 FROM pg_extension WHERE extname='vector';"
   ```
4. Run `hermes postgres-memory preflight`.
5. If pgvector, schema ownership, or object creation fails, stop and hand the user/admin `sql/000_create_database_and_role.sql`; do not request or use PostgreSQL superuser credentials from the agent.

### Search returns nothing

- Confirm the queried dimension column has vectors:
  ```sql
  SELECT count(*) FROM agent_memory WHERE vector_1024 IS NOT NULL;
  ```
- Confirm the query has token overlap. The current hybrid search uses FTS as
  a candidate pre-filter, then reranks with vector similarity. No FTS overlap
  can mean no candidates.
- If filtering by `target` or `category`, check SQL param ordering in
  `search_memories`; where-clause params belong between the first query param
  and the second query param.

### Embeddings fail open

If `HERMES_EMBED_FAIL_OPEN=1`, provider failures can write zero vectors.
Backfill after fixing the provider key/network:

```bash
python plugins/memory/postgres/scripts/backfill_embeddings.py --dim 1024
```

### Connection pressure

Recommended single-user defaults:

- DB role connection limit: 20
- Plugin pool max per process: 2
- Connect timeout: 3–5s
- Statement timeout: 10s

Inspect activity:

```sql
SELECT usename, state, count(*)
FROM pg_stat_activity
WHERE usename = current_user
GROUP BY usename, state;
```

## Uninstall

```bash
plugins/memory/postgres/scripts/uninstall.sh --plugin
plugins/memory/postgres/scripts/uninstall.sh --db --yes
plugins/memory/postgres/scripts/uninstall.sh --all --yes
```

The DB mode drops plugin tables. Dropping the role or database requires the
explicit `--role` / `--database` flags.

## Pitfalls

- Subprocesses do not automatically source `~/.hermes/.env`; scripts that need
  keys should read `.env` themselves or be launched from a sourced shell.
- Do not hardcode alternate providers when a key is missing; fail loudly or use
  the documented `noop` provider for tests.
- Every vector in a given `vector_<dim>` column must come from the same model.
  Mixing models inside a column silently ruins similarity scores.
- Do not claim embeddings work from schema alone. Verify non-null/non-zero
  vectors and run a real `pg_search` smoke test.
- Never assume the agent has PostgreSQL superuser access. Privileged DB work is prerequisite/admin-owned; agent-side automation verifies and fails loud.
