# Estimate link: door photos + size labels — plan

**Date:** 2026-08-18 · **Status:** BUILT 2026-08-18 (branch `feat/estimate-link-photos`) — Phases 1+2; Phase 3 declined
**Complaint (Doug):** "The estimate link that we send out does not show the pictures of the
doors. Also the pictures should have a label on them for the size of the door."

---

## 1. What's actually happening (verified in code)

The estimate email carries two things: the PDF attachment and the public link
(`/proposals/{token}`). The two disagree about photos:

| Surface | Shows door photos? |
|---|---|
| Estimate **PDF** (emailed attachment) | ✅ yes — `_estimate_attachments_for_pdf` ([pdf.py:135](../../gdx_dispatch/routers/pdf.py#L135)) embeds every image `Document` attached to the estimate; `estimate_pdf.html` prints an "Attached Photos" grid |
| Estimate **link page** (`ProposalPublicView`) | ❌ no — `_serialize_public_estimate` ([proposals/router.py:181](../../gdx_dispatch/modules/proposals/router.py#L181)) never touches attachments; the payload has no photo field at all |

So the pictures exist (estimate attachments: manual uploads in EstimateView's
attachment panel, plus the CHI capture flow's `_attachCapturedImage` which POSTs the
captured door image to `/api/estimates/{id}/attachments`) — the customer just never
sees them on the page they actually open, because the serializer is an explicit
projection (correctly — it deliberately adds nothing by default) and photos were
never added to it.

The label gap: attachment images have **no label field in use**. The PDF captions each
photo with `original_name` — for captured photos that's a machine name like
`chi-pricing-3f2a….jpg`, useless to a customer picking between a 16×7 and a 9×7.
`Document` already has a nullable `title` column
([tenant_models.py:818](../../gdx_dispatch/models/tenant_models.py#L818)) that nothing
writes — **no migration needed**.

### Prior art to mirror (don't invent)

The public **pay page** solved this exact problem for invoices
(`_invoice_public_photos`, [core/payments.py:685](../../gdx_dispatch/core/payments.py#L685)):

- photos ride **inside the JSON payload as downscaled `data:` URIs**
  (`photo_data_uri`, max 900px / q72, memoised — [core/job_photos.py:142](../../gdx_dispatch/core/job_photos.py#L142)),
  NOT a new anonymous image route. The token already unlocked the page; every extra
  ungated route is another thing to enumerate and get wrong.
- capped (`_PAY_PAGE_MAX_PHOTOS = 6`) so a phone doesn't download megabytes; the PDF
  still carries the complete set.
- **same selection as the PDF** so the page and the attachment can never show the
  customer different pictures.

We copy all three properties.

---

## 2. The plan

### Phase 1 — photos on the public link page (the actual complaint)

**Backend — `gdx_dispatch/modules/proposals/router.py`**

1. New helper `_estimate_public_photos(est, db, tenant_id) -> list[dict]`:
   - select `Document` where `estimate_id == est.id`, `deleted_at IS NULL`,
     `content_type LIKE 'image/%'`, ordered `uploaded_at ASC` — **the identical
     selection + order `_estimate_attachments_for_pdf` uses** (note: the staff
     attachments list endpoint orders DESC; the public page must match the *PDF*,
     not the staff panel).
   - resolve bytes at `UPLOAD_DIR/{tenant}/estimate/{id}/{filename}` (same path
     rule as the PDF helper); skip rows whose file is missing.
   - each row → `{"src": photo_data_uri(path, ct), "label": (doc.title or "").strip()}`.
     `photo_data_uri` is already generic (path + content-type), reuse as-is.
   - cap at `_PROPOSAL_MAX_PHOTOS = 6` (same rationale as the pay page; PDF still
     carries the full set). Best-effort `try/except` + `log.exception` like the
     sibling company/deposit lookups — a broken photo must never 500 the proposal.
2. In `_serialize_public_estimate`, add `body["photos"] = _estimate_public_photos(...)`.
   Explicit projection discipline holds: only `src` + `label` cross the wire — never
   filenames, uploader, or document ids. Customer-safety is already established by
   contract: **everything attached to an estimate already prints on the customer
   PDF**, so this reveals nothing the customer wasn't already sent.

**Frontend — `ProposalPublicView.vue`**

3. New "Photos" section (render whenever `payload.photos.length`), between the
   description and the tiers/lines: responsive thumbnail grid, each image with its
   `label` as an overlay badge (bottom-left, readable in light AND dark mode); tap
   opens the full data URI in a simple lightbox/`<dialog>` (the URI is already
   downscaled to 900px — fine full-screen, no extra fetch).

**Tests**

4. `test_proposals.py`: payload includes `photos` with `src`/`label` for image
   attachments; non-image and soft-deleted documents excluded; missing file skipped;
   cap enforced; no internal fields leak (assert exact key set).
5. Vitest spec for `ProposalPublicView`: gallery renders from `photos`, label badge
   text, section absent when empty.

### Phase 2 — door-size labels

**Where the size comes from:** the CHI capture flow already has the full door spec
at capture time (`draft.line_metadata` — the ADR-013 spec snapshot; `draft.description`
also embeds the size). Manual uploads get a hand-typed label.

6. **Upload accepts a title**: `upload_estimate_attachment`
   ([estimates.py:2633](../../gdx_dispatch/routers/estimates.py#L2633)) gains an
   optional `title: str | None = Form(None)` (cap 255, strip), written to
   `Document.title`. `_serialize_attachment` gains `"title"`.
7. **New `PATCH /api/estimates/{estimate_id}/attachments/{document_id}`** accepting
   `{title}` — auth like the sibling endpoints (`get_current_user` +
   `_get_estimate_or_404`, document must belong to the estimate), audit-logged
   (`estimate_attachment_labeled`). This is how the office fixes/adds labels on
   photos that are already up.
8. **Auto-label at capture**: `_attachCapturedImage` (EstimateView.vue) derives a
   label and sends it as the `title` form field:
   - try width/height keys in `draft.line_metadata`'s spec;
   - else first `NxN` / `N x N` pattern in `draft.description` (e.g. `16x7`);
   - else leave blank for manual edit.
   Format: `16' × 7'` (feet) — **decided by Doug 2026-08-18**. Defensive — the
   metadata shape belongs to the plugin.
9. **Label editing UI**: in EstimateView's attachment list, show the title under the
   thumbnail with inline edit (pencil → text input → PATCH). Tiny; no new component.
10. **PDF caption parity**: `_estimate_attachments_for_pdf` caption becomes
    `doc.title or doc.original_name` — labeled photos print "16' × 7'" instead of
    `chi-pricing-3f2a….jpg`; unlabeled rows keep today's behavior. One-line change
    (`"name": d.title or d.original_name`), template untouched;
    `test_pdf_template_render.py` gets a case.

### Phase 3 — ❌ DECLINED (Doug 2026-08-18: "we just need pictures of what is on the estimate on the estimate link")

Kept below for the record only — NOT being built. The estimate link shows the
estimate's own attachments, nothing else.

#### (out of scope) tech job photos on the estimate

Estimates built from a sales-call job carry `Estimate.job_id`, and the tech's phone
photos live there as `JobPhoto` rows — a completely separate pool from estimate
attachments. If Doug wants those on the quote too: add an "Attach from job photos"
picker in the estimate attachment panel that **copies the picked photo's bytes into
the estimate's upload dir as a normal `Document`** (caption → title). One pool
downstream means Phases 1–2 work unchanged; un-picking is just deleting the
attachment; no new join, no `customer_visible` cross-wiring. **Not building until
Doug confirms he wants tech photos customer-visible on estimates.**

---

## 3. Sizing & sequencing

- Phase 1: ~½ day incl. tests. Phase 2: ~½–1 day (four small touches). Phase 3: ~½ day if wanted.
- **Branch note:** `proposals/router.py` is mid-flight on `feat/deposit-ask-online-pay`
  (deposit-ask work, uncommitted). This work goes on its own branch **after** that PR
  lands — same file, don't entangle.
- No migration anywhere (Document.title exists; JobPhoto untouched).
- Verify per repo practice: pytest + vitest, then a headed-browser walk of a real
  estimate link (light + dark) before calling it done.

## 4. Decisions (all resolved 2026-08-18)

1. **Scope: estimate attachments only.** Doug: "we just need pictures of what is on
   the estimate on the estimate link." Phase 3 (job-photo picker) declined — the
   link mirrors the PDF's photo set, nothing more. Build = Phases 1 + 2.
2. **Label format: `16' × 7'`.**
3. Page photo cap: default 6 (matches /pay; PDF always carries the full set).
