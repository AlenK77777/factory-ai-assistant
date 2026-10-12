-- Read-only database role for the MCP server (least privilege).
--
-- Safe to run any number of times: it creates the role if it is missing,
-- otherwise it only resets the password and re-applies the grants.
-- The role name and password come from the container environment
-- (MCP_DB_USER / MCP_DB_PASSWORD, passed from .env by docker-compose.yml).
--
-- Fresh volume: runs automatically after 01_schema.sql.
-- Existing volume (PowerShell, from the project root):
--   docker compose up -d db
--   docker compose exec db psql -U factory -d factory -f /docker-entrypoint-initdb.d/02_readonly_role.sql

\set ON_ERROR_STOP on

\getenv ro_user MCP_DB_USER
\getenv ro_password MCP_DB_PASSWORD

\if :{?ro_password}
\else
    DO $$ BEGIN
        RAISE EXCEPTION 'MCP_DB_USER / MCP_DB_PASSWORD are not set in the container. Add them to .env, then run: docker compose up -d db';
    END $$;
\endif

-- \gexec runs the generated statement; %I and %L quote the name and password safely.
SELECT format('CREATE ROLE %I LOGIN', :'ro_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user')
\gexec

SELECT format(
    'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
    :'ro_user', :'ro_password')
\gexec

-- Every session of this role starts read-only (extra safety on top of the grants below).
SELECT format('ALTER ROLE %I SET default_transaction_read_only = on', :'ro_user')
\gexec

-- Read access only: connect, see the schema, SELECT from tables.
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'ro_user')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'ro_user')
\gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', :'ro_user')
\gexec

-- Tables created later by the owner (e.g. doc_chunks in Step 3) are readable too.
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO %I', :'ro_user')
\gexec

\echo Read-only role ready: :ro_user
