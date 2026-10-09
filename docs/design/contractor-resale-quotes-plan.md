# Contractor resale quotes — a branded PDF and a private markup, in the portal

Status: PARTIALLY BUILT — PR A (backend: migration 113, models, service, portal
routes, the PDF) is MERGED #937. PR B (the portal tabs: My Branding, My
Quotes, the "Resell this" dialog) is built and open as #938, not yet merged.
Neither is released. The GDPR and tax questions below
are open.
Date: 2026-10-08

## The ask

Contractor and wholesale accounts buy doors from us and resell them. Today they
retype our estimate into their own paperwork to put their price and their name
on it. In the portal they should be able to take one of their estimates, set
one markup percentage for the quote, and download a PDF under their own brand
that they hand to their customer.

Decisions (Doug, 2026-10-07):

- **Who:** portal accounts whose customer has `pricing_class` contractor or
  wholesale. Retail customers never see it.
- **Markup:** one percentage per quote, defaulting from the account's setting.
- **Private:** their branding, markup and quotes are theirs. A versioned
  disclaimer is accepted before any of it can be used.
- **Our books are never touched:** no invoice, payment, GL posting, AR, or
  change to the estimate.
- **Their brand:** they set up their own name, contact details, license number,
  logo and terms. The PDF is a download only. It has no tax line, and none of our
  notes, terms or attachments; they type their own notes and terms.
- **Packaging:** stacked PRs — A backend, B frontend.

## What already exists (do not rebuild)

| Need | Existing piece | Used as |
|---|---|---|
| Which estimates a portal customer may see | `routers/portal._get_customer_estimate_or_404` (customer-visible statuses only, own customer only) | the gate on create, so a draft or another customer's estimate is a 404 |
| The estimate as the customer sees it (lines, hidden prices, accepted tier, Good/Better/Best options) | `routers/pdf._estimate_payload` | the snapshot's input; we keep a whitelist of its keys |
| Image re-encode, EXIF/GPS strip, orientation | `modules/door_listings/service.compress_for_web` | the logo upload |
| Upload ceiling | `core/upload_limits.assert_upload_within_limit` | the logo upload |
| Fenced file serving | `core/branding_logo` (realpath + startswith) | `reseller_logo_file`, its own regex and subdirectory |
| Shop timezone | `core/pay_periods` `resolve_zone(shop_tz_name_from_settings(db))` | the date on the PDF |
| Line table markup | `templates/_pdf_line_items.html` | imported by the new template; it carries no identity |
| Portal module gate and principal | `require_module("customer_portal")`, `get_current_portal_customer` | every route |

## Design (as built in PR A)

**Tables (migration 113, guarded, SQLite and Postgres):**

- `reseller_profiles`: one row per customer (`customer_id` unique), holding
  company name, phone, email, address, website, license number, logo file name,
  terms, `default_markup_pct`, and the disclaimer acceptance (version, at, by).
- `resale_quotes`: customer, estimate, creator, reference, end customer name and
  address, notes, `markup_pct`, `base_subtotal`, `resale_subtotal`,
  `lines_snapshot` (JSON), `hide_line_prices`, `created_at`, `deleted_at`.

Existing rows: none, both tables are new. **Rollback:** downgrade drops
`resale_quotes`, then `reseller_profiles`. Logo files under
`UPLOAD_DIR/reseller/` stay on disk; delete them by hand if wanted.

**Markup math (`service.build_snapshot`, Decimal, ROUND_HALF_UP):**

- Each unit price gets `× (1 + m)`, rounded to the cent. A line that multiplies
  out on our estimate (quantity × unit = line total) is rebuilt as quantity ×
  the marked unit, so their row multiplies out too. Any other line (a hand-set
  line total) has its line total scaled instead.
- With no discount, the resale subtotal is the sum of the marked line totals.
  With a discount, their total is our discounted total × (1 + m), and their
  printed discount is the gap between that and their lines. It can be a cent
  away from our discount × (1 + m). In one rare case, a tiny markup on cheap
  units, their lines round below that target. Then no discount prints and the
  total is their lines. Either way their total is never below our pre-tax
  total for any markup ≥ 0, and a discount covering all of ours gives 0.00.
  Where our estimate also carries tax, see the tax question under Open questions.
- If our lines do not sum to our subtotal (a hand-set total), the line prices are
  dropped and the resale subtotal is our subtotal × (1 + m), so the PDF never
  prints a table that disagrees with its own Total.
- `hide_line_prices` on the estimate gives a totals-only snapshot.
- Open Good/Better/Best options are marked up per option. The subtotals are stored
  as NULL, because no single total exists until their customer picks one.
- The snapshot is frozen at create: editing our estimate later never moves a
  quoted number. Their terms are frozen into it too.

**Endpoints (`routers/portal_resale.py`, prefix `/portal`):**

