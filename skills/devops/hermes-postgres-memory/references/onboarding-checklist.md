# Postgres memory onboarding checklist

Greenfield-only checklist for installing the Hermes Postgres memory provider.

## Required before install

- PostgreSQL 13+ reachable
- `psql` and `pg_isready` available
- DBA has installed `vector` extension in the target database
- Dedicated non-superuser application role/database, usually `hermes` / `hermes`
- Runtime role owns `public` schema and can create/drop plugin objects
- `~/.hermes/.env` contains `PG_MEM_DB_CONN_STR`
- An embedder key is available, usually `KIMI_API_KEY`
- Hermes config can set `memory.provider: postgres`

## Preflight commands

```bash
plugins/memory/postgres/scripts/diagnose.sh
pg_isready -d "$PG_MEM_DB_CONN_STR"
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT 1;"
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

## Install commands

```bash
./plugins/memory/postgres/scripts/bootstrap.sh
# bootstrap verifies DBA prerequisites; it does not use superuser access
# or, if DB already exists and is already verified:
./install.sh
psql "$PG_MEM_DB_CONN_STR" -f plugins/memory/postgres/sql/000_schema.sql
```

## Verify

```bash
hermes postgres-memory preflight
hermes postgres-memory status
hermes postgres-memory model-list
```

Fresh Hermes session smoke test:

```text
pg_remember(content="postgres plugin is live", category="fact")
pg_search(query="postgres plugin")
```

## Failure mapping

- Missing `PG_MEM_DB_CONN_STR`: edit `~/.hermes/.env` and restart Hermes.
- `pg_isready` fails: host/port/firewall/role credential issue in the DSN.
- `pgvector` missing: ask the DB admin to run `sql/000_create_database_and_role.sql` or otherwise `CREATE EXTENSION vector`; do not use agent superuser access.
- Schema tables missing: run `sql/000_schema.sql` using `PG_MEM_DB_CONN_STR`; permission errors mean DBA prerequisites are incomplete.
- Search empty: confirm `vector_<dim>` rows exist and query has FTS token overlap.
