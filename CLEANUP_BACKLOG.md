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

A2 **landed** 2026-06-27 (`_coerce_uuid` deduped into
`core/mcp_tools/_helpers.py:coerce_uuid`, 22 copies removed). A1 and A3 verified
still pending as of 2026-06-27 (stub tools present, both dead routers present).

### A1. Delete 4 stub MCP tools (product-surface change, 46 → 42 tools)

Files:

- `gdx_dispatch/core/mcp_tools/customer_lookup.py`  (`customer.lookup`)
- `gdx_dispatch/core/mcp_tools/invoice_query.py`    (`invoice.query`)
- `gdx_dispatch/core/mcp_tools/event_emit.py`       (`event.emit`)
- `gdx_dispatch/core/mcp_tools/job_create.py`       (`job.create`)

Why: handlers return `{"_stub": True}` / `{"_staged": True}` and are superseded
by fuller tools (`customers.detail`, `invoices.list`, real `job` create path).
Only their own unit tests reference them.

Caveat: these are LIVE, AI-callable MCP tools (part of the registered 46). An
agent instructed to call `customer.lookup` by name would fail after removal.
Confirm no saved prompt / agent config / frontend references the names before
deleting. Also remove the matching `from . import customer_lookup, ...` entries
in `core/mcp_tools/__init__.py` and the dead-stub `if db is None:` branches in
any surviving tool. ~257 lines + tests.

### A2. Dedup `_coerce_uuid` across ~22 mcp_tools files — ✅ DONE (2026-06-27)

Landed: unified helper now lives in `core/mcp_tools/_helpers.py` as the public
`coerce_uuid` (a behavioral superset of the three prior variants — accepts
`None`/empty, returns `None` on any invalid input). All 22 per-module
`_coerce_uuid` defs removed and call sites point at the shared helper; orphaned
`from uuid import UUID` imports dropped where no longer used. Verified via
app-import (46 MCP tools still register) + 51 tool tests green.

### A3. Delete `modules/locations` + `modules/notifications` routers (needs test surgery)

Files:

- `gdx_dispatch/modules/locations/router.py`     (live: `core/locations.py`, mounted app.py:779)
- `gdx_dispatch/modules/notifications/router.py`  (live: `routers/notifications.py`, mounted app.py:480)

Both are unmounted duplicates. NOT a clean delete:

- `test_22_locations.py` — 9 tests exercise `modules.locations.models` (kept) plus
  one (`test_location_requires_auth`) that imports the dead router.
- `test_28_notifications.py` — all tests build a FastAPI app from the dead
  `modules.notifications.router` and hit it via TestClient.

Keep each module's `models.py` (ORM-registered). Either rewrite the tests
against the live routers or delete the router-specific tests. Verify the live
routers cover the same behavior first. ~480 lines.

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
