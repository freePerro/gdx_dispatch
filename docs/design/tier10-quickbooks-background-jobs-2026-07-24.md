# Tier 10 — Invisible state: QuickBooks sync + background jobs

**Status:** **SHIPPED AS SCOPED** — both items this PR claimed are on main
(verified 2026-08-21): `quickbooks/router.py:283` returns
`auth_state`/`needs_reconnect` and `QbOverviewPanel.vue:14` renders the
Reconnect banner; `qb_dirty`/`qb_synced_at` are serialized by
`_serialize_invoice` and `customers.py:206` and rendered at
`InvoiceDetailView.vue:1165` / `CustomerDetailView.vue:809`;
`tests/test_tier10_qb_visibility.py` exists.
The "Deliberately not done" table below is decision-gated deferral, not
unfinished scope. Re-checked 2026-08-21 against the QuickBooks phase-out:
**four of the eleven rows are now WON'T FIX** — they are push-side or
pull-side work for a book we no longer write to. Prod evidence:
`qb_token_store.auth_state = needs_reconnect` since 2026-08-18,
`qb_money_pull_paused = true`, entity maps frozen since May 2026, and the GL
is the book of record (live since the July cutover).
**The remaining seven have nothing to do with QuickBooks** and are still open:
the NextAction renderer, estimate_followup,
estimate auto-expire, circuit breakers, failed-task visibility, and recurring
run history. Do not let the QB-flavoured table heading bury them.

Companion to `backend-vue-contract-gaps-2026-07-24.md` Tier 10. This tier is
different from Tiers 1–9/11: most items are **not mechanical contract
mismatches**, they are product decisions ("pick the QB story") or behavior
changes (start sending outbound, start mutating QB) that are gated on the office
catching QuickBooks up first (see the dead-QB / dunning-off situation). This
doc records what shipped in the Tier 10 PR and what is deliberately deferred,
with the decision each deferral needs.

## Shipped in this PR (additive, read-only, story-independent)

