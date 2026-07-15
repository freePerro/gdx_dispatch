"""Posting rules — P1 invoice issuance (S5, spec §5.1).

Importing this module registers the rules on the service registry; the
chokepoint lazily imports it the first time it runs with the flag on, so
registration can never be skipped by import-order luck.

P1 fires on the transition out of ``draft`` into ``sent``/``paid``:

    debit  1200 AR                     invoice.total
    credit 4xxx per line category      (unmapped/NULL → 4000, memo-flagged)
    credit 2100 Sales Tax Payable      invoice.tax_amount (mirrored, never recomputed)
    residual → 6990 Rounding           (memo-flagged; keeps the entry balanced
                                        when total ≠ Σlines + tax, e.g. discounts)

Post-issuance edits reverse the live P1 and repost at current content
(§5.6 keys make replay/idempotency safe). Void reverses the live P1.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger import service as ledger_service
from gdx_dispatch.modules.ledger.engine import (
    PostingEvent,
    PostingLine,
    post_for_event,
    reverse_entry,
)
from gdx_dispatch.modules.ledger.models import (
    ENTRY_STATUS_POSTED,
    ROLE_AR,
    ROLE_ROUNDING,
    ROLE_SALES_FALLBACK,
    ROLE_SALES_TAX_PAYABLE,
    GlAccount,
    GlJournalEntry,
)
from gdx_dispatch.modules.ledger.money import to_cents

log = logging.getLogger(__name__)

ISSUED_STATUSES = ("sent", "paid")
EVENT_ISSUED = "issued"


def _dec(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _revenue_account_id_for(
    session: Session, settings, company_id: str, category: str | None
):
    """category → configured revenue account id, else None (fallback 4000)."""
    if not category:
        return None
    mapping = getattr(settings, "revenue_category_account_map", None) or {}
    acct_id = mapping.get(category)
    if not acct_id:
        return None
    try:
        acct_uuid = UUID(str(acct_id))
    except ValueError:
        return None  # garbage map entry → fallback (memo-flagged)
    account = session.get(GlAccount, acct_uuid)
    if account is None or account.company_id != company_id or not account.active:
        # dangling map entry — fall back loud-in-memo rather than crash a send
        return None
    return account.id


# The residual line absorbs genuine penny rounding ONLY. Anything larger is
# invoice data that doesn't reconcile (total ≠ Σlive-lines + tax) — posting
# it to 6990 would silently launder a composition bug into "rounding", which
# is exactly how the round-1 audit's reproduced $500 overstatement hid.
MAX_RESIDUAL_CENTS = 100


class IssuanceCompositionError(RuntimeError):
    """Invoice total does not reconcile with its lines + tax beyond rounding."""


def _live_invoice_lines(session: Session, invoice):
    """The invoice's LIVE lines — same filter as _recalculate_invoice. The
    ORM relationship includes soft-deleted rows (audit round 1: a line
    deleted while drafting still credited revenue at send)."""
    from gdx_dispatch.models.tenant_models import InvoiceLine

    return session.scalars(
        select(InvoiceLine).where(
            InvoiceLine.invoice_id == invoice.id,
            InvoiceLine.deleted_at.is_(None),
        )
    ).all()


def build_issuance_lines(session: Session, invoice) -> tuple[PostingLine, ...]:
    """The P1 line set for the invoice's CURRENT content. Empty tuple when
    there is nothing to post (zero-total invoice)."""
    company_id = invoice.company_id
    settings = ledger_service.get_gl_settings(session, company_id)
    total_cents = to_cents(_dec(invoice.total))
    if total_cents == 0:
        return ()

    lines: list[PostingLine] = [
        PostingLine(
            amount_cents=total_cents,
            role=ROLE_AR,
            job_id=invoice.job_id,
            customer_id=invoice.customer_id,
            memo=f"invoice {invoice.invoice_number} issued",
        )
    ]

    credited = 0
    for il in _live_invoice_lines(session, invoice):
        line_cents = to_cents(_dec(il.line_total))
        if line_cents == 0:
            continue
        account_id = _revenue_account_id_for(session, settings, company_id, il.category)
        memo = (il.description or "")[:200]
        if account_id is None:
            lines.append(
                PostingLine(
                    amount_cents=-line_cents,
                    role=ROLE_SALES_FALLBACK,
                    job_id=invoice.job_id,
                    customer_id=invoice.customer_id,
                    memo=(f"[unmapped category: {il.category}] " if il.category else "") + memo,
                )
            )
        else:
            lines.append(
                PostingLine(
                    amount_cents=-line_cents,
                    account_id=account_id,
                    job_id=invoice.job_id,
                    customer_id=invoice.customer_id,
                    memo=memo,
                )
            )
        credited += line_cents

    tax_cents = to_cents(_dec(getattr(invoice, "tax_amount", None)))
    if tax_cents:
        lines.append(
            PostingLine(
                amount_cents=-tax_cents,
                role=ROLE_SALES_TAX_PAYABLE,
                job_id=invoice.job_id,
                customer_id=invoice.customer_id,
                memo="sales tax (mirrored from invoice)",
            )
        )
        credited += tax_cents

    residual = total_cents - credited
    if residual:
        if abs(residual) > MAX_RESIDUAL_CENTS:
            raise IssuanceCompositionError(
                f"invoice {invoice.invoice_number}: total ({total_cents}c) differs "
                f"from live lines + tax ({credited}c) by {residual}c — beyond "
                "rounding; the invoice data doesn't reconcile and must not post"
            )
        lines.append(
            PostingLine(
                amount_cents=-residual,
                role=ROLE_ROUNDING,
                job_id=invoice.job_id,
                customer_id=invoice.customer_id,
                memo=f"[P1 residual] total − (lines + tax) = {residual} cents",
            )
        )
    return tuple(lines)


def _effective_at(invoice) -> date:
    """The issuance's economic date — MUST be stable per invoice, because it
    joins the idempotency content hash: a today()-fallback would mint a fresh
    key on every day-crossing touch of a NULL-date invoice (phantom
    reversal+repost pairs migrating revenue to the touch date — audit round
    1). Fallback chain is all invoice-immutable-ish state. S10's backfill
    MUST derive dates through this same function so replays land on the same
    keys."""
    if getattr(invoice, "invoice_date", None):
        return invoice.invoice_date
    sent_at = getattr(invoice, "sent_at", None)
    if sent_at:
        return sent_at.date()
    created_at = getattr(invoice, "created_at", None)
    if created_at:
        return created_at.date()
    return date.today()  # unreachable for persisted rows (created_at default)


def _live_issuance_entry(session: Session, invoice) -> GlJournalEntry | None:
    return session.scalars(
        select(GlJournalEntry).where(
            GlJournalEntry.company_id == invoice.company_id,
            GlJournalEntry.source_type == "invoice",
            GlJournalEntry.source_id == str(invoice.id),
            GlJournalEntry.status == ENTRY_STATUS_POSTED,
            GlJournalEntry.idempotency_key.like(f"invoice:{invoice.id}:{EVENT_ISSUED}:%"),
        )
    ).first()


def post_invoice_issuance(session: Session, invoice, old_status, new_status, actor) -> None:
    """P1 — registered for draft→sent and draft→paid."""
    lines = build_issuance_lines(session, invoice)
    if not lines:
        return  # zero-total invoice: nothing to record
    post_for_event(
        session,
        PostingEvent(
            company_id=invoice.company_id,
            source_type="invoice",
            source_id=str(invoice.id),
            event=EVENT_ISSUED,
            effective_at=_effective_at(invoice),
            lines=lines,
            created_by=actor,
        ),
    )


def repost_invoice_issuance(session: Session, invoice, actor: str | None = None) -> None:
    """Post-issuance content edit (spec §5.1): reverse the live P1 and repost
    at current content. No-op when: flag off, invoice not issued, or content
    unchanged (the engine's idempotency key already matches the live entry).
    Callers hook this after ``_recalculate_invoice`` recomputes totals.
    """
    if invoice.status not in ISSUED_STATUSES:
        return
    if not ledger_service.ledger_posting_enabled(session, invoice.company_id):
        return

    lines = build_issuance_lines(session, invoice)
    live = _live_issuance_entry(session, invoice)

    if not lines:
        # content collapsed to zero-total: reverse whatever is live
        if live is not None:
            reverse_entry(session, live, created_by=actor)
        return

    if live is None:
        # issued but never posted (edited during S5 rollout window) — post fresh
        post_invoice_issuance(session, invoice, invoice.status, invoice.status, actor)
        return

    # The engine's content hash decides whether this is an edit or a replay:
    # post first — identical content lands on the live entry (same key) and
    # nothing needs reversing; changed content mints a new key, after which
    # the stale entry is reversed.
    event = PostingEvent(
        company_id=invoice.company_id,
        source_type="invoice",
        source_id=str(invoice.id),
        event=EVENT_ISSUED,
        effective_at=_effective_at(invoice),
        lines=lines,
        created_by=actor,
    )
    posted = post_for_event(session, event)
    if posted.id != live.id:
        reverse_entry(session, live, created_by=actor)


def reverse_invoice_issuance(session: Session, invoice, old_status, new_status, actor) -> None:
    """Void — reverse the live P1 (draft voids have nothing live; fine)."""
    live = _live_issuance_entry(session, invoice)
    if live is not None:
        reverse_entry(session, live, created_by=actor)


# --- registration (module import = registration; the chokepoint imports us
# lazily on first flag-on transition) --------------------------------------

ledger_service._POSTING_RULES.update(
    {
        ("draft", "sent"): post_invoice_issuance,
        ("draft", "paid"): post_invoice_issuance,
        ("sent", "void"): reverse_invoice_issuance,
        ("paid", "void"): reverse_invoice_issuance,
        # draft→void: nothing posted, nothing to reverse — pass-through.
        # ("sent","paid") is P3 — S6 registers it.
    }
)
