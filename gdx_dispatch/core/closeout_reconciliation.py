"""Closeout→billing discrepancy detection — plan §12.

Doug's original complaint: "sometimes we do invoices for a job and when the job
is closed it doesn't match things up correctly — it sends to billing even though
we've been paid." Concretely: a job is billed from its closeout, then the closeout
is REVISED (the supersede model stamps the old snapshot and links a new one). The
invoice is now stale — it was written against numbers that no longer stand.

This module DETECTS that state (read-only). It does NOT create supplemental
invoices or credit memos — that reconciliation ACTION is a separate, money-writing
step. Gated company-wide by tenant_settings.closeout_billing_reconciliation.

The signal: a job that has a LIVE closeout AND at least one SUPERSEDED closeout
(a prior attestation exists) AND a non-void, non-estimate invoice created BEFORE
the live closeout. We key on `superseded_at` existing — NOT on the live row's
`supersedes_id` — because migration 041's backfill stamps `superseded_at` on
pre-existing duplicate closeouts but deliberately leaves `supersedes_id` NULL
("the true chain is unknowable after the fact"); keying on supersedes_id would
be blind to every historical revision. An estimate-billed invoice is excluded on
purpose — the customer agreed that fixed price, so a later closeout change isn't
a billing discrepancy.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.settings_flags import closeout_reconciliation_enabled
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job, JobCloseout


def _closeout_summary(jc: JobCloseout | None) -> dict[str, Any] | None:
    if jc is None:
        return None
    parts = jc.parts_used if isinstance(jc.parts_used, list) else []
    return {
        "closeout_id": str(jc.id),
        "hours_worked": float(jc.hours_worked or 0),
        "techs_on_site": int(jc.techs_on_site or 1),
        "parts_count": len(parts),
        "labor_matrix_item_id": jc.labor_matrix_item_id,
        "created_at": jc.created_at.isoformat() if jc.created_at else None,
    }


def find_closeout_billing_discrepancies(db: Session, tenant_id: str) -> dict[str, Any]:
    """Jobs billed from a closeout that was later revised. Returns
    {enabled, items}. When the company toggle is off, enabled=False and items
    is empty — the caller shows nothing rather than a misleading empty list."""
    if not closeout_reconciliation_enabled(tenant_id):
        return {"enabled": False, "items": []}

    # job_ids whose closeout was revised at least once — a superseded row exists.
    # (superseded_at, not supersedes_id: 041's backfill sets the former, NULLs
    # the latter, so this catches historical duplicates AND future re-closeouts.)
    revised_job_ids = set(
        db.execute(
            select(JobCloseout.job_id).where(JobCloseout.superseded_at.isnot(None)).distinct()
        ).scalars().all()
    )
    if not revised_job_ids:
        return {"enabled": True, "items": []}

    live = db.execute(
        select(JobCloseout).where(
            JobCloseout.superseded_at.is_(None),
            JobCloseout.deleted_at.is_(None),
            JobCloseout.job_id.in_(revised_job_ids),
        )
    ).scalars().all()

    items: list[dict[str, Any]] = []
    for jc in live:
        invoice = db.execute(
            select(Invoice)
            .where(
                Invoice.job_id == jc.job_id,
                Invoice.deleted_at.is_(None),
                Invoice.status != "void",
                Invoice.estimate_id.is_(None),        # estimate-billed = agreed price, not a discrepancy
                Invoice.created_at < jc.created_at,    # billed before this revision
            )
            .order_by(Invoice.created_at.asc())
        ).scalars().first()
        if invoice is None:
            continue

        job = db.get(Job, jc.job_id)
        customer = db.get(Customer, job.customer_id) if job and job.customer_id else None
        # The attestation the invoice was billed against — most recent superseded row.
        prior = db.execute(
            select(JobCloseout)
            .where(JobCloseout.job_id == jc.job_id, JobCloseout.superseded_at.isnot(None))
            .order_by(JobCloseout.created_at.desc())
        ).scalars().first()
        items.append({
            "job_id": str(jc.job_id),
            "job_number": getattr(job, "job_number", None),
            "customer_name": getattr(customer, "name", None),
            "invoice": {
                "id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "total": float(invoice.total or 0),
                "status": invoice.status,
                "created_at": invoice.created_at.isoformat() if invoice.created_at else None,
            },
            "current_closeout": _closeout_summary(jc),
            "billed_against": _closeout_summary(prior),
        })
    return {"enabled": True, "items": items}
