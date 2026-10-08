# Portal quote request — customers ask for new garage doors, by job, door by door

Status: PARTIALLY BUILT — everything under Design and "Edit and withdraw" is
built on branch `feat/portal-quote-request` (not yet merged); nothing under
"Not in this plan" is.
Date: 2026-10-07

## The ask

A form in the customer portal where a signed-in customer asks for a quote on
new garage doors. It is tracked by a job name, takes any number of doors, and
is the structured input a later AI estimate builder will read. Doug placed it
on a new portal tab (2026-10-07).

## What already exists (do not rebuild)

- **The office intake pipeline is Leads** (`routers/leads.py`, `LeadsView.vue`):
  stages `new → contacted → qualified → quoted → won/lost`, a bell alert
  category `lead` that deep-links to `/leads`, and **Start Estimate**
  (`POST /api/leads/{id}/start-estimate`), which reuses a lead's
  `converted_customer_id` and copies `lead.address` to the estimate's jobsite.
  The estimate page already shows the lead's request
  (`EstimateLeadRequestPanel.vue`). A portal request becomes a Lead; no new
  staff page.
- **Portal photo upload** — `POST /portal/door-listings/{id}/photos` and
  `modules/door_listings/service.py`: MIME allowlist, pre-read size ceiling,
  re-encode that applies orientation and strips EXIF/GPS, path fenced with
  realpath + startswith, files on the `UPLOAD_DIR` volume.
- **Portal auth and audit** — `get_current_portal_customer` → `PortalPrincipal`;
  actor `portal:{user_id}` (`portal_estimate_declined`).
- **Door vocabulary** — `DoorSpec` / `ChiDoorCatalog` (`tenant_models.py`).
  Customers do not know model numbers, so the form asks plain-language
  questions whose answers map onto that vocabulary later.

Traps: `POST /portal/message` has no UI caller and no staff reader;
`/portal/door-listings` is for selling *used* doors; `Lead` custom fields are
flat key/values and cannot hold N doors.

## Prior art (searched 2026-10-07)

Jobber Requests (custom questions, photos, measurements, Client Hub intake,
office notified); Housecall Pro's portal "request work"; Paperform's garage
door quote template and dealer forms asking size (width first), style,
material, insulation, windows, opener. None stores a request in a shape our
estimate builder can read, which is the point of this one.

## Design

### Data (migration 112, both engines, guarded `has_table`)

`quote_requests`: `id`, `customer_id`, `submitted_by_user_id` (the portal
`CustomerUser`), `lead_id`, `job_name` (200, required), `site_address` (500),
`notes` (text), `doors` (JSON list), `doors_version` (int, 1), `created_at`,
`updated_at`, `deleted_at`.

`quote_request_photos`: `id`, `quote_request_id` (FK), `door_index`,
`filename`, `content_type`, `sort_order`, `created_at`.

Doors are JSON, validated by a Pydantic model on the way in, because the AI
builder reads a request whole and the door questions will change;
`doors_version` says which shape a row holds. One door:

| field | values |
|---|---|
| `quantity` | 1–10 |
| `width_ft`/`width_in`, `height_ft`/`height_in` | required; "approximate is fine" |
| `placement` | `replace` · `new_opening` · `not_sure` |
| `material` | `steel` · `wood` · `wood_composite` · `aluminum_glass` · `not_sure` |
| `style` | `traditional` · `carriage_house` · `modern` · `not_sure` |
| `insulation` | `none` · `insulated` · `best` · `not_sure` |
| `windows` | `none` · `top_row` · `full_view` · `not_sure` |
| `color` | free text, 60 |
| `opener` | `yes` · `no` · `not_sure` |
| `notes` | free text, 1000 |

Limits: 10 doors per request, 4 photos per door.

Existing rows: none — both tables are new. Rollback: `downgrade()` drops both
tables (photos stay on disk under `quote_requests/`, unreferenced); the Leads
the requests created stay and read as ordinary leads.

### Submit (portal)

`POST /portal/quote-requests` in one transaction: the `QuoteRequest` row and a
`Lead` (`source="Customer portal"`, `stage="new"`, name/email/phone from the
customer, `address` = site address or the customer's, `notes` = one line
naming the job and door count and pointing at the request,
`origin_ref="quote_request:<id>"`, `converted_customer_id` = the customer,
`company_id` = the tenant, `created_by="portal:<user>"`). **The request row is
the only copy of the doors** — the lead does not restate them, so staff
editing the lead's notes cannot make the two disagree. Audit
`quote_request_submitted` (actor `portal:<user>`), then `notify_office`
category `lead`.

`POST /portal/quote-requests/{id}/photos` (form `door_index`, `file`): own
request only, door index in range, at most 4 per door; same validation and
re-encode as listing photos. No status window: a photo that arrives after the
quote went out is still useful, and a failed upload on a phone must be
retryable from "Your requests" (audit finding 3). Audited.

`GET /portal/quote-requests`: the customer's own requests, newest first, with
a customer-facing status derived from the lead's estimates by the Leads
page's own `_progress_for_leads` (estimate started → Being priced; sent →
Quote ready; accepted or a job → Accepted; declined; expired), and Closed only
when staff marked the lead lost with no estimate. The lead's `stage` alone is
not used: Start Estimate and sending a quote never write it (audit finding 1).
Each request lists its doors with an **Add photos** control per door.

### Staff

`GET /api/quote-requests/by-lead/{lead_id}` and
`GET /api/quote-requests/{id}/photos/{photo_id}`, both `leads.read` under the
`customers` module like the Leads router. A `QuoteRequestDoors` component
renders job name, doors and photo thumbnails in the lead dialog and in
`EstimateLeadRequestPanel`, so the estimator sees the doors while building.

