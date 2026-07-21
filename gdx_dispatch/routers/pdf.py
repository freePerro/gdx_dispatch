from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.pdf_generator import generate_estimate_pdf, generate_invoice_pdf
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Document, Invoice, Job, PdfTemplate
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.modules.proposals.totals import compute_estimate_totals
from gdx_dispatch.routers.auth import get_current_user

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
                "name": d.original_name,
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
    return {
        "estimate_number": estimate.estimate_number,
        "customer": _customer_payload(customer),
        "jobsite_address": getattr(estimate, "jobsite_address", None) or "",
        "description": getattr(estimate, "description", None) or "",
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


def _invoice_payload(invoice: Invoice, customer: Customer | None) -> dict[str, Any]:
    lines = sorted(invoice.lines, key=lambda row: (row.sort_order, row.created_at, row.id))
    return {
        "invoice_number": invoice.invoice_number,
        "customer": _customer_payload(customer),
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
        "total": _to_float(invoice.total),
        "balance_due": _to_float(invoice.balance_due),
        "status": invoice.status,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else "",
        "terms": invoice.notes or "",
        # "Total-only" display — hides per-line prices + Subtotal/Tax rows,
        # keeping Total + Balance Due. Snapshotted from the source estimate.
        "hide_line_prices": bool(getattr(invoice, "hide_line_prices", False)),
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
        select(Invoice).options(selectinload(Invoice.lines)).where(Invoice.id == invoice_id, Invoice.deleted_at.is_(None))
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
        invoice_data=_invoice_payload(invoice, customer),
        tenant_branding=_branding_payload(db),
        template_config=_template_config(db, "invoice"),
    )
    filename = f"invoice-{invoice.invoice_number}.pdf"
    return StreamingResponse(
        _byte_stream(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
