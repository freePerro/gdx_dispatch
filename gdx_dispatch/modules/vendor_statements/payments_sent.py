"""Payments sent to a supplier, read back from the processor's confirmation
emails — and set beside what each statement says was applied.

A statement tells us what the supplier APPLIED (``account._diff``: cleared plus
paid-down balances, derived). It cannot tell us what we SENT. The supplier's
payment portal emails a confirmation for every e-check, and those emails are
already in ``outlook_messages``; this module reads them and puts the two numbers
side by side per statement, so a gap between them is visible instead of
reconstructed by hand. On the first real dataset (12 statements, 13
confirmations) the two agreed to the cent in every gap that had emails — and
the one gap that did not was exactly the question worth asking.

It REPORTS and never posts: no ``vendor_bill_payments`` row, no GL entry. The
confirmation is evidence the portal accepted a payment, not that the money
cleared — which is why each payment is also looked up in the bank feed.

Rules, in one place:

- A confirmation is a message FROM a sender on
  ``outlook_settings.payment_confirmation_sender_allowlist`` (address or
  domain; tenant data, never code — empty means the feature is off) whose
  subject is ``Payment Confirmation - <payee>``. Replies and forwards come
  from people, so the sender filter excludes them without a subject heuristic.
  A listed sender is trusted: anyone who can send from it can put a line on
  this report, which is one more reason each line carries its bank match.
- Attribution goes by the MERCHANT printed in the body, learned from the
  subject. A supplier group can run several trading names through one portal
  and one credit desk, and the payer picks the name: on the first real dataset
  five of thirteen payments to one account were confirmed under a sister
  company's name, and that statement applied every one of them. So a
  confirmation whose ``<payee>`` equals a statement's ``vendor_name`` (case-
  and space-insensitive) teaches "this merchant is that vendor", and every
  confirmation from that merchant is attributed to it — with the payee name it
  was paid under kept and shown, so a mislabelled payment stays visible. A
  merchant linked to two vendors, or a vendor name shared by two accounts, is
  ambiguous and falls back to exact-payee matching / no attribution; see
  ``_statements_for_vendor`` for why two account codes are never merged.
- The same confirmation filed in two folders is two rows with one processor
  transaction ID; it counts once.
- The payment date is the one printed in the email (the payer's local date),
  not ``received_at`` — a payment made at 11 PM arrives the next UTC day.
- A payment belongs to the first statement dated AFTER it. A statement is cut
  before that day's payments post: every confirmation dated on a statement
  date in the data shows up on the following statement, never its own. A
  payment older than the first statement has no "applied" to sit beside, so
  it counts toward the total but lands on no statement.
- A confirmation from a listed sender that is not on the page is COUNTED,
  never silently dropped: one that can't be read (the preview is cut at 255
  characters) is skipped, never guessed at; one that reads but no vendor
  claims (a payee name nothing matches) is unattributed. Both counts are
  tenant-wide — neither can be pinned to a vendor, which is the point — and a
  copy of the same email in a second folder counts once.
- Bank confirmation: one posted debit of exactly the same amount within
  ``BANK_WINDOW_DAYS`` after the payment, each debit used at most once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.bank_feeds.models import BankFeedTransaction
from gdx_dispatch.modules.outlook.models import OutlookMessage, OutlookSettings
from gdx_dispatch.modules.vendor_statements.account import (
    ZERO,
    _diff,
    _lines_for,
    _statements_for_vendor,
)

SUBJECT_PREFIX = "payment confirmation - "
BANK_WINDOW_DAYS = 7

# The confirmation body is a fixed layout: "$1,234.56 USD", the local date and
# time, the merchant on its own line, then "Transaction ID <id>".
_AMOUNT = re.compile(r"\$\s*([\d,]+\.\d{2})\s+USD\s+(\d{2}/\d{2}/\d{4})")
# The ID must be followed by whitespace: previews are cut at 255 characters,
# and an ID running into the cut is a prefix, not an ID.
_TXN = re.compile(r"Transaction ID\s+(\S+)(?=\s)")
_MERCHANT = re.compile(
    r"\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M[ \t]*\r?\n\s*([^\r\n]+)"
)


@dataclass
class SentPayment:
    paid_on: date
    amount: Decimal
    reference: str          # processor transaction ID
    message_id: object
    paid_as: str = ""       # the payee name the portal confirmed it under
    merchant: str = ""      # normalized merchant line from the body
    bank_posted_on: date | None = None


def _parse_confirmation(preview: str) -> tuple[date, Decimal, str, str] | None:
    """(paid_on, amount, transaction id, merchant), or None if the layout
    doesn't match — an unreadable message is skipped, never guessed at."""
    amt = _AMOUNT.search(preview or "")
    txn = _TXN.search(preview or "")
    if not amt or not txn:
        return None
    try:
        amount = Decimal(amt.group(1).replace(",", ""))
        paid_on = datetime.strptime(amt.group(2), "%m/%d/%Y").date()
    except (InvalidOperation, ValueError):
        return None
    merchant = _MERCHANT.search(preview or "")
    return paid_on, amount, txn.group(1), (merchant.group(1).strip() if merchant else "")


