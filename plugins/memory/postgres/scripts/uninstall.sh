#!/usr/bin/env bash
# uninstall.sh — remove the postgres memory provider cleanly.

set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/hermes-agent}"

if [ -d "$HERMES_HOME" ] && [ ! -f "$HERMES_HOME/run_agent.py" ] && [ ! -f "$HERMES_HOME/AGENTS.md" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
    HERMES_HOME="$HERMES_HOME/hermes-agent"
fi
if [ ! -d "$HERMES_HOME" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
    HERMES_HOME="$HERMES_HOME/hermes-agent"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DO_PLUGIN=0; DO_DB=0; DO_ROLE=0; DO_DATABASE=0; ASSUME_YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --plugin) DO_PLUGIN=1; shift ;;
        --db) DO_DB=1; shift ;;
        --all) DO_PLUGIN=1; DO_DB=1; shift ;;
        --role) DO_ROLE=1; shift ;;
        --database) DO_DATABASE=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help)
            cat <<'EOF'
uninstall.sh — remove the postgres memory provider.

USAGE:
  ./uninstall.sh [--plugin] [--db] [--all] [--role] [--database] [--yes]

MODES:
  --plugin     remove plugin + skill files from HERMES_HOME
  --db         drop plugin tables using PG_MEM_DB_CONN_STR
  --all        plugin + DB tables + .env block cleanup

EXTRAS:
  --role       also drop the application role; requires PG_SUPER_DSN
  --database   also drop the database; requires PG_SUPER_DSN

REQUIRED FOR DB MODE:
  PG_MEM_DB_CONN_STR in ~/.hermes/.env or current environment

REQUIRED FOR --role / --database:
  PG_SUPER_DSN, e.g. postgresql://postgres:***@host:5432/postgres
EOF
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ $DO_PLUGIN -eq 0 ] && [ $DO_DB -eq 0 ]; then
    echo "nothing to do — specify --plugin, --db, or --all" >&2
    exit 1
fi

if [ -t 1 ]; then
    BOLD="\033[1m"; RESET="\033[0m"; RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"
else
    BOLD=""; RESET=""; RED=""; GREEN=""; YELLOW=""
fi

confirm() {
    local prompt="$1"
    [ $ASSUME_YES -eq 1 ] && return 0
    local answer
    read -r -p "$(printf '%b' "$prompt [y/N] ")" answer
    [[ "$answer" =~ ^[Yy]$ ]]
}

