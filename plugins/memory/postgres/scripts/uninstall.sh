#!/usr/bin/env bash
# uninstall.sh — remove the postgres memory provider cleanly.
#
# Three modes, each requires explicit confirmation:
#   --plugin       : remove plugin + skill files only (safest, no DB)
#   --db           : drop the agent_memory schema and friends from the DB
#   --all          : plugin + skill + DB + .env entries
#
# Removal is staged — every destructive step asks for confirmation, and
# the DB step prints a dry-run summary before it touches anything.
#
# Does NOT remove the role or the database by default (those are usually
# shared with other apps). Use --role and --database for that.
#
# Usage:
#   ./uninstall.sh --plugin
#   ./uninstall.sh --db --yes
#   ./uninstall.sh --all
#   ./uninstall.sh --all --role --database   # also drop the role and DB

set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/hermes-agent}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="$(cd "$PLUGIN_DIR/../.." && pwd)"

DO_PLUGIN=0; DO_DB=0; DO_ALL=0; DO_ROLE=0; DO_DATABASE=0
ASSUME_YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --plugin)    DO_PLUGIN=1; shift ;;
        --db)        DO_DB=1; shift ;;
        --all)       DO_ALL=1; DO_PLUGIN=1; DO_DB=1; shift ;;
        --role)      DO_ROLE=1; shift ;;
        --database)  DO_DATABASE=1; shift ;;
        --yes|-y)    ASSUME_YES=1; shift ;;
        -h|--help)
            cat <<'EOF'
uninstall.sh — remove the postgres memory provider.

USAGE:
  ./uninstall.sh [--plugin] [--db] [--all] [--role] [--database] [--yes]

MODES (at least one required):
  --plugin     remove plugin + skill files from HERMES_HOME
  --db         drop agent_memory, agent_memory_settings, agent_memory_models,
               memory_categories from the database
  --all        both of the above (the safe default for "I want it gone")

EXTRAS (use with --all or --db):
  --role       also drop the application role (e.g. 'hermes')
  --database   also drop the database (e.g. 'hermes')

FLAGS:
  --yes        skip the "are you sure?" prompt for each step
  --hermes-home PATH    override the hermes-agent checkout path

EXAMPLES:
  # remove just the plugin files (DB untouched)
  ./uninstall.sh --plugin

  # remove plugin + drop schema, but keep the role + database around
  ./uninstall.sh --all --yes

  # nuclear: drop everything
  ./uninstall.sh --all --role --database --yes

NOTES:
  - Drops are irreversible. The script asks for confirmation by default.
  - To uninstall the pgvector extension: that's a server-wide change, you
    need to do it by hand: psql -U postgres -c 'DROP EXTENSION vector;'
    (and only after every table that uses it is gone).
  - The .env entries added by bootstrap.sh are NOT removed by --plugin
    or --db. Use --all to also clean them up (interactive).

EOF
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ $DO_PLUGIN -eq 0 ] && [ $DO_DB -eq 0 ]; then
    echo "nothing to do — specify --plugin, --db, or --all. see --help." >&2
    exit 1
fi

# ─── helpers ────────────────────────────────────────────────────────────

if [ -t 1 ]; then
    BOLD="\033[1m"; DIM="\033[2m"; RESET="\033[0m"
    RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"
else
    BOLD=""; DIM=""; RESET=""; RED=""; GREEN=""; YELLOW=""
fi

confirm() {
    local PROMPT="$1"
    if [ $ASSUME_YES -eq 1 ]; then
        return 0
    fi
    local A
    read -r -p "$(printf '%b' "$PROMPT [y/N] ")" A
    [[ "$A" =~ ^[Yy]$ ]]
}

# ─── step 1: plugin files ──────────────────────────────────────────────

