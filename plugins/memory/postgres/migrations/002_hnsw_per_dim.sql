-- 002_hnsw_per_dim.sql
--
-- Build the per-dim HNSW indexes on the new vector columns. These
-- indexes are required for fast (<100ms) cosine search at scale.
-- Skipping this step means search will work but use sequential scan
-- (acceptable for <10k rows, painful above that).
--
-- We use CONCURRENTLY so we don't block writes during build. This
-- means this file must be run OUTSIDE a transaction (autocommit mode).
-- psql default is autocommit, so the standard
--   psql ... -f 002_hnsw_per_dim.sql
-- invocation works. If you wrap it in BEGIN/COMMIT, it will fail with
--   ERROR: CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
--
-- This file is idempotent (IF NOT EXISTS on every index).
--
-- Wait for the indexes to finish building before running
-- `hermes postgres-memory backfill`. CREATE INDEX CONCURRENTLY is
-- non-blocking but the index isn't usable until it finishes. Check
-- progress with: SELECT * FROM pg_stat_progress_create_index;
--
-- OR, if you don't have a huge table yet, you can drop the
-- CONCURRENTLY keyword — the build will be fast (sub-second per
-- 1000 rows) and block writes briefly.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memory_vector_768_hnsw
    ON agent_memory
    USING hnsw (vector_768 vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memory_vector_1024_hnsw
    ON agent_memory
    USING hnsw (vector_1024 vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memory_vector_1536_hnsw
    ON agent_memory
    USING hnsw (vector_1536 vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
