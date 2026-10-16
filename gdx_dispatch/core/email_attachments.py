"""User-chosen attachments for outgoing email — one resolver, one access rule.

The inbox's new / reply / forward, on desktop and phone, lets the sender
pick attachments from four places:

- a file from their own device (``upload``: the bytes ride the request);
- a stored job photo or document (``job_photo`` / ``document``);
- any of the customer's estimates or invoices, rendered as the same PDF the
  customer gets (``estimate_pdf`` / ``invoice_pdf``) — so one email can carry
  several estimates for the same customer.

Stored things travel BY REFERENCE: the client names an id, the server loads
the bytes. That keeps a phone from downloading a PDF only to upload it again,
and it means the server, not the client, decides what the sender may reach.

**One access rule, used twice.** :func:`list_attachment_sources` (what the
picker offers) and :func:`resolve_attachment_refs` (what a send accepts) both
ask :class:`AttachScope`, and both find a file through
:func:`_document_path` — so the picker never offers a record the caller
cannot reach or whose file is gone, and a hand-built request can never reach
something the picker would hide. (A listed photo can still be refused at send
by :func:`_clean_photo`: over 64MP, or one that cannot be cleaned of its
location.) The rule follows the read gates that already exist:

- job photos and job documents: ``jobs.read_all`` / ``invoices.read_all``
  (the photo router's ``_PHOTO_READ_KEYS``), else the caller's own jobs
  (:func:`core.job_access.user_job_ids`, the same set the mobile app uses);
- estimates: ``estimates.read_all``, else estimates on the caller's own jobs
  or for the customers of those jobs (``estimates.read_own``);
- invoices: ``invoices.read_all``, else invoices on the caller's own jobs
  (the mobile invoicing ``_tech_owned_invoice`` rule);
- customer documents: ``customers.read_all``, else customers of own jobs;
- a document attached to nothing: office tier only.

Anything the caller cannot reach answers 404 — the same non-disclosure
posture as every other read — and the whole send stops. A user picked these
files; silently dropping one and sending the rest would be a send that
"succeeds" without doing what was asked.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.link_sms import as_uuid
from gdx_dispatch.core.permissions import WILDCARD

log = logging.getLogger(__name__)

# Total raw bytes one email may carry in user-chosen attachments. Exchange
# Online's default send limit is 35 MB *encoded*, and the receiving side is the
# tighter bound: Gmail refuses over 25 MB. Base64 adds a third, so 18 MB raw is
# ~24 MB on the wire — under both, with room for the body.
MAX_EMAIL_ATTACHMENTS_BYTES = 18_000_000

# One device file. Phone photos are downscaled on the device before they get
# here (frontend/src/utils/emailAttachments.js); this bounds what is left —
# a PDF or a video clip.
MAX_UPLOAD_ATTACHMENT_BYTES = 15_000_000

# Base64 of MAX_UPLOAD_ATTACHMENT_BYTES, rounded up.
_MAX_UPLOAD_BASE64_CHARS = 20_000_004

AttachmentKind = Literal["upload", "document", "job_photo", "estimate_pdf", "invoice_pdf"]


class AttachmentRef(BaseModel):
    """One attachment the sender picked. ``upload`` carries its bytes; every
    other kind names a stored record by ``id``."""

    model_config = ConfigDict(extra="forbid")
    kind: AttachmentKind
    id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    content_type: str | None = Field(default=None, max_length=120)
    content_base64: str | None = Field(default=None, max_length=_MAX_UPLOAD_BASE64_CHARS)

    @model_validator(mode="after")
    def _shape(self) -> AttachmentRef:
        if self.kind == "upload":
            if not (self.name and self.content_base64):
                raise ValueError("an uploaded attachment needs a name and its bytes")
        elif not self.id:
            raise ValueError(f"a {self.kind} attachment needs an id")
        return self


# ── who may reach what ──────────────────────────────────────────────────


def _uid(user: Any) -> str:
    u = user or {}
    if isinstance(u, dict):
        return str(u.get("user_id") or u.get("sub") or u.get("id") or "")
    return str(getattr(u, "user_id", "") or getattr(u, "sub", "") or "")


def _hex(value: Any) -> str:
    """Engine-neutral id key: SQLite stores a Uuid as 32 dashless hex and
    Postgres renders it dashed, so compare on the dashless form."""
    key = as_uuid(value)
    return key.hex if key is not None else str(value or "")


@dataclass
class AttachScope:
    """What one caller may attach. Built once per request."""

    perms: set[str]
    own_job_ids: set[str] = field(default_factory=set)       # dashless hex
    own_customer_ids: set[str] = field(default_factory=set)  # dashless hex

    def _has(self, *keys: str) -> bool:
        return WILDCARD in self.perms or any(k in self.perms for k in keys)

    @property
    def all_jobs(self) -> bool:
        return self._has("jobs.read_all", "invoices.read_all")

    @property
    def all_estimates(self) -> bool:
        return self._has("estimates.read_all")

    @property
    def all_invoices(self) -> bool:
        return self._has("invoices.read_all")

    @property
    def all_customers(self) -> bool:
        return self._has("customers.read_all")

    @property
    def office(self) -> bool:
        return self._has("jobs.read_all", "customers.read_all")

    def job(self, job_id: Any) -> bool:
        return job_id is not None and (self.all_jobs or _hex(job_id) in self.own_job_ids)

    def customer(self, customer_id: Any) -> bool:
        return customer_id is not None and (
            self.all_customers or _hex(customer_id) in self.own_customer_ids
        )

    def estimate(self, est: Any) -> bool:
        if est is None or getattr(est, "deleted_at", None) is not None:
            return False
        if self.all_estimates:
            return True
        return (est.job_id is not None and _hex(est.job_id) in self.own_job_ids) or (
            est.customer_id is not None and _hex(est.customer_id) in self.own_customer_ids
        )

    def invoice(self, inv: Any) -> bool:
        if inv is None or getattr(inv, "deleted_at", None) is not None:
            return False
        if self.all_invoices:
            return True
        return inv.job_id is not None and _hex(inv.job_id) in self.own_job_ids

    def document(self, db: Session, doc: Any) -> bool:
        if doc is None or getattr(doc, "deleted_at", None) is not None:
            return False
        if doc.job_id is not None:
            return self.job(doc.job_id)
        if doc.estimate_id is not None:
            from gdx_dispatch.modules.proposals.models import Estimate  # noqa: PLC0415

            return self.estimate(db.get(Estimate, doc.estimate_id))
        if doc.customer_id is not None:
            return self.customer(doc.customer_id)
        return self.office


def attach_scope(db: Session, request: Request, user: Any, tenant_id: str) -> AttachScope:
    """The caller's reach, from the same permission set ``require_permission``
    resolves and the same own-jobs set the mobile app lists."""
    from gdx_dispatch.core.job_access import _permissions, user_job_ids  # noqa: PLC0415
    from gdx_dispatch.models.tenant_models import Job  # noqa: PLC0415

    perms = set(_permissions(db, request, user) or ())
    scope = AttachScope(perms=perms)
    if WILDCARD in perms:
        return scope
    own = {_hex(j) for j in user_job_ids(db, tenant_id, _uid(user))}
    scope.own_job_ids = own
    if own:
        ids = [u for u in (as_uuid(j) for j in own) if u is not None]
        if ids:
            rows = db.execute(
                select(Job.customer_id).where(Job.id.in_(ids), Job.customer_id.is_not(None))
            ).scalars().all()
            scope.own_customer_ids = {_hex(c) for c in rows if c}
    return scope


# ── bytes ───────────────────────────────────────────────────────────────


def _safe_name(name: str | None, fallback: str) -> str:
    """A filename a mail client will show as-is: no path, no control chars."""
    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    base = "".join(ch for ch in base if ch.isprintable()).strip().strip(".")
    return (base or fallback)[:200]


def _document_path(doc: Any, tenant_id: str) -> str | None:
    """Where a Document's stored file is, or None when it is gone.

    Two layouts exist: estimate attachments live under the estimate's own
    directory (routers/estimates.py ``_attachment_dir``); everything else is
    flat in UPLOAD_DIR (routers/documents.py ``download_document``). Both are
    realpath-contained, because ``filename`` is a column, not a constant.
    """
    root = os.path.realpath(os.getenv("UPLOAD_DIR", "/app/uploads/"))
    if doc.estimate_id is not None:
        base = os.path.realpath(
            os.path.join(root, str(tenant_id), "estimate", str(doc.estimate_id))
        )
        if not base.startswith(root + os.sep):
            return None
    else:
        base = root
    fullpath = os.path.realpath(os.path.join(base, doc.filename or ""))
    if not fullpath.startswith(base + os.sep) or not os.path.isfile(fullpath):
        return None
    return fullpath


def _document_bytes(doc: Any, tenant_id: str) -> bytes | None:
    """A Document's stored bytes, or None when the file is gone."""
    fullpath = _document_path(doc, tenant_id)
    if fullpath is None:
        return None
    # Present but unreadable is a server fault, not "not found": let it raise.
    with open(fullpath, "rb") as fh:
        return fh.read()


