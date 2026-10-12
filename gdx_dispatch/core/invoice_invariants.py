"""The invoice totals invariant, enforced at commit time.

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

Which invoices are checked
--------------------------
Every invoice the transaction wrote, and every invoice whose lines it wrote —
whether or not the header itself changed, and including the invoice a line was
moved away from. A flush empties ``session.new`` and ``session.dirty``, so a
listener that read only those at commit saw nothing once a handler had flushed
(GDXA-678): an ``after_flush`` hook records each touched invoice id in
``session.info`` as the flush happens, and the commit check reads that record.

Savepoints
----------
``before_commit`` also fires when a savepoint is released. The record is a
stack: each savepoint collects the invoices written while it is the innermost
one open. Its release checks exactly those, then hands them to its parent; a
savepoint that ends any other way (``rollback()``, an exception in
``with begin_nested()``, a failed flush) is discarded with its writes. This
is what the callers need on both sides:

- the QuickBooks per-row loops and the closeout autodraft (first draft or
  rebuild) wrap one invoice in a savepoint inside ``try``, so a bad invoice is
  refused at the release and the rest of the transaction survives;
- ``create_invoice`` flushes its lines, posts a deposit credit memo inside a GL
  savepoint (every GL post releases one, ``modules/ledger/engine.py``), and
  only then restates its header. The header was written before that savepoint,
  so the release does not check it; the outermost commit does.

The whole record is cleared when the outermost transaction ends.

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
from sqlalchemy import inspect as sa_inspect
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


_TOUCHED_KEY = "_gdx_invoice_touched"  # the outermost transaction's record
_SAVEPOINTS_KEY = "_gdx_invoice_touched_savepoints"  # savepoint -> [record, released]


def _record_for(session, transaction) -> set:  # noqa: ANN001
    """The record of the innermost open savepoint at or above ``transaction``,
    or the outermost transaction's."""
    frames = session.info.get(_SAVEPOINTS_KEY, {})
    while transaction is not None:
        if transaction.nested and transaction in frames:
            return frames[transaction][0]
        transaction = transaction.parent
    return session.info.setdefault(_TOUCHED_KEY, set())


def _record_touched(session) -> None:  # noqa: ANN001
    """Add the id of every invoice this flush wrote, directly or via a line."""
    from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine

    record = _record_for(session, session.get_nested_transaction())
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, Invoice):
            record.add(getattr(obj, "id", None))
        elif isinstance(obj, InvoiceLine):
            record.add(getattr(obj, "invoice_id", None))
            # A line moved to another invoice changes the one it left too.
            record.update(sa_inspect(obj).attrs.invoice_id.history.deleted)
    record.discard(None)


def _check(session, invoice_ids) -> None:  # noqa: ANN001
    from gdx_dispatch.models.tenant_models import Invoice

    for invoice_id in list(invoice_ids):
        invoice = session.get(Invoice, invoice_id)
        if invoice is None or getattr(invoice, "deleted_at", None) is not None:
            continue
        violation = check_invoice(session, invoice)
        if violation is None:
            continue
        if _strict():
            raise InvoiceTotalsInvariantError(violation)
        logger.error("invoice_totals_invariant_violated %s", violation)


def install(session_factory=None) -> None:
    """Register the commit-time guard and its flush-time collector. Idempotent."""
    target = session_factory or Session

    if getattr(target, "_gdx_invoice_invariant_installed", False):
        return

    # A line's invoice_id is a plain column: set on an expired line (any line
    # loaded before a commit), its history has no old value, so the invoice the
    # line left would go unrecorded. active_history loads it on set.
    from gdx_dispatch.models.tenant_models import InvoiceLine

    if not getattr(InvoiceLine, "_gdx_invoice_id_history", False):
        event.listen(InvoiceLine.invoice_id, "set", lambda *_: None, active_history=True)
        InvoiceLine._gdx_invoice_id_history = True

    # Collect at flush: after_flush still shows the pre-flush new/dirty/deleted
    # sets and attribute history, with primary keys and line foreign keys
    # already assigned.
    @event.listens_for(target, "after_flush")
    def _after_flush(session, flush_context):  # noqa: ANN001
        _record_touched(session)

    @event.listens_for(target, "after_transaction_create")
    def _after_transaction_create(session, transaction):  # noqa: ANN001
        if transaction.nested:
            session.info.setdefault(_SAVEPOINTS_KEY, {})[transaction] = [set(), False]

    # CHECK at commit, not flush. An invoice and its lines are routinely written
    # across several flushes (create the header, flush to get an id, then add
    # lines), so a flush-time check sees a half-built invoice and reports a
    # violation that the very next flush resolves. Commit is the boundary where
    # the unit of work is complete and the invariant genuinely has to hold.
    @event.listens_for(target, "before_commit")
    def _before_commit(session):  # noqa: ANN001
        # Make pending work visible to the SELECTs in check_invoice; the flush
        # also records whatever it writes through _after_flush. Only when there
        # is pending work: a clean session's commit must not flush.
        if session.new or session.dirty or session.deleted:
            session.flush()
        savepoint = session.get_nested_transaction()
        if savepoint is None:
            _check(session, session.info.get(_TOUCHED_KEY, ()))
            return
        # A savepoint release: check what was written inside it.
        frame = session.info.get(_SAVEPOINTS_KEY, {}).get(savepoint)
        if frame is not None:
            _check(session, frame[0])
            frame[1] = True

    # A released savepoint hands its record to its parent; any other ending
    # takes its writes with it, so its record goes too.
    @event.listens_for(target, "after_transaction_end")
    def _after_transaction_end(session, transaction):  # noqa: ANN001
        if transaction.parent is None:
            session.info.pop(_TOUCHED_KEY, None)
            session.info.pop(_SAVEPOINTS_KEY, None)
            return
        if not transaction.nested:
            return
        frame = session.info.get(_SAVEPOINTS_KEY, {}).pop(transaction, None)
        if frame is not None and frame[1]:
            _record_for(session, transaction.parent).update(frame[0])

    try:
        target._gdx_invoice_invariant_installed = True
    except (AttributeError, TypeError):  # pragma: no cover — exotic factories
        pass
