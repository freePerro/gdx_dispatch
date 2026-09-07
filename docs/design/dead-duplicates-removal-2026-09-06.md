# Dead duplicates removal — 2026-09-06

Status: RELEASED v1.118.0 — MERGED #633 2026-09-07 (squash 3c0e7b9, stacked on #630); prod and demo rolled to 1.118.0 on 2026-09-07 ~02:36Z with the six-table recount at 0 rows on both immediately before; migrations 091+092 ran, alembic head 092 on both; walked on prod and demo (API, desktop and mobile customer pages, light and dark). Closes #458 #459 #480 #568 #569 #571 #572 #574 #595 #599. Migration 092. — **Follow-up (mobile status/clock/contact orphans + campaigns module router) on branch `chore/mobile-campaign-orphans` (PR pending, 2026-09-07), NOT on prod; see the last section.**

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
| `GET/POST /api/campaigns`, `POST …/{id}/send` | `routers/campaigns.py` | `modules/campaigns/router.py` (3 of 4 routes; `/stats` stayed until the 2026-09-07 follow-up removed it too — see the last section) |
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
- **Walk:** as the auditor (prod) and demo owner (demo): `/api/admin/permissions` → 200 list from admin_ops; `/api/ai/usage` → 401 without a session, 200 with one; `/api/mobile/schedule`, `/api/mobile/my-jobs`, `/api/mobile/timecard`, `/api/booking/requests`, `/api/customers/{id}/communications` → 404; `/api/mobile/today` and `/api/purchase-orders` → 200. In the browser, the customer detail page shows ten tabs ending `Email, Portal` and the mobile customer page eight ending `Recurring, Portal`, in light and dark, on prod and demo. The running images contain no `booking.py`, `po_workflow.py` or `twilio_signature.py` <!-- link-ok: deleted 2026-09-06 -->, and the built bundle no longer contains the Communications tab.

## Follow-up (2026-09-07) — the #480 sweep was list-driven

The 2026-09-07 code review of this range re-ran #480's stated shape — every
`@router` path in `routers/mobile.py` against every `/api/mobile` call string in
`frontend/src` — instead of its 13-item candidate list, and the shape was still
alive. Removed in the follow-up PR from `chore/mobile-campaign-orphans`:

| Removed | Why |
|---|---|
| `POST /api/mobile/jobs/{id}/status`, `POST /api/mobile/job/{id}/status` | No SPA caller. The plural one wrote dispatch vocabulary straight into `Job.status` with no transition check — a corrupting write. The singular one only dispatched into `mobile_job_en_route` / `mobile_job_arrived` / `mobile_job_complete`, which the SPA calls directly and which all run `_validate_forward_transition`; the guard lives on there. The generic `/jobs/{id}/transition/{status}` route deleted above was a fourth guarded path. |
| `POST /api/mobile/clock-in`, `POST /api/mobile/clock-out` | Day-level clock duplicates; the mobile timeclock view calls `/api/timeclock/clock-in|out` directly. |
| `POST /api/mobile/job/{id}/clock-in`, `/clock-out`, `/notes` (singular aliases) | Stacked second paths on live handlers; the SPA uses the plural form only. |
| `GET /api/campaigns/{id}/stats` (the whole `modules/campaigns/router.py`) | Read `segment_id`, `template_id`, `channel`, `campaign_type` from `marketing_campaigns`, columns the real table (`MarketingCampaign`, `create_all`) does not have; its `CREATE TABLE IF NOT EXISTS` was a no-op on any real install, so the route 500'd; `marketing_campaign_sends` had no writer left and does not exist on prod or demo (`to_regclass` on both databases, 2026-09-07). No SPA caller. Its test seeded the module's own DDL into a bare SQLite and was deleted with it. |

Left in place, with the reason:

- `POST /api/mobile/jobs/{id}/complete` has no SPA caller, but that is a
  recorded decision — `MobileCloseoutOwnership.spec.js` guards it as
  deliberately unreachable, and `test_mobile_signature_gate.py` /
  `test_mobile_state_machine.py` still cover it.
