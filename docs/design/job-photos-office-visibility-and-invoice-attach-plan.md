# Job photos: the office can't see them, and can't put them on an invoice

Status: **RELEASED v1.102.0** (#482 / #485 / #484, follow-up #486) — prod + demo, walked 2026-08-26.
Built: S1 (`JobDetailView.vue:2392`), S2 (`uploads.py:225`), S3
(`InvoiceCreateView.vue`, `InvoiceDetailView.vue`, `MobileInvoiceDialog.vue:187`),
S5 (`photos.py` `_PHOTO_READ_KEYS`), S7 (migration 063), and the `AuthedImage`
fallback. S6 was **dropped** by Doug — not a gap.
**S4** — both orphan routes deleted, with `docs/tech_mobile.md`, `api.d.ts` and
the sibling `tech-mobile-workflow-plan.md` updated in the same change (#485).
**S9** — the EXIF-orientation defect this plan did not know it had (§9): job
photos now print upright on invoice PDFs (#482). **S10** — a size ceiling on
`POST /api/documents` (#484).

**RELEASED v1.102.0 — prod + demo, walked 2026-08-26.** Walk evidence, on the
real photos that had gone out sideways: `INV-000343` / `INV-000348` /
`INV-000356` now embed portrait (`900x1200`, `676x1200`) with **no orientation
tag**; they embedded landscape before. Follow-up #486 corrected an absence test
that asserted a status production never returns (see §S4).
**Still open:** the six unguarded upload sites S10 names but does not fix, the
duplicate `POST /api/jobs/{job_id}/photos` handlers, and a human browser walk —
every walk so far is artifact-level, not a person opening a PDF.
(#483 was the original S4 PR — GitHub auto-closed it when its stacked base
branch was deleted on merge; #485 is the same commit rebased onto main.)

> **§0's table is stale in the project's favour.** It recorded
> `invoices with attached_photo_ids = 0` — "never once been used". As of
> 2026-08-25 prod has **7 invoices carrying 18 photos, all `sent_via='email'`**.
> S3 works. Do not re-derive current state from that table; re-query it.

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

---

## 6.5 Decisions — answered by Doug 2026-08-25

5. **S4: delete or keep the orphan mobile writers?** — **DELETE both.** The
   2026-08-12 note said "repointed rather than deleted" because the handler
   captured EXIF GPS the other paths didn't. That payoff is measurably zero:
   **0 of 183 stored prod images carry GPS or `DateTimeOriginal`**. iOS strips
   location on photo-library uploads by design (WebKit
   [#207088](https://bugs.webkit.org/show_bug.cgi?id=207088), resolved
   "configuration changed" May 2023; opt-in only in the iOS 17+ picker), so
   the capture code can never fire for a PWA on a phone. Nothing is lost.
6. **Re-send the 7 invoices that went out with rotated photos?** — **NO.**
   They stay as delivered. There is no stored-PDF column — every invoice PDF
   is generated on demand at four call sites — so any later re-fetch of those
   invoices renders correctly once §9 lands. Nothing to clean up.
7. **Compress job photos on ingest?** — **NO.** Full fidelity is kept. A job
   photo is evidence someone may need to zoom into, and the re-encode cannot be
   undone. If page weight becomes a problem the answer is a derived thumbnail
   beside the original, never a smaller original. Settled; see §S10.

---

## 9. S9 — the photos print sideways (found 2026-08-25, not in the original plan)

**Nine of the eighteen photos already emailed to customers printed rotated 90°.**

A phone stores a portrait shot as LANDSCAPE pixels plus an EXIF `Orientation`
tag. Everything we ship to honors that tag — browsers default to
`image-orientation: from-image` ([Baseline since April 2020][mdn]), and
WeasyPrint has applied it since 57.0 (prod runs **69.0**, measured). So the
bytes on disk are sideways and every viewer straightens them.

Then something re-encodes the file. Pillow's `save()` drops EXIF unless handed
back explicitly, so a resize pass writes the **unrotated pixels with the
instruction deleted**. Nothing downstream can recover the rotation, because
nothing downstream is told there was any.

Measured on the prod runtime, on a real photo from a real emailed invoice:

```
source pixels    : (4000, 2252)   EXIF Orientation: 6
after _shrink_photo_for_pdf: (1200, 676)   orientation tag: None

WeasyPrint on the ORIGINAL file  : (2252, 4000)  <- what the customer SHOULD see
WeasyPrint on the shrunk output  : (1200, 676)   <- what the customer GOT
```

The irony is load-bearing: **WeasyPrint rendered it correctly on its own.**
The optimization is the sole cause.

### Blast radius (prod, 2026-08-25, all three gates applied)

`customer_visible` (pdf.py:281), the 2.5 MB email-attachment drop, and
mailed-vs-marked-sent were each checked — none reduced the count:

```
TOTAL attached=18  rendered_into_pdf=18  rendered_and_rotated=9
ROTATED PHOTOS IN A PDF ACTUALLY EMAILED TO A CUSTOMER = 9
```

### Sibling sweep

**Shape:** *re-encoding an uploaded photo with Pillow without first applying
`ImageOps.exif_transpose`, destroying the orientation tag every downstream
renderer would have honored.*
**Surface searched:** every `Image.open` / `.thumbnail(` / `.resize(` in the
tree outside tests and migrations, plus `proposals/router.py` traced to its
helper. **3 of 4 broken.**

| Site | Serves | Before |
| --- | --- | --- |
| `routers/pdf.py` `_shrink_photo_for_pdf` | invoice PDF → **customer** | 🔴 9 live |
| `routers/uploads.py` `_compress_image` | office job page | 🔴 **irreversible** |
| `modules/door_listings/service.py` `compress_for_web` | **public website** | 🔴 latent |
| `core/job_photos.py` `_photo_data_uri_cached` | pay page, proposals | ✅ correct |

`_compress_image` is the worst of the three even with zero prod rows: it
re-encodes on the way **in**, so a photo stored by the old code could never be
straightened afterwards. The invoice-PDF instance was recoverable — the stored
originals still carry their tags — that one was not.

### The fix, and the trap inside it

One shared helper, `core/images.py::upright()`, adopted at all three broken
sites; the correct site is refactored onto it so the four cannot drift again.

**The contract has two halves, and half two is not tidiness.** `upright()`
bakes the rotation into the pixels; if a later change also writes the original
EXIF back, every viewer rotates a **second** time:

```
target (correct display)   : (100, 400)
transpose + drop exif      : (100, 400)   <- correct
transpose + PRESERVE exif  : (400, 100)   <- DOUBLE ROTATION
```

This is the industry contract, not a local invention — imgproxy documents that
it auto-rotates on EXIF and "the orientation tag will be removed from the image
in all cases". `tests/test_photo_exif_orientation.py` asserts **both** halves at
every site; the tag assertion is what makes a future "let's preserve the
metadata" commit fail loudly instead of silently reopening this.

Two further traps recorded so they are not rediscovered:

* **The PDF cache.** `_shrink_photo_for_pdf` invalidates on the **source**
  mtime, which does not move when the encoder is fixed. A container that had
  already rendered an invoice would keep serving the pre-fix JPEG. Hence
  `_PDF_PHOTO_CACHE_VERSION`. (`/tmp` is container-local and wiped on
  `--force-recreate`, so a deploy clears it anyway — the salt is for the
  running container and for local dev.)
* **`exif_transpose` raises.** Pillow
  [#5580](https://github.com/python-pillow/Pillow/issues/5580) (KeyError while
  removing the orientation tag), #4238, #3973. `upright()` is deliberately
  written as `ImageOps.exif_transpose(img) or img` inside a `try` — the `or`
  covers the `None` case, the `except` the raising ones. Do not "clean it up".

### Do not rebuild: prior art checked 2026-08-25

* **The fix is Pillow's documented one-liner.** `ImageOps.exif_transpose`,
  stable since 6.0 (2019); prod runs Pillow 12.3.0.
* **Not ImageMagick `-auto-orient`** — open bugs specifically on Orientation 6,
  which is 9 of 9 of our affected files.
* **Not imgproxy / thumbor / libvips** — all are services or daemons.
  Standing up an image proxy for a single-tenant app to fix a missing one-line
  call is disproportionate; Pillow is already a dependency and already does
  this correctly in one file in this repo.
* **No prebuilt lint rule exists** for "`Image.open` without `exif_transpose`"
  (searched the semgrep community rules). Recurrence guard, if wanted, is a
  repo-local scanner in the existing `.{name}_baseline` style — and per the
  working agreement it only counts as evidence once it can be shown going red.

[mdn]: https://developer.mozilla.org/en-US/docs/Web/CSS/image-orientation

### What the adversarial review of the diff changed (2026-08-25)

The reviewer's headline was that some of the 18 invoice photos might have come
through the office uploader — whose old `_compress_image` capped at 2048px and
stripped EXIF — making them **permanently** sideways and the "no re-send"
answer above false. Checked, and it does not hold:

* All 55 prod `job_photos` rows resolve to documents with `entity_type` **NULL**
  — the signature of `POST /api/documents`, which never sets it. The office
  route (`uploads.py`, which *does* write `entity_type='job_photo'`) has
  produced **zero** rows.
* All nine rotated files are 4000px with `Orientation=6` intact. The one file
  matching the compressed shape (1536x2048, no EXIF) has **portrait pixels**,
  so it displays correctly either way.

"Recoverable" and "no re-send" stand — on row-level evidence rather than on the
audit-count inference that was cited for them first.

Three real holes it found in the tests are closed in the same commit: the cache
salt had no guard (deleting it left every test green), no fixture was large
enough to exercise a resize on a rotated image, and no test covered
`compress_for_web`'s fail-closed path, where a raising transpose would turn
every door-listing upload into a 422. It also noted `upright()` copied even when
there was nothing to apply — a second decoded frame per upload for nothing —
now short-circuited on `Orientation == 1`.

**Filed, not bundled — `/api/jobs/{job_id}/photos` has two POST handlers.**
Enumerated through `tests/conftest.py::iter_app_routes` (a flat `app.routes`
walk cannot see them):

```
['POST'] /api/jobs/{job_id}/photos  routers.uploads.upload_job_photo    <- wins
['POST'] /api/jobs/{job_id}/photos  routers.photos.create_job_photo     <- shadowed, dead
['GET']  /api/jobs/{job_id}/photos  routers.photos.list_job_photos
```

First include wins, so `photos.create_job_photo` is unreachable. That is the
same defect class as S4's orphan routes and belongs with them, not here.

---

## S4 — what shipped (2026-08-25)

Deleted `upload_mobile_job_photo` and both of its routes
(`POST /api/mobile/jobs/{id}/photos` and the alias `POST /api/mobile/job/{id}/photo`),
plus the helpers that existed only to serve it: `_photo_exif_metadata`,
`_VALID_PHOTO_KINDS`, and `_image_suffix` — 254 lines from `routers/mobile.py`.

Also removed the two path entries from `frontend/src/types/api.d.ts`, rewrote
`gdx_dispatch/docs/tech_mobile.md` to name `POST /api/documents` as the photo
endpoint with a "do not re-add these" note, and marked the **§"Photo capture"
section of `tech-mobile-workflow-plan.md` SUPERSEDED** — that doc is `RELEASED`
and still specified the deleted route as *the* mobile photo endpoint, which is
exactly how two plans in this repo previously reached opposite conclusions
about the same path without citing each other.

**The evidence that made this a deletion and not a risk:**

| Question | Answer |
| --- | --- |
| Does any frontend call it? | No — `usePhotoQueue` posts to `/api/documents` |
| Did prod ever use it? | No — all 55 `job_photos` resolve to documents with `entity_type` NULL, the `/api/documents` signature; the route's own audit action has zero rows |
| Was its unique feature worth keeping? | No — 0 of 183 stored prod images carry EXIF GPS or capture time |
| Why is that permanent? | iOS strips location from photo-library uploads by design (WebKit #207088, resolved May 2023; opt-in only in the iOS 17+ picker) |

The replacement test asserts **absence**, not presence: that both routes 404 and
that the handler and its EXIF helper are gone from the module. A presence test
would only prove someone typed the route name; absence is the property that can
actually regress — the moment somebody "restores" the endpoint instead of
pointing a caller at `/api/documents`.

**Still open from this shape:** `POST /api/jobs/{job_id}/photos` has two
handlers (`uploads.upload_job_photo` wins; `photos.create_job_photo` is
shadowed and unreachable). Same class, filed separately — not bundled here.

---

## S10 — a ceiling on the route every photo actually uses (2026-08-25)

`POST /api/documents` — the path `usePhotoQueue` posts to, and where all 55
production job photos came from — read the body with **no application ceiling
at all**. Its sibling `POST /api/jobs/{id}/photos` has capped photos at 10 MB
since it shipped; this one never did. Now capped at **25 MB**.

### What this cap does NOT do — corrected after adversarial review

The first version of this section, and the first implementation, claimed the
upload was "refused as it arrives" by reading in chunks and stopping at the
ceiling. **That was false, and the chunked read made things worse.** Measured
on this stack (starlette 1.6.0 / fastapi 0.141.1), at handler entry for a
30 MB post:

```
type = SpooledTemporaryFile   _rolled = True
on_disk_bytes = 31457280      UploadFile.size = 31457280
```

FastAPI awaits `request.form()` **before** the endpoint is called, so the whole
body has already crossed the wire and been spooled to a temp file. No handler
can refuse an upload "as it arrives" — only `client_max_body_size` can, and
prod nginx allows **50M**. Lowering that is the change that would match the
original claim; it is infra, not in this repo, and not done here.

Worse, the chunked read peaked at **2x** the body it was protecting:

```
bare file.read()       peak = 25.0 MB
chunk-and-join         peak = 50.0 MB
```

because it holds the chunk list *and* the joined result — and in an `async def`
it turned one blocking disk read into twenty-five with no await between them.

So the guard is now an **O(1) check on `UploadFile.size`**, which Starlette has
already computed. It allocates nothing. What it actually prevents is the
handler pulling an oversized body into process memory and persisting it under a
Document row — which is worth doing, and is all it claims.

`test_starlette_still_reports_upload_size_at_handler_entry` pins the assumption
the guard rests on: if an upgrade stopped populating `size`, the check would
silently fall back to seek/tell and nothing else would notice.

### Why 25 MB and not the sibling's 10 MB

Real tech photos in production already reach **10 MB exactly** (avg 5.3 MB). A
10 MB cap would start refusing uploads that succeed today, and a photo rejected
in a customer's driveway is worse than a large one stored. `DocumentsView.vue`
already refuses >25 MB client-side and `usePhotoQueue` treats a 413 as
permanent, so this is a **server-side backstop, not new user-facing behavior**.

Both directions are pinned: over the cap must 413 *and leave no bytes on disk*;
at the cap must still 201. A guard that refused everything would satisfy the
first test while breaking every upload in the app.

### Sibling sweep — six more instances, FILED not fixed

**Shape:** *an upload handler reading the request body with no size ceiling.*
**Surface:** every router with an upload endpoint. Beyond this route, six sites
do `await file.read()` with no 413 anywhere in the file:

```
routers/admin_ops.py:356        routers/resources.py:245
routers/vendor_invoices.py:245  routers/vendor_statements.py:253
routers/catalog.py:1673         routers/voice.py:141
```

They are **not** fixed here, deliberately. Each has different content
semantics and a different defensible ceiling, and bolting a uniform guard onto
six unfamiliar routers inside a photo-path change is precisely the bundling the
working agreement forbids. Recorded here so the class is not silently dropped.

Also unfixed, same shape: `uploads.py::_read_upload_with_limit` reads the whole
body *then* checks `len()`. Harmless given the above — nothing can be refused
pre-arrival anyway — but it should use the same O(1) check.

### Compression on ingest — ⛔ DECIDED: NO (Doug, 2026-08-26)

The original plan for this slice proposed running `_compress_image` on the
`as_photo` branch, downscaling to 2048px. **It was raised as a decision rather
than taken as a default, and Doug answered no.** Job photos stay stored at
full fidelity.

This is settled — do not re-propose it as an optimisation. The reason it looks
attractive is real (prod photos average 5.3 MB and the office job page loads
them as thumbnails), and that will keep making it look like a free win. It is
not free: a job photo is evidence a tech may need to zoom into, the re-encode
is irreversible, and it changes customer-facing output on the portal and the
invoice PDF. If page weight becomes the problem, the answer is a **derived
thumbnail** served alongside the original, never a smaller original.

See also §6.5 decision 7.

Orientation needs no work here: this route stores raw bytes, so the EXIF tag
survives and every viewer — including the invoice PDF, after S9 — renders these
upright already.
