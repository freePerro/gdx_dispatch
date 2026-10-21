# GDX Build Rules — Every New Endpoint/Router/Feature

**Status: CURRENT** — four unfollowable instructions corrected 2026-09-01
(they named a doc and a tool that do not exist). The SQL-portability,
silent-failure and audit rules below were verified against code and stand.

These rules exist because specific patterns kept causing production bugs. Non-negotiable.

## SQL Portability — No Database-Specific Functions
- NEVER `gen_random_uuid()::text` — use Python `str(uuid4())` as a bind parameter
- NEVER `NOW()` in SQL — use Python `datetime.now(timezone.utc)` as `:param`
- NEVER `::text` / `::int` Postgres cast syntax — convert in Python
- NEVER `COALESCE(bool_col, 1) = 1` — use `COALESCE(bool_col, true) = true`
- NEVER `ON CONFLICT ... DO NOTHING` on SQLite unless the unique constraint exists
- WHY: tests use SQLite, prod uses PostgreSQL. All SQL must work on both.
- SCAN: `grep -rn 'gen_random_uuid\|NOW()\|::text\|::int' gdx_dispatch/routers/ gdx_dispatch/core/` must return 0 hits in new code.

## Error Handling — No Silent Failures
- NEVER bare `except Exception:` that swallows errors
- ALWAYS `logging.getLogger(__name__).exception("context_message")` in every except block
- ALWAYS re-raise or return a meaningful error — never just `pass`
- NEVER catch broad exceptions around imports — log and let them surface
- WHY: 94 silent import failures in app.py went undetected for weeks.

## Audit Logging — Every Mutation Gets Logged
- ALWAYS `log_audit_event()` or `log_audit_event_sync()` on create/update/delete
- Include: `tenant_id`, `user_id`, `action`, `entity_type`, `entity_id`, `details`, `request`
- WHY: SOC 2 requires immutable audit trail.

## Module Gating — Every Router Gets Gated
- ALWAYS `dependencies=[Depends(require_module("module_key"))]` on router
- Use `require_role("admin", "owner")` for admin-only endpoints
- ALWAYS add the module key to `MODULES` in `gdx_dispatch/core/modules.py:17`
  (this line said `AVAILABLE_MODULES` until 2026-09-01; no such symbol exists)

## Import Safety — Verify Before Commit
- ALWAYS verify imported packages exist in `gdx_dispatch/requirements.txt`
- Commonly missed: `werkzeug`, `python-multipart`, `prometheus_client`, `weasyprint`, `google-api-python-client`
- Routers using `UploadFile` or `Form` require `python-multipart`
- Register new routers in `app.py` with `try/except` that LOGS (not silently empty router)
- Test the import after deploy: `docker exec <app> python -c "from gdx_dispatch.routers import <mod>"`
- WHY: missing `python-multipart` once silently killed 17 mobile endpoints.

## Database — One Database, Two Ways a Table Gets Created

Single-tenant, forever (`CLAUDE.md` § *Project map*): one Postgres database per
install, and the connection is the isolation boundary. There is no control
plane, no commerce plane, no row-level security and no per-tenant database
resolution. (The "three planes" section that stood here until 2026-09-06
described the shared-database SaaS this deployment never ran.)

- ALWAYS `Depends(get_db)`. `get_tenant_db` is the same generator under its older name.
- DO NOT add `WHERE tenant_id = :tid` / `WHERE company_id = :tid` filters. Redundant, and `IS NOT NULL` variants hide rows (the 2026-04-22 documents bug). `tools/tenant_plane_redundant_filter_scan.py` flags them.
- DO NOT add `tenant_id` / `company_id` columns to new models. Redundant and misleading.
- Read `request.state.tenant["id"]` — or call `core.tenant.company_id()` — only as a *value* (audit logs, storage key prefixes, log lines).
- Every unit of work (request OR Celery task) opens its own session (`get_db` / `SessionLocal`). There is one engine.

### Two metadata objects, two creation paths