- `DELETE /api/mobile/jobs/{id}/customer/contacts/{contact_id}` has no SPA
  caller either (found by the audit; its decorator spans lines, which the
  first sweep's regex missed). It is not a duplicate: it was built to a stated
  ask ("a wrong number a tech typed should be removable") and only the remove
  button never shipped. Whether to build the button or delete the endpoint is a
  product call — **decision owed**, not made here.

Evidence that the removed handlers were never called, not just never wired
(read-only prod query from the sixth audit pass, 2026-09-07): `audit_logs` is
delete-protected (`audit_logs_no_delete` trigger) and holds 29,779 rows from
2026-06-22 to today; rows whose action names any of the deleted handlers
(`mobile_day_clock_in`, `mobile_day_clock_out`, `mobile_job_status_changed`,
`update_mobile_job_status`): **0**. All 19 `clock_in` rows are `entity_type=job`
(the kept per-job clock); the day clock is fully accounted for by the surviving
router (`timeclock_clock_in` 22, `timeclock_clock_out` 21). `timeclock_entries_router`
holds 60 rows, 0 keyed by a `technicians.id` — the key only the deleted day
handler wrote. `/stats` leaves no audit row, so for it the proof is that it
could not have succeeded: `marketing_campaigns` on prod and demo has none of the
four columns it selected.

The sweep's declared surface was `routers/mobile.py`; the audit re-ran the
shape from the checked-in route table (`gdx_dispatch/openapi_routes.txt`) and
found two more `/api/mobile` routes mounted from other files with no SPA
caller — `POST /api/mobile/voice-note` (`routers/voice.py`) and
`POST /api/mobile/chat/{message_id}/read` (`routers/mobile_chat.py`) → #641,
not adjudicated here. Lesson recorded there: sweep from the route table, not
from decorator regexes over one file. The surviving `routers/campaigns.py`
serves four of its eleven routes to the SPA; the other seven are noted on #638.
The MOB-05 checklist row that still names `/complete` as the completion path is
part of #640.

The sweep also checked the paths a literal grep cannot see: the one dynamic
template in `MobileJobDetailView.vue` (`advance(path)`, only `en-route` and
`arrived`), the offline outbox (`postQueued` never queues a removed URL), and
the generated `types/api.d.ts` <!-- link-ok: deleted in #544 --> (deleted in #544). `openapi_snapshot --check` is
the proof the routes are gone, not a route count from a bare `create_app()`.

Filed, not done here: the orphaned `mobile_sync_actions` table (0 rows on prod
and demo 2026-09-07) needs migration 093 → #636; the same sweep across every
`modules/*/router.py` found 29 routes with no SPA call string that need an
external-consumer / give-it-a-UI / delete verdict each → #637; with its router
gone, the rest of `modules/campaigns` (service, tasks, models, and the empty
`campaigns` / `campaign_sends` tables) is headless → #638.

The removed paths are pinned in `test_dead_duplicates_retired.py::REMOVED_PATHS`
so they stay gone.

Found by the audit, not fixed here: the mobile job page's day clock
(`_clock_states`, day branch) reads `timeclock_entries_router` by `Technician.id`,
while the surviving writer `/api/timeclock/clock-in` keys rows by user id — so
the page shows "Not clocked in" for every tech who has a Technician row. The
deleted `mobile_day_clock_in` was the only writer that used the reader's key and
nothing ever called it → #639.

Also fixed on the way, and run against a throwaway container from this branch
(14 API-level e2e tests passed; MOB-06 twice back to back so the second run
took the router's 400 "already clocked in" branch): e2e MOB-09 sent
`signature_data` to `POST /api/jobs/{id}/signature`, whose model requires
`signature`, so it passed on a 422 without reaching the handler; it now sends
the SPA's body and asserts 201. MOB-06 targets `/api/timeclock/*` directly. The
sibling tolerances in MOB-03/04/05 are #640.
