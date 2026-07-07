# Billing Capture Hardening — Implementation Plan (DRAFT, awaiting Doug approval)

**Date:** 2026-07-07 · **Status:** v3 — audit round 1 folded in, all 5 open decisions resolved with Doug (see § Decisions). Awaiting final go-ahead to implement.
**Audit round 1 [AUDIT-R1]:** adversarial reviewer verified plan claims against code. Confirmed PR 1 wholesale. Four substantive corrections folded in: (a) canonical "billed" predicate — $0-draft invoices must not count as billed, RFB endpoints must align (they don't exclude void today); (b) forecasting + next_action `billing_status` clauses are dead tautologies — fix is deletion (zero behavior change), not a semantic swap; the deposit-subtraction forecasting improvement is a separate /audit-gated follow-up; (c) PR 3 stamp must GATE the line copy (UPDATE…RETURNING) or COs double-bill — same latent flaw retrofitted for the S122 parts path; CO tax semantics made an explicit Doug decision; (d) PR 4 fuzzy upsert-match replaced with per-event rows + `source` column + operator-reviewed checklist (undercount generator eliminated); PR 6 idempotency keyed on stored `threshold_days`, and recurring billing uses an extracted `create_invoice_core` (router handler isn't Celery-callable).
**Trigger:** Doug: "make sure every part gets captured, every job has a good workflow, and we get billing done."
**Basis:** three-agent code audit 2026-07-07 (job lifecycle / parts capture / billing-AR). Full leak map in session + memory `cashflow-leak-audit-2026-07-07`.

## The verdict being fixed

The system has *visibility* (Ready-to-Bill list, billing summary) but almost no *enforcement*. Confirmed leaks, ranked by money-at-risk:

1. Parts logged at closeout / mobile / van never reach billing (3 of 4 capture paths are cost-only).
2. Approved change orders are captured, signed, then orphaned — no path to an invoice.
3. Nothing gates or chases a completed-but-uninvoiced job beyond a passive list.
4. `Job.billing_status` never advances → unbilled-work alert, invoice-now nudge, and forecast revenue are all wrong.
5. Draft invoices are invisible to every AR KPI; recurring billing is entirely dead (task not scheduled, INSERT broken).
6. Reminders don't send (endpoints only log rows); no dunning automation; collections aging report always returns $0 (case bug + deprecated `amount_paid`).

**Out of scope (already owned elsewhere):** Stripe Payment-row consolidation + webhook wiring (GL Phase 1 §5.4/§12 — dormant, Stripe not in use); QB payment-push (GL Phase 1 P3/push design supersedes it — building it twice is waste; office reconciles manually in QB until GL cutover); job-profitability true costing (GL parts-inventory CPA question pending).

## Design principles

- **Derive, don't cache** — the display-state model locked 2026-05-17 (`core/job_display_state.py`) is the canonical answer to "what state is this job's money in." No new status caches.
- **Checklist, not silent auto-add** — the office reviews what lands on an invoice (existing S122 parts-checklist pattern). We widen what feeds the checklist; we don't auto-append lines behind the operator's back. Exception: the explicit one-click `create-invoice` convenience path, which is documented as "pull everything billable."
- **Never block the tech in the field** (Doug 2026-05-10) — completion gates stay tenant-opt-in; enforcement is aggressive surfacing + daily follow-up loop, not hard blocks.
- **Fail loudly** — every new automation reports counts + skip reasons; no silent no-ops (the lesson of the dead reminder/late-fee/recurring tasks).

---

## PR 1 — Quick wins: make existing AR surfaces tell the truth

*No schema changes. Lowest risk, ships first.*

