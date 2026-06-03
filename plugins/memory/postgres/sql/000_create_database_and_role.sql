-- 000_create_database_and_role.sql
--
-- ONE-TIME ADMIN-SIDE prerequisite bootstrap for the postgres memory provider.
--
-- Creates the database, the non-superuser application role, and the pgvector extension.
-- This file MUST be run as a PostgreSQL superuser (typically the `postgres`
-- role), ONCE per server.
--
-- Defaults (all overridable via psql \setvars before \i or by editing):
--   database name  : hermes
--   role name      : hermes (dedicated non-superuser runtime role)
--   role password  : (randomly generated, 24 chars, must be set in psql)
--   connection limit for the role: 20
--
-- To use custom names, run with custom GUCs:
--
--   PGPASSWORD=*** psql -h <host> -U postgres -d postgres \
--     -v dbname='my_memory' -v rolename='my_hermes' -v pw='choose_a_strong_password' \
--     -f 000_create_database_and_role.sql
--
-- The script is idempotent. Re-running is safe. It will:
--   * Skip CREATE EXTENSION if pgvector is already installed.
--   * NOT reset the password if the role already exists. (Run by hand
--     if you actually want to rotate it: ALTER ROLE ... WITH PASSWORD '...';)
--   * NOT drop the database or role. To uninstall, see the README's
--     'Uninstall' section.
--
-- What this script does NOT do (and why):
--   * It does NOT create the agent_memory table or any other plugin
--     objects. That happens in 001_schema.sql, run as the `hermes` role
--     AFTER this script completes.
--   * It does NOT install pgvector as a server package. If the extension
--     CREATE fails with "could not open extension control file", you
--     need to install the OS package (e.g. `apt install postgresql-15-pgvector`)
--     before re-running.

\set ON_ERROR_STOP on

-- Read GUCs with safe fallbacks
\set dbname     `SELECT coalesce(:'dbname',     'hermes')`
\set rolename   `SELECT coalesce(:'rolename',   'hermes')`
\set connlimit  `SELECT coalesce(:'connlimit',  '20')::int`
-- Password is REQUIRED. The script will refuse to run without it
-- unless you set :allow_weak_pw to 'on' AND :pw is empty.
\set allow_weak_pw `SELECT coalesce(:'allow_weak_pw', '')`

\echo ''
\echo '════════════════════════════════════════════════════════════════'
\echo '  postgres memory provider — database + role bootstrap'
\echo '════════════════════════════════════════════════════════════════'
\echo '  target database : ' :dbname
\echo '  target role     : ' :rolename
\echo '  role connlimit  : ' :connlimit
\echo '  current user    : ' :'USER'
\echo '════════════════════════════════════════════════════════════════'
\echo ''

-- ─────────────────────────────────────────────────────────────────────
-- 1. Sanity checks
-- ─────────────────────────────────────────────────────────────────────

-- 1a. Must be a superuser. CREATE ROLE / CREATE EXTENSION require it.
DO $$
DECLARE
    v_super bool;
BEGIN
    SELECT rolsuper INTO v_super FROM pg_roles WHERE rolname = current_user;
    IF v_super IS NULL OR NOT v_super THEN
        RAISE EXCEPTION 'current user % is not a superuser. Re-run as the `postgres` role (or another superuser).', current_user;
    END IF;
END
$$;

-- 1b. Password must be set. We will not generate one for the user
-- (interactive prompt expected in bootstrap.sh). If you really want
-- to skip the check, set -v allow_weak_pw=on and leave :pw empty.
DO $$
BEGIN
    IF :'pw' = '' AND :'allow_weak_pw' <> 'on' THEN
        RAISE EXCEPTION 'password is required. Re-run with -v pw=''<your-strong-password>'' (or set -v allow_weak_pw=on to skip the check, NOT recommended).';
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────
-- 2. Create the role (idempotent)
-- ─────────────────────────────────────────────────────────────────────

-- The DO block lets us conditionally include the PASSWORD clause.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'rolename') THEN
        IF :'pw' <> '' THEN
            EXECUTE format('CREATE ROLE %I WITH LOGIN PASSWORD %L CONNECTION LIMIT %s',
                           :'rolename', :'pw', :'connlimit');
        ELSE
            -- allow_weak_pw=on, no password set. Role is created with
            -- LOGIN but no usable password. Only safe in trust-auth dev.
            EXECUTE format('CREATE ROLE %I WITH LOGIN CONNECTION LIMIT %s',
                           :'rolename', :'connlimit');
        END IF;
        RAISE NOTICE 'created role %', :'rolename';
    ELSE
        RAISE NOTICE 'role % already exists — leaving it alone (password not reset)', :'rolename';
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────
-- 3. Create the database (idempotent, owner = the role)
-- ─────────────────────────────────────────────────────────────────────