def _photo_document_id(photo: Any) -> Any:
    """The Document id in a photo's canonical download url — the link the
    photo resolver in core/job_photos.py also reads (``filename`` disagrees
    between the two upload routes, so it is not used). None for a legacy
    ``/mobile/uploads`` url, which no surface can serve."""
    url = getattr(photo, "url", "") or ""
    if not (url.startswith("/api/documents/") and url.endswith("/download")):
        return None
    return as_uuid(url.removeprefix("/api/documents/").removesuffix("/download"))


def _photo_document(db: Session, photo: Any) -> Any:
    """photo → its live Document, or None."""
    from gdx_dispatch.models.tenant_models import Document  # noqa: PLC0415

    doc_id = _photo_document_id(photo)
    if doc_id is None:
        return None
    doc = db.get(Document, doc_id)
    return None if doc is None or doc.deleted_at is not None else doc


def _estimate_pdf(db: Session, est: Any, tenant_id: str) -> bytes:
    from gdx_dispatch.models.tenant_models import Customer  # noqa: PLC0415
    from gdx_dispatch.routers.estimates import _estimate_pdf_bytes  # noqa: PLC0415

    cust = db.get(Customer, est.customer_id) if est.customer_id else None
    return _estimate_pdf_bytes(db, est, cust, str(est.company_id or tenant_id))


