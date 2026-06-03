#!/usr/bin/env bash
# diagnose.sh — preflight checker for the postgres memory provider.
#
# Walks every prerequisite the plugin needs and prints a friendly
# pass/fail table at the end. Returns 0 if everything is ready for
# install, 1 if not. Each failing check has a one-line remediation.
#
# Usage:
#   ./diagnose.sh
#   ./diagnose.sh --hermes-home /path/to/hermes-agent
#   ./diagnose.sh --json       # machine-readable output
#
# Designed to be safe to run any number of times. Never modifies state.

set -uo pipefail

# ─── args ───────────────────────────────────────────────────────────────

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/hermes-agent}"
JSON=0
while [ $# -gt 0 ]; do
    case "$1" in
        --hermes-home) HERMES_HOME="$2"; shift 2 ;;
        --json)        JSON=1; shift ;;
        -h|--help)
            cat <<'EOF'
diagnose.sh — preflight checker for the postgres memory provider.

USAGE:
  ./diagnose.sh [--hermes-home PATH] [--json]

CHECKS (in order):
  1. HERMES_HOME exists and looks like a hermes-agent checkout
  2. plugins/memory/ directory exists
  3. ~/.hermes/.env exists and is readable
  4. POSTGRES_HOST/PORT/USER/PASSWORD/DATABASE are all set in .env
  5. KIMI_API_KEY (or another embedder key) is set
  6. PostgreSQL is reachable at $POSTGRES_HOST:$POSTGRES_PORT
  7. pg_isready exits 0 against the target DB
  8. The hermes role can connect to the target DB
  9. The pgvector extension is installed
  10. The hermes role owns the public schema (or has CREATE on it)
  11. The agent_memory table either does not exist (fresh install OK)
      or has the per-dim columns (already upgraded OK)
  12. agent_memory_settings table exists with a default_dim row
  13. agent_memory_models has 3 rows (768, 1024, 1536)
  14. HNSW indexes exist for each per-dim column

EXIT CODES:
  0  every check passed
  1  one or more checks failed — see remediation hints
  2  a tool (psql, pg_isready) is missing on $PATH

