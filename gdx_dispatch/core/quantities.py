"""A line's stored quantity, read as it was recorded (#560).

``quantity or 1`` re-rated a stored 0 as 1: a zero-quantity invoice,
change-order, estimate or parts line billed, ordered or scheduled one unit.
The rule — job costing's since #469, and the owner's ruling 2026-09-11 — is
that a blank (NULL) quantity is unstated and reads as 1, and a recorded 0 is 0.

This is for READING stored rows. Forms that normalise what a user types for a
new line (proposal tier lines clamp to at least 1) are a different decision
and are left as they are.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

T = TypeVar("T")


def recorded_quantity(raw: T | None) -> T | int:
    """``raw`` as recorded; 1 only when nothing was recorded at all."""
    return 1 if raw is None else raw


def zero_quantity_verdict(quantity: Any, line_total: Any) -> str | None:
    """What an automatic copy into an invoice should do with a stored line.

    ``None``  — billable: copy it (the normal case, including a blank quantity,
                which is unstated and reads as 1).
    ``"skip"`` — recorded 0 AND no money: there is nothing to bill, so the line
                is left out rather than invented as one unit (the owner's rule:
                show the 0, never bill it).
    ``"refuse"`` — recorded 0 BUT the line carries an amount. The copy stops and
                says so, naming the line.

    Why "refuse" rather than copying the amount through with its 0 (audit
    rounds 5 and 6, 2026-09-11): skipping such a line loses money — a change
    order signed at $350 invoiced at $300 and was stamped billed. But copying
    it writes an invoice line this system cannot represent. Every PATCH of a
    line recomputes ``line_total = quantity × unit_price``
    (``routers/invoices.py``), so a single unrelated edit — toggling `taxable`
    — silently zeroes the amount, and ``InvoiceLinePatchIn`` forbids quantity 0
    so nothing can put it back. The customer-facing PDF would print
    "Qty 0 · $50.00 · $50.00", and the office cannot save the invoice at all.
    A contradictory source row is a data problem to fix at the source, where a
    person can see it, not something to launder into an invoice.

    No prod row is affected: a census on 2026-09-11 found 0 zero and 0 NULL
    quantities across 1,281 rows in the six quantity columns.
    """
    try:
        if recorded_quantity(quantity) > 0:
            return None
    except TypeError:
        # Not a quantity we can compare (a str from a raw-SQL row, say).
        # Treat it as stated and let the copy proceed unchanged.
        return None
    try:
        return "skip" if not Decimal(str(line_total or 0)) else "refuse"
    except (InvalidOperation, ValueError):
        # Not an amount we can reason about — keep the line rather than drop it.
        return None
