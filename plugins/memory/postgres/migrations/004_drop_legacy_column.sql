-- 004_drop_legacy_column.sql
--
-- FINAL cutover. Drop the legacy content_vector column and its HNSW
-- index. After this, only the per-dim columns remain.
--
-- PRECONDITION:
--   1. 001_add_per_dim_columns.sql has been run.
--   2. 002_hnsw_per_dim.sql has been run (HNSW on the new columns).
--   3. 003_migrate_legacy_content_vector.sql has been run (data
--      copied from content_vector to the matching per-dim column).
--   4. The plugin is currently working with the new columns
--      (verify with `hermes postgres-memory status`).
--   5. You have a backup of agent_memory if you want a safety net.
--
-- This step is MANUAL. The repo's `hermes postgres-memory
-- finalize-cutover` CLI command runs this file. Don't run it by
-- hand unless you know what you're doing.
--
-- Idempotent (DROP COLUMN IF EXISTS, DROP INDEX IF EXISTS).

BEGIN;
DROP INDEX IF EXISTS idx_memory_vector_hnsw;
ALTER TABLE agent_memory DROP COLUMN IF EXISTS content_vector;
COMMIT;