1. **Collections aging fix** — `routers/collections.py:157`: status filter `("Sent","Overdue","Partial")` → lowercase reality. New predicate: `status.in_(("sent",)) OR (balance_due > 0 AND status NOT IN ("draft","void","paid"))` — simplest correct: `deleted_at IS NULL AND status NOT IN ("draft","void") AND balance_due > 0`. Amount from `balance_due` (not `total - amount_paid`; `amount_paid` is the deprecated field the GL audit flagged). Keep response shape (CollectionsView unchanged).
2. **Zero-price guard wiring** — `modules/catalog_policy/service.py:80` `block_or_warn_invoice_line()` is dead code. Call it from `create_invoice` (`routers/invoices.py:497`, both estimate-copy and inline-lines branches) and the add-line endpoint. Defaults stay permissive (`warn` on, `block` off) — behavior change is a warning surfacing, not a 422.
3. **Draft-invoice surfacing** — `billing_summary` (`invoices.py:344`): add `draft_count` + `draft_total` (deliberately separate from `total_outstanding`, which stays receivables-only). BillingView: chip/row "N drafts never sent, $X" linking to the drafts filter (list endpoint already supports `status=draft` via `effective_status` filter).
4. **`/send` voided-invoice guard** — `invoices.py:1030` resurrects voided invoices (GL doc §12). One `status == "void"` → 409 check. Tiny, pre-existing prod defect, fits here.

**Tests:** unit tests for aging predicate (fixture invoices across statuses/dates incl. lowercase + balance edge), zero-price warn path, draft summary counts, void-send 409.

---

## PR 2 — `billing_status` fix: convert readers to the derived truth

*Doug chose "fix billing_status." Recommended shape: make every reader derive from invoices (the locked model), keep the column as a write-only legacy default for now, drop it in a later cleanup PR. Making the cache "real" instead would need updates on invoice create/void/delete + payment + a nightly overdue sweep + multi-invoice semantics + backfill — high complexity to duplicate what `derive_job_display_state` already answers. If Doug specifically wants the column live, that alternative is Appendix A.*

**[AUDIT-R1] Canonical "billed" predicate — one shared helper, because "any live invoice ⇔ billed" is false.** `create_invoice_from_job` fabricates a $0 draft line when no estimate exists (`jobs.py:1663-1664`), and PR 6 mass-creates recurring drafts — a naive NOT-EXISTS would make those jobs invisible to every alert (silent false negatives, the worst trade for this project). New `core/billing_predicates.py` with ONE definition used everywhere:

> A job is **billed** iff it has an invoice with `deleted_at IS NULL AND status != 'void' AND (total > 0 OR status != 'draft')`.

