# First-time onboarding checklist for the postgres memory provider

Use this checklist before you run `bootstrap.sh` (or `install.sh`). Each
item has a one-liner the agent can run to verify it. If anything fails,
the matching item in this document tells you what to fix.

The plugin is self-healing for the things it can control (the
`agent_memory` schema, the `agent_memory_settings` defaults, the
`agent_memory_models` registry). It is NOT self-healing for the things
that require superuser or OS-level intervention. That's what this
checklist separates.

## Pre-flight (run these first)

```bash
# 1. psql is on PATH
command -v psql

# 2. PostgreSQL is reachable
PGPASSWORD=*** pg_isready -h <host> -p <port> -U postgres

# 3. You can connect as a superuser
PGPASSWORD=*** psql -h <host> -p <port> -U postgres -d postgres -c '\du'

# 4. The hermes-agent checkout exists
test -d ~/.hermes/hermes-agent/plugins/memory && echo OK

# 5. ~/.hermes/.env exists (or you're willing to create it)
test -f ~/.hermes/.env && echo OK

# 6. Python can import psycopg2
python3 -c "import psycopg2; print(psycopg2.__version__)"
```

If any of 1–4 fails, **stop and fix it** before running `bootstrap.sh`.
The bootstrap script will tell you about 5 and 6, but it doesn't create
the .env or install psycopg2 for you (it will offer to install
psycopg2, but not silently).

## Database-side prerequisites (one-time, requires superuser)

The plugin will need:

- A **database** (default name: `hermes`)
- An **application role** (default name: `hermes`) with:
  - `LOGIN` (so it can connect)
  - A non-empty `PASSWORD` (the plugin needs it to authenticate)
  - `CONNECTION LIMIT 20` (sensible default for a single-user Hermes
    deployment; bump to 30+ if you run many concurrent gateway workers,
    cron jobs, profiles, and subagents)
  - **NOT** `SUPERUSER`. The plugin does not need it, and giving it to
    an app role is a privilege escalation waiting to happen.
- The **`vector` extension** installed in the target database
- The application role must **own the `public` schema** of the target
  database, so the plugin can `CREATE TABLE` / `CREATE INDEX`

The bootstrap script handles all four of these if you give it
superuser credentials. If you want to do them by hand:

```sql
-- As postgres (superuser), in psql connected to the 'postgres' DB:
CREATE ROLE hermes WITH LOGIN PASSWORD 'choose_a_strong_password' CONNECTION LIMIT 20;
CREATE DATABASE hermes OWNER hermes ENCODING 'UTF8' LC_COLLATE 'C' TEMPLATE template1;

-- Then \c hermes
\c hermes
CREATE EXTENSION vector;
ALTER SCHEMA public OWNER TO hermes;
GRANT ALL ON SCHEMA public TO hermes;
```

Verify:

```sql
SELECT current_user;                                    -- should be 'hermes'
SELECT extname FROM pg_extension WHERE extname='vector'; -- should print 'vector'
SELECT pg_catalog.pg_get_userbyid(n.nspowner)
  FROM pg_namespace n WHERE n.nspname='public';          -- should print 'hermes'
```

