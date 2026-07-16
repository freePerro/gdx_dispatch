"""Ledger reports (S11, spec §6 / §9).

Pure read-side derivations over ``gl_journal_lines``:

- **Trial balance** — per-account balance as of a date, rendered zero-proof
  (Σ all signed balances == 0; the invariant every entry enforces, summed).
- **P&L** — accrual straight from the GL; cash basis DERIVED at report time
  by walking each invoice's payment events cumulatively (cap at the invoice
  total — overpayment excess is 2300, never revenue) and prorating each
  recognition delta across the invoice's components (§6, Intuit's
  architecture; pre-cutover invoices prorate off operational lines per
  §5.7). Expenses are identical under both bases — the documented Phase 1
  simplification (paid-on-date).
- **Balance sheet** — asset/liability/equity balances as of a date with a
  computed retained-earnings rollup (no closing entries in Phase 1: RE =
  the accumulated net of every P&L-account line to date).
- **Journal** — the browsable entry list with drill to source documents.

Sign convention: ``amount_cents`` is debit-positive. Balances are presented
in each type's natural sign (assets/expenses debit-positive; liabilities/
equity/revenue credit-positive).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger.cash_basis import (
    invoice_components,
    prorate_event_cents,
)
from gdx_dispatch.modules.ledger.coa import resolve_role_account
from gdx_dispatch.modules.ledger.models import (
    GlAccount,
    GlJournalEntry,
    GlJournalLine,
)
from gdx_dispatch.modules.ledger.money import to_cents
from gdx_dispatch.modules.ledger.rules import IssuanceCompositionError, _dec

CREDIT_NATURAL = ("liability", "equity", "revenue")


def _line_rows(session: Session, company_id: str, *, start: date | None, end: date | None):
    """(account, amount_cents) pairs for every journal line in the window.
    All entry statuses on purpose: a reversed original and its reversal are
    both real postings whose amounts net to zero."""
    q = (
        select(GlAccount, GlJournalLine.amount_cents)
        .join(GlJournalLine, GlJournalLine.account_id == GlAccount.id)
        .join(GlJournalEntry, GlJournalLine.entry_id == GlJournalEntry.id)
        .where(GlJournalEntry.company_id == company_id)
    )
    if start is not None:
        q = q.where(GlJournalEntry.effective_at >= start)
    if end is not None:
        q = q.where(GlJournalEntry.effective_at <= end)
    return session.execute(q).all()


def _sum_by_account(rows) -> dict:
    totals: dict = {}
    for account, amount in rows:
        slot = totals.setdefault(
            account.id,
            {"account": account, "signed_cents": 0},
        )
        slot["signed_cents"] += amount
    return totals


def _natural(account: GlAccount, signed_cents: int) -> int:
    return -signed_cents if account.type in CREDIT_NATURAL else signed_cents


def trial_balance(session: Session, company_id: str, *, as_of: date) -> dict:
    totals = _sum_by_account(_line_rows(session, company_id, start=None, end=as_of))
    rows = []
    debit_total = 0
    credit_total = 0
    zero_proof = 0
    for slot in totals.values():
        account, signed = slot["account"], slot["signed_cents"]
        if signed == 0:
            continue
        debit = signed if signed > 0 else 0
        credit = -signed if signed < 0 else 0
        debit_total += debit
        credit_total += credit
        zero_proof += signed
        rows.append(
            {
                "account_id": str(account.id),
                "code": account.code,
                "name": account.name,
                "type": account.type,
                "debit_cents": debit,
                "credit_cents": credit,
            }
        )
    rows.sort(key=lambda r: r["code"])
    return {
        "as_of": str(as_of),
        "rows": rows,
        "totals": {
            "debit_cents": debit_total,
            "credit_cents": credit_total,
            # The rendered zero-proof (spec §9): Σ signed balances. Non-zero
            # would mean a broken entry got past the balance trigger.
            "zero_proof_cents": zero_proof,
        },
    }


def _pnl_sections(totals: dict, *, start: date, end: date, basis: str) -> dict:
    revenue_rows = []
    expense_rows = []
    revenue_total = 0
    expense_total = 0
    for slot in totals.values():
        account, signed = slot["account"], slot["signed_cents"]
        if signed == 0 or account.type not in ("revenue", "expense"):
            continue
        natural = _natural(account, signed)
        row = {
            "account_id": str(account.id),
            "code": account.code,
            "name": account.name,
            "amount_cents": natural,
        }
        if account.type == "revenue":
            revenue_rows.append(row)
            revenue_total += natural
        else:
            expense_rows.append(row)
            expense_total += natural
    revenue_rows.sort(key=lambda r: r["code"])
    expense_rows.sort(key=lambda r: r["code"])
    return {
        "basis": basis,
        "start": str(start),
        "end": str(end),
        "revenue": revenue_rows,
        "expenses": expense_rows,
        "totals": {
            "revenue_cents": revenue_total,
            "expense_cents": expense_total,
            "net_income_cents": revenue_total - expense_total,
        },
    }


def pnl_accrual(session: Session, company_id: str, *, start: date, end: date) -> dict:
    totals = _sum_by_account(_line_rows(session, company_id, start=start, end=end))
    return _pnl_sections(totals, start=start, end=end, basis="accrual")


@dataclass
class _CashSlot:
    account: GlAccount
    signed_cents: int = 0


def _cash_events(session: Session, invoice):
    """The invoice's cash-recognition events, dated and signed. Current-state
    semantics (Phase 1): voided payments don't count; refunds are negative
    cash events at their own date; applying stored customer credit (P9)
    recognizes previously-capped overpayment cash. Plain credit memos are NOT
    cash events — the S7 cap restricts them to the unpaid portion, which
    cash basis never recognized in the first place.

    Two documented Phase 1 policies: (1) adjustment events are dated by
    ``created_at`` in UTC — an evening local-time refund can land on the
    next calendar day; this matches how P-rules date the SAME event on the
    accrual ledger, so the two books never disagree with each other.
    (2) ``credit_applied`` recognizes at application date, not the original
    overpayment's receipt date. [JUDGMENT]"""
    from gdx_dispatch.models.tenant_models import InvoiceAdjustment, Payment

    events = []
    for payment in session.scalars(
        select(Payment).where(
            Payment.invoice_id == invoice.id, Payment.voided_at.is_(None)
        )
    ).all():
        when = payment.payment_date or (
            payment.created_at.date() if payment.created_at else None
        )
        if when is not None:
            events.append((when, to_cents(_dec(payment.amount))))
    for adjustment in session.scalars(
        select(InvoiceAdjustment).where(
            InvoiceAdjustment.invoice_id == invoice.id,
            InvoiceAdjustment.kind.in_(("refund", "credit_applied")),
        )
    ).all():
        when = adjustment.created_at.date() if adjustment.created_at else None
        if when is None:
            continue
        sign = -1 if adjustment.kind == "refund" else 1
        events.append((when, sign * to_cents(_dec(adjustment.amount))))
    events.sort(key=lambda e: e[0])
    return events


