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

First inspect the live CLI shape. Current Hermes builds may register this plugin
as the top-level `hermes postgres` command while not exposing nested
`postgres-memory` subcommands. Do not assume the CLI verifier commands exist.

```bash
hermes --help | grep -E 'postgres|postgres-memory' || true
hermes postgres --help || true
hermes postgres-memory --help || true
```

Run repository/database verification:

```bash
cd ~/repos/hermes-postgres-memory
./plugins/memory/postgres/scripts/diagnose.sh
```

If `diagnose.sh` fails only because it checks the profile path while the plugin
is installed in Hermes' install tree, patch the path check or fall back to the
SQL probes in "Direct database verification" below. Do not block the migration
on the cosmetic path check if direct DB checks and pg tools pass.

If this build exposes working CLI subcommands, also run them:

```bash
hermes postgres-memory preflight
hermes postgres-memory status
hermes postgres-memory model-list
```

If `hermes postgres-memory` is not registered, skip those CLI commands and use
`pg_status`, `pg_remember`, and `pg_search` as the authoritative smoke test.

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
hermes postgres-memory preflight || true
hermes postgres-memory status || true
hermes postgres-memory model-list || true
```

If `postgres-memory` is not a registered command in the installed Hermes build,
use the direct SQL probes plus `pg_status`, `pg_remember`, and `pg_search`
instead. A top-level `hermes postgres` command with no nested subcommands is a
known registration shape in some builds.

Then smoke test with `pg_remember` and `pg_search`.

## Recipe D — Hermes profiles (multi-instance)

When the user runs multiple Hermes instances under `hermes profile …`, each
profile has its own `~/.hermes/profiles/<name>/` tree with its own
`config.yaml`, its own `.env`, and its own skills directory. The root
`~/.hermes/.env` is **not** inherited by profiles. This breaks naive copies of
the install/bootstrap recipes. Use this recipe instead.

### Key gotchas

- `HERMES_HOME` is `~/.hermes/profiles/<name>` for a profile, not
  `~/.hermes/hermes-agent`. Bootstrap and diagnose scripts now resolve the
  profile path automatically; the install script copies plugin files into
  `$HERMES_HOME/plugins/memory/postgres` which under a profile means
  `~/.hermes/profiles/<name>/plugins/memory/postgres`.
- Each profile needs its **own** `PG_MEM_DB_CONN_STR` entry in
  `~/.hermes/profiles/<name>/.env`. The plugin reads only the active
  profile's `.env`.
- Profile config is at `~/.hermes/profiles/<name>/config.yaml`. Set
  `memory.provider: postgres` per profile.
- After install/config changes, restart the right process:
  `hermes -p <name> gateway restart`, or restart the specific profile's
  gateway process.
- Tool availability is per-session. Existing profile sessions do **not** see
  newly installed plugins until `/reset` or a fresh `hermes -p <name>`.

### Storage topology — pick one

Two reasonable layouts. Pick explicitly; do not mix mid-deployment.

**Layout 1 (default): shared DB, shared schema.** All profiles connect to the
same `PG_MEM_DB_CONN_STR`. Memory rows are shared across profiles; nothing
extra to do. Best when profiles are personal variants of one operator (e.g.
default vs worktrees) and you want one brain.

**Layout 2: one DB per profile.** Each profile gets its own database (and
ideally its own runtime role) under a single Postgres server. Cleaner blast
radius — you can drop `hermes_sportsverse` without nuking `hermes_admin`.
Required when profiles are owned by different operators on the same host or
when one profile is unstable and you want isolation.

Bootstrap for Layout 2:

```bash
# 1. As DB admin, create role+db for the new profile (mirrors the admin SQL file).
psql -h <host> -U postgres -d postgres \
  -v dbname='hermes_<profile>' \
  -v rolename='hermes_<profile>' \
  -v pw='choose_a_strong_password' \
  -v connlimit='20' \
  -f ~/repos/hermes-postgres-memory/plugins/memory/postgres/sql/000_create_database_and_role.sql

# 2. Bootstrap the new profile with its own DSN.
hermes -p <profile> -- skills list >/dev/null  # ensure profile exists
PG_MEM_DB_CONN_STR='postgresql://hermes_<profile>:***@host:5432/hermes_<profile>' \
  HERMES_HOME="$HOME/.hermes/profiles/<profile>" \
  ~/repos/hermes-postgres-memory/plugins/memory/postgres/scripts/bootstrap.sh
```

The bootstrap script auto-detects profile mode (its `HERMES_HOME` contains
`/profiles/`) and writes `PG_MEM_DB_CONN_STR` into the profile's own
`~/.hermes/profiles/<profile>/.env`.

### Connection limit math

The default `CONNECTION LIMIT 20` is for **one** runtime role. Each profile
opens its own pool (default 1-5 connections per agent process, more with
subagents and cron). For N profiles, plan at least `20 * N` as a floor.
Bump the role's `CONNECTION LIMIT` to `30 * N` for headroom. Avoid unlimited
roles — a bug in one profile can saturate the server. See
`references/postgres-memory-connection-limits.md` for the full breakdown.

### Verify a profile install

```bash
hermes -p <profile> postgres-memory preflight || true
hermes -p <profile> postgres-memory status || true
hermes -p <profile> postgres-memory model-list || true
```

Or via direct probes with the profile's DSN:

```bash
hermes -p <profile> config env-path  # prints the .env path the profile uses
set -a; . "$(hermes -p <profile> config env-path)"; set +a
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT current_user, current_database();"
```

If `pg_status`/`pg_remember` work in a `hermes -p <profile>` session, the
profile is wired correctly.

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

Show current model registry if the CLI subcommand exists:

```bash
hermes postgres-memory model-list || true
```

If `postgres-memory` is not registered in the installed Hermes build, inspect
`agent_memory_models` directly with SQL instead:

```bash
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT dim, provider, model, is_default FROM agent_memory_models ORDER BY dim;"
```

Set default dimension if the CLI subcommand exists:

```bash
hermes postgres-memory model-set --dim 1024 || true
```

Without working CLI subcommands, set the default via SQL only when the user
explicitly asks to change it; otherwise leave the current registry alone.

Set explicit provider/model if the CLI subcommand exists:

```bash
hermes postgres-memory model-set --dim 1024 --provider kimi --model bge_m3_embed || true
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
