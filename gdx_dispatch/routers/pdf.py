from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.job_photos import resolve_photo_file
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.pdf_generator import generate_estimate_pdf, generate_invoice_pdf
from gdx_dispatch.models.tenant_models import (
    AppSettings,
    Customer,
    Document,
    Invoice,
    InvoiceAdjustment,
    Job,
    JobPhoto,
    PdfTemplate,
)
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.modules.proposals.totals import compute_estimate_totals
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["pdf"],
    dependencies=[Depends(get_current_user), Depends(require_module("documents"))],
)


async def _byte_stream(payload: bytes):
    yield payload


def _to_float(value: Any) -> float:
    return float(value or 0)


def _customer_payload(customer: Customer | None) -> dict[str, str]:
    if not customer:
        return {"name": "", "address": ""}
    return {
        "name": customer.name or "",
        "address": customer.address or "",
    }


def _branding_payload(db: Session) -> dict[str, str]:
    settings = db.query(AppSettings).first()
    if not settings:
        return {
            "company_name": "",
            "logo": "",
            "primary_color": "#0f172a",
            "secondary_color": "#2563eb",
            "address": "",
        }
    return {
        "company_name": settings.company_name or "",
        "logo": settings.logo or "",
        "primary_color": settings.primary_color or "#0f172a",
        "secondary_color": settings.secondary_color or "#2563eb",
        "address": settings.address or "",
    }


def _signature_payload(estimate: Any) -> dict[str, str]:
    """Captured quote-acceptance signature (signed on the tech's phone).
    signature_data is free Text — only a data:image/* URI is ever forwarded
    to the renderer, so a stray/hostile value degrades to the blank
    signature line instead of landing in the PDF markup."""
    raw = getattr(estimate, "signature_data", None) or ""
    # png/jpeg only — svg is a script container and WeasyPrint fetches its
    # sub-resources (audit round 5); the capture pad only produces PNG.
    if not isinstance(raw, str) or not raw.startswith(("data:image/png;", "data:image/jpeg;")):
        return {"image": "", "signed_by": "", "signed_at": ""}
    signed_at = getattr(estimate, "signed_at", None)
    return {
        "image": raw,
        "signed_by": getattr(estimate, "signed_by", None) or "",
        "signed_at": signed_at.date().isoformat() if signed_at else "",
    }


def _template_config(db: Session, template_type: str) -> dict[str, Any] | None:
    """Load the tenant's saved PDF-template config (Settings → PDF Templates)
    for the renderer. None → tenant never saved one → pdf_generator falls back
    to the legacy layout. Best-effort by design: a malformed row or a tenant DB
    that predates the pdf_templates table must never block PDF generation."""
    try:
        row = db.execute(
            select(PdfTemplate).where(PdfTemplate.template_type == template_type)
        ).scalar_one_or_none()
    except Exception:
        logging.getLogger(__name__).exception("pdf_template_config_load_failed type=%s", template_type)
        # A failed SELECT aborts the Postgres transaction; without a rollback
        # the same session is poisoned for whatever the caller does next
        # (e.g. the invoice-send email that shares this db) — audit catch.
        with contextlib.suppress(Exception):
            db.rollback()
        return None
    if not row:
        return None
    raw_blocks = row.blocks
    blocks: list[Any] | None
    if isinstance(raw_blocks, str):
        try:
            parsed = json.loads(raw_blocks)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        blocks = parsed if isinstance(parsed, list) else None
    else:
        blocks = raw_blocks if isinstance(raw_blocks, list) else None
    return {
        "brand_color": row.brand_color,
        "font_family": row.font_family,
        "header_content": row.header_content or "",
        "footer_content": row.footer_content or "",
        "blocks": blocks,
    }


