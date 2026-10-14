# GL Phase 3 — Parallel Run, Trust Switch & the QBO End-State

**Status:** v3 FINAL-DRAFT — survived 2 adversarial audit rounds (R1 NOT READY → six fixes; R2 delta READY-WITH-CONDITIONS → four surgical edits applied: reversal-window cap + amount-drift tracking, Phase 2 §5.5 backfill runbook now specified, AJE gate re-sequenced to tax season, §8 build-item text corrected). Blocked on CPA items (§9) before the gate can start.
**Revised 2026-10-13 (Doug on GDXA-877, recorded in GDXA-902):** the
trust-switch gate no longer uses QBO as its comparator. QB has read
`needs_reconnect` since 2026-08-18, and Phase 2's QBO half is WON'T BUILD
(2026-08-21). These now name the internal check (the daily trial-balance
zero-check plus the Phase 2 §2.2 statement tie-out): §3.2 criterion 1, the
QBO-only gate-start preconditions, §3.3's sign-off pack and §5.1 step (4).
§3.2 criterion 3 records instead that no AR comparator remains. The 2026-07-02
wording stays beside each one as history.
Limits of the internal check:
- The zero-check holds by construction (balance trigger), so the tie-out is
  the binding leg.
- The §2.2 tie-out is cash-only and **not built**. There is no tie-out-to-GL
  assertion, it is blocked on the 1050 clearing, and it is not the
  import-level `tie_out_status`.