- `GET` and `PUT /reseller/profile`. PUT takes `accept_disclaimer_version`; a
  stale version gets 409, and saving fields before acceptance gets 403.
- `POST` and `GET /reseller/logo`. PNG, JPEG and WebP are re-encoded. SVG and
  other types get 415; a non-image gets 422; an empty file gets 400; over 5 MB gets 413.
- `POST /estimates/{id}/resale` (201): markup and reference (default `Q-0001`
  and up), end customer, notes.
- `GET /resale-quotes`: tracking rows with our price, their price, the markup
  amount and our estimate number.
- `GET /resale-quotes/{id}/pdf`: an attachment, `private, no-store`.
- `DELETE /resale-quotes/{id}`: a soft delete.

Every route is 403 for a non-contractor/wholesale customer. Everything except
the profile read, the logo read and quote delete is 403 until the current
disclaimer is accepted.
Another customer's quote is a 404. `/portal/context` gains
`reseller: {eligible, disclaimer_accepted, set_up}` for PR B's tabs.

**Audit:** `reseller_disclaimer_accepted` {customer_id, version},
`reseller_profile_updated` {customer_id, fields}, `reseller_logo_uploaded`,
`resale_quote_created` and `resale_quote_deleted` {customer_id, estimate_id},
all under `portal:<user id>`. The details carry ids and field names only:
**no money, no markup, no end-customer name.** The audit log is staff-visible,
and the point is privacy.

**Disclaimer:** `service.DISCLAIMER_TEXT`. Its version is the first 16 hex
characters of its sha256, so rewording it forces a new acceptance.

**The PDF:** its own template `resale_quote_pdf.html` and its own renderer
`generate_resale_quote_pdf`. It does not go through `_render_template` or
`_default_branding`, so neither our AppSettings identity nor our PDF Template
header, footer and accent can reach it. Our estimate and invoice PDFs are unchanged by
construction. Rendered HTML hashes on main and the branch are identical for
estimate and invoice, with and without a template config (2026-10-08).

**Privacy guard:** `tests/test_reseller_privacy.py` fails when the tables,
the models or the `modules.reseller` package are named outside the reseller
files, including a bare `from gdx_dispatch.modules import reseller`. A new
staff screen, report, export or MCP tool that names them has to be allowlisted
in review. It allowlists whole files, which include four outside the package:
`routers/portal.py`, `core/pdf_generator.py`, `core/branding_logo.py` and
`models/__init__.py`. A new reader added inside one of those passes it.

## Deviations from the plan as written

- The code lives in a `modules/reseller/` package (models + service), not
  a `services/` module named for resale quotes, following #935's `modules/quote_requests`.
- Plain UUID columns, not foreign keys, following `door_listings`.
- Migration 113, not 112: #935 took 112.
- A dedicated template and renderer, rather than a flag on the estimate
  template, so "our PDF unchanged" holds by construction.
- The reseller logo has its own subdirectory and regex, rather than an
  extension of `resolve_logo_for_pdf`.
- These are left out of the snapshot: the estimate description, jobsite,
  valid-until date, our signature block and line categories (catalog names
  are ours).
- Delete does not require the disclaimer. Removing your own data should not need
  you to agree to anything first.

## Known limits

- **A customer reclassified to retail loses access**, delete included. Their
  rows stay.
- **Line and option text is ours:** descriptions are copied verbatim from our
  estimate, so a description that names us prints our name on their PDF.
- **Customer merge.** The merge rewrites `customer_id` everywhere, so a
  merged-away reseller's profile and quotes move to the kept customer. If
  *both* customers have a profile, the unique `customer_id` refuses the move.
  The whole merge rolls back and staff see a generic database error. Nothing
  is lost, but that merge cannot complete. No route removes a profile, so
  today the only way through is a hand edit of the database.
- **The merge also moves a profile silently.** If only the merged-away customer
  has one, its branding, markup and quotes move to the kept customer, who may be
  retail (so the rows go dark) or a different firm. The merge's staff-visible
  audit row counts `reseller_profiles.customer_id` in `rows_updated`, which
  reveals that a profile existed.
- **The privacy guard allowlists whole files** (see the guard, above). It is a
  text match, so a name assembled at runtime also passes it.
- Reflection-based tools that walk every table (the merge, `pave_tenant_db`)
  are invisible to the privacy guard. Neither reads rows back out to a user.
- No lock between the disclaimer check and the write; the window is a single
  request.

## Review round (2026-10-08, PR A)

An independent read-only review found no auth, ownership, SSRF, audit-leak or
books-write defect, and four bugs, all fixed with a test that fails without
the fix:

- A non-ASCII reference (`str.isalnum()` is true for "Ω") went into the
  latin-1 `Content-Disposition` header. The filename is now ASCII-filtered;
  the PDF still prints the reference as typed.
