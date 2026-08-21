# Job photos: the office can't see them, and can't put them on an invoice

Status: **PARTIALLY BUILT** (verified on main 2026-08-21). Built: S1
(`JobDetailView.vue:1836` reads `/api/jobs/{id}/photos`), S2 (`uploads.py:211-257`
writes `documents.job_id`), S3 (`InvoiceCreateView.vue:264` checkboxes,
`InvoiceDetailView.vue:589`, `MobileInvoiceDialog.vue:187`), S5
(`photos.py:159` `_PHOTO_READ_KEYS = ("jobs.read_all", "invoices.read_all")`),
S7 (migration 063), and the `AuthedImage` fallback.
**Not built:** S4 — both "dead mobile writer" routes still exist
(`mobile.py:3452-3453`, `POST /jobs/{id}/photos` + the `/job/{id}/photo`
alias) and `docs/tech_mobile.md` still advertises them. Note they are no
longer dead: the handler is a live multipart upload with EXIF, so deleting
them is now a decision, not a cleanup. S6 was **dropped** by Doug — not a gap.

Doug: *"a tech adds a photo to a job and can see it in mobile and the office
cannot see it in photos for the job. We are also supposed to be able to add the
photos to the invoice but there is no way of doing that."*

---

## 0. Production evidence (read-only, 2026-08-12)

Every number below is from prod, not inference. They kill three items the first
draft of this plan wanted to build.

| Query | Result | What it settles |
| --- | --- | --- |
| `job_photos` (live) | **31** | The techs' photos ARE reaching the server |
| …with url `/api/documents/%` | **31** | All of them are on the good path, bytes downloadable |
| …with url `/mobile/uploads/%` | **0** | The dead legacy writer has never been used in prod |
| …uploaded in last 30 days | **31** | This is current, active use — not historical residue |
| `documents` with `entity_type='job_photo'` AND `job_id IS NULL` | **0** | Nobody has ever uploaded a photo from the office job page. **The S2 backfill has zero rows.** |
| `documents` with `entity_type='job_signature'` | **0** | The signature defect (D8) has no prod data either |
| `invoices` with `attached_photo_ids` set | **0** | The invoice-photo feature (shipped v1.44.0) has **never once been used** |
| `users` by role (live) | admin 4, technician 3, owner 2 | **No accounting / sales / viewer users exist** — see D7 |

So: 31 real photos sitting in the system, correctly stored, and zero of them
have ever made it onto an invoice.

---

## 1. Root cause of what Doug reported

### D1 — the office job page filters on a field the API does not send *(THE bug)*

