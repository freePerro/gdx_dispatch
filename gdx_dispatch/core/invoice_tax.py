"""The tax share of a balance-reducing invoice adjustment (money-audit M18).

Doug's ruling (2026-08-24, AskUserQuestion): **pro-rata at the invoice's
rate** — a credit reduces the invoice's tax by ``credit × (tax / total)``.
``invoice_adjustments`` previously carried a flat ``amount`` with no tax
split, so credited tax kept counting as a remittance liability: the
sales-tax report could not subtract what was given back.

One function, used by every adjustment writer AND by migration 080's
backfill (same arithmetic, so historical rows and new rows can't disagree).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def credit_tax_component(invoice, amount) -> Decimal:
    """``amount × (tax_amount / total)``, half-up to cents, floored at 0 and
    capped at the invoice's tax (a component can never exceed the tax that
    was charged). Zero-tax / zero-total invoices → 0.00 — MN construction
    contracts carry no tax, so this is the common case at GDX.
    """
    total = Decimal(str(invoice.total or 0))
    tax = Decimal(str(invoice.tax_amount or 0))
    amt = Decimal(str(amount or 0))
    if total <= 0 or tax <= 0 or amt <= 0:
        return Decimal("0.00")
    component = (amt * tax / total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return min(component, tax.quantize(Decimal("0.01")))