if [ $DO_PLUGIN -eq 1 ]; then
    echo
    printf "${BOLD}Removing plugin + skill files${RESET}\n"
    echo

    PLUGIN_DST="$HERMES_HOME/plugins/memory/postgres"
    SKILL_DST="$HERMES_HOME/skills/devops/hermes-postgres-memory"

    [ -d "$PLUGIN_DST" ] && printf "  %s exists\n" "$PLUGIN_DST" || printf "  %s not present (skipping)\n" "$PLUGIN_DST"
    [ -d "$SKILL_DST" ]  && printf "  %s exists\n" "$SKILL_DST"  || printf "  %s not present (skipping)\n" "$SKILL_DST"

    if confirm "  remove these directories?"; then
        [ -d "$PLUGIN_DST" ] && rm -rf "$PLUGIN_DST" && printf "${GREEN}  ✓${RESET} removed %s\n" "$PLUGIN_DST"
        [ -d "$SKILL_DST" ]  && rm -rf "$SKILL_DST"  && printf "${GREEN}  ✓${RESET} removed %s\n" "$SKILL_DST"
    else
        echo "  skipping."
    fi

    # Restore the default memory provider in config.yaml if we set it
    CONFIG_FILE="$HERMES_HOME/config.yaml"
    if [ -f "$CONFIG_FILE" ] && grep -qE "^[[:space:]]*provider:[[:space:]]*postgres" "$CONFIG_FILE"; then
        if confirm "  revert memory.provider in config.yaml back to 'builtin'?"; then
            # Use python for YAML-aware editing; sed is brittle for YAML.
            PY_BIN="$HERMES_HOME/venv/bin/python"
            [ -x "$PY_BIN" ] || PY_BIN="python3"
            if $PY_BIN -c "import yaml" 2>/dev/null; then
                $PY_BIN - "$CONFIG_FILE" <<'PYEOF'
import sys, yaml
path = sys.argv[1]
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
if isinstance(cfg.get("memory"), dict) and cfg["memory"].get("provider") == "postgres":
    cfg["memory"]["provider"] = "builtin"
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
    print(f"  ✓ reverted memory.provider to 'builtin' in {path}")
else:
    print(f"  memory.provider is not 'postgres' in {path}, leaving alone")
PYEOF
            else
                # Fall back to a sed-ish edit, but only on the simple case
                sed -i.bak 's/^[[:space:]]*provider:[[:space:]]*postgres[[:space:]]*$/  provider: builtin/' "$CONFIG_FILE"
                rm -f "$CONFIG_FILE.bak"
                printf "${YELLOW}  !${RESET} used sed (no pyyaml available) — verify %s\n" "$CONFIG_FILE"
            fi
        else
            echo "  leaving config.yaml alone."
        fi
    fi

    # Clean .env block
    ENV_FILE="$(dirname "$HERMES_HOME")/.env"
    [ -f "$ENV_FILE" ] || ENV_FILE="$HOME/.hermes/.env"
    if [ -f "$ENV_FILE" ] && grep -q "^# ─── postgres memory provider" "$ENV_FILE"; then
        if confirm "  remove the postgres memory provider block from $ENV_FILE?"; then
            # Remove from the marker line to the next blank line
            $PY_BIN - "$ENV_FILE" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    lines = f.readlines()
out = []
i = 0
while i < len(lines):
    if lines[i].startswith("# ─── postgres memory provider"):
        # Skip the marker line + the next contiguous non-blank lines
        i += 1
        while i < len(lines) and lines[i].strip() != "":
            i += 1
        # Skip the blank line too
        if i < len(lines) and lines[i].strip() == "":
            i += 1
    else:
        out.append(lines[i])
        i += 1
with open(path, "w") as f:
    f.writelines(out)
print(f"  ✓ cleaned postgres block from {path}")
PYEOF
        else
            echo "  leaving .env alone."
        fi
    fi
fi

# ─── step 2: drop DB objects ───────────────────────────────────────────