If the last query returns `postgres` instead of `hermes`, the plugin
will be unable to `CREATE TABLE` (it'll fail with "permission denied
for schema public"). Re-run the `ALTER SCHEMA public OWNER TO hermes;`
line.

## OS-level prerequisites (one-time, requires root)

The `vector` extension is shipped as an OS package separately from
`postgresql` itself. On Debian/Ubuntu:

```bash
# Pick the version that matches your PG server (e.g. postgresql-15-pgvector)
apt install postgresql-15-pgvector
```

On RHEL/Rocky:

```bash
dnf install pgvector_<your_pg_version>.x86_64.rpm  # not in EPEL by default
# OR: pgvector is in PostgreSQL's own YUM repo (pgdg)
```

On macOS (Homebrew):

```bash
brew install pgvector
```

If you skip this step, `CREATE EXTENSION vector;` will fail with:

```
ERROR:  could not open extension control file ".../share/extension/vector.control": No such file or directory
```

## Embedder prerequisites (one-time, requires an API key)

Pick **at least one** of these. The plugin supports 3 dims out of the
box and lets you switch between them at runtime.

| Dim | Default provider/model | API key env var | Cost |
|---|---|---|---|
| 768 | `ollama_local` / `nomic-embed-text` | `OLLAMA_API_KEY` (only for `ollama_cloud`) | Free, local |
| 1024 | `kimi` / `bge_m3_embed` | `KIMI_API_KEY` | Free tier |
| 1536 | `kimi` / `text-embedding-3-small` (returns 1024-dim; OpenAI is the real 1536 path) | `KIMI_API_KEY` (default) or `OPENAI_API_KEY` | Free (default) or paid (OpenAI) |

**Default behaviour**: the plugin ships with the Kimi BGE-M3 model for
1024-dim. The free Kimi tier is enough for a single-user deployment
(verify the latest rate limits at https://platform.moonshot.cn). If you
have a Kimi key, that's all you need.

If you don't have a Kimi key but have Ollama running locally:

```bash
ollama pull nomic-embed-text
# Add to ~/.hermes/.env:
# HERMES_EMBED_PROVIDER_1024=ollama_local
# HERMES_EMBED_MODEL_1024=nomic-embed-text
# Now 1024-dim is served by your local Ollama
hermes postgres-memory model-set --dim 1024 --provider ollama_local --model nomic-embed-text
```

If you want 1536-dim with real OpenAI vectors:

```bash
# (Requires the 'openai' provider to be wired into embedder.py — not yet shipped.)
# For now, the 1536-dim default uses Kimi, which actually returns 1024-dim
# vectors regardless of model name. So a '1536' row written via Kimi is
# actually a 1024-dim vector stored in vector_1536 with a dimension
# mismatch — DO NOT mix this with real 1536-dim rows.
```

## Python prerequisites (one-time, in the gateway's venv)

The plugin imports `psycopg2` at module load. The hermes-agent
distribution ships with a venv at `~/.hermes/hermes-agent/venv/`
that's stripped of pip and most packages. You have three options:

**Option A: install into the hermes-agent venv**

```bash
~/.hermes/hermes-agent/venv/bin/pip install psycopg2-binary
```

This is the cleanest option. The gateway will pick it up automatically
because it uses the same venv.

**Option B: install into a system python that the gateway can see**

```bash
pip install --user psycopg2-binary
# or, with sudo:
sudo pip install psycopg2-binary
```

This works if the gateway's python path includes the system or user
site-packages. Hermit venvs often don't, so this is hit-or-miss.

**Option C: use a sidecar venv**

The bootstrap script can install psycopg2 into a sidecar venv and set
`PYTHONPATH` to include it. The plugin is pure Python so it doesn't
care which interpreter loads it, as long as the venv is on `sys.path`.

The `bootstrap.sh` script will offer to install `psycopg2-binary` for
you (in the active python) if it's missing.

## Runtime validation (after install)

Once `bootstrap.sh` finishes and you've added the API key and restarted
the gateway, the agent should run:

```bash
hermes postgres-memory preflight
hermes postgres-memory status
hermes postgres-memory model-list
```

The preflight checks 14 things. Status shows the per-dim row count and
the embedder cache stats. Model-list shows the per-dim model registry
(3 rows by default).

Then in a fresh Hermes session, the agent should run:

```
pg_remember(content="postgres plugin is live", category="fact")
pg_search(query="postgres plugin")
```

If `pg_search` returns the test memory, you're done.

## What if the user already has a 1.x install?

If the user is upgrading from the 1.0/1.1 single-dim schema, do **not**
run `000_schema.sql` — it'll fail with "table already exists". Instead:

1. The `diagnose.sh` will detect the existing `agent_memory` table and
   report it as "present with all 3 per-dim columns" only if the user
   has already run `001_add_per_dim_columns.sql` etc.
2. If the user has only the legacy `content_vector` column, run the
   migration set in `migrations/000_grant_ddl_to_hermes.sql` through
   `004_drop_legacy_column.sql` in order. The `migration-privileges.md`
   reference explains the ownership-transfer step.
3. The plugin auto-detects the legacy column and uses it for searches
   until `finalize-cutover --yes` is run.

The `hermes-postgres-memory` skill has full upgrade documentation.
