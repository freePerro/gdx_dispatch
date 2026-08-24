# Unimplemented endpoints — build, remove, or leave?

**Status:** **DECISIONS TAKEN 2026-08-24 (owner)** — all seventeen resolved;
implementation in flight. Sixteen of the seventeen now carry a recorded call
(build / remove / repoint / defer); item 11 is deliberately deferred pending a
business decision on whether parts *cost* is tracked at all. The 501 conversion
that produced this list was already done (`ui_compat.py` calls
`_not_implemented(...)` rather than answering `{"ok": true}`).

**The evidence that decided it.** The section below asks for prod hit counts
before deleting anything. Collected 2026-08-24 from the nginx access logs
(21 Aug 00:28 → 24 Aug 19:37 UTC, **175,965 requests**):

1. **Zero 501 responses** across all twenty-one stubbed endpoints — nobody
   pressed a button that would have failed.
2. **Zero GETs** to `/api/sso`, `/api/scheduling`, `/api/booking`,
   `/api/equipment-tracking` and bare `/api/pricing` — nobody opened the pages
   either. (Every apparent `/api/pricing` hit was `/api/pricing-engine/*`, a
   different router; every apparent `/api/jobs/*/parts` hit was
   `/parts-needed`.)

Measurement 2 is the load-bearing one and is stated separately on purpose: the
"zero 501s" figure alone proves nothing about whether a page was opened, because
these GETs answered **200**. An earlier draft of this doc conflated the two; an
adversarial review caught it.

Two caveats recorded honestly. The window is **3.5 days, not the month this doc
prescribes** — the app container's own logs were lost when v1.98.1 was deployed,
and nginx `max-size=10m max-file=3` rotation keeps roughly three days. And that
window has since aged out, so **the figures above are no longer reproducible**;
they were read once, on 2026-08-24. The direction is unambiguous; the evidence
is perishable.

**Three of this doc's own suggestions were wrong** and are corrected in the
table below — see "What re-verification changed" after it.
~~Three fake-success handlers also survive the conversion and still answer a
bare `return _ok()`~~ — **closed by #391 (2026-08-21)**: they now refuse with a
logged 501 and `_ok()` is gone. For the record they were
`PATCH /api/onboarding/checklist` (`ui_compat.py:327`) and
`PUT /api/campaigns/{id}/activate` / `/deactivate` (`:941`, `:946`).

Created 2026-08-12 from the C6 fix. These endpoints used to answer `{"ok": true}`
to a write they never performed; the Vue showed "Saved" and discarded the user's
edit. They now log at WARNING and return **501**.

**Nothing here is broken by the 501.** None of it ever worked. The change makes
the failure visible instead of silent.

## How to see what people actually try to use

Every refusal logs one line:

```
ui_compat_not_implemented feature=<name> method=<VERB> path=<path> tenant=<id> user=<sub>
```

```bash
ssh <prod-host> 'docker logs <app-container> 2>&1 | grep ui_compat_not_implemented' \
  | sed 's/.*feature=\([^ ]*\( [^ =]*\)*\) method=.*/\1/' | sort | uniq -c | sort -rn
```

That count is the evidence for the decisions below: an endpoint nobody hits in a
month is a candidate for deletion, not for building.

## The list

Ordered by how expensive the missing behaviour looks. "UI" = a Vue view calls it
today, so a user can reach the 501.

