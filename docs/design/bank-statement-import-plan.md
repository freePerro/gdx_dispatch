# Bank Statement Import + Verification — Plan

**Status:** IN PROGRESS — PR 1 (evidence layer) = PR #254; PR 2 (import UI + check/deposit-ticket images) built on `feat/bank-statement-import-ui`, browser-verified against the full real corpus
**Date:** 2026-07-31 (revised same day after adversarial audit — see §11)
**Purpose:** Import monthly bank statements (PDF) into GDX as an evidence layer, and use them to *verify that recorded information is correct* — payments, expenses, vendor bill payments — against what actually hit the bank.

---

## 1. Why now

- QB is being phased out; the payment backfill is being entered directly into GDX. The bank statement is the only ground truth that is *of the bank* rather than *of somebody's books* — it is the natural verifier for the backfill (dates, amounts, completeness) and, later, for aging/dunning safety.
- The GL Phase 2 design ([gl-phase2-reconciliation.md](gl-phase2-reconciliation.md) §2.1) already reserved this exact seam: `bank_accounts` / `bank_statement_lines` / `bank_statement_imports` as an append-only evidence layer with tie-out validation and an R1–R6 match pipeline. The `bank_feeds` module explicitly avoided those table names to leave room for it (`modules/bank_feeds/models.py:9-11`). This plan builds that layer.
- The Banno live-feed path exists but is gated on the bank provisioning the app. Statement import works today with zero external dependencies, and both feeders were designed to converge on the same evidence/matching layer.

**Scope difference from GL Phase 2:** Phase 2 matches statement lines against *GL lines* and requires ledger posting to be live (it is dark, pending CPA review). This plan points the same matcher at *operational records* (payments, expenses, vendor invoices) so verification is useful immediately. When GL goes live, the right-hand side re-targets per the Phase 2 spec; the evidence tables do not change.

## 2. Source material (verified against a 9-statement corpus, Jan–Jun 2026)

