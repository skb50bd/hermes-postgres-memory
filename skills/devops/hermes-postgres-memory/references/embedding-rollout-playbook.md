# Embedding rollout playbook (probe → wire → verify)

A reusable workflow for "I want real embeddings in this Postgres memory
table, and I don't know yet which provider is actually reachable from
this machine." Captured from the June 2026 Kimi/Ollama/minimax rollout
on Hermes 1.x; should generalize to any new provider, new model, or new
embedding dim.

The pattern is: **probe before you commit**. Don't pick a provider from
docs and start writing code; the docs lie, free tiers get downgraded,
and the key you have may not have the scope you need.

## Phase 1 — Discover what's reachable

For each provider you have credentials for, probe the embed endpoint
*before* you write any embedder code. Use a Python wrapper, not bash,
to avoid the `***` env-substitution trap on credentials.

```python
# probe_embeddings.py — run from any host with the relevant env vars set
import os, json, urllib.request, urllib.error

# Reads KIMI_API_KEY / OLLAMA_API_KEY / MINIMAX_API_KEY / etc. from the
# environment you sourced via `set -a; source ~/.hermes/.env; set +a`,
# or set inline if you understand the trust model.

def probe(label, url, key, body, shape_keys=("data", "embedding", "embeddings", "vectors")):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
            for k in shape_keys:
                if k in d and d[k]:
                    val = d[k]
                    if isinstance(val, list) and val and isinstance(val[0], list):
                        return {"ok": True, "status": r.status, "dim": len(val[0]), "model": d.get("model")}
                    if isinstance(val, list) and val:
                        return {"ok": True, "status": r.status, "dim": len(val), "model": d.get("model")}
            return {"ok": False, "status": r.status, "raw": str(d)[:200]}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "body": e.read().decode()[:200]}

# Kimi — 9+ model aliases, all return 1024-dim, free with KIMI_API_KEY
for model in ["bge_m3_embed", "bge-m3", "nomic-embed-text", "text-embedding-3-small"]:
    r = probe(f"KIMI {model}", "https://api.kimi.com/coding/v1/embeddings",
              os.environ.get("KIMI_API_KEY"),
              {"model": model, "input": "hello world"})
    print(f"  {model:30s} {r}")

# Ollama Cloud — currently chat-only, embed returns 401 on free tier
r = probe("OLLAMA nomic-embed-text", "https://ollama.com/api/embed",
          os.environ.get("OLLAMA_API_KEY"),
          {"model": "nomic-embed-text", "input": "hello"})
print(f"  ollama nomic          {r}")

# minmax — requires `texts` (plural) and `type` (db/query/passage)
r = probe("MINIMAX embo-01", "https://api.minimax.io/v1/embeddings",
          os.environ.get("MINIMAX_API_KEY"),
          {"model": "embo-01", "texts": ["hello"], "type": "db"})
print(f"  minmax embo-01        {r}")
```

**What the probe will tell you** (verified June 2026):

| Provider | Endpoint | Auth | Status | Dim |
|---|---|---|---|---|
| Kimi `bge_m3_embed` | `api.kimi.com/coding/v1/embeddings` | `KIMI_API_KEY` | 200 | 1024 |
| Kimi `nomic-embed-text` | same | same | 200 | 1024 (alias, same vector space) |
| Ollama Cloud | `ollama.com/api/embed` | `OLLAMA_API_KEY` | 401 | n/a — catalog is chat-only |
| minmax `embo-01` | `api.minimax.io/v1/embeddings` | `MINIMAX_API_KEY` | 200 | varies |

Don't trust docs. The probe is the truth. The Ollama Cloud free tier
*used* to serve embeddings; as of June 2026 it doesn't. Future agents
re-probing this should expect the matrix to drift.

## Phase 2 — Pick a model, plan the migration

The rule: **all rows in `content_vector` must come from the same model.**
Pick a model, then plan the dim migration as a one-shot:

1. **Decide the target dim**. The probe told you the model's native dim.
   Don't pad or truncate unless you have a reason; the storage cost is
   negligible at 1024 dims, and any size mismatch with the model's
   intended output degrades recall.
2. **Decide if you need a 1536-dim OpenAI `text-embedding-3-small` or
   you can stay on Kimi 1024-dim**. The user's preference, captured in
   the parent skill, is free first; switch to paid only after
   benchmarked recall loss is shown.
3. **Plan the migration order**:
   - Drop the old HNSW index (it locks the column dim)
   - `ALTER TABLE ... DROP COLUMN content_vector`
   - `ALTER TABLE ... ADD COLUMN content_vector vector(<dim>)`
   - Recreate the HNSW index over the empty column (instant)
   - Run the backfill (calls the embedder for every row, ~500ms each)
   - Optionally rebuild the HNSW index `CONCURRENTLY` over real vectors
     (only needed at scale; for <10k rows the empty-column build is fine)

