# Migration privilege prerequisites (DDL on agent_memory)

The `hermes` application role typically has DML (SELECT/INSERT/UPDATE/DELETE)
on `agent_memory` but **no DDL**. The table is owned by a `postgres`
superuser. This is by design — the application role is intentionally
downscoped.

Any migration that does `ALTER TABLE` or `DROP INDEX` (e.g. the
embedding-dim migration `001_embedding_dim.sql`) will fail with:

```
ERROR:  must be owner of table agent_memory
```

## Why a GRANT does NOT fix this

`ALTER` and `DROP` on a table are **not** grantable privileges in
PostgreSQL. They are ownership-gated, not ACL-gated. The only standard
table-level privileges you can GRANT are:

```
SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, MAINTAIN
```

This was verified directly on a PG 18.4 server via
`aclexplode(pg_class.relacl)` and `has_table_privilege('agent_memory', 'ALTER')`
(the latter returns `ERROR: unrecognized privilege type: "alter"`).
The only paths to DDL on a table are:

1. **Be the owner** (full DDL)
2. **Be a superuser** (full DDL on everything)
3. **Be a member of a role that owns the table** (inherits the owner's rights)
4. **Invoke a SECURITY DEFINER function** owned by a privileged role
   (out of scope here)

## Symptoms

```
$ psql "$PG_MEM_DB_CONN_STR" \
    -f plugins/memory/postgres/migrations/001_embedding_dim.sql
# (Legacy: psql -h $POSTGRES_HOST -U hermes -d $POSTGRES_DATABASE -f ...
#  The DSN form is preferred as of v1.5.0.)
BEGIN
DROP INDEX
ERROR:  must be owner of table agent_memory
ROLLBACK
```

Or, if the index is dropped first:

```
$ psql ... -c "ALTER TABLE agent_memory DROP COLUMN content_vector;"
ERROR:  must be owner of table agent_memory
```

## Diagnose

```sql
-- Run as the hermes user
SELECT
  current_user,
  pg_catalog.pg_get_userbyid(c.relowner) AS table_owner,
  (aclexplode(c.relacl)).privilege_type
FROM pg_class c
WHERE c.relname = 'agent_memory';
```

If `table_owner != current_user`, the hermes role cannot perform DDL on
the table. The list of privilege types from `aclexplode` will be the
standard 8 DML privileges (SELECT/INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/
TRIGGER/MAINTAIN) — no ALTER, no DROP, because those cannot be granted.

## Fix: transfer ownership (one-time, as a superuser or current owner)

`migrations/000_grant_ddl_to_hermes.sql` ships with the skill. Despite
the historical name (it used to attempt GRANT statements), the only
working fix is to transfer ownership:

```bash
PGPASSWORD='your_postgres_password' psql -h 10.49.0.33 -U postgres -d hermes \
  -f ~/.hermes/hermes-agent/plugins/memory/postgres/migrations/000_grant_ddl_to_hermes.sql
# (When using the standalone repo, replace the path with
#  $REPO_ROOT/plugins/memory/postgres/migrations/000_grant_ddl_to_hermes.sql.)
```

The file does:

```sql
ALTER TABLE agent_memory OWNER TO hermes;
```

This is atomic. All existing DML grants are preserved (the GRANTs are
re-evaluated under the new owner; nothing about the DML rights changes).

### Security note

After ownership transfer, `hermes` can ALTER, DROP, TRUNCATE, or rename
the table. This is the simplest path forward. For production, prefer one
of:

- A **dedicated migration role** that owns schema objects, distinct from
  the application role. Same `ALTER TABLE ... OWNER TO migrator;` pattern,
  different grantee.
- A **SECURITY DEFINER function** owned by `postgres` that wraps the
  migration DDL, with `EXECUTE` granted to `hermes`. The application role
  never directly holds DDL; it calls into the trusted function.

## Verify the transfer took

```sql
-- Reconnect as hermes
SELECT pg_catalog.pg_get_userbyid(c.relowner) AS table_owner
FROM pg_class c
WHERE c.relname = 'agent_memory';
-- expected: hermes
```

## Re-run the migration

```bash
psql "$PG_MEM_DB_CONN_STR" \
  -f plugins/memory/postgres/migrations/001_embedding_dim.sql
# (Legacy: psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DATABASE -f ...)
```

It should now succeed.

## After the migration: tighten back (optional)

The embedding runtime (pg_remember, pg_search, backfill_embeddings.py) does
not need DDL. If you prefer to keep the hermes role downscoped after the
migration:

```sql
-- As a superuser, after the migration is done
ALTER TABLE agent_memory OWNER TO postgres;
```

You'll re-transfer for the next migration. The pattern is:
`OWNER TO hermes` → migrate → `OWNER TO postgres`.

## Why ownership transfer is the standard pattern, not a bug

PostgreSQL role privilege inheritance works the way Unix file perms do.
The owner of a table has full control over it. Granting DDL to an
application role is a conscious security choice — most teams separate
"app role" (DML) from "migration role" (DDL). The `hermes` role being
DDL-locked by default is a *defense-in-depth* posture, not an oversight.

The migration workflow in this skill assumes you'll transfer ownership
once per migration. If your shop has a dedicated `migrator` role
(separate from the app role), use that instead — same statement, different
grantee.
