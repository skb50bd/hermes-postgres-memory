---
name: hermes-postgres-memory
description: "Recipe for installing, enabling, verifying, and troubleshooting the Hermes PostgreSQL/pgvector memory provider."
version: 1.7.0
author: Shakib Haris
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes-agent, memory, postgres, pgvector, embeddings, recipe, onboarding, preflight, troubleshooting]
    related_skills: [hermes-agent, hermes-gateway-troubleshooting, systematic-debugging]
---

# Hermes Postgres Memory Provider Recipe

Use this skill whenever the user asks to install, configure, enable, verify,
diagnose, tune, or uninstall the PostgreSQL memory plugin for Hermes Agent.

This is the canonical recipe. Do not make the user copy instructions from chat
into another place. Follow the steps from here, use the repo scripts, and verify
with real commands before claiming success. Apparently “it should work” is not a
test plan. Shocking.

## Non-negotiable contract

- Runtime DB config is exactly `PG_MEM_DB_CONN_STR`.
- The old five-variable `POSTGRES_*` connection form is not supported.
- `PG_MEM_DB_CONN_STR` may be either:
  - URI DSN: `postgresql://hermes:***@host:5432/hermes`
  - Semicolon DSN: `Host=host;Port=5432;Database=hermes;Username=hermes;Password=***`
- The Hermes agent/runtime should **not** have PostgreSQL superuser access.
- Privileged PostgreSQL work is a DBA/user prerequisite, not agent work:
  - create role/database
  - install `pgvector`
  - transfer `public` schema ownership to the runtime role
  - grant schema object creation rights
- Agent-side automation verifies prerequisites and fails loud. It does not ask
  for superuser credentials and does not improvise with elevated access.
- Schema is greenfield-only:
  - `agent_memory.vector_768`
  - `agent_memory.vector_1024`
  - `agent_memory.vector_1536`
- Default embedding dimension: `1024`.
- Default 1024 provider/model: `kimi` / `bge_m3_embed`.

## Required project location

The plugin repo normally lives at:

```bash
~/repos/hermes-postgres-memory
```

If missing, clone it:

```bash
mkdir -p ~/repos
cd ~/repos
git clone https://github.com/skb50bd/hermes-postgres-memory.git
cd ~/repos/hermes-postgres-memory
```

If the repo already exists, update it before install work:

```bash
cd ~/repos/hermes-postgres-memory
git fetch origin
git status --short --branch
```

Do not blindly reset local changes. If dirty, inspect and ask before destructive
git operations.

## Recipe A — fresh install with DBA prerequisites already done

Use this when the user/admin has already created the database role, installed
pgvector, and provided `PG_MEM_DB_CONN_STR`.

### 1. Confirm local prerequisites

```bash
cd ~/repos/hermes-postgres-memory
command -v psql
command -v pg_isready
python3 --version
```

If `psql` / `pg_isready` is missing, install PostgreSQL client tools first.

### 2. Run agent-side bootstrap

Use the runtime DSN from the user/admin:

```bash
cd ~/repos/hermes-postgres-memory
PG_MEM_DB_CONN_STR='postgresql://hermes:***@host:5432/hermes' \
  ./plugins/memory/postgres/scripts/bootstrap.sh
```

What bootstrap does:

1. Checks local tooling and Hermes checkout.
2. Reads `PG_MEM_DB_CONN_STR`.
3. Refuses a superuser runtime role.
4. Verifies pgvector is installed.
5. Verifies `public` schema is owned by the runtime role.
6. Verifies the runtime role can create/drop plugin objects.
7. Writes `PG_MEM_DB_CONN_STR` to `~/.hermes/.env` if missing.
8. Installs plugin files and this skill into Hermes.
9. Applies `000_schema.sql` using the runtime role.
10. Runs final preflight.

If bootstrap fails on DB privileges, stop. Hand the user/admin the admin-side
SQL file path and the failing check. Do not ask for or use superuser creds.

Admin-side SQL file:

```text
~/repos/hermes-postgres-memory/plugins/memory/postgres/sql/000_create_database_and_role.sql
```

### 3. Add embedder credentials

