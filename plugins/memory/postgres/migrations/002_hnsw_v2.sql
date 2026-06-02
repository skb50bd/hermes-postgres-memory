-- 002_hnsw_v2.sql
--
-- Build the HNSW index on the new content_vector_v2 column AFTER the
-- Python backfill has populated it with real embeddings. Building HNSW
-- over mostly-zero rows produces a low-quality graph (zero vectors are
-- all equidistant) and wastes the build work.
--
-- We use CONCURRENTLY so we don't block writes during build. This means
-- this file must be run OUTSIDE a transaction (autocommit mode). psql
-- default is autocommit, so the standard
--   psql ... -f 002_hnsw_v2.sql
-- invocation works. If you wrap it in BEGIN/COMMIT, it will fail with
--   ERROR: CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
--
-- This file is idempotent.
--
-- After this index exists, the plugin can use the v2 column for vector
-- similarity search. The plugin's runtime check `live_vector_column = 'v2'`
-- is what actually switches reads; this index can exist before that switch
-- without harm.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memory_vector_hnsw_v2
    ON agent_memory
    USING hnsw (content_vector_v2 vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