## Phase 3 — Wire the embedder

The embedder module (`plugins/memory/postgres/embedder.py`) is the
canonical place. It already:

- Discovers the API key from `HERMES_EMBED_API_KEY`, `KIMI_API_KEY`, or
  `OLLAMA_API_KEY` automatically (no caller-side plumbing)
- Has an in-memory cache and a content-addressable disk cache
- Has a dim contract check that fails open to zero on mismatch
- Has a `used_fallback` flag that refuses to cache zero-fallback vectors
  (added in v1.4.0 of this skill after the cache-poisoning rollout bug)

When wiring a new provider, add it to the `_embed_live()` dispatch
table. If the provider is OpenAI-compatible, you can probably reuse
`_embed_openai_compat()` without writing a new HTTP path. Otherwise add
a new method mirroring `_embed_ollama()`.

## Phase 4 — Migration with a privileged role

**The `hermes` application role does not own `agent_memory`.** This is
by design (defense in depth). You need a privileged role to do the
schema migration. Options, in order of complexity:

1. **User runs the SQL in pgAdmin** (preferred for one-shot). Ship them
   one file: `run_all_migration.sql` — it does the ownership transfer,
   the DDL, and the index rebuild, with verification SELECTs at the
   bottom. See `references/migration-privileges.md` for the file
   template.
2. **Transfer ownership permanently**. `ALTER TABLE agent_memory OWNER
   TO hermes;` then the migration can run as `hermes`. Trade-off:
   `hermes` now has DDL rights on the table forever, which broadens the
   blast radius of any future compromise.
3. **SECURITY DEFINER function**. Wrap the migration DDL in a function
   owned by `postgres`, grant `EXECUTE` to `hermes`. Production-grade
   but overkill for most Hermes deployments.

**PostgreSQL does NOT support `GRANT ... ALTER, DROP ON TABLE`.** Those
privileges are ownership-gated, not ACL-gated. Don't try. The
verifiable proof: `aclexplode(pg_class.relacl)` on a standard table
returns only `INSERT, SELECT, UPDATE, DELETE, TRUNCATE, REFERENCES,
TRIGGER, MAINTAIN`. Anything else requires ownership.

## Phase 5 — Backfill

`scripts/backfill_embeddings.py` is the canonical tool. It:

- Iterates the table with a server-side cursor (no full-table load)
- Skips rows that already have non-zero vectors (idempotent)
- Calls the embedder per row (cache makes duplicates free)
- Commits in batches
- Has `--dry-run` for smoke tests

**The env-loading trap**: `python backfill_embeddings.py` does NOT
auto-source `~/.hermes/.env`. You need:

```bash
set -a; source ~/.hermes/.env; set +a
python plugins/memory/postgres/scripts/backfill_embeddings.py
```

The script should also self-source the env in case it's launched
without the wrapper. Add this to the top of `main()`:

```python
def _maybe_source_env():
    if not os.environ.get("KIMI_API_KEY") and not os.environ.get("OLLAMA_API_KEY"):
        for line in open(os.path.expanduser("~/.hermes/.env")):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
```

## Phase 6 — Verify

Three checks before declaring success:

1. **Direct DB**: `SELECT count(*) FROM agent_memory WHERE
   content_vector <> array_fill(0, ARRAY[<dim>])::vector` should equal
   the active row count. If 0, the backfill wrote all zeros.
2. **pg_status**: should report `zero_vector_memories: 0` and
   `embedder.stats.errors: 0`. The shipped `PostgresMemoryProvider`
   exposes both.
3. **End-to-end `pg_search`**: search for a phrase whose tokens appear
   in the corpus, confirm `vector_sim` is non-zero on the top results.
   Empty results with `text_rank > 0` and `vector_sim = 0` mean the
   search SQL is wrong, not that the embedder is broken.

The shipped `scripts/verify_embeddings.py` runs all three.

## Anti-patterns to avoid

- **Don't** pick a model from a benchmark leaderboard and start coding.
  The leaderboard doesn't know your data, your latency budget, or your
  cost constraints. Probe first, then commit.
- **Don't** batch the embed call across many providers "for diversity."
  One provider per table, period. The dim and the embedding space
  lock you in.
- **Don't** try to migrate the dim and the provider in the same
  operation. One change per deploy; one rollback path per change.
- **Don't** trust `hermes memory status` after a backfill. The shipped
  `is_available()` only checks catalog tables, not that the column
  actually has non-zero vectors. Run the verification script.
- **Don't** leave the disk cache populated with zero-vectors from a
  failed run. Either the embedder refuses to cache them (the v1.4.0
  fix) or you must `rm -rf ~/.cache/hermes/embeddings/` before
  re-running. Otherwise the second run is a 100% cache hit of zeros
  and the table never gets real embeddings.
