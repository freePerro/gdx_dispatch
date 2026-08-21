# The invoice-create line editor: labor, categories, and what else fell out

**Status:** **RELEASED v1.72.0** (2026-08-19) — p1–p5 merged as **#377, #378,
#379, #380, #381**, deployed to prod and demo, and walked on prod. Decisions
§5 D1–D8 all locked. F1–F10 closed: Add Labor with two lanes, category
resolution off `pricing_category`, estimate provenance (migration 072), a
whole-invoice discount, tenant-flag taxability, and one shared category
resolver replacing four hardcoded copies. Migrations **071** and **072** are
both live.

**Follow-ups from the p1–p5 build — four, all built, stacked on each other and
merging bottom-up:**
- **#383** `fix/labor-provenance-autodraft-mobile` — the autodraft and mobile
  invoicing paths were writing labor lines with no provenance at all, which is
  the path most invoices actually take. **OPEN.**
- **#384** `fix/estimate-labor-size-label` — `EstimateView` divided `width_ft`
  by 12 when the column has been feet since 2026-05-07, so ten of eleven matrix
  rows rendered "1x1" and were indistinguishable in the picker. **OPEN.**
- **#385** `fix/line-editor-grid-width` — the 11-column line row needed 1018px
  in a 962px box at 1366px, putting the **Total** column off-screen. No column
  was dropped; the overflow was 56px. **OPEN.**
- **q4** `fix/pricing-bucket-mirror` — p5 deliberately refused to adopt any
  server bucket name the synonym table already settled. §8 below records why
  that was the wrong call and what replaced it. **OPEN.**

