# Commission as a Plugin — research and proposal

**Status:** **PLAN** — research complete, nothing built. Filed 2026-08-23 after
money-audit finding **M27** turned out to be the smallest of four defects in a
feature that has never once run. The proposal is Doug's: commission does not
belong in core.

Companion PR (2026-08-23) does **not** build any of this. It makes the dead
surface stop pretending — see §5.

## 1. What is actually there today

**Two overlapping commission systems**, with incompatible models, neither wired
end to end.

| | **A — Commissions** | **B — Payroll** |
|---|---|---|
| Screen | `CommissionsView.vue` → `/commissions` | `PayrollView.vue` → `/payroll` |
| Nav | `commissions` key, `nav.admin` | `payroll` key, `payroll.read` |
| **Module granted on prod?** | **NO** — not reachable | **YES** — in the nav |
| Config model | `CommissionRule`: per **role**, `parts_pct`, `labor_pct`, `bonus_per_review` | `TechCommissionRate`: per **tech**, `rate_type` ∈ percent\|flat\|hourly, `rate_value` |
| Output model | `CommissionEntry` (parts/labor/bonus/total, period) | `PayrollEntry` |
| Endpoints | `/rules` CRUD, `/earnings`, `/calculate`, `/summary` — all real | `/summary`, `/tech/{id}`, `/commission-rates` CRUD, `/export` — real |
| Rows on prod | rules **0**, entries **0** | rates **4**, entries **0** |

They disagree about the fundamental shape: A commissions a **role** on
parts/labor split; B commissions a **tech** on revenue, job count, or hours.
Neither is wrong — they are different businesses' rules — which is the point.

## 2. Four defects, measured on prod 2026-08-23

1. **The Payroll screen is entirely non-functional.** All three endpoints it
   calls — `pay-periods`, `pay-stubs`, `run-current-period` — are `ui_compat`
   501 stubs. Every control fails. The empty state told the operator to press
   the button that causes it.
2. **The revenue basis queries a column that does not exist.**
   `_fetch_tech_revenue` selects `j.assigned_tech_id`; the jobs table has
   `assigned_to`. The resulting `OperationalError` was caught and turned into
   an empty dict, so every tech reported **$0.00 revenue and $0.00 commission
   as though calculated**.
3. **The status literal matches nothing.** It filters `j.status = 'completed'`
   (lowercase). This tenant holds `'Complete'` (32 jobs) and `'Completed'`
   (17). Even with defect 2 fixed, the query would return zero rows. The
   codebase is inconsistent here generally: `reports.py:856` matches only
   `'Complete'`, `jobs.py:3376` only `'Completed'`/`'completed'` — neither
   covers all 49.
4. **M27 itself**: the invoice join has no `deleted_at`, void or draft filter,
   so once 2 and 3 are fixed it would immediately over-count. A voided and
   re-issued $8,000 invoice pays a 5% tech $800 instead of $400.

Plus a fifth, in the data rather than the code: all **4** configured rates are
`rate_type='percent', rate_value=0.01`. `calculate_commission` computes
`revenue × (0.01/100)` — **one hundredth of one percent**, $1.00 on a $10,000
job. Almost certainly 1% entered as a fraction, and never noticed because
defect 2 meant the number was never produced.

**Nothing has ever been generated:** `commission_entries` 0, `payroll_entries`
0. No live data depends on any of this.

## 3. Why a plugin

- **The rules are the product's variable, not its constant.** Percent of
  revenue, flat per job, parts-vs-labor split, per-review bonus, tiered by
  volume, split between sold-by and installed-by — every shop differs. Core
  already has two models because two shapes were needed and neither generalised.
- **Nothing to migrate.** Zero entries, zero rules, four rates that are
  probably wrong. This is the cheapest moment this decision will ever be.
- **Precedent exists.** Pricing plugins already carry money-adjacent logic
  (CHI, Midland), the plugin host and storefront are live, and
  `estimate-screen-plugin-pricing-plan` proved a plugin can write into a core
  money surface through a reviewed capture step.
- **Blast radius.** A commission bug pays a person the wrong amount. Keeping
  that logic behind the plugin boundary — declared capabilities, its own
  tables, an explicit write step — is the same argument that put pricing there.

## 4. What the plugin would need

1. **A calculation capability.** The host runs the plugin with a period, a set
   of completed jobs, their invoices and attested hours; the plugin returns
   per-person amounts with a breakdown. Core owns the inputs and the audit
   trail; the plugin owns the rules.
