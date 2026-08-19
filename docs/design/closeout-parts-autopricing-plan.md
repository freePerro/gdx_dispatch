# Job parts and labor → invoice: making the office's numbers survive to billing

**Status:** PLAN — nothing built. Investigated 2026-08-18 against prod v1.68.2
(read-only queries) and `main` at `ec2e14a`. Design corrected three times by
Doug on 2026-08-18; see § The model.
**Trigger:** an opener-install job closed out 2026-08-18 with two attested
parts; the auto-drafted invoice carried labor only. The office had already put
both parts on the job, priced, six hours earlier.
**Related plans:** completes the stated intent of
`billing-capture-hardening-plan.md` PR 4 and the unbuilt half of
`job-closeout-billing-visibility-plan.md` §8. Both cross-reference this doc.

## The model (Doug, 2026-08-18 — this governs every decision below)

> The office will figure it out at billing and from the tech's notes. We also
> want it to have the labor as tracked and/or billed. Sometimes the install
> price is in the part price.

Three consequences, and they point the opposite way from "make the autodraft
smarter":

1. **The system never decides what bills.** Its job is to make everything
   *visible and priced* at billing time. The office reconciles duplicates,
   scope changes, and goodwill using the tech's notes. This matches the
   2026-07-07 AUDIT-R1 ruling (never machine-dedup capture rows) and
   `job-closeout-billing-visibility-plan.md` §8 (never $0-line; flag it).
2. **Labor belongs on that surface too** — attested hours and what they'd
   bill, next to the parts, not in a separate mental step.
3. **Some part prices already include the install.** Billing a bundled part
   *and* an hourly labor line double-charges the customer. This is the one
   place the system must actively guard, because the office can't see the
   overlap from the line descriptions.

The office is the decider. Everything below exists to give them a complete,
priced picture — never to make the call for them.

## What already exists (do not rebuild)

