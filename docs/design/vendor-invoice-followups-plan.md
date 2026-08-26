# Vendor Invoice Intake — Follow-ups Execution Plan

**Date:** 2026-07-10 · **Status:** BUILT — all four workstreams on main (verified 2026-08-21). A: `models.py:78/103` `vendor_key` + the `uq_vendor_invoice_key` partial unique index. B: `modules/inventory/stock.py apply_stock_delta`, wired at **all three** sites (`confirm.py:35`, `inventory.py:271`, `purchase_orders.py:399`) — the recommended scope, so both pre-existing races are fixed too. C: `VendorBillsView` + `VendorBillDetailView`, and C2's provenance pill at `LineItemEditor.vue:84` with vendor rows excluded from the pre-check at `:955`. D: `modules/outlook/vendor_bill_ingest.py` — allowlist-gated, `graph_client.py:31` 429/503/504 + Retry-After backoff, the `vendor_bills_ingested_at` checkpoint, and both the `max_downloads` and LLM caps.
**Context:** Phase 1 backend shipped on branch `feat/vendor-invoice-intake` (commit aa08dec, local/unpushed). This plan covers the four known follow-ups from the build + code audits. Feature design: [vendor-invoice-intake-plan.md](vendor-invoice-intake-plan.md).

## The four workstreams

| # | Workstream | Size | Risk | Depends on | User-visible |
|---|---|---|---|---|---|
| A | DB dedup hardening (unique index + race-safe insert) | S | Low | — | No |
| B | Stock concurrency fix (atomic quantity delta) | S | Low | — | No |
| C | Frontend review-queue UI | L | Med | backend (done) | **Yes — makes the feature usable** |
| D | Phase 2 Outlook auto-intake (redesigned) | L | High | prod Outlook foundation live | Yes |

## Recommended sequence

