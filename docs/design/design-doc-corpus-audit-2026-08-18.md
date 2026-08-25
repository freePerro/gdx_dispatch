# Design-doc corpus audit — what the written record says vs. what shipped

**Status:** **AUDIT — every §4 defect it named is now fixed; §§1-3 and 5-6
still stand as the record.** Run 2026-08-18 against `main` at `ec2e14a`, prod
v1.68.2; §4 re-verified 2026-08-21 against `main` at `2a80480`, prod v1.73.0;
§4 re-verified again 2026-08-25 against `main`, each item checked in code
rather than taken from a tracker:

- `routers/admin_plugins.py` now audits its mutations (#365).
- **M4 currency — fixed.** `payments.py:216` and `:407` pass the module-level
  `CURRENCY` constant to Stripe. The `currency` field survives on the request
  bodies but is documented advisory and ignored (`:49-52`).
- **Money regression net — running.** `tests/test_zz_money_correctness_probe.py`
  carries no `health` marker, so `pytest.ini`'s
  `-m "not e2e and not load and not health"` does not exclude it.
- **Principal/idempotency stamp — registered.** `app.py:1402-1403` adds
  `PrincipalStampMiddleware`.
- **Mobile clock orphans — fixed 2026-08-25.** See §4.

Its header-repair recommendation was carried out on 2026-08-21 — a fresh sweep
found nine docs still misreporting shipped work, all now corrected.
**Added 2026-08-23:** §6 gained a non-doc hygiene entry — 25 stale git
worktrees (4.8 GB), 24 prunable, one holding unlanded source.
**Scope:** all 52 documents in `docs/design/` (14,702 lines), each verified
against git history, merged PRs, release tags, and current code.
**Repo hygiene:** this document names no customers, vendors, dollar amounts, or
internal domains. Keep it that way.

## Why this exists

Twice in one session, work was reported as "awaiting a decision" that the
design docs themselves recorded as decided days earlier. The summary being read
was stale; the source docs were correct. That prompted the question of how far
the problem went — whether the corpus as a whole could be trusted to say what
is built and what is not.

It cannot. But the failure is narrower and more fixable than "the docs are bad."

## Method

Seven parallel audits, six covering the corpus in batches of eight or nine and
one doing cross-document analysis. Each doc's claimed status was compared to a
true state established from a merged PR number, a commit sha, a release tag, or
a `file:line` read of current code. Claims were not accepted from the doc.

**Verified directly by hand after the sweep:** the four live defects in §4.
**Verified by agents with cited evidence, spot-checked:** the per-doc verdicts
and cross-document conflicts.
**Not verified at all:** anything requiring a production database or live
service read — the SOC 2 findings about `SENTRY_DSN` and MFA enrollment, the
phantom deposit rows, whether the QB backfill ran, and every prod row count
quoted in any plan.

## 1. The headline: planning converts, recording does not

| Outcome | Docs |
|---|---|
| Shipped and released | 26 |
| Partially built | 13 |
| Plan only, nothing built | 13 |

**Three quarters of the plans became code.** Whatever else is wrong here, the
planning process is not ceremony — it produces shipped features at a high rate.

The recording is the part that fails:

| Status verdict | Docs |
|---|---|
| MATCH — stated status is accurate | 15 |
| STALE — was true once, reality moved on | 23 |
| WRONG — actively misleading | 14 |

Fourteen documents would send a reader to build something that already exists.
Seven have no status line at all, including the two largest documents in the
corpus — the ones most likely to be consulted for "is this fixed yet?"

**The failure is one-directional: no doc overclaims.** Every error is a plan
that shipped and never had its header updated. Several were edited *during*
their own build — implementation notes, audit rounds, and follow-up sections
appended — while the status line at the top was left untouched. The one line a
reader trusts is the one line nobody updates.

## 2. Docs contradict each other

Six documents plan against `routers/estimates.py`, eight against
`modules/proposals/router.py`. Across the whole estimates cluster there is
exactly **one** cross-reference between any two plans. The consequences are
concrete.

**Two deposit plans have incompatible premises on the same money path.**
`deposit-cash-check-capture-plan.md:9` builds five features on a deposit
invoice "minted at estimate acceptance."
`deposit-ask-online-pay-plan.md:34-35`, locked six days later, removes exactly
that row: no invoice until the customer initiates payment. Neither cites the
other; both were marked "not built" while both had in fact shipped. They also
read the same production rows in opposite directions — one treats voided unpaid
deposits as possible over-billing, the other as phantoms to void deliberately.

**`payment_date` carries two definitions.**
`payment-date-recording-plan.md:14-16` (BUILT) makes it the bank deposit date.
`deposit-cash-check-capture-plan.md:107-110` argues it must be when money
changed hands in the field. A check taken on the 3rd and deposited on the 8th
gets two different canonical answers, and month-end reporting keys off it.

**One audit table was stale the day it was written.**
`plugin-storefront-plan.md:61` describes the plugin permission vocabulary as a
closed set of four. A same-day merge made it five by adding `email`
(`plugin_api/manifest.py:32`). The plan's pre-install permission-chip UI — a
decided item — is specced against the wrong set.

**One audit prescribes conforming to code that was already dead.**
`backend-vue-contract-gaps-2026-07-24.md:216-219` tells the UI to match a
router that had been deliberately unwired seven weeks earlier; following it
would resurrect a removed table. Nothing marks it superseded.

**A shipped money rule now exists twice.** Two estimate plans written a day
apart, neither citing the other, produced duplicate implementations of the same
tier-total rule at `modules/proposals/service.py:208` and
`modules/proposals/router.py:487`. No live bug — but two copies of a money rule
that must now be kept in sync.

**Latent, both plans still unbuilt:** `estimate-option-groups-plan.md:164`
sweeps siblings in `{draft, sent, rejected}` to `not_selected`, while
`estimate-rejection-visibility-plan.md:159` defines `rejected` as a *bounced
email the customer never saw*. A bounced sibling would be shown a "you chose a
different option" banner for an estimate that never arrived, and would fall
outside the re-send heal. Reconcile before either is built.

## 3. Half-shipped work is recorded as done

The pattern that produces the most dangerous entries: a multi-part fix lands
partially, and the record says "fixed."

- **M4 (money audit).** The doc lists currency-locking as fixed. The
  `core/payments.py` half shipped; the router half did not — see §4.
- **M22 and M23** were fixed *later*, in v1.58.0, and the doc still lists them
  as open. Stale in the safe direction, but it means the audit's own scoreboard
  can't be trusted in either direction.
- **`tech-mobile-workflow-plan.md`** shipped in v1.17.0 except one leg: the
  per-job clock. Its endpoints exist with zero UI callers.
- **`vendor-invoice-followups-plan.md`** presents four workstreams as awaiting
  a go-ahead. All four shipped five weeks ago.

## 4. Live defects surfaced by the sweep

Not new work — old work that was reported as finished. Each verified by hand
against current code.

**M4 is half-shipped on a mounted router.** `routers/payments.py:48` and `:54`
still accept a caller-supplied `currency`, forwarded to Stripe at `:158`. The
router is imported and mounted at `app.py:108`. The money audit's own text says
to drop `currency` from *both* request models.

**The money regression net is switched off.**
`tests/test_zz_money_correctness_probe.py:54` is `pytestmark =
pytest.mark.health`, and `pytest.ini:15` excludes `health` from every run. Ten
probes guarding the money invariants, all passing, none running. The audit
explicitly said to drop the marker once they passed.

**The idempotency middleware has never executed in production.**
`core/middleware/idempotency.py:69-71` returns early when
`request.state.principal` is None, and the only assignment to that attribute
anywhere is in `tests/test_idempotency_middleware.py:72`. Every double-submit
guard depending on it is inert. `routers/invoices.py:2481-2484` already
concedes this in a comment.

**`routers/admin_plugins.py` has zero `log_audit_event()` calls** while its
docstrings at `:6` and `:73` claim "Owner-only + audited." Plugin install is
code execution; installs, restarts, and consent grants leave no trail. This is
an open invariant #1 violation. `plugin-storefront-plan.md` PR 0 scopes the fix.

**Orphan endpoints:** ~~`POST /api/mobile/jobs/{id}/clock-in` and `/clock-out`
(`routers/mobile.py:3104,3181`) have no frontend caller — the class CLAUDE.md
forbids. Separately, `_close_open_time_entry` (`mobile.py:694`) closes a span
without setting `hourly_rate`.~~ **FIXED 2026-08-25.** Both endpoints now have a
UI caller: the job screen carries both clocks with a state-reflecting toggle
(`tech-mobile-workflow-plan.md` §"Clock in/out"). The `_close_open_time_entry`
half was worse than "no `hourly_rate`" — it wrote wall-clock elapsed into
`duration_minutes`, which `payroll.py:248` sums into `hours_worked` with no rate
filter, so wiring a Stop button would have paid out unattested time. A manual
stop now banks zero payable minutes and records the span in `notes`.

**Dated risk:** `modules/quickbooks/pnl.py:130` parses QBO report columns
positionally. Two GL docs name the QBO Reports v2 cutover as 2026-08-31 and it
is unmitigated. *The code is verified; the cutover date is taken from the docs
and has not been confirmed against Intuit — do that before planning around it.*

## 5. What the corpus does well — do not delete these

Every doc audited, including all 26 that fully shipped, carries durable content
that exists nowhere else: rejected alternatives and why, adversarial-audit
findings that changed the design, and the reasoning behind schema choices.
Examples worth preserving verbatim:

- Why a unique index on `documents.content_hash` was rejected (shared across
  all document types; the statement module deliberately allows
  soft-delete-then-reupload).
- Why the GL engine posts to account *roles*, never to numbers.
- Why the bank statement — not the feed — is the reconcile evidence.
- Why `created_by` must never enter `job_belongs_to_user` (payroll-evidence
  corruption).
- Why a tech tapping both ends of a job clock *is* attestation, and therefore
  does not violate the invented-hours rule. *(Kept as the record of the
  argument. 2026-08-25 note: the reasoning is sound but does not survive this
  schema — `duration_minutes` is payroll hours, so there is no way to record an
  attested span without paying it. The decision went the other way; see
  `tech-mobile-workflow-plan.md` §167. Preserved because the rejected
  alternative is the part code cannot recover.)*

Roughly 8% of the corpus sits under audit-response and trap headings. That is
not waste — it is where real defects were caught before shipping. **Staleness is
never a reason to delete a doc, only to correct its header.**

## 6. Hygiene risk

`qb-import-paid-status-repair-plan.md` contains real customer names, customer
UUIDs, and dollar amounts, in a public repository. It is currently **untracked**
and its own header says to scrub before committing. Keep it untracked or scrub
it. This is also a reason not to bulk-commit the untracked plan-doc batch
without reading each one first.

Thirteen docs are untracked and have no git history at all.

### Worktree pile — 25 trees, 4.8 GB, measured 2026-08-23

Not a doc-corpus finding. Recorded here because this is the repo's only
standing hygiene list, and the pile has now outlived two audits without an
owner.

`git worktree list` shows **25 secondary worktrees totalling 4.8 GB**, the bulk
of it duplicated `node_modules`. Every one of the 25 branches is already an
ancestor of `main`: the work landed and the tree was left behind.

- **Safe to remove: 24.** Seventeen are clean and come out with a plain
  `git worktree remove`. Seven more are dirty only in untracked noise —
  `node_modules`, walk screenshots, `uv.lock`, `uploads/` — so `remove`
  refuses them and `--force` is the right answer there, losing nothing.
  (Probe-verified 2026-08-23: one untracked file is enough to make `remove`
  refuse, so "dirty" here does not imply "holds work".)
- **Salvage first: 1.** `.wt-equipment-verb` is the only tree holding unlanded
  source: a modified `frontend/src/views/EquipmentView.vue` plus an untracked
  `tests/test_equipment_verb_contract.py`. Land or discard it deliberately —
  `git worktree remove` will refuse that tree, and `--force` would destroy
  both files. The test has never run in the default gate.

Supersedes the 2026-08-19 figure of 22 of 24 prunable. **Not re-verified:**
whether the equipment-verb change is still wanted, and whether any of the 4.8 GB
is shared via hardlinks rather than duplicated.

## 7. Right-sizing

Median doc is 197 lines; the two largest are 1,455 and 1,199 and together are
18% of the corpus. Neither has a status line.

`pdf-line-item-column-toggles-plan.md` is the model at 145 lines: it opens with
"what already exists (do not rebuild)" and establishes that half the ask needs
no code at all. The 500-line plans lack that section.

## 8. Recommended actions, ranked

1. **Flip the money probe marker** so the regression net runs. One line.
2. **Finish M4** — drop `currency` from both request models in
   `routers/payments.py`. Small, and it closes a claimed-fixed money hole.
3. **Reconcile the deposit conflict** (§2) before either plan is built on.
4. **Storefront PR 0** — the audit-trail repair; closes a live invariant #1
   violation.
5. **Confirm the QBO Reports v2 cutover date**, then decide on `pnl.py`.
6. **Adopt the status-line rule** (see `CLAUDE.md`, "The written record") and
   generate the doc index rather than maintaining one by hand.
7. **Correct the 14 WRONG headers**, starting with the four that would send
   someone to rebuild shipped work: `vendor-invoice-followups`,
   `tech-weekly-timesheet`, `tech-mobile-workflow`, `billing-capture-hardening`.

## Appendix — per-doc verdicts

`MATCH` = stated status accurate · `STALE` = was true once · `WRONG` = would
mislead a reader into rebuilding shipped work.

| Doc | True state | Verdict |
|---|---|---|
| backend-vue-contract-gaps-2026-07-24 | partial, 11 tiers v1.25.0 | WRONG |
| bank-statement-import-plan | shipped v1.36.0 | STALE |
| billing-capture-hardening-plan | shipped v1.10.0 | WRONG |
| books-convergence-plan | partial, Track 1 v1.59.0 | STALE |
| call-capture-followup-plan | shipped v1.11.0 | STALE |
| ci-test-suite-assessment-2026-08-03 | partial, items 3+4 v1.38.0 | STALE |
| closeout-parts-autopricing-plan | plan only | MATCH |
| comment-accuracy-audit-2026-08-12 | shipped v1.52.0 | MATCH |
| dashboard-activity-attribution-plan | partial, v1.30.0 | WRONG |
| deposit-ask-online-pay-plan | shipped v1.67.0 | WRONG |
| deposit-cash-check-capture-plan | shipped v1.53.0 | WRONG |
| door-listings-website-plan | partial, v1.31.0, Ph5 open | WRONG |
| email-inbox-improvement-plan | shipped v1.26.0 | STALE |
| email-overhaul-tech-debt | plan only, 12 rows open | MATCH |
| email-readability-and-delivery-plan | shipped v1.68.0 | STALE |
| estimate-link-door-photos-plan | shipped v1.67.0 | STALE |
| estimate-option-groups-plan | plan only | MATCH |
| estimate-rejection-visibility-plan | plan only | MATCH |
| estimate-screen-plugin-pricing-plan | shipped v1.54.0 / v1.55.0 | STALE |
| frontend-contract-gaps-2026-08-12 | partial, C6 fixed at birth commit | WRONG |
| gl-phase1-core-ledger | shipped v1.16.0, dark behind flag | STALE |
| gl-phase1-implementation-plan | shipped v1.16.0 | MATCH |
| gl-phase2-reconciliation | partial ~40%, schema superseded | STALE |
| gl-phase3-trust-switch | plan only | MATCH |
| hostinger-guided-onboarding-plan | partial, packaging v1.65.0 | STALE |
| invoicing-gap-fixes-2026-08-08 | shipped v1.46.0 | MATCH |
| job-closeout-billing-visibility-plan | shipped v1.32.0+ | STALE |
| job-photos-office-visibility-and-invoice-attach-plan | shipped v1.53.0 | WRONG |
| jobsite-address-visibility-plan | partial, PR1 merged unreleased | WRONG |
| mn-tax-jurisdiction-reporting-scope | plan only | MATCH |
| mobile-all-platforms-plan | shipped v1.51.0 | STALE |
| mobile-customer-job-create-fix-plan | shipped v1.23.0 | STALE |
| money-audit-2026-08-04 | partial, ~15 of 39 fixed | STALE |
| n8n-automation-plan | shipped v1.66.0 | WRONG |
| payment-date-recording-plan | shipped, PR #249 | MATCH |
| pdf-line-item-column-toggles-plan | plan only | MATCH |
| plugin-authorization-and-mobile-estimates-plan | shipped v1.50.0 / v1.50.1 | STALE |
| plugin-storefront-plan | plan only | MATCH |
| public-estimate-approval-plan | shipped v1.57.0 | STALE |
| qb-import-paid-status-repair-plan | partial v1.35.0 — **contains PII** | STALE |
| simplefin-bank-feed-plan | shipped v1.56.0 | STALE |
| sms-caller-identity-plan | plan only | MATCH |
| soc2-readiness-gap-analysis | plan only, findings still true | MATCH |
| tech-mobile-workflow-plan | partial v1.17.0, job clock never built | WRONG (job clock built 2026-08-25) |
| tech-weekly-timesheet-plan | shipped v1.54.0 | WRONG |
| testing-gaps-2026-07-24 | plan only, mostly still true | STALE |
| tier10-quickbooks-background-jobs-2026-07-24 | shipped v1.25.0 | STALE |
| tier-line-items-and-accept-fix-plan | shipped v1.58.0 | STALE |
| unimplemented-endpoints-decision-list | plan only, 17/17 still true | MATCH |
| vendor-invoice-followups-plan | shipped v1.16.0 / v1.22.0 | WRONG |
| vendor-invoice-intake-plan | partial, Ph1+2 shipped | WRONG |
| vendor-payment-visibility-plan | plan only, mechanism invalidated | STALE |