The spine shipped v1.10.0 (#112, `1584f2d`) and the autodraft v1.43.0.
**No migration is needed for PRs 0-3.**

| Already built | Where |
|---|---|
| `job_parts_needed.unit_price` + `source` columns | `models/tenant_models.py:2290-2293` |
| Parts spine — closeout / mobile / van write source-tagged rows | `routers/jobs.py`, `mobile.py`, `van_inventory.py` |
| Office catalog-add that stores the **sell price** on the job | `JobDetailView.vue:2543` → `parts_needed.py:160` |
| Autodraft that lines priced parts + a labor line | `core/closeout_billing.py` |
| Labor lanes — flat install price, or hourly from **attested** hours | `core/billing_lanes.py` |
| Office pull panel on `/billing/new` and draft Edit | `LineItemEditor.vue:622-727` |
| `sku-suggest` searching inventory + custom catalogs + CHI | `parts_needed.py:544` |
| Closeout notes + `hours_worked` captured and stored | `JobCloseout` |
| `wont_bill` — the office's dismiss verb for warranty/goodwill | `BillingView.vue:986` |

## Why the office's prices don't survive (the real root cause)

The office added both parts from the catalog picker at 13:44. They stored
**correctly, with real prices** — $536.00 and $85.50, which is exactly the
$621.50 missing from the $200 draft. The prices stayed. Nothing downstream
looks at them:

- The office's own invoice pull panel requests
  `status=ordered,received,used` (`LineItemEditor.vue:632`) — but the add-part
  endpoint hardcodes `status="needed"` (`parts_needed.py:171`). **A part the
  office adds is born into the one status the checklist doesn't ask for.**
- The autodraft requests `source IN (closeout, mobile, van)` — office rows are
  `source='request'`, excluded there too.

The intent is written into the code and has never once held: the office
catalog-add carries the sell price *"so it reaches the invoice-create checklist
pre-priced"* (`JobDetailView.vue:2538-2541`) — and the next lines set the status
that makes it unreachable.

Then at closeout the tech attests the same parts, and because nothing links an
attestation to the office's existing row, the system mints a **second** pair of
rows — this time unpriced, because capture resolves prices only from inventory
`Part` via `part_id`, and catalog-picked parts are structurally guaranteed to
have no `part_id` (FK to `parts.id`; see `parts_needed.py:580-584`). So the job
carries four rows for two parts: a priced pair nothing bills, and an unpriced
pair the autodraft skips for being unpriced.

Prod-wide: 61 rows sit in `needed`, exactly 1 was ever moved to `ordered`.
Nothing leaves the status it was created in.

## Blast radius (prod, 2026-08-18)

- All 4 captured rows (`closeout`/`mobile`) unbilled and unpriced; all 4
  exact-SKU-match an active catalog item priced > 0.
- A second job (`source='mobile'`, still `Scheduled`) will hit the identical
  failure at closeout.
- **Nothing has gone out underpriced** — the affected invoice is still a draft.

## The install-in-the-price trap — no signal exists

The catalog carries both variants of the same opener:

| Item | Price |
|---|---|
| `liftmaster 2220l opener` | $536.00 |
| `Liftmaster 2220l opener with install.` | $602.00 |

Bill the $602 item *and* an hourly labor line and the customer is charged for
the install twice. **There is no field that would let code tell the difference.**
Across 2,855 active catalog items the only attribute key in use is `vendor`;
`pricing_category` holds just `parts` (2,774) and `openers` (81). The sole
distinguishing signal today is the words "with install." in a free-text name —
and money code may not guess from prose.

Separately, ~29 legacy QB-imported `Install …` items sit at $0.00 — install as
its own catalog line. Noted, not in scope here.

## Adversarial audit, 2026-08-19 — the order changed

An adversarial review of this plan (before any code) found a defect severe
enough to reorder the stack. Recorded here because the rejected order is the
part that can't be recovered from the code.

**The finding: pricing before part-identity converts a silent under-bill into
an automatic double-bill.** One physical part can hold both a `mobile` row (the
tech logged it mid-job) and a `closeout` row (the tech re-listed it at
closeout). Verified in code:

- The closeout replace step deletes only `source='closeout'` unbilled rows
  (`routers/jobs.py:2050-2069`) — `mobile` and `van` rows survive untouched.
- `_billed_keys` (`jobs.py:2071-2080`) compares only against rows that are
  **already billed** and `source='closeout'`. It cannot see a `mobile` row.
- The builder's candidate query never dedupes by sku or name across sources.

Today both rows are usually NULL-priced, so neither bills — that *is* the
missing-parts symptom this plan was written to fix. Price them both and the
autodraft emits two lines for one opener with no human in the loop. The
`from_part_ids` 409 does not save it: that guard asserts *row* identity, never
*part* identity.

**Second finding, equally load-bearing: 1,493 of 1,843 priced catalog items
have `price == cost`** — QB imports where the price field was filled with cost.
A naive "use the catalog price" tier bills a $2,207 door at zero margin,
systematically, on 81% of priced items. The fix is not to skip them but to run
cost through the tenant's own margin engine (`services/pricing_engine.py`
`price_line()`, three-axis: `pricing_category` × `pricing_class` × cost tier),
which is already the declared single source of truth for cost→sell math.

**Third: `release_untouched_autodraft` (`closeout_billing.py:279-297`) deletes
every line on an untouched draft**, including lines the office added by hand.
A re-closeout silently discards the office's manual work — which is exactly the
interim workaround for the trigger job.

Consequence: **safety lands before pricing.** Nothing prices until a physical
part can only bill once.

### Second audit, on the code — the first collapse design was wrong

A follow-up adversarial review of the actual PR 0 diff killed the first
implementation, and the reason is worth keeping:

**A filter computed over unbilled rows cannot hold a billing invariant.** The
first attempt grouped candidate rows by part identity inside
`build_closeout_lines` and skipped the non-attested ones. That suppression
*evaporates the instant the winning row is stamped*: on the next call the
suppressed row is alone in its group, so it bills. Reachable two ways — the
tech taps "Create invoice" on the truck (`mobile_invoicing` has no
existing-invoice guard), or the office pulls it from the checklist, where it
still reads as genuinely unbilled and no 409 fires. The guard produced exactly
the double-bill it was written to prevent, one call later.

Three more findings, all upheld:

