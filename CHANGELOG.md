# Changelog

## 1.1.0 (2026-06-XX)

### Added
- Pluggable embedder: `kimi` (default, free, BGE-M3 family), `ollama_local`,
  `ollama_cloud`, `noop`. Configured via `HERMES_EMBED_PROVIDER`.
- Real 1024-dim embeddings for `pg_remember` (replaces 1536-dim zero-vector
  placeholder).
- Hybrid search: FTS pre-filter → cosine re-rank. Configurable text/vector
  weight (`HERMES_POSTGRES_HYBRID_TEXT_WEIGHT`).
- Content-addressable embedding cache: in-memory + on-disk, sha256 of
  (provider|model|text). Disk cache refuses to store fail-open zero
  vectors (defense against cache poisoning).
- `live_vector_column` settings table for non-destructive v1→v2 migration.
- Sidecar column `content_vector_v2`; old `content_vector` retained.
- Five migrations:
  - `000_grant_ddl_to_hermes.sql` — ownership transfer (idempotent)
  - `001_add_v2_column.sql` — add sidecar + settings table
  - `002_hnsw_v2.sql` — HNSW index on v2 (CONCURRENTLY)
  - `003_switch_live_column.sql` — flip live column to v2
  - `004_drop_v1_index.sql` / `005_drop_v1_column.sql` — cutover steps
- `scripts/backfill_embeddings.py` — idempotent backfill, auto-sources
  `~/.hermes/.env`, `--dry-run` / `--limit` / `--batch` / `--column`.
- `cli.py` — `hermes postgres-memory status | vector-column | backfill |
  preflight | finalize-cutover`.
- `verify_embeddings.py` — "are embeddings actually working" probe
  (counts non-zero vectors, runs hybrid query, prints embedder stats).
- 18 new tests (embedder contract, integration, cache-poisoning guard).
- `hermes-postgres-memory` skill: 4 reference docs, 2 scripts, 28 KB SKILL.md.

### Changed
- `_PostgresClient` no longer has a per-instance lock (was serializing
  all DB ops). The connection pool is already thread-safe.
- `system_prompt_block` reports the live vector column.
- `pg_status` includes `live_vector_column` and `embedder.stats`.

### Breaking changes
- The legacy `content_vector(1536)` column is no longer written to by
  default. New writes go to `content_vector_v2` (1024-dim) once the
  plugin's `live_vector_column` setting is 'v2'. Existing 1536-dim rows
  are preserved and queryable via FTS-only fallback until cutover.
- The hardcoded `_EMBED_DIM = 1024` shim has been removed; the
  embedder's `dim` property is the single source of truth.

### Removed
- The `dependencies:` field in `plugin.yaml` (replaced with upstream's
  `pip_dependencies:` convention; only `httpx` is declared since
  `psycopg2-binary` is a base Hermes dependency).

## 1.0.0

- Initial release: pgvector-backed MemoryProvider with HNSW index,
  GIN FTS, categories, tags, JSONB metadata, TTL, soft deletes.
- 5 tools: `pg_remember`, `pg_search`, `pg_recent`, `pg_forget`, `pg_status`.
- Schema: `content_vector vector(1536)`, zero-vector placeholder
  (never actually used; the column was an unused scaffold).