i.e. a $0 *draft* does not count (that's the auto-fabricated placeholder); a $0 invoice deliberately *sent* does (warranty work — don't nag forever). Both existing Ready-for-Billing queries (`jobs.py:1568`, `invoices.py:405`) currently exclude neither void nor $0-drafts and MUST be converted to the same helper — otherwise the recommendation and the RFB list disagree ([AUDIT-R1]: display state already excludes void; the RFB endpoints don't — pre-existing inconsistency, fixed here).

1. **`core/recommendations.py:547` (unbilled_work_alert)** — replace `billing_status == "unbilled"` with NOT-billed per the shared predicate (keeps its existing `lifecycle_stage == 'completed'` filter). Alert becomes truthful.
2. **`core/recommendations.py:162` (invoice_now)** — same swap.
3. **`recommendations.py:568` action_url** — `/jobs?billing_status=unbilled&stage=completed` → point at the Billing "Ready to Bill" surface (BillingView review tab).
4. **`core/next_action.py:316`** — [AUDIT-R1 correction] this is the stale-*estimate* rule; its `billing_status == "unbilled"` clause is a tautology today (the column is always `unbilled`). Fix = **delete the dead clause**, zero behavior change. Do not "swap" it.
5. **`modules/forecasting/service.py:214`** — [AUDIT-R1 correction] the window filter is `lifecycle_stage IN ('scheduled','estimate','service_call')` — pre-completion jobs, where the column is always `unbilled`, so this clause is also a **dead tautology**. Fix = delete it, zero behavior change. The real forecasting improvement (subtract already-invoiced deposit/up-front amounts from projected job revenue instead of binary include/exclude) is a **separate follow-up**, /audit-gated per the forecasting-accuracy standing rule — NOT smuggled into this PR.
6. **Serialization** — keep returning the raw field in `jobs.py:431/599` + gdpr export (frontend only references it in a comment; nothing breaks). Mark deprecated in the model comment. Column drop + `BILLING_TRANSITIONS`/`validate_job_transition` removal (exported, never called) → separate later cleanup PR, NOT this batch.

**Tests:** predicate helper unit tests: completed+sent-invoice → billed; completed+void-only → NOT billed (must alert); completed+$0-draft-only → NOT billed; completed+$0-SENT → billed. Parity: recommendation, RFB endpoint, and invoice-summary count all agree on the same fixture set (all three use the helper). Forecasting/next-action: snapshot-diff test proving the dead-clause deletion changes no output.

---

## PR 3 — Change orders reach the invoice

*Schema: one nullable column. The CO checklist mirrors the S122 parts-checklist pattern exactly.*

1. **Migration** — `change_orders.billed_invoice_id` UUID NULL, FK → invoices.id ON DELETE SET NULL, indexed (same shape/comment as `job_parts_needed.billed_invoice_id`, S122). Alembic migration; `change_orders` is created by the CO router's ORM model — verify at implementation time which plane owns the table (baseline vs create_orm_tables, per #41 pattern) and write the migration accordingly.
2. **Unbilled-CO query** — `GET /api/change-orders?job_id=X&unbilled=true`: `status='approved' AND billed_invoice_id IS NULL AND deleted_at IS NULL`.
3. **Invoice create integration — [AUDIT-R1] the stamp GATES the copy.** `InvoiceCreateIn` gains `from_change_order_ids: list[UUID]`. Order of operations inside the transaction: **stamp first** — `UPDATE change_orders SET billed_invoice_id = :inv WHERE id IN (...) AND billed_invoice_id IS NULL AND status='approved' ... RETURNING id` — then copy `ChangeOrderLine` → `InvoiceLine` **only for the RETURNING'd ids**; any requested CO not returned → 409 `already billed on invoice X` (whole transaction rolls back — the operator retries without it). The naive S122 mirror (copy from payload, stamp best-effort) double-bills sequentially and under race: lines copied to invoice B while the stamp no-op'd because invoice A owns the CO. That is the failure mode worse than the disease; the RETURNING gate is non-negotiable. Delete-invoice + line-removal release the stamp (same as parts, `invoices.py:864/1284`).
4. **[AUDIT-R1] Same latent flaw exists in the S122 parts path** (`invoices.py:685-701` stamps after line insert, guard only prevents re-stamping, not re-copying). Apply the same stamp-first-RETURNING rule to `from_part_ids`/line-level `part_id` stamping in this PR (shared helper).
5. **Tax semantics — RESOLVED (Doug 2026-07-07): COs are handled like invoices, tax shown to the customer.** `ChangeOrderLine` has no `taxable` flag today; copied lines defaulting `taxable=True` under a rate-mode invoice would add tax the customer never saw ($500 signed → $541.25 billed). Fix: migration adds `change_order_lines.taxable` BOOL default TRUE (forwarded on copy), and the CO detail/approval/signature view computes + displays subtotal / tax / total via the same rate resolver invoices use (`modules/tax/service.resolve_rate`), so the signed total equals the invoice total. CO serialization gains subtotal/tax_amount/total. Test asserts signed CO total == invoice total for the CO's lines, rate-mode and flat-tax both.
6. **One-click path** — `create_invoice_from_job` (`jobs.py:1600`): auto-append approved unbilled CO lines + stamp via the same gate (documented pull-everything path).
7. **UI** — InvoiceCreateView: "Approved change orders on this job" checklist section beside the parts checklist (same component pattern per D-S122 note that CO lines mirror `<LineItemEditor>` shape).
8. **Surfacing** — a job whose invoice exists but which has an approved unbilled CO is billing-incomplete. Add to the PR-5 follow-up loop (not a new display state — the locked state model stays untouched).
9. **CO lifecycle** — on billing, set CO `status='completed'`? NO — keep status independent; `billed_invoice_id` is the billing truth (status stays approval-workflow truth). Documented in the model comment.

**Tests:** stamp-gates-copy: billing the same CO on a second invoice → 409 AND zero lines copied (sequential + two-session concurrent test); invoice total == signed CO amount under the chosen tax semantics (explicit assertion, not "recompute ran"); delete-invoice releases CO; one-click path includes COs; unbilled filter; parts path retrofit: same double-bill test for `from_part_ids`.

---

## PR 4 — Parts capture unification: one billable spine

*Schema: two nullable columns. `JobPartNeeded` (already integrated with invoicing) becomes the single billing checklist; the three cost-only paths start feeding it. **[AUDIT-R1] Redesigned: no fuzzy matching, no overwrites** — the v1 sku/name upsert-match was an undercount generator (two legitimate same-sku closeout lines collapse to one; a qty-2 request clobbered by a qty-1 partial-visit closeout; undefined when two rows match). Every capture event gets identity; nothing is ever silently merged or overwritten.*

1. **Migration** — `job_parts_needed.unit_price` Numeric(10,2) NULL (suggested sell price; NULL = office prices it) **and `job_parts_needed.source` String(20) NOT NULL DEFAULT 'request'** (`request` | `closeout` | `mobile` | `van`). New status value `used` (status is String(20), no enum change needed).
2. **Closeout feed** — in `closeout_job` (`jobs.py:1404-1455`), same transaction: the closeout is the tech's attested parts statement for the job, and re-closeout replaces it. Event identity = (`job_id`, `source='closeout'`): delete the job's still-**unbilled** `source='closeout'` rows, insert one `status='used'` row per closeout line, 1:1 — two spring lines = two rows. `unit_price` from `Part.unit_price` when `part_id` resolves (catalog sell price — NOT the closeout `unit_cost`), else NULL. Billed rows are never touched. Free-text parts (not in inventory) land too — they're exactly the ones that leak today.
3. **Mobile parts-used feed** — `mobile.py:3055-3092`: one new `source='mobile'` row per event (events accumulate; never merged).
4. **Van-usage feed** — `van_inventory.py:147-160`: when the usage has a `job_id`, one `source='van'` row per log event.
5. **Request rows are never auto-superseded.** A tech's earlier parts-needed request and a closeout line for the same part will BOTH appear on the checklist — by design. The checklist UI groups by job and badges each row's source (`requested` / `used at closeout` / `mobile` / `van stock`) + shows qty and price; the office unticks duplicates. Rationale: the capture paths have no shared event identity, so any machine dedup either undercounts (destroys a legit line) or double-counts; a human-reviewed checklist with full provenance can only over-*show*, never over-bill silently — and the pull-into-invoice step (operator-selected, stamp-gated per PR 3) is where truth is decided. The one-click `create_invoice_from_job` pull-everything path pulls **`source='closeout'` rows only** (the attested set) to avoid mechanical request+used double-pull.
6. **Estimate-line overlap** — a part priced into an accepted-estimate line AND logged at closeout can't be reliably machine-matched (estimate lines are free-text); same answer: side-by-side operator review, now with a complete parts list instead of a partial one.
7. **Closeout inventory decrement** — closeout writes `JobPart` but never decrements `Part.qty_on_hand` (mobile does). Add the decrement for in-inventory parts, allow-negative non-blocking (Doug 2026-05-10). Fixes the closeout/mobile inconsistency.
8. **"Consumed but not billed" report** — new endpoint + BillingView Review section: jobs having unbilled `JobPartNeeded` rows in status `used`/`received` where the job is completed (with or without an invoice). Catches the invoice-already-sent-then-parts-logged case that Ready-to-Bill misses.

**Tests:** closeout 1:1 row insert incl. two same-sku lines staying distinct; re-closeout replaces unbilled closeout rows only (billed rows untouched); request row + closeout row coexist (no clobber — the v1 failure case: request qty 2 + closeout qty 1 → both visible); mobile/van events accumulate; price from catalog not cost; decrement parity closeout-vs-mobile incl. negative stock; one-click pulls closeout-source only; report includes invoiced-job-with-unbilled-parts; stamp + release round-trip.

---

## PR 5 — Completion workflow gate + the follow-up loop

*The enforcement layer. Soft-gate at completion, daily automated chase after it.*

1. **Optional hard gate** — new tenant flag `workflow_require_invoice_on_complete` (default OFF, like the other five): `/complete` + `/closeout` 422 `missing:["invoice"]` when no live invoice exists for the job. OFF by default because invoice-after-completion is the normal shop flow; the flag exists for operators who invoice up-front. (tenant_settings column addition — control-plane migration, same pattern as existing workflow_* columns.)
2. **Billing follow-up beat task** — new `gdx_dispatch/tasks/billing_followup.py` + beat entry (daily, 13:00 UTC ≈ 8-9am ET; `priority:low`), per-tenant dispatcher pattern like the others in `core/scheduler.py`. Computes, per tenant:
   - Ready-to-Bill jobs older than N days (default 3; setting)
   - Draft invoices older than N days (default 3)
   - Approved unbilled change orders (PR 3)
   - Unbilled used parts on completed jobs (PR 4)
   Writes one summary recommendation/notification (existing recommendations surface) + logs counts. Loud result dict `{ready: n, drafts: n, cos: n, parts: n, skipped_reason?}` — never a silent zero-work success. This is the "no job falls through" loop: every leak class from PR 1-4 gets re-surfaced daily until cleared.
3. **Parts gate upgrade + flag flip — RESOLVED (Doug 2026-07-07): require_parts ON, signature/hours stay OFF.** The gate as-is blocks a tech who genuinely used no parts. Upgrade before flipping: closeout payload + `JobCloseout` gain `no_parts_used` bool (deliberate checkbox in the dialog); free-text closeout parts gain an optional per-part `note` (goes into the `parts_used` snapshot + checklist row notes) so a part that isn't in the system gets explained. Gate logic: satisfied by a non-empty parts list OR `no_parts_used=true`; bare silence still 422s. `/complete`'s row-count fallback (`jobs.py:1272-1275`) gets the same attestation escape. Deploy step: `UPDATE tenant_settings SET workflow_require_parts_on_complete = true` for GDX after PR 4 ships.

**Tests:** gate 422 when flag on + no invoice, passes with invoice; follow-up task counts each category from fixtures; task result shape; beat entry registered.

---

## PR 6 — Money actually gets chased: dunning + recurring billing resurrection

1. **Reminder emails send for real** — `send_reminder` (`invoice_reminders.py:259`) and `collections.py:84`: when `channel == "email"`, render the tenant `ReminderSettings` template (`_render_template` + real invoice context: number, customer name, `balance_due`, days overdue, due date) and send via `core/transactional_email.py` (Outlook-Graph-first, SMTP fallback — existing plumbing). Persist outcome: `PaymentReminder.notes` gains `[delivered]` / `[skipped: no email config]` suffix + response returns `sent: bool, skip_reason`. **No email configured = visible failure in the UI, not a silent log row.** Non-email channels stay log-only (they're "I called them" records).
2. **Automated dunning beat task — opt-in, default OFF (Doug 2026-07-07).** Migration: `ReminderSettings.auto_send_enabled` BOOL default FALSE + `auto_send_nudge_dismissed` BOOL default FALSE; `invoices.dunning_paused` BOOL default FALSE. `invoice-reminders-daily` (13:15 UTC, after the follow-up task) keys ONLY off `auto_send_enabled` (the legacy `enabled` stays as the manual/preview feature switch): overdue invoices (`status='sent'`, `balance_due>0`, `due_date < today`, `dunning_paused = false`, not deleted) where `days_overdue >= threshold` for some threshold in `schedule_days`. While auto-send is OFF and not dismissed: weekly recommendation to admin/owner — "automated payment reminders are off; N overdue invoices ($X) unchased" — with a permanent don't-remind-again dismiss. Settings screen shows a live "N invoices currently qualify" preview before saving the toggle ON (replaces the dry-run flag with the same safety at the moment it matters). Per-invoice mute toggle on invoice detail; skipped-paused counted in the task result. **[AUDIT-R1] Idempotency records the actual threshold, not a stage-index mapping:** migration adds `payment_reminders.threshold_days` Integer NULL — automated sends set it; the skip check is "a reminder with `threshold_days == T` already exists for this invoice". Survives `schedule_days` edits mid-dunning ([7,14,30]→[5,10] neither re-fires nor skips wrongly: already-sent thresholds keep their rows; new thresholds fire when crossed) and manual "I called them" rows (`threshold_days` NULL) live in a separate keyspace — a manual log never silently suppresses an automated email (interplay is an open decision below; default: no suppression). Stage label still derived from threshold *position at send time* for the template, but it is display-only, never the key. Send via the same path as (1), write PaymentReminder rows. Per-invoice try/except; result dict with sent/skipped/error counts. Customer email resolved from the invoice's customer; missing email → counted + surfaced, not dropped.
3. **Recurring billing resurrection** — `tasks/recurring_billing.py` is dead (module not in `celery_app.py` includes, task not in beat) and its raw INSERT omits `subtotal`, `balance_due`, `invoice_date`, `due_date`, `sequence_number`, `billing_type`, lines. **[AUDIT-R1] `create_invoice` is a FastAPI handler (auth dep + `_["tenant_id"]`) and cannot be called from Celery** — extract the creation core into a plain function (`modules/billing/invoice_factory.py`: `create_invoice_core(db, *, tenant_id, customer_id, lines, job_id=None, estimate_id=None, ...)`) used by BOTH the router and the task, so recurring invoices get the billing-terms due-date resolver, sequence number, tax path, and balance fields for free. One line item from the agreement description. THEN add module to celery includes + beat (daily 12:45 UTC). New invoices stay `draft` but PR 1's draft surfacing + PR 5's follow-up loop chase them. ⚠ Deploy check first: `SELECT count(*) FROM service_agreements WHERE active AND next_billing_date <= now()` on prod — a backlog would bulk-create invoices on first run; decide whether to backfill `next_billing_date` forward before enabling.
4. **`/refund` enum fix** (GL doc §12, adjacent one-liner): `invoices.py:1524` writes `status="refunded"` which isn't in the enum → decide `paid` + refund note vs adding enum value; recommend: keep status `paid`, record refund in notes/audit (enum change is a migration; GL Phase 1 will own refund semantics properly).

**Tests:** template render with real invoice context; idempotency (task runs twice → one reminder per threshold; `schedule_days` edited between runs → no re-fire, no wrong skip); manual reminder does NOT suppress automated send; no-email-config skip surfacing; recurring: agreement → complete well-formed invoice via `create_invoice_core` (all NOT-NULL + balance fields, due-date from terms resolver), next_billing_date advance, backlog cap behavior; router + task parity (both paths produce identical invoices).

---

## Decisions — resolved with Doug 2026-07-07

1. **CO tax (PR 3): "handled like an invoice, tax shown so the customer sees it."** The CO detail/approval/signature view computes and displays subtotal + tax + total using the same rate resolver invoices use; what the customer signs is exactly what the invoice will say. `change_order_lines.taxable` default TRUE, forwarded on copy; CO serialization gains subtotal/tax_amount/total fields.
2. **Completion gates (PR 5): require_parts ON for GDX** once PR 4 lands — with an escape hatch: the gate is satisfied by (a) a parts list — catalog picks OR free-text entries, free-text gaining a per-part note field so the tech can explain a part that isn't in the system — or (b) an explicit "No parts used" attestation (new `no_parts_used` bool on the closeout payload + JobCloseout, rendered as a deliberate checkbox). A tech is never stuck; silence is the only thing blocked. Signature/hours flags stay OFF.
3. **Recurring billing (PR 6): enable directly** — Doug expects zero active service agreements on prod. Deploy step: run the backlog count query to confirm (verify-state discipline); if nonzero, stop and show the list. No backfill machinery built.
4. **Reminders (PR 6): robot keeps going** — manual logs never suppress the schedule. Plus a per-invoice mute for real arrangements: `invoices.dunning_paused` BOOL default FALSE (migration) + toggle on invoice detail; the beat task skips paused invoices and counts them in its result.
5. **Dunning go-live (PR 6): opt-in setting, default OFF, with a weekly nag.** New `ReminderSettings.auto_send_enabled` BOOL default FALSE (the existing `enabled` field stays as the feature switch for manual/preview; the beat task keys ONLY off `auto_send_enabled`). While OFF: a once-a-week recommendation to admin/owner — "automated payment reminders are off; N overdue invoices ($X) are not being chased" — with a permanent "don't remind me again" dismiss (`ReminderSettings.auto_send_nudge_dismissed`, so operators who don't want dunning are never nagged twice). When an operator flips it ON, the settings screen shows a live preview — "N invoices currently qualify (list)" — before saving, replacing the earlier dry-run-flag idea with the same safety at the moment it matters.

---

## Sequencing, branches, deploy

- **Order:** PR 1 → 2 → 3 → 4 → 5 → 6. Each independently shippable; 5 depends on 3+4 for two of its four categories (ship 5 after, or with those categories feature-guarded); 6.2 depends on 6.1.
- **Branching:** each PR from `main` in a worktree (release-workflow memory: clean-PR pattern; nav branch untouched; Doug merges + releases himself).
- **Migrations:** PR 3 (change_orders col), PR 4 (job_parts_needed col), PR 5 (tenant_settings col — control plane). All additive nullable columns. Verify plane ownership per #41 pattern; plugin tables NOT involved (no plugin-drift manual-ALTER risk).
- **Prod deploy watch-outs:** PR 5 = `require_parts_on_complete` ON for GDX (after PR 4); PR 6 recurring-billing backlog count BEFORE enabling beat (expected 0 — if nonzero, stop and show Doug the list); dunning ships default-OFF so deploy is safe — going live is Doug flipping `auto_send_enabled` in settings after reviewing the qualify-preview.
- **Verification:** pytest via docker-app harness per PR; browser verification via headed Playwright MCP (BillingView review tab, InvoiceCreateView CO/parts checklists, CollectionsView aging now non-zero) in light + dark; Manifest per commit per /manifest discipline.

## Appendix A — rejected alternative for billing_status

Make the cache real: update `billing_status` inside `_recalculate_invoice` + invoice create/void/delete + payment void + nightly overdue sweep; backfill migration derives current values for all existing jobs; drift-check job compares cache vs derivation weekly. Rejected because: duplicate source of truth vs the locked display-state model, multi-invoice-job semantics are ambiguous in a single enum, the overdue value needs a scheduled sweep anyway, and every consumer that matters is already a query that can JOIN. Revisit only if a hot path can't afford the JOIN (none identified; jobs list already does the invoice join for display state).

---

## Follow-ups filed by implementation-time audits (PR 1 & PR 2 rounds)

- **Portal `pay_invoice` void/paid door:** creates a Stripe checkout for any non-deleted invoice with no status check and records no Payment row (GL Phase-1 Stripe scope; dormant).
- **No void/un-void lifecycle:** nothing in the app can set `status="void"` today; the PR 1 guards are defense-in-depth for legacy/QB rows. Candidate small follow-up.
- **No "won't-bill" terminal state:** PR 2's void-exclusion means a job whose only invoice was voided re-enters Ready-for-Billing permanently — correct for real leaks, but a deliberate write-off is un-dismissable until display-model Slice 2 (Written Off) ships storage.
- **$0-draft stacking:** `create_invoice_from_job` has no existing-invoice guard; repeated raw-API calls stack $0 drafts (no UI caller exists — both Create Invoice buttons route to /billing/new). Guard when PR 4/5 touch that path.
- **Pre-merge prod check (PR 2):** `SELECT billing_status, count(*) FROM jobs WHERE deleted_at IS NULL GROUP BY 1` — repo history is squashed; if legacy rows hold non-`unbilled` values, the two "tautology deletion" claims become intended-fix claims and the forecast delta should be eyeballed after deploy (deposit invoices could double-count against open-AR projection until the deposit-subtraction follow-up).
