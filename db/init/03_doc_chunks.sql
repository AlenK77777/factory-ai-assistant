-- Chunks of the work instructions with their embeddings (RAG).
--
-- Safe to run any number of times.
-- Fresh volume: runs automatically after 01_schema.sql and 02_readonly_role.sql.
-- Existing volume (PowerShell, from the project root):
--   docker compose exec db psql -U factory -d factory -f /docker-entrypoint-initdb.d/03_doc_chunks.sql

\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per chunk; rag/index_docs.py replaces all rows on every run.
-- vector(768) must match the embedding model (nomic-embed-text).
-- No vector index: for a few dozen chunks an exact scan is fast and always exact.
CREATE TABLE IF NOT EXISTS doc_chunks (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source      TEXT        NOT NULL,
    chunk_index INTEGER     NOT NULL,
    content     TEXT        NOT NULL,
    embedding   vector(768) NOT NULL,
    UNIQUE (source, chunk_index)
);

-- Read access for the MCP server's read-only role, if it exists already.
-- (On a fresh volume 02 runs first; if the role is created later, 02 grants SELECT on all tables.)
\getenv ro_user MCP_DB_USER
\if :{?ro_user}
SELECT format('GRANT SELECT ON doc_chunks TO %I', :'ro_user')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user')
\gexec
\endif

\echo doc_chunks ready
