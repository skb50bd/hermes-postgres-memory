# PostgreSQL memory provider connection exhaustion

## Situation captured

A Hermes deployment had `memory.provider: postgres`; the plugin was installed and Postgres was reachable, but the provider was unavailable. A direct psycopg2 probe failed with:

```text
OperationalError: connection to server at "<host>", port 5432 failed: FATAL: too many connections for role "hermes"
```

`pg_isready` still reported the database accepting connections, confirming that this was not a network outage or missing environment variables.

## Useful checks

Check role limit and current usage:

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

Clear old idle sessions if they are safely stale:

```sql
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE usename = 'hermes'
  AND state = 'idle'
  AND now() - state_change > interval '10 minutes';
```

Raise the role limit:

```sql
ALTER ROLE hermes CONNECTION LIMIT 20;
```

Use `30` only for heavier multi-profile / multi-worker deployments. Avoid unlimited role limits for app users.

## Plugin behavior to watch

The observed PostgreSQL provider implementation used one persistent psycopg2 connection per `_PostgresClient`/provider instance and closed it only during provider shutdown. That can multiply across:

- gateway process/session handling
- CLI/API sessions
- profiles
- cron jobs
- subagents or spawned Hermes processes

The safer pattern is a small per-process pool with short checked-out connections.

## Provider hardening pattern that worked

Patch shape for a psycopg2-based Hermes memory provider:

- Add a module-level pool plus lock: `_POOL = None`, `_POOL_LOCK = threading.Lock()`.
- Import `psycopg2.pool` explicitly; `import psycopg2` alone may not expose the `pool` attribute in tests or fresh interpreters.
- Build the DSN with `psycopg2.extensions.make_dsn(...)` so options and names are correctly escaped.
- Default env knobs:
  - `HERMES_POSTGRES_POOL_MIN=0`
  - `HERMES_POSTGRES_POOL_MAX=2`
  - `HERMES_POSTGRES_CONNECT_TIMEOUT=5`
  - `HERMES_POSTGRES_STATEMENT_TIMEOUT_MS=10000`
  - `HERMES_POSTGRES_IDLE_TX_TIMEOUT_MS=30000`
- Include `application_name=hermes-memory-postgres` for `pg_stat_activity` visibility.
- Apply timeouts through DSN options:
  - `-c statement_timeout=<ms>`
  - `-c idle_in_transaction_session_timeout=<ms>`
- Create `_PostgresClient._cursor()` as a context manager:
  - get connection from pool
  - set `autocommit = True`
  - create cursor
  - yield cursor
  - close cursor in `finally`
  - return connection to pool in `finally`
- Convert all CRUD/status paths from `cur = self._cursor()` to `with self._cursor() as cur:`.
- Make `shutdown()` call module-level close-all and reset `_POOL = None`.
- Make `is_available()` use the pooled cursor path so availability checks do not add a separate direct connection path.

## Tests worth adding

Unit tests can monkeypatch `psycopg2.pool.ThreadedConnectionPool` with a fake pool and avoid any real DB:

- Two `_PostgresClient()` instances share one module-level pool.
- Default pool settings are min=0, max=2.
- DSN contains `connect_timeout=5` and `application_name=hermes-memory-postgres`.
- Normal queries call `getconn()` and `putconn()` exactly once each.
- Exceptions during query execution still return the connection.
- Provider shutdown calls `closeall()` and clears the module pool.
- Patch `psycopg2.connect` to raise in tests proving normal operations no longer use direct connections.

Useful command sequence:

```bash
python -m pytest tests/plugins/memory/test_postgres_pool.py -q -o 'addopts='
python -m pytest tests/plugins/memory/test_postgres_pool.py tests/hermes_cli/test_plugins.py -q -o 'addopts='
python -m py_compile plugins/memory/postgres/__init__.py tests/plugins/memory/test_postgres_pool.py
hermes memory status
```

## Verification after the fix

- The new focused tests pass.
- Related plugin tests pass.
- `py_compile` passes.
- `hermes memory status` reports the Postgres provider as available after the role limit is raised and the plugin patch is loaded.
- Restart gateway/session workers so long-lived processes load the new plugin code.
