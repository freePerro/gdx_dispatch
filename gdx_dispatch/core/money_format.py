"""Customer-facing currency formatting — one source for PDFs and emails.

The sign goes OUTSIDE the dollar sign: -$500.00, never $-500.00 (Tier-9.10 —
netting credit lines and refunds carry negative totals). Shared so the invoice
PDF, estimate PDF, and both email bodies can't drift apart.
"""
from __future__ import annotations


def format_money(value: object) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"
