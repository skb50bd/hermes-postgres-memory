# Database bootstrap for the postgres memory provider

This reference explains what the database needs, why each requirement
exists, and the exact SQL the `000_create_database_and_role.sql` script
runs. Read this if you need to bootstrap the database by hand, or if
the bootstrap script is failing and you want to understand why.

## What the plugin needs from the database

| Requirement | Why | Where it's enforced |
|---|---|---|
| A `vector` extension | pgvector is what makes the per-dim columns indexable and queryable by cosine distance | `000_create_database_and_role.sql` |
| A database (default: `hermes`) | The plugin connects to one DB; everything lives in the `public` schema | Same file |
| A role (default: `hermes`) | The plugin authenticates as one role; the role must own the schema to do DDL | Same file |
| The role owns the `public` schema | `CREATE TABLE` / `CREATE INDEX` are owner-gated, not grant-gated | Same file |
| A connection limit (default: 20) | Prevents a buggy plugin from saturating the server's connection slots | Same file |
| The role is **not** a superuser | Defense in depth. The plugin never needs DDL on system tables. | (Not enforced by the script — just convention) |

## What the SQL does, step by step

`000_create_database_and_role.sql` is the only file in the plugin that
requires superuser privileges. It must be run as `postgres` (or another
superuser role). It accepts GUCs so the database/role names and
password can be customized:

```bash
PGPASSWORD=*** psql -h <host> -U postgres -d postgres \
  -v dbname=my_memory \
  -v rolename=my_hermes \
  -v pw=choose_a_strong_password \
  -v connlimit=30 \
  -f 000_create_database_and_role.sql
```

GUCs (all optional, with safe defaults):

| GUC | Default | Notes |
|---|---|---|
| `dbname` | `hermes` | The database the plugin will connect to |
| `rolename` | `hermes` | The role the plugin will authenticate as |
| `pw` | (none, REQUIRED) | The role's password. Empty password is rejected unless `allow_weak_pw=on` |
| `connlimit` | `20` | The role's per-server connection limit |
| `allow_weak_pw` | `off` | Set to `on` to allow empty password (dev / trust-auth only) |

What the script does, in order:

1. **Sanity checks**
   - Verifies `current_user` is a superuser (raises `EXCEPTION` if not)
   - Verifies `pw` is non-empty (raises `EXCEPTION` if not, unless
     `allow_weak_pw=on`)
2. **Creates the role** (`CREATE ROLE`) — idempotent: skipped if the
   role already exists. The password is only set on creation; if you
   re-run the script on a server where the role already exists, the
   password is left alone. To rotate a password, run by hand:
   `ALTER ROLE hermes WITH PASSWORD 'new_password';`
3. **Creates the database** (`CREATE DATABASE ... OWNER ...`) —
   idempotent. The `\gexec` trick is used because `CREATE DATABASE`
   can't run inside a transaction block, so we can't use a `DO $$ ...
   END $$` wrapper. After the `\gexec`, a follow-up `DO $$` block
   verifies the database exists and (if it pre-existed) reports a
   warning if the owner is different from `rolename`.
4. **Reconnects to the new database** with `\connect :dbname`. This is
   why the script ends in this DB rather than `postgres`.
5. **Installs the `vector` extension** (`CREATE EXTENSION IF NOT EXISTS
   vector;`) — must be run as a superuser or as the database owner
   after the extension's control file is readable. Typically
   superuser-only in practice.
6. **Transfers ownership of the `public` schema to the new role**
   (`ALTER SCHEMA public OWNER TO :rolename;`). This is the critical
   privilege transfer. After this, the hermes role can `CREATE TABLE`
   and `CREATE INDEX` in the database.
7. **Prints a summary** with the next-steps commands the user needs to
   run by hand (add to .env, install plugin, restart gateway, etc.).

## The password-piping caveat

`psql` reads the password from `PGPASSWORD` env var or from
`~/.pgpass`. The bootstrap script uses `PGPASSWORD`. If you copy-paste
this:

```bash
PGPASSWORD=*** psql -h <host> -U postgres ...
```

…and the password has a `$` or a space or a backtick in it, bash will
eat those characters before psql sees them. Two safe patterns:

**Pattern A: use `~/.pgpass`**

```
# ~/.pgpass
<host>:<port>:<db>:<user>:<password>
chmod 600 ~/.pgpass
```