- **`TenantBase`** (`models/tenant_models.py`, registry in `models/__init__.py`) — every business table. Created by `TenantBase.metadata.create_all()`, which `tools/bootstrap_app.create_orm_tables()` runs from the container entrypoint on every boot, *before* Alembic. **`create_all` creates missing tables only; it never adds a column to an existing table.**
- **The Alembic base** (`gdx_dispatch/core/tenant_settings.py` — `Tenant`, `TenantSettings`, the game tables — plus everything migration 001's baseline creates; moved out of a `control/` package 2026-09-06) — owned by `alembic upgrade head`, also run by the entrypoint. `routers/admin_db.py` runs its ORM-drift check (`compare_metadata`) against both metadata objects together, so drift on these tables is reported like drift on `TenantBase`; it suppresses only `server_errors`, which no model on either base declares (GDXA-1537, 2026-10-18; it compared `TenantBase` alone before, which hid `game_events.created_by_user_id`, fixed by migration 094). A nullability mismatch is severity `mismatch` and sets the panel's verdict to drift.

#### Adding a column to an existing `TenantBase` table

Write an Alembic migration (`/migrate`); it must run on both SQLite and Postgres.
There is no additive sync tool — `gdx_dispatch.tools.sync_tenant_db` never existed (checked 2026-09-06: no source file names it) — and the drift scanners (`tools/tenant_plane_schema_drift.py`, `tools/tenant_schema_drift_check.py`) only *detect*.
`tools/pave_tenant_db.py` is the last resort: it DROP SCHEMAs the application database and reloads it from the dump it takes first.
Destructive; requires `--yes`, and the database URL's role must be a superuser: the data dump is taken with `--disable-triggers`, and its reload's `ALTER TABLE ... DISABLE TRIGGER ALL` needs superuser on any table with a foreign key, so any other role is refused before anything is dropped.
It refuses, with nothing dropped, while any other client is connected to the database: from the `DROP` to the end of the reload every table is empty, and a row `app` (its celery workers and beat included; beat drains outboxes every minute) or `plugin-host` writes into it aborts the reload's `COPY` after the `DROP` (GDXA-1811).
It checks again inside the `DROP`'s own transaction, takes the `DROP`'s locks under a `lock_timeout`, and fails the final verify if a client is connected when it runs (a snapshot: one that connected, wrote and left in between is not seen).
So stop those services first and run it as a module in a one-off app container, both with the compose invocation of the deployment that owns the database — for the self-hosted stack, `update.sh`'s `COMPOSE` (`docker compose -p gdx --env-file ./.env` plus one `-f` per `COMPOSE_FILE` entry in that `.env`, or `-f gdx_dispatch/docker/docker-compose.yml` when it has none, run from the directory holding it): `stop app plugin-host backup` (celery runs inside app), then `run --no-deps --entrypoint sh app -c 'set -a; . "$GDX_RUNTIME_ENV_FILE"; set +a; exec python -m gdx_dispatch.tools.pave_tenant_db --yes'`. The `sh -c` loads the runtime env `entrypoint.sh` would have; `DATABASE_URL` lives only there.
The demo stack defines only `app` and `plugin-host`; the base file adds a `backup` sidecar that also connects.
A `stop` aimed at the wrong project stops nothing and still exits 0; the preflight then refuses, naming the clients still connected (`--help` prints the commands).
Never with `--rm` (nor `docker run --rm`), which takes the `/tmp/<dbname>_full.sql` backup away with the container; `docker compose start` the services afterwards.
`--terminate-others` sets `CONNECTION LIMIT 0` for the run (restored on exit, and the restore command is printed first) and terminates the other clients, but a connection limit does not hold off a superuser, and every compose service connects as `POSTGRES_USER`, a superuser: a service left running reconnects and is refused again, so the flag does not replace stopping them.
It rebuilds all three kinds of table a tenant database holds: `create_all()` for `TenantBase`, `alembic upgrade head` for the Alembic base (`alembic_version`, `tenants`, `tenant_settings`, `server_errors`, the game tables), and a pre-drop `pg_dump --schema-only` replay for the plugin tables `schema_reconcile` creates at boot, plus the non-table objects nothing else owns (the `audit_logs` immutability guard among them).
An object of a *kind* it has no DDL for — a domain type, an RLS policy, a rule, a collation, a text-search object — refuses the run before anything is dropped, but that refuse-list is enumerated in `_UNSUPPORTED_OBJECTS_SQL` rather than derived, so a schema-scoped kind PostgreSQL adds later is neither rebuilt nor refused until someone adds it there.
Nor is the pre-drop check a guarantee of success: a replay that fails, or a rebuilt schema still missing something, aborts **after** the `DROP`, preserving `/tmp/<dbname>_full.sql` and printing the `psql` commands that restore it, in order: empty the schema, reload the public schema's owner, ACL and comment, then the full backup.
It also refuses, with nothing dropped, a database whose `alembic_version` is not this image's head (rows reloaded at head would skip every data migration in between, and a table a migration dropped would come back), and a run with `TENANT_AI_READONLY_PASSWORD` or `TENANT_AI_WRITE_PASSWORD` set: there is no AI-role bootstrap, the module it once imported never existed.
Owners, `GRANT`s (the public schema's own `USAGE` to `PUBLIC` included), schema default privileges and `COMMENT`s are captured before the drop, put back after the rebuild (default privileges straight after the `CREATE SCHEMA` and the schema's extensions, so the tables the rebuild creates get them and the extensions' members do not) and checked in the final verify.
These refuse the run before the drop: a comment on an object kind it cannot comment or on an index or constraint, whose name the rebuild does not keep; privileges on an extension member that re-creating the extension would reset (members are not compared in the final verify, nor is a re-created extension's owner, and the check misses at least one case: a member the extension's own script grants on, created while the schema had default privileges, loses those grants unnoticed); default privileges `FOR ROLE` a role other than the paving one; an extension at a version this server cannot install (extensions are re-created at the version they were).
Objects the rebuild adds are neither re-owned nor compared: they stay owned by the paving role.

## AI Access — Three Layers

Any AI-driven read or write uses three independent enforcement layers:

1. **Tool layer** — narrow, typed Python functions. Never free SQL. Tools own validation, audit, idempotency.
2. **Postgres role layer** — `gdx_ai_readonly` (SELECT only) or `gdx_ai_write` (explicit column grants). Never `ALL PRIVILEGES`.
3. **Audit layer** — every AI write goes through `log_audit_event()` like any other mutation. (An RLS layer was listed here until 2026-09-06; no migration in this repo creates a policy, and single-tenant isolation is the connection, not RLS.)

Tool blast-radius classes: Green (apply directly), Yellow (AI proposes → UI confirms → apply), Red (explicit admin gate or never). Prompt text is NOT a security boundary.

## Input Validation — Never Trust User Input
- ALWAYS sanitize file names (`_sanitize_filename` pattern in uploads.py)
- ALWAYS limit sizes (`MAX_PHOTO_BYTES=10MB`, `MAX_DOCUMENT_BYTES=25MB`)
- ALWAYS validate MIME types
- Use Pydantic `Field(min_length=1)` for required strings, `Field(ge=0)` for amounts, `Field(gt=0)` for quantities
- NEVER interpolate user input into SQL — use `:bind_params`

## Vue Frontend — Component Rules
- ALWAYS `data-testid` on interactive elements
- ALWAYS handle API errors in composables — toast, no silent failures
- ALWAYS `useApiWithToast` for user-facing calls (auto 401/500/network)
- ALWAYS Pinia stores for shared state
- ALWAYS lazy-load non-critical routes in `router/index.js`

## Testing — Must Pass Both SQLite and PostgreSQL
- Fixtures MAY create `company_module_grants` (the only grants table; `tenant_module_grants` was removed 2026-09-03). With no rows, the gate seeds every module on first check.
- Fixtures MUST inject `request.state.tenant = {"id": "tenant-test"}` via middleware
- Fixtures MUST seed module grants for the module under test
- Fixtures MUST use `company_id` matching the tenant middleware
- NEVER `@pytest.mark.skip` to hide real failures — only infra deps
- Mark live-VPS tests with `@pytest.mark.e2e` or `@pytest.mark.load`
- ALWAYS `npx vitest run` after Vue changes — 0 failures required

## Agent Work — Verify Before Committing
- ALWAYS run full test suite after a background agent completes
- If failures increase, determine WHY before reverting:
  - Agent's code correct (security fix) but tests wrong → fix tests
  - Agent's code wrong → revert code
- NEVER weaken security to pass tests
- Signature-changing agent work MUST update callers
- Monitor agents at 15-min marks; kill if stuck

## GDX-Specific Patterns
- FastAPI: `app.include_router(router.router if hasattr(router, "router") else router)`
- Tenant ID (as a value, not an isolation filter): `request.state.tenant["id"]`
- User ID: `user.get("sub") or user.get("user_id") or "system"`
- Soft delete: `UPDATE ... SET deleted_at = :now` — never `DELETE FROM`
- Passwords: support bcrypt (`$2b$`) and werkzeug (`pbkdf2:`, `scrypt:`)
- Booleans: Postgres `boolean` — use `true`/`false`, never `1`/`0`