Default 1024-dim embedding uses Kimi/Moonshot:

```bash
hermes config env-path
# edit the shown .env and add:
KIMI_API_KEY=sk-...
```

Other supported dimensions/providers exist, but do not switch providers just
because a key is missing. Ask or fail clearly.

### 4. Enable provider in Hermes config

Use Hermes CLI when possible:

```bash
hermes config set memory.memory_enabled true
hermes config set memory.provider postgres
```

If CLI config setting fails, edit `~/.hermes/config.yaml` manually:

```yaml
memory:
  memory_enabled: true
  provider: postgres
```

Restart after config or `.env` changes:

```bash
hermes gateway restart
```

For CLI-only use, start a fresh `hermes` process instead.

### 5. Verify before claiming success

Run all of these:

```bash
cd ~/repos/hermes-postgres-memory
./plugins/memory/postgres/scripts/diagnose.sh
hermes postgres-memory preflight
hermes postgres-memory status
hermes postgres-memory model-list
```

Expected high-level result:

- `PG_MEM_DB_CONN_STR` set
- runtime role can connect
- runtime role is non-superuser
- pgvector installed
- public schema owner is runtime role
- all tables exist
- `vector_768`, `vector_1024`, `vector_1536` exist
- three model rows registered
- HNSW indexes exist

Then run a real Hermes tool smoke test in a fresh session:

```text
pg_remember(content="postgres plugin is live", category="fact")
pg_search(query="postgres plugin")
pg_status()
```

Only say it works after the smoke test returns real results.

## Recipe B — admin prerequisites are not done yet

Use this when the user says they have pgAdmin/DBA access, or when bootstrap says
pgvector/schema ownership/object creation is missing.

Give the user/admin this file, not a hand-wavy paragraph:

```text
~/repos/hermes-postgres-memory/plugins/memory/postgres/sql/000_create_database_and_role.sql
```

Tell them to run it as PostgreSQL admin/superuser, with variables if needed:

```bash
psql -h <host> -U postgres -d postgres \
  -v dbname='hermes' \
  -v rolename='hermes' \
  -v pw='choose_a_strong_password' \
  -v connlimit='20' \
  -f plugins/memory/postgres/sql/000_create_database_and_role.sql
```

After they say it is done, continue with Recipe A using the final runtime DSN.

The admin-side script creates/verifies:

- non-superuser runtime role
- target database
- `CREATE EXTENSION IF NOT EXISTS vector`
- `ALTER SCHEMA public OWNER TO <runtime role>`
- `GRANT ALL ON SCHEMA public TO <runtime role>`
- connection limit

## Recipe C — plugin files installed but memory not active

Use this when files exist but `pg_remember`, `pg_search`, or `pg_status` are
missing/failing.

### 1. Verify install paths

```bash
test -d ~/.hermes/hermes-agent/plugins/memory/postgres && echo plugin-present
test -d ~/.hermes/hermes-agent/skills/devops/hermes-postgres-memory && echo skill-present
```

If missing:

```bash
cd ~/repos/hermes-postgres-memory
./install.sh --yes
```

### 2. Verify env/config

```bash
hermes config env-path
hermes config path
grep '^PG_MEM_DB_CONN_STR=' ~/.hermes/.env
hermes config | grep -A5 '^memory:'
```

Config must include:

```yaml
memory:
  memory_enabled: true
  provider: postgres
```

### 3. Restart

```bash
hermes gateway restart
```

Start a new CLI/gateway session. Tool availability is snapshotted per session;
old sessions may not see the new plugin. Yes, cache invalidation is still the
villain.

### 4. Re-run verification

```bash
hermes postgres-memory preflight
hermes postgres-memory status
hermes postgres-memory model-list
```

Then smoke test with `pg_remember` and `pg_search`.

## Direct database verification

Use these only after `PG_MEM_DB_CONN_STR` is available:

```bash
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT current_user;"
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT rolsuper FROM pg_roles WHERE rolname = current_user;"
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT extversion FROM pg_extension WHERE extname='vector';"
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT pg_catalog.pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='public';"
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT to_regclass('public.agent_memory');"
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT count(*) FROM agent_memory_models WHERE dim IN (768,1024,1536);"
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT count(*) FROM agent_memory WHERE vector_1024 IS NOT NULL;"
```

