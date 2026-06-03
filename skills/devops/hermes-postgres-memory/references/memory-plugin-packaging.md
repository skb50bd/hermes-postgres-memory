# Memory plugin packaging — what we learned, distilled

A condensed checklist for "I built a memory plugin, how do I share it?"
Captured from the June 2026 review of the postgres/pgvector memory
plugin. The full session plan (with all postgres-specifics) is in
`~/.hermes/plans/2026-06-02-postgres-memory-packaging/plan.md`; this
file is the portable, project-agnostic version.

## The 30-second decision tree

```
Want to share a memory plugin?
├── Yes, and you can upstream it          → PR into NousResearch/hermes-agent
├── Yes, but you want independence        → PyPI package + custom entry point
├── Yes, but minimum effort               → Standalone GitHub repo + setup.sh
└── Not yet, just want it clean           → Run the 11-issue review locally
```

Default recommendation: **upstream PR**. The hermes-agent catalog
benefits from a database-backed plugin, and shared maintenance beats
forked maintenance.

## The 11 issues that block a PR (always check these first)

1. **`plugin.yaml` field names** — upstream uses `pip_dependencies:`,
   `requires_env:`, `hooks:`. Other spellings are silently ignored.
2. **Don't re-declare base deps** — `psycopg2-binary` is a base
   hermes-agent dep; only declare the *additions* (e.g. `httpx`).
3. **Hardcoded dim constants** — if the embedder module exposes
   `dim` as a property, don't keep a parallel `_EMBED_DIM = 1024` in
   the plugin's `__init__.py`. Use the embedder's dim everywhere.
4. **Stale README** — must reflect the current schema, env vars, and
   schema and env contract. A README that describes a removed setup path is worse than no README.
5. **Public docs must match the current greenfield contract.** Do not ship
   stale setup notes, removed env vars, or historical prose in user setup docs. Git carries history; docs should describe what works today.
6. **Author/license/metadata** — match the convention of sibling
   plugins in the same directory. For hermes-agent: `author: "Hermes
   Agent"`, `license: MIT`.
7. **Per-method `self._lock`** that serializes everything on a
   single client instance, defeating the connection pool. Drop it
   (the pool is already thread-safe) or document why it's there.
8. **No integration test against real Postgres** — all-mocked tests
   pass even when the SQL is broken. Add at least one test that hits
   a dockerized PG + pgvector, exercises add → search, and asserts
   non-zero `vector_sim` on a known-good query.
9. **Fragile parameter lists in raw SQL** — assert in a test that
   the `%s` placeholder count matches `len(params)`. Future WHERE
   clauses that don't update the params list will silently break.
10. **`prefetch()` does live network work on every turn** — the first
    call is ~500ms latency on Kimi (cache miss). Document the cost
    in the PR; consider gating on minimum query length.
11. **Migration file names that lie about their content** — a file
    called `000_grant_ddl_to_hermes.sql` that does
    `ALTER TABLE ... OWNER TO` will confuse every reader. Rename
    to match.

## Three questions to ask the user before starting

1. **Packaging destination** — A (upstream), B (PyPI), C (standalone repo).
2. **Author name in `plugin.yaml`** — the personal brand, the
   company, or upstream's "Hermes Agent".
3. **Greenfield policy for stale installs** — destructive reset, explicit unsupported state, or separate import tooling if the user later asks for it.

Don't assume defaults. Each choice has trade-offs the user needs to
own.

## The phased plan, generic shape

| Phase | What | Output |
|---|---|---|
| 0 — Pre-PR cleanup | Run the checklist; rewrite README; centralize hardcoded constants; verify greenfield docs | A clean working tree |
| 1 — Self-review | Run the full hermes-agent test suite (no leftover state); run linter; test on a dockerized PG 16 | Green CI equivalent |
| 2 — PR description | State the gap, list what's new, call out breaking changes, include a copy-paste runbook | A PR description reviewers can act on |
| 3 — Open PR and iterate | Address review comments; expect questions on "why X default" and "what if Y"; merge | 1.1.0 tagged |
| 4 — Post-merge cleanup | Remove the duplicate user-level skill; update MEMORY.md with the new source of truth; save a "how to open an upstream PR for a memory plugin" skill for next time | Clean local state |

## Skill and reference file placement

A memory plugin's skill is part of the package. When shipping:

- Move `~/.hermes/skills/<cat>/<name>/` to
  `~/.hermes/hermes-agent/skills/<cat>/<name>/` so it ships in the
  same PR.
- Inside the skill, references like `scripts/verify_embeddings.py`
  resolve relative to the skill dir — they stay valid.
- Grep the references for any hardcoded `~/.hermes/hermes-agent/...`
  paths and confirm they still resolve in the new layout (they will,
  because the skill is now in that tree).
- After the move, the local `~/.hermes/skills/<cat>/<name>/` can be
  removed or kept as a user-level override; the repo copy wins for
  new installs.

## When NOT to ship

- The plugin is a one-off for a single deployment. Keep it local.
- The user has a custom schema that diverges from upstream's
  conventions in ways that would force a fork (different column
  names, different dim, different category table).
- The plugin is <100 lines and doesn't have its own embedder / SQL
  / schema. It's not really a plugin yet.

## Lessons that don't fit in a checklist

- **"Verify, don't guess" applies to *your own* claims too.** When
  the user asked "are embeddings wired up", the first answer was
  based on the schema. The schema was wrong — the column was 1536
  dim and held zero vectors. The fix was a verification script
  (`scripts/verify_embeddings.py`) that grep's the column for
  non-zero vectors and reports the embedder's runtime stats.
  Include that script in every plugin that does anything
  async/network/storage-related.
- **Catch cache poisoning in the test, not in production.** A
  fail-open embedder that caches its zero-vector fallback is a
  time bomb: a transient 401 poisons the cache, and every later
  call returns zeros. The shipped test
  `test_provider_failure_fails_open_to_zero_vector` asserts the
  second call hits the network, not the cache. Every plugin that
  has any fail-open / fallback path should have an equivalent
  test.
- **The user's environment may differ from yours.** In this session,
  the user has `KIMI_API_KEY` in `~/.hermes/.env`; you don't. Any
  embedder that defaults to Kimi will fail-open to zero vectors on
  your machine and look like it's working on theirs. The
  verification script catches this, but the lesson is: pick a
  default that's reachable from *the user's* env, and document the
  fallback path so a future agent doesn't get confused.