### 1. Ambient QuickBooks connection health — a dead token stops lying "Connected"
The `/api/qb/dashboard` endpoint (the frontend's **primary** status source) read
`last_error`/`realm_id`/`last_sync_at` only from the legacy `QBConnection` row
and **never returned `auth_state`/`needs_reconnect`**. A tenant on the modern
encrypted `QBTokenStore` whose token was revoked/expired
(`auth_state == 'needs_reconnect'`) showed **`connected: true`** on the dashboard
while every background sync silently no-op'd (`tasks.py:142-154`). That is the
2026-05 dead-QB incident, invisible on the page you'd look at to catch it.

- **Backend**: `qb_dashboard` now captures the `QBTokenStore` row it already
  queried (was a throwaway boolean) and returns `auth_state` +
  `needs_reconnect`, mirroring `qb_status`. `connected` is unchanged
  (`bool(conn) or token_present`).
- **Frontend**: `QuickbooksView` passes the two fields through; `QbOverviewPanel`
  renders a "Reconnect QuickBooks" banner + a "Reconnect needed" connection tag
  (instead of green "Connected") when `needs_reconnect`, with a button that runs
  the existing `connect` OAuth action.

### 2. Per-record QuickBooks push state on invoice + customer detail
`qb_dirty`/`qb_synced_at` were written (`push_invoice`/`push_customer` +
S122-14/S122-17 before_update listeners) but **serialized nowhere** — the office
could not tell, per record, whether it was pushed / has un-pushed changes /
never pushed.

- **Backend**: `_serialize_invoice` and `_customer_dict` now include `qb_dirty` +
  `qb_synced_at` (ISO). Invoice routes are `response_model=None` so both flow
  through; for customers the fields flow through only on the detail route
  (`get_customer`, `response_model=None`) — the list/search routes are
  `CustomerOut`-gated and intentionally not changed (see "Deliberately not done").
- **Frontend**: invoice detail + customer detail render one honest line —
  `QuickBooks: Synced <date>` (success) / `Synced <date> · changes pending`
  (warn) / `Not yet synced` (secondary) — **gated on
  `useTenantModules().isEnabled('quickbooks')`** so a tenant that doesn't use QB
  sees nothing. Rendered on **detail views only, not list rows**: `qb_dirty`
  defaults True for every row, so a list-wide chip would be a wall of
  "not synced" noise while the office is entering QB manually.

## Deliberately not done (needs Doug to pick the QB story / accept a behavior change)

| Item | Why deferred | Decision needed |
|---|---|---|
| ⛔ **WON'T FIX** — **Invoice mapper: missing `ItemRef`, deposit-lifecycle blind** (`sync.py`) | Changes what we **write** to QuickBooks. Lines push as `SalesItemLineDetail` with no `ItemRef` (Intuit-required → create likely 400s); `billing_type` ignored so a deposit invoice would push as a full standalone unpaid invoice; no push for payments/credit-memos/adjustments. High blast radius while QB books are behind. | ~~Design the QB write contract (item mapping, deposit → QB deposit/credit) and validate against a QB sandbox before enabling. A build, not a sweep fix.~~ **Closed by the QuickBooks phase-out 2026-08-21:** the invoice mapper only matters if we push, and we never will. |
| ⛔ **WON'T FIX** — **Push failures surface nowhere** (`tasks.py:120-131` `failed_permanent` list nothing reads; `_touch_sync_error` is pull-only) | The error model itself is incomplete — background push failures aren't recorded anywhere a status endpoint can read. `_touch_sync_error` even no-ops when there's no legacy `QBConnection` row. | ~~Decide where push errors are recorded (extend token store? per-record last_push_error?) then surface. The connection-health banner above covers the auth-failure case today.~~ **Closed by the QuickBooks phase-out 2026-08-21:** no pushes, therefore no push failures. |
| ⛔ **WON'T FIX** — **`last_error: None` hardcode on the modern `/status` path** (`router.py`) | Left as-is: the modern path's real error signal is `auth_state` (now surfaced). Reading `QBConnection.last_error` there would show a stale/absent legacy value — a worse lie than the honest blank. | ~~Same decision as "push failures surface nowhere".~~ **Closed by the QuickBooks phase-out 2026-08-21:** the modern path's real signal is `auth_state`, which is surfaced and now permanently reads needs_reconnect. |
| **NextAction renderer** (zero frontend refs; `billing_followup.py` + weekly nudge write NextActions nothing shows) | A whole new SPA surface. | Build a NextActions inbox, **or** stop writing them (`timeclock.py:961-963` already flags the dead loop). |
| ~~**Appointment reminders stub**~~ ✅ **REMOVED 2026-08-22** | The deferral reason was right and stayed true: making it real means starting outbound customer SMS. Prod has **no SMS transport at all** — `core/sms.py` is Twilio and none of its credentials are set — so the stub could never have sent, yet it fired hourly and logged success. Module + beat entry deleted rather than left looking alive. | Unchanged: a product go/no-go on automated reminders, and an outbound transport, before any code. |
| **estimate_followup stub** (`estimate_followup.py:46-50`, unscheduled, would stamp `reminder_sent_at` without sending) | Outbound behavior + scheduling. | Same as reminders. |
| **Estimate auto-expire** (`estimates.py:2107-2110`, user-auth endpoint, unscheduled/uncalled) | Auto-mutates estimate state (sent → expired) with no one watching. | Decide the expiry policy + whether to schedule it. |
| **Circuit breakers** (`circuit_breaker.py:180`, `app.py:1094`; `qb_circuit` not wired into QB calls) | Redis-only, no endpoint/view; wiring it changes call behavior. | Decide whether to wire + expose. |
| **Failed-task visibility** (legacy `/admin/tasks` HTML only; `task_monitor.py:122-137` records unhealthy-skips/partial-failures as success) | New SPA surface + a correctness fix to result recording. | Build a jobs-health page; fix partial-success-records-as-ok. |
| ⛔ **WON'T FIX** — **QB banking sync partial-failure records "ok"** (`tasks.py:378-405`) | Correctness fix in task-result recording; entangled with the failed-task surface. | ~~Same as above.~~ **Closed by the QuickBooks phase-out 2026-08-21:** pulls are paused and the connection is dead. |
| **Recurring generation run history** (`recurring.py:16-28`) | New surface. | Build a run-history view if needed. |

## Verification (shipped items)
- Backend serializers: `tests/test_tier10_qb_visibility.py` (5 tests, all states + row-mapping fallback).
- Live in a throwaway container (headed browser, light + dark): dashboard returns
  and flips `needs_reconnect`; reconnect banner + honest connection tag; invoice
  + customer detail render all three sync states with valid PrimeVue severities.
- No new DB columns (all pre-exist). Repo ruff count under baseline; contrast
  guard green.
