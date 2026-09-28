# Lead Intake, Call-Back Dates, and Start-Estimate

**Date:** 2026-09-28
**Status:** PARTIALLY BUILT — PR A (backend: migration 099, `leads.intake`, the §3 endpoints, digest, the boot-time seed of the default fields, and the Custom Fields "Lead" option — pulled forward from PR C because PR A creates the fields it labels) is #817, not yet merged. **Not built:** PR B (mobile intake form and entry points), PR C (desktop Leads view, dashboard count, Inbox/Calls buttons, the estimate-screen request panel).

Builds on, does not replace: `docs/design/archive/call-capture-followup-plan.md`
(the mobile quick-capture note, BUILT 2026-07-07) and P2.2 of
`docs/design/email-inbox-improvement-plan.md`, which deliberately left "Create
estimate from email" unbuilt as "a builder-prefill feature, not an inbox
button". This plan is that feature.

## Problem

Calls and emails asking for an estimate come in when nobody can price them.
Today there are two half-paths and neither finishes:

- The mobile **quick capture** saves a free-text planner task. It resurfaces in
  the morning digest, but it has no structure (no door size, no job kind), and
  nothing turns it into an estimate — someone has to re-key it.
- The **Leads** pipeline has structure and a dedupe-safe convert-to-customer,
  but a lead can only be typed in by hand on the desktop Leads page. There is
  no "save as lead" from a call or an email, no call-back date, and techs have
  no leads permission at all.

## Decisions taken (Doug, 2026-09-28)

1. A request lives as a **Lead** until it is estimated. Quick capture stays for
   non-sales reminders.
2. **Office and techs** can fill out the intake form.
3. **Start estimate** finds or creates the customer, then opens a new estimate
   with customer and jobsite address filled in. No line items are drafted, and
   **no request notes are copied onto the estimate** — its `notes` and
   `description` both print on the customer's PDF. Instead the estimate screen
   shows the lead's request beside the estimate (decision 7).
4. Intake asks for: repair or new door, door count and size, door options
   (windows, raised panel, etc.), opener info, how they found us — **and the
   extra fields are editable in settings.**
5. **Techs submit only.** No pipeline, no Start estimate.
6. **Default call-back date: next business day** (a Friday or Saturday lead
   defaults to Monday; weekdays to tomorrow), in the business's local
   calendar. New leads are **unassigned** — the office picks them up.
7. **Call notes: show beside, copy nothing.** Chosen over an internal-notes
   column (a second migration) and over copy-and-edit (private notes one
   missed edit away from the customer).

Prior art checked 2026-09-28: Jobber *Requests* → "Convert to Quote" preselects
the client and shows the request beside the quote builder
(help.getjobber.com/en/articles/converting-a-request-to-a-quote-or-job/);
Housecall Pro's Leads board converts a lead to an estimate, on mobile too
(help.housecallpro.com/en/articles/12833580-how-to-use-leads-on-mobile). Same
shape; we build it because every piece but the glue already exists here.

## What already exists (do not rebuild)

Verified against `origin/main` @ 5c5a15b9, 2026-09-28.

| Piece | Where | Use |
|---|---|---|
| Lead model: name, email, phone, address, stage, source, assigned_to, notes, last_contact_at, converted_customer_id | `models/tenant_models.py` `class Lead` | The intake record |
| Dedupe-safe convert (email, then last-10-digit phone; idempotent; says when it matched) | `routers/leads.py` `_find_matching_customer`, `convert_to_customer` | Customer step of Start estimate — refactor into a helper, don't copy |
| Custom field definitions + values, admin editor, types text/number/date/select/boolean | `routers/custom_fields.py`, `CustomFieldsView.vue` | The editable intake fields — add entity `lead` |
| Mobile capture sheet on the bottom nav | `components/QuickCaptureSheet.vue`, `AppBottomNav.vue` | Entry point on mobile |
| Phone matcher / normalizer | `modules/phone_com/customer_resolver.py` | Show "existing customer: X" as you type |
| Morning digest email | `tasks/planner_digest.py` | Add due/overdue leads |
| Dashboard website-leads attention count | `DashboardView.vue` `loadWebsiteLeads` | Add call-back-due count beside it |
| Inbox "Create task" (desktop + mobile) | `InboxView.vue`, `MobileInboxView.vue` | Put "Create lead" beside it |
| Estimate create with customer + address in one call | `create_estimate` handler behind `POST /api/estimates` (`EstimateCreateIn`) | Extract its body into a non-committing helper both paths call (§3) — no second copy |