def _invoice_pdf(db: Session, inv: Any) -> bytes:
    from gdx_dispatch.routers.pdf import invoice_pdf_bytes  # noqa: PLC0415

    return invoice_pdf_bytes(inv, db)


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Attachment not found: {what}")


def _wire(name: str, content_type: str | None, data: bytes) -> dict[str, Any]:
    return {
        "name": name,
        "content_type": content_type or "application/octet-stream",
        "content_base64": base64.b64encode(data).decode("ascii"),
        "size_bytes": len(data),
    }


def decode_base64_attachment(name: str | None, content_base64: str | None) -> bytes:
    """The bytes of a base64 attachment, or a 422 naming the file."""
    try:
        return base64.b64decode(content_base64 or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=f"Attachment {name!r} is not valid base64"
        ) from exc


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


#: Photos re-encoded on the way out, whatever the file says it is: the bytes
#: decide, not the name or the declared type (a JPEG can arrive as
#: ``application/octet-stream`` or named ``.pdf``). Any other image Pillow
#: reads goes as stored only when it carries no GPS (:func:`_clean_photo`).
_PHOTO_FORMATS = {"JPEG", "MPO", "WEBP", "HEIF", "PNG", "AVIF"}
_PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif", ".tif", ".tiff")
#: Longest side kept. Larger than any inbox shows; a 48MP phone mode would
#: otherwise go out as a 20MB file.
_PHOTO_MAX_SIDE = 4096
#: Refused from the header before decoding (the mms.py decompression-bomb
#: ceiling): a tiny flat PNG can decode to gigabytes.
_PHOTO_MAX_PIXELS = 64_000_000


