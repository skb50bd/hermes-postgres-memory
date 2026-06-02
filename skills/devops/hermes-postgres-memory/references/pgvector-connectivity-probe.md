# pgvector Connectivity Probe

Use this when `hermes memory status` says "available ✓" but you suspect the
connection is actually down, or when session env vars may be stale. Also catches
the post-restore case where the table exists but the Hermes role lacks grants.

## Direct psycopg2 probe (bypasses Hermes provider layer)

```bash
# Locate the venv that has psycopg2
find ~/.hermes -path "*/site-packages/psycopg2/__init__.py" | head -3

# Activate it and probe
source ~/.hermes/hermes-agent/venv/bin/activate
python3 << 'PYEOF'
import os, psycopg2

# Re-read .env directly — do NOT trust os.environ (may be stale from startup)
env = {}
with open(os.path.expanduser("~/.hermes/.env")) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v.strip('"').strip("'")

conn = psycopg2.connect(
    host=env["POSTGRES_HOST"],
    port=env["POSTGRES_PORT"],
    user=env["POSTGRES_USER"],
    password=env["POSTGRES_PASSWORD"],
    dbname=env["POSTGRES_DATABASE"],
    connect_timeout=5
)
cur = conn.cursor()

# ── Phase 1: Catalog checks (these always work regardless of table grants) ──
cur.execute("SELECT version()")
print(f"PG version: {cur.fetchone()[0].split(',')[0]}")

cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
row = cur.fetchone()
print(f"pgvector:   {row[0] if row else 'NOT INSTALLED'}")

cur.execute("SELECT tableowner FROM pg_tables WHERE tablename = 'agent_memory'")
row = cur.fetchone()
print(f"table owner: {row[0] if row else 'TABLE MISSING'}")

# ── Phase 2: Permission check (catches post-restore missing grants) ──
cur.execute("""
    SELECT privilege_type
    FROM information_schema.table_privileges
    WHERE table_name = 'agent_memory' AND grantee = current_user
    ORDER BY privilege_type
""")
grants = [r[0] for r in cur.fetchall()]
if grants:
    print(f"grants:      {', '.join(grants)}")
else:
    print("grants:      NONE — role has no table permissions!")
    print("             Fix: GRANT SELECT, INSERT, UPDATE, DELETE ON agent_memory TO hermes;")

# ── Phase 3: Data access (proves the role can actually work) ──
try:
    cur.execute("SELECT count(*) FROM agent_memory WHERE is_active = TRUE")
    active = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM agent_memory")
    total = cur.fetchone()[0]
    print(f"memories:    {active} active / {total} total")
except Exception as e:
    print(f"data access: FAILED — {e}")

cur.execute("SELECT pg_size_pretty(pg_total_relation_size('agent_memory'))")
print(f"size:        {cur.fetchone()[0]}")

cur.close()
conn.close()
PYEOF
```

## Why this beats `hermes memory status`

| `hermes memory status` | Direct probe |
|---|---|
| `is_available()` queries only catalog tables — misses missing grants | Phase 2 checks `information_schema.table_privileges` |
| Binary yes/no | Three-phase: catalog → permissions → data access |
| Reports env vars as "missing" even on connect errors | Actual error message (No route to host, auth failed, etc.) |
| Uses session-stale `os.environ` | Re-reads `.env` directly |

## Common failure modes and their probe output

| Symptom | Phase 1 (catalog) | Phase 2 (grants) | Phase 3 (data) | Root cause |
|---|---|---|---|---|
| `hermes memory status` says "available ✓" but writes fail | ✓ | NONE | `InsufficientPrivilege` | `pg_restore` lost grants; table owner ≠ Hermes role |
| `hermes memory status` says "not available ✗" | ✗ (connect error) | — | — | Wrong host/port/password or DB unreachable |
| `hermes memory status` says "available ✓" but `pg_search` returns empty | ✓ | ✓ | ✓ (0 results) | Legitimately no matching memories |
