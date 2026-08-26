# Scope — MN sales/use tax by jurisdiction, to produce the actual return

**Status:** scope only, nothing built. Written 2026-08-05, **revised after adversarial audit**.
**Driver:** Doug — "our tracking of sales tax is incorrect, it should be MN minimum + local,
because the return asks for each of them."

> **Revision note.** The first draft's central premise — that no address data exists, so
> jurisdiction must be operator-chosen — was **false**, and two independent audits killed it.
> It also had the phases backwards. What follows is the corrected plan. §10 records what was
> wrong, because the errors are more instructive than the plan.

---

## 1. What the return actually requires

From the MN DOR *Sales and Use Tax e-Services Reference Guide* (January 2026):

| Return element | What you enter |
| --- | --- |
| Gross Receipts | All MN sales, taxable **and exempt**, excluding sales tax |
| **Line 100** General Rate Sales Tax | "the total taxable sales… **enter the taxable sales amount and not the tax collected**" |
| **Line 200** Use Tax Purchases | Total purchases subject to MN use tax |
| **Line 210** Variable Rate Purchases | Out-of-state purchases where another state's tax was paid. Rare. |
| **Each local tax** | Its **own line**, chosen from the Tax Type drop-down, persisting on later returns |

The unit of reporting is the **taxable base per tax type**, not tax dollars. MN is
**destination-based**: 6.875% state + local options, 322 local jurisdictions.

## 2. Current state (verified in code + prod, 2026-08-05)

- `tax_config` holds **one flat `default_rate`** (prod `0.0738`) + `tax_labor`.
  `resolve_rate(db, customer_id)` returns exempt→0, else that number.
- Invoices persist `tax_rate` + `tax_amount`. **No taxable base is stored**, and no jurisdiction.
- `tax_jurisdictions` exists with Admin CRUD (`routers/admin_settings.py:116-225`) — 0 rows, read
  by nothing.
- The sales-tax report sums `invoices.tax_amount` and splits GDX/QuickBooks by invoice-number
  prefix (`routers/reports.py:419-446`).

### 2.1 Address data EXISTS (first draft got this backwards)

| Source | Prod state |
| --- | --- |
| `customers.address` | **225 plaintext**, 11 Fernet ciphertext; **185 have a parseable 5-digit ZIP** |
| `estimates.jobsite_address` | plaintext `Text`, 11–12 populated; already LIKE-searched at `routers/search.py:152` |
| `customer_locations` (city/state/zip/**lat**/**lng**) | 0 rows — but `Job.location_id` FK already exists (`tenant_models.py:343`) |

`EncryptedString` ran with `_FERNET = None` for months, so most writes landed as plaintext — the
type's own docstring documents this "NONE era" (`core/pii.py:56-80`). So ZIP extraction is a
**one-afternoon backfill**, not a multi-week project. The 11 ciphertext rows need an app-layer
decrypt.

**Corollary:** jurisdiction should be **derived from ZIP**, not chosen by an operator.

### 2.2 A confound worth naming

Historical invoices cluster hard: **133 of 139** valid-rate invoices at ~7.4%, 3 at ~6.9%. It is
tempting to read that as "one locality." **It is circular** — they cluster because the app only
ever had one rate to apply. The honest evidence of geographic spread is **41 distinct customer
ZIPs**. Neither the historical rates nor a single tenant default can be trusted to tell us the
right rate for a job; only a ZIP→rate lookup can.

## 3. Goal

For a filing period, produce:

```text
Gross receipts (taxable + exempt)
Line 100  state general rate ......... taxable base
Line 200  use tax purchases .......... base (material cost consumed in contracts)
<local N> sales tax ................... taxable base sourced to N
<local N> use tax ..................... use base sourced to N
```

Non-goal: filing, or e-Services integration.

## 4. Phase 0 — ANSWER THE COMPLIANCE QUESTION FIRST (blocking)

Both audits landed this and they are right: **the phase order was inverted.**