### Portal UI

A **Request a quote** tab: job name, site address (prefilled hint), one card
per door with **Add another door** / remove, per-door photo picker, notes,
Submit. Below it, "Your requests" with status. An empty Estimates tab links to
it. Phone-first layout.

## Risks the audit should press on

- Enumeration: every portal read and write filters on
  `principal.customer_id`; a guessed request id answers 404.
- A portal-created Lead must not be linkable to another customer's estimate:
  `resolve_lead_link` already requires `converted_customer_id` to match.
- Spam/abuse: a signed-in customer could submit many requests. Bounded by
  login (magic link / password) and the per-request limits; no rate limit
  beyond the app's own.
- Photos are customer-supplied and shown to staff only — re-encode anyway
  (EXIF/GPS, polyglots).

## Audit 2026-10-07 (adversarial, before code)

1. `lead.stage` does not follow the quote → status derived from estimates (above).
2. Doors were stored twice (JSON + lead notes) → the request is the only copy.
3. Photo uploads after submit had no retry → per-door Add photos on each request, no stage window.
4. `Lead.company_id` is NOT NULL and was missing from the field list → added.

## Build notes 2026-10-07

- The door questions and their labels live in one module,
  `frontend/src/components/quoteRequestOptions.js`, read by both the portal
  form and the staff `QuoteRequestDoors`, so an answer reads the same on both
  sides. A value added to `service.DoorIn` needs a label there.
- Staff photos render through the existing `AuthedImage` (a plain `<img>`
  cannot send the bearer token).
- The file picker accepts `image/jpeg,image/png,image/webp` explicitly, so an
  iPhone converts HEIC on the way out instead of the server refusing it.
- `door_count()` reads each door's quantity as stored: `DoorIn` holds it to
  1..10, and the #560 guard forbids defaulting a stored quantity.
- The two portal POST routes join `.authz_unpermissioned_baseline` (381 → 383)
  with their ownership check named; the photo-table columns and upload guard
  repeat `door_listings` and were blessed into `.duplicate_block_baseline`.
- Not carried over: the lead's own intake fields (door count, size, opener)
  stay empty — the doors are on the request. (The job name was not carried to
  the estimate either; see the next section.)

## Edit and withdraw (Doug, 2026-10-07)

Asked after the first browser walk: Start Estimate should fill the estimate's
Job Name from the request, and a customer should be able to change or
withdraw a sent request, with an office alert and the date and time shown.

- **Start Estimate** passes the request's job name as the new estimate's
  `label` (the field the estimate page calls Job Name). A reused estimate is
  returned as it is; a withdrawn request names nothing.
- **Change** — `PATCH /portal/quote-requests/{id}`, allowed while the status is
  Received or Being priced (no quote sent yet). Each door carries
  `source_index`, its position before the edit, so its photos follow it; a
  removed door's photos are soft-deleted (`quote_request_photos.deleted_at`).
  The audit row holds each changed field before and after; the lead's notes
  are rewritten only if they are still the line this code wrote. An edit that
  changes nothing is not stamped, audited or announced.
- **Withdraw** — `POST /portal/quote-requests/{id}/withdraw`, allowed in the
  same window as Change (no quote sent yet). Once a quote is out the customer
  accepts or declines *it* on the Estimates tab, and the request says so: a
  withdraw at Quote ready left the sent estimate live, and accepting it later
  flipped the lead back to won under a request reading "Withdrawn" (pre-commit
  audit, 2026-10-07). The request is kept and stamped
  `withdrawn_at`; its lead moves to `lost` (audited as `lead_updated` by the
  portal user); an estimate already started is left alone. Withdrawn is final
  from the portal — no further change, withdraw or photo.
- **Shown** — `edited_at` / `withdrawn_at` are on the request (migration 112,
  amended before merge), rendered with date and time in the portal list and
  in the staff `QuoteRequestDoors`. Each action sends an office notification
  whose message carries the time in the shop's zone (`AppSettings.timezone`):
  the bell shows only a row's age, then its date, and the containers run on
  UTC.

### Pre-commit audit 2026-10-07

1. A lead staff marked **won** or **quoted** by hand (sold or priced over the
   phone, no estimate in the system) read "Received", so the customer could
   still change or withdraw it. The status now reads the stage where no
   estimate speaks: won → Accepted, quoted → Quote ready, lost → Closed.
2. The edit/withdraw gate inherited the progress helper's degrade-to-empty on
   a read error, which reads as "Received". With estimates on the lead and no
   progress, the gate now refuses.
3. Withdraw at Quote ready → narrowed to the Change window (above).
4. The edit form's photo picker ignored photos already sent when capping a
   door at 4.

Second round, same day: a lead staff deleted, or closed as lost after Start
Estimate, read "Received"/"Being priced" and stayed editable; a cancelled or
written-off job read "Accepted". All three now read **Closed**. Left as
stated limits, not fixed:

- **A request is priced through Start Estimate.** Status follows estimates
  linked to the request's lead. An estimate made without the lead
  (`/estimates/new` with no `lead_id`, mobile quoting) is invisible to it, so
  the request can read "Received" with a quote out. Enforcing the link in
  those paths is its own change.
- **Withdraw leaves a draft estimate alone.** If staff send it anyway, the
  customer sees "Withdrawn" next to a live quote on the Estimates tab. The
  staff panel shows the Withdrawn tag where the estimate is built.
- **No lock between the status check and the write.** An edit that lands in
  the same instant as staff sending the quote can commit after it.

## Not in this plan

The AI estimate builder itself; mapping answers to `DoorSpec`/catalog SKUs;
a reason field on withdraw; re-opening a withdrawn request.