- **Dropping qty from the identity key inverted a documented ruling.**
  `closeout_job`'s `_billed_keys` deliberately *keeps* qty so a differing
  quantity still lands and over-shows for operator review. The collapse
  dropped it — and would have billed 2 rollers when 4 went in the door
  (2 mobile + 2 van, closeout typed as 2).
- **A collapse is a money decision and had no audit trail.** "GDX chose not to
  bill this captured part" was a `log.info`. Invariant #1 and the no-silent-
  writes rule both apply; a silent non-write of revenue is the same defect
  class as a silent write.
- **Claims in the docstring were not true yet** — it cited a review surface
  that PR 3 has not built.

So part identity becomes its own PR with a **durable** design (a claim on the
row plus an audit event, not a per-call filter), and PR 0 shrinks to the two
changes that are correct on their own.

## Fix plan — five PRs, stacked, merge bottom-up

### PR 0 — `wont_bill` rows must never reach an invoice

`closeout_billing.py:196-203` selects billable parts with **no status filter**:
source, unbilled, and price > 0 only. A part the office dismissed as
warranty/goodwill is still eligible. Live path: part logged mid-job, office
dismisses it, tech closes out later, autodraft bills it anyway. One-line fix,
independently correct, and a prerequisite — PR 1 widens what the builder can
price, which would widen this hole with it. *(Not reproduced; reported from the
absence of the filter in the query.)*

### PR 1 — one shared sell-price resolver, two call sites

`core/part_pricing.py::resolve_sell_price(db, *, job_id, sku, part_id)`,
first hit wins:

1. **The job's own priced row for that SKU** — the office's number for *this*
   job, which may be a price they adjusted for this customer. Doug's dominant
   workflow, so it outranks list price.
2. `part_id` → inventory `Part.unit_price` (existing behavior).
3. Exact-SKU match in `custom_catalog_items` (active, `price > 0`).
4. Exact-SKU match in `chi_parts_catalog` (active, `sell_price > 0`).
5. No match or ambiguous → `NULL`. Never guess. **Exact SKU only.**

Called from capture (stamps the snapshot; makes the office checklist readable
without an N+1 lookup) *and* from line-build for rows still NULL (catches
legacy rows, van stock, and future capture paths). One function, two callers —
the A1 rule against forking a pricing implementation.

**Van rows need no special case.** Per D2, the van is a location, not a pricing
lane — a part is worth its catalog price wherever it physically sat, so van
rows run the same tiers as everything else. This deletes, rather than extends,
the "Van items carry no sell price" branch at `van_inventory.py:182-194`.

Tier 1 does **not** violate AUDIT-R1: that ruling forbade merging or
overwriting capture rows because it undercounts. Copying a price overwrites
nothing — both rows survive, only one bills, and the office still reconciles.

### PR 2 — `sku-suggest` returns a sell price

Add `price` to every suggest row. Activates the existing
`LineItemEditor.addSelectedParts` fallback, which reads `hit.price` today from
an endpoint that has never returned it — so manual pulls currently land at $0.
The tech-facing closeout dialog shows sku/name/on-hand only; no price becomes
visible to techs.

### PR 3 — the job billing review surface (the centerpiece)

Everything the office needs to decide, in one place, priced:

- **Every** parts row for the job regardless of source or status — office-scoped,
  closeout-attested, mobile, van — each badged with where it came from, qty,
  and a resolved price. Duplicates are shown *as* duplicates, not hidden.
- The tech's closeout notes, verbatim — Doug: the office figures it out from
  these.
- Attested hours and the labor those hours would bill.
- The office ticks what bills. Nothing is auto-decided.
- Before an invoice can be verified, unbilled attested parts are surfaced —
  `require_deliverable` (`core/invoice_delivery.py:44`) today asks only "did a
  human sign off", never "is anything missing", which is the rubber-stamp
  failure `job-closeout-billing-visibility-plan.md` predicted at line 913.

This is where §8's decided-but-unbuilt "mark it needs pricing" lands.

### PR 4 — "price includes installation" checkbox + double-bill guard

**DECIDED (Doug, 2026-08-18/19):** there is no signal and none will be
inferred — the office gets a checkbox, **at billing time**, on the line.