MN DOR: garage door install and repair are construction contracts — **no customer sales tax**;
the contractor owes **use tax on materials**. GDX has charged **$33,158 of sales tax across 214
invoices**. If the DOR reading is correct, then:

- the sales-side reporting is precision machinery for a number that should trend to **zero**, and
- the real liability is **Line 200 use tax**, which the original plan deferred to Phase 3.

Nothing else should be built until the accountant answers:

1. Construction-contract treatment for installs/repairs — confirm or refute.
2. **A or B**: pay supplier sales tax at purchase (A) vs keep buying exempt and self-assess use
   tax on material cost (B). Doug currently buys exempt, which makes B the status quo and creates
   an unaccrued use-tax liability.
3. Which local jurisdictions is GDX registered for, and their DOR filing codes? (A phone call.
   No schema can be seeded without it.)
4. Historical exposure: $33,158 collected on transactions that may not have been taxable — those
   dollars must be remitted regardless, and customers may claim refunds.

**Answers reshape everything below.** Under A, the use-tax phase disappears. Under
construction-contract treatment, Line 100 shrinks toward retail-only.

## 5. Phase 1 — Record the tax facts the return needs

The gap is not only jurisdiction. Today an invoice cannot answer **why** its tax is what it is.

`resolve_rate` returns 0 for exempt customers, and `create_invoice` only enters rate-mode when
`candidate > 0` (`routers/invoices.py:834-838`) — so **exempt sales, pre-S110 legacy invoices,
and construction contracts all persist as `tax_rate IS NULL`**, indistinguishable. Those are three
different return treatments (Gross Receipts only / unknown / use tax) collapsed into one null.

Per invoice, persist:

| Field | Why |
| --- | --- |
| `tax_treatment` enum — `retail` \| `construction_contract` \| `exempt` \| `legacy_unknown` | The axis the return keys on. Without it no line can be assembled. |
| `taxable_base` (Numeric) | The return wants the base. It must be **stored at issue**, not re-derived: 20 imported invoices are `totals_locked` with lines summing ~2× truth, 282 have no lines, and 308/333 have `tax_rate IS NULL`. |
| `tax_jurisdiction_id` | Derived from ZIP; nullable for historical rows. |

```text
tax_jurisdiction        id, name, zip_prefix//match, is_default, effective_from/to
tax_jurisdiction_part   jurisdiction_id, kind(state|local), label,
                        filing_code, rate numeric(9,6), effective_from/to
```

`filing_code` is the DOR Tax Type — without it the report cannot name the line to pick in
e-Services.

**Landmine:** `TaxJurisdictionIn` validates `rate: float = Field(ge=0, le=100)` — a **percent** —
into `TaxJurisdiction.rate Numeric(9,6)` that every consumer reads as a **fraction**
(`admin_settings.py:121` vs `tenant_models.py:2998`). Seed "6.875" and you bill 687.5%. Inert
today only because nothing reads the table. Fix before wiring it.

**Do not seed the local rate by subtracting 6.875% from `0.0738`.** That yields 0.505% where the
true local is 0.500% — small, but wrong, and the reconciliation in §6 would not catch it because
it would be checking the figure against itself. Take the real rates from DOR (Phase 0 Q3).

## 6. Phase 2 — The return report

`GET /api/reports/sales-tax-return?start=&end=`, emitting §3, with drill-down.

Requirements the first draft missed, all from the audits:

- **Gross receipts** must include **exempt** sales. Today 113 invoices / **$239,393 (31% of base)**
  carry zero tax while `tax_exemption` has 0 rows. Read `total`, not `total_amount` (NULL on
  333/333 rows — see M8 in the money audit).
- **Net credit memos and supersessions.** Phase 1 must therefore add a tax component to
  `invoice_adjustments`, or Phase 2 cannot do this (the first draft required the netting without
  providing the field).