Two accounts at the same institution (a community bank on a Fiserv-style statement layout): a business checking account (monthly statements) and a business savings account (quarterly-ish statements). Note the two accounts are titled to **different legal entities** (the Inc. vs the owner's DBA) — verification reports for the savings account relate to the sole-prop entity; scope decision is Doug's. Findings from running the whole corpus through `pdftotext -layout`, then audited against pypdf:

1. **Text-extractable PDFs** — no OCR needed. pypdf `extraction_mode="layout"` (the repo's existing technique, `modules/vendor_statements/parsers/midwest.py:186`) is the production extractor, and its output **differs from pdftotext**: audit verified transaction/check/daily-balance rows survive intact, but the overdraft/returned-fees summary block is emitted **four times** (overlapping draws pdftotext dedupes) and all column offsets differ. Consequences: the parser must match rows by per-line regex, never column offsets; the fees block must be skipped statelessly (safe to encounter any number of times); and test fixtures must mimic **pypdf's** output shape, not pdftotext's.
2. **Consistent section anatomy** (headers repeat on every page and must be de-duplicated):
   - `SUMMARY OF ACCOUNTS` block: beginning balance, count + total of deposits/credits, count + total of checks/debits, service charge, interest paid, ending balance, statement period dates.
   - `ACCOUNT SERVICE CHARGE` (fee detail rows)
   - `DEPOSITS AND OTHER CREDITS`
   - `CHECKS AND OTHER DEBITS` (note: the bank's own column header is misspelled `DESCRIPTIION` — match both spellings)
   - `--- CHECKS IN NUMBER ORDER ---` (physical checks, two `date / check# / amount` pairs per text line; `*` marks a break in check-number sequence)
   - `DAILY BALANCE INFORMATION` (three `date / balance` pairs per line)
   - `INTEREST RATE SUMMARY` (savings only)
   - A trailing check-image page whose captions look like `Check: N Amount: $X Date: D` — **must not be parsed as transactions** (deposit slips image as `Check: 0`); usable as a cross-check for check numbers.
3. **Row grammar:** ` M/DD  DESCRIPTION  AMOUNT[-|-SC]`. Debits carry a trailing `-`; the service-charge row carries `-SC`. Descriptions wrap onto following lines (no date, no amount) and can continue across a page break after the repeated page header. Amounts use commas and may lack a leading zero (`.48`). Dates lack a year — derive from the statement period (rule: transaction month earlier than the period-start month ⇒ period-end year, for Dec→Jan spans).
4. **Statement periods are not calendar months** (e.g. a "January" statement can run 1/01–2/01 and the next 2/02–3/01). Store real period dates.
5. **Savings statements overlap** (one quarterly statement re-covered a period an earlier statement already included) ⇒ transaction-level dedup is mandatory, not just file-level.
6. **Three validation invariants hold corpus-wide** — these become the import gate (§5). Definitions below are the *audited* forms (§11): the naive forms fail on real files.
   - **Balance equation:** `beginning + deposits_total + interest − debits_total − service_charge = ending`. The summary's deposit/debit counts and totals *exclude* interest and the service charge; the SC also appears as a `-SC` row inside the debits section (don't double-count).
   - **Daily-balance recompute:** for **each row the bank lists** in `DAILY BALANCE INFORMATION`, assert `beginning + Σ(amount_cents of parsed lines with txn_date ≤ row date) = row balance`. Evaluated *at the bank's listed dates* — never by iterating parsed transactions and emitting a row per active day, because the table can contain a period-start seed row on a day with **no transactions** (equal to the beginning balance; present in 4 of the 9 real files). This catches any missed, duplicated, or mis-signed transaction — the strongest single check.
   - **Cross-statement continuity (period-aware):** a statement's beginning balance must equal the ending balance of the statement whose `period_end + 1 day == period_start` — its *period predecessor*, not "the previous statement by date". The real corpus contains a quarterly statement that **restates** the period of an earlier statement (both start the same day); naive prev-by-date continuity is false there. Restated overlaps are instead validated by the §5.5 overlap-integrity check. Continuity is warning-level (statements can arrive out of order; gaps are flagged, not fatal).

## 3. Architecture decision

**Build a `bank_reconciliation` capability inside the `bank_feeds` module, using the GL Phase 2 reserved table names, with Alembic migrations.**

- **Why the reserved tables (not folding into `bank_feed_transactions`):** the Banno tables ship via ORM `create_all` with no migrations, already exist in prod, and would need manual ALTERs plus a nullable `connection_id` to host file-sourced rows (the plugin-table-drift trap). The Phase 2 spec treats `bank_statement_lines` as the canonical evidence table with Banno/Plaid as *feeders into* it — building it now means GL Phase 2 later is additive, not migratory.
- **Why inside the `bank_feeds` module:** one nav surface for "bank stuff" (the existing Bank Feeds view has Banks/Accounts/Transactions/Statements/Settings tabs), the `require_module("bank_feeds")` gate and `bank_feeds.read/manage` permissions already exist. Shipping this means enabling the `bank_feeds` module grant (currently no `company_module_grants` row — the surface is dark); with no Banno institution configured the API tabs just show empty states.
- **Why Alembic (not create_all):** main-app convention; avoids the known drift trap for future column adds.
- **Format support:** the Phase 2 spec anticipated `csv_generic | ofx | qfx`. Reality delivered PDFs, so v1 ships `pdf_community_bank` as the first `statement_import_format`; CSV/OFX parsers are follow-ups if the bank portal offers those exports (worth checking — they'd be cheaper to parse, but the PDF parser is needed regardless since PDFs are what exists and what the bank archives).

## 4. Data model (Alembic migration, new tables)

```
bank_accounts
  id · name · kind (checking|savings) · institution · last4
  statement_import_format (pdf_community_bank | csv_generic | ofx | qfx)
  gl_account_id nullable FK (wired in GL Phase 2)
  created_at
  UNIQUE (institution, last4)

bank_statement_imports          -- one row per uploaded statement file
  id · bank_account_id FK · file_sha256 UNIQUE · original_filename · storage_path
  period_start · period_end
  beginning_balance_cents · ending_balance_cents
  deposits_count · deposits_total_cents · debits_count · debits_total_cents
  service_charge_cents · interest_cents
  tie_out_status (passed | failed) · tie_out_report JSON
  lines_added · lines_deduped
  voided_at nullable · imported_by · created_at

bank_statement_lines            -- append-only evidence; no updates
  id · bank_account_id FK · import_id FK (first-seen batch, provenance only)
  txn_date · amount_cents (signed BigInteger; negative = money out)
  description (full, wraps glued) · section (deposit | debit | check | service_charge | interest)
  check_number nullable · occurrence_n
  line_hash UNIQUE per account  -- sha256(account|date|amount|normalized ANCHOR line|occurrence_n)
  created_at

bank_statement_line_sources     -- attestations: import X vouches for line Y
  id · line_id FK · import_id FK · UNIQUE (line_id, import_id) · created_at
```

**`line_hash` hashes the anchor line only** — the first physical line (the one carrying date + amount), normalized — *not* the glued wrap lines. Wrap-attachment is the least arithmetic-checkable part of parsing (§11 finding 2); hashing only the anchor makes dedup across overlapping statements robust to wrap-gluing differences, while the full description is still stored. `occurrence_n` (1-based count of identical `(date, amount, anchor)` lines within the key) keeps the hash stable under file reordering and correct for genuinely-identical same-day transactions.

**Attestations, not exclusive ownership.** The real corpus contains the triggering case (§11 finding 5): a line first seen in the May savings import is re-attested by the overlapping June restatement. With only `import_id`, voiding May would silently delete evidence June still vouches for. Every import — first-seen or deduped — writes an attestation row; **voiding an import deletes its attestations and then only lines left with zero attestations**. Confirmed matches referencing dropped lines go to an exception state, never silently unlinked. Lines are never edited; a bad import is always voided by batch.

The daily-balance table is stored in `tie_out_report` (validation artifact, not row data).

**Match tables ship in PR 3, not migration 050** — and use the GL Phase 2 §3.2 three-table shape, because the dominant revenue event in the real corpus is a *batched deposit slip* (whole months where 2–3 deposits carry all revenue): one statement line must match **many** per-invoice payments, which a single `line_id · matched_id` row physically cannot hold (§11 finding 1):

```
bank_matches           -- id · bank_account_id · rule (R1..R6|manual) · status
                       --   (suggested|confirmed|rejected) · confidence · note
                       --   · created_by · created_at
bank_match_lines       -- match_id FK · line_id FK  (statement side; 1:N)
                       --   partial UNIQUE (line_id) WHERE match not rejected
bank_match_externals   -- match_id FK · source_table (payments|expenses|
                       --   vendor_invoices|…) · source_id  (books side; 1:N)
                       --   partial UNIQUE (source_table, source_id) WHERE not rejected
```

## 5. Import pipeline

**Endpoint:** `POST /api/bank-feeds/statements/import` — multipart, multiple files per request (mirrors `routers/vendor_invoices.py:163` shape). Per file:

1. **File dedup:** sha256 vs `bank_statement_imports.file_sha256` → reject exact re-upload (dedup layer 1, same pattern as `vendor_statements/service.py:72,76`).
2. **Account resolution — anchored, never a global regex.** `ACCT ENDING nnnn` also appears *inside transaction descriptions* (the loan autopay line references the loan's own number, in the real corpus). Resolve last4 exclusively from the summary line that also carries `Statement Dates` (`ACCOUNT NUMBER … ACCT ENDING nnnn Statement Dates …`); take account title + checking/savings kind from the `SUMMARY OF ACCOUNTS` block. Auto-create the `bank_accounts` row on first sight, reuse thereafter.
3. **Parse** (`modules/bank_feeds/statement_parsers/community_bank.py`, pypdf layout mode): section state machine keyed on section headers (which repeat across pages and re-affirm state); skip page-header blocks, the (possibly quadruplicated) fees block, and the check-image caption page statelessly; per-line regex row matching; wrap lines glue to the preceding anchor row — including across page breaks; parse the summary block and daily-balance table separately as the validation oracle. Strictness follows the Midwest supplier-parser precedent: two error flavors ("not this bank's statement" vs "is one and the structure defeated us" — the latter is loud).
4. **Tie-out gate (the "is this data correct" check, per §2.6):**
   - balance equation ✓
   - parsed deposit/debit row counts and totals == summary counts/totals ✓ (checks from `CHECKS IN NUMBER ORDER` count toward debits; interest and `-SC` rows sit outside the counts; SC detail section, when present, must sum to the summary service charge)
   - daily-balance recompute at the bank's listed dates (§2.6 form) ✓
   - ending = beginning + Σ(all parsed signed lines) ✓
   - continuity vs the account's *period-predecessor* statement (§2.6 form; warning-level)
   - **A failed tie-out stores the import row + report but inserts no lines.** Nothing partially-parsed ever enters the evidence table. This is what makes an LLM-fallback rung safe to add later for other banks' formats: the arithmetic gate catches hallucinated parses — though note the gate proves *arithmetic* identity, not description fidelity; that's what §5.5 exists for.
5. **Overlap-integrity check (dedup layer 2, made loud).** When the new statement's period intersects existing non-voided imports for the account, both statements claim full coverage of the intersection — so over that window the evidence must agree **bidirectionally**: every newly parsed line in the window must hash-match an existing line, and every existing line in the window must be hash-matched by the new parse. A `(date, amount)` pair that matches while the hash doesn't = **description/wrap drift → import fails loudly** (this is the check that catches wrap-gluing bugs the arithmetic gate cannot see). Clean matches dedupe: no new row, an attestation row instead. Report `lines_added` / `lines_deduped` per import. Tie-out always runs on *parsed* rows, not inserted rows, so a fully-overlapping restatement still validates end to end.
6. **Storage:** original PDF to `$UPLOAD_DIR/bank_statements/` (same tree Banno documents use), served through the existing path-guarded download endpoint pattern (`bank_feeds/router.py:828-853`). The file is kept even when tie-out fails — it's the diagnosis artifact.

Other endpoints: `GET /statements/imports` (list with tie-out status), `GET /statements/imports/{id}` (summary + report + lines), `POST /statements/imports/{id}/void`, `GET /statements/lines` (filterable evidence list).

## 6. Phase B — verification against GDX records

Suggest-and-confirm (the `vendor_invoices/matching.py` + `recategorize.py` precedent). Left: `bank_statement_lines`. Right (until GL goes live): `Payment` (`payment_date`, `amount`, `reference`, `method`), `Expense`, `VendorInvoice`, plus the historical `qb_bank_transactions` mirror for the transition window. Rule ladder, adapted from Phase 2 §3.3:

| # | Rule | Logic |
|---|---|---|
| R1 | Reference | check number in `CHECKS IN NUMBER ORDER` ↔ `Payment.reference`; processor payout ids in descriptions |
| R2 | Exact 1:1 | amount equal + date within ±3 business days + no competing candidate |
| R3 | Deposit sweep | a statement deposit is a *batched slip* — match against the sum of recorded payments (cash/check) within a 7-day window; bounded subset-sum (n≤12, k≤6), else manual grouping UI |
| R4 | Tolerance | R2 within a small configurable residual — suggest-only |
| R5 | Known-payee rules | recurring bank-only reality: card-network fuel/parts purchases → expense suggestions; SaaS subscriptions, loan autopay, merchant-services fees; **inter-account transfers** auto-classified `transfer`, paired when both accounts are imported — note the corpus contains **two distinct transfer grammars** (`Transfer from xNNNN to xNNNN` and `Trnsfr Frm/To Act Ending in NNNN` split across a wrap line); the classifier must handle both |
| R6 | Unmatched aging | anything unmatched after triage → exception list |

**Verification reports (the actual deliverable of Phase B):** per account + period —
- **Unmatched statement deposits** → money hit the bank that GDX has no payment for (missed revenue recording).
- **Unmatched GDX payments** → recorded but never hit the bank (mis-dated, duplicated, bounced, or QB-era artifacts). Directly verifies the QB paid-status Phase 2 backfill.
- **Date drift** — bank date vs recorded `payment_date` distribution (verifies the payment-date-recording work).
- **Unmatched debits** → spending with no `Expense`/`VendorInvoice` record (expense-capture gap).

Matches never mutate the matched records; confirming/rejecting is metadata-only in this phase (posting rules arrive with GL Phase 2).

## 7. UI

Extend `BankFeedsView.vue`:
- **Statements tab** (BUILT, PR 2): an **Import statement PDFs** action (multi-file upload) and an "Imported statements" table above the Banno-fetched documents — tie-out badges (✓ passed / ✗ failed), lines added/deduped, details dialog with the full check report, continuity warnings, and the paired **check/deposit-ticket image gallery** (scans fetched as authenticated blobs — they show full account numbers and are never plain URLs). Void uses a local confirm Dialog on purpose — `useDestructiveConfirm` can auto-accept without rendering (issue #215).
- New **Reconcile tab** (Phase B): per-account period picker → statement lines with match status, suggestion confirm/reject, the four verification reports, deposit-grouping UI for R3 manual cases.
- Nav/module: enable the `bank_feeds` module grant at ship time; permissions `bank_feeds.read` (view) / `bank_feeds.manage` (import, void) / match-confirm under `accounting.write`.

**Deploy step — enabling the module grant** (the surface is dark until this runs; the `bootstrap_modules_for_tenant.py` tool referenced in `core/modules.py:186` does not exist in the repo, so the grant is a one-row SQL insert on the tenant DB, verified working during PR 2 browser acceptance):

```sql
INSERT INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
SELECT gen_random_uuid(), '<company-id>', 'bank_feeds', now(), now()
WHERE NOT EXISTS (SELECT 1 FROM company_module_grants
                  WHERE company_id = '<company-id>' AND module_key = 'bank_feeds');
```

## 8. Testing & acceptance

- **Fixtures are synthetic.** Real statements contain names, addresses, and account digits and must never enter the repo (public-repo hygiene). Build a fixture generator that emits **pypdf-shaped** layout text (not pdftotext-shaped — §2.1) covering: all sections, wrap lines including across page breaks, the caption page, the quadruplicated fees block, a period-start daily-balance seed row, an overlapping statement pair (dedup + attestation), a *restating* statement (period-aware continuity), both transfer grammars, a description containing `ACCT ENDING nnnn` (loan autopay), and a Dec→Jan year span. Unit-test the parser and every tie-out invariant against it, including deliberate corruption cases (missing row, flipped sign, altered balance, **mis-glued wrap in an overlap window**) that must fail the gate.
- **Honesty notes (stated coverage boundaries, not oversights):**
  - Text-level fixtures exercise the grammar + tie-out + service layers; the pypdf *extraction* step itself is exercised only by the real-corpus acceptance below (generating true synthetic PDFs isn't worth a new dependency). `tools/bank_statement_acceptance.py` makes that run reproducible against any statements directory.
  - **The wrap-fidelity defense (§5.5) can never fire on the checking account**: overlap integrity needs a period intersection, and monthly checking statements are disjoint. Description/wrap contamination there is caught only by the strict grammar + fail-loud wrap rules, not by cross-statement comparison, until the Banno feed provides a second evidence source (post-cutover audit finding).
  - **The first real overdraft/returned-item month is a known refusal mode**: every corpus file shows `$.00` fees, so the parser has never seen an actual OD-fee row and its summary-count semantics are unknowable until one exists. Expected behavior: that month FAILS tie-out with a named check — the gate refusing what it can't prove — and the parser gets extended against the real file. Fail-closed is the design, but it must not be mistaken for "handles everything".
- **Acceptance (local, real data, per manifest discipline):** import the full real 9-statement corpus on a throwaway container — every tie-out green, period-aware continuity green for both accounts, overlapping savings statement dedupes cleanly with a passing tie-out and attestation rows. Then run the Phase B reports for one month against real GDX payment data and review the exceptions with Doug — the exceptions *are* the product.
- Contract tests for the new endpoints; frontend vitest for the import tab states.

## 9. Delivery slices

1. **PR 1 — evidence layer:** migration 050 (`bank_accounts`, `bank_statement_imports`, `bank_statement_lines`, `bank_statement_line_sources` — **no match table**), parser, tie-out + overlap-integrity service, import/list/detail/void endpoints, synthetic-fixture test suite.
2. **PR 2 — UI + images (BUILT):** Statements-tab import + tie-out display + details dialog; **check/deposit-ticket image extraction** (migration 051, `bank_statement_line_images`): the statement's trailing images page carries one scan per caption — deposit tickets and written checks — parsed captions pair scans to evidence lines by check number / (amount + full date), in caption order, degrading to an unpaired gallery on any count mismatch. Empirical acceptance: 34/34 scans paired across the real corpus; browser-verified end-to-end (all 9 PDFs uploaded through the real UI on a throwaway container, light + dark, void + re-import-after-void exercised live). Deploy step: the §7 module-grant SQL.
3. **PR 3 — verification:** migration for the three match tables (§4 shape), R1–R6 matcher, match endpoints, Reconcile tab, the four reports.
4. **Later (separate effort):** GL Phase 2 proper — re-target matcher right-hand side to GL lines, tie-out-to-GL assertion, period locks, posting rules. Evidence tables unchanged. Banno feeder converges here too (its `line_hash` gains `occurrence_n` at that point, per the Phase 2 note).

## 10. Open questions

1. Does the bank's online portal offer CSV/OFX/QFX export? (Cheaper parsing for future months; PDF stays the archival import path either way.)
2. Are the other accounts referenced by inter-account transfers worth importing too (so transfers pair up), or stay out of scope?
3. Should R5 auto-create draft `Expense` rows for recurring card purchases (fuel/parts), or suggest-only? Suggest-only is the safe default.
4. Monthly ritual: manual upload when the statement email arrives, or watch the email inbox ingest (the vendor-statement email rung precedent) for the bank's statement notifications later?

## 11. Adversarial audit findings (2026-07-31) and how this revision addresses them

An adversarial review re-ran the whole corpus through a scratch parser and live pypdf before implementation started. Its findings, all incorporated above:

1. **Match cardinality (the foundational one):** the original single-row `bank_matches` could not represent a batched deposit ↔ N payments — the *dominant* revenue shape in the corpus. → §4 now uses the GL Phase 2 three-table shape; no match table ships in migration 050.
2. **Tie-out is arithmetic-only:** description/wrap fidelity is invisible to all balance checks, so a wrap-gluing bug would silently poison `line_hash` dedup while wearing a green badge. → anchor-line-only hashing (§4) + the bidirectional overlap-integrity check (§5.5), which turns description drift into a loud import failure exactly where it matters.
3. **Two invariants were false as naively stated:** the daily-balance table contains period-start seed rows on days with no transactions (4/9 real files), and continuity is only true *period-aware* (a quarterly restatement's predecessor is the statement before the restated window, not the previous by date). → §2.6 redefines both; §8 adds both to fixtures so synthetic tests can actually fail on them.
4. **pypdf ≠ pdftotext:** the production extractor quadruplicates the fees block and shifts all column offsets vs the tool the format was first surveyed with. → §2.1 documents it; parser is per-line-regex with stateless skips; fixtures are pypdf-shaped; the extraction step's coverage boundary is stated honestly in §8.
5. **Void-by-batch was undecidable under overlap** (the corpus contains the exact triggering pair). → the `bank_statement_line_sources` attestation table (§4): voiding deletes attestations, and only orphaned lines drop. Runner-up finds folded in: anchored account resolution (§5.2), both transfer grammars (§6 R5), the DBA entity note (§2).

A **third adversarial audit ran on the PR 2 diff** (UI + images) before its commit. It found and forced fixes for: a **dead toast** (`api.toast` doesn't exist on `useApi` — duplicate-file and parse-error outcomes, which create no table row, would have been completely invisible in the UI; now uses PrimeVue `useToast` directly, and the same pre-existing dead call in the OAuth popup path was fixed too); **gallery failures could veto evidence** (undecodable image codecs like JBIG2, CMYK→PNG saves, disk errors — now every extraction/save failure degrades to a smaller or absent gallery, never a failed import); **RGBA blank filler beat the blankness filter** (transparent pixels convert to black in L mode — now alpha-flattened onto white first); **void unlinked files before commit** (a failed commit would leave rows pointing at deleted files — now commits first, unlinks after); and a blob-URL leak when the details dialog closes mid-fetch. Accepted-risk items it confirmed rather than broke: equal-count order-swap mispairing (structurally undetectable without OCR; the visible caption text beside each scan is the human catch), and the pairing thresholds being tuned to a 9-file corpus.

A **second adversarial audit ran on the implementation diff** before the first commit. It independently reproduced the 9/9 real-corpus acceptance and confirmed the arithmetic core, and found: a live crash (impossible date like `6/31` escaped as a bare ValueError → 500 mid-batch — fixed + regression-tested); fabricated `0` stored for blank summary counts (columns made nullable, stored as parsed); no stated recovery for a legitimate hash drift (documented: void conflicting imports, re-import both — deliberately no force flag); and the two §8 honesty notes above (checking-account wrap-fidelity hole; OD-fee refusal mode). Line-hash stability across pypdf version bumps remains an accepted risk — the overlap check fails loudly, never silently, if it breaks.
