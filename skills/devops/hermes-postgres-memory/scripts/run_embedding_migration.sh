#!/usr/bin/env bash
# run_embedding_migration.sh
#
# End-to-end runbook for the embedding-dim migration on agent_memory.
# 1536 (zero-vector placeholder) -> 1024 (Kimi bge_m3_embed).
#
# Prerequisites (run ONCE before this script, as a privileged role):
#   psql -U postgres -d hermes \
#     -f migrations/000_grant_ddl_to_hermes.sql
#
# After that, the hermes role can run everything below.
#
# Idempotency:
#   - Migration 001: re-running after a partial failure is safe (DROP IF EXISTS).
#   - Backfill: skips rows with non-zero vectors.
#   - Migration 002: CREATE INDEX CONCURRENTLY IF NOT EXISTS.
#
# Dry-run / smoke-test:
#   - python backfill_embeddings.py --dry-run --limit 5
#
# What this script does, in order:
#   1. Pre-flight: verify DDL grant took, verify current state.
#   2. Migration 001: drop HNSW, drop column, recreate as vector(1024).
#   3. Backfill: re-embed all zero-vector rows via the configured provider.
#   4. Migration 002: rebuild HNSW over real vectors (CONCURRENTLY).
#   5. Verify: zero-vector count = 0, embedder stats reasonable.
#
# Usage:
#   ./scripts/run_embedding_migration.sh          # full run
#   ./scripts/run_embedding_migration.sh --skip-migration  # backfill + index only
#   ./scripts/run_embedding_migration.sh --dry-run          # migration + dry-run backfill only
#   SKIP_BACKFILL=1 ./scripts/run_embedding_migration.sh    # migration + index, no backfill

set -euo pipefail

# ── Resolve paths ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"
MIG_DIR="$PLUGIN_DIR/migrations"

# ── Source credentials ──────────────────────────────────────────────────
if [ -f ~/.hermes/.env ]; then
  set -a; source ~/.hermes/.env; set +a
fi

# Prefer PG_MEM_DB_CONN_STR (v1.5.0+). Fall back to building a DSN from the
# legacy POSTGRES_* vars when the new form is unset.
if [ -n "${PG_MEM_DB_CONN_STR:-}" ]; then
  PSQL=(psql "$PG_MEM_DB_CONN_STR" -v ON_ERROR_STOP=1 -X -q)
else
  : "${POSTGRES_HOST:?POSTGRES_HOST not set}"
  : "${POSTGRES_PORT:=5432}"
  : "${POSTGRES_USER:=hermes}"
  : "${POSTGRES_DATABASE:=hermes}"
  : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD not set}"
  export PGPASSWORD="$POSTGRES_PASSWORD"
  PSQL=(psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" \
        -d "$POSTGRES_DATABASE" -v ON_ERROR_STOP=1 -X -q)
fi

SKIP_MIGRATION=0
SKIP_BACKFILL=0
DRY_RUN=0
for arg in "$@"; do
  case $arg in
    --skip-migration) SKIP_MIGRATION=1 ;;
    --skip-backfill)  SKIP_BACKFILL=1 ;;
    --dry-run)        DRY_RUN=1; SKIP_BACKFILL=0 ;;
  esac
done

# ── Step 1: Pre-flight ──────────────────────────────────────────────────
echo "═══ Step 1: Pre-flight ═══"
"${PSQL[@]}" -c "SELECT current_user, pg_catalog.pg_get_userbyid(relowner) AS agent_memory_owner FROM pg_class WHERE relname='agent_memory';"

# Note: PostgreSQL does NOT support `has_table_privilege('table', 'ALTER')`
# because ALTER and DROP on a table are ownership-gated, not ACL-gated. The
# real check is whether current_user IS the table owner. (Verified on PG 18.4.)
OWNER=$("${PSQL[@]}" -tA -c "SELECT pg_catalog.pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.relname='agent_memory';")
CURRENT=$("${PSQL[@]}" -tA -c "SELECT current_user;")
if [ "$OWNER" != "$CURRENT" ]; then
  echo "ERROR: table agent_memory is owned by '$OWNER', not '$CURRENT'."
  echo "The hermes role cannot perform DDL on the table."
  echo "Run migrations/000_grant_ddl_to_hermes.sql as a privileged role first:"
  echo "    ALTER TABLE agent_memory OWNER TO hermes;"
  exit 2
fi

# ── Step 2: Migration 001 ───────────────────────────────────────────────
if [ "$SKIP_MIGRATION" = "0" ]; then
  echo
  echo "═══ Step 2: Migration 001 (drop HNSW, resize column to 1024) ═══"
  "${PSQL[@]}" -f "$MIG_DIR/001_embedding_dim.sql"
  echo "  -> verifying column dim..."
  "${PSQL[@]}" -tA -c "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid='agent_memory'::regclass AND attname='content_vector';"
  # expected: vector(1024)
fi

# ── Step 3: Backfill ────────────────────────────────────────────────────
if [ "$SKIP_BACKFILL" = "0" ]; then
  echo
  echo "═══ Step 3: Backfill ═══"
  BACKFILL_FLAGS=""
  [ "$DRY_RUN" = "1" ] && BACKFILL_FLAGS="--dry-run"
  ( cd "$PLUGIN_DIR" && python scripts/backfill_embeddings.py $BACKFILL_FLAGS )

  if [ "$DRY_RUN" = "0" ]; then
    echo "  -> verifying zero-vector count..."
    ZEROS=$("${PSQL[@]}" -tA -c "SELECT count(*) FROM agent_memory WHERE content_vector = array_fill(0, ARRAY[1024])::vector;")
    echo "  zero-vector rows: $ZEROS (expected 0)"
    if [ "$ZEROS" != "0" ]; then
      echo "WARN: $ZEROS rows still have zero vectors. Re-run the backfill to retry."
    fi
  fi
fi

# ── Step 4: Migration 002 ───────────────────────────────────────────────
if [ "$SKIP_MIGRATION" = "0" ] && [ "$DRY_RUN" = "0" ]; then
  echo
  echo "═══ Step 4: Migration 002 (rebuild HNSW CONCURRENTLY) ═══"
  echo "NOTE: must run OUTSIDE a transaction block. psql -1 not used."
  "${PSQL[@]}" -f "$MIG_DIR/002_recreate_hnsw.sql"
  echo "  -> verifying index..."
  "${PSQL[@]}" -tA -c "SELECT indexname FROM pg_indexes WHERE tablename='agent_memory' AND indexname='idx_memory_vector_hnsw';"
  # expected: idx_memory_vector_hnsw
fi

# ── Step 5: Summary ─────────────────────────────────────────────────────
echo
echo "═══ Step 5: Summary ═══"
"${PSQL[@]}" -c "SELECT
  count(*) AS total_memories,
  count(*) FILTER (WHERE content_vector = array_fill(0, ARRAY[1024])::vector) AS zero_vector_memories,
  count(*) FILTER (WHERE content_vector <> array_fill(0, ARRAY[1024])::vector) AS embedded_memories
FROM agent_memory
WHERE is_active = TRUE;"
echo
echo "Done. Next: use pg_status to confirm embedder is wired and responsive."
