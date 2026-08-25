"""Creating the photo record for a file that was just stored.

Shared because two routes upload a job image — the Documents route (which the
Photos page and the mobile job screen use) and the older job-photo route (which
the desktop job page uses) — and BOTH must produce the JobPhoto record, or the
photo lands nowhere a human can see it.

That is not hypothetical: neither did, and job_photos had 0 rows in production
while the uploads themselves "succeeded".

Documents hold the bytes; job_photos is the photo. Different things, linked.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# The slots a photo can carry. photos.py validates against the same set via its
# own pattern; an unvalidated kind is not merely untidy — the column is
# String(20), so a long value raises on flush, the savepoint swallows it, and
# the photo vanishes silently. That is the original bug wearing the error
# handler that was supposed to fix it.
PHOTO_KINDS = ("before", "during", "after", "progress", "other")
DEFAULT_PHOTO_KIND = "during"


def normalize_kind(kind: object) -> str:
    """Coerce any caller-supplied kind to a slot the column can hold."""
    if isinstance(kind, str) and kind in PHOTO_KINDS:
        return kind
    if kind not in (None, "") and not isinstance(kind, str):
        return DEFAULT_PHOTO_KIND
    if kind:
        log.warning("job_photo_unknown_kind kind=%r -> %s", kind, DEFAULT_PHOTO_KIND)
    return DEFAULT_PHOTO_KIND

def link_job_photo(
    db: Session,
    *,
    tenant_id: str,
    job_id: str,
    document_id: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    uploaded_by: str,
    kind: str | None = None,
    caption: str | None = None,
) -> str | None:
    """Create the JobPhoto record for a just-stored file.

    The url points at the document's download route — the same shape the Photos
    page builds by hand today. Best-effort: a photo that stored its bytes must
    not 500 because the index row failed, but it MUST be logged, because a
    silently-missing record is exactly the failure this closes.

    Returns the new job_photos.id (the id every photo surface keys on), or None
    if the record could not be written — callers that report an id to the
    client need to know which happened.
    """
    from uuid import UUID as _UUID

    from gdx_dispatch.models.tenant_models import JobPhoto

    try:
        # SAVEPOINT: a failure here must not take the document down with it.
        # Bare try/except is not enough — a failed flush leaves the session
        # unusable, so the outer commit would fail too and the tech would lose
        # the upload they were told succeeded.
        with db.begin_nested():
            # JobPhoto.job_id is a Uuid column — binding the raw path string
            # raises rather than parsing.
            photo = JobPhoto(
                company_id=tenant_id,
                job_id=_UUID(str(job_id)),
                kind=normalize_kind(kind),
                url=f"/api/documents/{document_id}/download",
                filename=filename,
                mime_type=content_type,
                size_bytes=size_bytes,
                caption=caption,
                uploaded_by=uploaded_by,
            )
            db.add(photo)
            db.flush()  # assigns the id inside the savepoint
            return str(photo.id)
    except Exception:
        log.exception(
            "job_photo_link_failed job=%s document=%s", job_id, document_id
        )
        return None


# Types a browser will actually render. A customer-facing photo link must never
# become a generic file-serving oracle — same allowlist the portal applies to
# estimate attachments.
RENDERABLE_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")


@lru_cache(maxsize=256)
def _photo_data_uri_cached(path: str, mtime_ns: int, size: int, max_px: int, quality: int) -> str | None:
    """The decode, memoised on (path, mtime, size).

    The pay page is unauthenticated and gets fetched by link scanners, mail
    previewers and the customer reloading — and each render decoded every
    attached photo from scratch. A 12MP phone photo is real CPU, six of them
    per request, on a public URL. The PDF path has always cached its shrink;
    this one has to as well.

    mtime+size in the key means a replaced file re-renders instead of serving
    a stale image, and the bounded LRU keeps a busy tenant from pinning
    hundreds of base64 blobs in memory.
    """
    import base64
    import io

    try:
        from PIL import Image

        from gdx_dispatch.core.images import upright
    except Exception:  # noqa: BLE001 — Pillow missing is not a payment failure
        log.exception("photo_data_uri_pillow_unavailable")
        return None

    # iPhone-native HEIC/HEIF is in the estimate-attachment allowlist but bare
    # Pillow can't decode it — without this, an iPhone photo upload silently
    # vanishes from the page. Registration is idempotent; if the package is
    # absent, behavior degrades to skipping HEIC (today's behavior), not a 500.
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:  # noqa: BLE001, S110 — optional codec; absence is the pre-heif status quo
        pass

    try:
        with Image.open(path) as img:
            # Honor EXIF orientation FIRST — phone portraits carry the rotation
            # as metadata, and thumbnail() alone would serve them sideways.
            # This surface got it right before the others did; it now shares
            # core/images.upright with them so the four re-encoders cannot
            # drift apart again.
            img = upright(img).convert("RGB")
            img.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        # A photo that won't decode must never take the payment page down —
        # the customer is here to pay, not to look at pictures.
        log.exception("photo_data_uri_failed path=%s", path)
        return None


def photo_data_uri(path: str, content_type: str, *, max_px: int = 900, quality: int = 72) -> str | None:
    """A downscaled ``data:`` URI for a photo, or None if it can't be made.

    Used by the public pay page so it can SHOW the work without a public image
    route. Inlining beats adding an endpoint here: the /pay page is
    unauthenticated by design (the token is the credential), and every extra
    anonymous route is another thing to scope correctly, enumerate, and get
    wrong. The bytes ride inside a page you already needed the token to load.

    Downscaled because the alternative is a payment page carrying several
    megabytes of full-resolution door photos over a phone connection, and
    memoised (see _photo_data_uri_cached) because the page is public and gets
    re-fetched by scanners and reloads.
    """
    import os

    try:
        st = os.stat(path)
    except OSError:
        log.warning("photo_data_uri_stat_failed path=%s", path)
        return None
    return _photo_data_uri_cached(path, st.st_mtime_ns, st.st_size, max_px, quality)


def resolve_photo_file(db: Session, photo: object) -> tuple[str, str] | None:
    """Where a JobPhoto's bytes actually live: ``(absolute_path, content_type)``.

    ONE resolver, because three surfaces now need the same answer — the invoice
    PDF, the customer portal, and the public pay page — and they must agree
    about which photos are servable. Getting this subtly different per surface
    is how a photo prints on the PDF but 404s in the portal.

    The path is NOT ``job_photos.filename``: the two upload routes disagree
    about what that column holds (the office route stores the on-disk name, the
    documents route stores the ORIGINAL upload name), so it resolves on one
    path and misses on the other. The url is the reliable link — the canonical
    ``/api/documents/{id}/download`` shape written by link_job_photo — so go
    photo → document → the flat UPLOAD_DIR the download route itself reads.

    Returns None for anything unservable: a legacy /mobile/uploads url, a
    deleted or missing document, a file that isn't on disk, or a content type
    a browser wouldn't render.
    """
    import os
    from pathlib import Path
    from uuid import UUID as _UUID

    from gdx_dispatch.models.tenant_models import Document

    url = getattr(photo, "url", "") or ""
    if not (url.startswith("/api/documents/") and url.endswith("/download")):
        return None
    doc_id = url.removeprefix("/api/documents/").removesuffix("/download")
    try:
        doc_uuid = _UUID(doc_id)
    except (ValueError, AttributeError):
        return None

    doc = db.execute(
        select(Document).where(Document.id == doc_uuid, Document.deleted_at.is_(None))
    ).scalar_one_or_none()
    if doc is None or not doc.filename:
        return None

    content_type = (doc.content_type or "").lower()
    if content_type not in RENDERABLE_IMAGE_TYPES:
        return None

    # realpath + startswith is the form CodeQL recognizes as a path-injection
    # barrier, and the trailing separator stops a sibling dir like
    # "<root>-evil" from passing. doc.filename comes from the DB, never the
    # request — fence it anyway.
    base = os.path.realpath(Path(os.getenv("UPLOAD_DIR", "/app/uploads")))
    full = os.path.realpath(os.path.join(base, doc.filename))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        return None
    return full, content_type

