# Hermes profiles + postgres memory — gotchas

This document is the deep-dive companion to **Recipe D** in `SKILL.md`. Read
the recipe for the quick path; read this when something is broken in a
profile-mode Hermes install and you need to understand why.

## What "profile mode" means in Hermes

`hermes profile` is Hermes' multi-instance primitive. Each profile has its
own isolated tree under `~/.hermes/profiles/<name>/`:

```text
~/.hermes/
├── .env                          # root instance only
├── config.yaml                   # root instance only
├── auth.json                     # root instance only
├── skills/                       # root instance only
├── plugins/                      # root instance only
├── hermes-agent/                 # root instance checkout (if git-installed)
└── profiles/
    ├── default/                  # "default" is also a profile
    │   ├── .env
    │   ├── config.yaml
    │   ├── auth.json
    │   ├── skills/
    │   └── plugins/
    ├── work/
    │   ├── .env
    │   ├── config.yaml
    │   ├── auth.json
    │   ├── skills/
    │   └── plugins/
    └── personal/
        └── ...
```

In profile mode, the **active** profile is determined by the `-p <name>`
flag, the `HERMES_PROFILE` env var, or the sticky default set with
`hermes profile use`. Hermes reads the `.env` and `config.yaml` from
`~/.hermes/profiles/<active>/` only. The root `~/.hermes/.env` is not
inherited unless you opt in via symlink or duplication.

## Why this matters for the postgres memory plugin

The plugin reads `PG_MEM_DB_CONN_STR` from the active Hermes instance's
`.env` (via the standard Hermes env-loading path, see
`plugins/memory/postgres/__init__.py::get_pg_mem_db_conn_str()`). Under
profile mode, that's `~/.hermes/profiles/<active>/.env`, not
`~/.hermes/.env`.

Three failure modes this causes:

1. **"PG_MEM_DB_CONN_STR missing" even though you set it.** You put it in
   the root `~/.hermes/.env` but you're running a profile. Hermes doesn't
   see it. Fix: also write the line into `~/.hermes/profiles/<active>/.env`,
   or symlink the profile's `.env` to the root one if you genuinely want a
   single source.
2. **Bootstrap writes to the wrong .env.** Pre-fix `bootstrap.sh` always
   wrote to `~/.hermes/.env`. After the v1.7.x fix it auto-detects
   profile mode and writes to `~/.hermes/profiles/<active>/.env`.
3. **Config block lives in the wrong file.** `memory.provider: postgres`
   must be set in `~/.hermes/profiles/<active>/config.yaml`, not the root
   one, or the active profile uses whatever default its own config falls
   back to (usually `local`).

## Connection limit math, for real

The default `CONNECTION LIMIT 20` is calibrated for a single runtime role
with one agent process, a few subagents, and the occasional cron tick.
Profile deployments blow past that quickly.

Rough formula per profile:

```text
base connections = 1 (agent main loop)
+ 1 to 4 (open psycopg2 connection pool, default 1-5)
+ 1 per concurrent subagent
+ 1 per active cron job
+ 1 per kanban worker
+ 1 headroom
≈ 5 to 10 per profile under normal load
```

For N profiles sharing one role:

```text
recommended role CONNECTION LIMIT = max(20, 10 * N + 5)
```

For dedicated role per profile (cleaner), each role can stay at 20.

Avoid `CONNECTION LIMIT -1`. A runaway subagent in one profile should not
be able to saturate the entire Postgres server.

## Shared vs isolated storage — decision matrix

| | Shared DB, shared schema (Layout 1) | One DB per profile (Layout 2) |
|---|---|---|
| Setup effort | Lowest — one admin SQL run, one DSN | Higher — N admin SQL runs, N DSNs |
| Memory sharing | Yes — all profiles read/write same rows | No — strictly isolated |
| Connection limit | Multiply by N (shared role) | Per-role limit, isolated pools |
| Blast radius | One bad profile can pollute everyone | Drop one DB, others keep working |
| Best for | Personal profiles of one operator | Multi-operator hosts, untrusted profiles, work/personal split |
| Cost | One DB, one role, ~20 conn | N DBs, N roles, ~20 conn each |