- A logo whose audit or flush failed left its file orphaned on disk. The file is
  now removed on any failure after it is written.
- A discount larger than the subtotal gave a negative total and markup. Both
  totals now floor at 0, as ours do in `modules/proposals/totals.py`.
- Two first saves of a profile at once gave a 500 from the unique
  `customer_id`. The loser now gets a 409.

A second round (`/audit`, 2026-10-08) found two more defects, both fixed with a
test that fails without the fix:

- A marked row did not multiply out: 6 × 1.03 at 50% printed 1.55 each and
  9.27 for the line (6 × 1.55 = 9.30).
- The discount floor held only by rounding luck. Once the lines were rebuilt
  from units, a discount bigger than our subtotal left a 66.72 balance at 20%.

A third pass found that scaling the discount on its own, apart from lines
rounded per unit, could price below cost: 100 × 1.00 less 50 at 0.4% came to
49.80 against our 50.00. Their total is now derived from ours (above), with a
test across both failing inputs.

The second round also removed a catch-all that turned any bug in `_estimate_payload` into a
"try again" 503. It now surfaces as a logged 500. The tenant id for the
hidden-prices default now falls back to the estimate's company, as
`routers/portal` does.

## Open questions (for Doug)

- **GDPR erasure and export ignore both tables.** `routers/gdpr.py` does not
  redact a reseller's profile, or the end-customer names and addresses in their
  quotes. Adding it means `gdpr.py` names the tables, which the privacy guard
  refuses until that file is allowlisted on purpose. Not built: it needs a
  ruling on what erasure means for a contractor's own customers' data.
- **Tax on a contractor estimate.** The markup applies to our pre-tax subtotal,
  and their PDF has no tax line. Where a contractor or wholesale estimate
  computes tax, our real price is subtotal plus tax. The tracked "markup amount"
  is then overstated by our tax, and a markup below the tax rate prices their
  customer under what they pay us. A read-only prod query (2026-10-08) found 8
  live wholesale estimates: 7 at `tax_rate` 0, and one `sent` with `tax_rate`
  NULL, which inherits the 7.38% default (the customer has no exemption). That
  was read from the rows and the totals code; `compute_estimate_totals` was not
  run on it. The root cause is outside this feature: a taxed contractor
  estimate conflicts with the construction-contract rule.
- **Reselling an expired or declined estimate.** "Resell this" shows on every
  estimate the portal shows, and the backend accepts every customer-visible
  status (sent, accepted, rejected, declined, expired). So a contractor can
  hand their customer a branded price on an estimate we no longer honour, and
  nothing warns them. Not built either way: limiting it to sent and accepted
  is a product call. Raised by the PR B audit, 2026-10-08.

## PR B (as built)

Frontend only; no backend, migration or route change.

- `CustomerPortalView.vue` stores `/portal/context.reseller`. Only when
  `eligible` is true does it show two tabs, **My Branding** and **My Quotes**,
  and a **Resell this** button on the estimate detail beside Download PDF. A
  retail account sees none of them.
- `PortalBrandingTab.vue`: the disclaimer text with **I agree** (a 409, meaning
  the wording changed, reloads the new text), then the profile form, locked in a
  disabled fieldset until they agree. It holds company name, phone, email,
  website, license number, default markup (0–500 %), address and terms. Blank
  fields save as empty, so clearing a field works. The logo uploads as PNG,
  JPEG or WebP. The logo is behind the portal token, so the preview is fetched
  as a blob, not an `<img src>` URL. The logo box is white in both themes
  because the PDF page is white.
- `PortalMyQuotesTab.vue`: one card per quote, showing our price before tax
  (the snapshot carries no tax, and the tax question above is open) beside
  their price and the markup, or the option list for an options quote. Each
  card has a PDF download and a delete behind a destructive confirm. Before the
  disclaimer is agreed, the list's 403 shows a pointer to My Branding rather
  than an error. Its delete confirm uses the one `ConfirmDialog` that `CustomerPortalView` now
  mounts for the whole portal. The portal has no AppLayout, and every TabPanel
  is mounted at once, so the per-tab dialog that the quote-request tab (#935)
  carried, plus a second one here, stacked two copies of each confirm.
- `PortalResellDialog.vue`: reads the profile on open and starts the markup at
  the default. Without agreement it points to My Branding. On success it shows
  the reference, their price and our price before tax, with Download PDF and
  See My Quotes. A cleared markup blocks the quote; it is not read as 0 %.
- PrimeVue mounts every TabPanel, so My Quotes reloads on a counter that the
  view bumps when branding changes or a quote is made.
- Tests: vitest for each component and for the view gating (retail sees
  nothing), plus `e2e/portal-resale-quotes.spec.js`. The e2e seeds a contractor,
  a sent estimate and a portal session, then walks agree → brand → logo →
  resell → PDF download (`%PDF-`) → delete.
