-- gdx_ai_readonly and gdx_ai_write: the Postgres role layer of AI access
-- (gdx_dispatch/docs/BUILD_RULES.md, "AI Access — Three Layers"). This file
-- encodes the contract in test_gdx_ai_readonly_role.py and
-- test_gdx_ai_write_role.py and nothing wider; CI's ai-roles job applies it and
-- runs both (GDXA-1564). As of 2026-10-18 nothing in the app connects as either
-- role, and the one AI write tool (core/mcp_tools/mark_customer_contacted.py)
-- could not run under gdx_ai_write: it loads the whole customer row and its
-- ORM save also updates `version`. Wider grants for a real caller belong here
-- and in those tests together.
--
-- Run it with psql 15 or newer, as a superuser, connected to the tenant
-- database, after the schema is built (pave or create_all):
--
--   GDX_AI_READONLY_PASSWORD=... GDX_AI_WRITE_PASSWORD=... \
--     psql "$SUPERUSER_URL" -f gdx_dispatch/tools/gdx_ai_roles.sql
--
-- Passwords come from those two variables and are never written here. Both
-- must be set and non-empty, or nothing changes. Re-running it is safe: it
-- creates a missing role, resets both passwords and attributes, and puts the
-- customers grants back to what is below (both test files read them back).
-- Grants on other tables are not touched, so a role that already holds more
-- keeps it.
--
-- Only what the two tests need is granted:
--   gdx_ai_readonly  SELECT on customers.
--   gdx_ai_write     SELECT (id) on customers, so it can find a row and filter
--                    by id; UPDATE (notes_appended), the column the AI write
--                    tier may update (tenant_models.py, Customer.notes_appended).
--                    No INSERT, no DELETE, no other column.
-- Both get CONNECT on this database and USAGE on schema public.

\set ON_ERROR_STOP on

\getenv readonly_password GDX_AI_READONLY_PASSWORD
\getenv write_password GDX_AI_WRITE_PASSWORD
\if :{?readonly_password}
\else
  \set readonly_password ''
\endif
\if :{?write_password}
\else
  \set write_password ''
\endif
SELECT :'readonly_password' = '' AS readonly_missing,
       :'write_password' = '' AS write_missing \gset
\if :readonly_missing
  DO $$ BEGIN RAISE EXCEPTION 'GDX_AI_READONLY_PASSWORD is not set; nothing was changed'; END $$;
\endif
\if :write_missing
  DO $$ BEGIN RAISE EXCEPTION 'GDX_AI_WRITE_PASSWORD is not set; nothing was changed'; END $$;
\endif

BEGIN;

-- The ALTER ROLE statements below carry the passwords in clear text, and a
-- server running with log_statement = 'ddl' or 'all' would write them to its
-- log. This turns that off for this transaction only (superuser only). It
-- does not cover log_min_error_statement: if an ALTER ROLE itself fails, the
-- server logs that statement, password included.
SET LOCAL log_statement = 'none';

SELECT 'CREATE ROLE gdx_ai_readonly'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gdx_ai_readonly') \gexec
SELECT 'CREATE ROLE gdx_ai_write'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gdx_ai_write') \gexec

-- NOINHERIT: a role membership granted later cannot quietly widen either one.
ALTER ROLE gdx_ai_readonly WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD :'readonly_password';
ALTER ROLE gdx_ai_write WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD :'write_password';

GRANT CONNECT ON DATABASE :"DBNAME" TO gdx_ai_readonly, gdx_ai_write;
GRANT USAGE ON SCHEMA public TO gdx_ai_readonly, gdx_ai_write;

-- Table-level REVOKE ALL also drops column-level grants, so customers comes
-- out with exactly the grants that follow. It only revokes what this user
-- granted: run it as the same superuser each time.
REVOKE ALL ON TABLE public.customers FROM gdx_ai_readonly, gdx_ai_write;
GRANT SELECT ON TABLE public.customers TO gdx_ai_readonly;
GRANT SELECT (id) ON TABLE public.customers TO gdx_ai_write;
GRANT UPDATE (notes_appended) ON TABLE public.customers TO gdx_ai_write;

COMMIT;