- Migration: `invoice_lines.includes_labor` BOOLEAN NOT NULL DEFAULT FALSE.
  Default FALSE preserves today's behavior for every existing line; rollback is
  a column drop. No backfill, and **no catalog change** — the catalog stays
  untouched, so there is no per-item office pass over the 81 `openers` rows.
- **Checkbox on the invoice line**, in `LineItemEditor` so it appears on
  `/billing/new`, on draft Edit, and on the PR 3 review surface — every place
  the office builds an invoice. Consistent with § The model: the office decides
  at billing, with the tech's notes in front of them.
- When a ticked line and a labor line coexist, warn, naming both lines and the
  overlap. **Warn, never block** — the office removes the labor line, or
  doesn't, and either way the tick records *why*.
- Storing it on the line (rather than leaving it as UI state) is what makes an
  absent labor line reconstructable later — the auditability rule.

Tradeoff accepted deliberately: a line-level flag is decided per invoice rather
than once per catalog item, so the office re-ticks it each time a bundled item
is billed. That is the cost of keeping the judgement where Doug wants it.

Browser walk: real office role on a throwaway container — close out a job with
office-scoped parts, confirm the review surface shows all four rows priced with
notes and hours; tick and bill; flag a bundled item and confirm the double-bill
warning; light + dark, desktop + mobile.

### Sibling sweep

Bug class: *a price lookup that consults only inventory `Part` while the
tenant's parts live in the catalogs.* Sweep every capture and pull path — van
usage, the estimate builder, PO/receiving, and any report quoting a sell price.
Scope and result reported with PR 1.

## Open decisions

All three are resolved. Nothing blocks the build.

**D1 — how does the system know a price includes the install? RESOLVED**
(Doug, 2026-08-19): a checkbox **at billing time**, on the invoice line.
Nothing is inferred from the name, and the catalog is not touched.

**D2 — van-stock rows: RESOLVED** (Doug, 2026-08-19). *"Van-stock is the same
price as the catalog price, it is just inventory tracking."* Van is a
**location, not a pricing lane** — the part is worth what the catalog says
wherever it physically sat. So van rows resolve through the same resolver with
no van-specific tier, and `van_inventory.py:182-194`'s "Van items carry no sell
price" is the defect, not the policy: it treated a storage location as a
pricing decision. The only van-specific behavior that stays is the inventory
decrement.

**D3 — data repair for the 4 existing unpriced rows: DISSOLVED** by PR 1's
build-time call — unpriced rows price themselves the next time a draft is
built. The one exception is the already-built draft, which re-lines only on
re-closeout; the office can add its two lines by hand today.

## Non-goals — filed, not bundled

1. **Estimate→job copy strips prices into free-text notes** —
   `routers/estimates.py:2117` writes `"$X ea"` into `notes` and leaves
   `unit_price` NULL. No prod row currently shows this (0 rows carry a `$` in
   notes), so the path is cold — but it is the same defect class and will bite
   when estimate-converted jobs bill.
2. **`from_part_ids` API hole** — `invoices.py:1351-1373` stamps
   `billed_invoice_id` without requiring a matching line; an API caller can
   retire parts at $0 revenue. The Vue client always pairs them.
3. **Office estimate-derived invoices drop the estimate discount** —
   `InvoiceCreateView` never sends `estimate_id`.
4. **61 rows stuck in `needed`** — status is created and never advanced.
   PR 3 makes status irrelevant to visibility; whether the workflow still needs
   `ordered`/`received` at all is a separate question.

## Rival plans — reconciled

| Doc | True state | Relationship |
|---|---|---|
| `billing-capture-hardening-plan.md` | PR 4 shipped v1.10.0 (#112) | **Parent.** Specified catalog-sell-price capture at :87; this completes it. Its AUDIT-R1 ruling is honored. |
| `job-closeout-billing-visibility-plan.md` | shipped v1.32.0+, STALE | **Governing decision.** §8 (line 469) decided the unpriced policy; PR 3 builds its unshipped half. |
| `invoicing-gap-fixes-2026-08-08.md` | shipped v1.46.0 | Adjacent — the draft→verify→send rails PR 3 hooks into. No conflict. |

No plan in the corpus reaches a decision opposite to this one.
