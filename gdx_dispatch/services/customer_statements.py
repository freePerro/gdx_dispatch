"""Customer statements — a live account statement for one customer over a range.

Every rule below was broken once on real prod data by an earlier version of
this arithmetic; tests/test_customer_statements.py pins each one.

What is true, and where it comes from:

* How much of an invoice is settled is ``total − balance_due`` — the number the
  Pay page charges against. Payment rows are NOT a ledger: QuickBooks-era
  invoices are short of their rows, over them, and in three cases hold the same
  QuickBooks payment twice.
* A customer's credit on account is the ledger's 2300 balance.
* Payment rows, credit memos and applied credits only say WHEN an invoice's
  settlement happened. They are counted in date order up to the settled
  amount; a shortfall becomes one "no payment detail on record" entry.

An invoice whose records don't add up can still put wrong figures on a
statement, so it is reported as a warning (preview and audit row, never the
customer's PDF) whenever it can move a figure on that statement.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.money_format import format_money
from gdx_dispatch.core.pay_periods import shop_day_of, shop_today_from_settings
from gdx_dispatch.models.tenant_models import (
    AppSettings,
    Customer,
    Invoice,
    InvoiceAdjustment,
    Job,
    Payment,
)

log = logging.getLogger(__name__)

# Nothing before this date (Doug, 2026-09-15). QuickBooks-era payment rows are
# short of, over, or duplicated against their invoices, so a period reaching
# before 2026 would date settlement from records known to be wrong. Move this
# date back once those rows are repaired.
STATEMENT_EARLIEST = date(2026, 1, 1)
DEFAULT_PRESET = "last_90"
_DAY_PRESETS = {"last_30": 30, "last_60": 60, "last_90": 90}
_CENT = Decimal("0.01")
_TOLERANCE = Decimal("0.005")

KIND_LABELS = {
    "payment": "Payment",
    "credit_memo": "Credit memo",
    "credit_applied": "Credit applied",
    "unrecorded": "Paid — no payment detail on record",
}


class StatementRangeError(ValueError):
    """A range the statement cannot cover; the router answers 422."""


@dataclass(frozen=True)
class StatementRange:
    start: date
    end: date
    preset: str


@dataclass
class _Entry:
    day: date
    amount: Decimal
    kind: str
    invoice: Invoice
    method: str | None = None
    note: str | None = None


@dataclass
class _InvoiceState:
    invoice: Invoice
    day: date
    settled: Decimal
    records: list[tuple[date, Decimal]] = field(default_factory=list)
    counted: list[_Entry] = field(default_factory=list)
    remainder: _Entry | None = None
    anomalous: bool = False


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENT)


def _shop_tz(db: Session) -> str:
    return db.query(AppSettings.timezone).limit(1).scalar() or "America/New_York"


def _day_of_stamp(value: datetime | None, tz_name: str) -> date | None:
    """A stored instant as a shop calendar day.

    An instant at exactly 00:00:00 UTC is a date-only stamp — the QuickBooks
    importer and the backdated-payment path write dates that way — and is
    read as that UTC date, as the frontend's ``isDateOnlyStamp`` does. Read on
    the shop's calendar it would land the day before.
    """
    if value is None:
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    utc_moment = moment.astimezone(UTC)
    if utc_moment.time() == time(0, 0):
        return utc_moment.date()
    return shop_day_of(moment, tz_name)


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------

def completed_years(today: date, invoice_days: list[date]) -> list[int]:
    """Completed calendar years from the floor that hold an invoice."""
    years = {d.year for d in invoice_days}
    return [y for y in range(STATEMENT_EARLIEST.year, today.year) if y in years]


def resolve_range(
    today: date,
    *,
    preset: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> StatementRange:
    """Turn a preset or an explicit range into dates on the shop's calendar.

    Presets start no earlier than the floor and end no later than today.
    An explicit range is refused, not clamped, when it breaks either rule —
    the office typed those dates and should see why they don't work.
    """
    if start is not None or end is not None:
        if start is None or end is None:
            raise StatementRangeError("a custom range needs both a start and an end")
        if start < STATEMENT_EARLIEST:
            raise StatementRangeError(
                f"statements start on {STATEMENT_EARLIEST.isoformat()} or later"
            )
        if end > today:
            raise StatementRangeError("a statement cannot end after today")
        if end < start:
            raise StatementRangeError("the end date is before the start date")
        return StatementRange(start=start, end=end, preset="custom")

    key = preset or DEFAULT_PRESET
    if key in _DAY_PRESETS:
        begin = today - timedelta(days=_DAY_PRESETS[key] - 1)
        return StatementRange(start=max(begin, STATEMENT_EARLIEST), end=today, preset=key)
    if key == "ytd":
        return StatementRange(
            start=max(date(today.year, 1, 1), STATEMENT_EARLIEST), end=today, preset=key
        )
    if key.startswith("year_"):
        try:
            year = int(key.removeprefix("year_"))
        except ValueError as exc:
            raise StatementRangeError(f"unknown preset {key!r}") from exc
        if year < STATEMENT_EARLIEST.year or year >= today.year:
            raise StatementRangeError(
                f"year presets are completed years from {STATEMENT_EARLIEST.year}"
            )
        return StatementRange(start=date(year, 1, 1), end=date(year, 12, 31), preset=key)
    raise StatementRangeError(f"unknown preset {key!r}")


# ---------------------------------------------------------------------------
# Ledger reads
# ---------------------------------------------------------------------------

def _recognised_overpayments(
    db: Session, company_id: str, payment_ids_by_invoice: dict[UUID, list[str]]
) -> tuple[dict[UUID, Decimal], dict[str, Decimal]]:
    """The overpayment the ledger booked to 2300, per invoice and per payment.

    Sums every 2300 line on entries sourced from the invoice's payments —
    voided ones included, across all statuses, so reversals net — the
    ``invoice_ar_balance_cents`` pattern. Id lists are built in Python so the
    comparison is a string match (SQLite stores UUID columns dashless).

    Per payment matters: the ledger decides which payment overflowed in the
    order payments were recorded, net of every credit memo, so a payment
    dated earlier but recorded later can be the one that became credit.
    """
    by_invoice: dict[UUID, Decimal] = {}
    by_payment: dict[str, Decimal] = {}
    all_ids = [pid for ids in payment_ids_by_invoice.values() for pid in ids]
    if not all_ids:
        return by_invoice, by_payment
    from gdx_dispatch.modules.ledger.coa import LedgerConfigError, resolve_role_account
    from gdx_dispatch.modules.ledger.models import ROLE_CUSTOMER_CREDITS, GlJournalEntry, GlJournalLine

    try:
        credits_acct = resolve_role_account(db, company_id, ROLE_CUSTOMER_CREDITS)
    except LedgerConfigError:
        return by_invoice, by_payment
    rows = db.execute(
        select(GlJournalEntry.source_id, GlJournalLine.amount_cents)
        .join(GlJournalEntry, GlJournalLine.entry_id == GlJournalEntry.id)
        .where(
            GlJournalEntry.source_type == "payment",
            GlJournalEntry.source_id.in_(all_ids),
            GlJournalLine.account_id == credits_acct.id,
        )
    ).all()
    cents_by_payment: dict[str, int] = {}
    for source_id, amount_cents in rows:
        cents_by_payment[source_id] = cents_by_payment.get(source_id, 0) + int(amount_cents)
    # 2300 is credit-normal: stored negative.
    for pid, cents in cents_by_payment.items():
        if cents:
            by_payment[pid] = (Decimal(-cents) / 100).quantize(_CENT)
    for invoice_id, ids in payment_ids_by_invoice.items():
        total = sum((by_payment.get(pid, Decimal("0.00")) for pid in ids), Decimal("0.00"))
        if total:
            by_invoice[invoice_id] = total
    return by_invoice, by_payment


def _credit_on_account(db: Session, company_id: str, customer_id: UUID) -> Decimal:
    from gdx_dispatch.modules.ledger.coa import LedgerConfigError
    from gdx_dispatch.modules.ledger.rules import customer_credit_balance_cents
    from gdx_dispatch.modules.ledger.service import ledger_posting_enabled

    try:
        if not ledger_posting_enabled(db, company_id):
            return Decimal("0.00")
        cents = customer_credit_balance_cents(db, company_id, customer_id)
    except LedgerConfigError:
        return Decimal("0.00")
    return (Decimal(cents) / 100).quantize(_CENT) if cents > 0 else Decimal("0.00")


# ---------------------------------------------------------------------------
# The statement
# ---------------------------------------------------------------------------

def _aging_bucket(days_past_due: int | None) -> str:
    if days_past_due is None or days_past_due <= 0:
        return "current"
    if days_past_due <= 30:
        return "days_1_30"
    if days_past_due <= 60:
        return "days_31_60"
    if days_past_due <= 90:
        return "days_61_90"
    return "over_90"


def build_statement(
    db: Session,
    customer: Customer,
    rng: StatementRange,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Compute the statement. Reads only; never writes."""
    from gdx_dispatch.core.payments import public_pay_url

    today = today or shop_today_from_settings(db)
    tz_name = _shop_tz(db)
    company_id = str(customer.company_id)

    invoices = db.execute(
        select(Invoice)
        .where(
            Invoice.customer_id == customer.id,
            Invoice.deleted_at.is_(None),
            Invoice.status.notin_(("draft", "void")),
        )
    ).scalars().all()
    by_id = {inv.id: inv for inv in invoices}
    ids = list(by_id)

    payments = (
        db.execute(select(Payment).where(Payment.invoice_id.in_(ids))).scalars().all() if ids else []
    )
    adjustments = (
        db.execute(select(InvoiceAdjustment).where(InvoiceAdjustment.invoice_id.in_(ids))).scalars().all()
        if ids
        else []
    )
    job_ids = {inv.job_id for inv in invoices if inv.job_id}
    job_titles: dict[UUID, str] = {}
    if job_ids:
        for job_id, title in db.execute(
            select(Job.id, Job.title).where(Job.id.in_(job_ids), Job.deleted_at.is_(None))
        ).all():
            job_titles[job_id] = (title or "").strip()

    payment_ids_by_invoice: dict[UUID, list[str]] = {}
    for p in payments:
        payment_ids_by_invoice.setdefault(p.invoice_id, []).append(str(p.id))
    recognised, held_by_payment = _recognised_overpayments(db, company_id, payment_ids_by_invoice)

    # Settlement records per invoice, dated.
    records_by_invoice: dict[UUID, list[tuple[date, datetime, str, Decimal, str, str | None]]] = {}
    for p in payments:
        if p.voided_at is not None:
            continue
        day = min(p.payment_date or _day_of_stamp(p.created_at, tz_name) or today, today)
        records_by_invoice.setdefault(p.invoice_id, []).append(
            (day, p.created_at or datetime.min, str(p.id), _money(p.amount), "payment", p.method)
        )
    refunds: list[InvoiceAdjustment] = []
    for a in adjustments:
        if a.kind == "refund":
            refunds.append(a)
            continue
        if a.kind not in ("credit_memo", "credit_applied"):
            continue
        day = min(_day_of_stamp(a.created_at, tz_name) or today, today)
        records_by_invoice.setdefault(a.invoice_id, []).append(
            (day, a.created_at or datetime.min, str(a.id), _money(a.amount), a.kind, None)
        )

    states: list[_InvoiceState] = []
    for inv in invoices:
        inv_day = min(inv.invoice_date or _day_of_stamp(inv.created_at, tz_name) or today, today)
        settled = max(_money(inv.total) - _money(inv.balance_due), Decimal("0.00"))
        state = _InvoiceState(invoice=inv, day=inv_day, settled=settled)
        recs = sorted(records_by_invoice.get(inv.id, []), key=lambda r: (r[0], _sort_stamp(r[1]), r[2]))
        remaining = settled
        latest_nonzero: date | None = None
        for day, _created, rid, amount, kind, method in recs:
            state.records.append((day, amount))
            if amount != 0:
                latest_nonzero = day
            # The part of a payment the ledger booked as customer credit never
            # settled this invoice. It is taken off that payment — the one the
            # ledger names, not whichever payment happens to be dated last —
            # and the row says what was received, or a later refund or applied
            # credit would have no visible source on the customer's copy.
            held = (
                max(min(held_by_payment.get(rid, Decimal("0.00")), amount), Decimal("0.00"))
                if kind == "payment"
                else Decimal("0.00")
            )
            applies = amount - held
            take = min(applies, remaining) if remaining > 0 else Decimal("0.00")
            note = None
            if held > 0:
                note = f"{format_money(amount)} received; {format_money(held)} held as credit on account"
            if take > 0 or note:
                state.counted.append(
                    _Entry(day=day, amount=take, kind=kind, invoice=inv, method=method, note=note)
                )
                remaining -= take
        if remaining > _TOLERANCE:
            paid_day = _day_of_stamp(inv.paid_at, tz_name)
            rem_day = min(paid_day or latest_nonzero or inv_day, today)
            state.remainder = _Entry(day=rem_day, amount=remaining, kind="unrecorded", invoice=inv)
            state.counted.append(state.remainder)
        recorded_total = sum((amount for _d, amount in state.records), Decimal("0.00"))
        # A negative record cannot be placed by capping (it would silently drop
        # off the statement), so it always counts as records that don't add up.
        state.anomalous = (
            abs(recorded_total - settled - recognised.get(inv.id, Decimal("0.00"))) > _TOLERANCE
            or any(amount < 0 for _d, amount in state.records)
        )
        states.append(state)

    start, end = rng.start, rng.end

    # Total unpaid, aging, open invoices.
    aging = {k: Decimal("0.00") for k in ("current", "days_1_30", "days_31_60", "days_61_90", "over_90")}
    open_rows = []
    total_unpaid = Decimal("0.00")
    for state in sorted(states, key=lambda s: (s.day, s.invoice.invoice_number or "")):
        inv = state.invoice
        balance = _money(inv.balance_due)
        if balance <= 0:
            continue
        total_unpaid += balance
        days_past_due = (today - inv.due_date).days if inv.due_date else None
        aging[_aging_bucket(days_past_due)] += balance
        open_rows.append({
            "invoice_id": str(inv.id),
            "invoice_number": inv.invoice_number,
            "date": state.day.isoformat(),
            "job_title": job_titles.get(inv.job_id, "") if inv.job_id else "",
            "total": _money(inv.total),
            "balance": balance,
            "days_past_due": max(days_past_due, 0) if days_past_due is not None else None,
            "pay_url": public_pay_url(inv.public_token),
        })

    previous = Decimal("0.00")
    invoiced = Decimal("0.00")
    in_period = Decimal("0.00")
    period_rows = []
    entry_rows = []
    all_entries = [e for s in states for e in s.counted]
    for state in states:
        if state.day < start:
            previous += _money(state.invoice.total)
        elif state.day <= end:
            invoiced += _money(state.invoice.total)
            inv = state.invoice
            period_rows.append({
                "invoice_id": str(inv.id),
                "invoice_number": inv.invoice_number,
                "date": state.day.isoformat(),
                "job_title": job_titles.get(inv.job_id, "") if inv.job_id else "",
                "total": _money(inv.total),
                "paid": state.settled,
                "balance": _money(inv.balance_due),
            })
    for entry in sorted(all_entries, key=lambda e: (e.day, e.invoice.invoice_number or "")):
        if entry.day < start:
            previous -= entry.amount
        elif entry.day <= end:
            in_period += entry.amount
            entry_rows.append({
                "date": entry.day.isoformat(),
                "kind": entry.kind,
                "label": KIND_LABELS[entry.kind],
                "method": entry.method,
                "note": entry.note,
                "invoice_number": entry.invoice.invoice_number,
                "amount": entry.amount,
            })
    period_rows.sort(key=lambda r: (r["date"], r["invoice_number"] or ""))

    refund_rows = []
    for a in refunds:
        day = min(_day_of_stamp(a.created_at, tz_name) or today, today)
        if start <= day <= end:
            refund_rows.append({
                "date": day.isoformat(),
                "invoice_number": by_id[a.invoice_id].invoice_number,
                "amount": _money(a.amount),
                "method": a.refund_method,
            })
    refund_rows.sort(key=lambda r: r["date"])

    ending = previous + invoiced - in_period

    warnings = []
    for state in states:
        if not state.anomalous:
            continue
        reaches = (
            state.day >= start
            or any(day >= start and amount != 0 for day, amount in state.records)
            or (state.remainder is not None and state.remainder.day >= start)
        )
        if reaches:
            warnings.append({
                "kind": "records_dont_add_up",
                "invoice_id": str(state.invoice.id),
                "invoice_number": state.invoice.invoice_number,
                "message": "Payment records for this invoice don't add up; figures that depend on it may be wrong.",
            })
    draft_rows = db.execute(
        select(Invoice.id, Invoice.invoice_number)
        .where(
            Invoice.customer_id == customer.id,
            Invoice.deleted_at.is_(None),
            Invoice.status == "draft",
            Invoice.billing_type != "deposit",
            Invoice.id.in_(select(Payment.invoice_id).where(Payment.voided_at.is_(None))),
        )
    ).all()
    for draft_id, number in draft_rows:
        warnings.append({
            "kind": "payment_on_draft",
            "invoice_id": str(draft_id),
            "invoice_number": number,
            "message": "A payment is recorded on this draft invoice, which the statement does not show.",
        })
    warnings.sort(key=lambda w: (w["kind"], w["invoice_number"] or ""))

    return {
        "customer": {
            "id": str(customer.id),
            "name": customer.name or "",
            "address": customer.address or "",
            "email": customer.email or "",
        },
        "range": {"start": start.isoformat(), "end": end.isoformat(), "preset": rng.preset},
        "produced_on": today.isoformat(),
        "ends_today": end == today,
        "total_unpaid": total_unpaid,
        "aging": aging,
        "credit_on_account": _credit_on_account(db, company_id, customer.id),
        "open_invoices": open_rows,
        "previous_balance": previous,
        "invoices": period_rows,
        "invoiced": invoiced,
        "payments_and_credits": entry_rows,
        "payments_and_credits_total": in_period,
        "refunds": refund_rows,
        "ending_balance": ending,
        "warnings": warnings,
    }