1. **Fold A + B into the Phase-1 branch before it opens as a PR** (it's still unpushed) — Phase 1 should land race-safe, not land-then-patch. Both are small, isolated, and testable.
2. **C next, as its own branch** — the office can't reach any of the backend until this exists. Highest value.
3. **D last** — blocked on the prod Outlook delta/subscription foundation actually working, and it needs a fresh design pass (AUDIT-R3). Don't start until the precondition is verified live.

---

## A — DB dedup hardening

**Problem (audit):** upload dedup is app-level only. Two concurrent uploads of the same *new* bill both pass the layer-2 `find_duplicate_invoice` check (each sees no existing row) and both insert → duplicate `VendorInvoice`.

**Why not a unique index on `documents.content_hash`:** rejected. `content_hash` is `index=True` non-unique and shared across ALL document types; the statement module deliberately allows soft-delete-then-reupload (`test_upload_after_soft_delete_allowed`). A global unique there would break existing behavior. The DB guard belongs on `vendor_invoices`.

**Design:**
1. **New column `vendor_invoices.vendor_key`** (String(200), NOT NULL) — the normalized vendor identity, set in the service at insert: `str(vendor_id)` when resolved, else `normalize_name(vendor_name_raw)`. One stable key that covers both resolved and unresolved vendors (a partial index on nullable `vendor_id` alone wouldn't protect unresolved-vendor dupes).
2. **Partial unique index** `uq_vendor_invoice_key` on `(vendor_key, invoice_number) WHERE deleted_at IS NULL` — via SQLAlchemy `Index(..., unique=True, postgresql_where=..., sqlite_where=...)` in `__table_args__` so both Postgres (prod) and SQLite (tests) build it. The `WHERE deleted_at IS NULL` lets a voided/deleted bill be re-imported (mirrors the statement soft-delete allowance).
3. **Optional second index** `uq_vendor_invoice_document` on `document_id WHERE document_id IS NOT NULL AND deleted_at IS NULL` — one invoice per document.
4. **Race-safe insert in `service.py`:** wrap the `VendorInvoice` flush in `try/except IntegrityError`; on violation, `db.rollback()` (to a savepoint), re-run `find_duplicate_invoice`, and return the now-existing row as the dedup result (`created=False, duplicate_reason="vendor_invoice_number"`). The DB is the source of truth; the app-level check becomes a fast-path, the constraint the backstop.

**Migration:** vendor_invoices is ORM-created (no baseline row). Since Phase 1 is **not yet deployed**, adding the index to the model is enough — `create_orm_tables()` builds the table with the index on first boot; **no migration and no backfill needed** (no rows exist). ⚠ If A ships *after* Phase 1 is already on prod, it needs a guarded `CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS` migration + a `vendor_key` backfill first — which is the reason to fold A into the Phase-1 branch now.

**Tests:** insert two invoices same `(vendor_key, invoice_number)`, second flush raises IntegrityError → service returns the first (not a duplicate row); soft-deleted invoice can be re-imported; unresolved-vendor (vendor_id NULL) dupes are still caught via `vendor_key = normalized name`.

**Files:** `modules/vendor_invoices/models.py` (column + `__table_args__`), `service.py` (set `vendor_key`, IntegrityError fallback), `tests/test_vendor_invoice_dedup.py` (or a new `_upload` test with two inserts).

---

## B — Stock concurrency fix (atomic quantity delta)

**Problem (audit):** `confirm.py:167` `item.quantity = (item.quantity or 0) + delta` is an unlocked read-modify-write. Two *different* lines routed to the *same* `InventoryItem`, confirmed concurrently, each lock their own line row (the Phase-1 fix locks the *line*), both read qty=5, both write 5+delta → lost update. Same pattern pre-exists at `routers/purchase_orders.py:312` (`receive_po`) and `routers/inventory.py:268` (manual adjust).

**Design — one shared helper, fix all three sites:**
- New `modules/inventory/stock.py::apply_stock_delta(db, *, item_id, delta, reason, notes, job_id=None, clamp_nonneg=False) -> int` that issues an **atomic** `UPDATE inventory_items SET quantity = quantity + :delta [, clamped] WHERE id = :id RETURNING quantity` (or `SELECT … FOR UPDATE` then update — RETURNING is cleaner), writes the `StockAdjustment`, and returns the new quantity. The DB does the add, so concurrent deltas serialize with no lost update.
- `clamp_nonneg=True` reproduces the manual-adjust endpoint's `max(0, …)`; confirm.py + receive_po pass `False` (allow-negative per Doug 2026-05-10 + credit memos).
- Refactor `confirm.py` stock branch, `receive_po`, and `routers/inventory.py` adjust onto the helper. Fixes the two pre-existing races for free.

**Scope decision:** minimum = confirm.py only; recommended = all three (the helper is the same effort and the pre-existing races are real). See Open Decisions.

**SQLite note:** the atomic `UPDATE … quantity = quantity + :delta` is expression-based and works identically on SQLite, so tests are portable; `FOR UPDATE` (if used) is a no-op on SQLite but tests are single-threaded.

**Tests:** unit-test `apply_stock_delta` (delta up/down, clamp on/off, StockAdjustment written, returns new qty); a two-line/same-item confirm test asserting the final quantity equals base + both deltas (catches the lost update at the logic level even without true threads).

**Files:** new `modules/inventory/stock.py`, `modules/vendor_invoices/confirm.py`, `routers/purchase_orders.py`, `routers/inventory.py`, tests.

---

## C — Frontend review-queue UI

**The value delivery.** Backend is done; nothing surfaces to the office until this exists. Mirror the vendor-**statements** Vue views almost 1:1 (they are the sibling of this feature).

### C1 — Two views + nav/route

- **`views/VendorBillsView.vue`** (queue) — copy `VendorStatementsView.vue`: Toolbar with Refresh + PrimeVue `FileUpload` (`accept="application/pdf"`, `@uploader` → `api.post('/api/vendor-invoices/upload', formData)`), a status filter (`SelectButton`: All / Open / Needs-review / Paid → `?status=` + `?needs_review=`), a `<DataTable>` of `InvoiceSummaryOut` with `<Tag>` status chips, money/date formatters, a "⚠ possible duplicate" chip when `possible_duplicate_of_id`, a "needs review" chip when `!invariant_ok` (from `notes`) or `reviewed_at == null`, row-click → `/vendor-bills/:id`.
- **`views/VendorBillDetailView.vue`** (the review screen) — **split layout**:
  - *Left pane:* the PDF via `createAuthedBlobUrl('/api/documents/{invoice.document_id}/download')` → `<iframe>` (pattern from `DocumentsView.vue`; revoke on unmount).
  - *Right pane, header:* vendor / invoice # / date / total / terms / due; a **job-match** row showing `suggestions[]` (ranked, with score + reason) as one-click chips + a `Select`/`AutoComplete` fallback → `PATCH {matched_job_id}`; a possible-duplicate banner linking the other invoice; an invariant-mismatch banner when `!invariant_ok`; a Paid toggle → `PATCH {status}`.
  - *Right pane, lines:* one row per `LineOut` (mirror `LineItemEditor.vue`'s CSS-grid rows + the parts-checklist provenance pill), showing kind/description/qty/unit_cost/line_total, a **disposition `SelectButton`** (job / stock / overhead / skip — like the classification SelectButton in `VendorStatementDetailView`), a conditional target control (job `Select` defaulting to `matched_job_id`; inventory-item `Select`/`AutoComplete` from `/api/inventory/parts` for stock; a reason `InputText` for skip; a "update catalog cost" checkbox for stock), and a per-row **Confirm** button → `POST /lines/{id}/confirm` with `savingLineId` gating. Confirmed rows render read-only with an outcome badge (Expense created / received N into stock / skipped: reason). A "Confirm all as suggested" convenience that loops non-skip defaults (skip-defaulted lines require individual confirm, per [AUDIT-R2]).
- **Route** (`router/index.js`): two entries, `meta.requiresPermission: 'vendor_invoices.read'`, lazy-imported (copy the vendor-statements pair).
- **Nav** (`constants/modules.js`): add `{ key: 'vendor_bills', label: 'Vendor Bills', icon: 'pi pi-inbox', to: '/vendor-bills', type: 'Invoices', permission: 'vendor_invoices.read' }` in the `accounting` category next to Vendor Statements. ⚠ `navRouteCoverage.spec.js` pins nav-key↔route parity + globally-unique keys — add both together.

### C2 — Invoice-create checklist badge (the [AUDIT-R2] fix)

Separate, small, but part of "make the billing spine correct": in `components/LineItemEditor.vue`, the parts-from-job checklist currently pre-checks `received` rows. Vendor-invoice-sourced `JobPartNeeded` rows (`source='vendor_invoice'`) must arrive **unchecked** and **badged** (source + vendor + invoice #) so the office prices/adds them deliberately instead of silently landing $0 lines on a customer invoice. Adjust the pre-check logic + add the provenance pill for the `vendor_invoice` source.

**Tests (vitest):** mirror `views/__tests__/OverheadView.spec.js` — mock `../../composables/useApi`, stub PrimeVue, `flushPromises`, assert the queue calls `/api/vendor-invoices` and renders rows; a detail spec asserting a disposition change + Confirm POSTs the right payload and re-renders the confirmed row; a `LineItemEditor.spec.js` case that a `vendor_invoice`-source part is unchecked + badged.

**Browser verification:** headed Playwright MCP per `/verifyplaywright` on a throwaway container serving the built dist + the real sample PDF, light + dark — upload → queue → open → PDF renders beside lines → confirm a job line → confirm a stock line → see effects; verify the office Inventory page quantity moved and the invoice-create checklist shows the vendor row unchecked+badged.

---

## D — Phase 2 Outlook auto-intake (redesigned)

**Do NOT code the original sweep.** Per [AUDIT-R3] the design conflated Graph capabilities and rests on a broken prod foundation. Redesign:

**Hard precondition (verify live first):** prod `outlook_subscriptions` non-empty + delta actually bootstrapping (was broken 2026-07-07 — TENANT_BASE_DOMAIN unset; fixes were unmerged PRs #115–#121). Confirm these are merged + deployed and delta sync is producing tokens before starting D. If the Outlook foundation isn't live, D is dead on arrival.

**Reuse what exists, add what's missing:**
- The date-filtered historical page-through already exists: `backfill_outlook_mailbox` (tasks.py:418, `$filter=receivedDateTime ge {cutoff}`, `BACKFILL_MAX_MESSAGES_PER_RUN=5000`). It is **metadata-only** today — extend it (and `sync_outlook_mailbox` delta, tasks.py:361) to be **attachment-aware**: for messages from an allowlisted vendor sender (config in `OutlookSettings` JSON columns) with `hasAttachments`, list + download PDF attachments and feed `upload_midwest_invoice` (source='email').
- **Add 429/Retry-After handling** to `graph_client.py` (`_request` currently raises on any ≥400, zero backoff) — an uncapped first sweep firing one `download_attachment` per PDF WILL get throttled. Add Retry-After respect + bounded retry.
- **Add an attachment-download cap per run** and a **per-message "attachments ingested" checkpoint** (a column/flag on the OutlookMessage row) so re-runs don't re-download every attachment — the current claim of cheap idempotency is false (record-idempotent, not cost-idempotent).
- **LLM cost ceiling:** a per-run cap on how many PDFs get sent to Claude vision (rung 2), since a 12-month backfill could push hundreds at once. Allowlist gating (non-vendor senders never reach the LLM) is already the right posture — keep it.
- **Layer 3 stays advisory** (a weak hint); rely on layer 4 (statement reconciliation, Phase 3) for the real double-bill catch.

**Sequencing within D:** (1) 429/backoff + attachment checkpoint in the Graph client (pure infra, testable in isolation), (2) attachment-aware delta ingest (ongoing new mail), (3) the repeatable backfill sweep with the download cap, (4) LLM rung + cost ceiling. Each is independently shippable.

---

## Open decisions for Doug

1. **Fold A + B into the Phase-1 branch before the PR** (recommended — Phase 1 lands solid, no migration needed since it's undeployed), or keep them as separate follow-up PRs after Phase 1 merges (then A needs a CREATE INDEX migration + vendor_key backfill)?
2. **Stock-race scope (B):** fix only the new confirm path, or refactor `receive_po` + the manual inventory-adjust endpoint onto the shared `apply_stock_delta` helper too (recommended — fixes two real pre-existing races for the same effort)?
3. **Start order:** recommended A+B (quick hardening, fold into Phase 1) → C (the UI, the value) → D (blocked). Confirm, or reprioritize C first if the demo/office need is urgent.
4. **D precondition:** is the prod Outlook subscription/delta foundation actually live now? If not, D stays parked.
