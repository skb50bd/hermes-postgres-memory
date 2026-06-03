# pgvector connectivity probe

Use this when `hermes postgres-memory status` or provider availability is ambiguous.
It re-reads `~/.hermes/.env`, requires `PG_MEM_DB_CONN_STR`, and checks the
actual permissions the plugin needs.

```bash
python3 - <<'PY'
from pathlib import Path
import os
import psycopg2
from psycopg2.extensions import make_dsn

env = Path.home() / '.hermes' / '.env'
if env.exists():
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

dsn = os.environ.get('PG_MEM_DB_CONN_STR', '').strip()
if not dsn:
    raise SystemExit('PG_MEM_DB_CONN_STR is required')

conn = psycopg2.connect(make_dsn(dsn=dsn, connect_timeout=5, application_name='hermes-memory-probe'))
try:
    with conn.cursor() as cur:
        cur.execute('SELECT current_user, current_database()')
        print('connection:', cur.fetchone())
        cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
        print('pgvector:', cur.fetchone())
        cur.execute("SELECT to_regclass('public.agent_memory')")
        print('agent_memory:', cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM agent_memory WHERE is_active = TRUE")
        print('active rows:', cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM agent_memory WHERE vector_1024 IS NOT NULL")
        print('vector_1024 rows:', cur.fetchone()[0])
finally:
    conn.close()
PY
```

A green probe means the DSN works, pgvector is installed, the schema exists,
and the configured role can read the memory table.