| # | Endpoint | UI caller | What's missing | Suggested | **Decision (2026-08-24)** |
|---|---|---|---|---|---|
| 1 | `POST/PATCH /api/pricing[/{id}]` | `PricingView` | No `PricingEntry` model. The real pricing router is settings/markup/vendor-lists — a *different* concept. "Pricing entry" was never designed. | **Decide first**: is a pricing entry a catalog item? If the Pricing page duplicates the catalog, remove the page. | **REMOVE — done.** Page deleted (view + route + nav + 3 stubs). Its GET was a hardcoded `_empty_list()`, so PricingView could never show a row. The real pricing surface is `routers/pricing.py` (settings/calculate/markup/vendor-lists/seasonal/bundles); no real path was ever served by the stubs removed here. ⚠️ Correction: an earlier draft credited include order (`app.py:1606` ahead of ui_compat at `:1682`). Right conclusion, wrong mechanism — `reorder_literal_paths_first(app)` at `app.py:2041` runs after every include and is what actually keeps `/api/pricing/settings` ahead of a `/{entry_id}` catch-all. |
| 2 | `POST /api/payroll/run-current-period` | `PayrollView` | `PayrollEntry` exists, but "run a period" is a calculation (gather hours → rates → entries), never written. | **Build** if payroll is run in GDX; otherwise remove the button. | **ALREADY RESOLVED — no action.** M27 (#411, v1.80.0) removed the button and the page now states "Payroll runs are not built." Not a pending decision; recorded here so a top-down reader stops looking. |
| 3 | `POST /api/communications/bulk-sms` | `SegmentsView` | Real single-send exists (`POST /api/communications/send`). Bulk = loop + rate-limit + opt-out + audit. | **Build on top of the working single-send.** Check DNC list per recipient. | **REMOVE the affordance.** Owner call 2026-08-24: bulk SMS is not wanted. SegmentsView keeps its real router (`routers/segments.py`); only the bulk-send control and its stub go. |
| 4 | `POST /api/customers/{id}/recurring-jobs` | `CustomerDetailView` | `RecurringJobSchedule` exists but needs `job_template_id` + a `frequency` enum; the Vue sends free-text `title` + `interval_days`. **Incompatible models.** | **Decide the model first**, then either repoint the Vue at `/api/recurring` or widen that model. | **REMOVE the affordance.** `recurring_job_schedules` is 0 rows on prod and the real `/api/recurring` router exists with an incompatible model. Not worth reconciling two models for a feature nobody has used; the customer-page control goes. |
| 5 | `POST /api/customers/{id}/portal-account` | `CustomerDetailView` | Portal has login/password endpoints but no provisioning. (`DELETE` on the same path is also broken — C2.) | **Build** — customers can't be onboarded to the portal without it. | **BUILD — and fix a lying read first.** ⚠️ The GET is worse than missing: it returns a hardcoded `{"exists": false, "account": null}` for every customer, and prod has **1 real `customer_users` row**. The office is told that customer has no portal account. Read fix + real provisioning POST + audit. |
| 6 | `PATCH /api/sso`, `POST /api/sso/test-connection` | `SsoView` | Real SSO is OAuth redirect flows (`/auth/sso/google`), not config CRUD. No `SsoConfig` model. `GET /api/sso` is also a permanent blank (C5). | **Remove the page** unless per-tenant SSO config is genuinely wanted. | **REMOVE — done.** Page deleted. The GET returned a fake `{provider: null, active: false}` config. Real SSO is the OAuth redirect flow at `/auth/sso/google`; single-tenant, 9 users, no per-tenant SSO config wanted. |
| 7 | `POST/PATCH /api/scheduling[/{id}]` | `SchedulingView` | No `ScheduleEntry` model. Real scheduling is calendar + appointments + tech-unavailability. | **Probably remove** — likely duplicates the calendar. | **REMOVE — done, with a recorded loss.** Page deleted because both writes 501'd, so Save/Reassign never worked, and Dispatch and Jobs already own reassignment. ⚠️ But this was the one of the five that was **not** inert, and the loss is real — see "What the Team Scheduling removal costs" below. Do not read this row as "it was fake too". |
| 8 | `PATCH /api/booking/{slot_id}` | `BookingView` | No `Booking` model. Real booking is request/approve/decline. Editing a slot isn't in that model. | **Remove the edit affordance.** | **REMOVE — done.** Page deleted. ⚠️ Note the orphan it leaves: the *real* booking flow (`/api/booking/request`, `/requests`, `/requests/{id}/approve|decline` in `routers/booking.py`) is wired and module-gated but has **no office UI at all**. `portal_booking_requests` is 0 rows so nothing is broken today. Filed separately. |
| 9 | `POST/PATCH /api/equipment-tracking[/{id}]` | `EquipmentTrackingView` | ⚠️ `EquipmentAsset` exists but its router was **deliberately unwired 2026-05-03** to kill the parallel `equipment_assets` table. Implementing resurrects exactly what that consolidation removed. | **Repoint the Vue** at the canonical equipment API. Do NOT implement here. | **REMOVE — done** (supersedes the "repoint the Vue" suggestion). Page deleted. "Company Tools" (company-owned assets) and "Customer Equipment" (`/api/equipment`, live and working) are different concepts — repointing would have merged them and re-created exactly what the 2026-05-03 consolidation removed. Closes the screen half of #453. |
| 10 | `POST /api/reviews/{id}/responses` | `ReviewsView` | `CustomerReview` has no response column. Needs a migration. | **Build** if replying to reviews matters; small migration. | **BUILD.** 13 real `customer_reviews` rows on prod. Confirmed not shadowed: `routers/reviews.py` has no `/responses` route, so this is a genuine build — small migration + endpoint + UI. |
| 11 | `PATCH /api/jobs/{id}/parts/{part_id}` | `JobCostingView` | `JobPart` exists; only POST was built. Note `GET`/`DELETE` on the same resource are also broken (C2). | **Build the full CRUD** — the parts panel is non-functional without it. | **DEFERRED — needs a business decision, not a code decision.** See "What re-verification changed" below: `job_parts` (0 rows) is the *cost* side and `job_parts_needed` (73 rows, $1,911.00) is the *price* side. They are not duplicates. Building this PATCH delivers an editor for a table nothing populates. |
| 12 | `POST /api/jobs/{id}/apply-template` | `JobDetailView` | `JobTemplate` exists with checklist/duration/parts. Applying = copy onto the job. | **Build** — cheap and the model is ready. | **DEFERRED with a trap recorded.** ⚠️ A real `POST /api/job-templates/{id}/apply` already exists — but it calls `create_job_from_template` and returns a **new job**, whereas this button means "apply onto the job I am looking at". A naive repoint would silently mint a duplicate job per click. The operation genuinely does not exist. |
| 13 | `POST /api/marketing` | — | No model. | Remove. | **REMOVE.** No model, no UI caller (verified 2026-08-24), MarketingView is not even routed. |
| 14 | `POST /api/uploads` | — | Real uploads go through the documents/photos routers. | Remove. | **REMOVE.** No UI caller. Real uploads go through the documents/photos routers. |
| 15 | `POST /api/estimate/calculate`, `/api/estimate/save` | — | Portal estimate flow; the real estimate surface is `/api/estimates`. | Remove. | **REMOVE.** No UI caller. The real estimate surface is `/api/estimates`. |
| 16 | `POST /api/billing/change-plan`, `/api/billing/cancel` | — | SaaS-plan billing — a multi-tenant concept that went with the platform collapse. | Remove (single-tenant, self-hosted). | **REMOVE.** No UI caller, and dead by the single-tenant decision. |
| 17 | `POST /api/ai/quality/feedback` | — | No store. `GET /api/ai/quality/*` are also permanent blanks (C5). | Remove or build with the AI-quality page. | **REMOVE.** No UI caller; the sibling `GET /api/ai/quality/*` are permanent blanks (C5) and go with it. |

## What the Team Scheduling removal costs

Four of the five deleted pages were inert. Team Scheduling was not, and an
adversarial review caught the first draft of this doc — and a docstring in
`ui_compat.py` — asserting that it was. Recorded properly here.

`list_scheduling` ran real SQL over `jobs`. The 2026-04-27 pass had already
fixed it to read `jobs.scheduled_at` (it previously returned an empty list while
`/jobs` showed 8 scheduled rows on the same tenant), and the **2026-04-29 pass
widened it deliberately**:

```sql
WHERE ... AND ( j.scheduled_at IS NOT NULL
             OR LOWER(j.lifecycle_stage::text) = 'scheduled' )
```

That second clause exists to surface jobs a dispatcher advanced to Scheduled
**without setting a date** — the write-side gap — so the view could render an
"Unscheduled date" badge an operator could act on. Its own comment: *"previously
these 8 GDX rows were invisible."*

**Prod carries 5 such rows right now** (5 undated / 4 dated among
`lifecycle_stage = 'scheduled'`, checked 2026-08-24).

What is kept and what is lost:

- **Kept:** those 5 jobs remain reachable — Jobs, filtered to the "Scheduled"
  status chip, lists all 9 with a blank Scheduled Date column.
- **Lost:** nothing *flags* the anomaly any more. The badge that said "this job
  is Scheduled but has no date" is gone; you now have to notice a blank cell.

The page still went, because the badge led to Save/Reassign buttons that were
501 stubs — it named a problem it could not fix. But "Dispatch and Jobs already
own reassignment" answers the *write* side only, and that phrasing hid the read
side in the first draft. Filed as a follow-up rather than silently dropped:
nothing in the app flags a Scheduled job with no scheduled date.

## The pattern this list was missing

Six of the seventeen are not unbuilt features. They are **parallel fakes of
working features**, reachable from a different UI surface:

| # | The stub | The real thing, already shipped |
|---|---|---|
| 2 | `POST /api/payroll/run-current-period` | resolved by M27 (#411) — button removed, page states runs are not built |
| 4 | `POST /api/customers/{id}/recurring-jobs` | `routers/recurring_jobs.py` → `/api/recurring` |
| 5 | `*/api/customers/{id}/portal-account` | `portal.py` `staff_router` → `/api/portal`, with a working office UI in `PortalView.vue` |
| 8 | `PATCH /api/booking/{slot_id}` | `routers/booking.py` → request / approve / decline (no UI, see the orphan note) |
| 9 | `*/api/equipment-tracking` | `modules/equipment/router.py` → `/api/equipment` |
| 12 | `POST /api/jobs/{id}/apply-template` | `job_templates.py:313` — but different semantics; see the trap above |

Item 5 is the sharpest case. The doc says "customers can't be onboarded to the
portal without it." They can: `PATCH /api/portal/{customer_id}` creates the
`CustomerUser`, is gated on `customers.write`, writes a `portal_access_toggled`
audit row, and the office reaches it from the "Customer Portal" nav entry. What
exists on the customer page is a **second, fake** Portal tab reading a hardcoded
`{"exists": false}` — which is why it reports "No portal account registered for
this customer" even for the one customer who has one.

**So the question is usually not "build or remove" — it is "which of the two
surfaces survives."** Asking it the first way produces a plan to rebuild
something that already works. Every future row on a list like this should be
checked against the whole route table before it is called unbuilt: the giveaway
is a Vue path that no real router serves, not a feature that no code implements.

## What re-verification changed (2026-08-24)

Re-checked every row against `origin/main` @ 833d74a and prod data before the
decisions were taken. Three of this doc's own "Suggested" calls were wrong, and
the list was incomplete.

**Item 5 was understated.** "Build — customers can't be onboarded" describes a
missing feature. What is actually there is a *lying read*: `GET
/api/customers/{id}/portal-account` returns a hardcoded `{"exists": false,
"account": null}`. Prod has one real `customer_users` row, so for that customer
the office is being told something false. That is a defect and is fixed
regardless of whether provisioning gets built.

**Item 11's premise was wrong.** The suggestion "build the full CRUD — the parts
panel is non-functional without it" assumes one parts system. There are two, and
they are not duplicates:

| Table | Rows on prod | What it holds | Who writes it |
|---|---|---|---|
| `job_parts` (`modules/inventory`) | **0** | *cost* — `unit_cost_at_time`, consumed from inventory | `POST /api/jobs/{id}/parts`, `POST /api/mobile/jobs/{id}/parts-used` — both real and wired, **0 calls in the window** |
| `job_parts_needed` | **73** ($1,911.00) | *price* — `unit_price`, `price_source`, `billed_invoice_id` | parts_needed / van_inventory / estimates — the closeout→invoice flow |

So the business records what it charges for parts and never what they cost.
The consequence is live and money-adjacent: `_parts_for_job`
(`routers/job_costing.py:223`) is the only parts input to job costing
(`base = labor_total + parts_total`) and reads the empty table, so **job costing
reports $0 parts cost on all 25 prod jobs that have parts**, and JobDetailView's
"Parts Used" panel prints "No parts recorded." on every job in the system.
Margin is overstated by the entire parts cost. Filed separately — the fix is a
business decision about whether parts cost is tracked, not a CRUD endpoint.

**Item 12 hides a duplicate-job trap.** "Build — cheap and the model is ready"
missed that a real apply endpoint already exists: `POST
/api/job-templates/{id}/apply` (`routers/job_templates.py:313`), wired and
audited. But it calls `create_job_from_template` and returns a **new job**,
while `JobDetailView`'s button means "copy this template onto the job I am
looking at" and reloads that job. Repointing the Vue at the existing endpoint —
the obvious reading of "the model is ready" — would silently create a second job
on every click. `JobTemplatesView` is real, routed and in the nav, so the 0
`job_templates` rows are an office-hasn't-made-one, not a missing surface.

**The list was four endpoints short.** `ui_compat.py` carries 21 stubbed
endpoints, not 17. These four never had a decision recorded:

| Endpoint | UI caller | Status |
|---|---|---|
| `POST /api/customers/{id}/communications` | `CustomerDetailView` | 501, GET returns hardcoded `_empty_list()` — undecided |
| `PATCH /api/onboarding/checklist` | `OnboardingView` | converted by #391, disposition never recorded |
| `PUT /api/campaigns/{id}/activate` | `CampaignsView` | converted by #391, disposition never recorded |
| `PUT /api/campaigns/{id}/deactivate` | `CampaignsView` | converted by #391, disposition never recorded |

**Also corrected:** every one of these pages *is* reachable from the sidebar.
The nav is defined in `frontend/src/constants/modules.js`, not in the sidebar
component, so an office user could click into nine broken pages. Five of those
nav entries are removed by the work this doc records.


## Verified NOT an issue

`POST /api/role-permissions/migration-banner/ack` returns `{"pending": False}`
and does nothing — correctly. The per-tenant feature-flag table it acknowledged
was removed in the single-tenant collapse, so there is never a pending
migration to ack. `GET` on the sibling path always reports `pending: False`.
The scanner still flags it (C6=1); that is a known, accepted true-negative.

## Already fixed, for reference

`PATCH /api/service-agreements/templates/{id}` was in this class and is now
genuinely implemented in `routers/service_agreements.update_template` — the
model and the sibling GET/POST already existed, so it only needed writing.
It also accepts the Vue's legacy `price` field alongside `default_price`.