def cash_revenue_by_account(
    session: Session, company_id: str, *, start: date, end: date
) -> dict:
    """Report-time cash revenue: per invoice, walk events cumulatively,
    clamp recognition to [0, total], prorate each in-window delta across the
    invoice's components (operational lines — works identically for P8-era
    invoices, §5.7). Returns {account_id: {"account", "signed_cents"}} in the
    GL sign convention (revenue credits negative) so the shared section
    builder renders it."""
    from gdx_dispatch.models.tenant_models import Invoice

    buckets: dict = {}

    def _bucket(account: GlAccount, natural_cents: int) -> None:
        slot = buckets.setdefault(
            account.id, {"account": account, "signed_cents": 0}
        )
        # revenue recognition of +N == a credit of N == signed −N
        slot["signed_cents"] -= natural_cents

    skipped: list[dict] = []
    invoices = session.scalars(
        select(Invoice).where(
            Invoice.company_id == company_id,
            Invoice.deleted_at.is_(None),
            Invoice.status != "void",
            Invoice.status != "draft",
        )
    ).all()
    for invoice in invoices:
        total_cents = to_cents(_dec(invoice.total))
        if total_cents <= 0:
            continue
        events = _cash_events(session, invoice)
        if not events:
            continue
        components = None  # built lazily — most invoices have no in-window delta
        cumulative = 0
        recognized_prev = 0
        for when, amount in events:
            cumulative += amount
            recognized_now = min(max(cumulative, 0), total_cents)
            delta = recognized_now - recognized_prev
            recognized_prev = recognized_now
            if delta == 0 or when < start or when > end:
                continue
            if components is None:
                # One irreconcilable invoice (total ≠ lines + tax beyond
                # rounding — e.g. the line-less QB-imported population) must
                # skip ITSELF into the report, never 500 the whole P&L.
                # Same per-item discipline as the backfill replay.
                try:
                    components = invoice_components(session, invoice)
                except IssuanceCompositionError as exc:
                    skipped.append(
                        {
                            "invoice_number": invoice.invoice_number,
                            "reason": str(exc),
                        }
                    )
                    break
                if not components:
                    break
            parts = prorate_event_cents(
                [c.cents for c in components], delta, total_cents
            )
            for component, part in zip(components, parts):
                if part == 0:
                    continue
                if component.account_id is not None:
                    account = session.get(GlAccount, component.account_id)
                else:
                    account = resolve_role_account(
                        session, company_id, component.role
                    )
                if account is not None:
                    _bucket(account, part)
    return buckets, skipped


