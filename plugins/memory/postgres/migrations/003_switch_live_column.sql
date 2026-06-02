-- 003_switch_live_column.sql
--
-- Switch the plugin's read/write path from v1 (content_vector, 1536-dim)
-- to v2 (content_vector_v2, 1024-dim). This is a config-only change: it
-- updates agent_memory_settings.live_vector_column to 'v2'.
--
-- PRECONDITION: The backfill must be complete. Run:
--   python scripts/backfill_embeddings.py --dry-run
-- to confirm 0 rows still need embedding before running this.
--
-- This is also the step the user can use to ROLL BACK: setting the
-- value to 'v1' makes the plugin read from the old column again. We
-- recommend keeping a backup of agent_memory before running this if
-- the old column held anything important (in practice, v1 was a zero-
-- vector placeholder, so the backout is painless).
--
-- This file is idempotent.

UPDATE agent_memory_settings
SET value = '"v2"'::jsonb, updated_at = now()
WHERE key = 'live_vector_column';
