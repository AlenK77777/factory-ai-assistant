-- Limited role that rebuilds the instruction index.
-- It owns the chunk tables and can create a replacement table.
-- It cannot change orders, defects, or ingest_runs.
--
-- Safe to run more than once. The name and password come from the environment
-- (INDEX_DB_USER / INDEX_DB_PASSWORD in .env, passed through docker-compose.yml).
--
-- Fresh database: this file is in db/init, which Docker runs in name order
-- (01, 02, 03, then this file) only when the data volume is empty.
-- Existing database: the volume is not recreated. Apply this file once, and
-- again whenever the password in .env changes:
--   docker compose exec db psql -U factory -d factory -f /docker-entrypoint-initdb.d/04_indexer_role.sql

\set ON_ERROR_STOP on

\getenv idx_user INDEX_DB_USER
\getenv idx_password INDEX_DB_PASSWORD
\getenv ro_user MCP_DB_USER

\if :{?idx_password}
\else
    DO $$ BEGIN
        RAISE EXCEPTION 'INDEX_DB_USER / INDEX_DB_PASSWORD are not set. Add them to .env, then run: docker compose up -d db';
    END $$;
\endif

SELECT format('CREATE ROLE %I LOGIN', :'idx_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'idx_user')
\gexec

SELECT format(
    'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
    :'idx_user', :'idx_password')
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'idx_user')
\gexec
SELECT format('GRANT USAGE, CREATE ON SCHEMA public TO %I', :'idx_user')
\gexec

-- Production tables stay with the owner. Take away anything this role might have.
SELECT format('REVOKE ALL ON TABLE orders, defects, ingest_runs FROM %I', :'idx_user')
\gexec

-- The model that built the live vectors. Queries read this, not the unsaved setting.
CREATE TABLE IF NOT EXISTS doc_index (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    embedding_model  TEXT    NOT NULL,
    dimensions       INTEGER NOT NULL
);

INSERT INTO doc_index (id, embedding_model, dimensions)
VALUES (1, 'qwen3-embedding:0.6b', 1024)
ON CONFLICT (id) DO NOTHING;

SELECT format('ALTER TABLE doc_chunks OWNER TO %I', :'idx_user')
\gexec
SELECT format('ALTER TABLE doc_index OWNER TO %I', :'idx_user')
\gexec

SELECT format('GRANT SELECT ON doc_chunks, doc_index TO %I', :'ro_user')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user')
\gexec

-- Tables this role creates later (the next index) are readable by the MCP role.
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON TABLES TO %I',
    :'idx_user', :'ro_user')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user')
\gexec

\echo Indexer role ready: :idx_user