def _sort_stamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def to_json(statement: dict[str, Any]) -> dict[str, Any]:
    """Decimals as floats for the API and the audit row."""
    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    return convert(statement)


def presets_for(db: Session, customer: Customer, today: date) -> list[dict[str, str]]:
    """The dialog's options: day presets, year to date, completed years, custom."""
    days = [
        inv_date
        for (inv_date,) in db.execute(
            select(Invoice.invoice_date).where(
                Invoice.customer_id == customer.id,
                Invoice.deleted_at.is_(None),
                Invoice.status.notin_(("draft", "void")),
                Invoice.invoice_date.is_not(None),
            )
        ).all()
    ]
    options = [
        {"key": "last_30", "label": "Last 30 days"},
        {"key": "last_60", "label": "Last 60 days"},
        {"key": "last_90", "label": "Last 90 days"},
        {"key": "ytd", "label": "Year to date"},
    ]
    options += [{"key": f"year_{y}", "label": str(y)} for y in reversed(completed_years(today, days))]
    options.append({"key": "custom", "label": "Custom range"})
    return options


def render_statement_pdf(db: Session, statement: dict[str, Any]) -> bytes:
    """The customer's PDF. Warnings are deliberately not in it."""
    from weasyprint import HTML

    from gdx_dispatch.core.pdf_generator import _JINJA_ENV, _TEMPLATES_DIR, _default_branding
    from gdx_dispatch.routers.pdf import _branding_payload

    html = _JINJA_ENV.get_template("statement_pdf.html").render(
        s=statement,
        branding=_default_branding(_branding_payload(db)),
        kind_labels=KIND_LABELS,
    )
    return HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf()