def _estimate_attachments_for_pdf(
    db: Session, estimate_id: UUID, tenant_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return image attachments as file:// data URIs for WeasyPrint embedding,
    plus a flat list of non-image attachment names so the PDF can reference them."""
    rows = db.execute(
        select(Document)
        .where(Document.estimate_id == estimate_id, Document.deleted_at.is_(None))
        .order_by(Document.uploaded_at.asc())
    ).scalars().all()
    base = Path(os.getenv("UPLOAD_DIR", "/app/uploads")) / tenant_id / "estimate" / str(estimate_id)
    images: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for d in rows:
        path = base / d.filename
        ct = (d.content_type or "").lower()
        if ct.startswith("image/") and path.exists():
            # WeasyPrint resolves file:// URIs against base_url; use absolute path.
            images.append({
                "src": f"file://{path}",
                # The label (door size, "16' × 7'") captions the photo; the
                # filename is the fallback — for captured photos it's a
                # machine name, which is why title wins.
                "name": (d.title or "").strip() or d.original_name,
            })
        else:
            files.append({"name": d.original_name})
    return images, files


def _estimate_payload(
    estimate: Estimate,
    customer: Customer | None,
    default_terms: str = "",
    *,
    attachment_images: list[dict[str, Any]] | None = None,
    attachment_files: list[dict[str, Any]] | None = None,
    deposit_pct: int = 0,
    hide_line_prices_default: bool = False,
    db: Session | None = None,
) -> dict[str, Any]:
    from gdx_dispatch.modules.estimates_features import effective_hide_line_prices
    lines = sorted(estimate.lines, key=lambda row: (row.sort_order, row.created_at, row.id))
    totals = compute_estimate_totals(estimate, db)
    pct = max(0, min(100, int(deposit_pct or 0)))
    deposit_amount = round(totals["total"] * pct / 100.0, 2) if pct > 0 else 0.0
    # Tri-state: per-estimate override wins; NULL inherits the tenant default.
    hide_line_prices = effective_hide_line_prices(
        getattr(estimate, "hide_line_prices", None), hide_line_prices_default
    )
    valid_until = getattr(estimate, "valid_until", None)
    return {
        "estimate_number": estimate.estimate_number,
        "customer": _customer_payload(customer),
        "jobsite_address": getattr(estimate, "jobsite_address", None) or "",
        "description": getattr(estimate, "description", None) or "",
        "valid_until": valid_until.date().isoformat() if valid_until else "",
        # Captured acceptance signature — rendered as the image + signed-by
        # line instead of the blank signature line when present.
        "signature": _signature_payload(estimate),
        "lines": [
            {
                "description": line.description,
                "category": line.category or "",
                "quantity": line.quantity,
                "unit_price": _to_float(line.unit_price),
                "line_total": _to_float(line.line_total),
            }
            for line in lines
        ],
        "subtotal": totals["subtotal"],
        "discount": totals["discount"],
        "tax": totals["tax"],
        "tax_rate_pct": totals["tax_rate_pct"],
        "total": totals["total"],
        "hide_line_prices": hide_line_prices,
        "deposit_pct": pct,
        "deposit_amount": deposit_amount,
        # Terms = tenant-wide default text from Settings → Feature Settings.
        # Notes = per-estimate text the user typed in this estimate's form.
        "terms": default_terms or "",
        "notes": estimate.notes or "",
        "attachment_images": attachment_images or [],
        "attachment_files": attachment_files or [],
    }


def _shrink_photo_for_pdf(src: Path, cache_key: str) -> Path:
    """Downscaled JPEG copy for PDF embedding, cached in the system tmp dir.

    The email send path SKIPS the whole PDF attachment above
    MAX_INLINE_ATTACHMENT_BYTES (2.5 MB) — embedding a handful of full-size
    photos would silently strip the invoice from its own email. 1200px q78
    keeps a photo ~100-200 KB on the page. Falls back to the original file
    when Pillow can't process it (WeasyPrint may still manage)."""
    cache_dir = Path(tempfile.gettempdir()) / "gdx_invoice_pdf_photos"
    out = cache_dir / f"{cache_key}.jpg"
    try:
        if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
            return out
        from PIL import Image  # noqa: PLC0415

        cache_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as img:
            img = img.convert("RGB")
            img.thumbnail((1200, 1200))
            img.save(out, format="JPEG", quality=78, optimize=True)
        return out
    except Exception:
        log.exception("invoice_pdf_photo_shrink_failed src=%s", src)
        return src


def _invoice_photos_for_pdf(db: Session, invoice: Invoice) -> list[dict[str, Any]]:
    """The job photos PICKED on this invoice, as file:// URIs for WeasyPrint.

    invoice.attached_photo_ids is a JSON array of job_photos.id strings
    (migration 059). Each resolves photo → documents download URL →
    documents.filename on disk (the flat UPLOAD_DIR layout uploads.py
    writes). Anything unresolvable — deleted photo, wrong job, missing
    file, non-document URL (the dead legacy /mobile/uploads path) — is
    silently skipped: the PDF must always render.
    """
    raw = getattr(invoice, "attached_photo_ids", None)
    if not raw or invoice.job_id is None:
        return []
    try:
        ids = [str(i) for i in json.loads(raw) if i]
    except (ValueError, TypeError):
        log.warning("invoice_pdf_bad_photo_ids invoice=%s", invoice.id)
        return []
    if not ids:
        return []
    # Bind UUID objects — the Uuid column refuses str binds on SQLite.
    id_uuids = []
    for i in ids:
        with contextlib.suppress(ValueError, AttributeError):
            id_uuids.append(UUID(i))
    if not id_uuids:
        return []
    photos = db.execute(
        select(JobPhoto).where(
            JobPhoto.id.in_(id_uuids),
            JobPhoto.job_id == invoice.job_id,
            # Share gate (migration 063) — the PDF is a customer-facing
            # surface like the portal and the pay page, and all three read the
            # same flag. Attaching sets it, so the ordinary path is unchanged;
            # un-sharing a photo afterwards keeps it off the next render.
            JobPhoto.customer_visible.is_(True),
            JobPhoto.deleted_at.is_(None),
        )
    ).scalars().all()
    by_id = {str(p.id): p for p in photos}
    images: list[dict[str, Any]] = []
    for pid in ids:  # selection order is display order
        photo = by_id.get(pid)
        if photo is None:
            continue
        # ONE resolver, shared with the customer portal and the pay page
        # (core/job_photos.resolve_photo_file) — it walks photo → document →
        # the flat UPLOAD_DIR, skips the dead legacy /mobile/uploads urls, and
        # applies the same renderable-image allowlist. Three surfaces showing
        # the customer different photos is the failure this prevents.
        resolved = resolve_photo_file(db, photo)
        if resolved is None:
            continue
        path = Path(resolved[0])
        embed = _shrink_photo_for_pdf(path, cache_key=pid)
        label = (photo.caption or "").strip() or (photo.kind or "").strip().title()
        images.append({"src": f"file://{embed}", "name": label})
    return images


def _invoice_settlement(invoice: Invoice, db: Session | None) -> tuple[float, float]:
    """(paid_to_date, credits_applied) — the single source the PDF and the
    email body use so their totals agree.

    paid_to_date = Σ non-voided payments (zero on a void invoice); credits =
    Σ(credit_memo + credit_applied). balance_due = max(total − paid − credits, 0).

    These are the TRUE amounts, not capped to the total: an overpayment is a
    real fact a customer understands (they see the full amount paid and a $0
    balance), and per-adjustment caps already stop a credit alone from
    exceeding the remaining balance — so the only way the four printed numbers
    don't foot is a genuine overpayment, which is honest to show. Capping would
    LIE about how much money was received (audit round: the earlier clamp broke
    exactly that — paid $300 on a $150 invoice must print $300).
    """
    paid_to_date = 0.0
    if (getattr(invoice, "status", "") or "") != "void":
        paid_to_date = sum(
            _to_float(p.amount)
            for p in (getattr(invoice, "payments", None) or [])
            if getattr(p, "voided_at", None) is None
        )
    credits_applied = 0.0
    if db is not None and getattr(invoice, "id", None) is not None:
        credits_applied = _to_float(
            db.execute(
                select(func.sum(InvoiceAdjustment.amount)).where(
                    InvoiceAdjustment.invoice_id == invoice.id,
                    InvoiceAdjustment.kind.in_(("credit_memo", "credit_applied")),
                )
            ).scalar_one_or_none()
            or 0
        )
    return round(max(paid_to_date, 0.0), 2), round(max(credits_applied, 0.0), 2)


def _invoice_payload(invoice: Invoice, customer: Customer | None, db: Session | None = None) -> dict[str, Any]:
    lines = sorted(invoice.lines, key=lambda row: (row.sort_order, row.created_at, row.id))
    invoice_date = getattr(invoice, "invoice_date", None)
    if invoice_date is None:
        # App-created invoices often leave invoice_date NULL (it's optional on
        # the create form); the creation day is the honest fallback.
        created = getattr(invoice, "created_at", None)
        invoice_date = created.date() if created else None
    total = _to_float(invoice.total)
    balance_due = _to_float(invoice.balance_due)
    # Paid to Date = Σ non-voided Payment rows. NOT total - balance_due: the
    # recalc subtracts credit memos/applied credits from balance_due too, so
    # that difference would print "Paid" for money never received (audit
    # round 5). NOT amount_paid either — that column is deprecated. A voided
    # invoice zeroes its balance without payments, so the row is suppressed
    # outright there.
    # Paid to Date + Credits Applied (Tier-9.2 for the PDF, shared with the
    # email body). balance_due = total − paid − credits, so a credit-memo'd
    # invoice printed Total − Paid ≠ Balance Due with NO line explaining the
    # gap — the detail view has shown adjustments since PR #197, the PDF was
    # blind to them.
    paid_to_date, credits_applied = _invoice_settlement(invoice, db)
    return {
        "invoice_number": invoice.invoice_number,
        "customer": _customer_payload(customer),
        "invoice_date": invoice_date.isoformat() if invoice_date else "",
        "paid_to_date": paid_to_date,
        "credits_applied": credits_applied,
        "lines": [
            {
                "description": line.description,
                "category": line.category or "",
                # taxable default-True mirrors the column default — legacy rows
                # created before the column existed read as taxable.
                "taxable": True if line.taxable is None else bool(line.taxable),
                "quantity": line.quantity,
                "unit_price": _to_float(line.unit_price),
                "line_total": _to_float(line.line_total),
            }
            for line in lines
        ],
        "subtotal": _to_float(invoice.subtotal),
        "tax": _to_float(invoice.tax_amount),
        # Prefer the persisted rate (Numeric(6,4)) so the label matches the
        # configured rate exactly. Back-derivation drifts: tax_amount is
        # rounded to cents, and on small subtotals tax_amount/subtotal yields
        # a different percent (e.g. $6.75 × 7.38% rounds to $0.50 → 7.407%).
        # Pre-S110 / QB-imported invoices have tax_rate=NULL — fall back to
        # the back-derived value so they still show something sensible.
        "tax_rate_pct": (
            round(_to_float(invoice.tax_rate) * 100, 4)
            if invoice.tax_rate is not None
            else (
                round(_to_float(invoice.tax_amount) / _to_float(invoice.subtotal) * 100, 4)
                if _to_float(invoice.subtotal) > 0 else 0.0
            )
        ),
        "total": total,
        "balance_due": balance_due,
        "status": invoice.status,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else "",
        # invoice.notes is per-invoice text the operator typed — it used to be
        # shipped as "terms" and printed under a "Terms" heading (mislabel,
        # Phase 3 fix). The template's Notes block owns it now.
        "notes": invoice.notes or "",
        # "Total-only" display — hides per-line prices + Subtotal/Tax rows,
        # keeping Total + Balance Due. Snapshotted from the source estimate.
        "hide_line_prices": bool(getattr(invoice, "hide_line_prices", False)),
        # Job photos picked for this invoice (migration 059) — rendered as
        # the "Job Photos" grid. Empty unless the office selected some. All
        # four generate_invoice_pdf call sites come through this payload, so
        # email/send/mobile/GET all carry the same photos.
        "attachment_images": _invoice_photos_for_pdf(db, invoice) if db is not None else [],
    }


@router.get("/api/estimates/{estimate_id}/pdf")
def estimate_pdf(
    estimate_id: UUID,
    request: Request = None,  # type: ignore[assignment]  # tolerate test-only direct calls
    db: Session = Depends(get_db),
) -> StreamingResponse:
    estimate = db.execute(
        select(Estimate).options(selectinload(Estimate.lines)).where(Estimate.id == estimate_id, Estimate.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")

    customer = None
    if estimate.customer_id:
        customer = db.execute(
            select(Customer).where(Customer.id == estimate.customer_id, Customer.deleted_at.is_(None))
        ).scalar_one_or_none()

    # Pull tenant-wide default terms text + deposit % (Settings → Feature
    # Settings → Estimates card). Best-effort — defaults if anything fails.
    default_terms = ""
    deposit_pct = 0
    hide_line_prices_default = False
    tenant_id = ""
    if request is not None:
        tenant_id = str((getattr(getattr(request, "state", None), "tenant", {}) or {}).get("id") or "")
    try:
        from gdx_dispatch.modules.estimates_features import get_features
        if tenant_id:
            features = get_features(tenant_id)
            default_terms = features.default_terms
            deposit_pct = features.deposit_pct
            hide_line_prices_default = features.hide_line_prices
    except Exception:
        default_terms = ""
        deposit_pct = 0
        hide_line_prices_default = False

    images, files = _estimate_attachments_for_pdf(db, estimate.id, tenant_id)
    pdf_bytes = generate_estimate_pdf(
        estimate_data=_estimate_payload(
            estimate,
            customer,
            default_terms=default_terms,
            attachment_images=images,
            attachment_files=files,
            deposit_pct=deposit_pct,
            hide_line_prices_default=hide_line_prices_default,
            db=db,
        ),
        tenant_branding=_branding_payload(db),
        template_config=_template_config(db, "estimate"),
    )
    filename = f"estimate-{estimate.estimate_number}.pdf"
    return StreamingResponse(
        _byte_stream(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: UUID, db: Session = Depends(get_db)) -> StreamingResponse:
    invoice = db.execute(
        select(Invoice)
        .options(selectinload(Invoice.lines), selectinload(Invoice.payments))
        .where(Invoice.id == invoice_id, Invoice.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # job_id is optional (QB-imported invoices have customer_id but no job).
    job = None
    if invoice.job_id is not None:
        job = db.execute(select(Job).where(Job.id == invoice.job_id, Job.deleted_at.is_(None))).scalar_one_or_none()
    customer = None
    customer_lookup_id = (job.customer_id if job and job.customer_id else invoice.customer_id)
    if customer_lookup_id:
        customer = db.execute(
            select(Customer).where(Customer.id == customer_lookup_id, Customer.deleted_at.is_(None))
        ).scalar_one_or_none()

    pdf_bytes = generate_invoice_pdf(
        invoice_data=_invoice_payload(invoice, customer, db),
        tenant_branding=_branding_payload(db),
        template_config=_template_config(db, "invoice"),
    )
    filename = f"invoice-{invoice.invoice_number}.pdf"
    return StreamingResponse(
        _byte_stream(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