Then `psql` picks it up automatically and you don't need `PGPASSWORD`
in the environment at all.

**Pattern B: pass via a process substitution**

```bash
psql -h <host> -U postgres "host=<host> user=postgres password=<the actual literal password>"
```

The connection-string form is shell-safe (no env var interpolation).
Note that this prints the password in `ps`'s args list, so it's not
great for shared systems. Prefer `~/.pgpass` for production.

**Pattern C: a here-doc**

```bash
PGPASSFILE=<(umask 077; printf '%s' "<host>:<port>:<db>:<user>:<password>" > /dev/stdout) \
    psql -h <host> -U postgres ...
```

Or write a temp file with the right perms and point `PGPASSFILE` at it:

```bash
umask 077
TMP=$(mktemp)
printf '%s' "<host>:<port>:<db>:<user>:<password>" > "$TMP"
PGPASSFILE="$TMP" psql -h <host> -U postgres -c '\du'
rm -f "$TMP"
```

The bootstrap script uses Pattern A (env var + `set -a` to load from
`.env` if present) but you'll need to be careful with quoting if your
password contains special characters.

## Why no GRANT statements?

The script doesn't `GRANT CREATE` on the schema because that doesn't
work — `CREATE` is not a grantable privilege in PostgreSQL. The only
table-level privileges you can GRANT are:

```
SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, MAINTAIN
```

`ALTER` and `DROP` (and `CREATE`) are ownership-gated. The only ways
to get them are:

1. Own the table/schema
2. Be a superuser
3. Be a member of the role that owns the table
4. Invoke a `SECURITY DEFINER` function owned by a privileged role

This is verified directly via `aclexplode` on the table's `relacl` —
inspect table ownership directly with `pg_class.relowner` if needed.

The `ALTER SCHEMA public OWNER TO hermes;` statement is therefore the
single most important line in the whole bootstrap. Without it, the
plugin will fail at `CREATE TABLE agent_memory ...` with "permission
denied for schema public".

## Connection limits

The default `CONNECTION LIMIT 20` is right for a single-user Hermes
deployment with a gateway + occasional subagents. If you run many
concurrent things (multiple profiles, cron jobs, parallel subagents,
kanban workers, etc.), bump it to 30 or 50. Avoid `-1` (unlimited) for
app roles — it lets a bug in the plugin saturate the server.

Verify the limit:

```sql
SELECT rolname, rolconnlimit FROM pg_roles WHERE rolname = 'hermes';
```

Change it (as superuser):

```sql
ALTER ROLE hermes CONNECTION LIMIT 50;
```

If you actually hit the limit, you'll see this in the gateway log:

```
psycopg2.OperationalError: FATAL:  too many connections for role "hermes"
```

The fix is to either bump the limit (above) or shrink the plugin's
connection pool with `HERMES_POSTGRES_POOL_MAX=2` (default). Each
Hermes process opens at most 2 connections, so with the default pool
size and 20 connections you can have 10 concurrent Hermes processes
before hitting the limit. Plenty for most deployments.

## What if I already have a database with this name?

The script is idempotent. Re-running with the same `dbname` and
`rolename` is a no-op (the `\gexec` won't re-issue `CREATE DATABASE`
because the row exists in `pg_database`; the role-creation `DO $$`
block skips because the row exists in `pg_roles`).

If the database exists but is owned by a different role (e.g.
`postgres`), the script will print a NOTICE and continue. The plugin
will still work because the script also transfers ownership of the
`public` schema to `hermes`, which is what actually matters for DDL.

If you want to *change* the database owner to match the new role, do
that by hand:

```sql
-- As superuser
ALTER DATABASE hermes OWNER TO hermes;
```

This is purely cosmetic; the plugin doesn't care who owns the
database, only who owns the schema.

## Uninstall

To remove the database, role, and schema (separate from the plugin
files):

```bash
plugins/memory/postgres/scripts/uninstall.sh --all --role --database --yes
```

The script asks for confirmation before each destructive step (use
`--yes` to skip). It does **not** remove the `pgvector` extension by
default — that's a server-wide change, so do it by hand only after
every table that uses it is gone:

```sql
-- As superuser, after every plugin table is dropped
DROP EXTENSION IF EXISTS vector;
```
