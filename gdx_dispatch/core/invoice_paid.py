"""One source of truth for "how much has been paid on this invoice".

M35 (money-audit-2026-08-04): `Invoice.amount_paid` is a **cache that nothing
maintains**. `_recalculate_invoice` deliberately ignores it and derives the
balance from the `payments` table instead; the only writer in the repo is the
one-off `tools/qb_payment_substance_repair.py`, which ran on prod exactly once
(2026-07-31 10:23:58 UTC, 287 rows). Every payment recorded since has left the
column behind — measured 2026-08-22: **24 invoices, $62,473.72 of drift, all
understating**, and 24 of the 27 payments involved were recorded after that run.

Where it actually surfaced (checked, not assumed): job profitability reported
``total_paid`` short by the whole drift, and ``is_untouched_autodraft``'s
payment arm could not fire at all. The mobile "Paid" row did **not** show
``$0.00`` — it never rendered, because ``/api/invoices/{id}`` (the endpoint
MobileBillingView actually calls) had no ``amount_paid`` key at all. That is
fixed in the same change by emitting a real paid-to-date from the payments
already loaded on the detail response.

The rule this module encodes is the one `_recalculate_invoice` already uses:

    paid = Σ payments WHERE voided_at IS NULL

Voided payments stay as history but stop counting (GL S6/P4). Use these helpers
rather than reading the column — the column is being dropped.
"""
from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Payment


def paid_amount_sq():
    """Correlated scalar subquery: paid-to-date for the enclosing Invoice.

    Use inside a larger select so the sum stays a single round trip::

        select(Invoice.id, paid_amount_sq().label("paid"))
    """
    from gdx_dispatch.models.tenant_models import Invoice

    return (
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(Payment.invoice_id == Invoice.id, Payment.voided_at.is_(None))
        .correlate(Invoice)
        .scalar_subquery()
    )


def paid_to_date(db: Session, invoice_id) -> Decimal:
    """Paid-to-date for one invoice. Returns Decimal('0') when nothing is paid."""
    total = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.invoice_id == invoice_id,
            Payment.voided_at.is_(None),
        )
    ).scalar_one_or_none()
    return Decimal(str(total or 0))


def paid_to_date_bulk(db: Session, invoice_ids: Iterable) -> dict[str, Decimal]:
    """Paid-to-date for many invoices in ONE query, keyed by `str(invoice_id)`.

    List surfaces (the jobs board, billing lists) serialize dozens of invoices
    per request; per-row queries here would be an N+1 on a hot path. Invoices
    with no payments are simply absent — callers should default to 0.
    """
    ids = [i for i in invoice_ids if i is not None]
    if not ids:
        return {}
    rows = db.execute(
        select(Payment.invoice_id, func.coalesce(func.sum(Payment.amount), 0))
        .where(Payment.invoice_id.in_(ids), Payment.voided_at.is_(None))
        .group_by(Payment.invoice_id)
    ).all()
    return {str(inv_id): Decimal(str(total or 0)) for inv_id, total in rows}
