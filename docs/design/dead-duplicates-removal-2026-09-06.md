# Dead duplicates removal — 2026-09-06

Status: RELEASED v1.118.0 — MERGED #633 2026-09-07 (squash 3c0e7b9, stacked on #630); prod and demo rolled to 1.118.0 on 2026-09-07 ~02:36Z with the six-table recount at 0 rows on both immediately before; migrations 091+092 ran, alembic head 092 on both; walked on prod and demo (API, desktop and mobile customer pages, light and dark). Closes #458 #459 #480 #568 #569 #571 #572 #574 #595 #599. Migration 092.

## What already exists (do not rebuild)

Every path removed here is still served — by the handler that was **already
serving it**. FastAPI dispatches to the first `(method, path)` registration;
the copies deleted below were included later and never ran:

| Path | Serves today (unchanged) | Copy deleted |
|---|---|---|
| `GET /api/admin/permissions` | `routers/admin_ops.py::list_role_permissions` (admin-gated, real rows) | `routers/ui_compat.py` shim returning `{"items": [], "total": 0}` — the one pair where ui_compat *won*, so this is the only behaviour change: the read now returns real rows to admins |
| `GET /api/jobs/{id}/activity` | `routers/jobs.py` | `routers/activity.py::list_job_activity` |
| `POST /api/mobile/location` | `routers/tech_locations.py` | `routers/mobile.py::report_mobile_location` |
| `GET /api/ai/usage` | `core/ai_usage_logger.py` (durable table) — **now session-gated**, with `/usage/export`. **Two behaviour changes here, not one:** auth (ungated → session) *and* response shape (`requests/input_tokens/output_tokens/total_tokens/cost` → `totals/by_model/by_day` with `cost_usd`). No in-repo caller; prod nginx logs checked for external pollers before merge (see PR) | `core/ai_router.py::ai_usage` (in-process list, emptied on restart) |
| `GET/POST /api/purchase-orders`, `PATCH …/{id}`, `POST …/{id}/receive` | `routers/purchase_orders.py` | `routers/po_workflow.py` — whole router (all four routes shadowed) | <!-- link-ok: deleted 2026-09-06 -->
| `GET/POST /api/campaigns`, `POST …/{id}/send` | `routers/campaigns.py` | `modules/campaigns/router.py` (3 of 4 routes; `/stats` stays) |
| `GET /api/dispatch/locations` | `routers/tech_locations.py` | `modules/gps_dispatch/router.py::list_locations` |
| `GET/POST /api/fleet/vehicles` | `routers/fleet.py` | `modules/fleet/router.py` |
| `GET/POST /api/inventory/parts`, `GET …/low-stock` | `routers/inventory.py` | `modules/inventory/router.py` |
| `GET /api/timeclock/status`, `POST …/clock-in` | `routers/timeclock.py` | `modules/timeclock/router.py` |

The route-shadow baseline (`tools/route_shadow_baseline.txt`) drops from 21
pairs to the 2 that belong to #570 (`POST /api/customers/bulk-tag`,
`POST /api/jobs/{id}/line-items`), which are user-facing defects, not
duplicates, and are out of scope here.

## What was removed outright (no caller in either direction)

| Item | Issue | Evidence it was dead |
|---|---|---|
| `routers/booking.py` + `BookingRequest`/`BookingJob` models + the portal-side `POST /booking` write + `PortalBookingRequest` | #458 | zero Vue callers for any of the six routes (grep of `frontend/src` and every sibling repo); `booking_requests_router`, `booking_jobs_router`, `portal_booking_requests` all 0 rows on prod and demo | <!-- link-ok: deleted 2026-09-06 -->
| 11 `routers/mobile.py` routes: `/clock-status`, `/schedule`, `/my-jobs`, `/my-jobs/{id}`, `/jobs/{id}/checklist`, `/jobs/{id}/start`, `/timecard`, `/jobs/{id}/signature` + `/job/{id}/signature`, `/sync`, `/jobs/{id}/transition/{status}` | #480 | all 46 distinct `/api/mobile/*` call-site strings in `frontend/src` enumerated; none hits these; nothing in `plugin_host`, `plugin_api`, `services`, `core`, `tasks` or the plugins repo does either |
| `GET/POST /api/customers/{id}/communications` stubs + the Communications tab in `CustomerDetailView.vue` **and** `MobileCustomerDetailView.vue` | #459 | no communications model or table has ever existed; the GET returned a hardcoded `[]`, the POST 501'd |
| `tasks/email_poller.py` | #599 | registered with Celery, no beat entry, no `.delay`, no router; its INSERT named columns `inbound_emails` does not have; 0 rows ever | <!-- link-ok: deleted 2026-09-06 -->
| second `get_db_for_ai` in `routers/ai.py`, second `get_db_for_admin` in `modules/outlook/admin_settings_router.py`, `import get_db, get_db` | #595 | Python keeps the last definition; the surviving `ai.py` docstring described a control-plane / tenant-plane split that never existed and is rewritten |

## Tables dropped (migration 092)

`po_requests`, `po_request_lines`, `booking_requests_router`,
`booking_jobs_router`, `portal_booking_requests`. Counted on prod
(`gdx-db-1`) and demo (`gdx-demo-db-1`) on 2026-09-06: **0 rows in every
one**. None was created by a migration (`create_all` made them), so 092
guards each with `has_table`, counts all five before dropping any, and
refuses the whole migration if one holds a row (same contract as 088/091).
Rollback recreates all five empty.

