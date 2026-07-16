"""Report-time cash-basis derivation primitives (S10, spec §6 / §5.7).

The books are accrual; cash-basis reports are DERIVED at report time by
prorating each payment event across its invoice's revenue components —
Intuit's own architecture (one accrual store, proportional allocation;
their worked example is a unit test here). Nothing in this module posts
journal entries: pre-cutover invoices especially get NO 4xxx entries —
their category split reads straight from operational ``InvoiceLine`` rows
(spec §5.7 [AUDIT-R3]), which exist in the DB regardless of cutover.

Rules encoded here (spec §6 hardening):
- Proration ratio is capped at 1.0 — overpayment excess lives in 2300 and
  is never recognized as revenue.
- Refunds and credit memos are NEGATIVE payment events: they prorate
  negatively at their own effective date, so cash-basis revenue actually
  decreases when money goes back.
- Allocation is sum-preserving largest-remainder (``money.allocate``) — the
  recognized pieces always sum to exactly the recognized amount.

S11's P&L endpoint consumes these primitives; S10 ships them with the
proration tests so the cutover math is pinned before any report renders it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger.engine import PostingLine
from gdx_dispatch.modules.ledger.money import allocate
from gdx_dispatch.modules.ledger.rules import build_issuance_lines


@dataclass(frozen=True)
class RevenueComponent:
    """One credit-side slice of an invoice: a revenue line, the tax mirror,
    or the rounding residual. Exactly one of ``role`` / ``account_id`` —
    same contract as PostingLine."""

    cents: int
    role: str | None = None
    account_id: UUID | None = None
    memo: str | None = None


def invoice_components(session: Session, invoice) -> tuple[RevenueComponent, ...]:
    """The invoice's revenue/tax/rounding split, from OPERATIONAL rows.

    Reuses ``build_issuance_lines`` — the exact category→account mapping P1
    posts with — and keeps its credit legs (negated to positive weights).
    Works identically for post-cutover invoices (anchored to their live P1)
    and pre-cutover P8-anchored invoices (whose InvoiceLine rows exist even
    though no P1 was ever posted) — the §5.7 operational-line anchor.
    """
    return tuple(
        RevenueComponent(
            cents=-line.amount_cents,
            role=line.role,
            account_id=line.account_id,
            memo=line.memo,
        )
        for line in build_issuance_lines(session, invoice)
        if line.amount_cents < 0
    )


def prorate_event_cents(
    components: Sequence[int], event_cents: int, invoice_total_cents: int
) -> list[int]:
    """Recognize ``event_cents`` of a payment event across ``components``.

    ``components`` are the invoice's credit-side weights (revenue lines, tax,
    rounding — non-negative, normally summing to the invoice total).
    Positive events (payments) recognize proportionally; negative events
    (refunds, credit memos) recognize negatively. |event| is capped at the
    invoice total — the overpayment excess is a 2300 liability, never
    revenue. Sum-preserving: the result always sums to the capped amount.
    """
    if invoice_total_cents <= 0 or not components:
        return [0] * len(components)
    if event_cents >= 0:
        recognized = min(event_cents, invoice_total_cents)
    else:
        recognized = max(event_cents, -invoice_total_cents)
    if recognized == 0:
        return [0] * len(components)
    return allocate(recognized, list(components))


def prorate_components(
    components: Sequence[RevenueComponent], event_cents: int, invoice_total_cents: int
) -> list[tuple[RevenueComponent, int]]:
    """``prorate_event_cents`` zipped back onto the component objects —
    the shape S11's cash-basis P&L aggregates by account."""
    parts = prorate_event_cents(
        [c.cents for c in components], event_cents, invoice_total_cents
    )
    return list(zip(components, parts))


__all__ = [
    "PostingLine",
    "RevenueComponent",
    "invoice_components",
    "prorate_components",
    "prorate_event_cents",
]