[JobDetailView.vue:1515](gdx_dispatch/frontend/src/views/JobDetailView.vue#L1515)

```js
const photoDocs = computed(() => documents.value.filter((doc) => doc.entity_type === "job_photo"));
```

`documents` comes from `GET /api/documents?job_id=…`, whose response model
[DocumentOut](gdx_dispatch/routers/documents.py#L50-L64) has **no `entity_type`
field**. The filter is never true, so the Photos tab renders "No photos yet."
for every photo from every source, permanently.

Stronger than "empty today": it is *structurally unreachable*. The only writer
of `entity_type='job_photo'` is `uploads.py:285`, and that writer never sets
`job_id` — so no row can satisfy the `job_id` query filter and the
`entity_type` UI filter at the same time. (Audit-verified by executing both
handlers against Postgres.)

Where the tech's photo actually is today: the job page's **Email tab**, under
"Files on this job" — the same fetch with the filter inverted
([JobDetailView.vue:1518-1522](gdx_dispatch/frontend/src/views/JobDetailView.vue#L1518-L1522)).
*(First draft of this plan called that "the Documents tab". There is no
Documents tab on the job page.)*

### D2 — the two upload writers disagree about how a document is job-linked

[uploads.py:199-237](gdx_dispatch/routers/uploads.py#L199-L237) writes
`entity_type`/`entity_id` (both marked DEPRECATED in the ORM,
[tenant_models.py:838-841](gdx_dispatch/models/tenant_models.py#L838-L841)) and
never `documents.job_id`, which is what `GET /api/documents?job_id=` filters on.
An office-uploaded job photo is invisible to the page it was uploaded from.
**Prod count of affected rows: 0** — nobody uses that button, plausibly because
the tab it lands on has always looked empty.

### D3 — the tab could not show a photo even if the filter worked

[JobDetailView.vue:730](gdx_dispatch/frontend/src/views/JobDetailView.vue#L730)
renders `📷` + filename + a download button. The office wants to *see the door*.

### D4 — attaching photos to an invoice: one conditional card, and nothing else

The picker ([InvoiceDetailView.vue:541](gdx_dispatch/frontend/src/views/InvoiceDetailView.vue#L541))
renders only when `invoice.job_id && jobPhotos.length`; toggling is draft-only —
and that is a **server** rule too ([invoices.py:1377](gdx_dispatch/routers/invoices.py#L1377)
returns 409 on any non-draft edit), so once an invoice is sent there is no path
to attach photos at all. Meanwhile:

* `/billing/new` — zero photo affordance (0 matches for "photo" in InvoiceCreateView).
* Mobile invoice dialog — zero. The tech who shot the photos can't attach them.
* The closeout autodraft never sets `attached_photo_ids`.
* `fetchJobPhotos` swallows any error into `[]`
  ([InvoiceDetailView.vue:832-842](gdx_dispatch/frontend/src/views/InvoiceDetailView.vue#L832-L842)),
  so a failed fetch is indistinguishable from "this job has no photos".

**Prod: 0 invoices have ever carried a photo.** The backend half is sound
(PATCH validates every id against the invoice's job; the PDF renders them —
`test_invoice_job_photos.py` passes 7/7). Only the way in is missing.

---

## 2. Defects the audit found that I had missed

### D7 — office roles below dispatch-manager cannot read job photos at all *(latent)*

`assert_job_access` → `is_dispatch_manager` admits only
`DISPATCH_MANAGER_ROLES = {owner, admin, dispatcher, manager, super_admin}`
([roles.py:66-68](gdx_dispatch/core/roles.py#L66-L68)) — but `nav.office`, the
key that puts the office nav and the Photos page on screen, is granted to
**accounting**, **sales** and **viewer**
([permissions.py:226,240,244](gdx_dispatch/core/permissions.py#L226)).
Executed per role: those three get **404** on `GET /api/jobs/{id}/photos` and
**403** on `/api/photos/recent`. PhotosView treats only 403 as access-denied
([PhotosView.vue:241](gdx_dispatch/frontend/src/views/PhotosView.vue#L241)), so
the 404 falls through to "No photos yet" — a lie to the user.

**Not what is biting Doug**: prod has no such users (admin 4 / technician 3 /
owner 2). But it is a live trap the moment a bookkeeper account is created, and
every fix below routes through that endpoint. Fix the check, don't wait for it.

### D8 — the office's Signature tab is broken three ways over *(adjacent)*

`upload_customer_signature` ([uploads.py:364](gdx_dispatch/routers/uploads.py#L364))
writes `entity_type='job_signature'` + `job_id` NULL (invisible to the fetch),
the UI filters on `entity_type` (dropped by DocumentOut), **and** the bytes go
to `<root>/<tenant>/job_signature/<job>/<file>` while `download_document` reads
the flat `<root>/<filename>` — so even a direct link 404s. The job-photo sibling
was moved to the flat path with a comment explaining exactly this
([uploads.py:269-274](gdx_dispatch/routers/uploads.py#L269-L274)); the signature
was left behind. Prod rows: 0.

### D9 — smaller, real

* The dead mobile writer has **two** routes, not one: `POST /api/mobile/jobs/{id}/photos`
  **and** the alias `POST /api/mobile/job/{id}/photo` ([mobile.py:3333-3334](gdx_dispatch/routers/mobile.py#L3333-L3334)).
  Deleting "the route" would miss the alias. `docs/tech_mobile.md:62` still
  advertises it as *the* mobile photo endpoint.
* `/mobile/uploads/...` does not 404 — the SPA catch-all answers **200 with
  HTML**. `AuthedImage` checks `resp.ok`, gets `true`, and makes an object URL
  out of HTML: a broken frame with no fallback. Prod has 0 such rows, so this
  is a latent trap, not a live one.
* `job_photos` rows outlive a deleted job (`delete_job` cascades only to
  appointments), and `/api/photos/recent` joins no job.
* `list_job_photos` has no `company_id` filter (unlike `_get_photo_scoped`).
  Not a live isolation hole — one tenant per DB, isolation is the connection —
  but it is the odd one out and should match its sibling.

---

## 3. What I propose to build

### S1 — the job page reads the photo record, and shows the photo *(fixes D1, D3)*

* Replace `photoDocs`/`fetchDocuments` with `GET /api/jobs/{id}/photos` — the
  same call PhotosView and the invoice picker already make, reading `job_photos`,
  which [core/job_photos.py:1-11](gdx_dispatch/core/job_photos.py#L1-L11) names
  as the one source every photo surface must read.
* Render `<AuthedImage>` thumbnails with `kind`, caption, uploader, timestamp;
  click to open full size.
* "+ Add Photo" keeps posting to `POST /api/jobs/{id}/photos`, then refetches
  **photos**.
* Empty state must distinguish "no photos on this job" from "couldn't load" —
  and, given D7, from "you aren't allowed to see them".

*This alone is the fix for what Doug reported.* 31 photos become visible.

### S2 — one link convention *(fixes D2; code only, no migration)*

`_insert_document` also sets `documents.job_id`. **No backfill** — prod has 0
affected rows. Contract test: after `POST /api/jobs/{id}/photos`, the row comes
back from `GET /api/documents?job_id=`.

### S3 — a way onto the invoice, where invoices are actually made *(fixes D4)*

1. **`/billing/new`**: job-linked invoice → show the job's photos with
   checkboxes, send picks on create. `InvoiceCreateIn` (which is
   `extra="forbid"`) needs the field, and the create path must run the *same*
   ownership validation the PATCH does — not a weaker copy.
2. **InvoiceDetailView**: render whenever `invoice.job_id` exists, with honest
   empty/locked states ("this job has no photos yet" vs "sent invoices can't be
   edited" vs a load failure).
3. **Mobile invoice dialog**: let the tech tick the shots they just took. Best
   picks, least typing, and it is the tech who knows which frame shows the
   finished door.

### S4 — delete the dead mobile writers *(fixes D9)*

Remove **both** routes and the `/tmp` write; update `docs/tech_mobile.md`. No
soft-delete step is needed (0 prod rows). Give `AuthedImage` a real broken-image
fallback so a future dead url shows as failed rather than as a blank frame.

### S5 — the permission fix *(fixes D7)* — **needs Doug's call, see §6**

Either add job-photo reads to the office tier (a `jobs.read`-style check in
`photos.list_job_photos` / `recent_photos` instead of `is_dispatch_manager`), or
give office roles read-only job access. Also make PhotosView treat 404 like 403.

### S6 — signature parity *(fixes D8)* — separate PR, same shape as S1/S2

Flat storage path + `job_id` on the row + read the record, not the document.

---

## 4. Sequencing

`S1` → ship. `S2 + S4` → ship together (small, no data). `S3` → the feature
Doug asked for, once S1 proves the read path. `S5` before anyone creates a
bookkeeper login. `S6` whenever signatures matter.

## 5. Verification

* Backend: pytest per slice; **a role-parametrised test** for `list_job_photos`
  (owner/admin/dispatcher/accounting/technician-on-job/technician-off-job) —
  the audit's point stands that no existing test or fixture exercises a
  non-dispatch role, and both prior "photos are fixed" rounds passed their tests.
* Frontend: vitest per surface, including the three distinct empty states.
* Browser walk on a throwaway (light + dark): mobile upload → office job page
  shows it → tick onto a draft invoice → PDF renders with the photo → mobile
  dialog shows the same picks.
* Re-run the §0 prod queries after deploy; `invoices with attached_photo_ids`
  moving off 0 is the only proof S3 works for real.

## 6. Decisions — answered by Doug 2026-08-12

1. **Auto-attach "after" photos on the closeout autodraft?** — **NO.** The
   office picks; nothing attaches itself.
2. **Permission shape** — **widen the photo endpoints** (done in S5: reads are
   allowed for `jobs.read_all` OR `invoices.read_all`, technicians stay narrowed
   to their own jobs). Role permission sets were not touched.
3. **Customer-facing photos** — **YES.** Built as S7 below.
4. **S6 (signature parity)** — **dropped.** The Signature tab's three stacked
   defects (D8) stay documented here and unfixed, deliberately.

---

## S7 — photos the customer can see (built 2026-08-12)

Two surfaces, because customers arrive two different ways.

**The portal** (`/customer-portal`, one JWT per customer). The Jobs tab now
carries a "N photos" link that opens the job's photo roll. `GET
/portal/jobs/{job_id}/photos` lists them and `GET
/portal/jobs/{job_id}/photos/{photo_id}` serves the bytes — both scoped to the
authenticated customer's own job, image-types only, path fenced inside the
upload root, mirroring the existing estimate-attachment route. The job list
carries a `photo_count` so the link costs no extra request per row.

**The pay page** (`/pay/{token}`, anonymous — the token is the credential). The
photos the office attached to that invoice now render above the card form:
what you're paying for, then the amount, then the card. They are **inlined as
`data:` URIs**, not served from a new endpoint. The first cut did add
`/pay/{token}/photos/{id}`, and the repo's authz sweep failed it — correctly.
That baseline is a ratchet to work down, and an anonymous route to show a
picture is the wrong side of the trade, so the bytes now ride inside a page the
token already unlocks. Capped at 6 photos, downscaled at render; the PDF still
carries the full set.

One resolver (`core/job_photos.resolve_photo_file`) now decides what is
servable for the PDF, the portal and the pay page, so the three cannot disagree
about which photos a customer sees. It refuses non-images, missing files,
deleted documents, and the dead legacy `/mobile/uploads` urls — and it resolves
through the DOCUMENT, never `job_photos.filename`, because the two upload
routes disagree about what that column holds.

**Exposure this creates — worth Doug's eyes.** Every photo on a job is now
visible to that customer in the portal (the pay page is narrower: attached
photos only). There is no "internal only" flag on a photo today, so a shot a
tech takes as internal evidence — damage on arrival, another contractor's mess,
a hazard — is customer-visible the moment it uploads. Prod has 31 photos that
became visible retroactively. If that matters, the fix is a per-photo
"share with customer" toggle defaulting to visible; say the word and it's a
small change.

## 7. Verification (S7)

* Backend: `test_customer_facing_job_photos.py` — 12 tests: another customer's
  job 404s, a photo from a different job 404s, unservable photos are never
  advertised, the pay page shows only attached photos, caps at 6, adds **no**
  public route, and skips an unresolvable photo rather than breaking payment.
* Live isolation, against the throwaway: another customer's job list → 404, my
  own photo → 200, no auth → 401, a foreign photo id on my job path → 404.
* Browser: portal signed in as a real customer account — 2 photos, blob-loaded,
  captions shown; pay page renders both inlined, no broken images, all console
  errors pre-existing Stripe CSP noise.
* Pixel 8: portal photo dialog and pay page both verified in dark mode. The
  walk caught the portal Jobs table not stacking on a phone (PrimeVue 4 dropped
  `responsiveLayout="stack"`), so the photos link moved into the first column
  where a thumb can reach it.

---

## S8 — per-photo sharing, default OFF (Doug 2026-08-12: "per photo default off")

S7 showed a customer every photo on their job. That was the wrong default and
this reverses it. `job_photos.customer_visible` (migration 063, NOT NULL
DEFAULT false) is now the single answer to "may the customer see this photo?",
and it gates all three customer-facing surfaces together — the portal gallery,
the `/pay` page strip and the invoice PDF. Retroactive by construction: every
photo already in the system became internal the moment the migration ran (dev:
8 rows flipped to false; prod: 31 will).

* **Office control** — each photo on the job page's Photos tab reads
  "Internal only" or "Customer can see this" and toggles with one click
  (`PATCH /api/jobs/{id}/photos/{photo_id}`, `customer_visible`). Optimistic,
  but it rolls back on failure: a checkbox that keeps its new position after a
  failed write tells the office a customer can see a photo they can't.
* **Attaching shares** — putting a photo on an invoice IS deciding the customer
  may see it, so the attach path sets the flag. One decision, not two; without
  it the office ticks a photo, watches it not print, and goes hunting for a
  second switch. Un-sharing afterwards still pulls it from the PDF and the pay
  page.
* **The badge counts shared photos only.** A count that included internal ones
  would tell the customer they exist, which is most of what withholding is for.
* **The gate is on the bytes, not just the list** — a customer holding an old
  url stops loading a photo the office took back.
* The audit row for a share records the VALUE, not just the field name: who
  shared a customer's photo and when is the part worth answering later.

**Verified live** on the throwaway: before sharing, the customer's photo list is
`[]` and the pay page renders zero images. The office shares ONE photo (the
finished door) in the browser; the customer then sees exactly that one, the
badge reads 1, the withheld photo's bytes 404 even when requested directly by
the owning customer, and the pay page renders 1. Attaching a fresh internal
photo to a new invoice flipped its flag `f → t`. Re-checked on the Pixel 8: the
portal badge reads "1 photo".

**Note for deploy:** prod has 0 invoices carrying photos, so nothing needs
backfilling — but if that changes before this ships, photos already attached to
a SENT invoice were effectively disclosed in that PDF already, and a one-line
backfill (`customer_visible = true` where the photo is attached to a non-draft
invoice) would keep the pay page consistent with what the customer received.