**Deploy pre-flight (the adversarial audit's one operational risk):** the
writers for two of these tables still exist on prod until this ships
(`POST /portal/booking` for any portal token holder, `POST /api/booking/request`
for any staff session). A row landing between the count and the deploy turns
092's refusal into an app container that will not start. So the counts are
re-run immediately before `update.sh`, not trusted from this record:

```sql
select 'po_requests', count(*) from po_requests union all
select 'po_request_lines', count(*) from po_request_lines union all
select 'booking_requests_router', count(*) from booking_requests_router union all
select 'booking_jobs_router', count(*) from booking_jobs_router union all
select 'portal_booking_requests', count(*) from portal_booking_requests;
```

All five must read 0 on prod and on demo; otherwise export, empty, then deploy.

## Corrections to the issues as filed

- **#572 said to drop `technician_locations`. It stays.** `routers/gps.py`
  defines `TechnicianLocation` on that table and is wired with five live
  `/api/gps/*` routes. Only the shadowed `mobile.py` writer was removed. The
  table is 0 rows on prod and demo today, but it belongs to a live router.
- **#459 named one view; there were two.** `MobileCustomerDetailView.vue` had
  the same Communications tab reading the same stub.
- **`tests/test_marketing.py` tested the dead module handlers.** Its
  create/send/audit tests exercised `modules/campaigns/router.py`, not the
  `routers/campaigns.py` that serves. Those tests went with the dead code;
  the stats test now seeds the module's own tables directly. The canonical
  campaigns router has **no direct tests** — filed as #631, not fixed here.
- **Two mobile tests pinned the write gate through `/start`.** They now pin
  it through the notes write (`add_mobile_job_note`), a route the phone
  actually calls.

## Guards that ship with this

- `tests/test_dead_duplicates_retired.py` — every removed module fails to
  import, every removed path is unregistered, every resolved pair has exactly
  one handler and it is the canonical module.
- `tests/test_route_shadow_baseline.py::RESOLVED_SAFE_SHADOWS` — the 19
  resolved pairs are pinned absent, not baselined.
- `tests/test_migration_092_drop_dead_duplicate_tables.py` — both engines,
  refusal on any occupied table, round trip. Also exercised under **real
  alembic** on a fresh Postgres database replaying the container entrypoint
  (`create_all`, then `upgrade head`): the full chain reached 092; `downgrade
  091` recreated all five tables; `upgrade head` dropped them; with one seeded
  row the upgrade refused, kept all five and left the version at 091.
  The `_RECREATE` column sets were checked against the real-dump
  `structure.sql` blocks this PR deletes: all five match.

## Not done here

- #570's two remaining shadows (bulk-tag, line-items): user-facing defects
  with a pending contract decision.
- `technician_locations` / `tech_locations` consolidation (both 0 rows): a
  separate decision about which GPS surface survives.

## Sibling sweep (declared before the fix)

**Shape:** a Celery task module that `core/celery_app.py` imports, whose
tasks nothing outside tests triggers — no beat entry in `core/scheduler.py`
or `celery_app.py`, no `.delay` / `.apply_async` / `.s` / `send_task` caller
anywhere under `gdx_dispatch/`. That is what `tasks/email_poller.py` was. <!-- link-ok: deleted 2026-09-06 -->

**Surface searched:** all 21 modules in the `include=[...]` list, every
`@shared_task`/`@celery_app.task` function in each, grep over the whole
package with tests excluded (2026-09-06, this branch).

**Instances found (candidates, not verdicts — a scheduler that reads task
names from a table would not show up in this grep):**

| Module | Task with no visible trigger |
|---|---|
| `modules/outlook/tasks.py` | `repair_blank_outlook_messages` |
| `tasks/billing_followup.py` | `billing_followup_tick` |
| `tasks/estimate_followup.py` | `check_estimate_followups` |
| `modules/quickbooks/tasks.py` | nine `sync_*` / `pull_payments_task` tasks (QB is being retired; its sync is set to manual on prod) |

None is removed here: each needs a read, not a grep, and the QuickBooks set
belongs to the QB retirement. Filed as #632.

## Shipped (2026-09-07)

- **Merged:** #630 → 651aadf, then #633 rebased onto it with `--onto` and merged → 3c0e7b9. 16 of 16 PR checks green on each.
- **Released:** v1.118.0 tagged on 3c0e7b9 after all three main-tip runs went green; Release workflow succeeded; both images present in GHCR.
- **Prod (`gdx`):** recount immediately before `update.sh` — `po_requests`, `po_request_lines`, `portal_booking_requests`, `booking_requests_router`, `booking_jobs_router`, `inbound_sms` all 0. `update.sh` snapshot taken, 091 and 092 ran at boot, alembic head `092_drop_dead_duplicate_tables`, the six tables gone, `technician_locations` still present, all containers on 1.118.0 and healthy. Rollback target 1.117.1.
- **Demo (`gdx-demo`):** same recount, all 0; pin bumped, recreated, healthy in seconds, alembic head 092.
- **Walk:** as the auditor (prod) and demo owner (demo): `/api/admin/permissions` → 200 list from admin_ops; `/api/ai/usage` → 401 without a session, 200 with one; `/api/mobile/schedule`, `/api/mobile/my-jobs`, `/api/mobile/timecard`, `/api/booking/requests`, `/api/customers/{id}/communications` → 404; `/api/mobile/today` and `/api/purchase-orders` → 200. In the browser, the customer detail page shows ten tabs ending `Email, Portal` and the mobile customer page eight ending `Recurring, Portal`, in light and dark, on prod and demo. The running images contain no `booking.py`, `po_workflow.py` or `twilio_signature.py`, and the built bundle no longer contains the Communications tab.
