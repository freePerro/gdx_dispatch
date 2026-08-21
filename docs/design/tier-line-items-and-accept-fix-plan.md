# Tier Accept Fix + Line-Built Tiers — Plan

**Date:** 2026-08-14 · **Status:** RELEASED v1.58.0 — both phases on main (verified 2026-08-21). Phase A: `proposals/service.py:22/31` `tier_contract_lines` + `tier_contract_subtotal`, tier-aware recalc, `invoices.py:1329` accepted-tier copy/synthesize. Phase B: `proposals/models.py:91 ProposalTierLine`, `router.py:111/121/126` tier-line CRUD, `service.py:217 _resync_tier_price`, `EstimateView.vue:609-620` per-tier grid with the price read-only once lines exist, `ProposalPublicView.vue:77` included-lines list. Items 7-8 and §5 are declared out of scope and stay that way.
**Branch:** `feat/tier-lines-accept-fix` off `main` (v1.57.0 tip)
**Doug's ask:** "fix the tier accept" + "tiers should be able to be built like the estimates that we have now"

## 1. Problem (verified in code, sharpened by audit)

When a good/better/best tier is accepted, the accepted price lives nowhere in
billing — `accepted_tier_id` is a pointer:

- Staff `accept_tier` (`proposals/service.py:154`) and the public accept set
  the pointer only. **Mobile truck accept already does it right**:
  `estimate.total = _money(tier.total_price)` (`mobile_quoting.py:607`).
- Invoice-from-estimate (`invoices.py:1073`) copies ALL estimate lines and
  `_recalculate_invoice` derives totals from those lines. For office-built
  tiers that bills the base lines ($500 instead of $8,000). **For
  mobile-built proposals it is WORSE: the mobile builder writes every
  tier's lines into `estimate_lines` untagged (`mobile_quoting.py:444-460`,
  sort_order bands 100/200/300), so the invoice bills Good+Better+Best
  SUMMED.**
- `est.total` on an open mobile proposal = the HIGHEST tier
  (`mobile_quoting.py:476-479`) — there is no "Σ base lines" invariant to
  preserve.
- Tiers are flat rows; they cannot be built from line items in the office
  editor (EstimateView.vue:553+ is three cards with a manual Price input).

## 2. Architecture decision (the audit's §5 drives this)

**Tier lines live in their own table, `proposal_tier_lines` — NOT in
`estimate_lines`.** The audit enumerated ~12 tier-blind consumers of
`EstimateLine.estimate_id` (PDF payload, /send email lines, portal detail,
public GET flat lines, install sheet, labor variance, appointments, door
specs, office grid serializer, job conversion, `_recalculate_total`,
duplicate). A `tier_id` column would require every one to gain a filter or
silently mis-render; prod data is also pre-poisoned (mobile's untagged
bands). A separate table has ZERO blast radius: every existing consumer
keeps meaning "the estimate's own lines", `_recalculate_total`
(`estimates.py:249`) keeps summing exactly what it sums today, and tier
content is only ever read by code that asks for it.

**Consistency rule (audit §3):** the accepted page and the invoice must
compute from the SAME base by construction:

- Accepted-tier subtotal = Σ accepted tier's `proposal_tier_lines` when any
  exist, else `tier.total_price` (helper `tier_contract_subtotal`).
- `compute_estimate_totals` for an accepted-tier estimate feeds its
  labor-exclusion loop the accepted TIER's lines only (never base lines,
  never other tiers) — flat tier ⇒ no exclusion ⇒ fully taxable.
- The invoice copy uses the same lines (or one synthesized package line for
  a flat tier, category-less ⇒ taxable — matching the totals side exactly).
- `est.discount` stays applied on BOTH sides (the invoice path keeps its
  existing discount materialization for tier accepts too). No skip.

## 3. Build

### Phase A — the accepted tier price lands in billing (fixes flat AND mobile)

1. **Helpers** (`modules/proposals/service.py`): `tier_contract_lines(db,
   tier)` → the tier's own lines; `tier_contract_subtotal(db, tier)` →
   Σ lines else `total_price`, quantized like `_money`.
2. **Accept persists the price**: staff `accept_tier` and the public accept
   set `estimate.total = tier_contract_subtotal(...)` + `updated_at`
   (mobile already does; unchanged).
