# Vendor Invoice Intake — Design (DRAFT v4, awaiting Doug approval)

**Date:** 2026-07-07 · **Status:** DRAFT v4 — two audit rounds folded in, no code written
**Trigger:** Doug: vendor invoices (e.g. Midwest Wholesale Doors retail-sale PDFs) arrive by email; they need to be imported, pointed at the correct job, **and** be able to receive parts into inventory — not only attach to jobs. OCR requested for scanned documents.
**Basis:** codebase survey 2026-07-07 + a real sample Midwest retail-sale invoice (kept OUT of the repo per Doug): a PO# carrying a customer/job name, door lines under a generic "Garage Door Material" label with the real product in the Notes row, a shipping line, a Net-30 term. All example values in this doc are schematic — no real bill data is committed.

**Audit round 1 [AUDIT-R1]:** adversarial reviewer verified claims against code. Corrections folded in: (a) stock receipts target **`InventoryItem`**, not `Part` — the office InventoryView and the existing `receive_po` flow both run on `InventoryItem` (newer routers register first per the `app.py` comment; receiving into `Part` would update a number the office never sees and duplicate the existing receive path); (b) the JobPartNeeded feed uses the billing-capture plan's **post-audit per-event-row pattern** (`source` column, no fuzzy upsert); (c) match the recorded **order chain first**, PO#-text fuzzy second; (d) PO-received and JobReceipt overlaps get explicit skip/warn defaults; (e) vendor-invoice Expenses never flow to QB (banking sync already mirrors the paid bill); (f) vendor identity normalized before the uniqueness guard; (g) arithmetic invariant honestly scoped (catches structural errors, not misreads — fidelity comes from the PDF-beside-lines review UI).

**v4 delta (Doug 2026-07-07):** (a) Phase 2 expanded from "new mail only" to a **mailbox sweep** — repeatable historical backfill through the connected Outlook mailbox plus ongoing delta polling; (b) duplicate detection promoted to its own section (§3a) with a fourth layer: same-vendor/same-amount **double-billing warning**.

**Audit round 3 [AUDIT-R3] (2026-07-08, sweep/dedup delta):** dedup (§3a) verified **sound** — `documents.content_hash` dedup is genuinely tenant-wide across all Document types, the (vendor, invoice_number) layer and the layer-3-non-blocking + layer-4-statement posture are right; **Phase 1 is unaffected and cleared to build.** But the Phase 2 **sweep subsection was found unsound and must NOT be coded as written** — corrections, now binding on Phase 2:

