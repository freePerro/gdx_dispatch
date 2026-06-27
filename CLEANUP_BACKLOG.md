# Repo TODO / cleanup backlog

Single source of truth for outstanding work markers in the repo. Two parts:

- **Part A — Cleanup backlog**: over-engineering-audit items that are judgment
  calls / need care. The clean dead-code deletions already landed on the
  `cleanup` branch (commits `ec63ecc`, `3773857`, `6145b83`, merged to main).
- **Part B — Inline code TODOs**: integration hooks and deferred features that
  live as `TODO` comments in the source. Listed here so there's one place to
  look. Delete each entry as it lands.

Last refreshed 2026-06-27. Verify file paths before acting — see the recall
note about checking that named files still exist.

## Part A — Cleanup backlog

All three items **landed** 2026-06-27 (A1 stub-tool deletion, A2 `_coerce_uuid`
dedup, A3 dead-router deletion — PRs #70, #69, #72 merged to main). Part A is
complete; the per-item ✅ DONE notes below are kept as a record.

### A1. Delete 4 stub MCP tools — ✅ DONE (2026-06-27), 46 → 42 tools

Deleted `customer_lookup.py` (`customer.lookup`), `invoice_query.py`
(`invoice.query`), `event_emit.py` (`event.emit`), `job_create.py`
(`job.create`) plus their four `test_mcp_tools_*` unit tests, and removed the
matching entries from `core/mcp_tools/__init__.py` (imports + docstring).

Pre-deletion reference sweep: the four tool-name strings appear elsewhere only
as test fixtures / docstring examples (each builds its own `ToolDescriptor`) —
no frontend, config, or saved-prompt invokes the live tools. No surviving tool
had an `if db is None:` dead-stub branch. Verified: app-import registers 42
tools (was 46), the four names are gone, ruff clean. `EXPECTED_MIN_TOOLS = 35`
floor still satisfied.

### A2. Dedup `_coerce_uuid` across ~22 mcp_tools files — ✅ DONE (2026-06-27)

Landed: unified helper now lives in `core/mcp_tools/_helpers.py` as the public
`coerce_uuid` (a behavioral superset of the three prior variants — accepts
`None`/empty, returns `None` on any invalid input). All 22 per-module
`_coerce_uuid` defs removed and call sites point at the shared helper; orphaned
`from uuid import UUID` imports dropped where no longer used. Verified via
app-import (46 MCP tools still register) + 51 tool tests green.

### A3. Delete `modules/locations` + `modules/notifications` routers — ✅ DONE (2026-06-27)

Deleted both unmounted router duplicates; kept each module's `models.py`. Test
surgery resolved by comparing each dead router against its live counterpart:

- **locations** — live `core/locations.py` (mounted app.py:769) guards every
  route with `get_current_user` and already has `test_locations.py` coverage.
  `test_22_locations.py`'s 9 model tests are untouched; its lone router test
  (`test_location_requires_auth`) was **rewritten** to inspect the live router.
- **notifications** — live `routers/notifications.py` exposes a *different* API
  (settings / templates / send / history / count), NOT the dead router's
  `/devices`, `/preferences`, `/read-all`, `/unread-count`. No 1:1 rewrite
  target, so `test_28_notifications.py` was **deleted** wholesale.

Follow-up — ✅ DONE (2026-06-27): `tests/test_notifications.py` now covers both
the kept `modules/notifications/models.py` (8 direct-ORM tests: CRUD, defaults,
tenant isolation, `DeviceToken.token` unique constraint) and the live
`routers/notifications.py` (15 functional `TestClient` tests: settings get/patch,
template seed/create/validation, send happy-path/manual-override/404/422 channel,
history pagination, in-app count/list/mark-read with user+broadcast scoping, and
the `require_module("communications")` gate). 23 tests, green.

## Part B — Inline code TODOs

Grouped by kind. These are `TODO` comments in source, surfaced here for
visibility. Each links back to the line that owns the work.

### B1. Unwired-by-design (additive readiness — waiting on a flag / sprint merge)

These modules exist and are tested but are intentionally NOT mounted in
`gdx_dispatch/main.py` until the owning sprint integrates. No action unless the
feature is being turned on.

- **SPIFFE (SS-32)** — `core/spiffe/__init__.py:10`, `core/middleware/spiffe_auth_middleware.py:36`.
  Mount middleware (BEFORE the SS-7 auth middleware) + include `routers.spiffe_admin`
  when the SPIFFE-enabled flag flips.
- **MCP registry / invoke (SS-19)** — `core/mcp_registry.py:7`, `core/mcp_tools/__init__.py:22`,
  `core/mcp_invoke.py:29`. Register the `routers/mcp_registry.py` router in main.py;
  ensure the mcp_tools package is imported on app start; swap `_dummy_log_execution`
  for the real SQLAlchemy writer once the SS-19 migration lands (shape is stable).
- **API metadata (SS-25)** — `routers/api_metadata.py:16`. `main.py` must
  `include_router(api_metadata.router)` at integration time (no auto-mount).
- **API versioning** — `core/middleware/api_versioning.py:19`. Self-referential
  TODO at the bottom of the module; resolve when versioning is switched on.

### B2. Real deferred features (genuine future work)

- **Payroll adapters** — `modules/payroll/router.py:5`. Gusto / QBO Payroll
  adapters in `modules.payroll.adapters` are TODO stubs.
- **Phone.com contact PATCH (Phase 2)** — `modules/phone_com/push_contacts.py:12`.
  Create path shipped; updating an already-pushed contact (PATCH) + per-tenant
  work cap are the Phase-2 follow-up.
- **Audit Log Viewer (SS-28)** — `frontend/src/views/AuditLogViewer.vue:4,7,266`.
  Component not routed (mount at `/admin/audit-log`, gate behind tenant-admin
  cap); backend `/api/admin/audit-log` and `.csv` export not built. Expected
  shape: `{ total, offset, limit, rows[], chain_integrity: {valid, break_at} }`.

### B3. Test placeholders (intentional — assert 404 until implemented)

Not bugs — these tests pin endpoints as not-yet-implemented. When the feature
lands, flip the expected status and drop the `# TODO: implement` comment.

- `tests/test_40_ai_quote.py` — `/api/ai/quote-generate|history|feedback`, `/api/catalog/items`.
- `tests/test_39_tax_settings.py` — tax settings / jurisdictions / rate lookup /
  tax-exempt / sales-tax report.

## Reference

`git log` on `cleanup` for the completed passes; each commit carries a
VERIFICATION MANIFEST. Deleted code is recoverable from history if a parked
feature (e.g. the SOC2/GDPR scaffolding from pass 1) resumes.