#: Uploads are stored as ``<uuid4 hex>-<name>`` (routers/uploads.py).
_STORED_PREFIX = re.compile(r"^[0-9a-f]{32}-")


def _shown_name(filename: str | None) -> str | None:
    """The name the person uploaded, not the one it is stored under."""
    return _STORED_PREFIX.sub("", filename) if filename else filename


def _photo_too_large(name: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=f"{name} is too large a photo to email — use the camera's normal size.",
    )


#: The EXIF block header, as JPEG (APP1), HEIC, WebP and PNG all carry it.
_EXIF_MARK = b"Exif\x00\x00"
_GPS_IFD = 0x8825


def _location_refused(name: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=(
            f"{name} may carry the location it was taken at and can't be cleaned "
            "here — save it as a JPEG and attach it again."
        ),
    )


def _looks_like_photo(name: str, content_type: str | None, data: bytes) -> bool:
    return (
        (content_type or "").startswith("image/")
        or name.lower().endswith(_PHOTO_EXTS)
        or data[:3] == b"\xff\xd8\xff"  # JPEG, whatever it is called
    )


def _as_stored(name: str, content_type: str | None, data: bytes) -> tuple[str, str | None, bytes]:
    """A photo Pillow could not clean, leaving exactly as stored — refused
    when its bytes hold an EXIF block, which may hold a GPS position."""
    if _EXIF_MARK in data:
        raise _location_refused(name)
    return name, content_type, data


def _clean_photo(name: str, content_type: str | None, data: bytes) -> tuple[str, str | None, bytes]:
    """A photo as it should leave us: turned upright, with no EXIF — so no GPS
    position of the customer's driveway or the tech's truck — and HEIC, WebP
    or AVIF as a JPEG any mail client opens. The same rule every other
    re-encoder follows (core/images.py, modules/phone_com/mms.py).

    Every file is offered to Pillow, whatever its name or declared type.
    - A re-encodable photo (``_PHOTO_FORMATS``) is re-encoded; one too large
      to decode safely is refused (422).
    - Any other image Pillow reads (TIFF, GIF, BMP…) goes as stored unless its
      EXIF holds a GPS block; then it is refused.
    - A file Pillow cannot read goes as stored — unless it looks like a photo
      (type, extension or JPEG magic) and carries an EXIF block: a damaged
      photo is refused rather than sent with its location.
    A PDF or Word file with a photo embedded inside it is not opened; its
    contents are the sender's document, sent as written."""
    try:
        from PIL import Image  # noqa: PLC0415

        from gdx_dispatch.core.images import upright  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — no Pillow: send what we were given
        log.exception("email_attachment_pillow_unavailable")
        return _as_stored(name, content_type, data)
    try:
        from pillow_heif import register_heif_opener  # noqa: PLC0415

        register_heif_opener()
    except Exception:  # noqa: BLE001 — optional codec; HEIC then refused or sent per _as_stored
        log.debug("pillow_heif_unavailable — HEIC attachments cannot be cleaned", exc_info=True)
    try:
        opened = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise _photo_too_large(name) from exc
    except Exception:  # noqa: BLE001 — not a format Pillow knows
        if not _looks_like_photo(name, content_type, data):
            return name, content_type, data
        log.info("email_attachment_photo_not_opened name=%s", name)
        return _as_stored(name, content_type, data)
    try:
        with opened as img:
            fmt = (img.format or "").upper()
            if fmt not in _PHOTO_FORMATS:
                # The GPS pointer's presence, not its parse: a malformed IFD
                # would raise into the damaged-file path, whose byte check a
                # TIFF (no "Exif" header) never trips.
                if _GPS_IFD in img.getexif():
                    raise _location_refused(name)
                return name, content_type, data
            if img.width * img.height > _PHOTO_MAX_PIXELS:
                # Not sent as-is: it would leave with its EXIF, GPS and all.
                raise _photo_too_large(name)
            if fmt in ("JPEG", "MPO"):
                img.draft("RGB", (_PHOTO_MAX_SIDE, _PHOTO_MAX_SIDE))
            img.thumbnail((_PHOTO_MAX_SIDE, _PHOTO_MAX_SIDE))
            img = upright(img)
            buf = io.BytesIO()
            if fmt == "PNG":
                # A screenshot stays a lossless PNG; saving without exif=
                # drops any eXIf chunk.
                img.save(buf, format="PNG", optimize=True)
                stem, dot, _ext = name.rpartition(".")
                return (f"{stem}.png" if dot else f"{name}.png"), "image/png", buf.getvalue()
            img.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True)
    except HTTPException:
        raise
    except Image.DecompressionBombError as exc:
        raise _photo_too_large(name) from exc
    except Exception:  # noqa: BLE001 — damaged: it can still go, but only without EXIF
        log.warning("email_attachment_photo_not_decoded name=%s", name, exc_info=True)
        return _as_stored(name, content_type, data)
    stem, dot, ext = name.rpartition(".")
    if dot and ext.lower() not in ("jpg", "jpeg"):
        name = f"{stem}.jpg"
    elif not dot:
        name = f"{name}.jpg"
    return name, "image/jpeg", buf.getvalue()