def _norm(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


def configured_senders(db: Session) -> list[str]:
    # Imported here, not at module top: vendor_bill_ingest pulls in the Celery
    # app, which imports it back — at module level that is a circular import
    # for anything (this router included) loaded before the outlook package.
    from gdx_dispatch.modules.outlook.vendor_bill_ingest import normalize_allowlist

    row = db.execute(select(OutlookSettings).limit(1)).scalars().first()
    return normalize_allowlist(row.payment_confirmation_sender_allowlist if row else None)


def confirmations(db: Session, senders: list[str]) -> tuple[list[SentPayment], int]:
    """Every parseable confirmation, once per processor transaction, oldest
    first — plus how many distinct confirmations could not be read."""
    if not senders:
        return [], 0
    from gdx_dispatch.modules.outlook.vendor_bill_ingest import sender_allowed  # see configured_senders

    rows = db.execute(
        select(OutlookMessage.id, OutlookMessage.from_address,
               OutlookMessage.subject, OutlookMessage.body_preview)
        .where(func.lower(OutlookMessage.subject).like(SUBJECT_PREFIX + "%"))
    ).all()

    seen: set[str] = set()
    out: list[SentPayment] = []
    unreadable: set[tuple[str, str]] = set()
    for msg_id, sender, subject, preview in rows:
        if not sender_allowed(sender, senders):
            continue
        if not _norm(subject).startswith(SUBJECT_PREFIX):
            continue
        parsed = _parse_confirmation(preview or "")
        if parsed is None:
            unreadable.add((_norm(subject), preview or ""))  # a folder copy is the same pair
            continue
        paid_on, amount, ref, merchant = parsed
        if ref in seen:
            continue  # the same confirmation filed in a second folder
        seen.add(ref)
        paid_as = " ".join((subject or "").split())[len(SUBJECT_PREFIX):].strip()
        out.append(SentPayment(paid_on, amount, ref, msg_id,
                               paid_as=paid_as, merchant=_norm(merchant)))
    out.sort(key=lambda p: (p.paid_on, p.reference))
    return out, len(unreadable)


def _attribute(payments: list[SentPayment], vendor_names: dict[str, int]) -> dict[str, list[SentPayment]]:
    """normalized vendor name -> its payments. ``vendor_names`` counts accounts
    per normalized name; a name held by two accounts gets nothing."""
    merchant_vendors: dict[str, set[str]] = {}
    for p in payments:
        if p.merchant and _norm(p.paid_as) in vendor_names:
            merchant_vendors.setdefault(p.merchant, set()).add(_norm(p.paid_as))

    out: dict[str, list[SentPayment]] = {}
    for p in payments:
        vendor = _norm(p.paid_as)
        if vendor not in vendor_names:
            learned = merchant_vendors.get(p.merchant, set())
            vendor = next(iter(learned)) if len(learned) == 1 else None
        if vendor is not None and vendor_names.get(vendor) == 1:
            out.setdefault(vendor, []).append(p)
    return out


def _attach_bank(db: Session, payments: list[SentPayment]) -> None:
    """Called ONCE with every attributed payment, so one debit can never
    confirm payments to two suppliers."""
    if not payments:
        return
    lo = min(p.paid_on for p in payments)
    hi = max(p.paid_on for p in payments) + timedelta(days=BANK_WINDOW_DAYS)
    cents = {-int(p.amount * 100) for p in payments}
    debits = db.execute(
        select(BankFeedTransaction.id, BankFeedTransaction.amount_cents,
               BankFeedTransaction.posted_date)
        .where(
            BankFeedTransaction.deleted_at.is_(None),
            BankFeedTransaction.pending.is_(False),
            BankFeedTransaction.posted_date.is_not(None),
            BankFeedTransaction.posted_date >= lo,
            BankFeedTransaction.posted_date <= hi,
            or_(*[BankFeedTransaction.amount_cents == c for c in cents]),
        )
        .order_by(BankFeedTransaction.posted_date)
    ).all()
    used: set = set()
    for p in sorted(payments, key=lambda p: (p.paid_on, p.reference)):
        want = -int(p.amount * 100)
        for txn_id, amount_cents, posted in debits:
            if txn_id in used or amount_cents != want:
                continue
            if p.paid_on <= posted <= p.paid_on + timedelta(days=BANK_WINDOW_DAYS):
                used.add(txn_id)
                p.bank_posted_on = posted
                break


@dataclass
class StatementPayments:
    statement_id: object
    statement_date: date
    sent_total: Decimal = ZERO
    sent_count: int = 0
    # Derived from the diff against the previous statement; None on the first.
    applied_total: Decimal | None = None


@dataclass
class VendorPayments:
    vendor_name: str
    vendor_code: str | None
    payments: list[SentPayment] = field(default_factory=list)
    statements: list[StatementPayments] = field(default_factory=list)
    sent_total: Decimal = ZERO
    after_latest_total: Decimal = ZERO
    after_latest_count: int = 0
    senders_configured: bool = False
    # Tenant-wide, repeated on every row: confirmations from a listed sender
    # that are on no vendor's page.
    unreadable_count: int = 0
    unattributed_count: int = 0


def build_vendor_payments(db: Session) -> list[VendorPayments]:
    grouped = _statements_for_vendor(db)
    name_count: dict[str, int] = {}
    for vendor_name, _code in grouped:
        name_count[_norm(vendor_name)] = name_count.get(_norm(vendor_name), 0) + 1
    senders = configured_senders(db)
    payments, unreadable = confirmations(db, senders)
    by_vendor = _attribute(payments, name_count)
    unattributed = len(payments) - sum(len(ps) for ps in by_vendor.values())
    _attach_bank(db, [p for ps in by_vendor.values() for p in ps])

    result: list[VendorPayments] = []
    for (vendor_name, code), statements in grouped.items():
        vp = VendorPayments(vendor_name=vendor_name, vendor_code=code,
                            senders_configured=bool(senders),
                            unreadable_count=unreadable, unattributed_count=unattributed)
        dated = sorted((s for s in statements if s.statement_date is not None),
                       key=lambda s: s.statement_date)

        prev_lines = None
        prev_date = None
        for s in dated:
            lines = _lines_for(db, s.id)
            row = StatementPayments(statement_id=s.id, statement_date=s.statement_date)
            if prev_lines is not None:
                row.applied_total = _diff(prev_lines, lines, prev_date).implied_payment_total
            vp.statements.append(row)
            prev_lines, prev_date = lines, s.statement_date

        vp.payments = by_vendor.get(_norm(vendor_name), [])

        for p in vp.payments:
            vp.sent_total += p.amount
            target = next((r for r in vp.statements if r.statement_date > p.paid_on), None)
            if target is not None and target is vp.statements[0]:
                continue  # older than the first statement: nothing to compare to
            if target is None:
                vp.after_latest_total += p.amount
                vp.after_latest_count += 1
            else:
                target.sent_total += p.amount
                target.sent_count += 1
        result.append(vp)
    return result
