# Backend ↔ Vue Contract Gaps — Full Sweep, 2026-07-24

**Status:** **FIXED** — every tier closed or explicitly won't-fix, as of
2026-08-22 (PRs #399-#403). Re-verified against main 2026-08-21, then finished. Tiers 1-9
and 11 are substantially closed: the `api.delete` alias (`useApi.js:183/313`),
the onboarding import, `PUT /commissions/rules/{id}`, `POST /settings/branding/logo`,
the A/R UI doors (credit-memo, apply-credit, warranties, job dependencies,
reminders, finalize), `is_return_visit` badges, PO `received_date`, every
invalid `'warning'` severity, the Tier-6 write-path drops, the forecasting
authorization hole, the dead `AdminSettingsView`, and the mobile invoice email.
**Tier 10's non-QuickBooks half is RESOLVED** (2026-08-22): the
appointment-reminder task was three stubs wired to celery beat, firing hourly on
prod and logging `scheduled_count: 0` forever. Removed — module, beat entry and
all — because the blocker is transport, not the finder. The stub's own
`_send_sms` was a no-op, and the shared `core/sms.py` is Twilio, whose
credentials are unset on prod. **Correction to an earlier draft of this line:**
that draft said "no outbound SMS transport exists", which is false — Phone.com
can send (`modules/phone_com/client.py::send_message`, called from that module's
router). The stub simply never touched the working sender, so implementing the
finder alone would still have sent zero messages while logging success.
Reviving it needs a product go/no-go on automated customer SMS **and** wiring to
Phone.com.
**Tier 3's latent item is RESOLVED too**: `amount_paid` is now on
`_serialize_invoice` — sourced from the payments table, not the dead column,
which is itself now **dropped** (migration 073). See money-audit M35. Worth
recording why this doc read the situation backwards: it framed the gap as "the
field is missing from the serializer", and the first instinct was to add the
column. Adding it would have surfaced a cache nothing wrote. The field was
genuinely missing — MobileBillingView gates its "Paid" row on it, so that row
had never rendered — but the fix was to derive the value, not expose the column.
**⛔ Tier 10's QuickBooks half is WON'T FIX** as of the 2026-08-21 phase-out:
the missing `ItemRef` and the unrendered per-record push state were only ever
about *writing to* QuickBooks, and we no longer do. Prod:
`auth_state = needs_reconnect` since 2026-08-18, pulls paused, maps frozen
since May, GL live as the book of record.
_See § Score below. The line "Everything below is UNFIXED" was true the day it
was written and has been misleading ever since — it cost a full re-verification
pass on 2026-08-21 to discover most of it had shipped._

Two-direction audit of the entire backend↔frontend contract, run after the
deposit-invoice gap fixes shipped in PR #197 (`fix/deposit-gaps-notify-sms`).
Method: enumerate the API surface (~1,006 endpoints across ~115 routers + ~40
module routers), match against every `api.get/post/patch/del`/fetch call in
`frontend/src`, then sweep the reverse direction (frontend calls, field reads,
status maps, error handling, client-side money math). The five highest-impact
claims were independently re-verified against the code before this document was
written. **Everything below was UNFIXED on 2026-07-24, the day this was written.
Most of it has since shipped — read § Score, not this sentence.** Line numbers
will drift — re-verify before editing.

Deliberately excluded (have external/non-UI callers, not gaps): webhooks,
`/pwa`, booking/branding_public/instant_estimate/well_known, telemetry sinks
(`ux_telemetry`, `error_sink`, `api_metadata`), cron endpoints
(`estimates expire-stale`), and `tasks/{id}/reopen` (reachable via status
PATCH from TasksView instead).

---

## Score — what has since been fixed (re-verified against `main` 2026-08-21)

This section exists because its absence made this document a trap. For thirteen
months of repo time the intro said "everything below is UNFIXED", and a reader
had no way to learn otherwise without hand-checking ~50 items across eleven
tiers — which is exactly what the 2026-08-21 sweep had to do, only to find most
of it had shipped. **Update this table in the PR that fixes the item.** The
sibling `money-audit-2026-08-04.md` §0.6 is the pattern being copied.

| Tier | State | Evidence on `main` |
|---|---|---|
| 1 — broken today (5) | ✅ all fixed | `useApi.js:183/313` exports a `delete` alias with a distinct signature; OnboardingView posts through the shared client; `commission.py:140 PUT /rules/{rule_id}`; `settings.py:754 POST /branding/logo`; the `total_amount` fallback is gone from DashboardView |
| 2 — built, no UI door (7) | ✅ doors exist | credit-memo + apply-credit on `InvoiceDetailView.vue:474/483`; `WarrantiesView.vue`; job dependencies at `JobDetailView.vue:403/2102`; receipt→expense on the bank-feeds line path; per-invoice reminders in `CollectionsView.vue:383-423`; finalize at `InvoiceDetailView.vue:1745`. **2.7 (mobile batch sync) is still undecided** — the endpoint remains, replay still goes per-URL |
| 3 — serialized, never rendered | ✅ fixed | `is_return_visit` renders on DispatchView + TechTimelineColumn; PO `received_date` is a column; budget seed returns its result. **Fixed 2026-08-22:** `_serialize_invoice` now emits `amount_paid` (and `credit_total`), derived from payments — the MobileBillingView "Paid" row it gates had never rendered |
| 4 — status-map divergence | ✅ fixed | zero invalid `'warning'` severities remain in any `.vue` file |
| 5 — swallowed server detail | ✅ fixed | the Tier-1.2 onboarding raw fetch was the only real swallow |
| 6 — write-path data loss (12) | ✅ fixed | technician name/email/phone on both models (`technicians.py`); `expenses.py:54/68` accepts `status`; `customers.py:146` `access_notes`; `jobs.py:629` honours `date=` |
| 7 — permission / module gating | ✅ fixed | forecasting GETs carry `require_permission("accounting.read")`; nav entries key on real modules via `requires:` (`modules.js:46/59/61/160`) |
| 8 — dead frontend surface | ✅ fixed | `AdminSettingsView.vue` deleted |
| 9 — customer-facing documents | ✅ headline fixed | `mobile_invoicing.py:969` now passes `subtotal`/`tax_amount`/`balance_due`, so truck "Generate & email" actually sends |
| 10 — invisible background state | ✅ resolved / ⛔ split | **Resolved (not QB):** the `tasks/reminders.py` stub and its hourly beat entry were removed 2026-08-22 — it could never send, because prod has no SMS transport configured. **⛔ Won't fix (QB phase-out 2026-08-21):** the zero `ItemRef` in `quickbooks/sync.py`, per-record push state missing from lists, and push failures surfacing nowhere — all push-side work for a book we no longer write to |
| 11 — dark mode + dead tail | 🟡 partly | scheduling has a real UI (`SchedulingView.vue`); the dark-mode items were not individually re-checked in the 2026-08-21 sweep |

Two tiers were **not** re-verified item-by-item on 2026-08-21 and should not be
read as clean: Tier 11's dark-mode list, and the long dead-endpoint tail. Saying
so is the point of keeping score.

---

## Tier 1 — Broken today (frontend calls nothing serves)

### 1.1 `api.delete(...)` does not exist — 9 dead delete buttons  ⚠ systemic
`useApi()` / `useApiWithToast()` export `del`, not `delete`
(`composables/useApi.js:160`). Every `api.delete(...)` call invokes
`undefined` → `TypeError` → the row is never deleted; where there's no
try/catch the button silently does nothing.

| Caller | What breaks |
|---|---|
| `views/CampaignsView.vue:233` | delete campaign |
| `views/ChangeOrdersView.vue:314` | delete change order |
| `views/CatalogView.vue:579` | delete catalog item |
| `views/CatalogView.vue:606` | delete catalog |
| `views/admin/PayrollView.vue:155` | delete payroll entry (declares a `successMessage` that can never fire) |
| `views/PurchaseOrdersView.vue:385` | delete purchase order |
| `views/VendorsView.vue:193` | delete vendor |
| `views/LaborMatrixView.vue:361` | delete labor-matrix item |
| `composables/usePushSubscription.js:128` | push unsubscribe |

Fix: rename to `api.del` per site, or add a `delete` alias in `useApi` (one
line) — prefer the alias plus a lint rule so this can't recur.

### 1.2 Onboarding customer import always fails, and reports success
`views/OnboardingView.vue:61-71` posts `/api/admin/import/customers` with
`Authorization: Bearer ${auth.token}` — the auth store exports `accessToken`,
not `token` (`stores/auth.js:6`), so the header is `Bearer undefined` → 401.
The raw `fetch` never checks `resp.ok`, onboarding proceeds to
`/api/onboarding/complete` and routes to the dashboard. **A new tenant's CSV
customer import silently imports nothing.** Fix: use the shared api client
(correct token + error toast) and gate the completion step on success.

### 1.3 Edit commission rule → guaranteed 404
`views/CommissionsView.vue:228` PUTs `/api/commissions/rules/{id}`;
`routers/commission.py` defines only `GET /rules` and `POST /rules`. Rules can
be created but never edited. Fix: add the PUT/PATCH route (or repoint the UI).

### 1.4 Company-logo upload → silent 404
`views/SettingsView.vue:1220` (`uploadLogoIfPresent`) posts
`/api/settings/branding/logo`; no such route exists anywhere
(`routers/settings.py` has only GET/PATCH `/branding`). The surrounding
branding PATCH succeeds, so saving *looks* fine while the logo never persists.
Fix: implement the upload route or drop the control.

### 1.5 Dashboard fallback revenue KPI sums a nonexistent field
`views/DashboardView.vue:674` computes month revenue from `i.total_amount`
over `/api/invoices`, but `_serialize_invoice` (`routers/invoices.py:115`)
emits `total` — `Number(undefined) || 0` → the fallback KPI totals **$0**
whenever the primary reports endpoint fails/zeros (branch gated at `:634`).
Fix: read `total`.

---

## Tier 2 — The deposit pattern: backend fully built, no UI door

### 2.1 Customer-credit lifecycle + payment plans (whole A/R capability) ★ biggest
- `POST /api/invoices/{id}/credit-memo` (`routers/invoices.py:2180`) — zero callers
- `POST /api/invoices/{id}/apply-credit` (`routers/invoices.py:2242`) — zero callers
- `POST /api/invoices/{id}/payment-plan` (`routers/invoices.py:2393`) — zero callers

Ledger-integrated on the backend. The office can *see* an existing credit memo
(adjustments panel added in PR #197) but can never issue one, apply a
customer's credit balance, or set up installments. This is the deposit story
repeated: finished backend, no entry point. Deserves its own build/plan.

### 2.2 Warranty claims — recordable, never filable
`POST /api/warranties/{id}/claim` (`routers/warranties.py:195`) and the entire
claims router (`routers/warranty_claims.py:77/:113/:132/:184`) have zero
callers. `WarrantiesView.vue:141` only offers a "Claimed" *filter*. A warranty
can be recorded; a claim against it can never be filed or tracked in the app.

### 2.3 Job dependencies — invisible and unsettable
`POST`/`GET /api/jobs/{id}/dependencies` (`routers/jobs.py:2719/:2761`) —
zero callers. Blocking relationships exist server-side only.

### 2.4 Receipt → expense promotion
`POST /api/expenses/promote-from-receipt` (`routers/expenses.py:566`) — zero
callers. Job-captured receipts can't become expense records from the UI,
breaking the receipt→bookkeeping handoff.

### 2.5 Per-invoice payment-reminder ledger
`GET`/`POST`/`DELETE /api/collections/reminders`
(`routers/collections.py:67/:84/:209`) — zero callers; `CollectionsView.vue`
uses only bulk `send-reminders` (`:462`). The per-invoice reminder history —
including the promised-payment-date field — is never shown or manageable.

### 2.6 Invoice finalize + batch create
- `POST /api/invoices/{id}/finalize` (`routers/invoices.py:2059`) — zero
  callers; no explicit lock action anywhere (locking only happens implicitly
  on paid/void).
- `POST /api/invoices/batch` (`routers/invoices.py:2093`) — zero callers; its
  per-job `errors[]` (`:2153`) would be invisible even if called.

### 2.7 Mobile batch sync — dead by divergence
`POST /api/mobile/sync` (`routers/mobile.py:4043`) is never called: offline
replay (`composables/useOfflineSync.js`) replays each queued action against
its original URL. The batch endpoint's `failed`/`skipped_duplicates`
accounting (`:4191`) is maintained but unreachable. Decide: delete the
endpoint or migrate replay onto it.

Note: `POST /api/jobs/{job_id}/create-invoice` (found in the deposit audit)
remains caller-less too — all Create Invoice buttons route to `/billing/new`.

---

## Tier 3 — Serialized but never rendered (data captured, never shown)

- **`is_return_visit`** (jobs, `routers/jobs.py:455`) — zero frontend refs,
  even though spawn-return-visit IS wired
  (`components/JobStateOverrideDialog.vue:152`). A warranty return-trip
  renders identically to fresh work in every list/detail — dispatchers and
  techs can't tell. Cheap, high-value badge.
- **`received_date`** (+ `quantity_received`) on POs
  (`routers/purchase_orders.py:134/:118`) — never displayed; the UI shows only
  the received *status*. No received-on date weakens the receiving/audit
  trail. (`quantity_received` is currently always all-or-nothing.)
- **Live silent result:** `useBudget.js:62` awaits `POST /api/budgets/seed`
  but discards the returned `{created, skipped_existing}`
  (`routers/budgets.py:700`) — "0 created, 40 skipped" is indistinguishable
  from success. Good precedents that DO surface such fields:
  `MobileJobsView.vue:68` (`truncated`), `AccountingLedgerView.vue:102`
  (`skipped_invoices`), `MobileInvoiceDialog.vue` (`deposit_netting.skipped`,
  PR #197).
- **Latent:** `_serialize_invoice` never emits `amount_paid`; every
  `total − amount_paid` fallback computes `total − 0`. All current fallbacks
  are guarded by `balance_due ??`, so dormant — but adding `amount_paid` to
  the serializer removes the trap.

---

## Tier 4 — Status-map divergence + invalid `'warning'` token

Project is PrimeVue 4 (valid Tag severities: `secondary/success/info/warn/
danger/contrast`). The PrimeVue-3 token `'warning'` renders **colorless**.
PR #197 consolidated the five invoice maps into
`utils/statusSeverity.js#invoiceStatusSeverity`; these remain:

- **Estimates:** `views/EstimatesView.vue:293` and `views/EstimateView.vue:1321`
  carry identical hand-rolled maps with a dead `Converted` key (backend enum:
  draft/sent/accepted/declined/rejected/expired — conversion only sets
  `job_id`). Expired/rejected estimates render neutral grey instead of the
  util's `warn`/`danger`. Fix: import `estimateStatusSeverity`.
- **Appointments disagree per view:** `views/AppointmentsView.vue:346` vs
  `views/JobDetailView.vue:1160` map the same statuses differently
  (`scheduled`: 'warning'(invalid)↔'info'; `confirmed`: 'info'↔'success';
  `en_route`: 'info'↔'warning'(invalid); `arrived`: 'success'↔'warning'
  (invalid)). Same appointment, different color per screen.
- **Timeclock disagrees desktop vs mobile:** `views/TimeclockView.vue:476`
  (clocked-out → danger) vs `views/MobileTimeclockView.vue:207` (→ secondary);
  both use invalid 'warning' for on-break.
- **Remaining invalid-'warning' sites (colorless tags):**
  `views/PaymentsView.vue:268` (Voided), `views/LeadsView.vue:429`
  (Contacted), `views/PayrollView.vue:156` (pending/processing/running),
  `views/ChangeOrdersView.vue:195` (pending_approval),
  `views/CollectionsView.vue:323`.
- **VendorBillsView** `void` → `secondary` (`views/VendorBillsView.vue:147`)
  vs the shared util's `contrast` — minor, but pick one.

Fix pattern: grow `utils/statusSeverity.js` (appointment + timeclock + lead +
payroll maps), import everywhere, and add a vitest source-assertion banning
the literal `'warning'` severity in `.vue` files.

---

## Tier 5 — Swallowed server detail (write ops)

`useApi.fireError` surfaces server `detail` by default, so the genuine
swallows are raw-`fetch` writes without `resp.ok` checks:

- **`views/OnboardingView.vue:61-71`** — the Tier 1.2 import; any 4xx/5xx
  explanation is discarded and onboarding claims success.

Checked clean: `VendorStatementsView.vue:157-179` (proper 409/detail
handling), mobile write catches pass `err?.message`,
`FeedbackPortalView.vue:213` double-toasts but doesn't suppress the server
detail. Portal `authedFetch` was fixed in PR #197.

---

## Tier 6 — Write-path data loss (UI sends it, backend silently drops it, user sees "Saved")

Round 2 of the sweep (same day). Each finding cites both sides; the two most
severe were independently re-verified.

1. **Technician name/email/phone/certifications/work-hours — dropped on BOTH
   create and update.** `TechniciansView.vue:203-211` sends them;
   `routers/technicians.py:34-44` models accept only
   `user_id/skills/hourly_rate/active`. `name/email/phone` are real columns
   (`tenant_models.py:2840`) that are simply never written; certs/work-hours
   aren't columns at all. Editing a technician does essentially nothing.
2. **Expense approval workflow is a complete no-op.** `ExpensesView.vue:461`
   PATCHes `status` (Draft→Submitted→Approved→Reimbursed UI at `:178/:259`);
   `routers/expenses.py:46-62` models have no `status`, the `Expense` model
   has no status column, and the list re-derives everything as 'Draft'
   (`ExpensesView.vue:394`). Every approval reverts on reload.
3. **Equipment make/type — field-name mismatch both directions.** UI sends
   `make`/`type` (`EquipmentView.vue:224-227`); backend expects
   `manufacturer`/`equipment_type` (`equipment_tracking.py:50-57`). Edits
   don't save AND the list reads `item.make`, which the API never returns.
4. **Customer access notes never save** — `CustomerDetailView.vue:362/:1031`
   sends `access_notes`; `customers.py:53-66` model omits it and no column
   exists.
5. **Lead stage dropped on update** (saves on create only) —
   `LeadsView.vue:535` vs `leads.py:88-96` `LeadPatch`. Only the separate
   advance-stage button works.
6. **Task related job/customer dropped on update** — `TasksView.vue:294` vs
   `tasks.py:50-56` `TaskPatchIn`.
7. **PO order_date dropped on update** — `PurchaseOrdersView.vue:360` vs
   `purchase_orders.py:311-336` `update_po`.
8. **Service agreement start_date/template/customer dropped on update** —
   `ServiceAgreementsView.vue:449-452` vs `service_agreements.py:71-77`.
9. **Appointment job/customer relink dropped on update** —
   `AppointmentsView.vue:500-501` vs `appointments.py:169-182`.
10. **Change-order relink half-applies** — `change_orders.py:300-303` updates
    `customer_name` but never `job_id`/`customer_id`: display and storage
    silently split (`ChangeOrdersView.vue:281`).

**Query params:**
11. `/api/invoices` `status` Literal (`invoices.py:539`) is dead — no caller
    sends it; BillingView filters client-side and has no Void/Partial tab, so
    those states are reachable only under "All".
12. **`/api/jobs?date=` is ignored server-side** (`jobs.py:541-551` has no
    `date` param) and neither dispatch board passes `per_page`, so the
    default page cap of 50 applies — **on a tenant with >50 jobs, jobs for
    the selected day can be missing from the dispatch board entirely.**
    (`DispatchView.vue:1513`, `MobileDispatchView.vue:191`.)

Adjacent read-path: branding GET returns `accent_color` but the form reads
`secondary_color` (`settings.py:150` vs `SettingsView.vue:1186`) — saved
accent color never reloads.

Checked clean: customer main fields, jobs update, estimate create/patch,
vendors, catalog patch, branding PATCH write side, mobile customer edit.

---

## Tier 7 — Permission / module gating mismatches

Mechanism: every backend module is seeded enabled on first boot
(`core/modules.py:110-132`); frontend nav = module enabled AND permission held
(`useTenantModules.js:104-111`). Several nav entries key on strings that are
NOT backend modules (`maps`, `reports`, `technicians`, `fleet`), so disabling
the real module can never hide them.

**Visible-but-403s (type a):**
- **Service Agreements**: nav `nav.office` (`constants/modules.js:90`) but
  every endpoint requires `settings.write` (`service_agreements.py:33`) —
  dispatcher/sales/accounting/viewer open a fully rendered page of 403s.
- **Maintenance Plans**: nav `nav.office` (`modules.js:39`) vs
  `require_role(admin,owner)` (`maintenance.py:34`) — same four roles walled.
- **Invoice Reminders**: gated `invoices.read_all` (`modules.js:105`,
  `router/index.js:204`) vs admin/owner-only backend
  (`invoice_reminders.py:32`) — accounting sees a dead Reminders tab.
- **Dispatch board sub-panel**: TechEfficiencyPanel
  (`DispatchView.vue:433`) needs `dispatch.read`
  (`tech_efficiency.py:137`) — 403s for sales/accounting inside a page they
  can open.

**Module-key mismatches (type c — disabling the module leaves the UI up):**
- Maps nav keys on `maps`, endpoints require `google_maps`
  (`modules.js:45` vs `maps.py:19`).
- Reports nav keys on `reports`, endpoints require `reports_advanced`
  (`modules.js:144` vs `reports.py:21`).
- Technicians nav keys on `technicians`, endpoints require `dispatch`
  (`modules.js:40` vs `technicians.py:31`).
- Fleet cluster keys on legacy `fleet` alias vs `equipment_tracking`
  (`modules.js:43/:49`, `fleet.py:24`, alias `core/modules.py:59`) — one
  toggle hides half the surface.
- **Notification bell**: rendered unconditionally (`AppTopbar.vue:136-151`)
  while all `/api/notifications*` require the `communications` module
  (`notifications.py:31`); the store collapses 403 to zero — with
  Communications off, notifications are silently lost behind a
  permanently-empty bell.

**Hidden-but-capable (type b) + security note:**
- **Forecasting**: nav is admin/owner-only (`modules.js:146`) but route guard
  + backend admit `accounting.read` (`router/index.js:209`) — and the GET
  endpoints (`modules/forecasting/router.py:52,75,108`) have **no
  authorization beyond login at all**: any authenticated user, including a
  technician, can pull revenue projections by URL. ⚠ Treat the missing
  server-side gate as a security fix, not a UX fix.

---

## Tier 8 — Dead frontend surface

- `views/AdminSettingsView.vue` — fully orphaned (no route, no importer;
  `/admin-settings` redirects to `/settings` at `router/index.js:296`).
  Retire it.
- `AuditLogViewer` (`/admin/audit-log`, `router/index.js:361`) — live
  backend, but the only in-app link lives inside the dead AdminSettingsView →
  URL-only. Decide: nav entry or retire (ActivityView is the live surface).
- `AIAssistantView` (`/ai-assistant`, `router/index.js:302`) — functional
  wrapper, no nav/link (topbar panel is the real UI). Confirm it's an
  intentional deep-link target.
- (Positive: no orphaned components; `_ViewTemplate.vue` is intentional.)

---

## Tier 9 — Customer-facing documents (PDFs + outbound email)

Round 3. The two HIGH findings were independently re-verified.

1. **⚠ FIX IMMEDIATELY — mobile invoice email NEVER sends (verified).**
   `routers/mobile_invoicing.py:721-737` calls `build_invoice_email_html`
   without `subtotal`/`tax_amount`/`balance_due`, which have no defaults
   (`core/email_sender.py:156-169`) → TypeError, swallowed by the enclosing
   try/except → `return False`. A `# type: ignore[misc]` silenced it; every
   test mocks `_send_invoice_email`. Every truck "Generate & email" and
   re-send since mobile invoicing shipped has delivered nothing.
2. **Invoice PDF is blind to adjustments (verified).** `_invoice_payload`
   (`routers/pdf.py:206-274`) never queries `InvoiceAdjustment`; the template
   prints Subtotal/Tax/Total/Paid/Balance with no credit line — a
   credit-memo'd invoice's PDF shows numbers that don't reconcile
   (Total − Paid ≠ Balance Due, credit invisible).
3. **PDF template editor offers 6 doc types; only estimate + invoice render
   the config.** work_order/safety_checklist/purchase_order have no renderer
   at all; install_sheet renders its own template ignoring the saved config
   (`PdfTemplateEditorView.vue:186-193`, `core/pdf_generator.py:205-221`,
   `install_sheet.py:452-466`). Tenants can configure branding that never
   appears anywhere.
4. **Invoice email body omits Paid-to-Date** while its attached PDF shows it
   (`core/email_sender.py:223-240`) — body totals don't foot on
   partially-paid invoices.
5. **Composer draft says gross "Total", never balance due**
   (`routers/invoices.py:1266-1272`; ctx computes `balance_due` at `:1326`
   but the template never uses it).
6. **Tiered proposals render flat** — PDF payload and totals use base
   `EstimateLine`s, never `ProposalTier`/`accepted_tier_id`
   (`routers/pdf.py:149-203`, `modules/proposals/totals.py:88-125`); the
   printed total can differ from the tier the customer picked.
7. **Online card payment sends no receipt** — `# send_receipt_email(invoice)`
   is a commented-out placeholder (`core/payments.py:509-513`); no receipt
   email exists for pay-link payments.
8. **Estimate emails never include the accept link** — the builder's CTA only
   renders when `portal_url` is passed, and no send/compose path passes it
   (`routers/estimates.py:1459-1467`, `core/email_sender.py:111-117`).
9. Install Sheet hardcodes `company_name="DispatchApp"`
   (`install_sheet.py:454-455`).
10. Netting credit prints as `$-500.00` (sign after `$`)
    (`templates/_pdf_line_items.html:21-22`, `email_sender.py:180-181`).
11. Editor-saved `logo_url` is never read by the renderer
    (`pdf_templates.py:61` vs `pdf_generator.py:143-160`).
12. **Estimate-nurture "sends" nothing** — the run endpoint only inserts a
    log row; zero email calls in the file (`estimate_nurture.py:170-175`).
    UI reports follow-ups as sent that were never delivered.

Verified clean: reminder/dunning amounts use `balance_due`; reminder +
estimate-compose placeholders all populate; "% Down" consistent PDF↔accept;
pay-link suppressed on void/zero across every path; netting line + totals on
final-invoice PDF/email correct; `hide_line_prices` honored.

---

## Tier 10 — Invisible state: QuickBooks sync + background jobs

Round 3. The three most dramatic claims re-verified (reminders stub,
next-actions renderer absence, ItemRef absence).

**QuickBooks:**
- **Per-record sync status invisible** — `qb_dirty`/`qb_synced_at` are
  written (`quickbooks/sync.py:1275/:1412`) but serialized nowhere and
  rendered nowhere; the office can't tell pushed/failed/never-tried per
  invoice or customer.
- **Push failures surface nowhere** except the hand-clicked button's toast:
  `_touch_sync_error` is pull-only; background push failures go to a
  `failed_permanent` list in the Celery result backend that nothing reads
  (`quickbooks/tasks.py:120-131`).
- **The live status path hardcodes `last_error: None`**
  (`quickbooks/router.py:280-282`) — the one error banner is fed a blank on
  the modern token-store path.
- **The invoice mapper is blind to the deposit lifecycle and likely broken
  outright (verified: zero `ItemRef` in sync.py).** Lines push as
  `SalesItemLineDetail` with no ItemRef (Intuit-required → create likely
  400s); no push for payments/credit-memos/adjustments; `billing_type`
  ignored, so a deposit invoice would push as a full standalone unpaid
  invoice. Relevant to the "catch QB up" plan — books would diverge silently.
- **No ambient connection-health indicator** — `needs_reconnect`/`auth_state`
  are returned and consumed by nothing; a dead token silently no-ops every
  sync (`quickbooks/tasks.py:142-154`). The dead-QB incident can recur
  undetected.

**Background jobs:**
- **The NextAction system has no renderer (verified: zero frontend refs).**
  The entire billing-leak loop (`tasks/billing_followup.py`) and the weekly
  overdue nudge write NextActions nothing displays — corroborated by the
  in-repo comment at `routers/timeclock.py:961-963`.
- ~~**Appointment reminders are a scheduled stub (verified).**~~
  ✅ **Resolved 2026-08-22 by deletion.** It was three stubs
  (`_find_upcoming_appointment_ids` -> `[]`, `_get_appointment` -> `None`,
  `_send_sms` -> no-op) firing hourly on prod and logging
  `scheduled_count: 0` forever; its only test monkeypatched all three. Module,
  beat entry, celery registration and test removed. Reviving it needs a product
  go/no-go on automated customer SMS **and** wiring to the Phone.com sender
  (`modules/phone_com/client.py::send_message`) — `core/sms.py` is Twilio and
  has no credentials on prod, which is what the stub would have reached for.
- **estimate_followup**: registered, unscheduled, and would stamp
  `reminder_sent_at` without sending if ever run
  (`tasks/estimate_followup.py:46-50`).
- **Circuit breakers**: Redis-only, no endpoint, no view; `qb_circuit` isn't
  even wired into QB calls (`core/circuit_breaker.py:180`, `app.py:1094`).
- **Failed-task visibility**: legacy `/admin/tasks` HTML only (outside the
  SPA), and unhealthy-skips / partial-failure batches record as success
  (`core/task_monitor.py:122-137`).
- **QB banking sync**: per-entity pull failures are captured into the result
  and the run still records "ok" (`quickbooks/tasks.py:378-405`) — the
  schedule card shows green on partial failure.
- **Recurring generation**: no run history/failure surface
  (`tasks/recurring.py:16-28`).
- **Estimate auto-expire never fires** — it's a user-authenticated endpoint
  (`routers/estimates.py:2107-2110`), unscheduled and uncalled; stale
  estimates stay "sent" forever.

Verified clean: customer rolling-volume refresh (rendered w/ timestamp), QB
banking top-level status, QB delete-sync + reconciliation panels,
ledger-pulls-off banner, timeclock sweep exceptions, estimate_archive.

---

## Tier 11 — Dark mode (static scan) + dead-surface tail

**Dark-mode breaks** (root theme IS dark; hardcoded light values win over it):
- `components/NotificationsDrawer.vue:200-230` — dark-on-dark title/message
  text (`#0f172a`/`#334155` with no light background), light hover
  (`#f8fafc`) and unread (`#eff6ff`) blocks. The drawer we just added delete
  buttons to is itself unreadable in dark. (Verified against the file.)
- `views/MobileSummaryView.vue:144` — "Next stop" card `#eff6ff` bg, themed
  light text → unreadable on the tech's home screen.
- `views/InvoiceRemindersView.vue:407` — template preview `#f8fafc` bg,
  light-on-light.
- `views/JobCostingView.vue:1260-1292` — calculator panel light bg + dark
  labels; unreadable both ways.
- `views/AppointmentsView.vue:592` — unconfirmed-banner `#fff9e6`,
  light-on-light.
- `components/AIAssistantPanel.vue:259` — history rows `#f9fafb`,
  light-on-light.
- `views/MobileTodayView.vue:1595-1602` — offline/online banners hardcode
  light alert backgrounds.
- Theme-ignoring-but-readable pairs worth normalizing: LineItemEditor status
  pills (`:802-818`), AIAssistantPanel message cards, DocumentsView
  `.bulk-warn`. Clean: Billing/Customers core, DispatchView, DashboardView
  (icon tints only), signature canvases (intentionally fixed white).

**Dead-endpoint tail** (routers the first pass skipped; zero real callers):
- **Entire `routers/scheduling.py` calendar API orphaned**: month/week
  calendar, events, per-tech calendar, available-slots, conflict detection,
  tech-unavailability CRUD, recurring-schedule generate (`:175-:540`).
  Scheduling capability exists wholesale with no UI.
- Fleet service history + due-for-service (`routers/fleet.py:294/:330/:401`)
  — the preventive-maintenance half of Fleet is unreachable.
- Admin reconciliation report (`admin_ops.py:509`) and role/permission
  management (`admin_ops.py:448/:464`) — no UI.
- Winback candidates feed (`winback.py:240`) — the input list the feature is
  built around — and bulk-send follow-ups (`winback.py:478`).
- Suggested appointment duration (`appointments.py:346`); segment live
  count/member drill-down (`segments.py:381/:392`).
- Confirmed wired/expected-dark: change-orders approve/decline, maintenance,
  service-agreements, custom-fields, pdf-templates, labor-pricing, timeclock,
  appointments core, campaigns, leads, technicians, photos, all reports
  endpoints; GL + bank_feeds dark behind flags as intended.

---

## What this audit still does NOT cover

Sampling tails remain (~1,006 endpoints; serializer field checks sampled; the
write-path pass covered the main edit surfaces, not every dialog). Still
unaudited: generated `types/api.d.ts` drift, and **live-data correctness**
(pages rendering wrong numbers from real data — `/ux-audit` territory,
needs the running app, not static reads).

---

## Suggested fix order (all three rounds)

**Fix immediately (live breakage / security):**
1. **9.1** mobile invoice email TypeError — 3 missing kwargs; every truck
   email since launch silently undelivered. Smallest fix, biggest lie.
2. **Tier 7** forecasting GET endpoints: add server-side authorization
   (security, not UX).
3. **6.12** dispatch board `?date=`+pagination — jobs can be missing from
   the board on busy days.
4. **1.1** `api.delete` alias + 9-site sweep; **1.2** onboarding import.

**Fix soon (silent data loss / wrong customer-facing numbers):**
5. **6.1/6.2** technician edits + expense approval (persist or remove the UI).
6. **9.2** invoice PDF credit-memo line (pairs with the detail-view fix
   already shipped in PR #197); **9.4/9.5** email body Paid-to-Date +
   composer balance-due.
7. **9.7** payment receipt email; **9.8** estimate accept-link.
8. Remaining Tier 6 dropped-on-update fields; **1.3/1.4** missing routes.

**Fix deliberately (visibility & consistency):**
9. **Tier 10** pick the QB story before the office catches QB up: per-record
   sync status + push-failure surfacing + the deposit/ItemRef mapper gap.
10. **Tier 4 + 11** status-map consolidation, 'warning' ban test, dark-mode
    literal-color fixes (NotificationsDrawer first).
11. **Tier 10** NextAction renderer (or stop writing them), reminders stub
    (implement or unschedule), estimate auto-expire scheduling, circuit
    breaker/task-health surfacing.

**Plan as builds (backend exists, UI missing — decide build vs delete):**
12. **2.1** credits/payment-plans UI; **11** calendar/scheduling API;
    warranty claims; fleet service history; winback candidates; the rest of
    Tier 2/11.

*Companion history: the deposit-feature round that motivated this sweep is
PR #197; its own findings live in that PR body.*
