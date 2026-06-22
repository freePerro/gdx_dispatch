# Cleanup backlog

Remaining over-engineering-audit items not yet done. The clean dead-code
deletions are already on the `cleanup` branch (commits `ec63ecc`, `3773857`,
`6145b83`). These three are judgment calls / need care — delete each section
as it lands.

## 1. Delete 4 stub MCP tools  (product-surface change, 46 → 42 tools)
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

## 2. Dedup `_coerce_uuid` across ~22 mcp_tools files  (~140 lines)
The same 6–8 line UUID-coercion helper is copy-pasted byte-for-byte into ~22
files under `gdx_dispatch/core/mcp_tools/` (catalog_*, documents_*, estimates_*,
email_*, etc). Move one copy to a shared `core/mcp_tools/_helpers.py` (or
`mcp_registry`) and import it. Mechanical but touches 22 files — do it as its
own commit, run app-import + the mcp tool tests after.

## 3. Delete `modules/locations` + `modules/notifications` routers  (needs test surgery)
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

---
Reference: `git log` on `cleanup` for the completed passes; each commit carries
a VERIFICATION MANIFEST. Deleted code is recoverable from history if a parked
feature (e.g. the SOC2/GDPR scaffolding from pass 1) resumes.