- **Graph capability errors:** `OutlookGraphClient.list_messages(delta_token=…)` does NOT support a `$filter`/date range (only `$top/$skip/$orderby/$select`), and with `delta_token=None` it returns no `@odata.deltaLink` — it **cannot** bootstrap delta (the code moved to `list_messages_delta` for exactly this). A historical backfill is a **separate** date-filtered page-through (`$filter=receivedDateTime ge …`, the pattern already in `modules/outlook/tasks.py`), which today only upserts `OutlookMessage` metadata and is **not attachment-aware**. "Two modes, one code path" is false: the attachment→pipeline bridge and the new-message→ingest trigger are **entirely new code**, not reuse.
- **No throttle handling:** the Graph client has zero 429/Retry-After/backoff. An uncapped first 12-month sweep firing one `download_attachment` per PDF **will** get throttled and abort. Phase 2 must add Retry-After handling + an attachment-download cap + a per-message "attachments ingested" checkpoint (today's re-run is record-idempotent but re-downloads every attachment — **safe ≠ cheap**; the "idempotent, safe to re-run" claim is only true for stored records, not cost).
- **Broken foundation precondition:** in prod today `outlook_subscriptions` is empty and delta never bootstraps (TENANT_BASE_DOMAIN unset); the fixes are unmerged (PRs #115–#121). Phase 2 has a **hard precondition**: Outlook subscriptions non-empty + delta bootstrapped on prod first.
- **Layer-3 is a weak hint, not a control:** in a garage shop identical-total reorders within 45 days are normal, and partial-shipment/tax-corrected re-bills have *different* totals (missed). The $0.02-exact-total discriminator carries no signal to separate a true double-bill from a legit reorder. Keep the flag, label it advisory; **layer 4 (statement reconciliation) is the real double-bill catch.**
- **LLM backfill cost:** the "few docs/week" cost note is steady-state; a 12-month backfill pushes hundreds of PDFs to Anthropic in one run — Phase 2 needs a per-run cost/enqueue ceiling. (Allowlist gating already keeps non-vendor senders' PDFs out of the LLM — that part is sound.)

**Audit round 2 [AUDIT-R2]:** second adversary confirmed round 1's corrections are real and load-bearing (checklist filter `status=ordered,received&unbilled=true` DOES surface the new rows; InventoryItem/router-order/receive_po claims faithful). New corrections folded in: (a) **the invoice checklist UI is in scope** — today's `LineItemEditor` pre-checks every `received` row and maps it to a $0 line, so untouched, vendor rows would land on customer invoices as $0/duplicate lines by default; vendor-source rows must arrive unchecked + badged; (b) **tax/shipping become routable synthetic lines** — v2 captured them on the header and allocated them nowhere (dollars leaked between payables and costing); (c) the double-receive guard made **bidirectional** (`receive_po` warns about already-confirmed invoice stock lines); (d) skip-defaults are excluded from bulk confirm (a false PO match must not silently undercount inventory in one click); (e) the duplicate-document catch defines the Document-without-VendorInvoice case; (f) QB exclusion becomes a mechanism: **`Expense.source` column** (the push path has zero callers today — the column is for the future author's anti-join, one line of schema instead of a sentence of intent); (g) three undeclared schema items declared (vendor aliases, `supplies` not `shop_supplies`, Outlook allowlist config home); (h) credit memos/returns get first-class negative-line support.

## Goal

Every vendor invoice that arrives lands as a structured record, its lines are routed (job / stock / shop overhead) with human confirmation, and the effects flow to surfaces that already exist: job costing, the invoice parts checklist (billing spine), the office inventory page, and payables visibility.

## Non-goals

- **Full A/P accrual accounting** — GL Phase 1 owns bill/payment ledger semantics (blocked on CPA answers). `VendorInvoice` is source data the GL consumes later; its open/paid status is visibility only.
- **Inventory valuation policy** (moving average vs last cost vs FIFO) — the open CPA question. We record cost per receipt event; no silent catalog-cost rewrites.
- **Unifying `Part` vs `InventoryItem`** — pre-existing split (techs' mobile flow consumes `Part`; the office receives and views `InventoryItem`). This plan receives into the office-visible system and must not widen the split; convergence is its own future cleanup.
- **Tech road-purchase receipts** — `JobReceipt` (photo + QBO expense recon) remains that path. This pipeline is for vendor A/P bills. Overlap guard in §4.

## Design principles (inherited from billing-capture plan)

- Suggest, don't silently auto-attach — every match is human-confirmed in a queue.
- Fail loudly — unparseable documents become visible queue entries, never dropped.
- Per-event rows with provenance; nothing machine-merged that a human should adjudicate.
- Every guard is a mechanism (constraint, gate, default, column) — not a sentence in a doc.

---

## 1. Intake

**Phase 1 — manual upload.** "Vendor Bills" page with a drop target, following the vendor_statements pipeline shape: bytes → sha256 vs `documents.content_hash` → save to `UPLOAD_DIR` → `Document` row → extraction. [AUDIT-R1] The statements service *raises* `DuplicateDocumentError` on a hash hit; our wrapper catches it. [AUDIT-R2] Two hit cases, both defined: existing Document **with** a VendorInvoice → return that record; existing Document **without** one (same PDF was earlier attached to a job or statement) → create the VendorInvoice referencing the existing Document (no byte re-store) and proceed to extraction.

**Phase 2 — Outlook mailbox sweep (ongoing + backfill).** `modules/outlook/` already has everything the sweep needs: `list_messages(folder, top, skip, delta_token)` (pages any folder, delta for new-mail), `list_attachments`, `download_attachment` (bytes), beat-scheduled polling. Two modes, one code path:

- **Ongoing:** delta polling picks up new messages; those passing the candidate filter get PDF attachments fed to the pipeline.
- **Backfill sweep (v4):** a repeatable task pages the mailbox backward N months (default 12, configurable), same candidate filter, same pipeline. The §3a dedup layers make it **idempotent — safe to re-run anytime**, so "did we miss any?" is answered by running the sweep, not by wondering. Result is loud: `{scanned, candidates, imported, duplicates, parse_failed→manual, skipped+reasons}` surfaced in the UI, not just a log line.

**Candidate filter (privacy/cost boundary):** allowlisted senders (config in the `OutlookSettings` JSON forward-compat columns [AUDIT-R2] + sweep lookback months) → full ladder including LLM extraction. Non-allowlisted senders with PDF attachments → rung-1 deterministic parsers only; if a known-vendor parser claims the document (it's recognizably a Midwest invoice someone forwarded), it imports and the queue suggests adding the sender to the allowlist — but arbitrary strangers' PDFs are never sent to the LLM. Do NOT build on `tasks/email_poller.py` (IMAP): not beat-scheduled, raw-SQL schema drifted from the ORM.

## 2. Extraction ladder (the OCR answer)

Three rungs; each failure falls to the next, visibly.

1. **Deterministic parser (text PDFs).** `parse_midwest_invoice()` beside the existing `parse_midwest_statement()` (pypdf, layout mode — same API, pin `>=6.10.2,<7.0` already satisfies). Extracts: invoice no, date, PO#, terms, lines (qty, description, unit price, total), tax, shipping, total. The meaningful description is the `Notes:` row under each generic line — capture and merge it. Per-vendor parsers are fine; rung 2 covers the rest.
2. **LLM vision extraction (scans + unknown layouts).** The Anthropic SDK + per-tenant key storage already exist (`core/llm/`); **sending documents is new code** (SDK-supported PDF/image content blocks), not free plumbing — but it beats a tesseract stack: no new system deps (~100MB, and tesseract still can't do table layout), handles scans and never-seen layouts, volume is a few docs/week (cents). Needs egress (fixed 2026-06-29) + configured key; if absent → rung 3 with reason "AI extraction unavailable".
3. **Manual entry queue.** Document stored + listed "needs entry"; office keys header + lines. Also the correction surface for rung 1/2 failures.

**Negative amounts are first-class [AUDIT-R2]:** credit memos and returns (wrong springs, damaged panels — routine in this trade) parse to negative lines/totals and flow through the same dispositions (negative Expense, negative stock adjustment). The invariant math is sign-agnostic.

**Validation, honestly scoped [AUDIT-R1]:** every extraction must pass the arithmetic invariant — sum(lines) + tax + shipping == total (±$0.02), qty × unit price == line total — else manual queue. This catches *structural* errors (missed line, merged rows), **not** misreads: a consistent hallucination passes. Fidelity guards: (a) the review UI renders the PDF page beside the extracted lines — human eyes are on the money before anything commits; (b) golden-fixture tests per supported layout (initially one: Midwest — the fixture set grows with each vendor added); (c) rung-2 per-field confidence is advisory display ordering only, never a gate. LLM output is data, never instructions (prompt-injection posture: extraction only, no tool use).

**Rejected:** ocrmypdf/tesseract sidecar (system deps + still needs layout logic per vendor). Revisit only if Doug wants zero cloud for vendor docs (they're low-sensitivity B2B pricing data).

## 3. Data model

In `modules/vendor_statements/` (the de-facto A/P intake module):

- **`VendorInvoice`** (`vendor_invoices`): id, vendor_id (FK `vendors`, nullable), vendor_name_raw, invoice_number, invoice_date, po_reference, terms, subtotal, tax, shipping, total, due_date (from terms), status (`open`/`paid`/`void`), matched_job_id (nullable, header-level), document_id (FK documents), source (`upload`/`email`), extraction_method (`parser`/`llm`/`manual`), created_at, reviewed_at, reviewed_by_user_id.
- **Vendor identity [AUDIT-R1]:** before the uniqueness guard, resolve `vendor_id` against the `vendors` table — normalized name match (casefold, strip punctuation/whitespace) plus alias matching. [AUDIT-R2] Aliases are new schema, declared here: **`vendors.name_aliases`** Text NULL (JSON array), migration included; covers LLM variants like "Midwest Whsle Doors". Uniqueness = (vendor_id, invoice_number) when resolved, else (normalized vendor_name_raw, invoice_number). Catches re-prints/re-scans whose bytes differ.
- **`VendorInvoiceLine`** (`vendor_invoice_lines`): id, vendor_invoice_id FK, line_no (nullable for synthetic lines), **kind** (`item`/`freight`/`tax`), description, qty, unit_cost, line_total (negative allowed), **disposition** (`job`/`stock`/`overhead`/`skip`), job_id (nullable), inventory_item_id (FK `inventory_items`, nullable), expense_id (nullable), job_part_needed_id (nullable), skip_reason (required when `skip`), status (`pending`/`confirmed`), confirmed_by_user_id, confirmed_at.
- **Tax + shipping as synthetic lines [AUDIT-R2]:** extraction materializes header tax and shipping as `kind='tax'`/`kind='freight'` lines in the queue, routable like any line. Default disposition: the header job when every item line went to one job, else `overhead`. Rejected alternative: silent pro-rata allocation across lines — opaque math a one-person office can't audit; a visible line they route in one click is truer to the principles. (Landed-cost valuation is the CPA's call later; the data — which invoice, which lines, what freight — is all preserved for it.)
- **`Expense.source`** [AUDIT-R2]: new column String(20) NOT NULL DEFAULT `'manual'` (`manual`/`vendor_invoice`), migration included. This is the QB-exclusion mechanism: `push_expense` has zero callers today, but whoever wires it later anti-joins `source='vendor_invoice'` instead of needing tribal knowledge of a reverse FK.
- **Statement linkage:** `VendorStatementLine.vendor_invoice_no` already exists → monthly statement view flags "billed but never imported" and vice-versa. No schema change.

## 3a. Duplicate detection (four layers, v4)

Duplicates arrive four different ways; each gets its own mechanism:

1. **Same bytes** (email re-delivered, PDF forwarded twice, sweep re-run): `documents.content_hash` sha256 hit → returns the existing record. Blocks silently — this is never interesting.
2. **Same invoice, different bytes** (re-print, re-scan, "resending in case you missed it" with a fresh PDF): uniqueness on (vendor_id — or normalized `vendor_name_raw` — , invoice_number) → returns the existing record, noted in the sweep summary.
3. **Possible double-billing** (vendor actually bills twice — different invoice numbers, same goods): same vendor + total within $0.02 + invoice dates within 45 days + different invoice numbers → **non-blocking warning flag** on both records in the queue ("possible duplicate of #NNN"). Never auto-skipped: legitimate repeat orders exist (two identical stock orders a month apart), so a human decides — but the flag means a true double-bill can't slide through unremarked.
4. **Statement reconciliation (Phase 3):** the monthly statement is the vendor's own ledger — it catches both invoices we never imported AND invoices appearing twice on their books.

## 4. Match queue (review UI)

One "Vendor Bills" surface: PDF rendered beside extracted lines. Per invoice:

- **Order-chain first [AUDIT-R1]:** the invoice is usually the *end* of a purchase the system partly recorded. Suggestion order: (1) `po_reference` exact match against the in-app PO tables (`purchase_orders` — the office one with the receive flow — then `inventory_purchase_orders`; `po_requests` surfaced read-only); (2) open `JobPartNeeded` rows with supplier ≈ vendor and status `needed`/`ordered` (their jobs become header suggestions); (3) PO#-text fuzzy match against customer names → that customer's open jobs (material-expecting stages, recent first). Rung 3 stays because phone/portal orders bypass the system entirely — the sample invoice proves it (the PO# is a human note, not a key).
- **Per-line disposition**, defaulted but editable: header-matched job → default `job`; line matches an `InventoryItem` (by sku, manufacturer_part_number, then name fuzzy) → offer `stock`; `overhead` for shop supplies; `skip` requires a reason.
- **Double-receive guard, both directions [AUDIT-R2]:** (a) invoice-after-PO: if the invoice matched an in-app PO whose status is `received`, stock-matching lines default to `skip` (reason auto-filled "received via PO NNN"); (b) PO-after-invoice: `receive_po` gains a check — items on the PO that already have confirmed vendor-invoice stock lines (matched via `po_reference`) produce a warning naming the invoice before incrementing. Neither side silently double-counts.
- **Skip-defaults never bulk-confirm [AUDIT-R2]:** "confirm all as suggested" applies only to `job`/`stock`/`overhead` defaults. Any line auto-defaulted to `skip` by a guard requires individual confirmation — a false PO match must not convert into silent inventory undercount in one click.
- **JobReceipt overlap guard [AUDIT-R1]:** if a `JobReceipt` exists with ≈vendor + ≈amount + ≈date, the queue shows a warning banner ("tech already logged a receipt for this purchase") and job lines default to `skip` (individually confirmed, per the bulk rule above).
- **JobPart overlap warning:** confirming a `job` line whose description fuzzy-matches an existing `JobPart` on that job (tech already logged it as stock consumption) requires explicit resolution — confirm-anyway (with note) or skip. Plus the costing-side report (§5.4).
- Mixed invoices are normal (doors → job, rollers box → stock, freight line → routed). Nothing has downstream effect until confirmed.

## 5. Effects on confirm

### 5.1 `job` lines

- `Document.job_id` set (visible on the job).
- **Cost:** `Expense` row (job_id, vendor, amount, date, category `materials`, description, `source='vendor_invoice'`; provenance both ways via `VendorInvoiceLine.expense_id`) + extend `routers/job_costing.py` to include job-linked Expenses in the materials bucket — today costing sums only `JobPart.unit_cost_at_time`, so per-job special orders (this entire sample invoice) are invisible to profitability. Job lines deliberately do NOT create `JobPart` (that stays the stock-consumption path); the §4 overlap warning is the human gate. ⚠ Implementation check: how the overhead % in `job_costing.py` applies — adding Expenses to the base changes overhead math; decide + test, don't discover it.
- **Billing spine [AUDIT-R1/R2] (post-audit PR 4 pattern):** insert **one per-event `JobPartNeeded` row per confirmed line** — `source='vendor_invoice'`, status `received`, supplier = vendor, `unit_price` NULL (office prices it). Never merged, never upserted. Verified: the invoice checklist fetch (`status=ordered,received&unbilled=true`) WILL surface these rows. **[AUDIT-R2] Checklist UI change is in scope, not inherited:** today's `LineItemEditor` pre-checks every `received` row and maps it to a $0 line — untouched, vendor rows would flow onto customer invoices as $0 or duplicate lines by default. Phase 1 therefore includes: vendor-source rows arrive **unchecked**, badged with source + vendor + invoice number, price field prompting entry (special-order doors are usually already an estimate line — the badge is what lets the office untick with confidence). Coordinate with billing-plan PR 4's checklist work (same component, same badge pattern). Sequencing: PR 4's migration is **two columns** (`source` + `unit_price`); whichever plan ships first carries both, in PR 4's exact shape.
- **QB direction:** these Expenses never flow to QB (mechanism: `Expense.source`, §3) — the paid bill already reaches QB via banking sync as a `Purchase`. GL Phase 1 owns final cost-truth; until then costing reads Expense+JobPart, QB reads its own mirror, and §5.5's paid-matcher is the only bridge.

### 5.2 `stock` lines

- **Target: `InventoryItem.quantity` increment + `StockAdjustment(reason="vendor_invoice", notes="Invoice {number} line {n}")`** — the exact `receive_po` pattern, on the system the office actually sees. [AUDIT-R1: v1 targeted `Part`, which the office UI never renders.] Negative (credit-memo) lines decrement with `reason="vendor_credit"`.
- Receipt cost lives on the `VendorInvoiceLine` (qty, unit_cost, kind, confirmed_at) — the valuation source data for the GL/CPA work; no separate receipts table. Catalog-cost drift shown in the queue ("catalog $X, this receipt $Y"); updating `InventoryItem.unit_cost` is an explicit per-line checkbox (default ON only when catalog cost is 0), never silent.
- Unmatched line + `stock` → inline "create inventory item" (sku, name, supplier prefilled).
- Confirming a stock line whose vendor+part matches an open parts-to-order/`JobPartNeeded` `ordered` row offers (not forces) flipping it to `received` — same-permission office action, surfaced not silent.

### 5.3 `overhead` lines

- `Expense` without job_id, `source='vendor_invoice'`. [AUDIT-R2] Category `supplies` (the existing ExpensesView vocabulary: materials/travel/meals/equipment/supplies/other — `shop_supplies` would render unmapped), freight/tax synthetic lines default category `materials` when job-routed, `supplies` when overhead.

### 5.4 Costing visibility

- Extend the job-costing endpoint's materials bucket: `JobPart` rows + job-linked `Expense` rows, labeled by source. New "possible double-count" report: jobs where an Expense and a JobPart fuzzy-match on description — feeds the same review discipline as the billing checklist.

### 5.5 Payables

- Open `VendorInvoice` list with due dates (Net 30 sample → due 07/30) + totals. OverheadObligation projection can consume it later.
- **Paid detection (assist, not automation):** QB banking sync already pulls `Purchase`/`BillPayment`; a matcher suggests "this payment to Midwest matches open invoice #NNNN for the same amount" — office confirms → status `paid`. Never auto-marked.

## 6. Inventory-system facts (settled by code, decision noted)

- `routers/inventory.py` (backed by `InventoryItem`) registers before `modules/inventory/router.py` (backed by `Part`) — FastAPI first-match means the office InventoryView reads/writes **`InventoryItem`** (`app.py` comment documents newer-first deliberately).
- The office PO receive flow (`routers/purchase_orders.py` `receive_po`) already increments `InventoryItem` + logs `StockAdjustment`.
- `Part` remains what mobile parts-used decrements and job costing snapshots — the pre-existing two-ledger split. This plan receives where the office looks and leaves `Part` untouched; **Part↔InventoryItem convergence is its own future cleanup** (twin of the billing_status pattern). Open question 3 asks Doug which data set is real to pick the convergence direction — it no longer blocks this plan.

## 7. Phasing

1. **Phase 1:** migrations (`vendor_invoices`, `vendor_invoice_lines`, `vendors.name_aliases`, `Expense.source`, JobPartNeeded `source`+`unit_price` if PR 4 hasn't shipped) + Midwest invoice parser + manual upload + match queue + confirm effects (5.1–5.4, incl. the checklist UI changes + bidirectional receive guard) + payables list.
2. **Phase 2:** LLM extraction rung + Outlook mailbox sweep — ongoing delta polling AND the repeatable backfill task (allowlist + lookback config in `OutlookSettings` JSON columns). Audit the v4 sweep delta before coding this phase.
3. **Phase 3:** statement ↔ invoice reconciliation view + QB paid-detection assist (5.5 matcher).

## 8. Testing / verification

- Parser golden files: synthetic layout text (fabricated data — the real PDF stays out of the repo) → exact header + lines incl. Notes-merged descriptions + freight/tax synthetic lines; invariant-failure fixture → manual queue with visible reason; credit-memo fixture with negative lines.
- Match ordering: in-app PO beats PO-text fuzzy; a PO# name → the matching customer's jobs ranked; ambiguous → no suggestion above threshold.
- Guards as tests [AUDIT-R1/R2]: PO-received invoice defaults stock lines to skip; `receive_po` warns on already-confirmed invoice lines (reverse direction); skip-defaults excluded from bulk confirm; JobReceipt-overlap warns; JobPart-overlap requires resolution; duplicate upload returns existing record; duplicate hash on a non-invoice Document still creates the VendorInvoice; (vendor, invoice_number) re-scan caught with differing bytes; alias-normalized vendor resolution.
- Duplicates + sweep [v4]: sweep run twice → zero new records, summary reports the duplicates; same-vendor/same-total/different-number pair within 45 days → both flagged "possible duplicate", neither auto-skipped; identical repeat order >45 days apart → NOT flagged; non-allowlisted sender's Midwest PDF imports via rung 1, stranger's PDF never reaches the LLM.
- Confirm effects: job line → Expense (`source='vendor_invoice'`) + one per-event JobPartNeeded row (two same-sku lines stay two rows) + no JobPart; stock line → InventoryItem delta + StockAdjustment + optional cost update; negative line decrements; freight/tax lines route to job Expense vs overhead; double-confirm idempotent; skip requires reason.
- **Invoice-create surface [AUDIT-R2]:** vendor-source checklist rows arrive unchecked + badged; pricing a row produces a non-$0 line; unticking works; a job with an estimate line covering the same door shows both (human dedupe point). This is where the billing outcome lives — tested in the browser, not just the queue.
- Costing: materials bucket = JobPart + Expense labeled by source; overhead-% base decision has an explicit test; double-count report fixture.
- QB: expense-push anti-join on `Expense.source` (tested even though the path has no callers — the column contract is the deliverable).
- Browser verification (headed Playwright MCP, light+dark): the queue UI, **the office Inventory page showing the incremented quantity**, and the invoice-create checklist with vendor rows.

## Open questions for Doug

1. Sender allowlist for Phase 2 — which vendor domains actually send these emails?
2. Should confirming a stock receipt auto-offer flipping matching parts-to-order rows to `received`? (Designed as offer-not-force in 5.2 — confirm that's the right default.)
3. Inventory convergence direction (doesn't block this plan): is the office InventoryView (`InventoryItem`) the data you actually maintain, with `Part` as the techs'/mobile catalog? Determines the future cleanup.