3. **`_recalculate_total` becomes tier-aware** (`estimates.py:249` — the
   audit's §6.1 clobber): when `estimate.accepted_tier_id` is set, it
   re-derives from the tier helper instead of Σ estimate_lines, so a
   post-accept line edit can't silently revert the contract price.
4. **`compute_estimate_totals`**: accepted-tier estimates get their
   labor-exclusion base from `tier_contract_lines` (explicit branch BEFORE
   the `estimate.lines` relationship shortcut in `_load_lines`,
   `totals.py:68-85`).
5. **Invoice-from-estimate** (`invoices.py:1073`): `accepted_tier_id`
   resolves → copy the tier's lines (taxable per the existing labor rule);
   flat tier → synthesize ONE line `"{Label} package — {description}"`,
   qty 1, unit_price = tier.total_price, taxable=True-under-labor-rule.
   Base estimate lines are NOT copied on the tier path (they are either
   informational office scope or mobile's 3-band mess — and copying them is
   today's summed-bill bug). Discount materialization RUNS as today.
6. **Job conversion** (`_create_job_from_estimate`): accepted tier →
   parts-needed rows from tier lines (else one package row named for the
   tier); estimate lines are NOT copied on the tier path (same reason as
   the invoice — on mobile-built proposals they are three tiers' worth).
   Scope detail remains on the linked estimate. Non-tier estimates
   unchanged.
7. **Tier-line lock**: tier lines follow the existing accepted-lock rule
   (tiers are already locked once accepted — `test_proposals.py:260`); the
   new line endpoints 409 on accepted/declined estimates.

### Phase B — tiers built from line items

1. **New table, NO migration** (built as shipped — the plan's original
   "Migration 065" was corrected to the repo convention): NEW ORM tables
   ship via the entrypoint's `create_orm_tables()` at boot (see migration
   064's own docstring for the precedent); the `ProposalTierLine` model on
   TenantBase also covers the SQLite test harness via `create_all`.
2. **CRUD** (proposals router, staff auth): POST/PATCH/DELETE
   `/api/estimates/{id}/proposal-tiers/{tier_id}/lines[/{line_id}]`,
   tier-belongs-to-estimate validation, accepted-lock gate, and a single
   `_resync_tier_price` choke called by all three writes:
   `tier.total_price = Σ line_total` whenever the tier HAS lines (a tier
   with no lines keeps its manual price — today's behavior). Audit-logged
   like tier writes. `delete_proposal_tier` cascades the tier's lines.
3. **Duplicate estimate** (`estimates.py:2276+`, audit §6.3): tier cloning
   copies each tier's LINES with it; new tiers keep synced prices.
4. **Serializers**: staff `get_proposal` + the public GET tiers gain
   `lines` (+`lines_subtotal`); public projection is public-safe and
   strips prices under `hide_line_prices` exactly like estimate lines.
   The public flat `lines` array is UNCHANGED (base lines only — no double
   render; the tier card renders its own lines).
5. **Editor UI** (EstimateView.vue tier cards): per-tier line grid
   (description/qty/unit price, add+remove) wired to the new endpoints;
   the tier Price input turns read-only when the tier has lines (synced Σ,
   one source of truth); tiersLocked disables the grid.
6. **Public page**: tier cards list included lines (descriptions always;
   prices per hide_line_prices).
7. **PDF/email/portal: UNTOUCHED** — zero blast radius is the point of the
   separate table. PDF tier sections are explicitly out of scope v1 (the
   serializer payload exists for a later pass).
8. **Mobile builder: untouched.** Its untagged estimate_lines bands remain;
   Phase A's accept/invoice/job/totals paths make accepted mobile
   proposals bill the chosen tier correctly (synthesized package line),
   which retires the summed-bill bug without a data backfill.

## 4. Tests

Backend:
- Staff + public tier accept set `est.total` (flat: tier price; line-built:
  Σ tier lines). Post-accept line edit does NOT clobber it
  (`_recalculate_total` guard).
- **Page-vs-invoice equality (the audit's §3 test)**: accepted tiered
  estimate WITH a discount AND labor base lines → `compute_estimate_totals`
  total == invoice total after `_recalculate_invoice`. Run for flat and
  line-built tiers.
- Invoice copy: office-tier shape bills the tier (not base); **mobile shape
  (3 untagged tier bands) bills ONLY the chosen tier** — the summed-bill
  regression; non-tier estimates unchanged.
- End-to-end netting: Best $8k over $500 base → $4k deposit → final invoice
  → `apply_deposits_to_final` nets fully, zero unapplied.
- Tier line CRUD: validation, price re-sync on create/update/delete, manual
  price preserved when no lines, accepted-lock 409, tier delete cascades
  lines, duplicate clones tier lines + keeps synced prices.
- Public payload: tier lines present + stripped under hide_line_prices;
  flat `lines` array unchanged (no tier leakage).
- Job conversion: tier path yields package rows only; non-tier unchanged.

Frontend (vitest): tier line grid add/remove posts tier_id-scoped routes;
price read-only with lines; public tier card shows included lines; existing
specs unchanged (the `totals: undefined` proposal_mode pins still hold —
public totals stay omitted for OPEN proposals; accepted proposals may now
include totals, pinned by a new case).

## 5. Out of scope

- Portal tier accept UI (portal has no tier path — unchanged).
- PDF tier sections (payload plumbing only).
- Backfilling mobile's untagged historical lines (retired by behavior, not
  data surgery).
- est.total corrections on historical accepted-tier estimates.

## 6. Deploy

- No migration: `proposal_tier_lines` is created by the entrypoint's
  `create_orm_tables()` on first boot (verify in the deploy log). Rollback =
  version pin (the extra table is inert to old code).
- No new env, no compose churn.

## 7. Audit response (2026-08-14, adversarial subagent — pre-build)

All six findings adopted; the big ones reshaped the design:

1. "NULL tier_id = base lines" was false on live data (mobile's untagged
   bands) → **separate `proposal_tier_lines` table**, no estimate_lines
   semantic change, no backfill needed (§2).
2. The live bug is worse than planned-for: mobile-built accepted proposals
   bill ALL tiers summed → Phase A's invoice path explicitly skips
   estimate_lines on tier accepts; regression test pins the mobile shape.
3. `_recalculate_total` clobber (audit §6.1) → tier-aware guard (§3.A3).
4. Page-vs-invoice mismatch on discount/tax (audit §3) → same-base rule
   (§2) + discount kept on both sides + the equality test (§4).
5. Duplicate path (audit §6.3) → explicit tier-line cloning (§3.B3).
6. Blast-radius enumeration (audit §5) → dissolved by the separate table;
   the twelve consumers keep their current meaning untouched.
