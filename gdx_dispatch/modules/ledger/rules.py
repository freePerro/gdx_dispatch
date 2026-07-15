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
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger import service as ledger_service
from gdx_dispatch.modules.ledger.coa import LedgerConfigError
from gdx_dispatch.modules.ledger.engine import (
    PostingEvent,
    PostingLine,
    post_for_event,
    reverse_entry,
)
from gdx_dispatch.modules.ledger.models import (
    ENTRY_STATUS_POSTED,
    ROLE_AR,
    ROLE_CUSTOMER_CREDITS,
    ROLE_EXPENSE_FALLBACK,
    ROLE_OPERATING_BANK,
    ROLE_ROUNDING,
    ROLE_SALES_FALLBACK,
    ROLE_REFUNDS,
    ROLE_SALES_TAX_PAYABLE,
    GlAccount,
    GlJournalEntry,
    GlJournalLine,
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


# ---------------------------------------------------------------------------
# P3 / P4 — payments (S6, spec §5.3)
# ---------------------------------------------------------------------------

EVENT_PAYMENT = "received"


def _prior_nonvoided_paid_cents(session: Session, invoice, payment) -> int:
    """Σ non-voided payments recorded BEFORE this one (created_at, id order —
    deterministic, so a backfill replay computes the same overpayment split
    this payment got at recording time)."""
    from gdx_dispatch.models.tenant_models import Payment

    rows = session.scalars(
        select(Payment).where(
            Payment.invoice_id == invoice.id,
            Payment.voided_at.is_(None),
            Payment.id != payment.id,
        )
    ).all()

    def _key(p):
        # SQLite hands back naive datetimes, fresh rows carry aware ones —
        # normalize so the ordering never TypeErrors.
        ts = p.created_at
        if ts is not None and ts.tzinfo is not None:
            ts = ts.astimezone(dt_timezone.utc).replace(tzinfo=None)
        return (ts or datetime.min, str(p.id))

    marker = _key(payment)
    return sum(to_cents(_dec(p.amount)) for p in rows if _key(p) < marker)


def invoice_credited_cents(session: Session, invoice) -> int:
    """Σ balance-reducing adjustments (credit_memo + credit_applied) — the
    other half of the receivable arithmetic. Audit round 3 (executed):
    payment splits that ignored credit memos drove live AR negative and
    swallowed the customer's overpayment instead of minting a 2300 credit."""
    from gdx_dispatch.models.tenant_models import InvoiceAdjustment

    rows = session.scalars(
        select(InvoiceAdjustment).where(
            InvoiceAdjustment.invoice_id == invoice.id,
            InvoiceAdjustment.kind.in_(("credit_memo", "credit_applied")),
        )
    ).all()
    return sum(to_cents(_dec(r.amount)) for r in rows)


def build_payment_lines(session: Session, payment, invoice) -> tuple[PostingLine, ...]:
    """P3: debit the method's account role (1000/1050 per the settings map),
    credit 1200 AR — with any excess beyond the invoice's remaining AR
    credited to 2300 Customer Credits instead (the opt-in overpayment path;
    the API rejects overpayment without the opt-in). Remaining AR counts BOTH
    prior payments AND balance-reducing adjustments — a pure function of
    current state, so replays and resettles stay key-identical."""
    amount_cents = to_cents(_dec(payment.amount))
    if amount_cents == 0:
        return ()
    company_id = invoice.company_id
    settings = ledger_service.get_gl_settings(session, company_id)
    if settings is None:  # flag can't be on without a row, but never guess
        raise LedgerConfigError("gl_settings missing — accounting not initialized")
    method_role = ledger_service.resolve_payment_method_role(settings, payment.method)

    total_cents = to_cents(_dec(invoice.total))
    remaining_ar = max(
        total_cents
        - _prior_nonvoided_paid_cents(session, invoice, payment)
        - invoice_credited_cents(session, invoice),
        0,
    )
    ar_portion = min(amount_cents, remaining_ar)
    excess = amount_cents - ar_portion

    lines = [
        PostingLine(
            amount_cents=amount_cents,
            role=method_role,
            job_id=invoice.job_id,
            customer_id=invoice.customer_id,
            memo=f"payment on {invoice.invoice_number} ({payment.method})",
        )
    ]
    if ar_portion:
        lines.append(
            PostingLine(
                amount_cents=-ar_portion,
                role=ROLE_AR,
                job_id=invoice.job_id,
                customer_id=invoice.customer_id,
                memo=f"payment applied to {invoice.invoice_number}",
            )
        )
    if excess:
        lines.append(
            PostingLine(
                amount_cents=-excess,
                role=ROLE_CUSTOMER_CREDITS,
                job_id=invoice.job_id,
                customer_id=invoice.customer_id,
                memo=f"overpayment on {invoice.invoice_number} → customer credit",
            )
        )
    return tuple(lines)


def post_payment_received(session: Session, payment, invoice, actor: str | None = None):
    """P3 — called from every path that creates a Payment row. No-op with
    the flag off or for zero-amount payments. Never commits."""
    if not ledger_service.ledger_posting_enabled(session, invoice.company_id):
        return None
    lines = build_payment_lines(session, payment, invoice)
    if not lines:
        return None
    return post_for_event(
        session,
        PostingEvent(
            company_id=invoice.company_id,
            source_type="payment",
            source_id=str(payment.id),
            event=EVENT_PAYMENT,
            effective_at=payment.payment_date or date.today(),
            lines=lines,
            created_by=actor,
        ),
    )


def _live_payment_entries(session: Session, payment, company_id):
    return session.scalars(
        select(GlJournalEntry).where(
            GlJournalEntry.company_id == company_id,
            GlJournalEntry.source_type == "payment",
            GlJournalEntry.source_id == str(payment.id),
            GlJournalEntry.status == ENTRY_STATUS_POSTED,
            GlJournalEntry.idempotency_key.like(f"payment:{payment.id}:{EVENT_PAYMENT}:%"),
        )
    ).all()


def resettle_invoice_payments(session: Session, invoice, actor: str | None = None) -> None:
    """P4's other half (audit round 2, executed repro): voiding a payment
    changes the AR arithmetic for every LATER payment on the invoice — their
    AR/2300 splits were computed against the old void set. Leaving them
    stale (a) diverges GL AR from balance_due the moment the void commits,
    and (b) breaks replay determinism (a backfill after the void computes
    different content → new keys → double-posts).

    So: reverse the voided payments' live entries, then reverse+repost every
    remaining payment whose split changed — the same §5.6 key machinery the
    invoice-edit path uses. Replays after this are idempotent again (same
    state → same splits → same keys). Never commits.
    """
    if not ledger_service.ledger_posting_enabled(session, invoice.company_id):
        return
    from gdx_dispatch.models.tenant_models import Payment

    payments = session.scalars(
        select(Payment).where(Payment.invoice_id == invoice.id)
    ).all()
    for payment in payments:
        if payment.voided_at is not None:
            for live in _live_payment_entries(session, payment, invoice.company_id):
                reverse_entry(session, live, created_by=actor)
            continue
        lines = build_payment_lines(session, payment, invoice)
        if not lines:
            continue
        posted = post_for_event(
            session,
            PostingEvent(
                company_id=invoice.company_id,
                source_type="payment",
                source_id=str(payment.id),
                event=EVENT_PAYMENT,
                effective_at=payment.payment_date or date.today(),
                lines=lines,
                created_by=actor,
            ),
        )
        for live in _live_payment_entries(session, payment, invoice.company_id):
            if live.id != posted.id:
                reverse_entry(session, live, created_by=actor)


# ---------------------------------------------------------------------------
# S7 — credit memos / refunds / apply-credit (spec §5.2, P9)
# ---------------------------------------------------------------------------


def _reason_role(settings, reason: str | None) -> str:
    """reason → 4900 DISCOUNTS vs 4910 REFUNDS via the settings map;
    unmapped/NULL falls to REFUNDS (the conservative contra-revenue bucket —
    a mystery credit must not silently inflate the discounts line)."""
    mapping = (settings.credit_reason_role_map or {}) if settings else {}
    return mapping.get((reason or "").strip().lower(), ROLE_REFUNDS)


def post_credit_memo(session: Session, adjustment, invoice, actor: str | None = None):
    """Credit memo: debit 4900/4910 per reason, credit 1200 AR — forgiving
    part of the receivable (spec §5.2). Keyed per adjustment row."""
    if not ledger_service.ledger_posting_enabled(session, invoice.company_id):
        return None
    amount_cents = to_cents(_dec(adjustment.amount))
    if amount_cents == 0:
        return None
    settings = ledger_service.get_gl_settings(session, invoice.company_id)
    return post_for_event(
        session,
        PostingEvent(
            company_id=invoice.company_id,
            source_type="adjustment",
            source_id=str(adjustment.id),
            event="credit_memo",
            effective_at=adjustment.created_at.date() if adjustment.created_at else date.today(),
            lines=(
                PostingLine(
                    amount_cents=amount_cents,
                    role=_reason_role(settings, adjustment.reason),
                    job_id=invoice.job_id,
                    customer_id=invoice.customer_id,
                    memo=f"credit memo on {invoice.invoice_number}: {adjustment.reason or 'unspecified'}",
                ),
                PostingLine(
                    amount_cents=-amount_cents,
                    role=ROLE_AR,
                    job_id=invoice.job_id,
                    customer_id=invoice.customer_id,
                    memo=f"credit memo applied to {invoice.invoice_number}",
                ),
            ),
            created_by=actor,
        ),
    )


def post_refund(session: Session, adjustment, invoice, actor: str | None = None):
    """Refund: debit 4910 (contra-revenue per reason), credit the cash
    account the money leaves through (refund_method via the payment map).
    AR untouched — a refund is money back for money paid, not forgiveness."""
    if not ledger_service.ledger_posting_enabled(session, invoice.company_id):
        return None
    amount_cents = to_cents(_dec(adjustment.amount))
    if amount_cents == 0:
        return None
    settings = ledger_service.get_gl_settings(session, invoice.company_id)
    if settings is None:
        raise LedgerConfigError("gl_settings missing — accounting not initialized")
    cash_role = ledger_service.resolve_payment_method_role(settings, adjustment.refund_method)
    return post_for_event(
        session,
        PostingEvent(
            company_id=invoice.company_id,
            source_type="adjustment",
            source_id=str(adjustment.id),
            event="refund",
            effective_at=adjustment.created_at.date() if adjustment.created_at else date.today(),
            lines=(
                PostingLine(
                    amount_cents=amount_cents,
                    role=_reason_role(settings, adjustment.reason),
                    job_id=invoice.job_id,
                    customer_id=invoice.customer_id,
                    memo=f"refund on {invoice.invoice_number}: {adjustment.reason or 'unspecified'}",
                ),
                PostingLine(
                    amount_cents=-amount_cents,
                    role=cash_role,
                    job_id=invoice.job_id,
                    customer_id=invoice.customer_id,
                    memo=f"refund paid out via {adjustment.refund_method}",
                ),
            ),
            created_by=actor,
        ),
    )


def post_credit_application(session: Session, adjustment, invoice, actor: str | None = None):
    """P9: consume 2300 Customer Credits balance against an open invoice —
    debit 2300, credit 1200 (spec §5.3). Caps are enforced at the API."""
    if not ledger_service.ledger_posting_enabled(session, invoice.company_id):
        return None
    amount_cents = to_cents(_dec(adjustment.amount))
    if amount_cents == 0:
        return None
    return post_for_event(
        session,
        PostingEvent(
            company_id=invoice.company_id,
            source_type="adjustment",
            source_id=str(adjustment.id),
            event="credit_applied",
            effective_at=adjustment.created_at.date() if adjustment.created_at else date.today(),
            lines=(
                PostingLine(
                    amount_cents=amount_cents,
                    role=ROLE_CUSTOMER_CREDITS,
                    job_id=invoice.job_id,
                    customer_id=invoice.customer_id,
                    memo=f"customer credit applied to {invoice.invoice_number}",
                ),
                PostingLine(
                    amount_cents=-amount_cents,
                    role=ROLE_AR,
                    job_id=invoice.job_id,
                    customer_id=invoice.customer_id,
                    memo=f"credit applied to {invoice.invoice_number}",
                ),
            ),
            created_by=actor,
        ),
    )


def customer_credit_balance_cents(session: Session, company_id: str, customer_id) -> int:
    """The customer's live 2300 balance from the ledger (credit-normal:
    stored negative, returned positive). Sums ALL lines with NO status
    filter — reversal entries negate their originals, and filtering by
    status counts a reversal without the original it cancels (audit round
    3, executed: an overpay-void drove the balance NEGATIVE and locked the
    customer out of credit they were genuinely owed). Company scoping rides
    the entry join. PG callers should have locked the credit rows (SELECT
    FOR UPDATE) before trusting this for a spend."""
    from gdx_dispatch.modules.ledger.coa import resolve_role_account

    credits_acct = resolve_role_account(session, company_id, ROLE_CUSTOMER_CREDITS)
    total = 0
    rows = session.execute(
        select(GlJournalLine.amount_cents)
        .join(GlJournalEntry, GlJournalLine.entry_id == GlJournalEntry.id)
        .where(
            GlJournalLine.account_id == credits_acct.id,
            GlJournalLine.customer_id == customer_id,
            GlJournalEntry.company_id == company_id,
        )
    ).all()
    for (amount_cents,) in rows:
        total += amount_cents
    return -total


def reverse_invoice_adjustments(session: Session, invoice, actor: str | None = None) -> None:
    """On invoice void: the P1 reversal alone would leave adjustment entries
    dangling on AR — reverse every live adjustment entry too."""
    if not ledger_service.ledger_posting_enabled(session, invoice.company_id):
        return
    from gdx_dispatch.models.tenant_models import InvoiceAdjustment

    adjustments = session.scalars(
        select(InvoiceAdjustment).where(InvoiceAdjustment.invoice_id == invoice.id)
    ).all()
    for adjustment in adjustments:
        live = session.scalars(
            select(GlJournalEntry).where(
                GlJournalEntry.company_id == invoice.company_id,
                GlJournalEntry.source_type == "adjustment",
                GlJournalEntry.source_id == str(adjustment.id),
                GlJournalEntry.status == ENTRY_STATUS_POSTED,
            )
        ).all()
        for entry in live:
            reverse_entry(session, entry, created_by=actor)


# ---------------------------------------------------------------------------
# S8 — expenses (P5/P6, spec §5.5)
# ---------------------------------------------------------------------------

EVENT_EXPENSE = "recorded"


class ExpenseCompositionError(RuntimeError):
    """Expense lines don't sum to the header amount — refuse to post."""


def _expense_account_id(session: Session, settings, company_id: str, category: str | None):
    """category → mapped expense account id, else None (EXPENSE_FALLBACK).
    Canonicalizes first (audit round 4): historical rows carry the legacy
    frontend vocabulary (materials/supplies/…) — without normalization the
    whole backfill would land in 6900."""
    from gdx_dispatch.core.expense_categories import canonicalize_expense_category

    mapping = (settings.expense_category_account_map or {}) if settings else {}
    canonical = canonicalize_expense_category(category)
    acct_id = mapping.get(canonical or category or "")
    if not acct_id:
        return None
    try:
        acct_uuid = UUID(str(acct_id))
    except ValueError:
        return None
    account = session.get(GlAccount, acct_uuid)
    if account is None or account.company_id != company_id or not account.active:
        return None
    return account.id


def build_expense_lines(session: Session, expense) -> tuple[PostingLine, ...]:
    """P5: debit the category's mapped expense account (unmapped/dangling →
    6900 EXPENSE_FALLBACK, memo-flagged), credit Operating Bank — the
    paid-on-date simplification (spec §5.5 [JUDGMENT][CPA]). If detail lines
    exist they must sum to the header (refuse, never guess)."""
    amount_cents = to_cents(_dec(expense.amount))
    if amount_cents == 0:
        return ()
    company_id = expense.company_id
    settings = ledger_service.get_gl_settings(session, company_id)

    detail = [l for l in (expense.lines or [])]
    if detail:
        lines_sum = sum(to_cents(_dec(l.amount)) for l in detail)
        # Overshoot can never reconcile and must refuse; an UNDER-complete
        # set is a legitimate work-in-progress (lines build incrementally) —
        # the entry is header-level either way, lines are detail.
        if lines_sum > amount_cents:
            raise ExpenseCompositionError(
                f"expense lines sum to {lines_sum}c, over the header amount "
                f"{amount_cents}c — reconcile before posting"
            )

    account_id = _expense_account_id(session, settings, company_id, expense.category)
    memo = f"{expense.vendor}: {expense.category}"
    if account_id is None:
        debit = PostingLine(
            amount_cents=amount_cents,
            role=ROLE_EXPENSE_FALLBACK,
            job_id=expense.job_id,
            memo=f"[unmapped category: {expense.category}] {memo}",
        )
    else:
        debit = PostingLine(
            amount_cents=amount_cents,
            account_id=account_id,
            job_id=expense.job_id,
            memo=memo,
        )
    return (
        debit,
        PostingLine(
            amount_cents=-amount_cents,
            role=ROLE_OPERATING_BANK,
            job_id=expense.job_id,
            memo=f"paid {expense.vendor} on {expense.date}",
        ),
    )


def _live_expense_entry(session: Session, expense) -> GlJournalEntry | None:
    return session.scalars(
        select(GlJournalEntry).where(
            GlJournalEntry.company_id == expense.company_id,
            GlJournalEntry.source_type == "expense",
            GlJournalEntry.source_id == str(expense.id),
            GlJournalEntry.status == ENTRY_STATUS_POSTED,
            GlJournalEntry.idempotency_key.like(f"expense:{expense.id}:{EVENT_EXPENSE}:%"),
        )
    ).first()


def post_expense_recorded(session: Session, expense, actor: str | None = None):
    """P5 — no-op with the flag off or for zero-amount expenses."""
    if not ledger_service.ledger_posting_enabled(session, expense.company_id):
        return None
    lines = build_expense_lines(session, expense)
    if not lines:
        return None
    return post_for_event(
        session,
        PostingEvent(
            company_id=expense.company_id,
            source_type="expense",
            source_id=str(expense.id),
            event=EVENT_EXPENSE,
            effective_at=expense.date or date.today(),
            lines=lines,
            created_by=actor,
        ),
    )


def repost_expense(session: Session, expense, actor: str | None = None) -> None:
    """P6 — every mutation (PATCH, line add, category change) reverses the
    live entry and reposts at current content. Same post-first-then-compare
    shape as invoice edits; soft-deleted expenses reverse outright."""
    if not ledger_service.ledger_posting_enabled(session, expense.company_id):
        return
    live = _live_expense_entry(session, expense)
    if getattr(expense, "deleted_at", None) is not None:
        if live is not None:
            reverse_entry(session, live, created_by=actor)
        return
    lines = build_expense_lines(session, expense)
    if not lines:
        if live is not None:
            reverse_entry(session, live, created_by=actor)
        return
    posted = post_for_event(
        session,
        PostingEvent(
            company_id=expense.company_id,
            source_type="expense",
            source_id=str(expense.id),
            event=EVENT_EXPENSE,
            effective_at=expense.date or date.today(),
            lines=lines,
            created_by=actor,
        ),
    )
    if live is not None and posted.id != live.id:
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
        # No ("sent","paid") rule on purpose: P3 posts per-payment at the
        # recording sites (S6), not on the status transition; the flip is a
        # derived consequence of the money, never the money itself.
        # ("paid","sent") reopen after a payment void is likewise money-free.
    }
)
