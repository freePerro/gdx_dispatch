"""The invoice totals invariant, enforced at flush time.

    total == Σ(active line_total) + tax_amount

Money audit 2026-08-04. Thirty-nine findings, and the worst of them were not
arithmetic errors — the arithmetic in ``_recalculate_invoice`` is careful
Decimal work. They were *callers that violated the invariant it assumes*:

- the QuickBooks importer wrote QBO SubTotal lines as real lines, so Σlines was
  2x the header total, and the next recalc "corrected" a settled invoice to
  double (M1);
- three conversion paths hand-set a discounted total with no discount line, so
  the first recalc sprang the total back up and re-billed the customer (M7);
- two creation paths stamped a tax amount with no rate, freezing tax while the
  subtotal moved (M9, M10).

Each was fixed individually. This guard is what stops the sixth one: an
invariant the system enforces rather than merely assumes. The ledger has had
exactly this for its own balance rule (``modules/ledger/guard.py``) since S4,
and it is why the GL side of the audit came back clean.

Exemption
---------
``Invoice.totals_locked`` invoices are skipped by design — a QuickBooks-imported
invoice's header total is authoritative while its local lines are lossy or
absent. That exemption is explicit and greppable, which is the point: a row
that cannot satisfy the invariant has to say so.

Mode
----
Strict by default: a violation raises, so the offending write rolls back rather
than persisting a wrong total. Set ``GDX_INVOICE_INVARIANT=log`` to downgrade to
a logged warning if a legitimate path is ever found that needs breathing room —
but treat that as an incident, not a configuration.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal

from sqlalchemy import event, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

TOLERANCE = Decimal("0.005")  # half a cent — quantization noise, not drift


class InvoiceTotalsInvariantError(Exception):
    """An invoice was about to persist with total != Σlines + tax."""


def _dec(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def check_invoice(session: Session, invoice) -> str | None:
    """Return a human-readable violation for ``invoice``, or None if it holds."""
    if bool(getattr(invoice, "totals_locked", False)):
        return None

    from gdx_dispatch.models.tenant_models import InvoiceLine

    rows = session.execute(
        select(InvoiceLine).where(
            InvoiceLine.invoice_id == invoice.id,
            InvoiceLine.deleted_at.is_(None),
        )
    ).scalars().all()

    # A line-less invoice cannot have its total derived from lines, so there is
    # nothing to contradict. That shape is the QuickBooks import (282 such rows
    # historically) and legacy header-only invoices — both of which belong to
    # `totals_locked`, not to this guard. Enforcing here would only convert a
    # legitimate shape into a hard error while catching nothing: the bug class
    # this exists for is a hand-set total sitting ALONGSIDE lines that
    # disagree with it (M1's duplicated import lines, M7's dropped discount).
    if not rows:
        return None

    line_total = sum((_dec(r.line_total) for r in rows), Decimal("0"))
    expected = line_total + _dec(invoice.tax_amount)
    actual = _dec(invoice.total)
    if abs(actual - expected) <= TOLERANCE:
        return None
    return (
        f"invoice {getattr(invoice, 'invoice_number', invoice.id)}: "
        f"total {actual} != Σlines {line_total} + tax {_dec(invoice.tax_amount)} "
        f"(= {expected}). Represent adjustments as lines, or set totals_locked "
        f"if the header total is authoritative (imported invoices)."
    )


def _strict() -> bool:
    return os.getenv("GDX_INVOICE_INVARIANT", "strict").strip().lower() != "log"


def install(session_factory=None) -> None:
    """Register the flush-time guard. Idempotent."""
    target = session_factory or Session

    if getattr(target, "_gdx_invoice_invariant_installed", False):
        return

    # COMMIT, not flush. An invoice and its lines are routinely written across
    # several flushes (create the header, flush to get an id, then add lines),
    # so a flush-time check sees a half-built invoice and reports a violation
    # that the very next flush resolves. Commit is the boundary where the unit
    # of work is complete and the invariant genuinely has to hold.
    @event.listens_for(target, "before_commit")
    def _before_commit(session):  # noqa: ANN001
        from gdx_dispatch.models.tenant_models import Invoice

        touched = [
            obj
            for obj in (*session.new, *session.dirty)
            if isinstance(obj, Invoice) and getattr(obj, "deleted_at", None) is None
        ]
        if not touched:
            return
        # Make pending work visible to the SELECTs in check_invoice.
        session.flush()
        for invoice in touched:
            if getattr(invoice, "id", None) is None:
                continue
            violation = check_invoice(session, invoice)
            if violation is None:
                continue
            if _strict():
                raise InvoiceTotalsInvariantError(violation)
            logger.error("invoice_totals_invariant_violated %s", violation)

    try:
        target._gdx_invoice_invariant_installed = True
    except (AttributeError, TypeError):  # pragma: no cover — exotic factories
        pass
