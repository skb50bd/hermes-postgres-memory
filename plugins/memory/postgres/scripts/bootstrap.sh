#!/usr/bin/env bash
# bootstrap.sh — agent-side installer for the postgres memory provider.
#
# This script intentionally does NOT ask for PostgreSQL superuser credentials
# and does NOT create roles, databases, extensions, or privileged grants.
# Those are DBA/user prerequisites. The agent's job is to verify they are in
# place before installing/using the plugin. If they are missing, stop and hand
# the user the SQL/admin steps; do not improvise with escalated access.
#
# What it does, in order:
#   1. Sanity checks local tooling and the Hermes checkout.
#   2. Reads or prompts for PG_MEM_DB_CONN_STR.
#   3. Verifies the DSN connects as a non-superuser runtime role.
#   4. Verifies DBA prerequisites: pgvector exists and the runtime role owns
#      the public schema so plugin DDL can run.
#   5. Writes PG_MEM_DB_CONN_STR to ~/.hermes/.env if missing.
#   6. Installs plugin + skill via install.sh.
#   7. Runs 000_schema.sql through PG_MEM_DB_CONN_STR.
#   8. Runs diagnose.sh.
#
# Usage:
#   ./bootstrap.sh                           # interactive
#   PG_MEM_DB_CONN_STR='postgresql://...' ./bootstrap.sh --non-interactive
#   ./bootstrap.sh --hermes-home /path/to/hermes-agent
#
# Env vars:
#   HERMES_HOME          (default: ~/.hermes/hermes-agent)
#   PG_MEM_DB_CONN_STR   required runtime DSN; URI or semicolon connection string

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/hermes-agent}"
NON_INTERACTIVE=0
EXPLICIT_PG_MEM_DB_CONN_STR="${PG_MEM_DB_CONN_STR:-}"
while [ $# -gt 0 ]; do
    case "$1" in
        --hermes-home)       HERMES_HOME="$2"; shift 2 ;;
        --non-interactive)   NON_INTERACTIVE=1; shift ;;
        -y|--yes)            NON_INTERACTIVE=1; shift ;;
        -h|--help)
            sed -n '2,/^set -euo pipefail/p' "$0" | head -n -1 | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="$(cd "$PLUGIN_DIR/../../.." && pwd)"