def pnl_cash(session: Session, company_id: str, *, start: date, end: date) -> dict:
    """Cash-basis P&L: derived revenue (above) + GL expense lines unchanged
    (expenses identical under both bases — Phase 1 simplification, spec §6).
    Non-revenue components (sales tax, rounding) recognized by the walk stay
    out of the P&L body: tax is a liability under either basis; rounding
    lands on its expense account via the accrual side already."""
    buckets, skipped = cash_revenue_by_account(session, company_id, start=start, end=end)
    revenue_buckets = {
        account_id: slot
        for account_id, slot in buckets.items()
        if slot["account"].type == "revenue"
    }
    expense_totals = {
        account_id: slot
        for account_id, slot in _sum_by_account(
            _line_rows(session, company_id, start=start, end=end)
        ).items()
        if slot["account"].type == "expense"
    }
    out = _pnl_sections(
        {**revenue_buckets, **expense_totals}, start=start, end=end, basis="cash"
    )
    # Visible omission beats silent truncation: any invoice whose cash events
    # couldn't be attributed is named in the payload.
    out["skipped_invoices"] = skipped
    return out


def balance_sheet(session: Session, company_id: str, *, as_of: date) -> dict:
    totals = _sum_by_account(_line_rows(session, company_id, start=None, end=as_of))
    sections: dict[str, list] = {"asset": [], "liability": [], "equity": []}
    section_totals = {"asset": 0, "liability": 0, "equity": 0}
    retained_cents = 0  # computed rollup — no closing entries in Phase 1
    for slot in totals.values():
        account, signed = slot["account"], slot["signed_cents"]
        if signed == 0:
            continue
        if account.type in ("revenue", "expense"):
            retained_cents += -signed  # net income in natural sign
            continue
        natural = _natural(account, signed)
        sections[account.type].append(
            {
                "account_id": str(account.id),
                "code": account.code,
                "name": account.name,
                "amount_cents": natural,
            }
        )
        section_totals[account.type] += natural
    for rows in sections.values():
        rows.sort(key=lambda r: r["code"])
    sections["equity"].append(
        {
            "account_id": None,
            "code": "3900*",
            "name": "Retained Earnings (computed)",
            "amount_cents": retained_cents,
        }
    )
    equity_total = section_totals["equity"] + retained_cents
    return {
        "as_of": str(as_of),
        "assets": sections["asset"],
        "liabilities": sections["liability"],
        "equity": sections["equity"],
        "totals": {
            "asset_cents": section_totals["asset"],
            "liability_cents": section_totals["liability"],
            "equity_cents": equity_total,
            # zero-proof: A − L − E == 0 by the entry balance invariant
            "zero_proof_cents": section_totals["asset"]
            - section_totals["liability"]
            - equity_total,
        },
    }


# ---------------------------------------------------------------------------
# Journal browser (spec §9 drill: report → entry → line → source document)
# ---------------------------------------------------------------------------