Written from Doug's report
("after clicking create new invoice it is missing the option to add labor…
and it does not carry category over when adding from the catalog… I am sure
there is other stuff missing"). Every claim below is backed by code at a
named line or a read-only query against the **prod** database; the queries
are reproduced verbatim in §7.

**Corrections applied 2026-08-19, second pass:** the first draft counted
catalog rows without joining `custom_catalogs`, so it included a
**soft-deleted** catalog holding 89.5% of the items — rows no picker can
reach. F2 (97.3% → **74.3%**), F3 (🔴 live → **🟡 latent**) and F7 (scope)
were all re-sized against the live catalog; F4 was **upgraded to 🔴** and is
now the sharpest live money bug here. §7 leads with the join every catalog
query needs. The plan's direction did not change; three of its numbers did.

**Rival plans:** none covers this surface. The closest neighbour is
`closeout-parts-autopricing-plan.md` (parts → invoice pricing, MERGED
#360/#362/#363) — F3 and the provenance follow-up below are the same defect
class it left open, and it is cited, not superseded.

---

## 0. What already exists (do not rebuild)

A lot of this is built and works. The gaps are narrow.

| Already there | Where |
| --- | --- |
| Shared line editor with category / cost / margin / taxable / incl-install columns | `frontend/src/components/LineItemEditor.vue` |
| Tier-aware `recomputeSell()` — cost + category → marked-up unit price | `LineItemEditor.vue:506` |
| Add-from-Catalog dialog, one tab per real catalog | `components/CatalogPickerDialog.vue` |
| Parts-from-job checklist with provenance badges | `LineItemEditor.vue:47-108` |
| Closeout context card + attested-hours labor prefill | `views/InvoiceCreateView.vue:118-169, 610-664` |
| Approved-change-order checklist | `InvoiceCreateView.vue:205-228` |
| Job-photo picker at create time | `InvoiceCreateView.vue:236-268` |
| Duplicate-part warning | `InvoiceCreateView.vue:172-201` |
| A working labor matrix — 10 live rows, $475–$1,800 flat installs | `routers/labor_pricing_admin.py`, prod `labor_price_items` |
| A correct free-form-category → pricing-bucket normalizer, **on the backend** | `routers/catalog.py:1129-1153` (`_PRICING_SYNONYMS`, `_normalize_to_bucket`) |
| A data-driven category endpoint | `GET /api/catalogs/pricing-categories` (`catalog.py:780`) |
| **A tenant "Tax labor lines" setting, with a Settings toggle, honored by three of the four billing paths** | `TaxConfig.tax_labor`, `SettingsView.vue:409`, `invoices.py:1153`, `mobile_invoicing.py:613`, `closeout_billing.py:130` |
| **A correct `pricing_category` on all 300 live catalog items** — the bucket never needs to be parsed out of the free-form string | prod `custom_catalog_items.pricing_category` (219 `parts` / 81 `openers`, zero unusable) |
| **A source catalog on every picked item**, already carried by the picker and simply never put on the line | `CatalogPickerDialog.mapCatalogItem` `_group`, `catalogTabs[].name` |
| **A line-provenance pill pattern in this exact component** — reuse it for D2, don't invent one | `LineItemEditor.vue:80-107` (parts: vendor bill / ordered / received / used) |
| **The catalog-category fix — already written once, on the estimate page** | `views/EstimateView.vue:1936-1961` |

That last row is the shape of this whole plan. Most of what is missing on
`/billing/new` **exists on `/estimates/:id`** and never made it into the
shared component. This is a half-shipped fix, not a greenfield feature.

---

## 1. The user, and their walk

**Office staff at a desk, building an invoice for an installation.** They
open `/billing` → **+ New Invoice** → `/billing/new`. Today:

1. They pick the customer and the job.
2. They click **Add from Catalog**, tick a `Logic 5 Openers` row and a
   `3" Struts` row, **Add**.
3. The lines land **with the Category cell blank** — 223 of the 300 items
   the picker can offer do this — and with nothing on the line saying which
   catalog it came from.
4. A supplier price went up, so they retype a cost. The line silently
   re-prices off the **`other`** tier instead of `parts`, 10 points high.
5. They look for the labor. There is no Add Labor button, no matrix, no
   hours × rate helper. The only path is **Add Line** → type "Labor" →
   remember the flat price → pick Labor from the dropdown — while 10 priced
   install rows sit in the labor matrix, reachable only from estimates.
6. If they came from an accepted estimate, the lines were copied — but the
   invoice will not record which estimate it came from.
7. If the job was quoted with a discount, there is no way to enter one.

Steps 3 and 5 are the report; 4 is what the report turned out to be sitting
on top of. 6 and 7 are what Doug suspected was also missing.

---

## 2. Findings

Severity: 🔴 money or unusable · 🟠 real defect · 🟡 debt · ⬜ unverified.

### F1 🔴 There is no labor affordance on `/billing/new`

`EstimateView.vue:433` renders **Add Labor**, opening a matrix picker
(`EstimateView.vue:836-868`, `addFromLabor()` at `:1710`) that reads
`/api/labor-pricing/items?active=true` and writes a line with
`category: "Labor"`, `unit_price = flat_price`, `cost = loaded_labor_rate ×
assumed_man_hours`, `_priceOverridden: true`, plus the `labor_price_item_id`
FK and `estimated_man_hours` snapshot.

`LineItemEditor.vue:290-308` — the toolbar the invoice page renders — has
exactly two buttons: **Add Line** and **Add from Catalog**. No labor path.

Prod has **10 active labor-matrix rows**, all installs, $475–$1,800, with
man-hours attached, and `loaded_labor_cost_per_hour = 65.00`. All of it is
reachable from the estimate page and none of it from the invoice page.

The closeout prefill (`InvoiceCreateView.vue:610`) partly covers *service
calls* — but only when the editor is still the untouched starter row
(`starterOnly`, `:647`). Add one catalog line first and the attested-hours
labor line silently never appears. Installations, which are exactly what the
matrix prices, get nothing either way.

**Consequence:** every installation invoice's labor is typed from memory.

### F2 🔴 The catalog's category cannot survive the add — 74% of the picker

**Corrected 2026-08-19** (was "97.3%"). The first pass counted every row in
`custom_catalog_items`. It should have counted only rows the picker can
reach — see F3b. The bug is real; the number was wrong. Live figures below.

`LineItemEditor.vue:820` sets `category: item.category || null` — the raw
free-form catalog string. The Select it feeds
(`LineItemEditor.vue:188-198`) has six hardcoded Title-Case options
(`InvoiceCreateView.vue:359-366`). A PrimeVue `Select` whose model value
matches no option renders the **placeholder**, i.e. blank.

Prod, items in a **live** catalog only (`i.deleted_at IS NULL AND
c.deleted_at IS NULL`):

| | count |
| --- | ---: |
| items the picker can offer | **300** |
| category exactly in (Doors, Openers, Springs, Labor, Parts, Other) | **77** — all of them `Springs` |
| everything else → renders blank | **223 (74.3%)** |

The actual values, all of them: `Springs` (77), `Operators` (30), `Logic 5
Openers` (27), `MAXUM Openers` (18), `Hinges` (16), `Fasteners` (12),
`Accessories` (11), `Horizontals` (8), `Rollers` (7), `Drums` (7), `Cable
Sets` (6), `Torsion Bars 16ga` (6), `3" Struts` (5), `2" Struts` (5), `Punch
Angle` (5), `Solid Shafts 1" keyways` (5), `Commercial Track Sets (2"C/A)`
(5), `Bottom Fixtures` (4), `Top Fixtures` (4), `Hardware Boxes` (4),
`Decorative Hardware` (4), `Verticals` (4), `PVC` (4), `Misc` (4), and a
tail of 1–3s. The model comments say so plainly —
`CustomCatalogItem.category` is documented as "free-form", explicitly
"independent of" `pricing_category` (`models/tenant_models.py:965-971`).

**Precision, because it changes the fix:** the value *is* carried to the
server and *is* stored. Prod `invoice_lines` already holds `Accessories`,
`Inventory`, `Operators`, `Seals`. What is broken is that the operator
cannot **see** or **change** it, and nothing downstream can use it.

**Every live item has a usable `pricing_category`** (`parts` or `openers`,
zero nulls except 6 rows already tagged `openers`). So the bucket needed for
pricing is always available *without* parsing the free-form string — which
is what makes the D1 resolution in §5 work.

**EstimateView already fixed exactly this** (`EstimateView.vue:1940-1945`):
it derives the display category from the canonical pricing bucket,
title-cases it, checks membership in the option list, and falls back to
"Parts". The shared editor never received the fix.

### F3 🟡 Adding from the catalog bills at cost — **latent, not live**

**Downgraded from 🔴 2026-08-19 after simulating it.** The claim was that
58% of the catalog adds at zero markup. That is true of the *table* and
false of the *picker*, and the picker is what the office uses.

`LineItemEditor.addFromCatalog` (`:810`) sets `unit_price = item.price` and
**never calls `recomputeSell()`**, unlike `EstimateView.addFromCatalog`
(`EstimateView.vue:1960`). There is also no server-side net —
`routers/invoices.py:1204-1228` persists `unit_price` exactly as posted, and
no pricing engine runs on the invoice create path.

But the live catalog does not exercise the defect. Simulating the tier
against **every live item with a cost** — `cost/(1-margin_pct)` from
`margin_tiers` ⋈ `pricing_tier_sets` (retail, active) — every single one is
already sitting on its tier price:

| catalog | items w/ cost | price would rise | price would fall | **unchanged** |
| --- | ---: | ---: | ---: | ---: |
| Hardware | 130 | 0 | 0 | **130** |
| Springs | 77 | 0 | 0 | **77** |
| Opener catalog A | 45 | 0 | 0 | **45** |
| Opener catalog B | 41 | 0 | 0 | **41** |
| Doors | 6 | 0 | 0 | **6** |

299 of 299. The live catalog was priced *through the engine*, so
"add at catalog price" and "add at tier price" are the same number today.

**Why it still gets fixed:** the equality is a property of the current data,
not of the code. Any new import, any cost edit that isn't followed by a
re-price, or a restore of the catalog in F3b re-opens it instantly, and
nothing would notice. Calling `recomputeSell()` on add costs nothing when
the numbers already agree (D2 in §5 confirms this is a no-op today) and
closes the hole permanently.

### F3b 🟠 89.5% of the catalog rows sit in a soft-deleted catalog

The finding that corrected F2 and F3, and it deserves its own line.

| catalog | items | in the picker? |
| --- | ---: | --- |
| **QuickBooks Catalog** | **2,555** | **no — `deleted_at` is set** |
| Hardware | 130 | yes |
| Springs | 77 | yes |
| Opener catalog A | 45 | yes |
| Opener catalog B | 41 | yes |
| Doors | 7 | yes |

`list_catalogs` filters `CustomCatalog.deleted_at.is_(None)`
(`catalog.py:863`) and the soft-delete handler says so on purpose: *"the
catalog and its items leave the pickers"* (`catalog.py:953-954`).
`sku_suggest` filters it too, through a join whose comment insists on it —
*"The JOIN is not decoration"* (`parts_needed.py:~776-791`). So the 2,555
rows are unreachable from **every** invoice path, checked, not assumed.

That is almost certainly deliberate — QuickBooks is being phased out. Two
consequences worth stating rather than leaving implicit:

1. **Every alarming number in the first draft lived in this catalog** — the
   1,644 at `price = cost`, the 861 priced *below* cost, the 307 hand-set
   prices, the 2,401 `NonInventory` rows, and all 138 `Service` rows. None
   of them can reach an invoice today.
2. **It is a soft delete, so it is one restore away from being live again**,
   at which point F3 and F7 both become 🔴 with no code change. That is the
   argument for fixing them now rather than marking them WONTFIX.

### F4 🔴 Editing a cost prices the line off the *wrong* tier — 142 live items

**This is now the sharpest live money bug in the plan.** F3 turned out to be
latent; F4 did not. It fires on the most ordinary action there is: typing a
cost.

Because F2 leaves the category unusable, `categoryToPricingCategory()`
(`LineItemEditor.vue:485`) — a much weaker duplicate of the backend's
`_derive_pricing_category` — buckets almost everything to `other`. It only
recognises exact lowercase `doors|openers|parts|labor|other` plus
`springs→parts`. The backend knows `operator(s)→openers`,
`hardware/remote/keypad/accessory/track/cable→parts`; the frontend does not.
And critically, it reads the **display** string, never the item's own
`pricing_category`, which is correct on all 300 live rows.

Live prod catalog, by what the frontend actually does with it:

| item's real bucket | live items | frontend gets it right | **falls through to `other`** |
| --- | ---: | ---: | ---: |
| `parts` | 219 | 77 (the `Springs` rows) | **142** |
| `openers` | 81 | 0 | **81** |

Prod retail margin tiers:

| bucket | $0–100 | $100–500 | $500–2,000 | $2,000+ |
| --- | --- | --- | --- | --- |
| doors | 60% | 30% | 30% | 25% |
| openers | 60% | 50% | 35% | 25% |
| parts | 50% | 40% | 35% | 25% |
| **other** | 60% | 50% | 35% | 25% |

**The 142 parts rows are the live defect.** `other` carries a margin 10
points higher than `parts` below $500, so any cost edit on a hinge, roller,
drum, cable set, strut, fixture or bracket over-prices it:

- a $50-cost roller → `other` @60% = **$125** instead of parts @50% =
  **$100**
- a $150-cost hardware box → `other` @50% = **$300** instead of parts @40% =
  **$250**

**The 81 opener rows are currently harmless — by coincidence.** The
`openers` and `other` tier rows are identical at every break on this tenant,
so mis-bucketing them changes nothing *today*. Edit one number in Settings →
Margin Tiers and 81 items start mispricing silently. Same argument as F3:
the correctness is a property of the data, not the code.

Nothing about any of this is visible to the operator — the margin column
fills in with a number that looks authoritative.

### F5 🟠 `/billing/new` never records which estimate the invoice came from

`prefillFromJobEstimate()` (`InvoiceCreateView.vue:670`) copies an **accepted**
estimate's lines into the editor. The POST payload (`:750-764`) contains no
`estimate_id`. `MobileInvoiceDialog.vue:185` **does** send it.

Prod: **5 of 340** invoices have `estimate_id` set. The invoice detail page's
"linked estimate" chip (`InvoiceDetailView.vue:73-74`) is therefore dead for
every office-created invoice.

Note the contract trap: `InvoiceCreateIn` treats `estimate_id` as *"copy the
estimate's lines and ignore `line_items`"* (`invoices.py:583-584`). Sending it
as-is would **discard the operator's edits**. The honest fix is a
provenance-only field, not reusing this one — see PR C.

### F6 🟠 A discount cannot be entered on an office-created invoice

`InvoiceLineCreateIn.unit_price` is `ge=0` and `quantity` is `gt=0`
(`invoices.py:459-460`). The only negative line the system mints is the
`category="discount"` line the **estimate-copy** path creates
(`invoices.py:1188-1201`) — a path `/billing/new` never triggers, because of
F5. `EstimateView.vue:461` has a first-class Discount field. The invoice
create page has nothing, and the contract forbids the workaround.

### F7 🔴 Catalog adds ignore the tenant's own "tax labor lines" setting

Upgraded from 🟠 after checking (2026-08-19): this is not a missing default,
it is **one path out of four ignoring a setting that already exists**.

`TaxConfig.tax_labor` is a real tenant flag, exposed in the UI at
**Settings → Tax → "Tax labor lines"** (`SettingsView.vue:409`,
`data-testid="tax-labor-toggle"`), defaulting to false. Three billing paths
resolve it through the *same shared helpers*, deliberately, so they cannot
drift:

| path | line | expression |
| --- | --- | --- |
| estimate → invoice copy | `routers/invoices.py:1153` | `True if _tax_labor else not _is_labor_line(ln)` |
| mobile tier → invoice | `routers/mobile_invoicing.py:613` | same |
| closeout autodraft | `core/closeout_billing.py:130-131` | `labor_taxable = bool(_load_tax_labor_flag(db))` |
| **office catalog add** | `LineItemEditor.vue:819` | **`taxable: true`, hardcoded** |

The estimate→invoice comment (`invoices.py:1129-1135`) records what this
class of bug already cost once: *"a quote of $2,000 materials + $1,000 labor
priced tax on $2,000 and then billed tax on $3,000"* — money audit M24,
2026-08-04. The fix landed on the copy path and never reached the catalog
path. Half-shipped, exactly the pattern the working agreement names.

**Prod state:** `tax_config` = `default_rate 0.0738`, **`tax_labor = false`**.
So the tenant has explicitly said *do not tax labor*, and this path taxes it
anyway at 7.38%. **143 of 340** prod invoices carry a real rate; 197 are at
zero.

The two prefill paths on the very same page already get this right
(`InvoiceCreateView.vue:692` for the estimate prefill, `:657` for the
closeout prefill). Only the button the office actually clicks does not.

**Scope, corrected 2026-08-19 alongside F3b.** The 138 QuickBooks `Service`
rows this finding originally pointed at are all in the soft-deleted catalog
and cannot be added. What the hardcode actually reaches today:

- the **Built-in tab's 4 `Labor` items** — Tune-Up & Maintenance $95,
  Service Call / Diagnostic $85, Emergency / After-Hours Fee $150,
  Commercial Door Service $500 (`CatalogPickerDialog.vue:148-150, 153`). These
  are unambiguously labor, carry `category: 'Labor'`, and land taxed.
- **every labor line PR B is about to add**, which is the bigger reason to
  fix it in the same slice rather than after.
- the QuickBooks `Service` rows again, the moment that catalog is restored.

Smaller than first written, still a defect, and the fix is deleting a
hardcode in favour of a helper three other paths already call.

### F8 🟠 `pricing_category` is a dead field on the invoice path

`addFromCatalog` attaches `pricing_category` with the comment *"Carry the
catalog item's cost + pricing bucket so the backend tier engine computes the
marked-up sell price"* (`LineItemEditor.vue:821-823`). On the invoice path
that is false in two independent ways: `InvoiceCreateView`'s POST mapper is a
strict allowlist that drops it (`:719-742`), and `InvoiceLineCreateIn` is
`extra="forbid"` with no such field (`invoices.py:455-467`) — it would 422 if
it ever were sent. Comment drift on a money path.

### F9 🟡 The six-category list is hardcoded in three places

`InvoiceCreateView.vue:359`, `InvoiceDetailView.vue:1018`,
`EstimateView.vue:1127` — while `GET /api/catalogs/pricing-categories`
(`catalog.py:780`) exists specifically so "adding a margin tier for a new
type (e.g. 'gates') surfaces it everywhere".

### F10 🟡 The detail edit page shows the same blank cell for existing rows

Prod `invoice_lines` distinct categories include `Accessories`, `Inventory`,
`Operators`, `Seals`. Entering edit mode on those invoices renders the same
blank Select. The value survives (the model still holds it) but the operator
sees nothing and cannot tell a blank from an unmatched value.

### F11 🟡 `ChangeOrdersView` passes no `categories` at all

`ChangeOrdersView.vue:73` mounts `<LineItemEditor>` with no `:categories`, so
the column is hidden entirely — and CO lines are copied verbatim onto
invoices server-side (`invoices.py:1216`). Every change-order line reaches
billing with `category = NULL`.

### F12 🟡 No catalog provenance on an invoice line

Catalog adds hardcode `quantity: 1`, drop the item's `sku`, and there is no
`catalog_item_id` on `invoice_lines`. "Who priced this line and from what" is
unanswerable — the same open item `closeout-parts-autopricing-plan.md` names
for the parts lane, against invariant #1.

### F13 🟡 Estimate-only affordances the invoice never got

Save-line-to-catalog (`EstimateView.vue:395`), AI Suggest (`:437`), and the
plugin capture / in-context-pricing buttons (`:415-432`). Listed for
completeness; not proposed for this slice.

### F14 ✅ MEASURED 2026-08-20 — the grid does overflow, and it is handled

Walked in a real browser at 1366×900. Measured rather than eyeballed:

```
grid tracks : 36 130 180 80 110 110 70 76 90 100 36  = 1018px
editor       clientWidth 962   scrollWidth 1102      → overflows by 140px
editor       overflow-x: auto                        → scrolls in its own box
document     scrollWidth 1366  clientWidth 1366      → page does NOT scroll
```

So the row **does** overflow at a common laptop width, and it is contained
correctly — the page never scrolls horizontally, which is this repo's stated
rule. The cost is real though: the **Total column sits off-screen** and the
operator must scroll inside the editor to see a line's total.

**This stack did not cause it.** The track list on `origin/main` is
byte-identical. It is also why the p1 catalog-source pill went *inside* the
Description cell rather than becoming a 12th column — a 12th track would have
pushed Margin off too.

Filed as a follow-up, not fixed here: the honest fix is dropping or collapsing
a column at narrow widths (Incl. install and Margin are the candidates), which
is a design decision about what the office needs visible while billing.

### F14 (original note, kept) — why it was unverifiable before

With categories + cost + margin + taxable + incl-install the grid track list
(`LineItemEditor.vue:661-679`) sums to ~1,018px minimum before padding, and
the Category track is a fixed 130px. jsdom applies no media queries; only a
browser proves whether the Category select is being squeezed on a 1366px
screen with the sidebar open. **Needs a walk, not an assertion.**

---

## 3. The plan

Separate focused PRs, stacked, merged bottom-up. **PR A first** — it closes
Doug's second complaint (categories) *and* F4, the 142-row mispricing that
turned out to be the sharpest live money bug here. **PR B** closes the first
complaint (labor). With §5 D1–D8 all answered, **nothing is blocked.**

### PR A — the category column stops lying, and starts saying where the line came from

Closes F2, F4, F8, F10 and — as insurance rather than a live fix — F3.
Implements **D2** (catalog provenance), **D3** (tier wins) and **D4**
(normalize at add-time, never backfill).

1. **New `composables/useLineCategories.js`.** `normalizeToBucket(freeForm)`
   mirrors the backend's `_PRICING_SYNONYMS` + singular→plural rules
   (`catalog.py:1129-1153`) so the two sides cannot drift again;
   `bucketToOption(bucket)` returns the Title-Case option value.
2. **`addFromCatalog` fills the Category select from the item's own
   `pricing_category`**, not from the free-form string. All 300 live items
   carry one, so the cell is never blank — this alone closes F2 and F4's
   142-row mis-bucketing.
3. **`addFromCatalog` attaches a catalog-source pill to the line** — the
   picker already knows the source (`CatalogPickerDialog.mapCatalogItem`
   sets `_group`; the tab list carries the name). Rendered on the
   Description cell with the pill pattern already used for parts
   provenance (`LineItemEditor.vue:80-107`), so no 12th grid column and no
   width regression (F14).
4. **Call `recomputeSell()` after each catalog add** when a cost is known
   (D3). Verified a no-op on all 299 live costed items — it is closing F3
   for the next import, not changing today's prices. **The PR body must say
   this and show the simulation**, or a reviewer will reasonably assume it
   re-prices the catalog.
5. **Make the Select tolerate an unmatched stored value** — an "as stored:
   X" option — so prod's existing `Accessories` / `Operators` / `Seals`
   invoice lines render instead of blanking (F10). Required by D4: since the
   catalog is never rewritten, unmatched values are permanent.
6. **Delete `categoryToPricingCategory()`** in favour of the shared helper,
   and either fix or remove `pricing_category` on the invoice line path,
   which nothing can consume (F8).

**Guard — and it has to be the right guard.** A unit test that feeds the
**25 real live category strings** (§7) plus a representative dead-catalog
string through the helper and asserts each lands on a rendered option *and*
the correct bucket. A test that only exercises `Springs` proves nothing:
that is the 25.7% that already worked, and it is why this shipped broken.

*Sibling sweep required before this PR is called done:* `EstimateView`'s own
copy must switch to the shared helper — it falls back to `"Parts"`, which
mis-buckets a door — and `ChangeOrdersView` (F11) must decide whether it
passes categories at all.

### PR B — Add Labor, on the invoice — **two lanes, operator picks** (D1)

Doug: *"it could be either."* So the dialog offers both sources and never
picks for the operator. One `components/LaborPickerDialog.vue`, two lanes:

**Lane 1 — from the labor matrix (the quoted install price).**
The 10 active rows. Writes `category: "Labor"`, `quantity: 1`,
`unit_price = flat_price`, `cost = loaded_labor_cost_per_hour ×
assumed_man_hours` ($65/hr on prod), `_priceOverridden: true`.

**Lane 2 — from the tech's attested hours (the service-call evidence).**
Present only when the job has a closeout. Reuses the existing
`/api/jobs/:id/closeout-billing-suggestion` payload the page already
fetches (`InvoiceCreateView.vue:610`) — no new endpoint, no second
computation of the billing lanes.

**The invariant this design protects.** Billed labor comes from attested
hours only; code may not invent hours. A matrix flat price is a *quoted
contract price*, not a claim about hours — so:

- Lane 1 emits a **flat-price line and must never put an hours count in the
  description**. `assumed_man_hours` is used for the cost snapshot (margin
  math) and nothing else. A line reading "6.5 hrs labor" sourced from the
  matrix would be the code inventing hours, which is exactly the thing the
  agreement forbids.
- Lane 2 is the only lane allowed to express hours, and only the attested
  ones.
- When both are available **and disagree** — the matrix assumes 6.5h for a
  16x7 install, the tech attested 9h — the dialog **shows both numbers side
  by side** and makes the operator choose. Hiding the attested number
  behind a flat price is how the evidence gets lost.

Also in this PR:

3. Add the button to `LineItemEditor` behind a `show-labor` prop, so
   `/billing/new` **and** the invoice detail edit mode both get it.
4. `taxable` follows the tenant `tax_labor` flag, not a hardcode — same
   helper as PR D. On prod (`tax_labor = false`) these land non-taxable.
5. Gate on `pricing.labor_matrix.read` — **hide with a tooltip, never 403**
   (the parts panel's precedent, `LineItemEditor.vue:34-43`).
6. Add a **"Bill the attested hours"** button on the closeout context card
   too, so lane 2 is reachable even when the editor is no longer empty —
   the `starterOnly` gate at `InvoiceCreateView.vue:647` currently makes it
   unreachable the moment a catalog line is added.

**7. The migration — decided, D7: add the columns.** `InvoiceLine` has no
`labor_price_item_id` / `estimated_man_hours` (`EstimateLine` has both).
Without them a matrix-priced invoice line cannot say what priced it —
unanswerable provenance on a money row, invariant #1, and the same gap
`closeout-parts-autopricing-plan.md` leaves open for parts.

```
invoice_lines
  + labor_price_item_id  uuid     null  FK -> labor_price_items.id
  + estimated_man_hours  numeric  null
  + labor_source         varchar  null  matrix | attested | manual
```

All nullable, so every existing row is valid without a data migration.
Must run on **both SQLite and Postgres**; escape any literal `%` as `%%`.
Rollback is a plain column drop — no data is derived from them yet.
`labor_source` is the field that makes the D1 two-lane design auditable:
it records which lane the operator chose, which is exactly the question
"is this labor attested or quoted?".

### PR C — the invoice remembers which estimate it came from

Add `source_estimate_id: UUID | None` to `InvoiceCreateIn` — **provenance
only, no line copy** — because the existing `estimate_id` field means
"copy the lines and ignore mine" and would eat the operator's edits.
Persist to `Invoice.estimate_id`, `log_audit_event()`, and send it from
`InvoiceCreateView` whenever `prefillFromJobEstimate` actually fired.

### PR D — discount (whole-invoice), and the taxability the tenant already chose

**1. Discount is a whole-invoice field (D8).** Add `discount: float | None`
to `InvoiceCreateIn`, mirroring `EstimateView.vue:461` exactly, and mint the
*same* `category="discount"` line the estimate-copy path already mints
(`invoices.py:1188-1201`) — one code path, so the two surfaces cannot
produce different rows. The negative-price line stays server-minted; the
`unit_price ge=0` guard on operator input stays as-is.

Carry the existing behaviour with it: the discount reduces the **taxable**
base, and `_recalculate_invoice` floors that base at 0 when the discount
exceeds the goods.

**2. Taxability: stop hardcoding, honor the flag (D6).** F7 upgraded to 🔴
once checked — the "option somewhere" Doug asked for **already exists** and
is already wired:

- Tenant-wide: **Settings → Tax → "Tax labor lines"**
  (`SettingsView.vue:409`) — the toggle that flips the default.
- Per line: the **Taxable checkbox** in the editor
  (`LineItemEditor.vue:244-250`) — the per-invoice override.

So this PR writes no new setting. It deletes a hardcode: the catalog-add
path resolves `taxable` the same way the other three paths do, through the
same shared `_load_tax_labor_flag` / `_is_labor_line` helpers rather than a
fourth copy of the category convention (the copy-drift the `invoices.py:1133`
comment warns about by name).

⚠ **Say this out loud at review, it changes live behaviour:** prod has
`tax_labor = false`. Honoring the flag means labor-bucket catalog items
start landing **non-taxable**, where today they land taxed at 7.38%. That is
the tenant's own recorded choice being respected for the first time on this
path — but it is a visible change to what the next invoice totals, so it
ships with the before/after on a real invoice in the PR body. If GDX wants
labor taxed, the answer is one toggle in Settings, not a code change.

### PR E — data-driven categories

Replace all three hardcoded lists with `/api/catalogs/pricing-categories`,
with the current six as the offline fallback. Closes F9.

### Filed as follow-ups, not bundled

- `catalog_item_id` + price-source provenance on `invoice_lines` (F12) —
  file against the same open item in `closeout-parts-autopricing-plan.md`.
- Save-to-catalog / AI Suggest / plugin buttons on the invoice (F13).
- ~~Backfilling the free-form catalog categories into buckets~~ (223 live,
  2,778 counting the dead catalog) —
  **rejected 2026-08-19 (D4): normalize at add-time only, leave the catalog
  rows alone.** Recorded rather than deleted because the reasoning matters:
  the free-form strings (`3" Struts`, `Torsion Bars 16ga`, `Punch Angle`)
  are the office's own vocabulary and carry information the six buckets do
  not. Rewriting them would flatten that to serve a dropdown. This is why
  PR A must make the Select tolerate an unmatched stored value (F10) instead
  of assuming every row can be coerced into an option.

---

## 3b. Audit findings on p1 (adversarial reviewer, 2026-08-20)

Run before the p1 commit, against the real staged diff. Three findings were
real; all three are fixed in p1 rather than deferred.

**A1 🔴 `margin_pct_override` posted as an operator override nobody set.**
`recomputeSell` auto-fills the margin column so the operator can SEE the tier.
`InvoiceCreateView`'s POST mapper forwarded it on `!= null && > 0` alone, while
`EstimateView` has always gated on `_marginUserEdited`. The backend ranks a
stored override at top precedence (`services/pricing_engine.py`), so the fake
override would outrank the tier permanently. **Pre-existing** — it already
leaked on every cost edit — but p1's tier-on-add would have widened it to every
costed catalog add. *Fixed:* the mapper now gates on `_marginUserEdited`, and a
spec pins that a plain catalog add leaves the flag false.

**A2 🟠 The "no-op" proof validated a different function than the code.**
The §7 D3 simulation joins tier sets on `item.pricing_category`; the shipped
recompute derived its bucket from the *display* string via
`categoryToPricingCategory`. The 299/299 result was true but not evidence for
the path that actually runs. Also raises the stakes correctly:
`routers/invoices.py` stores `unit_price` verbatim with no engine, so **on
invoices the client is the pricing authority** — this is the billed number, not
display polish. *Fixed:* new `bucketForLine()` reads the item's canonical
`pricing_category` first and only maps the display string as a fallback, so the
simulation and the code now agree by construction. `onCategoryChange` clears
the bucket so an operator re-filing a line gets the new category's tier.

**A3 🟡 A dead field carrying a comment that asserted the opposite.**
`pricing_category` was attached to lines under a comment claiming the backend
tier engine consumed it; no invoice mapper sends it and the contract forbids
extras. *Fixed:* the field is now genuinely load-bearing (A2) and the comment
says what is true — client-side only, deliberately not posted.

**Second pass (same day) found three more, all fixed:**

- **A4 🔴 The margin leak was still live in `InvoiceDetailView`.** The A1 fix
  landed on the create surface only. That view mounts the same editor with
  `show-cost show-margin`, so the tier auto-fill runs there too and `saveEdit`
  read the column ungated. *Fixed*, and the sibling sweep is now: create, edit,
  estimate — all three gate on `_marginUserEdited`.
- **A5 🔴 The A1 fix introduced a regression.** `prefillFromJobEstimate` and
  `openEdit` both load a *stored* override, which by definition a human set —
  neither stamped the flag, so the new gate silently DROPPED real overrides.
  *Fixed:* both stamp `_marginUserEdited` when the loaded line already carries
  an override. This is the sharpest lesson of the round — the guard against a
  false positive created a false negative on the money field.
- **A6 🟡 The comment justifying A1 was itself false**, citing a pricing engine
  `routers/invoices.py` never calls. Verified by grep: no engine on that path;
  the value is stored as a snapshot. *Fixed* — and the snapshot is the worse
  case, because nothing later re-prices it away.

**Also corrected:** the composable spec's fixture carried a 4th column
documented as the expected bucket, never asserted, holding pre-fix values —
theater in the guard itself. Replaced with a **round-trip assertion** that
`display → bucket` returns the item's own `pricing_category` for all 32 live
rows, which is what actually proves the create and edit surfaces agree. Dead
`default export` removed; `LINE_CATEGORY_OPTIONS` wired into
`InvoiceCreateView` so it has a real consumer instead of being an unused export.

**Follow-up, deliberately not in p1:** the catalog-source pill ships on the
invoice surfaces only. `EstimateView` has a custom line table and would need
its own template work; carrying `_catalogName` there with nothing to render it
would be dead code. Doug's report was the invoice screen. Filed here, not
silently dropped.

**Survived attack, verified independently:** `_catalogName` does not leak (all
six consumers are allowlists); `optionsForLine` does not break Select identity
or loop; the grid track list is unchanged; title-casing mangles no real
category; the EstimateView swap is inert on live data.

**Rounds 3-5: the same money field broke three times in a row.** Worth
recording as a pattern, not just an outcome. Each round's fix for
`margin_pct_override` persistence created the next round's defect:

| round | fix | regression it introduced |
| --- | --- | --- |
| 2 | gate the POST on `_marginUserEdited` | dropped REAL stored overrides on the estimate carry |
| 3 | stamp `_marginUserEdited` on load | `markPriceOverride` clears that flag on any price edit → a price edit nulled a stored override while the cell still showed a number |
| 4 | split out `_marginPersisted` | `recomputeSell` reads `_marginUserEdited`, so a loaded line was treated as tier-priced: a **cost** edit refilled the cell from the tier and then persisted it over the operator's number |
| 5 | stamp BOTH flags on load | none — verified by counterfactual |

The root cause was one boolean carrying two meanings ("a human touched this"
for the recompute, and "this is safe to store" for the save). Round 5 separated
them and stamps both on load, because a stored override genuinely is both.

**Round 5 verification was a counterfactual, not an opinion.** The auditor
mounted the real editor chained to the real views and ran nine operator
sequences (cost edit, price edit, margin edit, clear, category away-and-back,
duplicate-then-edit, open-and-save-untouched, catalog add, catalog add + cost
edit), then **deleted the two new stamp lines and re-ran**: the 42% override
came back as the tier's 50% and was PATCHed to the DB. Restored, hash
re-verified. That is the evidence the fix is a fix and not a coincidence.

**Two pre-existing issues it surfaced — follow-ups, NOT p1 work:**

1. `_priceOverridden` short-circuits `recomputeSell` before the margin cell
   refreshes, so editing the price and then the cost displays *and* persists a
   stale margin (56.5% when the truth is 50%). Cell and DB agree; both are
   wrong. Predates this work.
2. A stored `margin_pct_override` of exactly `0` passes the `!= null` load
   check but fails `recomputeSell`'s `> 0`, so a cost edit refills and persists
   the tier margin. Unreachable from either UI (both gate `> 0`), but
   `routers/invoices.py` accepts `ge=0`, so an API client could plant one.

**Accepted with eyes open, not fixed in p1:**

- `VALID_BUCKETS` is a hardcoded 5 while the backend widens it from live
  `pricing_tier_sets`, so an admin-seeded bucket (e.g. `gates`) falls through
  to `other` client-side. **p5 closes this** by construction — it is the PR that
  swaps the constant for `/api/catalogs/pricing-categories`.
- The `Other` fallback lands on this tenant's *richest* tier, so an unbucketed
  item prices high rather than safe. Kept for backend parity; safe only while
  every live row carries a usable `pricing_category`, which the spec pins.
- **`taxable` stays hardcoded `true` on catalog add.** The auditor called the
  deferral indefensible: the Built-in tab's four `Labor` items land taxed
  against `tax_config.tax_labor = false`. It is **p4's** first-class scope
  (D6), not a doc line — p4 does not ship without it.

---

## 3c. p2 as built (2026-08-20)

**Both lanes, per D1.** One `components/LaborPickerDialog.vue`:

- **Matrix lane** — the 10 active rows. Emits a flat-price line at
  `flat_price`, cost snapshotted from `loaded_labor_cost_per_hour ×
  assumed_man_hours` ($65/h on prod), `_priceOverridden` so the tier engine
  leaves it alone.
- **Attested lane** — present only when the job has a closeout. Reuses the
  `closeout-billing-suggestion` payload the create page **already fetches**, so
  the dialog cannot disagree with the prefill about what the hours are worth.
  No new endpoint, no second computation of the billing lanes.

**The invariant, enforced and tested.** Billed labor comes from attested hours
only. A matrix row is a *quoted contract price*, not a claim about duration, so
the matrix lane **never writes an hours count into the description** —
`assumed_man_hours` feeds the cost snapshot and the disagreement warning and
nothing else. A test asserts the emitted description matches no
`\d+\s*(h|hr|hour|man-hour)` pattern.

**Disagreement is shown, not resolved.** Matrix assumes 6.5 h for a 16x7; the
tech attested 9 h. Both numbers render side by side and the operator chooses.
Silent below 0.5 h difference.

**Migration 071** adds `labor_price_item_id` (FK, ON DELETE SET NULL),
`estimated_man_hours`, `labor_source` (`matrix`|`attested`|`manual`) to
`invoice_lines` — all nullable, no backfill. It also closes an asymmetry that
predates this work: `estimate_lines` has carried the first two since S97, so
the estimate → invoice copy was already dropping that provenance; it now
forwards it.

**Verified on both engines, not asserted:** SQLite lane applies and is a no-op
on re-run; Postgres lane applies, and `ON DELETE SET NULL` was *exercised* —
deleting the matrix row nulls the link while `estimated_man_hours 5.00` and
`labor_source 'matrix'` survive on the line. Downgrade runs clean.

**Contract guard:** `labor_source='matrix'` without a `labor_price_item_id` is
rejected, and an id without a source is rejected — an unverifiable "matrix"
claim is the exact provenance gap the column exists to close. `labor_source`
is a `Literal`, not free text, because it is what separates quoted from
attested.

**Reachability (D1's other half):** the `starterOnly` gate meant the closeout
labor prefill became unreachable the moment a catalog line was added. The Add
Labor button makes the attested lane reachable at any time, which is the real
fix for that.

**Taxability, corrected mid-p2.** The plan said this was deferred to p4. The
review showed deferring it was not defensible: the closeout **autodraft**
already honours `tax_config.tax_labor`, so a picker that hardcoded a value
would bill the same job's labor differently depending on which route created
the line — the exact drift `invoices.py:1133` warns about by name and that
money audit M24 has already been paid for once. All three lanes now resolve the
same tenant flag, defaulting to *not* taxing labor when it cannot be read
(defaulting the other way re-introduces an overbill). **p4 still owns the
catalog-add path**, which is where D6's remaining work is.

**Second review pass on p2 found three more, all fixed:**

- **HIGH — the attested lane was dead on the invoice DETAIL screen.**
  `show-labor` was added there without `:closeout`, and the dialog hides lane 2
  when that is null rather than fetching for itself. The office fixing a draft
  would see "Bill these hours" on `/billing/new` and not on the very next
  screen, for the same job. It now loads the suggestion on entering edit mode.
- **MEDIUM — the estimate→invoice copy wrote a shape the API rejects.** That
  block builds `InvoiceLine` directly and so skips the validator: it could
  write `estimated_man_hours` with no `labor_source`, and it stamped
  `labor_source='matrix'` from id-presence alone — crediting the matrix for a
  price a human overrode. `_labor_provenance_for()` now enforces the
  validator's rules on that path, and records an overridden price as `manual`.
- **MEDIUM — the persistence fix had no guard.** The 17 contract tests were
  pure pydantic and passed unchanged against the code that dropped the fields
  on the way to the database. Three real persistence tests now exist, and the
  `POST /lines` one was **proven** by counterfactual: reverting the persistence
  fails it, restoring passes all 20.

**Also fixed:** the closeout prefill — the *dominant* labor path, since most
invoices get their labor line there rather than from the picker — now carries
provenance instead of leaving the column NULL for the majority of lines.

**Filed, not fixed here:** `EstimateView.vue:1697` divides `width_ft` by 12 when
the column is already feet, so every 16x7 row renders "1x1". The new dialog
does not reproduce it; fixing the estimate copy is its own change. Autodraft
and mobile invoicing still write labor lines with no provenance — a broader
sweep than p2.

---

## 3d. p3 as built (2026-08-20)

`source_estimate_id` is a **new column** (migration 072), deliberately not a
reuse of `estimate_id`. The
existing one means *"copy this estimate's lines and ignore mine"*, so the
create page could never send it: it prefills the editor and then lets the
operator edit, and sending `estimate_id` would have thrown those edits away
server-side. So it sent nothing, and the link went unrecorded — 5 of 340 prod
invoices have one, all from the mobile dialog, leaving the detail page's
"linked estimate" chip dead for every office-created invoice.

**The first attempt reused `Invoice.estimate_id`, and that was wrong in a way
that moved money.** Two consumers read that column as *"this invoice IS the
estimate's bill"*:

- `modules/deposits/service.py` matches deposits on
  `or_(job_id == X, estimate_id == E)`. On office invoices `estimate_id` was
  effectively always NULL (5 of 340 rows), so the estimate arm was **dormant**.
  Populating it on the majority path **arms** it — the review reproduced a
  $2,000 invoice coming out at $1,500 with a "Less deposit paid" line for a
  **different job's** money. The double-application guard beside it is
  job-scoped and does not cover that arm.
- `core/closeout_reconciliation.py` skips invoices with an `estimate_id`
  ("estimate-billed = agreed price, not a discrepancy"). Reuse would have
  silently dropped most office invoices out of the discrepancy list — and the
  premise is false for a prefilled invoice anyway, since the operator can edit
  every line.

So the two meanings get two columns. The audit event records which happened:
`estimate_link: "copied"` vs `"prefilled"`. A trail that only says a link
exists loses the distinction that matters.

**This is the finding that most justifies the audit cadence.** A field I
described in the plan as "provenance only, no line copy" turned out to change
what customers get charged, because a dormant query arm woke up the moment the
column stopped being NULL.

The contract rejects sending both — the server would be told to copy AND told
the client already has the lines.

Provenance is claimed only once estimate lines actually landed, and cleared on
job change **and on customer change** — `onCustomerChange` nulls `job_id`
*programmatically*, so the Select's `@change` never fires and `onJobChange`'s
cleanup never ran. Customer A → job with an estimate → switch to customer B
shipped B's invoice carrying A's estimate link, audited as "prefilled". That
was also how an out-of-scope estimate id could reach the API from the real UI.

`source_estimate_id` is validated exactly like its sibling — existence,
soft-delete, job scope, and `job_id` required (estimates are job-scoped, so a
counter sale cannot have come from one). It had **none** of those checks in the
first cut.

**A test-quality note worth carrying forward.** Three source-text guards in
this suite broke during p1–p3, none because the behaviour they protect
changed — all three sliced a fixed character window from a function and failed
when that function grew. A guard that fails for reasons unrelated to what it
guards trains people to widen the window without reading it, which is how it
stops guarding anything. Converted to **function-boundary** slicing as each one broke. One more
(`InvoiceCreateView.spec.js`, the job-change clear) still uses a fixed window
and is noted here rather than claimed as done.

---

## 3e. p4 as built (2026-08-20)

**Discount is a whole-invoice field (D2)**, mirroring the estimate's. It
materializes server-side as the *same* `category="discount"` negative line the
estimate-copy path already mints — one shape, one recalculation path, no
special case in `_recalculate_invoice`.

Before this the office could not enter a discount at all: `unit_price` is
`ge=0` and `quantity` is `gt=0`, so a negative line is unrepresentable, and the
only discount row the system minted came from a path `/billing/new` never
triggers.

Rejected in the contract: `discount` + `estimate_id`. The copied estimate
carries its own discount, and accepting both would bill the customer two
discounts for one negotiation. `discount` + `source_estimate_id` **is** allowed
— a prefilled invoice's lines are the operator's, so its discount is too.

The client mirrors the server's flooring (`Math.max(base - discount, 0)`) in
both the taxable base and the total. Showing an operator a total the invoice
will not have is worse than showing none, and the discount renders in the
totals block rather than only in the payload.

**D6 finished.** Catalog adds were the last path hardcoding `taxable: true`.
The Built-in tab's four `Labor` items landed taxed against the tenant's own
recorded choice, while the estimate copy, mobile tier, closeout autodraft and
(since p2) the labor picker all honoured `tax_config.tax_labor`. Now all five
agree. Goods stay taxable regardless — only the labor bucket follows the flag.

---

## 3f. p5 as built (2026-08-20)

`GET /api/catalogs/pricing-categories` exists so an admin-seeded margin tier
("gates") *"surfaces everywhere with no code change"*. Three hardcoded copies
of the six options were exactly what it was meant to prevent — and this work
briefly made it four, by adding `VALID_BUCKETS` to the shared composable.

All four now derive from the endpoint, with the base six as the offline
fallback.

**Additive, never replacing.** The server's list is unioned into the client's
set rather than overwriting it. A truncated or empty response must not be able
to *shrink* the vocabulary — if it did, live catalog rows would start bucketing
to `other`, which is the 10-point overcharge this entire plan began with. Three
tests pin that: short response, empty response, failed call.

`displayCategoryFor` and `isRenderableOption` now default to options derived
from the live bucket set rather than the frozen constant, so widening actually
reaches the resolver instead of only the dropdown.

`Springs` stays an explicit display option: it has no bucket of its own
(springs price as `parts`), but 77 live catalog rows are labelled that way and
the office thinks in springs.

---

## 4. Verification each PR must carry

Per the working agreement, and because none of this is provable by jsdom:

- Full backend + frontend matrix, every FAIL and SKIP named; lint ratchet
  checked against baseline.
- Throwaway container + **real browser**, real office role, real prod-shaped
  catalog data. Walk items that actually exist in a **live** catalog — the
  first draft's `NonInventory` / `Service` examples are in the deleted one
  and would have produced a walk that proved nothing:
  - a `3" Struts` row (Hardware) — the F2 blank-cell case
  - a `Logic 5 Openers` row (opener catalog) — the F4 mis-bucket case
  - a Built-in **Labor** item, e.g. Service Call $85 — the F7 taxable case
  - screenshot the Category cell, the catalog pill, the unit price and the
    Taxable box for each. Light **and** dark, desktop **and** 1366px.
- The F14 grid question answered by that same walk — the D2 pill is placed
  on the Description cell specifically to avoid a 12th column, so the walk
  has to confirm it didn't widen the row anyway.
- **PR A must show the D3 no-op**: the same 299-row simulation in §7, re-run
  against the branch, proving catalog prices did not move.
- Sibling sweep reported by scope and result: every `<LineItemEditor>` mount
  (invoice create, invoice detail, change orders) and `EstimateView`'s
  parallel implementation.

---

## 5. Decisions — locked 2026-08-19 (Doug)

Two rounds, same day. Nothing in this plan is blocked on a decision.

### D1 — Labor: **either. Both lanes, operator picks.**

> *"it could be either"*

**Add Labor** offers the matrix flat price **and** the tech's attested
hours, side by side, and never chooses for the operator. The invariant that
survives it: only the attested lane may express hours; the matrix lane bills
a flat contract price and puts no hours count in the description. When the
two disagree, both numbers are shown. Built in **PR B**.

### D2 — Category cell: **show which catalog the item came from.**

> *"what catalog did it come from"*

A fourth answer, better than the three offered, and the live data backs it:
there are only **five** live catalogs, and their names are short and
meaningful — `Hardware`, `Springs`, `Opener catalog A`, `Opener catalog B`, `Doors` — versus 25+ free-form item categories like `Solid
Shafts 1" keyways`.

**How it is built, stated explicitly because it is an interpretation.**
Taken literally — "the Category select now shows the catalog name" — this
would collide with D3: the select is what drives the margin tier, and
replacing it with provenance leaves the operator no way to correct a
mis-bucketed line (F4's 142 rows). So the plan splits the two jobs the one
column was doing:

| what | where | behaviour |
| --- | --- | --- |
| **which catalog it came from** | a small pill on the **Description** cell | read-only provenance, set on add. Uses the pill pattern already in this component for parts (`LineItemEditor.vue:80-107`) — no 12th grid column, no width regression (F14) |
| **the pricing bucket** | the existing **Category** select | auto-filled from the item's `pricing_category` on add, so it is never blank; stays editable so a bucket can be corrected |

This delivers what was asked — the catalog is visible on the line — while
keeping the tier correct, which D3's answer requires. **One line to veto if
the intent was literally to replace the column.**

Hand-typed lines (Add Line) get no pill and an empty, editable Category, as
today.

### D3 — Catalog price vs tier: **tier always wins.**

> *"Tier always wins"*

Matches the estimate page, so both surfaces price identically. **Simulated
before writing it down: this is a no-op on 299 of 299 live items** — the
live catalog is already sitting exactly on its tier prices (F3). So the
decision costs nothing today and closes the hole permanently for the next
import or cost edit.

The alarming population the question was framed around — 307 hand-priced,
861 below-cost — turned out to live entirely in the soft-deleted QuickBooks
catalog (F3b) and cannot be reached. **The answer stands; the risk it was
weighed against was smaller than presented.** Recorded here because the
record should show the question was decided on numbers that were later
corrected.

### D4 — Categories: **normalize at add-time. No backfill.**

> *"at add time"*

The catalog rows are not rewritten. This makes F10 load-bearing: since
`Accessories` / `Operators` / `3" Struts` stay in the data, the Select has
to render an unmatched stored value instead of blanking it. Built in
**PR A**; the rejected backfill and its reasoning are kept in §3.

### D5 — Labor identification: **operator decides per line.**

> *"Operator decides per line"*

No auto-classification — the data cannot support it. The 138 QuickBooks
`Service` rows mixed real labor (`Residential Service` $100, `Opener
install` $200) with plain parts (`Spring 218x24` $63, `Residential Bearing
Plate` $12), all tagged `pricing_category = 'parts'`. Nothing separates
them. Catalog adds use the pricing bucket; the operator ticks the existing
per-line Taxable checkbox.

Since deciding, F3b established those 138 rows are in the deleted catalog
and unreachable anyway — so this decision now governs the Built-in tab's
four Labor items and anything a future import brings in. No catalog
migration, no new column.

### D6 — Taxability: **don't default it off — but the option must exist.**

> *"no but we need the option somewhere to make it yes"*

Checked, and the answer is better than expected: **the option already exists
in two places and this path is the only one ignoring it.**

- Tenant-wide default — **Settings → Tax → "Tax labor lines"**
  (`SettingsView.vue:409`).
- Per line — the **Taxable checkbox** already in the editor
  (`LineItemEditor.vue:244-250`).

So PR D adds no new setting. It removes the `taxable: true` hardcode at
`LineItemEditor.vue:819` and resolves the flag through the same shared
helpers the estimate-copy, mobile-tier, and closeout-autodraft paths use
(F7).

⚠ **The consequence, stated plainly:** prod has `tax_labor = false`, so
labor lines start landing **non-taxable** where today they land taxed at
7.38%. Post-F3b this affects the Built-in tab's four Labor items and every
line PR B adds — not the 138 QuickBooks rows, which cannot be reached. If
the intent is that labor *is* taxed, that is one toggle in Settings, and it
will also change estimates, mobile invoices, and closeout autodrafts, which
have honored `false` all along. **Worth one look before PR D merges;**
nothing is blocked on it.

### D7 — Labor provenance columns: **add them.**

> *"Add the columns"*

`invoice_lines` gains `labor_price_item_id` (uuid, null),
`estimated_man_hours` (numeric, null) and `labor_source`
(`matrix`|`attested`|`manual`, null) — nullable, both SQLite and Postgres,
with a rollback path, in **PR B**. `EstimateLine` already carries the first
two, so this closes an invoice/estimate asymmetry as well as the "what
priced this labor line" gap (invariant #1).
### D8 — Discount: **whole-invoice field.**

> *"whole invoice"*

Mirrors `EstimateView`'s Discount field, minting the *same* server-side
`category="discount"` line the estimate-copy path already mints
(`invoices.py:1188-1201`). One code path, so the estimate and the invoice
cannot disagree about what a discount is. Built in **PR D**. (Answered in
round 1; numbered last to avoid renumbering the rest.)

---

## 6. What this does not touch

The estimate→invoice conversion rules, the deposit-netting line, and the
parts-from-job checklist all stay exactly as they are.

**Tax policy is deliberately not changed either — but it is now *applied*.**
PR D writes no new rule and adds no setting; it makes the office catalog-add
path obey the `tax_labor` value the tenant already set, the same way three
other paths already do. The rule is unchanged; the fourth path stops
ignoring it. Whether that value *should* be `false` is a separate question
for Doug and Settings, noted at D4 and out of scope here.

---

## 7. Evidence — prod queries, 2026-08-19

Read-only, `gdx-db-1` / database `gdx`.

⚠ **Read the catalog queries with the join.** Counting
`custom_catalog_items` alone includes the soft-deleted QuickBooks catalog
(2,555 rows) that no picker can reach. Every catalog figure in this plan
joins `custom_catalogs` and filters **both** `deleted_at`s. The first draft
did not, and three findings were mis-sized as a result (F2, F3, F7).

```sql
-- which catalogs actually exist, and which are reachable
SELECT c.name, c.deleted_at IS NOT NULL AS gone, count(i.id)
FROM custom_catalog_items i JOIN custom_catalogs c ON c.id = i.catalog_id
WHERE i.deleted_at IS NULL GROUP BY 1,2 ORDER BY 3 DESC;
--   QuickBooks Catalog    | t | 2555   <- soft-deleted, out of every picker
--   Hardware              | f |  130
--   Springs               | f |   77
--   Opener catalog A | f |   45
--   Opener catalog B| f |   41
--   Doors                 | f |    7
--                            live total: 300

-- reach of the six-option Select, LIVE catalogs only
--   300 items; 77 match exactly (all `Springs`); 223 render blank (74.3%)

-- the 25 live category values, in full — the PR A test fixture
--   Springs 77 · Operators 30 · Logic 5 Openers 27 · MAXUM Openers 18
--   Hinges 16 · Fasteners 12 · Accessories 11 · Horizontals 8 · Rollers 7
--   Drums 7 · Cable Sets 6 · Torsion Bars 16ga 6 · (null) 6
--   Solid Shafts 1" keyways 5 · 2" Struts 5 · 3" Struts 5 · Punch Angle 5
--   Commercial Track Sets (2"C/A) 5 · Bottom Fixtures 4 · Top Fixtures 4
--   Hardware Boxes 4 · Decorative Hardware 4 · Verticals 4 · PVC 4 · Misc 4
--   + a tail of 1-3s (Locks, Nails, Seals, Bearing Plates, High Lift, …)

-- what the frontend does with them (F4)
SELECT i.pricing_category, count(*),
       sum(CASE WHEN lower(i.category) IN
           ('doors','openers','parts','labor','other','springs')
           THEN 1 ELSE 0 END) AS frontend_correct
FROM custom_catalog_items i JOIN custom_catalogs c ON c.id = i.catalog_id
WHERE i.deleted_at IS NULL AND c.deleted_at IS NULL GROUP BY 1;
--   parts   | 219 |  77   -> 142 fall through to `other`
--   openers |  81 |   0   ->  81 fall through to `other`

-- D3 simulation: would "tier always wins" move any live price?
WITH live AS (SELECT i.* FROM custom_catalog_items i
              JOIN custom_catalogs c ON c.id = i.catalog_id
              WHERE i.deleted_at IS NULL AND c.deleted_at IS NULL AND i.cost > 0)
SELECT count(*) FILTER (WHERE abs(round(cost/(1-margin_pct),2) - price) <= 0.01)
FROM live JOIN pricing_tier_sets s
       ON s.pricing_category = live.pricing_category
      AND s.pricing_class = 'retail' AND s.active
     JOIN margin_tiers m ON m.tier_set_id = s.id
      AND live.cost >= m.cost_min AND (m.cost_max IS NULL OR live.cost < m.cost_max);
--   299 of 299 unchanged. Tier-always-wins is a no-op on live data.

-- the dead catalog's numbers, kept because a restore makes them live again
--   1,644 at price = cost · 861 priced BELOW cost · 307 hand-priced above
--   2,401 `NonInventory` · 138 `Service` (a mix of real labor and parts)

SELECT count(*) FROM labor_price_items;                 --  10 (all installs)
SELECT loaded_labor_cost_per_hour FROM pricing_settings; --  65.00
SELECT count(*) FROM invoices WHERE deleted_at IS NULL;  -- 340
SELECT count(*) FROM invoices WHERE estimate_id IS NOT NULL; --   5
SELECT count(*) FROM invoice_lines;                      -- 796
SELECT count(*) FROM invoice_lines WHERE category <> ''; --  61
-- distinct invoice_lines.category:
--   (null), Accessories, Deposit, Doors, Inventory, Labor, Openers,
--   Operators, Other, Parts, Seals, Springs
-- invoices by tax_rate: NULL|197, 0.0738|32, 0.073750|21, 0.073751|9, …
```

```sql
SELECT name, default_rate, tax_labor FROM tax_config;
--   Default | 0.073800 | f      ← the tenant says: do not tax labor
```

Retail margin tiers (`margin_tiers` ⋈ `pricing_tier_sets`, active, retail):
doors 60/30/30/25 · openers 60/50/35/25 · parts 50/40/35/25 · other
60/50/35/25, at cost breaks $100 / $500 / $2,000. `labor` tier sets exist but
are **inactive** — labor prices from the matrix, not the engine.

---

## 8. q4 — reversing p5's client/server divergence (2026-08-20)

p5 shipped a deliberate divergence, and it was the wrong call. This section
records the reasoning on both sides, because the rejected alternative is the
part that cannot be recovered from the code.

### What p5 did

`loadPricingCategories()` widens the client's bucket set from
`/api/catalogs/pricing-categories`, so an admin-seeded margin tier surfaces with
no code change. p5 added one exception:

```js
if (PRICING_SYNONYMS[b]) continue;   // never admit a settled name
```

The stated reason: `normalizeToBucket` checks `VALID_BUCKETS` before
`PRICING_SYNONYMS`, so admitting `springs` would flip its mapping and "reprice
78 live catalog rows from a GET response." The same argument covered
`accessories`, `hardware`, `operators`, `tracks`, `cables`, `remotes`, `keypads`.

### Why it was wrong

**1. The backend has no such guard.** `routers/catalog.py:_normalize_to_bucket`
checks the valid set first and the synonym table second — the exact order p5
called dangerous. The moment a tier is seeded, the server re-points the name.
Refusing client-side did not prevent a repricing; it only made the two sides
choose **different tiers for the same line**.

**2. The client's number is the one that ships.** `add_invoice_line`
(`routers/invoices.py`) stores `payload.unit_price` verbatim — there is no
server-side repricing on invoice line create. So when the two sides disagree,
the client wins, and the customer is billed off a tier the server does not
believe in. A silent, permanent, per-line mispricing.

The size of it, **measured** rather than reasoned. Seed an `accessories` retail
tier at 10% alongside `parts` at 35%, then price a $100-cost line the backend
classified as `accessories`:

| | bucket used | unit price |
| --- | --- | --- |
| p5 (refusing the name) | `parts` | **$153.85** |
| q4 (mirroring the server) | `accessories` | **$111.11** |

`bucketForLine` gates on `VALID_BUCKETS.has(pc)`; with the name refused it falls
through to the display category, which the synonym table sends to `parts`.
**$42.74 overcharged on a single $100 line**, and nothing on screen says so.

**3. The 78 Springs rows were never at risk.** Prod, 2026-08-19: all 300 live
catalog items carry an explicit `pricing_category` (219 `parts`, 81 `openers`,
zero null), and every springs-labelled row among them carries `parts`. Both
sides honour an explicit value ahead of any free-form word, so **widening cannot
move a row that states its own bucket**. The guard protected against something
that could not happen, at the cost of something that could.

**4. It re-opened the exact drift the module exists to close.** The header of
`useLineCategories.js` warns that a second copy of the category convention "is
exactly how the two drifted apart the first time." p5 was that second copy.

### What replaced it

- Every bucket the server declares is adopted, synonym-table name or not. The
  mirror is restored.
- `seededBuckets` records what was adopted beyond the base five, so a seeded
  tier is inspectable rather than folklore.
- The additive property p5 got right is kept: a short or failed response can
  never shrink the client's vocabulary.
- Seeded types sort **ahead of** the `Other` catch-all in the line editor's
  dropdown. They were previously appended past the end — below the fallback,
  and below the visible edge of the overlay's internal scroll.

### The guard that would have caught it

`src/composables/__tests__/pricingBucketParity.spec.js` asserts the client
against `routers/catalog.py` itself: identical synonym tables key-for-key,
identical base bucket sets, and the same valid-before-synonyms lookup order.
Every earlier test asserted the client against **itself**, which is why nothing
failed when p5 broke the mirror.

Counterfactually verified: re-introducing the `continue` fails the parity spec,
the two mounted `LineItemEditor` guards, and the browser spec (which reports
`server offers "accessories" but the dropdown does not`).

### Residual, named rather than hidden

- Editing a **legacy invoice line** whose free-form category matches a
  newly-seeded name will now price it off that tier. That is the seed's intent
  and matches the server, but it is a behaviour change on edit. It cannot fire
  by merely opening an invoice: all five `recomputeSell` call sites are explicit
  operator actions (cost, category, margin, margin-override, catalog add), and
  `cloneLines` — the single ingest point — does not recompute.
- `VALID_BUCKETS` never shrinks within a page session, so un-seeding a tier
  leaves the name offered until reload. Deliberate: the additive rule is what
  stops a transient short response bucketing live rows to `other`.