## Design

### 1. Data — one additive migration (next free number at build time)

- `leads.follow_up_date` DATE NULL, indexed — "call back by".
- `leads.estimate_id` UUID NULL — makes Start estimate idempotent (a second tap
  reopens the same draft).
- `leads.origin_ref` VARCHAR(120) NULL — provenance, e.g.
  `phone_com_call:<id>` or `outlook_message:<id>`, so a call/email is never
  captured twice without a warning.
- Custom fields: `ENTITY_TYPES` gains `"lead"` (code only; there is no DB check
  constraint). Seed (at boot, not in the migration — see *Seed target*) default lead definitions — Job kind (select: Repair / New
  door / New opener / Door + opener), Door count (number), Door size (text),
  Door options (text), Opener (text). They are ordinary rows, so the admin can
  rename, reorder, delete or add to them.
- "How they found us" stays on the existing `Lead.source` column (reports read
  it), as a fixed dropdown plus "Other".
- Guarded `ADD COLUMN` (fresh installs build columns from the ORM before
  alembic runs); must pass on SQLite and Postgres. Rollback drops the three
  columns only: the grant stays (the 029 reasoning — upgrade cannot tell its
  own grants from later ones). The seeded definitions are not the
  migration's; they stay (the table carries `deleted_at`, invariant #2).
  No money rows touched.

- **Seed target and seed step (audit round 1, finding 3) — resolved in #817.**
  Prod carries two company ids in `tenant_roles`, so guessing from table
  contents is ambiguous (and the first draft's fallback queried a column that
  does not exist, crashing any database with no leads — demo has none). The
  seed is **not** in the migration at all: `pave_tenant_db` rebuilds by
  re-running the migration chain on an empty schema and reloading the dumped
  rows, so a migration seed collides with its reloaded copy
  (`uq_custom_field_key`). It is `custom_fields.seed_default_lead_fields`,
  called by `bootstrap_app` on every boot after migrations, under
  `company_id()` — the env-sourced id the app serves (verified 2026-09-28:
  prod's `GDX_TENANT_ID` equals the `company_id` on all 12 of its leads;
  demo has its own id and no leads). It seeds only if no lead field was ever
  defined (deletion is soft, so deletions stick), writes a
  `lead_intake_fields_seeded` audit row, and a failure is logged without
  failing boot.

### 2. Permission

New key `leads.intake` — "Submit a lead through the intake form". Added to
`BUILTIN_ROLES["technician"]` **and** delivered by a grant migration using
`migrations/grant_helpers.py::grant_permission_to_seeded_roles`, the migration
029 pattern (audit finding 1): `_load_user_permissions` trusts a non-admin
role's stored snapshot verbatim, and prod's two technician rows are snapshots
with no `leads.*` key — BUILTIN alone would ship a form that 403s for every
tech. Roles holding `leads.write` are accepted too; "either key" is a
`has_permission` check inside the handler, because `require_permission` with
two keys demands both (audit finding 5).

### 3. Endpoints (`routers/leads.py`)