def _source_descriptor(session: Session, entry: GlJournalEntry) -> dict:
    """What the entry is ABOUT, in clickable terms."""
    from gdx_dispatch.models.tenant_models import Expense, Invoice, Payment

    out: dict = {"source_type": entry.source_type, "source_id": entry.source_id}
    try:
        source_uuid = UUID(entry.source_id) if entry.source_id else None
        if entry.source_type == "invoice" and source_uuid:
            invoice = session.get(Invoice, source_uuid)
            if invoice is not None:
                out.update(
                    invoice_id=str(invoice.id), invoice_number=invoice.invoice_number
                )
        elif entry.source_type == "payment" and source_uuid:
            payment = session.get(Payment, source_uuid)
            if payment is not None and payment.invoice_id:
                out["invoice_id"] = str(payment.invoice_id)
                invoice = session.get(Invoice, payment.invoice_id)
                if invoice is not None:
                    out["invoice_number"] = invoice.invoice_number
        elif entry.source_type == "adjustment" and source_uuid:
            from gdx_dispatch.models.tenant_models import InvoiceAdjustment

            adjustment = session.get(InvoiceAdjustment, source_uuid)
            if adjustment is not None:
                out["adjustment_kind"] = adjustment.kind
                out["invoice_id"] = str(adjustment.invoice_id)
                invoice = session.get(Invoice, adjustment.invoice_id)
                if invoice is not None:
                    out["invoice_number"] = invoice.invoice_number
        elif entry.source_type == "expense" and source_uuid:
            expense = session.get(Expense, source_uuid)
            if expense is not None:
                out.update(
                    expense_id=str(expense.id),
                    vendor=expense.vendor,
                    category=expense.category,
                )
                from gdx_dispatch.modules.ledger.models import ExpenseReceipt

                receipts = session.scalars(
                    select(ExpenseReceipt).where(
                        ExpenseReceipt.expense_id == expense.id,
                        ExpenseReceipt.deleted_at.is_(None),
                    )
                ).all()
                out["receipts"] = [
                    {
                        "id": str(r.id),
                        "filename": r.filename,
                        "download_url": f"/api/expenses/{expense.id}/receipts/{r.id}/download",
                    }
                    for r in receipts
                ]
    except Exception:  # a broken source row must never 500 the journal
        out["source_lookup_failed"] = True
    return out


def journal_page(
    session: Session,
    company_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
    source_type: str | None = None,
    account_id: str | None = None,
) -> dict:
    from sqlalchemy import func

    q = select(GlJournalEntry).where(GlJournalEntry.company_id == company_id)
    count_q = select(func.count()).select_from(GlJournalEntry).where(
        GlJournalEntry.company_id == company_id
    )
    if source_type:
        q = q.where(GlJournalEntry.source_type == source_type)
        count_q = count_q.where(GlJournalEntry.source_type == source_type)
    if account_id:
        line_match = select(GlJournalLine.entry_id).where(
            GlJournalLine.account_id == account_id
        )
        q = q.where(GlJournalEntry.id.in_(line_match))
        count_q = count_q.where(GlJournalEntry.id.in_(line_match))
    total = session.scalar(count_q) or 0
    entries = session.scalars(
        q.order_by(GlJournalEntry.posted_at.desc(), GlJournalEntry.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    account_cache: dict = {}

    def _account(account_id):
        if account_id not in account_cache:
            account_cache[account_id] = session.get(GlAccount, account_id)
        return account_cache[account_id]

    rows = []
    for entry in entries:
        lines = session.scalars(
            select(GlJournalLine).where(GlJournalLine.entry_id == entry.id)
        ).all()
        rows.append(
            {
                "id": str(entry.id),
                "entry_no": entry.entry_no,
                "effective_at": str(entry.effective_at),
                "posted_at": entry.posted_at.isoformat() if entry.posted_at else None,
                "status": entry.status,
                "reverses_entry_id": str(entry.reverses_entry_id)
                if entry.reverses_entry_id
                else None,
                "source": _source_descriptor(session, entry),
                "lines": [
                    {
                        "account_id": str(line.account_id),
                        "account_code": getattr(_account(line.account_id), "code", "?"),
                        "account_name": getattr(_account(line.account_id), "name", "?"),
                        "amount_cents": line.amount_cents,
                        "memo": line.memo,
                    }
                    for line in lines
                ],
            }
        )
    return {"total": total, "limit": limit, "offset": offset, "entries": rows}