def _photo_wire(name: str, content_type: str | None, data: bytes) -> dict[str, Any]:
    return _wire(*_clean_photo(name, content_type, data))


def resolve_attachment_refs(
    db: Session,
    request: Request,
    user: Any,
    tenant_id: str,
    refs: list[AttachmentRef] | None,
    *,
    budget_bytes: int = MAX_EMAIL_ATTACHMENTS_BYTES,
    scope: AttachScope | None = None,
) -> list[dict[str, Any]]:
    """Load every picked attachment as ``{name, content_type, content_base64,
    size_bytes, kind, ref_id}`` — the wire shape transactional_email and the Graph sender
    take — or raise before anything is sent.

    404 for anything the caller cannot reach (or that is gone); 413 when the
    set is over ``budget_bytes``, naming the total so the sender knows what to
    remove. Never a partial list.
    """
    if not refs:
        return []
    from gdx_dispatch.models.tenant_models import Document, Invoice, JobPhoto  # noqa: PLC0415
    from gdx_dispatch.modules.proposals.models import Estimate  # noqa: PLC0415

    scope = scope or attach_scope(db, request, user, tenant_id)
    out: list[dict[str, Any]] = []
    total = 0
    for ref in refs:
        if ref.kind == "upload":
            data = decode_base64_attachment(ref.name, ref.content_base64)
            if len(data) > MAX_UPLOAD_ATTACHMENT_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"{ref.name} is {_mb(len(data))}; one file can be at most "
                        f"{_mb(MAX_UPLOAD_ATTACHMENT_BYTES)}."
                    ),
                )
            item = _photo_wire(_safe_name(ref.name, "attachment"), ref.content_type, data)
        else:
            key = as_uuid(ref.id)
            if key is None:
                raise _not_found(str(ref.id))
            if ref.kind == "document":
                doc = db.get(Document, key)
                if not scope.document(db, doc):
                    raise _not_found(str(ref.id))
                data = _document_bytes(doc, tenant_id)
                if data is None:
                    raise _not_found(doc.original_name or str(ref.id))
                item = _photo_wire(_safe_name(doc.original_name, "document"), doc.content_type, data)
            elif ref.kind == "job_photo":
                photo = db.get(JobPhoto, key)
                if photo is None or photo.deleted_at is not None or not scope.job(photo.job_id):
                    raise _not_found(str(ref.id))
                doc = _photo_document(db, photo)
                data = _document_bytes(doc, tenant_id) if doc is not None else None
                if data is None:
                    raise _not_found(photo.caption or photo.filename or str(ref.id))
                item = _photo_wire(
                    _safe_name(doc.original_name or photo.filename, "photo.jpg"),
                    doc.content_type or photo.mime_type,
                    data,
                )
            elif ref.kind == "estimate_pdf":
                est = db.get(Estimate, key)
                if not scope.estimate(est):
                    raise _not_found(str(ref.id))
                data = _estimate_pdf(db, est, tenant_id)
                item = _wire(
                    f"estimate-{est.estimate_number or str(est.id)[:8]}.pdf", "application/pdf", data
                )
            else:  # invoice_pdf
                inv = db.get(Invoice, key)
                if not scope.invoice(inv):
                    raise _not_found(str(ref.id))
                data = _invoice_pdf(db, inv)
                item = _wire(
                    f"invoice-{inv.invoice_number or str(inv.id)[:8]}.pdf", "application/pdf", data
                )
        item["kind"] = ref.kind
        item["ref_id"] = ref.id
        total += item["size_bytes"]
        # Refused as soon as the set is over, before the rest is loaded or
        # rendered — 25 refs of a large document would otherwise all sit in
        # memory first.
        if total > budget_bytes:
            raise attachments_over_budget(total, budget_bytes)
        out.append(item)
    return out