load_env() {
    ENV_FILE="$(dirname "$HERMES_HOME")/.env"
    [ -f "$ENV_FILE" ] || ENV_FILE="$HOME/.hermes/.env"
    if [ -f "$ENV_FILE" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$ENV_FILE"
        set +a
    fi
}

if [ $DO_PLUGIN -eq 1 ]; then
    echo
    printf "${BOLD}Removing plugin + skill files${RESET}\n"
    PLUGIN_DST="$HERMES_HOME/plugins/memory/postgres"
    SKILL_DST="$HERMES_HOME/skills/devops/hermes-postgres-memory"
    [ -d "$PLUGIN_DST" ] && echo "  $PLUGIN_DST exists" || echo "  $PLUGIN_DST not present"
    [ -d "$SKILL_DST" ] && echo "  $SKILL_DST exists" || echo "  $SKILL_DST not present"

    if confirm "  remove these directories?"; then
        [ -d "$PLUGIN_DST" ] && rm -rf "$PLUGIN_DST" && printf "${GREEN}  ✓${RESET} removed %s\n" "$PLUGIN_DST"
        [ -d "$SKILL_DST" ] && rm -rf "$SKILL_DST" && printf "${GREEN}  ✓${RESET} removed %s\n" "$SKILL_DST"
    fi

    CONFIG_FILE="$HERMES_HOME/config.yaml"
    if [ -f "$CONFIG_FILE" ] && grep -qE "^[[:space:]]*provider:[[:space:]]*postgres" "$CONFIG_FILE"; then
        if confirm "  revert memory.provider in config.yaml back to builtin?"; then
            python3 - "$CONFIG_FILE" <<'PY'
import sys, yaml
path = sys.argv[1]
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
if isinstance(cfg.get("memory"), dict) and cfg["memory"].get("provider") == "postgres":
    cfg["memory"]["provider"] = "builtin"
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
    print(f"  ✓ reverted memory.provider to builtin in {path}")
PY
        fi
    fi

    load_env
    if [ -f "$ENV_FILE" ] && grep -q "^# ─── postgres memory provider" "$ENV_FILE"; then
        if confirm "  remove postgres memory provider block from $ENV_FILE?"; then
            python3 - "$ENV_FILE" <<'PY'
import sys
path = sys.argv[1]
lines = open(path).readlines()
out = []
i = 0
while i < len(lines):
    if lines[i].startswith("# ─── postgres memory provider"):
        i += 1
        while i < len(lines) and lines[i].strip():
            i += 1
        if i < len(lines) and not lines[i].strip():
            i += 1
    else:
        out.append(lines[i]); i += 1
open(path, "w").writelines(out)
print(f"  ✓ cleaned {path}")
PY
        fi
    fi
fi

if [ $DO_DB -eq 1 ]; then
    echo
    printf "${BOLD}Dropping database objects${RESET}\n"
    load_env
    if [ -z "${PG_MEM_DB_CONN_STR:-}" ]; then
        printf "${RED}✗${RESET} PG_MEM_DB_CONN_STR is required for --db\n" >&2
        exit 1
    fi
    if ! psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT 1;" >/dev/null 2>&1; then
        printf "${RED}✗${RESET} could not connect via PG_MEM_DB_CONN_STR\n" >&2
        exit 1
    fi

    OBJECTS=$(psql "$PG_MEM_DB_CONN_STR" -tAc "SELECT string_agg(table_name, ', ' ORDER BY table_name) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('agent_memory', 'agent_memory_settings', 'agent_memory_models', 'memory_categories');" 2>/dev/null | tr -d ' ')
    if [ -z "$OBJECTS" ]; then
        echo "  no plugin tables found"
    else
        echo "  plugin tables: $OBJECTS"
        echo "  ${BOLD}DROPPING THESE IS IRREVERSIBLE.${RESET}"
        if confirm "  drop these tables and their indexes?"; then
            psql "$PG_MEM_DB_CONN_STR" <<'SQL'
DROP TABLE IF EXISTS agent_memory CASCADE;
DROP TABLE IF EXISTS agent_memory_settings CASCADE;
DROP TABLE IF EXISTS agent_memory_models CASCADE;
DROP TABLE IF EXISTS memory_categories CASCADE;
SQL
            printf "${GREEN}  ✓${RESET} dropped plugin tables\n"
        fi
    fi

    if [ $DO_ROLE -eq 1 ] || [ $DO_DATABASE -eq 1 ]; then
        if [ -z "${PG_SUPER_DSN:-}" ]; then
            printf "${YELLOW}!${RESET} PG_SUPER_DSN required for --role/--database; skipping those extras.\n"
        else
            if [ $DO_ROLE -eq 1 ]; then
                ROLE_NAME=$(python3 - <<'PY'
import os, urllib.parse
u = urllib.parse.urlparse(os.environ["PG_MEM_DB_CONN_STR"])
print(urllib.parse.unquote(u.username or ""))
PY
)
                if [ -n "$ROLE_NAME" ] && confirm "  drop role '$ROLE_NAME'?"; then
                    psql "$PG_SUPER_DSN" -c "DROP ROLE IF EXISTS \"$ROLE_NAME\";"
                fi
            fi
            if [ $DO_DATABASE -eq 1 ]; then
                DB_NAME=$(python3 - <<'PY'
import os, urllib.parse
u = urllib.parse.urlparse(os.environ["PG_MEM_DB_CONN_STR"])
print((u.path or "/").lstrip("/"))
PY
)
                if [ -n "$DB_NAME" ] && confirm "  drop database '$DB_NAME'?"; then
                    psql "$PG_SUPER_DSN" -c "DROP DATABASE IF EXISTS \"$DB_NAME\";"
                fi
            fi
        fi
    fi
fi

echo
printf "${GREEN}✓${RESET} uninstall complete. Restart Hermes if it was running.\n"