if [ $DO_DB -eq 1 ]; then
    echo
    printf "${BOLD}Dropping database objects${RESET}\n"
    echo

    # Load .env
    ENV_FILE="$(dirname "$HERMES_HOME")/.env"
    [ -f "$ENV_FILE" ] || ENV_FILE="$HOME/.hermes/.env"
    if [ -f "$ENV_FILE" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$ENV_FILE"
        set +a
    fi

    : "${POSTGRES_HOST:=localhost}"
    : "${POSTGRES_PORT:=5432}"
    : "${POSTGRES_USER:=hermes}"
    : "${POSTGRES_DATABASE:=hermes}"

    if [ -z "${POSTGRES_PASSWORD:-}" ]; then
        printf "${RED}✗${RESET} POSTGRES_PASSWORD is not set. cannot connect.\n"
        exit 1
    fi

    if ! PGPASSWORD="$POS...RD" psql \
            -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" \
            -tAc "SELECT 1;" >/dev/null 2>&1; then
        printf "${RED}✗${RESET} could not connect as $POSTGRES_USER@$POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DATABASE\n"
        exit 1
    fi

    # Discover what exists
    OBJECTS=$(PGPASSWORD="$POS...RD" psql \
        -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" \
        -tAc "SELECT string_agg(table_name, ', ' ORDER BY table_name) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('agent_memory', 'agent_memory_settings', 'agent_memory_models', 'memory_categories');" \
        2>/dev/null | tr -d ' ')

    if [ -z "$OBJECTS" ]; then
        echo "  no plugin tables found in $POSTGRES_DATABASE. nothing to drop."
    else
        echo "  the following plugin objects exist in $POSTGRES_DATABASE:"
        echo "    $OBJECTS"
        echo
        echo "  ${BOLD}DROPPING THESE IS IRREVERSIBLE.${RESET} all memories stored by the"
        echo "  plugin will be permanently deleted."
        echo
        if confirm "  drop these tables and their indexes?"; then
            PGPASSWORD="$POS...RD" psql \
                -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" \
                <<EOF
DROP TABLE IF EXISTS agent_memory CASCADE;
DROP TABLE IF EXISTS agent_memory_settings CASCADE;
DROP TABLE IF EXISTS agent_memory_models CASCADE;
DROP TABLE IF EXISTS memory_categories CASCADE;
EOF
            printf "${GREEN}  ✓${RESET} dropped plugin tables\n"
        else
            echo "  skipping."
        fi
    fi

    # Optional: drop role + database (only with explicit flag)
    if [ $DO_ROLE -eq 1 ]; then
        if confirm "  drop the application role '$POSTGRES_USER'? (requires connecting as a superuser)"; then
            : "${POSTGRES_SUPERUSER:=postgres}"
            if [ -z "${POSTGRES_SUPERUSER_PASSWORD:-}" ]; then
                # Try reading from .env
                if [ -f "$ENV_FILE" ] && grep -qE "^POSTGRES_SUPERUSER_PASSWORD=" "$ENV_FILE"; then
                    set -a; . "$ENV_FILE"; set +a
                fi
            fi
            if [ -z "${POSTGRES_SUPERUSER_PASSWORD:-}" ]; then
                printf "${YELLOW}!${RESET} need POSTGRES_SUPERUSER_PASSWORD to drop a role. skipping.\n"
            else
                PGPASSWORD="$POS...RD" psql \
                    -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_SUPERUSER" -d postgres \
                    -c "DROP ROLE IF EXISTS $POSTGRES_USER;"
                printf "${GREEN}  ✓${RESET} dropped role\n"
            fi
        fi
    fi

    if [ $DO_DATABASE -eq 1 ]; then
        if confirm "  drop the database '$POSTGRES_DATABASE'? (requires connecting as a superuser)"; then
            : "${POSTGRES_SUPERUSER:=postgres}"
            if [ -z "${POSTGRES_SUPERUSER_PASSWORD:-}" ]; then
                if [ -f "$ENV_FILE" ] && grep -qE "^POSTGRES_SUPERUSER_PASSWORD=" "$ENV_FILE"; then
                    set -a; . "$ENV_FILE"; set +a
                fi
            fi
            if [ -z "${POSTGRES_SUPERUSER_PASSWORD:-}" ]; then
                printf "${YELLOW}!${RESET} need POSTGRES_SUPERUSER_PASSWORD to drop a database. skipping.\n"
            else
                PGPASSWORD="$POS...RD" psql \
                    -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_SUPERUSER" -d postgres \
                    -c "DROP DATABASE IF EXISTS $POSTGRES_DATABASE;"
                printf "${GREEN}  ✓${RESET} dropped database\n"
            fi
        fi
    fi
fi

# ─── done ──────────────────────────────────────────────────────────────

echo
printf "${GREEN}✓${RESET} uninstall complete. you may want to ${BOLD}hermes gateway restart${RESET} now.\n"