def attachments_over_budget(total: int, budget_bytes: int) -> HTTPException:
    """The 413 for a set over the cap, naming the total so far so the sender
    knows how much to remove."""
    return HTTPException(
        status_code=413,
        detail=(
            f"Attachments total {_mb(total)}; one email can carry at most "
            f"{_mb(budget_bytes)}. Remove a file and send again."
        ),
    )


def attachment_audit_meta(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """What went out, for the audit row: name, size, and which record it came
    from — never the bytes."""
    return [
        {
            "name": item.get("name"),
            "size_bytes": item.get("size_bytes"),
            "kind": item.get("kind"),
            "ref_id": item.get("ref_id"),
        }
        for item in items or ()
    ]


# ── what the picker offers ──────────────────────────────────────────────


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def _num(value: Any) -> float | None:
    # Numeric columns: a Decimal or None, never text.
    return float(value) if value is not None else None


def list_attachment_sources(
    db: Session,
    request: Request,
    user: Any,
    tenant_id: str,
    *,
    job_id: str | None = None,
    customer_id: str | None = None,
) -> dict[str, Any]:
    """Everything the picker may offer for this job and/or customer, already
    filtered by :class:`AttachScope`. A job with a customer widens to that
    customer's estimates, invoices and documents — the reason a tech on a job
    can send the customer every estimate they have open."""
    from gdx_dispatch.models.tenant_models import (  # noqa: PLC0415
        Customer,
        Document,
        Invoice,
        Job,
        JobPhoto,
    )
    from gdx_dispatch.modules.proposals.models import Estimate  # noqa: PLC0415

    scope = attach_scope(db, request, user, tenant_id)
    job_key = as_uuid(job_id) if job_id else None
    cust_key = as_uuid(customer_id) if customer_id else None

    job = db.get(Job, job_key) if job_key else None
    if job is not None and (job.deleted_at is not None or not scope.job(job.id)):
        job = None
    if cust_key is None and job is not None and job.customer_id is not None:
        cust_key = as_uuid(job.customer_id)
    customer = db.get(Customer, cust_key) if cust_key else None
    if customer is not None and getattr(customer, "deleted_at", None) is not None:
        customer = None

    photos: list[dict] = []
    job_docs: list[dict] = []
    if job is not None:
        job_photos = list(db.execute(
            select(JobPhoto)
            .where(JobPhoto.job_id == job.id, JobPhoto.deleted_at.is_(None))
            .order_by(JobPhoto.uploaded_at.desc())
            .limit(200)
        ).scalars())
        doc_ids = {i for i in (_photo_document_id(p) for p in job_photos) if i is not None}
        photo_docs = {
            d.id: d
            for d in db.execute(
                select(Document).where(Document.id.in_(doc_ids), Document.deleted_at.is_(None))
            ).scalars()
        } if doc_ids else {}
        for p in job_photos:
            # Offered only when the send can load it: a legacy url, a deleted
            # document or a file gone from disk would 404 the whole email.
            doc = photo_docs.get(_photo_document_id(p))
            if doc is None or _document_path(doc, tenant_id) is None:
                continue
            photos.append({
                "id": str(p.id),
                "name": p.caption or _shown_name(p.filename) or "Photo",
                "url": p.url,
                "size_bytes": p.size_bytes,
                "kind": p.kind,
                # The office decides per photo whether the customer may see it
                # (migration 063). The picker shows it so the sender knows.
                "customer_visible": bool(p.customer_visible),
                "uploaded_at": _iso(p.uploaded_at),
            })
        photo_doc_urls = {p["url"] for p in photos}
        for d in db.execute(
            select(Document)
            .where(Document.job_id == job.id, Document.deleted_at.is_(None))
            .order_by(Document.uploaded_at.desc())
            .limit(200)
        ).scalars():
            # A photo's bytes are also a Document on the job; list it once.
            if f"/api/documents/{d.id}/download" in photo_doc_urls:
                continue
            if _document_path(d, tenant_id) is not None:
                job_docs.append(_doc_out(d))

    estimates: list[dict] = []
    invoices: list[dict] = []
    customer_docs: list[dict] = []
    if customer is not None:
        for e in db.execute(
            select(Estimate)
            .where(Estimate.customer_id == customer.id, Estimate.deleted_at.is_(None))
            .order_by(Estimate.created_at.desc())
            .limit(100)
        ).scalars():
            if scope.estimate(e):
                estimates.append({
                    "id": str(e.id),
                    "number": e.estimate_number,
                    "label": e.label or e.description,
                    "status": e.status,
                    "total": _num(e.total),
                    "created_at": _iso(e.created_at),
                })
        for i in db.execute(
            select(Invoice)
            .where(Invoice.customer_id == customer.id, Invoice.deleted_at.is_(None))
            .order_by(Invoice.created_at.desc())
            .limit(100)
        ).scalars():
            if scope.invoice(i):
                invoices.append({
                    "id": str(i.id),
                    "number": i.invoice_number,
                    "status": i.status,
                    "total": _num(i.total),
                    "created_at": _iso(getattr(i, "created_at", None)),
                })
        if scope.customer(customer.id):
            for d in db.execute(
                select(Document)
                .where(
                    Document.customer_id == customer.id,
                    Document.job_id.is_(None),
                    Document.estimate_id.is_(None),
                    Document.deleted_at.is_(None),
                )
                .order_by(Document.uploaded_at.desc())
                .limit(100)
            ).scalars():
                if _document_path(d, tenant_id) is not None:
                    customer_docs.append(_doc_out(d))

    return {
        "job": {"id": str(job.id), "label": job.job_number or job.title} if job is not None else None,
        "customer": (
            {"id": str(customer.id), "name": customer.name} if customer is not None else None
        ),
        "job_photos": photos,
        "job_documents": job_docs,
        "customer_documents": customer_docs,
        "estimates": estimates,
        "invoices": invoices,
        "max_total_bytes": MAX_EMAIL_ATTACHMENTS_BYTES,
        "max_upload_bytes": MAX_UPLOAD_ATTACHMENT_BYTES,
    }


def _doc_out(d: Any) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "name": d.title or d.original_name or "Document",
        "file_name": d.original_name,
        "content_type": d.content_type,
        "size_bytes": d.file_size or d.size_bytes,
        "uploaded_at": _iso(d.uploaded_at),
    }