You can mix: a shared "common memory" DB plus a per-profile "private memory"
DB by setting two different `PG_MEM_DB_CONN_STR` values, but the current
plugin only reads one. To do mixed, you need two installs or a future
multi-DSN feature.

## Per-profile bootstrap recipe

```bash
# 1. (Layout 2 only) DB admin creates role+db+pgvector for the new profile.
psql -h <host> -U postgres -d postgres \
  -v dbname='hermes_<profile>' \
  -v rolename='hermes_<profile>' \
  -v pw='choose_a_strong_password' \
  -v connlimit='20' \
  -f ~/repos/hermes-postgres-memory/plugins/memory/postgres/sql/000_create_database_and_role.sql

# 2. Ensure the profile exists in Hermes.
hermes profile list
hermes -p <profile> config env-path

# 3. Bootstrap the profile with its own DSN.
#    bootstrap.sh auto-detects profile mode and writes to the profile's .env.
PG_MEM_DB_CONN_STR='postgresql://hermes_<profile>:***@host:5432/hermes_<profile>' \
  HERMES_HOME="$HOME/.hermes/profiles/<profile>" \
  ~/repos/hermes-postgres-memory/plugins/memory/postgres/scripts/bootstrap.sh
```

If `HERMES_HOME` is not exported, bootstrap derives it from
`$HOME/.hermes` and `~/.hermes/hermes-agent`. For profiles, export
`HERMES_HOME="$HOME/.hermes/profiles/<name>"` to be explicit.

## Per-profile verification

```bash
# CLI verifier (only works if the build registers postgres-memory)
hermes -p <profile> postgres-memory preflight || true
hermes -p <profile> postgres-memory status || true
hermes -p <profile> postgres-memory model-list || true

# Direct DB probe against the profile's DSN
PROFILE_ENV="$(hermes -p <profile> config env-path)"
set -a; . "$PROFILE_ENV"; set +a
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT current_user, current_database();"
psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT count(*) FROM agent_memory;"

# Plugin tool smoke test in a fresh profile session
hermes -p <profile> chat -q "Run pg_status() and pg_remember(content='profile test', category='fact')"
```

A profile is correctly wired when `pg_status` and `pg_remember` work in a
fresh `hermes -p <profile>` session. Old sessions don't see new plugins —
that's expected, the toolset is snapshotted at session start.

## Common profile-mode footguns

- **Forgetting to restart the right process.** `hermes gateway restart`
  restarts the root gateway. Profile gateways are separate systemd
  units/processes; restart those explicitly with
  `hermes -p <name> gateway restart` or via the per-profile service.
- **Tools missing in a long-lived session.** Existing sessions cached
  their tool list at startup. Run `/reset` or start a new `hermes -p <name>`
  session to pick up newly installed plugins.
- **Two profiles pointing at the same DSN with different default dim.**
  Writes from profile A land in `vector_1024`, from profile B in
  `vector_768`. Reads from either profile see both, but cosine similarity
  is meaningless across dims — those rows will be semantically noise.
  Pick one default per DSN.
- **Sharing one KIMI_API_KEY across profiles.** Fine for rate limits
  usually, but profile A's heavy ingest can starve profile B's casual
  recall. Use a credential pool (`hermes auth add kimi …`) if you
  hit this.
- **Running the same `agent_memory` from non-Hermes clients.** Fine.
  Just respect the same role/DSN rules.

## Uninstall / migration per profile

`plugins/memory/postgres/scripts/uninstall.sh` only touches the active
profile's install. To clean all profiles, run it once per profile
directory. To migrate a profile to a new DSN:

```bash
hermes -p <profile> config env-path
# edit the printed .env, change PG_MEM_DB_CONN_STR
hermes -p <profile> gateway restart
hermes -p <profile> postgres-memory preflight || true
```

Migration of the DB itself (e.g. layout 1 → layout 2) is a database
operation: dump, create new DB, restore, swap DSN, drop old DB. The
plugin does not do this for you; treat it like any other Postgres
migration.

## See also

- `references/postgres-memory-connection-limits.md` — connection pool math
- `references/database-bootstrap.md` — admin-side SQL details
- `SKILL.md` Recipe D — quick-path profile bootstrap
- `hermes-agent` skill — `hermes profile` command reference
