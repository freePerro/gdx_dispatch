# Lead-to-Paid Tracking: a Lead Follows Its Estimates Through to Payment

**Date:** 2026-09-29
**Status:** PARTIALLY BUILT — PR A (§1–§4: estimates carry their lead; duplicate keeps it; accept marks the lead won and picks the estimate; staff move the pick in the lead dialog) is built on `feat/lead-estimate-link`. **Not built:** PR B (§5, the lead's progress through to Paid on the Leads list).

Extends, does not replace: `docs/design/lead-intake-followup-plan.md`
(RELEASED v1.128.0: #817, #818, #819), which added `leads.estimate_id` so **Start
estimate** reopens the same draft, and which deliberately left the lead's
stage alone ("quoted means sent"). This plan keeps that column and that
idempotency and adds the other direction: an estimate knows its lead.

## Problem

Each record's status only follows its own section. A lead says `new` …
`won` only when someone presses a button; an estimate says `accepted`
forever; the lead never learns that the work was sold, done, billed or paid.
Doug (2026-09-29): *"the tags only follow what is in their section — should
it be followed across the whole process, like accepted, sold, paid?"*

## Decisions (Doug, 2026-09-29)

1. **A lead can have several estimates, and which one counts as won must be
   selectable.** *"which estimate lead counts as won it will have to be
   selected."*
2. **Estimates started from the Leads page track their lead.** *"if the
   estimates are started from the leads page it should be able to track the
   selected one."*
3. **Duplicate keeps the lead.** *"if we click duplicate estimate that it
   follows the original lead."* (The duplicate display defect Doug reported
   in the same message is fixed separately, #823.)
4. Scope is steps 1–4 of the plan agreed in conversation; showing the shared
   status on the estimate and invoice pages (step 5) is **not** in scope.

## What already exists (do not rebuild)

| Thing | Where | Use |
|---|---|---|
| One lead → one started draft (`leads.estimate_id`), idempotent Start estimate | `routers/leads.py` `start_estimate` | Keep as-is; also stamp the new `estimates.lead_id` |
| Lead lookup from an estimate (side panel) | `GET /api/leads/by-estimate/{id}` | Switch to `estimates.lead_id`, keep `leads.estimate_id` as fallback |
| "Create estimate" button on a lead | `LeadsView.vue` `createEstimateFromLead` → `/estimates/new?customer_id=` | Add `lead_id` to the query and carry it into the create payload |
| Estimate create (API + draft autosave) | `routers/estimates.py` `EstimateCreateIn` / `create_draft_estimate_record` | Accept an optional `lead_id` |
| Duplicate | `routers/estimates.py` `duplicate_estimate` | Copy `lead_id` |
| Whole-flow status for a job (Lead → … → Paid), batched | `core/job_display_state.py`, `routers/jobs.py` `_display_state_for_jobs` | PR B reuses it for the selected estimate's job — no second derivation |
| Status chip for that state | `components/JobStateChip.vue`, `utils/jobDisplayState.js` | PR B reuses it on the Leads list |
| Accept → job (office, portal, public proposal, mobile) | five `status = "accepted"` writes, see §3 | Each calls one new helper |

## Design

### 1. Data — one additive migration (PR A)

- `estimates.lead_id` UUID NULL, indexed. Many estimates per lead.
- `leads.selected_estimate_id` UUID NULL — which of the lead's estimates
  counts as won. Written once, by the accept helper, when the lead's FIRST
  estimate is accepted; afterwards only staff change it (§4). It is not
  derived from `accepted_at`, because `accept_tier`
  (`modules/proposals/service.py`) re-stamps `accepted_at` on an estimate
  that is already accepted — a derived "earliest accepted" would silently
  flip (plan audit, 2026-09-29). Accepted estimates cannot be reopened, so a
  stored pick cannot go stale by status.
- Backfill: `estimates.lead_id` from every live lead's `estimate_id` (column
  to column, so SQLite's dashless-hex storage is preserved), only where
  `lead_id IS NULL`. Idempotent; a no-op on the empty schema
  `pave_tenant_db` rebuilds on.
- Guarded `ADD COLUMN` (the 099 pattern); SQLite and Postgres. Rollback
  drops the two columns and the index. No money rows touched.

### 2. Carrying the lead (PR A)

- **Start estimate** sets `estimate.lead_id` as well as `lead.estimate_id`.
- **Create estimate** from a lead passes `lead_id` in the query string;
  `EstimateView` sends it on both create paths (autosave draft-create and
  manual Create). `EstimateCreateIn.lead_id` is optional. The server refuses
  it unless the caller holds `leads.write` (403 — `POST /api/estimates` itself
  has no gate beyond login), the lead is live (422), the lead has already been
  converted to a customer (422 — an unconverted lead has no person to match,
  so linking it would let a stranger's accepted estimate win it; PR A audit),
  and the estimate's customer is that customer (422). A lead link means "this
  person's estimate". The audit row records it.
- **Duplicate** copies `lead_id` when the copy keeps the source's customer
  (it drops to NULL with the customer when that customer was deleted).
- **Reassign customer** clears `lead_id` when the new customer is not the
  lead's, with an audit detail saying so.
- Existing behavior kept: PR C's **Create estimate** button already moves the
  lead to `quoted` at creation (`LeadsView.vue` `ensureCustomer(lead,
  'quoted')`), unlike Start estimate. This plan does not change either.

### 3. Accept marks the lead won (PR A)

One helper, `mark_lead_won_for_estimate(db, estimate, actor, ...)`, called
right after each of the five accept writes (the plan audit confirmed
exactly five, none calling another, no raw-SQL writer, and no status field
on the estimate PATCH): `routers/estimates.py`
(office accept), `routers/portal.py`, `modules/proposals/router.py` (public
proposal), `modules/proposals/service.py` (tier accept),
`routers/mobile_quoting.py`. When the estimate has a live lead: if the lead
has no live pick (none, or one naming a since-deleted estimate — PR A
audit) it sets the pick to this estimate; if the stage is
not already `won` it sets `stage = "won"`; and it writes one `lead_won` audit
row naming the estimate, the previous stage and the actor. It reads the
lead `FOR UPDATE`, so two of its estimates accepted at the same moment
(office and portal) cannot both claim the empty pick on Postgres. It never un-wins a lead, never touches a
`lost` lead's stage without saying so (a lost lead whose estimate is accepted
*is* won — it moves, and the audit row carries the previous stage), and it
never raises into the accept: a failure is logged and audited, the way
job auto-create already is. A guard test enumerates every
`status = "accepted"` write on an estimate and fails if one does not call
the helper.

### 4. Which estimate counts (PR A)

`selected estimate` = `leads.selected_estimate_id` if that estimate is still
live and belongs to the lead; otherwise none (a soft-deleted pick shows as
no pick, and staff choose again).

- `GET /api/leads/{id}/estimates` — the lead's live estimates (number,
  label, status, total, accepted_at, job_id) plus `selected_estimate_id`.
  `leads.read` + `estimates.read_all` (the catalog has no `estimates.read`).
- `PUT /api/leads/{id}/selected-estimate` `{estimate_id | null}` —
  `leads.write`. 422 unless the estimate is the lead's and accepted; null
  clears the pick. Audited (`lead_selected_estimate_changed`,
  old and new).
- Leads page, lead dialog: an **Estimates** section listing them, with a
  **Counts as won** choice on accepted rows, and a link to each estimate.

### 5. The lead follows its selected estimate to Paid (PR B)

`GET /api/leads` adds `progress` per lead, derived, batched (no N+1):

- selected estimate with a job → that job's display state
  (`_display_state_for_jobs`): Scheduled, In Progress, Ready to Bill,
  Invoiced, Partially Paid, Overdue, **Paid**, Cancelled;
- selected estimate without a job → **Sold**;
- else any estimate `sent` → **Quoted**; any `declined` and none open →
  **Declined**; any draft → **Estimate started**; no estimates → none.

The Leads table gets a **Progress** column rendering it with the existing
chip. Nothing is stored — "derive, don't cache" (billing-capture-hardening).
Roles with `leads.read` but no invoice read (sales, dispatcher) see the
state label (e.g. **Paid**, **Overdue**) and never an amount. A lead's
`stage` and its `progress` can differ (a PR C lead reads `quoted` while its
only estimate is a draft) — they answer different questions, and both show.

## PRs

- **PR A** — §1–§4, backend + Leads dialog + EstimateView `lead_id` carry.
- **PR B** — §5, built on main after PR A merges (not stacked).

Each: full local frontend suite, the backend tests it touches run in the
docker image (plus CI's full matrix), a throwaway-container browser walk as
office staff, `/audit` before commit.

## Out of scope

Showing the lead-to-paid status on the estimate and invoice pages (step 5 of
the conversation plan); auto-moving a lead to `quoted` on send (PR B's
derived **Quoted** covers the display without writing); marking a lead `lost`
when its estimates are declined (staff decide); the lead intake plan's
unbuilt PR C.

## Plan audit (2026-09-29)

Held: exactly five accept writes, no double-firing; the column-to-column
backfill is type-safe (prod: both UUID, 0 live leads with an `estimate_id`,
so the backfill changes nothing there); writing `won` drops the lead out of
the follow-up lists as intended. Fixed in this revision: the selected
estimate is stored at first accept instead of derived from `accepted_at`
(which `accept_tier` re-stamps); the list endpoint's gate names a key that
exists (`estimates.read_all`); both docs' status lines (the intake plan's
PR C shipped as #819); setting `lead_id` needs `leads.write` and a matching
customer, and reassigning the customer clears it; PR C's `quoted`-at-create
behavior recorded as existing.

## PR A audit (2026-09-29)

Fixed before commit: an unconverted lead could be linked to any customer
through the API (now refused); a pick naming a soft-deleted estimate blocked
every later accept from picking (now retaken); concurrent accepts could both
write the pick (lead row now locked). Accepted as-is and recorded: Duplicate
copies an existing `lead_id` with `estimates.write` alone (it only repeats a
link that already passed the gate); the accept guard test matches the five
writers by variable name (`est`/`estimate`), which is every writer today;
`accept_tier` has no status gate and can accept a draft — pre-existing, and
now it also wins the lead.