2. **The inputs have to be right first.** A plugin fed the current
   `_fetch_tech_revenue` inherits all four defects. Whoever builds this fixes
   the column, the status vocabulary and the invoice predicate **in the input
   layer**, once, and uses `core/billing_predicates.py` rather than a fourth
   private definition.
3. **The deposit question, still unanswered.** Two prod jobs carry both a
   deposit and a standard invoice and they disagree: on one the deposit is
   exactly half the standard (summing double-counts), on the other they look
   like two halves of one job (summing is right). The commission basis needs an
   explicit rule. `job_billed_exists` already excludes deposits, with a
   documented reason — the likely default, but it is a business decision.
4. **Attested hours only.** Billed labour comes from attested hours; elapsed
   clock time is not evidence. A commission rule keyed on hours inherits that.
5. **An audit trail core keeps.** Who ran it, for what period, what each person
   was credited, and which plugin version computed it. A pay number whose
   derivation cannot be reconstructed is not auditable.
6. **A decision on the two existing screens.** `/commissions` is not granted and
   has never been configured; `/payroll` is granted and entirely 501. Both are
   candidates for removal once the plugin exists — that is the
   `unimplemented-endpoints-decision-list` call for `run-current-period`,
   still untaken.

## 5. What the companion PR did (and did not) do

It did **not** repair commission. It stopped the surface lying:

- `_fetch_tech_revenue` now raises `RevenueBasisUnavailable` instead of
  returning `{}`. All three consumers surface a **503** naming the reason
  rather than rendering a page of $0.00 rows that look calculated.
- `PayrollView` says plainly that payroll runs are not built, disables the
  button that 501s, stops calling three endpoints that cannot answer, and no
  longer instructs the operator to press the failing button. Hours are called
  out as unaffected, because they are.

The tables and detail modal are deliberately left in place: they are the shape
the screen takes when runs exist, and the build-or-remove call has not been
made.

## 5.5 Why this doc has to be found from the money audit

On 2026-08-23 a later pass worked the money audit top-down, reached **M6**, and
started building a uniqueness constraint on `commission_entries` — a fix for a
feature this document had already retired. Doug caught it: *"commissions were
supposed to be dropped, it is something we will build as a plugin later. How
did this get missed from the last time this was worked on and I answered that
question."*

**The mechanism:** the decision was recorded here and on the money audit's
**M27**, but M6 in that same document still read as a live `HIGH` finding with
a "Fix." prescription and no cross-reference. A reader entering from M6 had
nothing telling them to stop. The corpus audit already names this failure —
*"check for a rival plan first; two plans in this repo reached opposite
decisions about the same money path without ever referencing each other"* —
and this was the same shape, one document to the next.

**Fixed by** putting a DO-NOT-BUILD banner on M6 itself, pointing here. If a
further commission finding is ever added to that audit, it needs the same
banner: a decision recorded in one place and not the other is a decision that
will be re-litigated by whoever reads the other one.

**One thing that work did establish, worth keeping:** the natural key for a
commission entry is NOT `(user_id, job_id, period)`. `CommissionRule` is
per-**role** and `CommissionEntry` has no role column, so the same person on
the same job as `tech` (10%) then `lead` (50%) collapses to one row at $500
instead of two totalling $600 — silently, audited as an ordinary update. One
person selling *and* installing a job is the ordinary owner-operator pattern
here, so the plugin's model must carry the role. That is a money rule for
whoever builds it, not something a constraint should decide.

## 6. Traps carried forward

- **Do not "fix" `_fetch_tech_revenue` in place and call commission working.**
  Three of the four defects are in that one query, and repairing them turns a
  silent zero into a confident wrong number until the deposit basis is decided.
- **`j.status` has no canonical vocabulary.** Any consumer matching a single
  literal is wrong for roughly half the data. Match case-insensitively against
  both, or fix the vocabulary — but decide, do not copy the next literal along.
- **`POST /api/commissions/rules` has a button and no unique index.** It is a
  check-then-insert on `commission_rules.role`, and both `set_rules` and
  `calculate_commission` then call `scalar_one_or_none()` on that role — so a
  duplicate makes both raise. Unlike `/calculate`, this one is reachable from
  `CommissionsView.vue`. Left alone deliberately: it is commission, and
  commission is leaving core.

- **The 0.01 rates.** Whatever consumes them should refuse or flag a percent
  rate below some floor rather than silently paying a hundredth of a percent.
