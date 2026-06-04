#!/usr/bin/env bash
# diagnose.sh — preflight checker for the postgres kanban provider.
# Verifies the connection, schema, and required tables are all in
# place before the runtime tries to use them.
#
# Usage:
#   ./diagnose.sh [--hermes-home PATH] [--json]
#
# Required config:
#   PG_MEM_DB_CONN_STR    PostgreSQL libpq DSN for the Hermes memory DB
#                        (kanban reuses the same connection string)

set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/hermes-agent}"
JSON=0
EXPLICIT_PG_MEM_DB_CONN_STR="${PG_MEM_DB_CONN_STR:-}"
HERMES_KANBAN_SCHEMA="${HERMES_KANBAN_SCHEMA:-hermes_kanban}"
while [ $# -gt 0 ]; do
    case "$1" in
        --hermes-home) HERMES_HOME="$2"; shift 2 ;;
        --json) JSON=1; shift ;;
        -h|--help)
            cat <<'EOF'
diagnose.sh — preflight checker for the postgres kanban provider.

USAGE:
  ./diagnose.sh [--hermes-home PATH] [--json]

REQUIRED CONFIG:
  PG_MEM_DB_CONN_STR    PostgreSQL libpq DSN (kanban reuses the memory DSN)
EOF
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Single helper for all failure paths
_fail() {
    local msg="$1"
    if [ $JSON -eq 1 ]; then
        printf '{"ok": false, "error": "%s"}\n' "$msg"
    else
        echo "✗ $msg" >&2
    fi
    exit 1
}

if [ -d "$HERMES_HOME" ] && [ ! -f "$HERMES_HOME/run_agent.py" ] && [ ! -f "$HERMES_HOME/AGENTS.md" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
    HERMES_HOME="$HERMES_HOME/hermes-agent"
fi
if [ -d "$HERMES_HOME" ] && [ ! -d "$HERMES_HOME/plugins" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
    HERMES_HOME="$HERMES_HOME/hermes-agent"
fi

# Check hermes-agent checkout
if [ ! -d "$HERMES_HOME" ]; then
    _fail "HERMES_HOME does not exist: $HERMES_HOME"
fi
if [ ! -d "$HERMES_HOME/plugins" ]; then
    _fail "Plugins dir not found: $HERMES_HOME/plugins"
fi

# Check PG_MEM_DB_CONN_STR
if [ -z "$EXPLICIT_PG_MEM_DB_CONN_STR" ]; then
    if [ -f "$HOME/.hermes/.env" ]; then
        if grep -q "^PG_MEM_DB_CONN_STR" "$HOME/.hermes/.env" 2>/dev/null; then
            EXPLICIT_PG_MEM_DB_CONN_STR=$(grep "^PG_MEM_DB_CONN_STR" "$HOME/.hermes/.env" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
        fi
    fi
fi
if [ -z "$EXPLICIT_PG_MEM_DB_CONN_STR" ]; then
    _fail "PG_MEM_DB_CONN_STR not set in env or ~/.hermes/.env"
fi

# Check psql
if ! command -v psql >/dev/null 2>&1; then
    _fail "psql not found in PATH"
fi

# Check connectivity (suppress password prompts; assume DSN is complete)
if ! psql "$EXPLICIT_PG_MEM_DB_CONN_STR" -c "SELECT 1" -t -A >/dev/null 2>&1; then
    _fail "Cannot connect to Postgres with PG_MEM_DB_CONN_STR"
fi

# Check schema + tables
SCHEMA_EXISTS=$(psql "$EXPLICIT_PG_MEM_DB_CONN_STR" -t -A -c \
    "SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = '$HERMES_KANBAN_SCHEMA')")
if [ "$SCHEMA_EXISTS" != "t" ]; then
    _fail "Schema $HERMES_KANBAN_SCHEMA does not exist. Run the hermes-postgres-memory bootstrap to install it."
fi

# Check all 8 tables
REQUIRED_TABLES="tenants tasks task_runs task_events task_links task_comments task_attachments notify_subs"
MISSING=""
for T in $REQUIRED_TABLES; do
    EXISTS=$(psql "$EXPLICIT_PG_MEM_DB_CONN_STR" -t -A -c \
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = '$HERMES_KANBAN_SCHEMA' AND table_name = '$T')")
    if [ "$EXISTS" != "t" ]; then
        MISSING="$MISSING $T"
    fi
done
if [ -n "$MISSING" ]; then
    _fail "Missing tables in $HERMES_KANBAN_SCHEMA:$MISSING"
fi

if [ $JSON -eq 1 ]; then
    cat <<EOF
{"ok": true, "schema": "$HERMES_KANBAN_SCHEMA", "tables": "$REQUIRED_TABLES"}
EOF
else
    echo "✓ hermes-postgres-kanban preflight passed"
    echo "  schema: $HERMES_KANBAN_SCHEMA"
    echo "  tables: $REQUIRED_TABLES"
fi
exit 0
