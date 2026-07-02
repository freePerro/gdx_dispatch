# UI/UX Audit — Action Plan

**STATUS: ALL ITEMS COMPLETED 2026-07-01** — implemented, unit-tested (838/838 green,
production build clean), and visually verified in a headed browser in both light and
dark mode on branch `feat/measurement-diagram`. Deliberate scope decisions are noted
inline (photos `capture` attr rejected, timeclock/quote-build not offline-queued,
offline reads deferred).

Compiled 2026-07-01 from a four-domain UX deep-dive (navigation/IA, pattern consistency,
mobile/offline, a11y/theming/perf), followed by a four-agent adversarial verification pass.
**Every item below survived verification** — refuted findings are listed at the bottom so
they don't get re-discovered and re-litigated later.

---

## 1. Fix on the current branch (`feat/plugin-declarative-ui`) before merge

- [x] **MeasurementDiagram: dark-mode invisibility.**
  The SVG paints its own light background rect, so light mode is fine. Elements rendered
  *outside* the rect sit on the page background and disappear in dark mode:
  - arrow strokes `#0f172a` — `frontend/src/components/MeasurementDiagram.vue:27,31`
  - "floor" label `#64748b` at y=316 (below the rect) — `MeasurementDiagram.vue:20`
  - HTML input labels `color: #334155` — `MeasurementDiagram.vue:77`
  Fix: CSS vars (`var(--text-primary)` etc.) for everything outside the fixed-background rect.

---

## 2. Small bug batch (~1 hour, real bugs)

- [x] **Broken `/voice` redirect → 404.** `/voice` redirects to `/phone-com`, which is not a
  route (only `/phone-com/calls|messages|faxes|cold-leads` exist). Legacy bookmarks land on
  NotFoundView. Point it at `/phone-com/calls`.
  `frontend/src/router/index.js:170` vs `:238-241`
- [x] **Duplicate `payroll` module key.** Two sidebar entries both labeled "Payroll" pointing
  at different views (`/payroll` in Financials, `/admin/payroll` in Admin) share one
  enablement key — can't disable one without the other; key-keyed maps silently collide.
  `frontend/src/constants/modules.js:84` and `:163`
- [x] **"Pull to refresh" hint text is a lie.** No pull gesture exists anywhere. Either
  implement it or fix the copy. MobileJobsView's empty state additionally has **no refresh
  affordance at all**.
  `frontend/src/views/MobileJobsView.vue:38`, `MobileTodayView.vue:1080`, `MobileInboxView.vue:22`

---

## 3. Offline: decide, then wire or remove (highest-impact field-tech item)

The queue infrastructure (`postQueued`/`patchQueued`, Dexie `sync_queue`, idempotency keys)
is fully built and has **zero callers**. Worse, the UI advertises it:

- [x] **The offline banner lies to techs.** MobileTodayView shows
  "*N action(s) will sync when you're back*" wired to a queue nothing writes to —
  `pendingCount` is permanently 0; a tech in a dead zone is told failed taps were saved.
  **Either wire the queue (below) or pull the banner. Do not ship the banner alone.**
  `frontend/src/views/MobileTodayView.vue:967-990`
- [x] **Wire mobile mutations to the queue** (swap `api.post()` → `api.postQueued()` and
  handle the `{ queued: true }` stub):
  - en-route / arrived / reorder — `MobileTodayView.vue:196,221,265`
  - closeout submit — `frontend/src/components/MobileJobCloseoutDialog.vue:201`
  - candidates: MobilePartsToOrderView:138, MobileTimeclockView:391-433,
    MobileChatDialog:81,98, MobileQuoteBuilderDialog:163
  - Prereq: confirm server-side idempotency middleware handles replays.
- [x] **Closeout dialog: block silent discard while offline.** On failed submit the dialog
  correctly stays open with signature/parts/hours intact (verified) — but the header X /
  Escape still discards everything with no warning. Add `:closable="false"` while a
  submission is pending/offline, or a dismiss-confirm.
  `MobileJobCloseoutDialog.vue:261`
