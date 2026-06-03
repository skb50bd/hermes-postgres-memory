#!/usr/bin/env bash
# diagnose.sh — greenfield preflight checker for the postgres memory provider.
# Safe to run repeatedly; never modifies state.

set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/hermes-agent}"
JSON=0
while [ $# -gt 0 ]; do
    case "$1" in
        --hermes-home) HERMES_HOME="$2"; shift 2 ;;
        --json) JSON=1; shift ;;
        -h|--help)
            cat <<'EOF'
diagnose.sh — preflight checker for the postgres memory provider.

USAGE:
  ./diagnose.sh [--hermes-home PATH] [--json]

REQUIRED CONFIG:
  PG_MEM_DB_CONN_STR    PostgreSQL libpq DSN for the Hermes memory DB
  KIMI_API_KEY          Default 1024-dim embedder key

CHECKS:
  - hermes-agent checkout/plugin path
  - ~/.hermes/.env and PG_MEM_DB_CONN_STR
  - psql / pg_isready / psycopg2
  - PostgreSQL reachability, pgvector, schema ownership
  - fresh schema tables, per-dim vector columns, model registry, HNSW indexes
EOF
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Accept HERMES_HOME as either ~/.hermes or ~/.hermes/hermes-agent.
if [ -d "$HERMES_HOME" ] && [ ! -f "$HERMES_HOME/run_agent.py" ] && [ ! -f "$HERMES_HOME/AGENTS.md" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
    HERMES_HOME="$HERMES_HOME/hermes-agent"
fi
if [ ! -d "$HERMES_HOME" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
    HERMES_HOME="$HERMES_HOME/hermes-agent"
fi
if [ ! -d "$HERMES_HOME" ] && [ -d "$HERMES_HOME/.hermes/hermes-agent" ]; then
    HERMES_HOME="$HERMES_HOME/.hermes/hermes-agent"
fi

ENV_FILE="${HERMES_HOME%/hermes-agent}/.env"
[ -f "$ENV_FILE" ] || ENV_FILE="$HOME/.hermes/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

PASS=0
FAIL=0
RESULTS=()
ok() { RESULTS+=("PASS|$1|$2"); PASS=$((PASS+1)); }
fail() { RESULTS+=("FAIL|$1|$2"); FAIL=$((FAIL+1)); }
skip() { RESULTS+=("SKIP|$1|$2"); }

run_psql() {
    psql "$PG_MEM_DB_CONN_STR" -v ON_ERROR_STOP=1 -X -q -tAc "$1" 2>/dev/null
}

# hermes checkout/plugin
if [ -d "$HERMES_HOME" ]; then
    if [ -d "$HERMES_HOME/plugins/memory/postgres" ] || [ -d "$HERMES_HOME/plugins/memory" ]; then
        ok "hermes-agent checkout" "found at $HERMES_HOME"
    else
        fail "plugins/memory exists" "missing in $HERMES_HOME — install the plugin first"
    fi
else
    fail "HERMES_HOME exists" "$HERMES_HOME does not exist"
fi

if [ -f "$ENV_FILE" ]; then
    ok ".env exists" "$ENV_FILE"
else
    fail ".env exists" "no .env found at $ENV_FILE"
fi

PG_MEM_DB_CONN_STR="${PG_MEM_DB_CONN_STR:-}"
if [ -n "$PG_MEM_DB_CONN_STR" ]; then
    PG_MEM_DB_CONN_STR=$(python3 - <<'PY'
import os
from urllib.parse import quote
raw = os.environ.get('PG_MEM_DB_CONN_STR', '').strip()
if ';' not in raw or '=' not in raw.split(';', 1)[0]:
    print(raw)
else:
    pairs = {}
    mapping = {'host':'host','server':'host','port':'port','database':'dbname','dbname':'dbname','user':'user','username':'user','userid':'user','uid':'user','password':'password','pwd':'password','sslmode':'sslmode'}
    for part in raw.split(';'):
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        nk = mapping.get(k.strip().replace(' ', '').lower())
        if nk and v.strip():
            pairs[nk] = v.strip()
    def q(v): return quote(v, safe='')
    if {'host','dbname','user','password'} <= pairs.keys():
        port = ':' + pairs['port'] if pairs.get('port') else ''
        ssl = '?sslmode=' + q(pairs['sslmode']) if pairs.get('sslmode') else ''
        print(f"postgresql://{q(pairs['user'])}:{q(pairs['password'])}@{pairs['host']}{port}/{q(pairs['dbname'])}{ssl}")
    else:
        print(raw)
PY
)
    ok "PG_MEM_DB_CONN_STR env var" "set"
else
    fail "PG_MEM_DB_CONN_STR env var" "missing in $ENV_FILE"
fi

HAS_KEY=0
[ -n "${KIMI_API_KEY:-}" ] && HAS_KEY=1
[ -n "${MINIMAX_API_KEY:-}" ] && HAS_KEY=1
[ -n "${OLLAMA_API_KEY:-}" ] && HAS_KEY=1
[ -n "${HERMES_EMBED_API_KEY:-}" ] && HAS_KEY=1
if [ $HAS_KEY -eq 1 ]; then
    ok "embedder API key" "configured"
else
    fail "embedder API key" "set KIMI_API_KEY (default), or another configured embedder key"
fi

PSQL_BIN="$(command -v psql 2>/dev/null || true)"
PGISREADY_BIN="$(command -v pg_isready 2>/dev/null || true)"
[ -n "$PSQL_BIN" ] && ok "psql on PATH" "$PSQL_BIN" || fail "psql on PATH" "install postgresql-client"
[ -n "$PGISREADY_BIN" ] && ok "pg_isready on PATH" "$PGISREADY_BIN" || fail "pg_isready on PATH" "install postgresql-client"

if python3 -c "import psycopg2" 2>/dev/null || { [ -x "$HERMES_HOME/venv/bin/python" ] && "$HERMES_HOME/venv/bin/python" -c "import psycopg2" 2>/dev/null; }; then
    ok "psycopg2 import" "available"
else
    fail "psycopg2 import" "install psycopg2-binary in the Hermes Python environment"
fi

if [ -n "$PG_MEM_DB_CONN_STR" ] && [ -n "$PGISREADY_BIN" ]; then
    if "$PGISREADY_BIN" -d "$PG_MEM_DB_CONN_STR" -q; then
        ok "postgres reachable" "pg_isready succeeded"
    else
        fail "postgres reachable" "pg_isready failed via PG_MEM_DB_CONN_STR"
    fi
fi

if [ -n "$PG_MEM_DB_CONN_STR" ] && [ -n "$PSQL_BIN" ]; then
    if run_psql "SELECT 1;" >/dev/null; then
        ok "role can connect" "PG_MEM_DB_CONN_STR works"
    else
        fail "role can connect" "psql failed via PG_MEM_DB_CONN_STR"
    fi

    if [ "$(run_psql "SELECT 1 FROM pg_extension WHERE extname='vector';" | tr -d ' ')" = "1" ]; then
        ok "pgvector extension" "installed"
    else
        fail "pgvector extension" "missing; run sql/000_create_database_and_role.sql as DB admin"
    fi

    PUB_OWNER="$(run_psql "SELECT pg_catalog.pg_get_userbyid(n.nspowner) FROM pg_namespace n WHERE n.nspname='public';" | tr -d ' ' || true)"
    CURRENT_USER="$(run_psql "SELECT current_user;" | tr -d ' ' || true)"
    if [ -n "$PUB_OWNER" ] && [ "$PUB_OWNER" = "$CURRENT_USER" ]; then
        ok "public schema owner" "$PUB_OWNER"
    else
        fail "public schema owner" "owned by '$PUB_OWNER', current user '$CURRENT_USER'"
    fi

    HAS_AGENT_MEMORY="$(run_psql "SELECT to_regclass('public.agent_memory') IS NOT NULL;" | tr -d ' ' || true)"
    if [ "$HAS_AGENT_MEMORY" = "t" ]; then
        ok "agent_memory table" "exists"

        for col in vector_768 vector_1024 vector_1536; do
            HAS_COL="$(run_psql "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agent_memory' AND column_name='$col');" | tr -d ' ' || true)"
            [ "$HAS_COL" = "t" ] && ok "agent_memory.$col" "present" || fail "agent_memory.$col" "missing; run sql/000_schema.sql"
        done

    else
        skip "agent_memory table" "not installed yet; run sql/000_schema.sql"
    fi

    for tbl in memory_categories agent_memory_settings agent_memory_models; do
        HAS_TBL="$(run_psql "SELECT to_regclass('public.$tbl') IS NOT NULL;" | tr -d ' ' || true)"
        [ "$HAS_TBL" = "t" ] && ok "$tbl table" "exists" || fail "$tbl table" "missing; run sql/000_schema.sql"
    done

    MODEL_COUNT="$(run_psql "SELECT count(*) FROM agent_memory_models WHERE dim IN (768,1024,1536);" | tr -d ' ' || true)"
    [ "$MODEL_COUNT" = "3" ] && ok "agent_memory_models rows" "3 dims registered" || fail "agent_memory_models rows" "expected 3, got ${MODEL_COUNT:-?}"

    for idx in idx_memory_vector_768_hnsw idx_memory_vector_1024_hnsw idx_memory_vector_1536_hnsw; do
        HAS_IDX="$(run_psql "SELECT to_regclass('public.$idx') IS NOT NULL;" | tr -d ' ' || true)"
        [ "$HAS_IDX" = "t" ] && ok "$idx" "exists" || fail "$idx" "missing; run sql/000_schema.sql"
    done
fi

if [ "$JSON" = "1" ]; then
    printf '{"pass":%s,"fail":%s,"checks":[' "$PASS" "$FAIL"
    FIRST=1
    for r in "${RESULTS[@]}"; do
        IFS='|' read -r status name detail <<<"$r"
        [ $FIRST -eq 0 ] && printf ','
        FIRST=0
        python3 - <<PY
import json
print(json.dumps({"status":"$status","name":"$name","detail":"$detail"}), end="")
PY
    done
    printf ']}\n'
else
    echo
    echo "Postgres memory preflight"
    echo "========================="
    for r in "${RESULTS[@]}"; do
        IFS='|' read -r status name detail <<<"$r"
        case "$status" in
            PASS) icon="✓" ;;
            FAIL) icon="✗" ;;
            *) icon="-" ;;
        esac
        printf " %s %-34s %s\n" "$icon" "$name" "$detail"
    done
    echo
    echo "$PASS passed, $FAIL failed"
fi

[ "$FAIL" -eq 0 ]
