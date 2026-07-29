"""Confirm an order to a job — the step that closes the circle.

Matching only ever SUGGESTS. This is where a human decides, and it is the single
action that connects the leg nothing had connected before:

    order confirmation  →  bill  →  statement line     already threaded on one number
                          ↓
                         job                            ← this
                          ↓
                  paperwork on the job

One confirmation files EVERY document held for that order number, not just the
order's own PDF. The supplier's order number becomes their invoice number, so
the order confirmation and the bill are two documents about one purchase; making
someone confirm the same job twice to file both would be busywork that invites
them to file the second against a different job.

Existing filing is never overwritten. A document already sitting on a job was
put there by someone or something with more context than this, and silently
moving a customer's paperwork is worse than leaving it where it is. That is why
each write is guarded on the field being empty, and why the result reports what
it actually changed rather than what it attempted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Document, Job
from gdx_dispatch.modules.vendor_invoices.models import VendorInvoice
from gdx_dispatch.modules.vendor_orders.models import VendorOrder

log = logging.getLogger(__name__)


class OrderConfirmError(ValueError):
    """The confirmation cannot be applied as asked."""


@dataclass
class FiledDocument:
    document_id: str
    kind: str            # 'order_confirmation' | 'bill'
    original_name: str | None
    newly_filed: bool    # False = it was already on a job, left alone


@dataclass
class OrderConfirmResult:
    order_number: str
    job_id: str
    customer_id: str | None
    documents: list[FiledDocument] = field(default_factory=list)

    @property
    def newly_filed_count(self) -> int:
        return sum(1 for d in self.documents if d.newly_filed)


def _file_document(
    db: Session, document_id, job_id: UUID, customer_id, kind: str,
) -> FiledDocument | None:
    """Put one document on the job. Never moves one that is already filed."""
    if document_id is None:
        return None
    doc = db.get(Document, document_id)
    if doc is None or doc.deleted_at is not None:
        return None

    newly_filed = doc.job_id is None
    if newly_filed:
        doc.job_id = job_id
    # customer_id is filled independently: a document could have been attached
    # to a job by the bill-confirm path, which sets job_id and never touches
    # this. Filling it here means the paperwork also shows on the customer.
    if doc.customer_id is None and customer_id is not None:
        doc.customer_id = customer_id

    return FiledDocument(
        document_id=str(doc.id),
        kind=kind,
        original_name=doc.original_name,
        newly_filed=newly_filed,
    )


def confirm_order_job(
    db: Session,
    order: VendorOrder,
    *,
    job_id: UUID,
    actor_id: str | None = None,
) -> OrderConfirmResult:
    """Attach ``order`` to ``job_id`` and file every document for its number.

    Raises ``OrderConfirmError`` if the job doesn't exist — a dangling job_id
    would put paperwork somewhere nobody can find it, which is worse than
    refusing.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise OrderConfirmError(f"job {job_id} not found")

    order.matched_job_id = job_id
    order.job_confirmed_at = datetime.now(timezone.utc)
    order.job_confirmed_by = actor_id

    customer_id = getattr(job, "customer_id", None)
    result = OrderConfirmResult(
        order_number=order.order_number,
        job_id=str(job_id),
        customer_id=str(customer_id) if customer_id else None,
    )

    filed = _file_document(
        db, order.document_id, job_id, customer_id, "order_confirmation"
    )
    if filed is not None:
        result.documents.append(filed)

    # The bill for the SAME number is the same purchase — scoped by vendor,
    # because an invoice number is only unique per supplier.
    for bill in db.execute(
        select(VendorInvoice)
        .where(VendorInvoice.invoice_number == order.order_number)
        .where(VendorInvoice.vendor_name_raw == order.vendor_name)
        .where(VendorInvoice.deleted_at.is_(None))
    ).scalars().all():
        filed = _file_document(db, bill.document_id, job_id, customer_id, "bill")
        if filed is not None:
            result.documents.append(filed)
        if bill.matched_job_id is None:
            bill.matched_job_id = job_id

    db.flush()
    log.info(
        "vendor order %s confirmed to job %s by %s — %d document(s) filed",
        order.order_number, job_id, actor_id or "?", result.newly_filed_count,
    )
    return result
