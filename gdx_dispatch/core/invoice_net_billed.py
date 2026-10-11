"""One source of truth for "how much did we actually bill on this invoice".

GDXA-590 / M19 (money-audit-2026-08-04): a credit memo never reduces
``Invoice.total``, only ``balance_due``. ``issue_credit_memo`` (routers/invoices.py)
and the deposit supersede (modules/deposits/service.py) append an
``invoice_adjustments`` row with ``kind='credit_memo'`` and leave the total as
issued, so the invoice stays a faithful record of what was first billed. That
is deliberate and stays. It means any aggregate reading ``SUM(Invoice.total)``
as billed, revenue or lifetime value overstates every credited invoice by the
whole credit. The revenue reports were fixed first (``_credits_by_period``, PRs
#402 and #446). This module is the shared form the remaining sites are meant
to adopt. Callers so far: the Billing dashboard's ``paid_this_month``
(GDXA-592) and the phone's day summary ``revenue_invoiced`` (GDXA-598); the
other sites are listed on GDXA-590 for their owners.

    net billed = Invoice.total − Σ invoice_adjustments.amount WHERE kind = 'credit_memo'

What is deliberately NOT subtracted:

- ``credit_applied`` is settlement, not a reduction in what was billed. The
  credit memo that created the customer credit already reduced billing, and
  applying that credit to another invoice would subtract it a second time. See
  the ``_credits_by_period`` docstring in routers/reports.py and the ledger's
  ``_cash_events``, which signs ``credit_applied`` +1 as cash.
- ``refund`` returns cash and does not change what was billed.
- Deposits are not excluded. The final invoice already nets the deposit with a
  negative line, so excluding the deposit subtracts it twice (M19 docstring).

The credit is netted against its own invoice. It is not moved to the date the
credit was issued: a per-job or per-customer sum nets at invoice level. The
by-period revenue report is the one place that attributes by issue date,
because it mirrors the GL.

Adjustment rows are append-only and carry no void or delete marker, so every
credit memo counts. The invoice universe is the CALLER's: keep filtering
``Invoice.status != 'void'`` and ``Invoice.deleted_at IS NULL`` as before. The
helper nets one invoice's credits and does not decide which invoices count.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from gdx_dispatch.models.tenant_models import Invoice, InvoiceAdjustment

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session

CREDIT_MEMO = "credit_memo"


def credit_memo_total_sq():
    """Correlated scalar subquery: Σ credit memos on the enclosing Invoice (0 when none)."""
    return (
        select(func.coalesce(func.sum(InvoiceAdjustment.amount), 0))
        .where(
            InvoiceAdjustment.invoice_id == Invoice.id,
            InvoiceAdjustment.kind == CREDIT_MEMO,
        )
        .correlate(Invoice)
        .scalar_subquery()
    )


def invoice_net_billed_sq():
    """Column expression: the enclosing Invoice's total net of its credit memos.

    The counterpart of ``paid_amount_sq()`` (core/invoice_paid.py). Use it
    where ``Invoice.total`` was summed as billed or revenue::

        select(func.coalesce(func.sum(invoice_net_billed_sq()), 0)).where(...)
        select(Invoice.job_id, func.sum(invoice_net_billed_sq())).group_by(Invoice.job_id)
    """
    return func.coalesce(Invoice.total, 0) - credit_memo_total_sq()


def invoice_net_billed_sql(alias: str = "i") -> str:
    """The same expression as raw SQL, for ``text()`` queries over ``invoices <alias>``.

    ``alias`` is spliced into the SQL, so it must be a fixed identifier written
    in the code and never a value that came in on a request.
    """
    if not alias.isidentifier():
        raise ValueError(f"invoice alias must be a bare SQL identifier, got {alias!r}")
    # The alias is a checked identifier from code and the kind a module constant.
    return (
        f"(COALESCE({alias}.total, 0) - COALESCE(("  # noqa: S608
        f"SELECT SUM(cm.amount) FROM invoice_adjustments cm "
        f"WHERE cm.invoice_id = {alias}.id AND cm.kind = '{CREDIT_MEMO}'"
        f"), 0))"
    )


def net_billed_bulk(db: Session, invoice_ids: Iterable) -> dict[str, Decimal]:
    """Net billed for many invoices in ONE query, keyed by ``str(invoice_id)``.

    For sites that already hold invoice rows in Python. Every id that names an
    invoice is present, credited or not; an unknown id is absent.
    """
    wanted = [i for i in invoice_ids if i is not None]
    if not wanted:
        return {}
    return {
        str(inv_id): Decimal(str(net))
        for inv_id, net in db.execute(
            select(Invoice.id, invoice_net_billed_sq()).where(Invoice.id.in_(wanted))
        ).all()
    }
