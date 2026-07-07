"""Canonical "is this job billed" predicate — ONE definition, shared.

PR2-billing-capture (2026-07-07). `Job.billing_status` is a dead cache (only
ever written "unbilled" — see core/job_display_state.py's header), so every
consumer that filtered on it was wrong: the unbilled-work alert counted paid
jobs, and the two Ready-for-Billing queries disagreed with the display state
about voided invoices. Per the locked derive-don't-cache model (Doug
2026-05-17), readers derive from invoices instead.

The definition (design doc: docs/design/billing-capture-hardening-plan.md,
audit round 1):

    A job is BILLED iff it has an invoice with
        deleted_at IS NULL
        AND status != 'void'
        AND (total > 0 OR status != 'draft')

Two deliberate exclusions:
- **Void invoices don't bill a job.** The display state already treats void
  as dead money; the old RFB queries didn't — a job whose only invoice was
  voided silently vanished from Ready-for-Billing forever.
- **$0 DRAFTS don't bill a job.** `create_invoice_from_job` fabricates a
  single $0 draft line when a job has no estimate — treating that placeholder
  as "billed" would hide the job from every alert (silent false negative).
  A $0 invoice that was deliberately SENT does count (warranty work — the
  operator said "this is free", stop nagging).

Known edge (documented, accepted): the display state's `live_invoices` still
counts a $0 draft as "Invoiced" — the locked display model is untouched here.
A job with only the fabricated $0 draft shows "Invoiced" on the job card but
stays in Ready-for-Billing/alerts, which is the cash-flow-safe direction.
"""
from __future__ import annotations

from sqlalchemy import exists, or_

from gdx_dispatch.models.tenant_models import Invoice, Job


def job_billed_exists():
    """SQLAlchemy EXISTS clause: the correlated Job has a billing-real invoice.

    Use as a filter on a Job query:

        query.filter(job_billed_exists())          # billed jobs
        query.filter(~job_billed_exists())         # unbilled jobs
    """
    return exists().where(
        Invoice.job_id == Job.id,
        Invoice.deleted_at.is_(None),
        Invoice.status != "void",
        or_(Invoice.total > 0, Invoice.status != "draft"),
    )


def invoice_bills_job(status: str | None, total: float | None, deleted_at) -> bool:
    """Python-side twin of job_billed_exists() for already-loaded rows.

    Keep the two in lockstep — a test pins them against the same fixtures.
    """
    if deleted_at is not None:
        return False
    s = (status or "").strip().lower()
    if s == "void":
        return False
    return float(total or 0) > 0 or s != "draft"
