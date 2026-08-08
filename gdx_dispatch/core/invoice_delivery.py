"""The §11 delivery gate — ONE guard for every path that puts an invoice
in front of a customer.

2026-08-08 audit finding: `verified_at` was enforced in exactly two places,
both on the MOBILE send path — while every desktop path (send, mark-sent,
email-compose, pay-link, send-reminder, and the bulk Send-Selected sweep
over the Draft filter) delivered unverified drafts without a check. Since
the closeout autodraft (v1.43.0) machine-prices a draft for every closeout,
that hole meant machine-computed totals could reach customers with no human
review — the exact thing §11 exists to prevent.

The rule, deliberately narrow:

    A DRAFT may not be delivered (emailed, marked sent/mailed, pay-linked,
    reminded about) until a human verified it. An invoice already past
    draft cleared the gate when it was issued — re-sends, receipts and
    reminders on issued invoices are unaffected, so the thousands of
    pre-rail invoices (verified_at NULL, status sent/paid) keep working
    with no backfill migration.

Callers raise on the returned reason; the string is stable so the frontend
can key the "verify and send" flow off code == "awaiting_verification".
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

AWAITING_VERIFICATION_DETAIL = (
    "invoice is an unverified draft — review it and Verify before it can "
    "reach the customer"
)


def draft_needs_verification(invoice: Any) -> bool:
    """True when the §11 gate blocks delivery of this invoice."""
    return (
        str(getattr(invoice, "status", "") or "").lower() == "draft"
        and getattr(invoice, "verified_at", None) is None
    )


def require_deliverable(invoice: Any) -> None:
    """Raise 409 (code awaiting_verification) when the gate blocks delivery.

    Apply to every endpoint that emails, marks-sent, pay-links, receipts,
    or reminds. Void/paid/status guards remain each endpoint's own concern —
    this gate answers exactly one question: has a human signed off on this
    draft's numbers?
    """
    if draft_needs_verification(invoice):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "awaiting_verification",
                "detail": AWAITING_VERIFICATION_DETAIL,
            },
        )
