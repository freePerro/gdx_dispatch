# ADR-016 — Forward-looking overhead projection

**Status:** Proposed (design only). Converged through two adversarial `/audit` rounds with
Doug on 2026-06-28; nothing built yet. See **Rejected alternatives** — they are the load-bearing
part of this ADR.

## Context

Doug (owner) wants to answer a cash question: **"What recurring monthly expenses must I pay to
keep the doors open — loans, insurance, rent, subscriptions — and what will that look like 3, 6,
and 12 months out as loans pay off and rates renew?"** The headline is the *forward* view: a chart
that **steps down** when a loan finishes and steps up when insurance renews higher.

The app already touches this space, but nothing answers the question:

- `RecurringStream` (`modules/forecasting/models.py`) detects recurring **cash outflows** from the
  QB bank feed (cadence, `term_end_date`, `term_total_occurrences`, status lifecycle). But it is
  detection-shaped: `amount_min`/`amount_max` (a *range*, not an amount), `payee_pattern` (a bank
  token, **no `vendor_id`**, no GL account), and **`term` is almost always NULL** — a detector sees
  a recurring debit; it cannot know a loan has 23 payments left.
- `qb_pnl_monthly` (`models/tenant_models.py`, populated by `modules/quickbooks/pnl.py`) caches
  monthly expense actuals per GL account — but **has no basis column**, defaults to **accrual**, is
  overwritten per-year, and (even on cash basis) excludes loan **principal**, owner draws,
  transfers, and CC payments. It is neither cash nor cash-out.
- `MonthlyBudget` classifies budget *lines* fixed/variable/percent-of-revenue per
  `(year, month, qb_account_id)` — a budgeting tool, backward-looking, no forward term modelling.
- The forecasting service (`modules/forecasting/service.py`) projects **inflows only** (AR +
  scheduled jobs + recurring invoices) over a single near-term window; it never nets outflows and
  does not run 12 months out.

The job-centric `Expense` model is one-off per-job costs — unrelated to standing overhead.

## The decision in one line

Build **one owned, human-curated obligation register** with a month-by-month projection engine —
**not** a fusion of the two read-only caches above. The forward step-down is driven by
owner-entered payoff/renewal dates (the one input no detector can infer); the bank feed is used to
*check completeness*, not to compute the total.

## Decisions

1. **One owned model, `OverheadObligation`, in a durable core/finance domain — NOT inside the
   experimental forecasting module.** Forecasting is experimental-by-design and schema-churning;
   a permanent finance feature must not bind to it. Fields:
   `label`, `category`, `vendor_id` (real FK), `amount`, `cadence`, `start_date`,
   `end_date`/`term_total_occurrences`, `scheduled_changes` (list of `{effective_date, new_amount}`
   for renewals/escalations), `cost_type`, `is_estimate`, `source`
   (`manual` | `seeded_from_stream` | `qb`), `basis = CASH`.

2. **Month-by-month projection engine.** For each month in the horizon, sum the obligations active
   that month (respecting `start_date`, `end_date`/term, and any `scheduled_changes` effective by
   that month). Obligations that end drop out → the chart steps down exactly where the loan
   finishes. Annual/quarterly cadences normalize to the month they actually fall in.

3. **Manual-first entry is the primary workflow, not a fallback.** Only the owner knows a loan's
   payoff date or a policy's renewal increase. The setup form asks for these directly (loan:
   payments-remaining / payoff date; insurance/rent: renewal date + new amount). We do **not**
   pretend the system discovers them.

4. **`RecurringStream` is demoted to an amount *hint*.** "We saw a recurring ~$X debit to COMCAST —
   add it? (you set the end date)." A detected stream can pre-fill amount + payee as a **draft**
   obligation the user confirms into the single canonical list. It is never a completeness source
   and never auto-summed.

