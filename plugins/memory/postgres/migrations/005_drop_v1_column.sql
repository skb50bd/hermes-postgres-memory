-- 005_drop_v1_column.sql
--
-- FINAL cutover. Drop the old 1536-dim content_vector column. After this:
--   - agent_memory has only content_vector_v2 (1024-dim) for vector data.
--   - The agent_memory_settings.live_vector_column row is now redundant
--     but we leave it in place — it's one row and documents the history.
--   - All read/write paths in the plugin go to v2 only.
--
-- PRECONDITION:
--   1. 003_switch_live_column.sql has been run (live_vector_column = 'v2').
--   2. 004_drop_v1_index.sql has been run.
--   3. The plugin is currently working (verify with hermes postgres-memory
--      status or scripts/verify_embeddings.py).
--   4. You have a backup of agent_memory if you want a safety net.
--
-- This step is MANUAL. The repo's `finalize-cutover` CLI command runs
-- 004 + 005 in a single transaction. Don't run this file by hand unless
-- you know what you're doing.
--
-- This file is idempotent.

BEGIN;
ALTER TABLE agent_memory DROP COLUMN IF EXISTS content_vector;
COMMIT;
