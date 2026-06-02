-- 000_grant_ddl_to_hermes.sql
--
-- Reassign ownership of agent_memory from `postgres` (or whoever the
-- current owner is) to `hermes` so the hermes role can perform DDL
-- on the table — needed for the migration in 001.
--
-- Run as a superuser (e.g. psql -U postgres -d hermes) ONCE.
--
-- Why ownership instead of GRANT?
-- PostgreSQL does NOT support GRANT ... ALTER, DROP ON TABLE. Those
-- privileges are ownership-gated, not ACL-gated. The only standard
-- table-level privileges you can GRANT are:
--   SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, MAINTAIN
-- (verified via aclexplode on the table's relacl). For DDL, ownership
-- is the only path. This file transfers ownership atomically.
--
-- This file is idempotent: re-running is a no-op.

ALTER TABLE agent_memory OWNER TO hermes;
