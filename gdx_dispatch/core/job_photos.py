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
) -> None:
    """Create the JobPhoto record for a just-stored file.

    The url points at the document's download route — the same shape the Photos
    page builds by hand today. Best-effort: a photo that stored its bytes must
    not 500 because the index row failed, but it MUST be logged, because a
    silently-missing record is exactly the failure this closes.
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
            db.add(JobPhoto(
                company_id=tenant_id,
                job_id=_UUID(str(job_id)),
                kind=normalize_kind(kind),
                url=f"/api/documents/{document_id}/download",
                filename=filename,
                mime_type=content_type,
                size_bytes=size_bytes,
                caption=caption,
                uploaded_by=uploaded_by,
            ))
    except Exception:
        log.exception(
            "job_photo_link_failed job=%s document=%s", job_id, document_id
        )