- **Nothing in the gate checks AR any more** (see criterion 3).
**The gate cannot pass today.** The blockers this revision touches are §2.2
(not built) and an AR check (Doug's call). Other parts of the gate are also
unbuilt and are not changed here. They include criterion 4 (no 1060 account in
the chart of accounts, no orphan-bt queue), criterion 6 (no period-lock path
outside cutover) and the §3.2 scorecard. This list is not exhaustive. No job records
the daily zero-check; it can be re-run per day with `trial_balance(as_of=…)`.
Left as written, not gates on QBO-as-comparator:
- Criterion 5 (push health) is now vacuous; it is noted in place.
- §5.4's monthly/quarterly "diff" cadence, §7's rollback via QBO catch-up and
  "unexplained diff" abort trigger, and §6's Option A all belong to the QBO
  end-state decision, which is out of scope here.
The §9 CPA items are untouched and still open.
**Date:** 2026-07-02
**Depends on:** Phase 1 (`gl-phase1-core-ledger.md` v3), Phase 2 (`gl-phase2-reconciliation.md` v4). **This phase is mostly process and decision-making; the build surface is small (§8).**
**Provenance:** **[VERIFIED]** adversarially verified 3-0 against primary sources (deep-research 2026-07-02, 24/25 confirmed) · **[CONVERGENT]** consistent across regulatory/vendor/practitioner sources but no authoritative standard exists — the gap-fill research explicitly established the *absence* of an AICPA/ISACA prescription, so these are honest convergences, not standards · **[CODE]** direct code reading · **[JUDGMENT]** / **[CPA]** as before.

## 1. What the trust switch is

The moment GDX's GL becomes the **management book of record**: Doug runs the business off GDX's reports, the monthly close happens in GDX, and QBO is demoted to the **tax book** — the thing the CPA files from. It is an authority change, not a data migration: both books already exist and reconcile monthly by the time this phase starts (Phase 2's verification loop).

Non-goals: moving payroll or tax filing out of QBO (permanent QBO territory); building new accounting features (Phases 1–2 built them); any forced QBO downgrade (§6 recommends against one for at least a year).

## 2. Tax foundation — settled, with one standing artifact required

All **[VERIFIED]** against regulation text:

- **Accrual internal books beside a cash-basis return is expressly accommodated.** IRC §446(a) states the book-conformity rule, but Treas. Reg. §1.446-1(a)(4) defines required records to include "a reconciliation of any differences between such books and his return" — the regulatory hook that permits books on one basis, return on another. AICPA practitioner guidance concurs (accrual/GAAP books don't bar cash-method returns).
- **Enforcement is the §446(b) "clearly reflect income" override**, not an automatic penalty — and the trust switch itself triggers **no tax filing** because the *return's* method doesn't change. If the return's basis ever changes, that requires Form 3115 (Rev. Proc. 2015-13 automatic consent), prospective only.
- **The standing artifact:** a **book-to-return reconciliation workpaper** — GDX accrual trial balance → cash-basis conversion → return figures. Phase 1's derived cash-basis reports generate exactly this; Phase 3 packages it as a named report in the year-end package (§5.3), produced every year, kept with the books. This is the §1.446-1(a)(4) compliance artifact and it is **non-optional**.
- **[CPA]** confirm filing basis + entity type one final time at the go/no-go (carried from Phase 1 §12; the research says the architecture is safe either way, but the workpaper's direction depends on the answer).

## 3. The parallel run

### 3.1 It already started

Phase 2 go-live *is* the start of the parallel run: two books, entity-level push keeping QBO current, monthly statement tie-outs, monthly GDX-vs-QBO diff. The classical objection to parallel runs — doubled workload — is mostly absorbed by automation here: nobody keys anything twice; the push does the second book, and the diff does the comparison labor (**[CONVERGENT]** — the doubled-work warning appears in ISACA-derived material, Oracle's FLEXCUBE implementation guide, and FFIEC guidance; our automation is the mitigation, not a reason to skip the discipline).

*(2026-10-13, GDXA-902: the push and the GDX-vs-QBO diff never shipped, and both are WON'T BUILD. So the "second book" half of this run never happened. What remains of the parallel run is the internal check: the daily trial-balance zero-check plus the monthly bank statement tie-out. The gate below is restated on that basis.)*

### 3.2 Acceptance gate **[CONVERGENT — criteria-based, not fixed-duration]**

No authoritative body prescribes a duration. The convergent practice (Oracle: "at least one End of Month included"; accounting-migration guides: "at least one full accounting cycle, often 30–60 days"; FFIEC: "until they verify accuracy and reliability") is criteria-based with a close-cycle floor. GDX's gate, deliberately stricter than the floor because the marginal cost of waiting is near zero **[JUDGMENT]**:

**Gate-start preconditions** (**[AUDIT-R1]** — the count begins at the first close AFTER all of these; earlier months are rehearsal, which also settles the old §9.5 question):

- Phase 2 fully deployed: `push_payment` live **and the historical-payment backfill/QBO-dedup procedure executed** (Phase 2 §5.5 — specified there as of v4.1), Stripe catch-up posted, first clean statement tie-out done.
- **QBO revenue ItemRefs configured** (Phase 2 §5.3's three-Item mapping) — no longer optional: without it, revenue diffs are coarse-group forever and gate criterion 1 can't mean anything at account level.
- *(2026-10-13, GDXA-902: the QBO parts of these preconditions are dropped. These are `push_payment`, the QBO-dedup backfill and the ItemRefs bullet. They existed to make the QBO diff possible, and that diff is WON'T BUILD. What remains: Stripe catch-up posted and the first clean statement tie-out done.)*

**Three consecutive clean monthly closes**, where *clean* means all of:

1. **Trial-balance zero-check:** the trial balance's `zero_proof_cents` is 0 on every day of the month. This is a smoke check: the balance trigger makes it 0 by construction, so it fails only if a write got past the trigger. *(Reworded 2026-10-13, GDXA-902. The 2026-07-02 wording, kept as history: "**Trial-balance diff:** zero unexplained differences (every diff in an `expected_diff_class`, §Phase 2 6.2) — under the classification governance below." That diff was against QBO.)*
2. **Bank tie-outs:** every bank account tied out to its statement, no `aggregate_only` flags in the window (Phase 1-era months exempt).
3. **AR:** no comparator left. Criteria 1 and 2 do not catch AR errors such as a payment applied to the wrong invoice, an invoice that never posted, or open-invoice balances drifting from the AR control account. The candidate internal check is an open-invoices-vs-AR-control tie. It does not exist yet, and whether to build it is Doug's call. Until then this criterion is **unmet, not passed**. *(Reworded 2026-10-13, GDXA-902. The 2026-07-02 wording, kept as history: "**AR aging:** GDX vs QBO agings match within tolerance.")*
4. **Stripe:** 1060 zeroed per payout; orphan-bt queue empty or triaged.
5. **Push health:** outbox empty at close; no `qb_dirty` older than the threshold; zero `blocked_on` items. *(2026-10-13, GDXA-902: the QBO push will not be built, so this passes trivially. It is left as written because it is not a comparator.)*
6. **Close ritual executed** (§5.1) including the period lock.

A dashboard renders the gate as a 3-slot scorecard. Any failed month resets the count — consecutive is the point (**[CONVERGENT]** — "verification of financial equality... any laxity leads to indefinite extensions," Oracle; the reset forces root-causing instead of averaging).

**Classification governance** (**[AUDIT-R1]** — without this, criterion 1 is a self-certifying rug: classify everything as "expected" and the gate passes itself). *(2026-10-13, GDXA-902: this governs the QBO diff's `expected_diff_class`. With criterion 1 reworded to the zero-check, nothing in the gate is classified any more, so this section is history. The same self-certifying risk now sits in Phase 2 §2.2's "accepted-outstanding" lines, which carry no rationale or age rule. That is not addressed here.)*

- Every `expected_diff_class` assignment requires a **rationale string** and is audit-logged; assignments have a lifecycle — `timing` class diffs carry an expected-reversal window (**a register field, capped at 60 days unless the CPA approves longer** — **[AUDIT-R2]**) and **auto-escalate to unexplained** if the diff persists past it.
- **Amount-drift tracking for non-reversing classes** (**[AUDIT-R2]** — `qbo_only`/`gdx_only` mappings never "reverse," so without this an approved classification is a permanent blind spot where new errors hide): each such classified mapping records a baseline amount at approval; the scorecard tracks the mapping's amount month-over-month, and drift beyond tolerance re-flags the account as **unexplained** despite the standing classification.
- The scorecard reports **resolved vs classified counts** per month, and lists every classification created or changed during the gate window.
- **The classification register is itself a CPA sign-off artifact** (§3.3): the CPA reviews *what was classified away and why*, not just the residual diff report. Doug can classify unilaterally month-to-month; the gate cannot pass without that register surviving CPA review.

### 3.3 Go/no-go

- **Sign-off: Doug + the CPA**, at a scheduled review of the third clean close (**[CONVERGENT]** — every cutover source assigns a named authority; for a one-owner shop with an external accountant, it's these two). The CPA reviews: the three diff reports, one bank tie-out, and the draft book-to-return workpaper (§2). *(2026-10-13, GDXA-902: "the three diff reports" now means the three months' zero-check and tie-out results, since there is no QBO diff.)*
- **Hard switch date**, first day of the next month after sign-off (**[CONVERGENT]** — "set a hard cutover date... without one, you'll run both forever"; "don't do the cutover when you have to close your books"). Never mid-quarter-close, never during tax season. **[CPA]** pick the month.
- The decision is recorded in the repo (`docs/decisions/ADR-0xx-gl-trust-switch.md`) with the scorecard attached.

## 4. The switch itself

What actually changes on the switch date — deliberately small:

1. A config flag (`gl_book_of_record = true`): GDX financial reports drop any "provisional" labeling and become the official management reports; the demo/report headers say so.
2. The monthly close ritual (§5.1) becomes mandatory, not rehearsal.
3. QBO's role is formally "tax book": nobody makes management decisions from QBO reports; the CPA's workflow is unchanged (Option A, §6).
4. The verification loop **continues unchanged** for at least 12 months post-switch (**[JUDGMENT]** — it is the rollback instrument, §7, and its cost is near zero).

## 5. Operating model after the switch

### 5.1 Monthly close ritual (GDX)

Distilled from the convergent checklists (Intuit month-end guide, practitioner close checklists) minus what automation already does: (1) import statements + clear match queue; (2) tie out every bank account; (3) review AR aging, unbilled work, uncategorized expenses; (4) confirm the trial-balance zero-check held every day of the month *(2026-10-13, GDXA-902; was "run the GDX-vs-QBO diff, resolve or classify everything")*; (5) eyeball P&L vs prior month; (6) lock the period (`gl_period_locks`). Target: under an hour for a clean month.

### 5.2 CPA year-end package (new report bundle — the main §8 build item)

What CPAs uniformly need from clients on third-party GLs (**[CONVERGENT]** — practitioner year-end checklists; the trial-balance-software ecosystem, CCH Axcess et al., is built on exactly this flow): **final trial balance (accrual + derived cash-basis) · GL detail export (CSV) · AJE list from prior year with posting status · book-to-return reconciliation workpaper (§2) · bank/loan statement index · fixed-asset additions list**. One button, one zip. The CPA never needs GDX credentials — tax-prep-only from a client TB is the standard engagement architecture.

### 5.3 AJE flow-back (the reverse-direction problem — answered)

**[VERIFIED/CONVERGENT]:** there is **no machine-readable AJE interchange outside the Intuit ecosystem** (the electronic import is QuickBooks-Desktop-specific). The universal convention for third-party-GL clients: the CPA delivers an **adjusting-entry report** (their trial-balance software produces it), and the client keys the entries into their own GL. GDX implements this as:

- A `cpa_adjusting` manual-JE type (Phase 1 P7 variant; key `cpa_aje:{tax_year}:{n}`), posted by Doug from the CPA's report, `effective_at` = fiscal year-end (posting into the locked period via the `accounting.close` override, audit-logged — this is exactly what the override exists for).
- **Account restriction** (**[AUDIT-R1]** — an AJE touching a bank account would silently falsify that month's statement tie-out, and Phase 2's re-assertion only fires on reopen, not on override posts): `cpa_adjusting` entries **may not touch any account mapped in `bank_accounts`** (or 1060). CPA AJEs are overwhelmingly non-cash (depreciation, accruals, owner-comp reclasses) so this costs nothing in practice; the rare cash-touching correction must go through the Phase 2 §2.2 tie-out reopen flow instead, which re-asserts the affected months.
- A **book-to-return net-income check** (**[AUDIT-R1]** — generalized from "Schedule M-1 check": M-1 exists on 1120-S/1065 but a Schedule C sole proprietor has no M-1): after posting, GDX's book net income for the tax year must reconcile to the return through the §2 workpaper — against Schedule M-1 line 1 for an S-corp/partnership, against Schedule C net profit plus workpaper adjustments for a sole proprietor. The report label resolves with the entity-type **[CPA]** answer. One-line pass/fail on the year-end screen.
- **Under Option A the CPA's workflow truly is unchanged** (**[AUDIT-R1]** consistency fix): the CPA keeps posting AJEs into QBO exactly as today; *Doug* mirrors them into GDX from the CPA's adjusting-entry report. The dual-keyed surface is a handful of year-end entries. Only under Options B/C does the CPA's delivery format matter.
- **Timing, sequenced to tax season** (**[AUDIT-R2]** — CPA AJEs arrive at return time, March–April or later, so blocking the *January* close would stall the ritual for months): prior-year AJE status starts as `pending`. Between year-end and mirroring, CPA-posted QBO AJEs surface in the monthly diff under a system class `pending_aje` (time-boxed: auto-escalates if the return-filing season passes without resolution) instead of flooding unexplained. When the CPA delivers the AJE report, Doug mirrors, the book-to-return check runs, and the `pending_aje` class clears. **The first close after return delivery** — not January — is blocked until status is `posted` or `none-issued`. The drift warning stands ("if you don't add the adjusting entries, you'll pay your CPA to do this again next year"); the sequencing just matches when AJEs actually exist. **[JUDGMENT]**

### 5.4 Safeguards cadence

Monthly: close ritual + diff. Quarterly: CPA glance at the diff trend + classification register (**[CPA]** confirm appetite/fee). Yearly: year-end package, AJE flow-back, book-to-return check, and a retention snapshot. **Retention correction** (**[AUDIT-R1]**, verified against code): both existing backup scripts prune at 30 days (`scripts/backup-db.sh:26` `-mtime +30 -delete`; `gdx_dispatch/scripts/backup.sh` 30-day S3 cutoff), so the GL export cannot simply "ride the DB backups" — it gets a **dedicated archival tier** (separate path/S3 prefix with a ≥7-year lifecycle, added to the `/backup` runbook and exercised in the next restore drill). The GL is IRS "books and records" now; 30-day retention would be a compliance failure. <!-- gdx_dispatch/scripts/backup.sh deleted 2026-09-06 (multi-tenant S3 script nothing scheduled); link-ok -->

## 6. QBO end-state — decision matrix

| | A: Keep QBO + entity push (status quo) | B: Demote to QuickBooks Ledger | C: Full QBO exit |
|---|---|---|---|
| Cost | current QBO SKU (~$35–100/mo) | $10/mo, **CPA must own it** (accountant-only, firm-billed, non-transferable) **[VERIFIED]** | $0 to Intuit |
| Migration | none | **fresh company file required** — no downgrade path from an existing QBO sub **[VERIFIED]**; opening balances re-established | full export first: **1 year read-only after cancel**, Excel/QBD only **[VERIFIED]** |
| GDX integration | unchanged (Phase 2 push) | **entity push dies** — Ledger has no invoicing/payments/AR; only JE-level and bank feeds fit; whether the QBO API even accepts writes against a Ledger realm is **unconfirmed** **[VERIFIED gap]** → would require building a summarized-JE push | integration retired; GDX GL stands alone |
| CPA workflow | unchanged | native (Ledger is built for exactly this: year-end/tax-only clients, TB sync to ProConnect) **[VERIFIED]** | CPA works from the year-end package only |
| Payroll | **moot — external payroll company, no Intuit dependency** (Doug, 2026-07-02; the Workforce-SKU facts below kept for reference only) | same — payroll unaffected by a Ledger move | same — payroll unaffected by exit |
| Reversibility | total | low (fresh file, upgrade path exists but history is split) | lowest |

**Recommendation [JUDGMENT]:** **Option A through at least the first full tax year post-switch.** It is the cheapest *risk* posture: the CPA's world doesn't move while the trust switch beds in, the entity push is already built and free (Core writes), and the folklore that "real platforms summarize to the GL" was **refuted** — ServiceTitan's own docs show entity-level invoice export (JE-form only for specific payment classes) **[VERIFIED]**, so keeping entity push forever is a legitimate end-state, not a smell. Revisit B with the CPA after the first clean year-end: the $10 Ledger + summarized-JE push saves real money *only if* the CPA is enthusiastic and the Ledger-realm API question (§9.2) resolves.

**Standing bet, named (**[AUDIT-R1]**):** Option A assumes Intuit keeps Core writes unmetered. That is current policy, not contract (the `qb-api-changes-2025-2026` research flagged "how stable is writes-stay-free" as an open question). **Revisit trigger:** any Intuit announcement reclassifying write calls or metering Core → re-run this end-state decision immediately; checked at each yearly review alongside Intuit's release notes.

## 7. Rollback

**[CONVERGENT]** cutover realism: "rollback" in accounting migrations means extending the dual-run or re-designating authority — not reverting data. GDX's version:

- **Before the switch:** nothing to roll back; QBO is still authoritative; the gate simply doesn't pass.
- **After the switch (Option A makes this cheap):** QBO has stayed fully fed *when the push is healthy* — but a failed push is itself a likely abort cause (**[AUDIT-R1]**). Rollback therefore starts with a **QBO catch-up**: drain the push outbox (its backlog list bounds exactly what QBO is missing), verify via the diff, and only then re-designate QBO authoritative. Flip `gl_book_of_record` off, root-cause, re-attempt later. The GL never stops posting either way — append-only, nothing lost.
- **Abort triggers [JUDGMENT]:** two consecutive post-switch closes that would have failed the §3.2 gate; a material unexplained diff the drill-down can't resolve within a close cycle; or CPA rejection of the year-end package. Each trigger and its response is pre-written in the switch ADR so the decision under stress is a lookup, not a debate.

## 8. Build items (small, mostly reporting)

1. Year-end package export (§5.2) — one endpoint + zip assembly; contents are existing reports plus the GL-detail CSV.
2. Book-to-return reconciliation workpaper report (§2) — accrual TB → cash conversion schedule (the Phase 1 cash-basis derivation, presented as a workpaper).
3. `cpa_adjusting` JE type + prior-year-AJE close gate (tax-season-sequenced) + book-to-return check line (§5.3).
4. Trust-switch scorecard (§3.2) — renders from data the Phase 2 loop already produces.
5. `gl_book_of_record` flag + report labeling.
6. Retention snapshot job (§5.4) — yearly GL export to the **dedicated ≥7-year archival tier** (NOT the existing 30-day-pruned backup flow — §5.4's code-verified correction), plus the `/backup` runbook update and a restore-drill exercise.

## 9. Open questions

1. **[CPA]** Filing basis + entity type final confirmation; switch-month selection; quarterly-review appetite; Option B interest at first year-end.
2. **Ledger-realm API behavior** — does the QBO API hard-reject entity writes against a Ledger company, or accept them unserviceably? **[VERIFIED as unknown]** — needs a developer-doc check or a sandbox probe before Option B is ever costed. Deferred until Option B is live.
3. ~~Does Doug use QBO Payroll today?~~ **Resolved (Doug, 2026-07-02): payroll runs through an external payroll company, not QBO Payroll.** Consequences: the Workforce-SKU/payroll-alongside-Ledger research is moot; Options B and C lose their biggest complication (no payroll migration, no $150 year-end filing fee exposure); payroll reaches the GDX GL through Phase 2's bank-statement matching (the payroll company's debits → R5 user rules → wages/payroll-tax expense accounts), with `PayrollEntry` rows (already external-first: `source ∈ csv_import/gusto/...`) available for per-tech cost detail. In the QBO diff, payroll accounts are classified by however payroll lands in QBO today (payroll-company integration or CPA entries) — confirm which at the go/no-go. **[CPA]** *(2026-10-13, GDXA-902: no QBO diff gates the switch any more, so the classification half of this question is moot. The [CPA] confirmation is left open; it is Doug's to answer.)*
4. Sales-tax filing path (MN) under Options B/C — unresearched; irrelevant under Option A. Carried until Option B is considered.
5. ~~Parallel-run window start~~ — resolved in §3.2 (**[AUDIT-R1]**): the count begins at the first close after the named gate-start preconditions; everything earlier is rehearsal.

## 10. Sources

**[VERIFIED]** (3-0, primary): IRC §446(a)/(b), Treas. Reg. §1.446-1(a)(1)/(a)(4)/(e)(2)(ii)(a), Rev. Proc. 2015-13 (Cornell LII/eCFR/IRS Practice Units); QuickBooks Ledger price/channel/no-downgrade/feature limits + 1099 e-file applies-to (Intuit product pages + FAQ, live 2026-07); QBO cancellation = 1-year read-only, Excel/QBD export (Intuit help, updated 2026-03); ServiceTitan entity-level export, JE-form per payment type only (ServiceTitan docs — refutes the summarized-JE folklore); AICPA Tax Adviser (Apr 2017) accrual-books/cash-return. **[VERIFIED, gap-fill agents 2026-07-02]:** standalone Workforce Payroll pricing/no-accounting-SKU (Intuit pricing bundle page + Firm of the Future 2021); payroll-cancel duties + $150 year-end fee + 1-year payroll data access (Intuit help ~2026-05); payroll-alongside-Ledger (Intuit help applies-to tags + Ledger page footnote — indirect, flagged); AJE flow-back conventions (Intuit QBOA AJE docs; Mahoney CPA 2022; CCH Axcess/BalanceWare TB-software ecosystem). **[CONVERGENT]** (no authoritative standard exists — established by search): parallel-run duration/criteria (Oracle FLEXCUBE impl. guide ch.9; FFIEC D&A booklet 2004 p.27; Eleven/Numeric/Beancount migration guides 2025-26; Microsoft Dynamics cutover guide 2024-25; Umbrex finance-ERP playbook; ISACA CISA doctrine via secondary reproductions); go/no-go sign-off + hard-date + not-during-close (Microsoft, Protelo, practitioner consensus); rollback-as-extended-dual-run (Umbrex; Thoughtworks parallel-run-with-reconciliation).