if [ -t 1 ]; then
    BOLD="\033[1m"; DIM="\033[2m"; RESET="\033[0m"
    RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"; BLUE="\033[34m"
else
    BOLD=""; DIM=""; RESET=""; RED=""; GREEN=""; YELLOW=""; BLUE=""
fi
say()    { printf "%b\n" "$*"; }
ok()     { printf "${GREEN}✓${RESET} %b\n" "$*"; }
warn()   { printf "${YELLOW}!${RESET} %b\n" "$*"; }
fail()   { printf "${RED}✗${RESET} %b\n" "$*"; }
header() { printf "\n${BOLD}${BLUE}═══ %s ═══${RESET}\n" "$*"; }

normalize_dsn() {
    python3 - <<'PY'
import os
from urllib.parse import quote
raw = os.environ.get('PG_MEM_DB_CONN_STR', '').strip()
if ';' not in raw or '=' not in raw.split(';', 1)[0]:
    print(raw)
    raise SystemExit
pairs = {}
mapping = {
    'host':'host','server':'host','port':'port','database':'dbname','dbname':'dbname',
    'user':'user','username':'user','userid':'user','uid':'user',
    'password':'password','pwd':'password','sslmode':'sslmode',
}
for part in raw.split(';'):
    if '=' not in part:
        continue
    key, value = part.split('=', 1)
    mapped = mapping.get(key.strip().replace(' ', '').lower())
    if mapped and value.strip():
        pairs[mapped] = value.strip()
missing = {'host', 'dbname', 'user', 'password'} - pairs.keys()
if missing:
    raise SystemExit(f"PG_MEM_DB_CONN_STR semicolon form missing: {', '.join(sorted(missing))}")
def q(value: str) -> str:
    return quote(value, safe='')
port = ':' + pairs['port'] if pairs.get('port') else ''
ssl = '?sslmode=' + q(pairs['sslmode']) if pairs.get('sslmode') else ''
print('postgresql://' + q(pairs['user']) + ':' + q(pairs['password']) + '@' + pairs['host'] + port + '/' + q(pairs['dbname']) + ssl)
PY
}

mask_dsn() {
    python3 - <<'PY'
import os, re
s = os.environ.get('PG_MEM_DB_CONN_STR', '')
s = re.sub(r'(postgres(?:ql)?://[^:/?#]+:)[^@]+(@)', r'\1***\2', s)
s = re.sub(r'(?i)(Password|Pwd)=([^;]+)', r'\1=***', s)
print(s)
PY
}

run_psql() {
    psql "$PG_MEM_DB_CONN_STR" -v ON_ERROR_STOP=1 -X -q -tAc "$1"
}

header "1 / 5  ·  local preflight"

if ! command -v psql >/dev/null 2>&1; then
    fail "psql not found on PATH"
    say "  install postgresql-client/libpq first."
    exit 1
fi
ok "psql $(psql --version | head -1)"

if [ -d "$HERMES_HOME" ] && [ ! -f "$HERMES_HOME/run_agent.py" ] && [ ! -f "$HERMES_HOME/AGENTS.md" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
    HERMES_HOME="$HERMES_HOME/hermes-agent"
fi
if [ ! -d "$HERMES_HOME" ] || [ ! -d "$HERMES_HOME/plugins/memory" ]; then
    fail "no hermes-agent checkout at $HERMES_HOME"
    say "  re-run with: ${BOLD}./bootstrap.sh --hermes-home /path/to/hermes-agent${RESET}"
    exit 1
fi
ok "hermes-agent at $HERMES_HOME"

# Resolve the .env path. In a profile-mode Hermes install, the active
# profile has its own .env at ~/.hermes/profiles/<name>/.env and the
# root ~/.hermes/.env is NOT inherited. HERMES_HOME for a profile is
# ~/.hermes/profiles/<name>; for the root instance it is ~/.hermes.
if [ -d "$HERMES_HOME" ] && [[ "$HERMES_HOME" == */profiles/* ]]; then
    ENV_FILE="$HERMES_HOME/.env"
else
    ENV_FILE="${HERMES_HOME%/hermes-agent}/.env"
    [ -f "$ENV_FILE" ] || ENV_FILE="$HOME/.hermes/.env"
fi
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
    [ -n "$EXPLICIT_PG_MEM_DB_CONN_STR" ] && PG_MEM_DB_CONN_STR="$EXPLICIT_PG_MEM_DB_CONN_STR"
else
    touch "$ENV_FILE"
fi
ok ".env at $ENV_FILE"

PY_BIN=""
for CAND in "$HERMES_HOME/venv/bin/python" python3 python; do
    if command -v "$CAND" >/dev/null 2>&1; then PY_BIN="$CAND"; break; fi
done
[ -z "$PY_BIN" ] && PY_BIN="python3"
ok "python: $PY_BIN ($($PY_BIN --version 2>&1))"

if ! $PY_BIN -c "import psycopg2" 2>/dev/null; then
    warn "psycopg2 not installed in $PY_BIN"
    say "  fix inside the Hermes Python environment before runtime use:"
    say "  ${BOLD}$PY_BIN -m pip install psycopg2-binary${RESET}"
fi

header "2 / 5  ·  runtime DSN"

PG_MEM_DB_CONN_STR="${PG_MEM_DB_CONN_STR:-}"
if [ -z "$PG_MEM_DB_CONN_STR" ]; then
    if [ "$NON_INTERACTIVE" = "1" ]; then
        fail "PG_MEM_DB_CONN_STR is required in non-interactive mode"
        exit 1
    fi
    say "  Enter the pre-provisioned runtime DSN. This must be the non-superuser app role."
    read -r -p "  PG_MEM_DB_CONN_STR: " PG_MEM_DB_CONN_STR
fi
if [ -z "$PG_MEM_DB_CONN_STR" ]; then
    fail "PG_MEM_DB_CONN_STR cannot be empty"
    exit 1
fi
export PG_MEM_DB_CONN_STR
PG_MEM_DB_CONN_STR="$(normalize_dsn)"
export PG_MEM_DB_CONN_STR
ok "PG_MEM_DB_CONN_STR loaded" "$(mask_dsn)"

header "3 / 5  ·  DBA prerequisite verification"

if ! psql "$PG_MEM_DB_CONN_STR" -v ON_ERROR_STOP=1 -X -q -tAc "SELECT 1;" >/dev/null; then
    fail "cannot connect with PG_MEM_DB_CONN_STR"
    say "  Ask the DB admin to create the role/database and provide the final runtime DSN."
    exit 1
fi
ok "runtime role can connect"

CURRENT_USER="$(run_psql "SELECT current_user;" | tr -d ' ')"
IS_SUPER="$(run_psql "SELECT rolsuper FROM pg_roles WHERE rolname = current_user;" | tr -d ' ')"
if [ "$IS_SUPER" = "t" ]; then
    fail "runtime role is superuser"
    say "  Refusing to install against a superuser DSN. Use the dedicated non-superuser app role."
    exit 1
fi
ok "runtime role is non-superuser" "$CURRENT_USER"

if [ "$(run_psql "SELECT 1 FROM pg_extension WHERE extname='vector';" | tr -d ' ')" != "1" ]; then
    fail "pgvector extension missing"
    say "  Prerequisite not met. Have a DB admin run plugins/memory/postgres/sql/000_create_database_and_role.sql"
    say "  or otherwise CREATE EXTENSION vector in the target database. Then re-run this script."
    exit 1
fi
ok "pgvector extension installed"

PUB_OWNER="$(run_psql "SELECT pg_catalog.pg_get_userbyid(n.nspowner) FROM pg_namespace n WHERE n.nspname='public';" | tr -d ' ')"
if [ "$PUB_OWNER" != "$CURRENT_USER" ]; then
    fail "public schema owner is '$PUB_OWNER', not '$CURRENT_USER'"
    say "  Prerequisite not met. The runtime role must own public schema so 000_schema.sql can create tables/indexes."
    say "  DB admin fix: ALTER SCHEMA public OWNER TO $CURRENT_USER; GRANT ALL ON SCHEMA public TO $CURRENT_USER;"
    exit 1
fi
ok "runtime role owns public schema" "$PUB_OWNER"

if ! run_psql "CREATE TABLE IF NOT EXISTS public.__hpm_privilege_probe(id int); DROP TABLE public.__hpm_privilege_probe;" >/dev/null; then
    fail "runtime role cannot create/drop objects in public schema"
    say "  DB admin must fix schema ownership/privileges before the plugin can install its schema."
    exit 1
fi
ok "runtime role can create/drop plugin objects"

header "4 / 5  ·  install plugin + schema"

if grep -q '^PG_MEM_DB_CONN_STR=' "$ENV_FILE" 2>/dev/null; then
    say "  ${DIM}PG_MEM_DB_CONN_STR already exists in $ENV_FILE — not rewriting${RESET}"
else
    printf '\n# postgres memory provider\nPG_MEM_DB_CONN_STR=%s\n# KIMI_API_KEY=sk-...\nHERMES_EMBED_DEFAULT_DIM=1024\nHERMES_EMBED_FAIL_OPEN=1\n' "$PG_MEM_DB_CONN_STR" >> "$ENV_FILE"
    ok "wrote PG_MEM_DB_CONN_STR to $ENV_FILE"
fi

CONFIG_FILE="$HERMES_HOME/config.yaml"
if [ -f "$CONFIG_FILE" ]; then
    if grep -qE '^[[:space:]]*provider:[[:space:]]*postgres' "$CONFIG_FILE"; then
        say "  ${DIM}memory.provider is already postgres${RESET}"
    elif ! grep -qE '^memory:' "$CONFIG_FILE"; then
        printf '\nmemory:\n  memory_enabled: true\n  provider: postgres\n' >> "$CONFIG_FILE"
        ok "added memory block to $CONFIG_FILE"
    else
        warn "memory block exists but provider is not postgres; edit $CONFIG_FILE manually"
    fi
else
    warn "$CONFIG_FILE not found — add memory.provider: postgres by hand"
fi

INSTALL_SH="$REPO_DIR/install.sh"
if [ -f "$INSTALL_SH" ]; then
    HERMES_HOME="$HERMES_HOME" "$INSTALL_SH" --yes
    ok "plugin + skill installed"
else
    fail "missing $INSTALL_SH"
    exit 1
fi

SCHEMA_SQL="$PLUGIN_DIR/sql/000_schema.sql"
if psql "$PG_MEM_DB_CONN_STR" -v ON_ERROR_STOP=1 -X -f "$SCHEMA_SQL" >/dev/null; then
    ok "agent_memory schema installed"
else
    fail "could not install agent_memory schema with runtime role"
    psql "$PG_MEM_DB_CONN_STR" -v ON_ERROR_STOP=1 -X -f "$SCHEMA_SQL" 2>&1 | sed 's/^/    /'
    exit 1
fi

header "5 / 5  ·  final preflight"
DIAGNOSE_SH="$PLUGIN_DIR/scripts/diagnose.sh"
HERMES_HOME="$HERMES_HOME" PG_MEM_DB_CONN_STR="$PG_MEM_DB_CONN_STR" "$DIAGNOSE_SH"

cat <<EOF

${BOLD}${GREEN}════════════════════════════════════════════════════════════════${RESET}
${BOLD}${GREEN}  ✓ postgres memory bootstrap complete${RESET}
${BOLD}${GREEN}════════════════════════════════════════════════════════════════${RESET}

  Before embeddings work, ensure an embedder key exists in $ENV_FILE:

      KIMI_API_KEY=sk-...

  Then restart and verify:

      ${BOLD}hermes gateway restart${RESET}
      ${BOLD}hermes postgres-memory preflight${RESET}
      ${BOLD}hermes postgres-memory status${RESET}
      ${BOLD}hermes postgres-memory model-list${RESET}

EOF