- [x] **Note (deliberate scope call):** offline *reads* don't exist — `db.jobs`/`db.photos`
  stores are never written and `public/sw.js` intentionally caches nothing (cold launch
  offline = blank). Only pursue if you want a real PWA story; partial fixes buy little.

---

## 4. Mobile feature gaps (real feature work, ranked by tech-day impact)

- [x] **Field change-order initiation.** Tech discovers extra scope mid-job → must call
  dispatch. Add a "Request CO" action on the job card (description + rough amount, offline-
  queueable). No `/mobile/change-orders` needed — a dialog is enough.
- [x] **Field payment capture.** "Can I pay you now?" → "No." Deferred per sprint plan
  (`MobileInvoiceDialog.vue:9-11`). Start with a payment-link send + manual cash record;
  card-reader integration later.
- [x] **Purchase-order request from field** (or fold into the parts-to-order flow, which
  already exists on mobile).
- [x] *(Optional polish)* Photos: works today (primary tech bottom-nav tab, file input opens
  camera) but the view is desktop-shaped and lacks the `capture` attribute — a mobile-shaped
  capture flow with job context would tighten it.

---

## 5. Navigation / IA quick wins

- [x] **Rename the ambiguous pair + fix icons.** "Appointments" → "Appointment Confirmations",
  "Scheduling" → "Team Scheduling". Both views have bare `<h2>` titles, module entries have
  no description field, and sidebar tooltips just repeat the label. Bonus: Appointments and
  Planner share the identical icon (`pi pi-calendar-plus`).
  `frontend/src/constants/modules.js:22,24`
- [x] **Sub-group Financials** (13 flat items): Invoicing (Billing, Payments, Collections,
  Invoice Reminders) / Accounting (Expenses, Job Costing, Pricing, Labor Matrix, Vendor
  Statements) / Payroll (Payroll, Commissions). `modules.js:78-91`