- **Declare the basis.** The current report is a hybrid — accrual by `invoice_date` but
  `tax_collected` off `paid_at` (`reports.py:436`) — which is neither MN basis.
- **Period lock + amended returns.** A void or soft-delete after filing silently mutates a filed
  period. 7 soft-deleted and 1 void invoice already exist.
- **A real reconciliation, not a tautology.** "Σ components = Σ `invoices.tax_amount`" checks the
  report against the column it just read and always passes. It cannot see the 66 below-floor
  invoices. Reconcile instead against **independent** facts: Σ taxable base × jurisdiction rate,
  and Σ payments/adjustments — and fail loudly on divergence.

## 7. Phase 3 — Use tax accrual (likely the real deliverable)

Material cost consumed in construction contracts → Line 200 + local use tax, sourced from vendor
bills (`modules/vendor_invoices`), which capture line-level cost and need a
"consumed in contract vs resold" disposition.

Reality check on the data: **10 vendor bills / 33 lines** against ~$762K of billed work. The cost
side is barely populated, so this phase is as much a data-capture problem as a reporting one.
Gated on Phase 0 A/B.

## 8. Phase 4 — ZIP→jurisdiction backfill

Regex the trailing ZIP5 from `customers.address` (185 available), decrypt the 11 ciphertext rows
in the app layer, fall back to `estimates.jobsite_address`, import the DOR ZIP+4 spreadsheet
(quarterly, ~34.6k rows), map ZIP→jurisdiction. Now days, not weeks (§2.1).

Historical invoices still get **no** retroactive jurisdiction: their `tax_amount` is a fact, their
base often is not (see §5). They report under an explicit "unassigned" bucket.

## 9. Out of scope

Filing/e-Services integration; multi-state; Avalara/TaxJar; special local taxes (liquor, lodging,
admissions); repairing the 66 below-floor invoices.

## 10. What the first draft got wrong

Kept deliberately — the errors are the useful part.

1. **"No address data exists; sourcing is dead on arrival."** False. 225 plaintext addresses, 185
   with parseable ZIPs, plus a documented `jobsite_address` search path and a `Job.location_id` FK.
   The premise was never tested against the data. It made a data-entry gap look like an
   architectural constraint and pushed the correct design (derived) into a deferred phase.
2. **Phase order inverted.** Built sales-side precision first for a number MN says should be ~zero,
   while deferring the use tax that is probably the actual liability.
3. **Tautological reconciliation.** Checked the report against the same column it read.
4. **Missed the "why is tax zero" axis** — exempt, legacy, and construction contract all collapse
   to `tax_rate IS NULL`, which is the axis the return actually keys on.
5. **Gross receipts unbuildable** — the report shape sums `tax_amount`, so zero-tax and exempt
   sales are invisible, and gross receipts must include them.
6. **Seed decomposition** by subtracting the state rate from a truncated total.

Audit corrections I checked and did **not** accept:

- One auditor put the seed error at "local 0.925%, ~85% overstated". The arithmetic is
  `0.0738 − 0.06875 = 0.00505` → **0.505% vs 0.500%**, a 1% relative error. Real, but two orders
  of magnitude smaller than claimed.
- "97 distinct implied rates" counts penny-rounding noise as distinct rates; there are **two**
  clusters (~7.4% and ~6.9%). But see §2.2 — the clustering is circular evidence and does not
  rescue the single-default design either.
- "66 vs 84 below-floor invoices" is a filter difference, not a contradiction: 66 excludes
  QuickBooks-imported rows, 84 includes them.

## 11. Estimate

Phase 0 is a conversation, and it gates the rest. Phase 1 ~3–4 days (bigger than the first
draft's estimate — it now stores treatment + base, not just a rate). Phase 2 ~3 days. Phase 4
~1–2 days. Phase 3 ~3–4 days if B.

**Recommendation: build nothing until Phase 0 is answered.** The most likely outcome is that
Phases 1, 4 and 3 matter and Phase 2's sales-side lines are mostly zeros.
