# GDX Build Rules — Every New Endpoint/Router/Feature

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
- ALWAYS add the module key to `AVAILABLE_MODULES` in `gdx_dispatch/core/modules.py`

## Import Safety — Verify Before Commit
- ALWAYS verify imported packages exist in `gdx_dispatch/requirements.txt`
- Commonly missed: `werkzeug`, `python-multipart`, `prometheus_client`, `weasyprint`, `google-api-python-client`
- Routers using `UploadFile` or `Form` require `python-multipart`
- Register new routers in `app.py` with `try/except` that LOGS (not silently empty router)
- Test the import after deploy: `docker exec <app> python -c "from gdx_dispatch.routers import <mod>"`
- WHY: missing `python-multipart` once silently killed 17 mobile endpoints.

## Tenant Isolation — Three Planes

See `ARCHITECTURAL_STATE.md` (top-level) for the canonical picture. Every table lives in exactly one plane; the isolation rule is different in each.

### Tenant plane (per-tenant Postgres)
Tables: customers, jobs, invoices, documents, technicians, estimates, notes, leads, catalog, parts, photos, signatures, equipment, schedules.

- ALWAYS `Depends(get_tenant_db)` — the connection is the isolation boundary.
- DO NOT add `WHERE tenant_id = :tid` / `WHERE company_id = :tid` filters. Redundant and breaks on NULL (caused the 2026-04-22 document failure; same pattern as Flask bug fixed 2026-03-29 — it keeps recurring).
- DO NOT add `tenant_id` / `company_id` columns to new tenant-plane models. Redundant and misleading.
- DO NOT add RLS policies on tenant-plane tables — no-op in db-per-tenant.
- Read `request.state.tenant["id"]` only as a *value* (audit logs, R2 key prefixes, log lines, control-plane FKs).
- Every unit of work (request OR Celery task) must open its own `get_tenant_db` session. Per-tenant engines are cached in `engine_registry`.

#### Adding columns / tables to tenant-plane models
`TenantBase.metadata.create_all()` runs at signup and creates *new tables* on demand. It does NOT add new columns to existing tables. So every column you add to an existing tenant-plane model silently drifts on every long-running tenant DB until someone repaves it. Symptom: `psycopg2.errors.UndefinedColumn` 500s on whichever endpoint touches the new column, only on tenants that existed before the column was added.

After merging any tenant-plane model change, run the non-destructive sync tool to bring every tenant DB up to the model:

    docker exec docker-app-1 python -m gdx_dispatch.tools.sync_tenant_db --all-tenants            # dry-run
    docker exec docker-app-1 python -m gdx_dispatch.tools.sync_tenant_db --all-tenants --apply    # apply

The tool only does *additive* DDL: `add_table`, `add_column`, `add_index`, `add_constraint` (CHECK only). It refuses to drop, change types, or change nullability — those are deliberate human decisions, not auto-fixes. It prints a per-tenant report of what was applied and what was skipped (so you can hand-write the risky ALTERs if the model intends them). Idempotent: re-running on a synced DB is a no-op.

Use `pave_tenant_db.py` only when the diff is too tangled to apply additively (large type migrations, FK restructures with existing rows). It DROP SCHEMA + reloads — destructive, slow, last resort.

### Control plane (shared `gdx_control`)
Tables: tenants, memberships, tenant_module_grants, billing_plan, metering_usage, notification_template (if shared), tenant_relationships, cross_tier_module_grants, audit aggregation.

- ALWAYS `Depends(get_control_db)`.
- Every tenant-scoped control-plane table MUST have:
  - `tenant_id` / `company_id` column, `NOT NULL`.
  - RLS enabled, SELECT policy `USING (tenant_id = current_setting('app.tenant_id')::text)`.
  - RLS `WITH CHECK (...)` on INSERT and UPDATE for write-capable roles.
- Every `get_control_db` session MUST call `set_session_role(tenant_id=..., principal_role=..., ...)` immediately after open — RLS cannot enforce without the GUCs.
- App-level filters (`.where(Model.tenant_id == tid)`) are allowed as readability sugar but are NOT the isolation boundary — RLS is. Do not rely on app filters for security.

### Commerce plane (shared B2B data)
Tables: dealer_orders, wholesale catalog items, pricing_tier, channel_analytics, distributor_analytics, cross-tier documents — any table with TWO tenant IDs on the same row.

- Shared DB by design. One row legitimately visible to two tenants.
- RLS policies reference all party columns:
  - SELECT: `USING (current_setting('app.tenant_id')::text IN (supplier_tenant_id, dealer_tenant_id))`
  - WITH CHECK on writes prevents forging the counterparty.
- Same session-role requirement as control plane.

## AI Access — Three Layers

Any AI-driven read or write uses three independent enforcement layers:

1. **Tool layer** — narrow, typed Python functions. Never free SQL. Tools own validation, audit, idempotency.
2. **Postgres role layer** — `gdx_ai_readonly` (SELECT only) or `gdx_ai_write` (explicit column grants). Never `ALL PRIVILEGES`.
3. **RLS layer** — mandatory on control + commerce planes; WITH CHECK clauses required for any table AI can write to.

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
- Fixtures MUST create `tenant_module_grants` + `company_module_grants`
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