5. **Completeness/drift check against the bank feed — not P&L.** Compare the obligation list to the
   **bank-transaction feed** (real cash that left the account, already read by the stream detector)
   and surface *recurring debits not on the list* as a live badge ("3 recurring debits not
   tracked"). This is the honest completeness signal AND the anti-drift signal — it keeps the list
   from becoming a launch-day fossil after month 1.

6. **Cash-basis throughout; honest scope label.** Everything is "what you pay." The full loan
   payment is modelled (what Doug cares about), so a P&L comparison would differ by the principal
   portion — therefore we do **not** reconcile against P&L at all. The page states plainly that it
   projects **overhead (outflow)**, not **runway** — runway needs forward *inflow*, which is out of
   scope here.

## Rejected alternatives (the audits)

These were proposed and killed; recording them so they are not resurrected.

| Rejected | Why it collapses |
| --- | --- |
| **Compose the two caches** — fuse `RecurringStream` + `qb_pnl_monthly`, dedup on a `(vendor + GL account)` reconciliation key | That key exists on **neither** side. `RecurringStream` has `payee_pattern` + bank `account_name` (no `vendor_id`, no GL acct); `qb_pnl_monthly` has GL acct id but no vendor. Dedup is impossible; the only fallback is lossy fuzzy text+amount matching, which the existing `_combined_recurring` already does and self-flags as double-counting. |
| **Sum accrual P&L + cash-basis streams into "net cash / runway"** | Dimensional error — adding accrual (expense when incurred) to cash (when it clears the bank). The forecasting team already hit and rejected this exact mistake (`modules/forecasting/models.py`, ForecastSnapshot design notes). |
| **A reconciliation panel comparing modelled overhead to "QB cash-out"** | `qb_pnl_monthly` is not cash-out: no basis column, defaults accrual, and even cash-basis P&L excludes loan principal / owner draws / transfers. A green panel would mean nothing → false-confidence gauge. Replaced by the **bank-feed** check (Decision 5). |
| **Auto-seed payoff dates from detected streams** | `term_end_date` on observed streams is almost always NULL; the detector cannot know payments-remaining. Auto-seeding produces a flat line that never steps down — the inverse of the feature's purpose. Hence manual-first (Decision 3). |
| **Decorative `cost_type`/`is_estimate` flags with no consumer** | A label is not behaviour. They stay **only if** the engine treats variable costs differently (trailing run-rate, later a seasonal index) from flat fixed costs; otherwise they are dropped. |

## The hard problems

**Gone** (vs. the fusion design): no impossible join key; no accrual+cash addition; no
false-confidence P&L panel; no dependency on the experimental forecasting schema.

**Remain:**

1. **The list is only as complete as the owner makes it.** Mitigated — not solved — by the
   bank-feed drift badge (Decision 5). It surfaces *recurring* misses; a one-off-looking-but-real
   obligation can still be forgotten. This is acknowledged, not hidden.
2. **Variable costs (payroll, fuel) are seasonal in a garage-door business.** v1 projects them as a
   flat run-rate and **labels it as an assumption**. A seasonal index is a later refinement
   (`cost_type = variable` is the hook).
3. **`scheduled_changes` is owner-entered guesswork for future renewals.** Acceptable: the chart is
   a planning aid, and every projected change is attributable to a row the owner can edit.
4. **Overhead ≠ runway.** Deliberately out of scope; the page must not imply financial health. If
   runway is wanted later, it pairs this outflow projection with a forward inflow projection — a
   separate decision.

## Migration plan (slices)

1. **Slice 1 — register + projection.** `OverheadObligation` model + guarded migration; manual
   entry form (incl. loan payoff / renewal inputs); month-by-month engine; 3/6/12-mo chart + by-month
   + by-category tables. Delivers the user-visible goal on manual data alone.
2. **Slice 2 — stream-hint seeding.** Surface detected `RecurringStream` items as draft obligations
   (amount/payee pre-fill, user confirms + sets term).
3. **Slice 3 — bank-feed completeness/drift badge.** Detect recurring debits absent from the list;
   live "untracked recurring debits" signal.
4. **Slice 4 (optional) — variable-cost realism.** Trailing run-rate, then seasonal index for
   `cost_type = variable`.

## Consequences

- One new ORM model + guarded migration in a new core/finance area (per the
  migration-baseline rule).
- A projection service (pure function over obligations + horizon) and a new page/route + nav entry.
- Reuses the existing bank-transaction store and stream detector for hints + completeness; **no**
  new dependency on `qb_pnl_monthly` and **no** coupling to the forecasting module's schema.
- Scope is explicitly outflow-only; runway remains a future, separate decision.
