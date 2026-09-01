# Unimplemented endpoints — build, remove, or leave?

**Status:** **ALL SEVENTEEN ADJUDICATED — 16 SHIPPED, 1 REMOVED BY DECISION**
(owner decisions 2026-08-24/25; released v1.99.0 + the PRs that followed).
**Addendum 2026-08-31:** the Automations sequences shell is RETIRED — see the
dated section at the end (same shape as the original seventeen).

Shipped: items 1, 2, 3, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 17 and 4 — five dead
pages retired, the declined stubs removed, the customer-page Portal tab pointed
at the API that already worked (and **walked end to end on prod** — the first
customer portal sign-in this system has ever recorded), recurring jobs posting
the shape the server accepts, and the Apply Template button dropped.

Both formerly-open items closed 2026-08-25:

* **Item 11 — parts cost.** ✅ **BUILT** on the third attempt. The office now
  states which part a bill line paid for (`fulfils_part_id`); costing uses the
  bill when linked, an exact-SKU catalog estimate when not, and reports the
  variance. Two earlier implementations were pulled before merge (#469, #471)
  and the third's audit caught a data-destruction bug and a UUID mismatch that
  made "actual cost" match nothing on SQLite.
* **Item 10 — review replies.** ✅ **REMOVED** by owner decision, after research
  showed customers cannot submit a review at all — every reviews route requires
  staff auth, which is why all 13 prod rows have zero text. Recorded as #473.

The 501 conversion that produced this list was already done (`ui_compat.py`
calls `_not_implemented(...)` rather than answering `{"ok": true}`).

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
| 1 | `POST/PATCH /api/pricing[/{id}]` | `PricingView` | No `PricingEntry` model. The real pricing router is settings/markup/vendor-lists — a *different* concept. "Pricing entry" was never designed. | **Decide first**: is a pricing entry a catalog item? If the Pricing page duplicates the catalog, remove the page. | **REMOVE — done.** Page deleted (view + route + nav + 3 stubs). Its GET was a hardcoded `_empty_list()`, so PricingView could never show a row. The real pricing surface is `routers/pricing.py` (settings/calculate/markup/vendor-lists/seasonal/bundles); no real path was ever served by the stubs removed here. ⚠️ Correction, twice over: the original text credited include order (`app.py:1606` ahead of ui_compat at `:1682`); I then "corrected" that to `reorder_literal_paths_first(app)` at `app.py:2041`. **The first version was right and my correction was wrong.** `reorder_literal_paths_first` sorts `app.router.routes` on `getattr(r, "path", "")`, and the lazy `_IncludedRouter` wrappers have no `.path` — so it cannot see router-included routes at all (it logs `moved=3` out of 217 top-level entries). Include order IS the mechanism. Restored, with the evidence, so the next reader is not sent to the wrong function. |
| 2 | `POST /api/payroll/run-current-period` | `PayrollView` | `PayrollEntry` exists, but "run a period" is a calculation (gather hours → rates → entries), never written. | **Build** if payroll is run in GDX; otherwise remove the button. | **ALREADY RESOLVED — no action.** M27 (#411, v1.80.0) removed the button and the page now states "Payroll runs are not built." Not a pending decision; recorded here so a top-down reader stops looking. |
| 3 | `POST /api/communications/bulk-sms` | `SegmentsView` | Real single-send exists (`POST /api/communications/send`) *[2026-08-31: it did not — that route stored to a process dict and reported "sent" without sending; removed in #350]*. Bulk = loop + rate-limit + opt-out + audit. | **Build on top of the working single-send.** Check DNC list per recipient. | **REMOVE the affordance — done.** Owner declined building it 2026-08-24. The stub and SegmentsView's "Send SMS" button, dialog and handlers are gone; SegmentsView keeps its real router (`routers/segments.py`) and everything else on the page. |
| 4 | `POST /api/customers/{id}/recurring-jobs` | `CustomerDetailView` | `RecurringJobSchedule` exists but needs `job_template_id` + a `frequency` enum; the Vue sends free-text `title` + `interval_days`. **Incompatible models.** | **Decide the model first**, then either repoint the Vue at `/api/recurring` or widen that model. | ✅ **DECIDED + BUILT 2026-08-25 — owner: "yes to recurring jobs".** The READ was always real (`sub_resources.py` shadows the shim and queries `recurring_job_schedules`); only the create path 501'd. The dialog now posts to the real `POST /api/recurring` with `{job_template_id, frequency, customer_id, next_run}` — it had been sending `{title, interval_days}`, a shape no endpoint has ever accepted. Frequency is the server's enum (weekly/biweekly/monthly/quarterly); a free-text interval 400s. ⚠️ A schedule is built FROM a job template by the daily 6am `generate_recurring_jobs` task, and `job_template_id` is NOT NULL — with **0 templates on prod** the dialog now says so and points at Job Templates rather than offering an empty dropdown. Silent failure filed: the generator `continue`s past a schedule whose template is missing, with no log (#468). |
| 5 | `POST /api/customers/{id}/portal-account` | `CustomerDetailView` | Portal has login/password endpoints but no provisioning. (`DELETE` on the same path is also broken — C2.) | **Build** — customers can't be onboarded to the portal without it. | **DONE — and it needed no build.** Owner 2026-08-24: *"if the portal account works keep it, it is something I want to offer customers."* It works, proven on prod data: the 2026-07-24 audit chain shows `portal_access_toggled` then `portal_invite_sent` with `email_sent: true, skip_reason: null`. Provisioning, the magic-link token, the email transport and the audit trail were all real the whole time on `portal.py`'s `staff_router`, with a working office surface at PortalView (`/portal`). What was fake was the **customer page's Portal tab**: a hardcoded `{"exists": false}` read, a 501 write and a DELETE with no handler (405). Fixed by adding `GET /api/portal/{customer_id}` and repointing the tab — plus the **mobile** customer page, which called the same shim and which the first sweep missed. The password dialog was replaced with the invite flow, because provisioning never sets a password: the customer arrives by link and sets their own. ✅ **WALKED END TO END ON PROD, 2026-08-25** (v1.99.0). The owner sent an invite from the repaired Portal tab to a real inbox and completed the customer side. Prod audit chain, 26 seconds: `portal_invite_sent` `email_sent: true` (02:04:21) → `portal_login_verified` (02:04:34) → `portal_password_set` (02:04:47); `last_login_at` set and the single-use token consumed and cleared. **The first customer portal sign-in this system has ever recorded.** ⚠️ One constraint surfaced by the walk — see "Portal invites need a personal Outlook connection" below. |
| 6 | `PATCH /api/sso`, `POST /api/sso/test-connection` | `SsoView` | Real SSO is OAuth redirect flows (`/auth/sso/google`), not config CRUD. No `SsoConfig` model. `GET /api/sso` is also a permanent blank (C5). | **Remove the page** unless per-tenant SSO config is genuinely wanted. | **REMOVE — done.** Page deleted. The GET returned a fake `{provider: null, active: false}` config. Real SSO is the OAuth redirect flow at `/auth/sso/google`; single-tenant, 9 users, no per-tenant SSO config wanted. |
| 7 | `POST/PATCH /api/scheduling[/{id}]` | `SchedulingView` | No `ScheduleEntry` model. Real scheduling is calendar + appointments + tech-unavailability. | **Probably remove** — likely duplicates the calendar. | **REMOVE — done, with a recorded loss.** Page deleted because both writes 501'd, so Save/Reassign never worked, and Dispatch and Jobs already own reassignment. ⚠️ But this was the one of the five that was **not** inert, and the loss is real — see "What the Team Scheduling removal costs" below. Do not read this row as "it was fake too". |
| 8 | `PATCH /api/booking/{slot_id}` | `BookingView` | No `Booking` model. Real booking is request/approve/decline. Editing a slot isn't in that model. | **Remove the edit affordance.** | **REMOVE — done.** Page deleted. ⚠️ Note the orphan it leaves: the *real* booking flow (`/api/booking/request`, `/requests`, `/requests/{id}/approve|decline` in `routers/booking.py`) is wired and module-gated but has **no office UI at all**. `portal_booking_requests` is 0 rows so nothing is broken today. Filed separately. |
| 9 | `POST/PATCH /api/equipment-tracking[/{id}]` | `EquipmentTrackingView` | ⚠️ `EquipmentAsset` exists but its router was **deliberately unwired 2026-05-03** to kill the parallel `equipment_assets` table. Implementing resurrects exactly what that consolidation removed. | **Repoint the Vue** at the canonical equipment API. Do NOT implement here. | **REMOVE — done** (supersedes the "repoint the Vue" suggestion). Page deleted. "Company Tools" (company-owned assets) and "Customer Equipment" (`/api/equipment`, live and working) are different concepts — repointing would have merged them and re-created exactly what the 2026-05-03 consolidation removed. Closes the screen half of #453. |
| 10 | `POST /api/reviews/{id}/responses` | `ReviewsView` | `CustomerReview` has no response column. Needs a migration. | **Build** if replying to reviews matters; small migration. | ✅ **REMOVED — owner 2026-08-25: "item 10 you can remove we do not need to build it".** Research before building found the premise did not hold: **a customer cannot submit a review at all** — every route on `routers/reviews.py` requires `Depends(get_current_user)`, with no token or public route (unlike the customer portal, which does this correctly). That is why all 13 prod rows carry **zero review text**: 9 are emailed requests bearing a `google_reviews_link` — i.e. pointing customers at Google — and the 4 "submitted" rows are bare star ratings entered by staff. A reply feature would have answered something customers have no way to write. Removed: the Respond button, its dialog and handlers, the `Responded` column and `Unresponded` tab (both frozen forever once replying is gone), three now-unused imports, dead dialog CSS, and the ui_compat stub. The underlying gap is recorded as **#473** should reviews ever be wanted in-app rather than on Google. |
| 11 | `PATCH /api/jobs/{id}/parts/{part_id}` | `JobCostingView` | `JobPart` exists; only POST was built. Note `GET`/`DELETE` on the same resource are also broken (C2). | **Build the full CRUD** — the parts panel is non-functional without it. | ✅ **BUILT on the third attempt.** Owner's rules: only `closeout/used` carries cost; *"the catalog is the estimated cost but the bill from the vendor is what counts"*; *"we should be able to see the diff in case we need to change pricing"*. What finally made it work was giving the office a way to **state** the link rather than inferring it: `confirm_line(fulfils_part_id=…)` and a "Pays for which part?" picker on the vendor-bill line. A bill line carries no SKU — only the vendor's free text — so which part it paid for is not derivable, and guessing by name is what AUDIT-R1 forbids. Costing then resolves: **actual** from linked confirmed item lines (freight/tax and soft-deleted bills excluded), **estimate** by exact SKU (TRIM'd, matching `part_pricing`'s resolver), **unknown** otherwise, with `catalog_variance` on both the job and the profitability report. Attempts 1 and 2 (#469, #471) both double-counted; the audit of attempt 3 caught two more before merge — voiding a bill would have **hard-deleted the tech's attested closeout row**, and the actual-cost tier matched **zero rows on SQLite** because a `Uuid` column stores un-dashed while the query bound the dashed form, with fixtures that agreed with the query rather than the application. Both fixed and guarded. |
| 12 | `POST /api/jobs/{id}/apply-template` | `JobDetailView` | `JobTemplate` exists with checklist/duration/parts. Applying = copy onto the job. | **Build** — cheap and the model is ready. | ✅ **REMOVED — owner 2026-08-25: "12 drop the button".** The Apply Template control and its handler are gone from JobDetailView and the ui_compat stub with them. The trap that made this worth deciding rather than repointing is preserved: a real `POST /api/job-templates/{id}/apply` exists but **creates a new job** from the template, so wiring the button to it would have minted a duplicate job per click. The operation the button implied — copy a template's checklist and parts ONTO the job you are looking at — never existed, and with 0 job templates on prod nothing was losing anything. |
| 13 | `POST /api/marketing` | — | No model. | Remove. | **REMOVE — done.** No model, no UI caller (literal grep, 2026-08-24), MarketingView is not even routed. Both handlers gone. |
| 14 | `POST /api/uploads` | — | Real uploads go through the documents/photos routers. | Remove. | **REMOVE — done.** No UI caller. Real uploads go through the documents/photos routers. Both handlers gone. |
| 15 | `POST /api/estimate/calculate`, `/api/estimate/save` | — | Portal estimate flow; the real estimate surface is `/api/estimates`. | Remove. | **REMOVE — done.** No UI caller. The real estimate surface is `/api/estimates`. Both handlers gone. |
| 16 | `POST /api/billing/change-plan`, `/api/billing/cancel` | — | SaaS-plan billing — a multi-tenant concept that went with the platform collapse. | Remove (single-tenant, self-hosted). | **REMOVE — done.** No UI caller, dead by the single-tenant decision. The fix owned the class: all six SaaS-plan handlers went (subscription, invoices, payment-methods, usage, change-plan, cancel), not just the two this row names. ⚠️ `/api/billing/terms` is a DIFFERENT, real endpoint called by SettingsView — untouched. |
| 17 | `POST /api/ai/quality/feedback` | — | No store. `GET /api/ai/quality/*` are also permanent blanks (C5). | Remove or build with the AI-quality page. | **REMOVE — done.** No UI caller; all four went — `summary`, `recent`, `feedback` GET and POST. |

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

## Portal invites need a personal Outlook connection (found by the prod walk)

`send_transactional_email` (`core/transactional_email.py`) picks a provider in
this order:

1. **Outlook Graph — as the CALLING USER**, if that user has an active
   `outlook_accounts` row.
2. Legacy SMTP, if `email_settings` has a row.
3. Neither → `(False, None, "no_email_provider_connected")`.

On prod today: **one** Outlook connection exists (`Doug@garagedoorxperts.com`,
refresh token live since 2026-04-29) and **zero** rows in `email_settings`.

So a portal invite only actually emails when the person clicking Send is that
one user. Anyone else in the office — and any service or automation account —
gets `invite_sent: false` and has to hand the sign-in link over themselves.
Demonstrated 2026-08-25: the same invite failed as `auditor28` with
`no_email_provider_connected` and succeeded seconds later as the connected
owner, to the same address.

This is not a defect in the invite path — it degrades honestly, surfaces the
real reason, and offers the link. It is a **capacity limit on the feature**: for
a portal offered to customers, delivery currently depends on one individual's
personal mailbox connection. A tenant-level sender (an `email_settings` SMTP row
or a shared mailbox) would remove that dependency. Filed as **#466** rather than fixed here.

Second, smaller: `_portal_link_base(request)` derives the link's host from the
request. Calling the endpoint over `127.0.0.1:8002` mints
`http://127.0.0.1:8002/customer-portal?token=…`, which is useless to a customer.
Harmless from the UI (the browser's Host is the public domain) but a trap for
any server-side or scripted invite.

## The duplicate-shim trap (found while executing the removals)

`ui_compat.py` is not the only stub router. `routers/sub_resources.py` — whose
docstring says it *"replaces shim endpoints from ui_compat.py with real DB-backed
implementations"* — was never finished on the ui_compat side. The shims it
superseded stayed, so several paths carried **two definitions**, and
`sub_resources` registers at `app.py:1677`, ahead of ui_compat at `:1682`. Its
copy always won; the ui_compat copy was unreachable dead code.

Two consequences, both of which bit this pass:

**1. Auditing ui_compat by reading it describes behaviour that never runs.**
Row 4 of this doc was written up from the ui_compat handler, which returns
`_empty_list()`. The handler actually serving `GET /api/customers/{id}/
recurring-jobs` is `sub_resources.py:26` (confirmed by resolution order —
sub_resources is registered first), which queries `recurring_job_schedules` for
real. Only the create path 501s.

**But do not upgrade that to "the read works".** An earlier revision of this doc
did, in bold, and it is not established:
- The handler wraps its query in `except Exception: return {"items": [], "total": 0}`.
  A broken query and an empty table produce a **byte-identical 200**.
- `recurring_job_schedules` appears in **zero Alembic migrations** — it exists
  only where `create_all` reached it.
- Prod has **0 rows**, so no walk can distinguish the two either.

What is established: the ui_compat description was wrong, and a different, real
query is the one being served. Whether it returns correct rows is untested and
currently untestable on this data.

**2. Deleting a stub from ui_compat can be a no-op.** The first execution of
decisions 16 and 17 removed the billing and AI-quality handlers from ui_compat
and changed nothing: `app.openapi()` still served
`/api/billing/subscription|invoices|payment-methods|usage` and
`/api/ai/quality/summary|recent` from `sub_resources`. They were only genuinely
removed once both copies went. `/api/billing/subscription` was the worst of
them — a hardcoded `{"plan": "pro", "status": "active", "seats": 5}`, SaaS-plan
fiction in a single-tenant app, and a lying read of the same class as
portal-account.

**The check that catches this — and the WRONG check I first prescribed.**
An earlier revision of this section said "assemble the app and read
`app.openapi()`". **That is wrong, and wrong in the most misleading way.**
FastAPI collapses duplicate `path+method` into a single spec entry, so
`openapi()` cannot show you a duplicate at all — and for
`GET /api/customers/{customer_id}/recurring-jobs` it reports
`operationId: list_customer_recurring_jobs…`, which is the **ui_compat** copy:
the loser. The check named the handler that does not run.

The correct check walks the resolved route table. FastAPI ≥0.137 defers
`include_router` into lazy `_IncludedRouter` wrappers, so a flat read of
`app.routes` yields ~8 entries; you must recurse `.original_router.routes` and
re-apply `include_context.prefix`. The repo already has this helper twice —
`gdx_dispatch/tests/conftest.py::iter_app_routes` and
`gdx_dispatch/tools/frontend_contract_scan.py::routes_from_app` (note the
`gdx_dispatch/` prefix — a root `conftest.py` and a root `tools/` also exist and
do NOT contain these). Better: just run
`gdx_dispatch/tools/route_shadow_scan.py`, which now does it for you.

Run that way, the app resolves **1442 routes with 43 duplicate `(method, path)`
pairs**, and the FIRST registration wins at request time.

**Row 5 was re-checked against this trap and survives it.**
`/api/customers/{id}/portal-account` has exactly one definition
(`ui_compat.py:228`), unshadowed, so the hardcoded `{"exists": false,
"account": null}` really is what the customer page receives — with one real
`customer_users` row on prod. The lying-read finding stands.

**This pass fixed 6 of the class, not the class.** The resolved route table
carries **43 duplicate `(method, path)` pairs**.

⚠️ **And an earlier revision of this section had the direction backwards.** It
said "roughly ten of them ui_compat shadowing a real router". Measured: **19
pairs involve ui_compat, and ui_compat wins exactly ONE** —
`GET /api/admin/permissions`, because admin_ops is included at `app.py:1729`,
after ui_compat at `:1682`. For the other **18 the real router wins and the
ui_compat handler is unreachable dead code** (technicians :1532, onboarding
:1582, campaigns :1649, sub_resources :1677 all precede it).

That makes this doc's own moral *stronger* than it was stated: auditing
ui_compat by reading it does not merely risk describing dead code — for 18 of
its 19 collisions it **is** dead code. `ui_compat.py`'s module docstring
asserted the opposite ("For many paths it WINS route arbitration") and has been
corrected.

The remaining 24 pairs are real-router-vs-real-router, which is the worrying
set because the losing copy may be the better one: `purchase_orders` vs
`po_workflow` (including `/api/purchase-orders/{id}/receive` — the three-PO-systems
collision, still live), `fleet` vs `modules/fleet`, `inventory` vs
`modules/inventory`, `timeclock` vs its module, `uploads.upload_job_photo` vs
`photos.create_job_photo`, `settings` vs `branding_public`,
`jobs.get_job_costing` vs `labor.get_job_labor_costing`.

An earlier revision also claimed "zero duplicate (method, path) pairs" — that
came from `openapi()`, which collapses them, and was false. Enumerated in #462.

**The gate for this class already existed and was blind — now fixed.**
`gdx_dispatch/tools/route_shadow_scan.py` has shipped since June with a test
(`tests/test_route_shadow_baseline.py::test_no_net_new_route_shadows`) that is
supposed to stop net-new shadows. `collect_shadows()` iterated `app.routes`
**flat**, so it saw 10 of 1442 routes and reported **zero shadows against 43
live ones**; `route_shadow_baseline.txt` had never been generated, so
`load_baseline()` returned an empty set and the test compared nothing to
nothing and passed. Textbook green-ratchet-that-cannot-fail.

Fixed in this pass: the scanner now recurses `_IncludedRouter` and finds all 43,
and the baseline is committed. Counterfactual run to prove the guard bites —
deleting one line from the baseline turns the test **red**. The class can now
only shrink.

**Rows still to be re-checked this way before anyone acts on them:** the four
undecided stubs in #459, and every C5 "permanent blank" listed in
`frontend-contract-gaps-2026-08-12.md`. An empty response and a false response
are indistinguishable from the frontend, and now so is a dead handler.

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

⚠️ Before acting on any row here, re-check which handler actually serves it — run `gdx_dispatch/tools/route_shadow_scan.py`, or use the recursive walk described in "The duplicate-shim trap" above. **Do not use `app.openapi()`**; it collapses duplicates and names the loser.

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

## 2026-08-31 — the "Automations" sequences page was a shell; retired

Found while chasing one dead built-in template (`send_google_review_request`):
`routers/automations.py` (sequences / steps / enrollments, from the initial
public release) had **no executor anywhere** — nothing in `tasks/`, `core/` or
the beat schedule ever read `AutomationStep.action_type`; enrollments had no
writer (0 rows ever). The Automations page let office staff create a sequence,
toasted "Automation created", and nothing would ever run. Its `/templates`
endpoint (no UI caller) advertised six actions, five of which exist nowhere and
none of which the `automation_action_type` enum even allows; `/history` queried
a table named `audit_log` (the table is `audit_logs`) and always returned `[]`
through a bare `except`. Prod held one inactive "QA Seed" row from 2026-04-08.

The engine that runs is **Event Rules** — `modules/workflows` (`/api/workflows`,
`AutomationRulesView` at `/automation-rules`), gated by the same `automations`
module key (`core/modules.py` aliases `workflows → automations`), enabled on prod.

**Removed:** the router, its 14 CRUD tests, `AutomationsView.vue`, the nav entry.
`/automations` now redirects to `/automation-rules`; the module-gating e2e map
points the `automations` key at `/api/workflows`.
**Also removed (same shape, sibling sweep):** `modules/outlook/automations.py`
(`dispatch_trigger` — zero callers; would have sent through Outlook with no
outbound_emails row) and its tests. The sweep pattern was "an automation
code path nothing executes or calls"; surface searched: every reader of
`AutomationStep.action_type`, every caller of `dispatch_trigger`, every
action named by `BUILTIN_TEMPLATES`, across `routers/ tasks/ core/ modules/
api/ plugin_api/`; instances: those two plus the templates themselves.
**Two more instances the audit surfaced, fixed in the same PR:** (a) the Outlook
Auto-Email templates tab (Settings → Outlook) saved `auto_email_triggers` that
nothing read once `dispatch_trigger` was gone — an editor whose own notice said
"Not active yet"; tab and spec removed, column kept (this is the D6
revive-or-delete call from email-inbox-improvement-plan.md: **delete**). The API
fields stayed for ONE release as an accepted-and-ignored / inert-default
transition so a tab open across the deploy could still save; **removed in
v1.112.6 (#546)** — a stale tab now gets a bounded "Save failed" toast on 422,
and a stale chunk force-reloads on navigation;
(b) `POST/PUT /api/workflows` accepted four action types with no executor
(`send_sms`, `create_followup_task`, `emit_webhook`, `update_job_field` → every
run "not_implemented"); the router now refuses them at create/update.
`modules/workflows` is the one automation path left in the tree.

**Kept:** the three ORM models and tables (no DDL, data untouched) — dropping
them is a separate decision. Event Rules requires `nav.admin` where the old page
took `nav.office`; nobody loses a working feature because the old page did nothing.

## 2026-08-31 — review-request machinery that never sent; retired

Same shape as the Automations shell above, found in the same pass:

- `POST /api/reviews/request/{job_id}` wrote a `customer_reviews` row with
  status "requested" plus an audit row and **sent nothing**; no UI called it.
  Removed. `submit_review` (records a staff-entered rating), the list and the
  stats stay — they do what they say.
- `routers/marketing.py::schedule_review_request_for_completed_job` had **no
  production caller** and queued rows into `review_requests`, a table with
  **no reader** (0 rows on prod and demo). Function, `ReviewRequest` model and
  table removed — **migration 085** drops it (both engines; downgrade
  recreates the empty table).
- The `CustomerReview` ORM comment "columns from production schema not yet in
  ORM" was stale: the ORM matched `information_schema` column-for-column when
  checked. Comment corrected; no drift.
- `test_marketing.py` carried two permanently-skipped placeholders for the
  moved functions and two tests of the dead scheduler — removed.
- Found on the way: the Reviews page renders `source` / `customer` / `content`
  and the list API returned none of them, so Customer and Comment were blank.
  The list now carries those keys. On prod the visible change is the nine
  customer names: every stored row has a NULL `source` (nothing writes it) and
  no `review_text`, so Source still reads "Unknown" and Comment stays empty.

**Open, filed here rather than bundled (audit 2026-08-31):**
- The nine "requested" rows are orphans now — no rating, no text — and render
  on the page indistinguishable from real reviews (page shows 13, stats count
  4). Hide `status="requested"` from the list, or soft-delete them: a data call.
- `list_reviews` ignores `deleted_at`, so soft-deleting would not hide them
  until that filter exists.
- The page's Flagged tab/toggle read a `flagged` key no column provides — dead
  UI. `token`, `sent_at`, `google_reviews_link`, `message`, `scheduled_for` are
  now write-only columns; `token` is still serialized.

**#473 — DECIDED, option B (owner, 2026-09-01): reviews live on Google.** No
in-app customer submission will be built. What that means in code: the Google
review link rides every receipt and invoice footer (v1.112.0); the Reviews
page lists only the ratings the office has recorded (`list_reviews` now skips
soft-deleted rows and the nine dead `requested` rows the retired request route
minted, so page and stats agree); the page no longer promises Google/Yelp/
Facebook ingestion, and its platform filter and `Flagged` tab (no such column)
are gone. `POST /api/reviews` (office-entered rating) stays.