- [x] **Three true orphan routes** — reachable only by direct URL (command palette does NOT
  index them; only modules.js entries are indexed). Add nav entries or delete:
  - `/admin/games` (linked only from inside the games section)
  - `/feedback` (nothing links to it)
  - `/phone-com/cold-leads` (siblings don't cross-link to it)
- [x] **Delete 5 dead view files** (hygiene only — verified they never enter any bundle
  chunk; redirect routes have no component): MessagesView, VoiceView, MarketingView,
  InboundCommsView, UploadsView.
- [x] **Messaging labels.** Communications vs Inbox vs Phone.com Calls/SMS are genuinely
  different channels (different backends — NOT duplication), but labels don't say so.
  Cheap fix: clearer names/subtitles. Long-term: unified inbox with channel tabs.

---

## 6. Consistency consolidation backlog

These measure *migration progress* of shared components built in the May-2026 remediation
wave — treat as a consolidation backlog, not a bug list. Batchable, mechanical.

- [x] **`useFormatters` migration.** Zero importers today; **62 views** define local
  `formatDate`/`formatCurrency`/etc. Migrate view-by-view (dates/currency only — file sizes,
  lat/long, ISO timestamps are out of scope for it).
- [x] **Empty states on ~25 genuinely silent *list* views** (e.g. CampaignsView, FleetView,
  EquipmentView, PaymentsView). 75 views already have inline `#empty` handling; don't touch
  those or non-list views. Use `EmptyState.vue` (icon + message + action).
- [x] **Dirty-check on high-traffic form dialogs only.** Real gap (~2 views check), but
  severity is moderate: `dismissableMask` defaults false, so loss requires Esc or the X —
  not click-outside. Shared `useDirtyDialog` composable; apply to the big form dialogs
  (customer, job, estimate, inventory, vendor), skip confirm dialogs.
- [x] **Standard DataTable recipe** for the major list views: sortable columns everywhere
  (PurchaseOrders, Vendors, Tasks, Billing lack them), `useListPrefs` (2 views use it),
  `useTableExport` (0 views use it), consistent rows-per-page.
- [x] **`FormField.vue` adoption** as views get touched (zero adopters; verified it IS a
  proper generic wrapper with label/error/aria wiring). This is also where per-field a11y
  comes for free.
- [x] **PdfTemplateEditorView: add a loading state** (async loads, zero feedback).
  `frontend/src/views/PdfTemplateEditorView.vue:177`
- [x] *(Cosmetic)* 6 views use raw `useConfirm` instead of the `useDestructiveConfirm`
  wrapper (AdminSettings, Estimates, Estimate, Billing, Leads, InvoiceDetail). Same
  ConfirmDialog renders either way — styling consistency only.

---

## 7. Accessibility & theming (verified subset)

- [x] **Keyboard-inaccessible interactive components:**
  - `frontend/src/components/TechTimelineColumn.vue:41` — job blocks are `div @click` +
    drag-only; no role/tabindex/keydown.
  - `frontend/src/components/FolderTreeNode.vue:9` — folder row is `div @click`; the
    toggle/delete buttons are real buttons, but row select/drag is mouse-only.
- [x] **Icon-only Buttons without accessible names** (~23 upper bound; PrimeVue does NOT
  synthesize a name from the icon prop). Add `aria-label`.
- [x] **No skip link.** Add one `<a href="#main">` in AppLayout.
- [x] **ReportsView chart colors hard-coded** (`#94a3b8` ticks ≈ 2.6:1 on white — WCAG
  miss; `#1e293b` grid heavy on white). No view themes charts reactively and no shared
  helper exists — build one small chart-theme util reading CSS vars, use it in ReportsView
  first. `frontend/src/views/ReportsView.vue:140-141,159`
- [x] **Remaining hard-coded colors** worth a sweep: BrowserStream status dots,
  MobilePartsToOrderView status pills, TimeclockView GPS indicators, PdfTemplateEditorView
  preview grays.
- [x] *(Deprioritized — deliberate)* i18n: zero callers of the homegrown `useI18n`, not
  wired in main.js, ~1,164 hard-coded strings. Single-tenant US shop → leave dormant or
  delete the scaffold.

---

## 8. Performance (what actually survived)

- [x] **Client-side full-dataset fetches:** JobsView/CustomersView fetch `?per_page=1000`
  then paginate client-side (`frontend/src/views/JobsView.vue:997`). DOM is bounded (≤100
  rows) so rendering is fine; the cost is payload/memory. Move to server-side pagination
  (`lazy` DataTable) if/when datasets grow. **Not urgent.**
- Already verified as good: route lazy-loading (5 eager / 120 lazy), polling (45s, pauses on
  hidden tab), toast conventions, touch-target sizing (52–56px), mobile/desktop split
  (intentional, low duplication).

---

## Refuted — do NOT spend time on these (adversarially verified)

| Claimed | Reality |
|---|---|
| 6 views delete without confirmation | All 6 confirm (template Dialogs / `window.confirm` / `confirmAsync`). Zero work. |
| MeasurementDiagram breaks in *light* mode | Inverted — dark mode breaks (see §1). |
| Add `virtualScroll` to big DataTables | Tables paginate; DOM ≤100 rows. Would change nothing. |
| MobileCustomerDetailView: 9 serial fetches | 1 awaited fetch + lazy per-tab loads. No waterfall. |
| CommunicationsView N+1 | On-demand + cache-guarded thread loading. Correct as-is. |
| No field photo capture for techs | Photos is a primary tech bottom-nav tab; file input opens camera. |
| `/margin-tiers` orphaned | Linked from CatalogView (test-asserted) + embedded Settings tab. |
| Closeout data lost on failed submit | Dialog stays open, state intact; loss only via explicit dismiss (§3 fix). |
| Deleting deprecated views shrinks bundle | They never enter any chunk. Hygiene only. |
| SMS duplicated in two nav places | Different backends/channels. Labeling issue only. |
