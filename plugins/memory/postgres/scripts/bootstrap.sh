#!/usr/bin/env bash
# bootstrap.sh — one-shot end-to-end installer for the postgres memory provider.
#
# What it does, in order:
#   1. Sanity checks the environment (psql, hermes-agent checkout, .env)
#   2. Asks the user for DB superuser credentials (interactive)
#   3. Asks the user for the new hermes role's password (interactive)
#   4. Runs 000_create_database_and_role.sql as superuser
#   5. Runs 000_schema.sql as the new hermes role
#   6. Installs the plugin + skill via install.sh
#   7. Runs diagnose.sh to confirm everything is wired up
#   8. Prints the exact commands the user needs to run by hand
#      (e.g. adding API keys to .env, restarting the gateway)
#
# Idempotent: re-running is safe. Every step is either a CREATE ... IF
# NOT EXISTS, a copy, or a probe.
#
# Usage:
#   ./bootstrap.sh                           # interactive
#   ./bootstrap.sh --non-interactive         # all prompts must be set via env
#   ./bootstrap.sh --hermes-home /path/to    # override HERMES_HOME
#
# Env vars for non-interactive mode:
#   HERMES_HOME                  (default: ~/.hermes/hermes-agent)
#   POSTGRES_SUPERUSER           (default: postgres)
#   POSTGRES_SUPERUSER_PASSWORD  (required in non-interactive)
#   PG_SUPER_HOST                (default: localhost)
#   PG_SUPER_PORT                (default: 5432)
#   NEW_DB_NAME                  (default: hermes)
#   NEW_ROLE_NAME                (default: hermes)
#   NEW_ROLE_PASSWORD            (required in non-interactive; refused empty otherwise)
#   ALLOW_WEAK_PW                (set to 'on' to allow empty password for dev)

set -uo pipefail

# ─── args ───────────────────────────────────────────────────────────────

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/hermes-agent}"
NON_INTERACTIVE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --hermes-home)       HERMES_HOME="$2"; shift 2 ;;
        --non-interactive)   NON_INTERACTIVE=1; shift ;;
        -y|--yes)            NON_INTERACTIVE=1; shift ;;
        -h|--help)
            sed -n '2,/^set -uo pipefail/p' "$0" | head -n -1 | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# Layout: scripts/ → postgres/ → memory/ → plugins/ → <repo root>.
# So we go up 4 levels from SCRIPT_DIR (or equivalently 3 from PLUGIN_DIR).
REPO_DIR="$(cd "$PLUGIN_DIR/../../.." && pwd)"

# ─── color helpers (auto-disable if no tty) ─────────────────────────────

