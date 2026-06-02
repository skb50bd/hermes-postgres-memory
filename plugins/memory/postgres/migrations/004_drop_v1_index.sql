-- 004_drop_v1_index.sql
--
-- Drop the old HNSW index on content_vector (1536) before we drop the
-- column itself in 005. The index must be dropped first because ALTER
-- TABLE ... DROP COLUMN on a column with an index requires the index
-- be dropped first.
--
-- This file is idempotent.

DROP INDEX IF EXISTS idx_memory_vector_hnsw;