Interpretation:

- `rolsuper` must be `f`.
- extension query must return a pgvector version.
- public schema owner must equal runtime role.
- `agent_memory` must resolve.
- model count must be `3`.

## Model and dimension operations

Show current model registry:

```bash
hermes postgres-memory model-list
```

Set default dimension:

```bash
hermes postgres-memory model-set --dim 1024
```

Set explicit provider/model:

```bash
hermes postgres-memory model-set --dim 1024 --provider kimi --model bge_m3_embed
```

Backfill missing embeddings:

```bash
cd ~/repos/hermes-postgres-memory
python plugins/memory/postgres/scripts/backfill_embeddings.py --dim 1024
```

Without `--dim`, backfill attempts all supported dimensions:

```bash
python plugins/memory/postgres/scripts/backfill_embeddings.py
```

## Troubleshooting decision tree

### `PG_MEM_DB_CONN_STR` missing

Fix `.env`:

```bash
hermes config env-path
```

Add one line:

```bash
PG_MEM_DB_CONN_STR='postgresql://hermes:***@host:5432/hermes'
```

Restart Hermes.

### Cannot connect

Run:

```bash
pg_isready -d "$PG_MEM_DB_CONN_STR"
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT 1;"
```

If it fails, this is host/port/firewall/credential/DB availability. Do not edit
plugin code yet. The database is unreachable. Heroic debugging of Python at this
stage is just performance art.

### Runtime role is superuser

Refuse it. Ask for a dedicated non-superuser app role DSN.

### pgvector missing

Admin prerequisite missing. Give the admin SQL file path:

```text
plugins/memory/postgres/sql/000_create_database_and_role.sql
```

### Public schema owner mismatch

Admin prerequisite missing. Runtime role must own `public` so it can create and
maintain plugin tables/indexes.

Admin fix pattern:

```sql
ALTER SCHEMA public OWNER TO hermes;
GRANT ALL ON SCHEMA public TO hermes;
```

### Tables/indexes missing

If privilege checks pass, apply schema:

```bash
cd ~/repos/hermes-postgres-memory
psql "$PG_MEM_DB_CONN_STR" -v ON_ERROR_STOP=1 -f plugins/memory/postgres/sql/000_schema.sql
```

If this fails with permission errors, go back to admin prerequisites.

### Search returns nothing

Check whether vectors exist:

```sql
SELECT count(*) FROM agent_memory WHERE vector_1024 IS NOT NULL;
```

If zero, fix embedder key/network and backfill.

Also remember: current hybrid search uses full-text search as a candidate
prefilter, then vector rerank. No token overlap can mean no candidates.

### Embeddings write zero vectors

If `HERMES_EMBED_FAIL_OPEN=1`, provider failures can write zero vectors. Fix the
provider key/network, then backfill:

```bash
python plugins/memory/postgres/scripts/backfill_embeddings.py --dim 1024
```

## Uninstall recipe

Plugin files only:

```bash
cd ~/repos/hermes-postgres-memory
plugins/memory/postgres/scripts/uninstall.sh --plugin
```

Plugin DB tables only:

```bash
plugins/memory/postgres/scripts/uninstall.sh --db --yes
```

Everything agent-side:

```bash
plugins/memory/postgres/scripts/uninstall.sh --all --yes
```

Dropping the PostgreSQL role/database/extension is admin-owned. Do not assume the
agent can or should do it.

## Agent behavior rules

- Load this skill before working on Postgres memory.
- If the task touches Hermes CLI/config/gateway behavior, also load
  `hermes-agent`.
- Prefer the repo scripts over hand-rolled shell snippets.
- Never use or request PostgreSQL superuser credentials for the agent.
- Never claim success from file installation alone. Verify DB + CLI + actual
  memory tools.
- If `.env` is edited, restart gateway or start a fresh Hermes CLI session.
- If a command fails, report the exact failing check and the next concrete fix.
- If you discover a recurring pitfall not covered here, patch this skill before
  finishing.