if [ -t 1 ]; then
    BOLD="\033[1m"; DIM="\033[2m"; RESET="\033[0m"
    RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"; BLUE="\033[34m"
else
    BOLD=""; DIM=""; RESET=""
    RED=""; GREEN=""; YELLOW=""; BLUE=""
fi

say()    { printf "%b\n" "$*"; }
ok()     { printf "${GREEN}✓${RESET} %b\n" "$*"; }
warn()   { printf "${YELLOW}!${RESET} %b\n" "$*"; }
fail()   { printf "${RED}✗${RESET} %b\n" "$*"; }
header() {
    printf "\n${BOLD}${BLUE}═══ %s ═══${RESET}\n" "$*"
}

# ─── prompt helper (skipped in non-interactive) ────────────────────────

prompt() {
    local VAR="$1"; local PROMPT="$2"; local DEFAULT="${3:-}"; local SECRET="${4:-}"
    if [ -n "${!VAR:-}" ]; then
        return 0
    fi
    if [ "$NON_INTERACTIVE" = "1" ]; then
        if [ -n "$DEFAULT" ]; then
            printf -v "$VAR" '%s' "$DEFAULT"
            export "$VAR"
            return 0
        fi
        fail "non-interactive mode but $VAR is unset and has no default"
        exit 1
    fi
    local INPUT=""
    if [ "$SECRET" = "secret" ]; then
        read -r -s -p "$(printf '%b' "$PROMPT")" INPUT
        echo
    else
        if [ -n "$DEFAULT" ]; then
            read -r -p "$(printf '%b' "$PROMPT [$DEFAULT] ")" INPUT
            INPUT="${INPUT:-$DEFAULT}"
        else
            read -r -p "$(printf '%b' "$PROMPT")" INPUT
        fi
    fi
    printf -v "$VAR" '%s' "$INPUT"
    export "$VAR"
}

# ─── 1. preflight ──────────────────────────────────────────────────────

header "1 / 6  ·  preflight"

# 1a. psql on PATH
if ! command -v psql >/dev/null 2>&1; then
    fail "psql not found on PATH"
    say "  install: ${BOLD}apt install postgresql-client${RESET} (Debian/Ubuntu)"
    say "           ${BOLD}brew install libpq && echo 'export PATH=\"\$(brew --prefix libpq)/bin:\$PATH\"' >> ~/.zshrc${RESET} (macOS)"
    say "           ${BOLD}pacman -S postgresql-libs${RESET} (Arch)"
    exit 1
fi
ok "psql $(psql --version | head -1)"

# 1b. hermes-agent checkout
if [ ! -d "$HERMES_HOME/plugins/memory" ]; then
    fail "no hermes-agent checkout at $HERMES_HOME"
    say "  re-run with: ${BOLD}./bootstrap.sh --hermes-home /path/to/hermes-agent${RESET}"
    say "  or set:      ${BOLD}export HERMES_HOME=/path/to/hermes-agent${RESET}"
    exit 1
fi
ok "hermes-agent at $HERMES_HOME"

# 1c. .env exists
ENV_FILE="$(dirname "$HERMES_HOME")/.env"
[ -f "$ENV_FILE" ] || ENV_FILE="$HOME/.hermes/.env"
if [ ! -f "$ENV_FILE" ]; then
    warn ".env not found at $ENV_FILE — will use defaults for connection details"
    touch "$ENV_FILE"
    say "  created empty .env — you can fill it in after this script finishes"
fi
ok ".env at $ENV_FILE"

# 1d. python for the plugin
PY_BIN=""
for CAND in "$HERMES_HOME/venv/bin/python" python3 python; do
    if command -v "$CAND" >/dev/null 2>&1; then
        PY_BIN="$CAND"
        break
    fi
done
[ -z "$PY_BIN" ] && PY_BIN="python3"
ok "python: $PY_BIN ($($PY_BIN --version 2>&1))"

# 1e. psycopg2
if ! $PY_BIN -c "import psycopg2" 2>/dev/null; then
    warn "psycopg2 not installed in $PY_BIN"
    say "  the plugin will fail at import time until this is fixed."
    say "  fix: ${BOLD}$PY_BIN -m pip install psycopg2-binary${RESET}"
    if [ "$NON_INTERACTIVE" != "1" ]; then
        read -r -p "  install it now? [Y/n] " INSTALL_PSYCOPG2
        INSTALL_PSYCOPG2="${INSTALL_PSYCOPG2:-Y}"
    else
        INSTALL_PSYCOPG2="Y"
    fi
    if [[ "$INSTALL_PSYCOPG2" =~ ^[Yy]?$ ]]; then
        $PY_BIN -m pip install --quiet psycopg2-binary
        ok "psycopg2 installed"
    else
        warn "skipping psycopg2 install — plugin will be broken until you install it"
    fi
fi

# ─── 2. collect superuser credentials ──────────────────────────────────

header "2 / 6  ·  database superuser credentials"

# Superuser connection details are bootstrap-only. The plugin runtime uses only PG_MEM_DB_CONN_STR.
: "${PG_SUPER_HOST:=localhost}"
: "${PG_SUPER_PORT:=5432}"
: "${POSTGRES_SUPERUSER:=postgres}"

if [ "$NON_INTERACTIVE" != "1" ]; then
    say "  ${DIM}enter the connection details for the postgres SUPERUSER${RESET}"
    say "  ${DIM}(typically 'postgres' — the role that was created at initdb time)${RESET}"
    say
fi

prompt PG_SUPER_HOST     "  superuser host: "     "$PG_SUPER_HOST"
prompt PG_SUPER_PORT     "  superuser port: "     "$PG_SUPER_PORT"
prompt POSTGRES_SUPERUSER "  superuser role: "     "$POSTGRES_SUPERUSER"
prompt POSTGRES_SUPERUSER_PASSWORD "  superuser password: " "" "secret"

# Test connection as superuser
if ! PGPASSWORD="$POSTGRES_SUPERUSER_PASSWORD" psql \
        -h "$PG_SUPER_HOST" -p "$PG_SUPER_PORT" -U "$POSTGRES_SUPERUSER" -d postgres \
        -tAc "SELECT 1;" >/dev/null 2>&1; then
    fail "could not connect to postgres as $POSTGRES_SUPERUSER@$PG_SUPER_HOST:$PG_SUPER_PORT"
    say "  double-check the host/port/user/password. the role must be a SUPERUSER (or have CREATEROLE + CREATEDB)."
    exit 1
fi
ok "connected to postgres as $POSTGRES_SUPERUSER"

# Verify the user really is a superuser
IS_SUPER=$(PGPASSWORD="$POSTGRES_SUPERUSER_PASSWORD" psql \
    -h "$PG_SUPER_HOST" -p "$PG_SUPER_PORT" -U "$POSTGRES_SUPERUSER" -d postgres \
    -tAc "SELECT rolsuper FROM pg_roles WHERE rolname='$POSTGRES_SUPERUSER';" | tr -d ' ')
if [ "$IS_SUPER" != "t" ]; then
    fail "role $POSTGRES_SUPERUSER is not a SUPERUSER"
    say "  this script needs to CREATE ROLE + CREATE DATABASE + CREATE EXTENSION, all of which require superuser."
    say "  re-run as the 'postgres' role, or grant the current role SUPERUSER (then revoke after)."
    exit 1
fi
ok "role $POSTGRES_SUPERUSER is a superuser"

# ─── 3. collect new role + database config ─────────────────────────────

header "3 / 6  ·  new role + database"

: "${NEW_DB_NAME:=hermes}"
: "${NEW_ROLE_NAME:=hermes}"

if [ "$NON_INTERACTIVE" != "1" ]; then
    say "  ${DIM}this script will create a dedicated application role + database for the plugin.${RESET}"
    say "  ${DIM}defaults: role 'hermes', database 'hermes'. press enter to accept.${RESET}"
    say
fi

prompt NEW_DB_NAME       "  new database name: "         "$NEW_DB_NAME"
prompt NEW_ROLE_NAME     "  new application role name: " "$NEW_ROLE_NAME"
prompt NEW_ROLE_PASSWORD "  new role password: "        "" "secret"

if [ -z "$NEW_ROLE_PASSWORD" ] && [ "${ALLOW_WEAK_PW:-}" != "on" ]; then
    fail "refusing to create a role with no password"
    say "  set --non-interactive + NEW_ROLE_PASSWORD, or run interactively and type one."
    say "  or set ALLOW_WEAK_PW=on to allow empty passwords (NOT recommended, even for dev)."
    exit 1
fi

# Confirm the choice
if [ "$NON_INTERACTIVE" != "1" ]; then
    echo
    say "  ${BOLD}about to create:${RESET}"
    say "    database:  $NEW_DB_NAME"
    say "    role:      $NEW_ROLE_NAME"
    say "    password:  ${DIM}*** ($([ -n "$NEW_ROLE_PASSWORD" ] && echo "${#NEW_ROLE_PASSWORD} chars" || echo "empty — trust auth only"))${RESET}"
    say "    connlimit: 20"
    read -r -p "  proceed? [Y/n] " CONFIRM
    CONFIRM="${CONFIRM:-Y}"
    if [[ ! "$CONFIRM" =~ ^[Yy]?$ ]]; then
        say "  aborted."
        exit 0
    fi
fi

# ─── 4. run the database bootstrap ─────────────────────────────────────

header "4 / 6  ·  creating database + role + extensions"

CREATE_SQL="$PLUGIN_DIR/sql/000_create_database_and_role.sql"
if [ ! -f "$CREATE_SQL" ]; then
    fail "missing $CREATE_SQL"
    exit 1
fi

# Build the psql command. Use -v to pass GUCs through to the script.
PSQL_CMD=(psql
    -h "$PG_SUPER_HOST"
    -p "$PG_SUPER_PORT"
    -U "$POSTGRES_SUPERUSER"
    -d postgres
    -v "dbname=$NEW_DB_NAME"
    -v "rolename=$NEW_ROLE_NAME"
    -v "pw=$NEW_ROLE_PASSWORD"
    -v "connlimit=20"
    --set=ON_ERROR_STOP=on
    -f "$CREATE_SQL"
)

if [ "${ALLOW_WEAK_PW:-}" = "on" ]; then
    PSQL_CMD+=( -v "allow_weak_pw=on" )
fi

say "  ${DIM}running:${RESET}"
printf "    PGPASSWORD=*** psql -h %s -U %s -d postgres -v dbname=%s -v rolename=%s -f %s\n" \
    "$PG_SUPER_HOST" "$POSTGRES_SUPERUSER" "$NEW_DB_NAME" "$NEW_ROLE_NAME" "$CREATE_SQL"
echo

if ! PGPASSWORD="$POSTGRES_SUPERUSER_PASSWORD" "${PSQL_CMD[@]}"; then
    fail "bootstrap SQL failed"
    say "  see the error above. the script is idempotent — fix the issue and re-run this bootstrap.sh."
    exit 1
fi
ok "database + role + extensions created"

# Verify we can actually connect as the new role
if ! PGPASSWORD="$NEW_ROLE_PASSWORD" psql \
        -h "$PG_SUPER_HOST" -p "$PG_SUPER_PORT" -U "$NEW_ROLE_NAME" -d "$NEW_DB_NAME" \
        -tAc "SELECT extname FROM pg_extension WHERE extname='vector';" 2>&1 | grep -q vector; then
    fail "could not verify pgvector as $NEW_ROLE_NAME"
    say "  the extension may not have been installed. check: PGPASSWORD=*** psql -U $NEW_ROLE_NAME -d $NEW_DB_NAME -c '\\dx'"
    exit 1
fi
ok "$NEW_ROLE_NAME can connect + pgvector is visible"

# ─── 5. install plugin + skill ─────────────────────────────────────────

header "5 / 6  ·  installing plugin + skill"

# Write a fresh .env block for the plugin (only if not already set).
ENC_ROLE=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$NEW_ROLE_NAME")
ENC_PW=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$NEW_ROLE_PASSWORD")
PG_MEM_BOOTSTRAP_DSN="postgresql://$ENC_ROLE:$ENC_PW@$PG_SUPER_HOST:$PG_SUPER_PORT/$NEW_DB_NAME"
ENV_BLOCK="
# ─── postgres memory provider (added by hermes-postgres-memory bootstrap) ───
PG_MEM_DB_CONN_STR=$PG_MEM_BOOTSTRAP_DSN
# KIMI_API_KEY=sk-...         # required for the default 1024-dim embedder
HERMES_EMBED_DEFAULT_DIM=1024
HERMES_EMBED_FAIL_OPEN=1
"

# Append to .env if not already present
if grep -q "^# ─── postgres memory provider" "$ENV_FILE" 2>/dev/null; then
    say "  ${DIM}postgres memory provider block already exists in $ENV_FILE — not duplicating${RESET}"
else
    printf '%s\n' "$ENV_BLOCK" >> "$ENV_FILE"
    ok "wrote $ENV_FILE (add KIMI_API_KEY before restarting the gateway)"
fi

# Switch config.yaml to use the plugin
CONFIG_FILE="$HERMES_HOME/config.yaml"
if [ -f "$CONFIG_FILE" ]; then
    if grep -qE "^[[:space:]]*provider:[[:space:]]*postgres" "$CONFIG_FILE"; then
        say "  ${DIM}memory.provider is already 'postgres' in config.yaml${RESET}"
    else
        # Insert memory block if missing
        if ! grep -qE "^memory:" "$CONFIG_FILE"; then
            printf "\nmemory:\n  memory_enabled: true\n  provider: postgres\n" >> "$CONFIG_FILE"
            ok "added memory block to $CONFIG_FILE"
        else
            warn "memory block exists in config.yaml but provider is not 'postgres'"
            say "  ${DIM}manually edit $CONFIG_FILE and set memory.provider: postgres${RESET}"
        fi
    fi
else
    warn "$CONFIG_FILE not found — you'll need to add the memory block by hand"
fi

# Run install.sh for the plugin + skill
INSTALL_SH="$REPO_DIR/install.sh"
if [ -f "$INSTALL_SH" ]; then
    if HERMES_HOME="$HERMES_HOME" "$INSTALL_SH"; then
        ok "plugin + skill installed into $HERMES_HOME"
    else
        fail "install.sh failed"
        exit 1
    fi
else
    warn "$INSTALL_SH not found — copy the plugin + skill files by hand"
fi

# ─── 6. run the plugin schema install + preflight ─────────────────────

header "6 / 6  ·  plugin schema + final preflight"

# Run the plugin's 000_schema.sql (creates agent_memory + settings + models)
SCHEMA_SQL="$PLUGIN_DIR/sql/000_schema.sql"
if [ -f "$SCHEMA_SQL" ]; then
    if psql "$PG_MEM_BOOTSTRAP_DSN" -f "$SCHEMA_SQL" >/dev/null 2>&1; then
        ok "agent_memory schema installed"
    else
        fail "could not install agent_memory schema"
        psql "$PG_MEM_BOOTSTRAP_DSN" -f "$SCHEMA_SQL" 2>&1 | sed 's/^/    /'
        exit 1
    fi
fi

# Run the plugin's preflight
DIAGNOSE_SH="$PLUGIN_DIR/scripts/diagnose.sh"
if [ -f "$DIAGNOSE_SH" ]; then
    echo
    HERMES_HOME="$HERMES_HOME" "$DIAGNOSE_SH"
fi

# ─── done ──────────────────────────────────────────────────────────────

cat <<EOF

${BOLD}${GREEN}════════════════════════════════════════════════════════════════${RESET}
${BOLD}${GREEN}  ✓ bootstrap complete${RESET}
${BOLD}${GREEN}════════════════════════════════════════════════════════════════${RESET}

  ${BOLD}before the plugin can actually embed, you still need to:${RESET}

  1. add an embedder API key to $ENV_FILE

       KIMI_API_KEY=sk-...    # https://platform.moonshot.cn (free, 1024-dim)

     (alternatively, OLLAMA_API_KEY for 768-dim, or OPENAI_API_KEY for 1536-dim)

  2. restart the gateway so the new .env + plugin take effect:

       ${BOLD}hermes gateway restart${RESET}

  3. confirm:

       ${BOLD}hermes postgres-memory preflight${RESET}
       ${BOLD}hermes postgres-memory status${RESET}
       ${BOLD}hermes postgres-memory model-list${RESET}

  4. smoke test in a fresh hermes session:

       pg_remember(content="postgres plugin is live", category="fact")
       pg_search(query="postgres plugin")

  ${BOLD}${DIM}if anything looks off, re-run:${RESET} ${BOLD}$SCRIPT_DIR/diagnose.sh${RESET}

EOF