Every mutation writes its audit row in the same transaction (invariant #1).

- `GET /api/leads/intake-form` — lead custom field definitions + source list.
  `leads.intake` or `leads.write`. (The custom-fields router is admin-only, so
  the form cannot read definitions from there.)
- `POST /api/leads/intake` — create the Lead + its custom values atomically;
  `follow_up_date` defaults to the next business day (decision 6); returns the lead plus
  `matched_customer` (display only) and `possible_duplicate` (an open lead with
  the same phone/email or the same `origin_ref`). Warn, never block.
- `GET|PUT /api/leads/{id}/custom-fields` — read/edit answers (`leads.write`;
  read also `leads.read`).
- `GET /api/leads?follow_up=overdue|today|upcoming` + overdue-first sort;
  `GET /api/leads/follow-up-summary` → `{overdue, due_today}`.
- Existing `PATCH /api/leads/{id}` accepts `follow_up_date`.
- `POST /api/leads/{id}/start-estimate` — `leads.write` + `estimates.write`.
  One transaction: reuse `lead.estimate_id` if that estimate is live; else
  resolve the customer (existing / matched / new — the response says which,
  because a dedupe match is a merge decision the user must see); create the
  draft estimate with `customer_id` and `jobsite_address = lead.address` —
  `notes` and `description` stay empty (decision 7); set `lead.estimate_id`.
  The lead's stage is left alone — "quoted" means sent.
- `GET /api/leads/by-estimate/{estimate_id}` — the lead (notes, source,
  intake answers, call-back date) whose `estimate_id` matches, or 404.
  `leads.read`. Feeds the estimate screen's side panel.

  **This is a real extraction, not a call-through (audit finding 2).** There
  is no estimate service: `create_estimate` (`routers/estimates.py`) is a
  handler that commits and then audits, and `convert_to_customer`'s `_finish`
  commits, audits (which commits again) and defaults the stage to `won`. PR A
  extracts two non-committing helpers — *resolve-or-create customer* and
  *create draft estimate* — used by both the existing handlers and
  start-estimate, with the existing handlers' behavior pinned by their
  current tests before the move. Numbering, tax defaults and
  `hide_line_prices` inheritance must come from the extracted helper, never a
  second copy.

  **Audit trail gaps the reuse would inherit (finding 5), fixed in the same
  extraction:** `convert_to_customer` creates a Customer with no
  customer-entity audit row (only `lead_converted_to_customer` on the lead),
  and `create_estimate` audits with `tenant_id=None`. Start-estimate writes
  `lead_estimate_started`, `customer_created` (when new) and
  `estimate_created`, all with the tenant id, in the one commit.

### 4. Where people see it

- **Mobile (PR B).** The bottom-nav capture button asks *Quick note* or
  *Estimate request*; the second opens `LeadIntakeForm`. The Phone view's call
  rows and the Inbox message view get a **Lead** button that opens the same
  form prefilled (number, name, email, subject + snippet as notes,
  `origin_ref`). The **Lead** button on the Phone view is office-only by
  construction (that view already requires `nav.office`); techs reach the
  form from the "+" button. On save: techs see "Saved — the office will follow up";
  office users also get **Start estimate**.
- **Desktop (PR C).** Leads view: call-back column with inline date edit,
  *Due* filter, overdue first, the intake answers in the detail panel, and a
  **Start estimate** button that lands on `/estimates/{id}`. "Create lead" on
  InboxView, PhoneComCallsView rows and the Cold Leads view, opening the same
  form in a dialog. Dashboard: "N leads to call back (M overdue)" beside the
  website-leads count. Admin → Custom Fields: "Lead" in the entity dropdown.
  **Estimate screen:** a read-only "Customer request" panel beside the
  builder when the estimate came from a lead — notes, intake answers, source,
  and a link back to the lead. Hidden for users without `leads.read`. It must
  work on a phone too (office users open `/estimates/:id` from mobile), so it
  collapses above the builder on narrow screens — proven in a real browser,
  since jsdom applies no media queries. It is never part of the PDF or the
  customer's proposal page.
- **Digest.** `planner_digest` adds a "Leads to call back" section, and must
  now send when leads are due even if no planner task is open (today it
  returns `nothing_open` first).

## PR slicing (stacked, merge bottom-up)

- **PR A — backend:** migration, permission, endpoints, custom-field entity,
  convert refactor, digest. Tests on SQLite and PG.
- **PR B — mobile:** `LeadIntakeForm`, capture-sheet choice, Phone + Inbox
  buttons. Verified on the Pixel 8 AVD as a real tech and a real office user,
  light + dark.
- **PR C — desktop:** Leads view, dashboard, Inbox/Calls/Cold Leads buttons,
  Custom Fields option. Real browser, light + dark.

Done = a call taken on a phone becomes a lead in under a minute, shows up
overdue on the dashboard and in the next morning's digest email, and one tap
from the lead opens a draft estimate for a new or matched customer — walked on
prod.

## Decision notes

- **Techs (decision 5).** Start estimate requires `leads.write` +
  `estimates.write`, which techs do not hold. Audit finding 4 corrected the
  earlier reasoning: the boundary is *not* that techs cannot create
  estimates — `POST /api/estimates` is gated by module membership alone, so a
  tech can already create a draft estimate for any customer today. That gap
  is pre-existing and out of this plan's scope; reported to Doug, not filed.
- **Notes (decision 7).** Doug first asked for the request notes in the
  estimate's notes field; `estimates.notes` prints on the customer's PDF
  (`templates/estimate_pdf.html` notes block) and there is no internal-only
  field. Offered: copy-and-edit, a new internal-notes column, or show-beside.
  Doug chose show-beside.
- **Business day (decision 6)** reads the existing tenant settings
  `AppSettings.default_workdays` (bitmask, Mon=1..Sun=64) and
  `holiday_calendar` — no new weekday list.

## Audit round 1 (2026-09-28) — folded in

Adversarial review of this plan against `origin/main` @ 5c5a15b9, with
read-only prod queries. All five findings confirmed and folded above:
(1) BUILTIN-only permission would never reach prod's technician snapshots —
grant migration added; (2) there is no estimate service and both reused paths
commit mid-flow — helper extraction made explicit; (3) two company ids on prod
make a migration seed ambiguous — seed target resolved at build, admin-button
fallback; (4) the stated reason for techs-submit-only was false — corrected;
(5) convert creates customers with no customer audit row, estimate create
audits with no tenant id, and an either-key gate needs `has_permission` —
all folded into PR A. Still unverified: whether a technician's mobile session
reaches `/api/leads/*` past the `customers` module gate and the frontend route
guards — PR B proves it on the AVD as a real tech.

## Review of #817 (2026-09-28) — fixed on the branch

The first cut of PR A had a red CI (lint + 5 of 7 shards) and seven code
defects, all fixed before merge: the no-leads migration crash above (and,
found while fixing it, the migration seed breaking `pave_tenant_db`'s
rebuild — the seed moved to `bootstrap_app`); an
undeclared change to `core/modules.py`'s role lookup (reverted — it broke
4 closeout-read tests); intake echoing a matched customer's name, phone and
email to technicians (now shown only to `customers.read_all`, duplicate-lead
details only to `leads.read`; both still recorded in the audit row); the
dedupe phone match widened from last-10-digits to substring (reverted);
lazy re-seeding that made the default fields undeletable (removed);
start-estimate forcing `hide_line_prices=False` over the tenant's total-only
default (now inherits); and raw exception text in a 500 detail (opaque now).
The Custom Fields admin view labelled every non-job group "Customer Fields",
so the seeded lead fields would have appeared as five unexplained customer
fields — it now has a "Lead Intake Fields" group and a Lead option.
Also folded in: `create_estimate`'s audit row now commits with the estimate
and carries the tenant id, and the handlers this PR touched that stage
before auditing (intake, start-estimate, convert-to-customer,
`create_estimate`, the boot seed) call `ensure_audit_table` first. Not swept:
other handlers of that shape remain (e.g. `delete_landing_lead` on main).

## Out of scope

Automatic text/email to the customer (A2P SMS is still blocked per the
call-capture plan), missed-call text-back, drafting line items, a customer
self-serve request form.
