-- Verify a hermes-postgres-kanban install by listing tasks.
--
-- Usage: PGPASSWORD=*** psql -h <host> -p <port> -U <user> -d <db> -f verify-kanban.sql

\echo '== hermes_kanban schema present? =='
SELECT EXISTS (
    SELECT 1 FROM information_schema.schemata WHERE schema_name = 'hermes_kanban'
) AS schema_present;

\echo '== Tables =='
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'hermes_kanban' ORDER BY table_name;

\echo '== Tenants =='
SELECT id, slug, display_name, created_at FROM hermes_kanban.tenants ORDER BY id;

\echo '== Tasks by status =='
SELECT status, COUNT(*) AS n FROM hermes_kanban.tasks GROUP BY status ORDER BY status;

\echo '== Recently changed =='
SELECT id, title, status, priority, updated_at
FROM hermes_kanban.tasks ORDER BY updated_at DESC LIMIT 5;