EOF
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Auto-resolve HERMES_HOME: the production layout is ~/.hermes (parent)
# with the checkout at ~/.hermes/hermes-agent. Three cases the caller
# might trip over:
#   (a) HERMES_HOME unset or empty — fall through to the default, which
#       already points at the checkout.
#   (b) HERMES_HOME set to the parent (/home/u/.hermes) — the agent
#       runtime exports it this way, and ./diagnose.sh needs to find
#       the checkout one level down.
#   (c) HERMES_HOME set to a path that simply doesn't exist — try the
#       <HERMES_HOME>/hermes-agent and <HERMES_HOME>/.hermes/hermes-agent
#       fallbacks before giving up.
#
# Resolution: if HERMES_HOME looks like the parent (no run_agent.py or
# AGENTS.md at the top level, but one nested at /hermes-agent), point
# at the nested one. AGENTS.md is the dev-guide sentinel; run_agent.py
# is the runtime entry point — either is a strong signal it's the
# checkout, not a parent dir.
if [ -d "$HERMES_HOME" ]; then
    if [ ! -f "$HERMES_HOME/run_agent.py" ] && [ ! -f "$HERMES_HOME/AGENTS.md" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
        HERMES_HOME="$HERMES_HOME/hermes-agent"
    fi
fi
if [ ! -d "$HERMES_HOME" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
    HERMES_HOME="$HERMES_HOME/hermes-agent"
fi
if [ ! -d "$HERMES_HOME" ] && [ -d "$HERMES_HOME/.hermes/hermes-agent" ]; then
    HERMES_HOME="$HERMES_HOME/.hermes/hermes-agent"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if present so we can pick up POSTGRES_* without the user
# having to re-export everything.
ENV_FILE="${HERMES_HOME%/hermes-agent}/.env"
[ -f "$ENV_FILE" ] || ENV_FILE="$HOME/.hermes/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# ─── check state ────────────────────────────────────────────────────────

PASS=0
FAIL=0
RESULTS=()

ok()    { RESULTS+=("PASS|$1|$2"); PASS=$((PASS+1)); }
fail()  { RESULTS+=("FAIL|$1|$2"); FAIL=$((FAIL+1)); }
skip()  { RESULTS+=("SKIP|$1|$2"); }

check_psycopg2() {
    # Check that the hermes-agent venv has psycopg2 (or that the user has
    # it globally). We don't actually use it for the diagnose, but the
    # plugin does, and a missing psycopg2 will surface as a confusing
    # runtime error rather than a clean check.
    python3 -c "import psycopg2" 2>/dev/null && return 0
    [ -x "$HERMES_HOME/venv/bin/python" ] && "$HERMES_HOME/venv/bin/python" -c "import psycopg2" 2>/dev/null
}

# ─── 1-2. hermes-agent checkout ────────────────────────────────────────

if [ -d "$HERMES_HOME" ]; then
    # Accept both the old flat layout (plugins/memory/) and the current
    # nested layout (plugins/memory/postgres/) — a hermes-agent install
    # of the postgres memory provider leaves one of the two on disk.
    if [ -d "$HERMES_HOME/plugins/memory/postgres" ] || [ -d "$HERMES_HOME/plugins/memory" ]; then
        ok "hermes-agent checkout" "found at $HERMES_HOME"
    else
        fail "plugins/memory/ exists" "missing in $HERMES_HOME — not a hermes-agent checkout?"
    fi
else
    fail "HERMES_HOME exists" "$HERMES_HOME does not exist. Set --hermes-home or export HERMES_HOME"
fi

# ─── 3-4. .env + required POSTGRES_* ──────────────────────────────────

if [ -f "$ENV_FILE" ]; then
    ok ".env exists" "$ENV_FILE"
else
    fail ".env exists" "no .env found at $ENV_FILE — create one with POSTGRES_* and KIMI_API_KEY"
fi

# Always define the vars to avoid `set -u` blowing up on unset lookups.
POSTGRES_HOST="${POSTGRES_HOST:-}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-hermes}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
POSTGRES_DATABASE="${POSTGRES_DATABASE:-hermes}"

MISSING=()
[ -z "$POSTGRES_HOST" ]     && MISSING+=("POSTGRES_HOST")
[ -z "$POSTGRES_PORT" ]     && MISSING+=("POSTGRES_PORT")
[ -z "$POSTGRES_USER" ]     && MISSING+=("POSTGRES_USER")
[ -z "$POSTGRES_PASSWORD" ] && MISSING+=("POSTGRES_PASSWORD")
[ -z "$POSTGRES_DATABASE" ] && MISSING+=("POSTGRES_DATABASE")

if [ ${#MISSING[@]} -eq 0 ]; then
    ok "POSTGRES_* env vars" "all 5 set (host=$POSTGRES_HOST user=$POSTGRES_USER db=$POSTGRES_DATABASE)"
else
    fail "POSTGRES_* env vars" "missing in $ENV_FILE: ${MISSING[*]}"
fi

# ─── 5. embedder key ───────────────────────────────────────────────────

HAS_KEY=0
[ -n "${KIMI_API_KEY:-}" ]      && HAS_KEY=1
[ -n "${OLLAMA_API_KEY:-}" ]    && HAS_KEY=1
[ -n "${OPENAI_API_KEY:-}" ]    && HAS_KEY=1
[ -n "${HERMES_EMBED_API_KEY:-}" ] && HAS_KEY=1

if [ $HAS_KEY -eq 1 ]; then
    WHICH=""
    [ -n "${KIMI_API_KEY:-}" ]   && WHICH="KIMI"
    [ -n "${OLLAMA_API_KEY:-}" ] && [ -n "$WHICH" ] && WHICH="$WHICH+ollama" || [ -n "${OLLAMA_API_KEY:-}" ] && WHICH="ollama"
    [ -n "${OPENAI_API_KEY:-}" ] && [ -n "$WHICH" ] && WHICH="$WHICH+openai" || [ -n "${OPENAI_API_KEY:-}" ] && WHICH="openai"
    ok "embedder API key" "$WHICH configured"
else
    fail "embedder API key" "no KIMI_API_KEY / OLLAMA_API_KEY / OPENAI_API_KEY in $ENV_FILE — plugin cannot embed without one"
fi

# ─── 6-7. psql on PATH + pg_isready ────────────────────────────────────

PSQL_BIN="$(command -v psql 2>/dev/null || true)"
PGISREADY_BIN="$(command -v pg_isready 2>/dev/null || true)"

if [ -z "$PSQL_BIN" ]; then
    fail "psql on PATH" "psql not found — install postgresql-client (apt install postgresql-client, brew install libpq)"
else
    ok "psql on PATH" "$PSQL_BIN"
fi

if [ -z "$PGISREADY_BIN" ]; then
    fail "pg_isready on PATH" "pg_isready not found — comes with postgresql-client"
else
    ok "pg_isready on PATH" "$PGISREADY_BIN"
fi

# Only run reachability checks if psql + POSTGRES_* are present
if [ -n "$PSQL_BIN" ] && [ -n "$POSTGRES_HOST" ] && [ -n "$POSTGRES_PASSWORD" ] && [ -n "$POSTGRES_USER" ]; then
    if PGPASSWORD="$POSTGRES_PASSWORD" "$PGISREADY_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -q; then
        ok "postgres reachable" "$POSTGRES_HOST:$POSTGRES_PORT (user=$POSTGRES_USER, db=$POSTGRES_DATABASE)"
    else
        fail "postgres reachable" "pg_isready failed — check host/port, firewall, and that the role exists. Run 'psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DATABASE' manually for the raw error."
    fi

    # ─── 8-10. role can connect + extensions + schema ownership ──────

    # 8. Role can connect
    if PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tAc "SELECT 1;" >/dev/null 2>&1; then
        ok "role can connect" "$POSTGRES_USER@$POSTGRES_DATABASE"
    else
        fail "role can connect" "could not connect as $POSTGRES_USER. Verify password and that the role exists (psql -U postgres -c '\\du')."
    fi

    # 9. pgvector extension
    PG_VECTOR_OK=$(PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tAc "SELECT 1 FROM pg_extension WHERE extname='vector';" 2>/dev/null | tr -d ' ' || true)
    if [ "$PG_VECTOR_OK" = "1" ]; then
        ok "pgvector extension" "installed"
    else
        fail "pgvector extension" "not installed in $POSTGRES_DATABASE. Run sql/000_create_database_and_role.sql as superuser."
    fi

    # 10. hermes owns public schema
    PUB_OWNER=$(PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tAc "SELECT pg_catalog.pg_get_userbyid(n.nspowner) FROM pg_namespace n WHERE n.nspname='public';" 2>/dev/null | tr -d ' ' || true)
    if [ "$PUB_OWNER" = "$POSTGRES_USER" ]; then
        ok "public schema owner" "$PUB_OWNER (matches POSTGRES_USER)"
    else
        fail "public schema owner" "owned by '$PUB_OWNER', expected '$POSTGRES_USER'. Re-run 000_create_database_and_role.sql as superuser, or: ALTER SCHEMA public OWNER TO $POSTGRES_USER;"
    fi

    # ─── 11-14. schema state ─────────────────────────────────────────

    TABLE_EXISTS=$(PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='agent_memory';" 2>/dev/null | tr -d ' ' || true)

    if [ "$TABLE_EXISTS" = "1" ]; then
        # Already installed — check it has per-dim columns
        HAS_768=$(PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tAc "SELECT 1 FROM information_schema.columns WHERE table_name='agent_memory' AND column_name='vector_768';" 2>/dev/null | tr -d ' ' || true)
        HAS_1024=$(PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tAc "SELECT 1 FROM information_schema.columns WHERE table_name='agent_memory' AND column_name='vector_1024';" 2>/dev/null | tr -d ' ' || true)
        HAS_1536=$(PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tAc "SELECT 1 FROM information_schema.columns WHERE table_name='agent_memory' AND column_name='vector_1536';" 2>/dev/null | tr -d ' ' || true)

        if [ "$HAS_768" = "1" ] && [ "$HAS_1024" = "1" ] && [ "$HAS_1536" = "1" ]; then
            ok "agent_memory schema" "present with all 3 per-dim columns"
        else
            fail "agent_memory schema" "exists but is missing per-dim columns. Re-run sql/000_schema.sql (it is idempotent)."
        fi

        # Settings table
        SETTINGS_OK=$(PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='agent_memory_settings';" 2>/dev/null | tr -d ' ' || true)
        if [ "$SETTINGS_OK" = "1" ]; then
            DEFAULT_DIM=$(PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tAc "SELECT value FROM agent_memory_settings WHERE key='default_dim';" 2>/dev/null | tr -d ' ' || true)
            if [ -n "$DEFAULT_DIM" ]; then
                ok "default_dim configured" "=$DEFAULT_DIM"
            else
                fail "default_dim configured" "agent_memory_settings exists but no default_dim row. INSERT INTO agent_memory_settings (key, value) VALUES ('default_dim', '1024'::jsonb);"
            fi
        else
            fail "agent_memory_settings" "missing. Re-run sql/000_schema.sql (idempotent)."
        fi

        # Models table — need 3 rows
        MODEL_COUNT=$(PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tAc "SELECT count(*) FROM agent_memory_models;" 2>/dev/null | tr -d ' ' || true)
        if [ "$MODEL_COUNT" = "3" ]; then
            ok "agent_memory_models" "3 rows (768/1024/1536)"
        else
            fail "agent_memory_models" "has $MODEL_COUNT rows, expected 3. Re-run sql/000_schema.sql."
        fi

        # HNSW indexes
        for DIM in 768 1024 1536; do
            IDX=$(PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -tAc "SELECT 1 FROM pg_indexes WHERE indexname='idx_memory_vector_${DIM}_hnsw';" 2>/dev/null | tr -d ' ' || true)
            if [ "$IDX" = "1" ]; then
                ok "HNSW index dim=$DIM" "idx_memory_vector_${DIM}_hnsw"
            else
                fail "HNSW index dim=$DIM" "missing. Run migrations/002_hnsw_per_dim.sql"
            fi
        done
    else
        # Fresh install — plugin can install schema
        ok "agent_memory schema" "not present yet (fresh install — will be created by sql/000_schema.sql)"
        # We can't pre-check 12-14 because the tables don't exist yet
        skip "default_dim configured" "will be created by sql/000_schema.sql"
        skip "agent_memory_models" "will be created by sql/000_schema.sql"
        skip "HNSW indexes" "will be created by sql/000_schema.sql"
    fi
else
    fail "postgres reachability preflight" "could not run — psql missing or POSTGRES_* unset. Fix those first."
    skip "role can connect"        "blocked by earlier failure"
    skip "pgvector extension"     "blocked by earlier failure"
    skip "public schema owner"    "blocked by earlier failure"
    skip "agent_memory schema"    "blocked by earlier failure"
    skip "default_dim configured" "blocked by earlier failure"
    skip "agent_memory_models"    "blocked by earlier failure"
    skip "HNSW indexes"           "blocked by earlier failure"
fi

# ─── psycopg2 (informational, the plugin needs it at runtime) ────────

if check_psycopg2; then
    ok "psycopg2 installed" "importable in the active python"
else
    fail "psycopg2 installed" "missing. pip install psycopg2-binary (the plugin needs it at import time)"
fi

# ─── report ────────────────────────────────────────────────────────────

if [ $JSON -eq 1 ]; then
    # Machine-readable: {results: [{status, check, detail}], pass, fail, skip}
    printf '{"pass":%d,"fail":%d,"skip":%d,"results":[' "$PASS" "$FAIL" "$((${#RESULTS[@]} - PASS - FAIL))"
    FIRST=1
    for R in "${RESULTS[@]}"; do
        IFS='|' read -r STATUS CHECK DETAIL <<< "$R"
        [ $FIRST -eq 1 ] && FIRST=0 || printf ','
        printf '{"status":"%s","check":"%s","detail":"%s"}' \
            "$STATUS" \
            "$(printf '%s' "$CHECK"  | sed 's/"/\\"/g')" \
            "$(printf '%s' "$DETAIL" | sed 's/"/\\"/g')"
    done
    echo ']}'
else
    # Human-friendly
    echo
    echo "════════════════════════════════════════════════════════════════"
    echo "  postgres memory provider — preflight report"
    echo "════════════════════════════════════════════════════════════════"
    printf "  %-4s  %-30s  %s\n" "STAT" "CHECK" "DETAIL"
    echo "  ────  ──────────────────────────────  ────────────────────────"
    for R in "${RESULTS[@]}"; do
        IFS='|' read -r STATUS CHECK DETAIL <<< "$R"
        case "$STATUS" in
            PASS) ICON="✓" ;;
            FAIL) ICON="✗" ;;
            *)    ICON="·" ;;
        esac
        printf "  %s %-3s  %-30s  %s\n" "$ICON" "$STATUS" "$CHECK" "$DETAIL"
    done
    echo "════════════════════════════════════════════════════════════════"
    echo "  $PASS passed, $FAIL failed"
    echo "════════════════════════════════════════════════════════════════"
    echo
    if [ $FAIL -gt 0 ]; then
        echo "  one or more checks failed. common fixes:"
        echo "  • install psql + pg_isready:    apt install postgresql-client"
        echo "  • bootstrap the database:        $SCRIPT_DIR/bootstrap.sh"
        echo "  • or run by hand:                psql -U postgres -f sql/000_create_database_and_role.sql"
        echo "  • re-run this script after each fix: $SCRIPT_DIR/diagnose.sh"
        echo
    else
        echo "  ready to install. next:"
        # install.sh lives at the repo root, which is 4 levels up from
        # this script (plugins/memory/postgres/scripts/diagnose.sh).
        # Resolve to an absolute path so the next-steps hint is readable
        # regardless of where the user ran diagnose.sh from.
        INSTALL_SH="$(cd "$SCRIPT_DIR/../../../../" && pwd)/install.sh"
        echo "  • $INSTALL_SH"
        echo "      # install plugin + skill into HERMES_HOME"
        echo "  • hermes gateway restart         # pick up the new .env + plugin"
        echo "  • hermes postgres-memory preflight   # confirm the plugin sees the DB"
        echo
    fi
fi

[ $FAIL -eq 0 ]
exit $?  # explicit exit code