-- CREATE DATABASE cannot run inside a transaction block, so we use
-- a wrapper that checks first. We cannot use \if ... \fi here because
-- the database we're checking against IS the one we'd be creating.
SELECT format('CREATE DATABASE %I OWNER %I ENCODING ''UTF8'' LC_COLLATE ''C'' TEMPLATE template1',
              :'dbname', :'rolename')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname')
\gexec

-- Belt-and-suspenders: if the database exists with the wrong owner,
-- log it (do not silently change ownership — that could surprise the user).
DO $$
DECLARE
    v_owner name;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(d.datdba) INTO v_owner
    FROM pg_database d
    WHERE d.datname = :'dbname';

    IF v_owner IS NULL THEN
        -- Should not happen (we just created or confirmed it exists), but be safe.
        RAISE EXCEPTION 'database % does not exist after CREATE step — investigate manually', :'dbname';
    ELSIF v_owner <> :'rolename' THEN
        RAISE NOTICE 'database % exists but is owned by % (not %). Plugin will still work — hermes role will be GRANTed USAGE — but the role is not the owner.', :'dbname', v_owner, :'rolename';
    ELSE
        RAISE NOTICE 'database % exists, owner = % ✓', :'dbname', v_owner;
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────
-- 4. Grant the role the privileges it needs to run plugin DDL
--    (create the pgvector extension + create the agent_memory table)
-- ─────────────────────────────────────────────────────────────────────

-- These grants are database-level. They let the hermes role create
-- the pgvector extension in the target database, and create objects
-- (tables, indexes) of its own.

-- Need to connect to the target DB to run these. \c terminates the
-- current transaction so it must be the LAST command in this file
-- (after ON_ERROR_STOP is set and all the above has succeeded).
-- Actually, on_error_stop is set, and we are already in default
-- autocommit per statement for DDL. \c will reconnect.

\echo ''
\echo '→ Connecting to the target database to install pgvector and grant object creation rights...'
\connect :dbname

-- 4a. The pgvector extension. Must be installed as a superuser (or
--     as the database owner if pgvector's control file is readable;
--     typically superuser in practice). Run it NOW, before granting
--     the hermes role CREATE on schema, so the extension's tables
--     land in pg_catalog and are not affected by future GRANTs.
CREATE EXTENSION IF NOT EXISTS vector;
\echo '  ✓ pgvector extension installed (or already present)'

-- 4b. Make the hermes role the owner of the public schema in this DB
--     so it can CREATE TABLE / CREATE INDEX. This is the standard
--     Postgres convention — public schema owner is the DB owner by
--     default, and we want plugin DDL to "just work".
ALTER SCHEMA public OWNER TO :rolename;
\echo '  ✓ public schema owner set to ' :rolename

-- 4c. Belt-and-suspenders: explicit grants. These are already implicit
--     for the public schema owner, but listing them makes the intent
--     obvious to anyone reading this file later.
GRANT ALL ON SCHEMA public TO :rolename;
\echo '  ✓ GRANT ALL ON SCHEMA public TO ' :rolename

-- 4d. Allow the role to create databases in the future (handy if the
--     user wants a second hermes DB for testing). Cheap to grant.
--     Skipping this to keep the role downscoped. Uncomment to enable:
-- ALTER ROLE :rolename CREATEDB;

-- ─────────────────────────────────────────────────────────────────────
-- 5. Final summary
-- ─────────────────────────────────────────────────────────────────────

\echo ''
\echo '════════════════════════════════════════════════════════════════'
\echo '  ✓ bootstrap complete'
\echo '════════════════════════════════════════════════════════════════'
\echo '  next steps:'
\echo '  1. add to ~/.hermes/.env:'
\echo '       PG_MEM_DB_CONN_STR=postgresql://' :rolename ':<the password you used above>@<host>:' :port '/' :dbname
\echo '       KIMI_API_KEY=<from https://platform.moonshot.cn>'
\echo '       PG_MEM_DB_CONN_STR is the only supported runtime DB connection setting.'
\echo '  2. give the runtime DSN to the agent and run the agent-side bootstrap:'
\echo '       ./plugins/memory/postgres/scripts/bootstrap.sh'
\echo '     bootstrap verifies prerequisites and runs 000_schema.sql as the runtime role.'
\echo '  3. restart the hermes gateway: hermes gateway restart'
\echo '  4. verify: hermes postgres-memory preflight && hermes postgres-memory status'
\echo '════════════════════════════════════════════════════════════════'
\echo ''
