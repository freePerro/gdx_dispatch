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
- **The Alembic base** (`gdx_dispatch/control/models.py` — `Tenant`, `TenantSettings`, the game tables — plus everything migration 001's baseline creates) — owned by `alembic upgrade head`, also run by the entrypoint. `routers/admin_db.py` suppresses these tables from its ORM-drift check because `compare_metadata` runs against `TenantBase` only.

#### Adding a column to an existing `TenantBase` table

Write an Alembic migration (`/migrate`); it must run on both SQLite and Postgres. There is no additive sync tool — `gdx_dispatch.tools.sync_tenant_db` never existed (checked 2026-09-06: no source file names it) — and the drift scanners (`tools/tenant_plane_schema_drift.py`, `tools/tenant_schema_drift_check.py`) only *detect*. `tools/pave_tenant_db.py` is the last resort: it DROP SCHEMAs the application database and reloads it from the dump it takes first. Destructive; requires `--yes`.

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
