"""Vendor account view — what is actually owed, from the statements we hold.

A statement of account is a SNAPSHOT of open items, not a list of new charges:
an unpaid invoice reappears on every statement until it clears. Summing
statements therefore double-counts enormously — on the 11 statements GDX held
on 2026-07-28 the naive sum was $556,880.66 against a real open balance of
$36,855.52, off by a factor of fifteen. Everything here follows from the one
rule that avoids that:

    **The most recent statement is the current position. Older ones are history.**

Two facts the supplier already encodes, which this module reads rather than
recomputes:

- ``amount`` is what the invoice was, ``balance`` is what is still open. The
  difference is what has been paid against it, so partial payments need no
  separate record to be visible.
- Diffing consecutive statements shows what moved: invoices that vanished were
  cleared, invoices whose balance fell were paid down, invoices that appeared
  are new charges. The supplier keeps the ledger; we read it back.

That diff is INFERENCE, and is labelled as such wherever it surfaces. It is
only as granular as the statement cadence (10–29 days, irregularly, in the data
so far): two payments inside one gap merge into a single implied figure, a
credit memo or a returned part looks exactly like a payment, and a statement
that never arrives silently merges two periods. It is an excellent hint and a
poor journal entry — which is precisely why this module reports and does not
post.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.vendor_statements.models import VendorStatement, VendorStatementLine

ZERO = Decimal("0.00")

# Oldest-first, so the aging summary reads the way an A/P person scans it.
AGING_ORDER = ["120+", "90-119", "60-89", "30-59", "0-29", "current", "retainage"]


@dataclass
class AccountLine:
    """One invoice as it stands on the latest statement."""
    invoice_no: str
    line_date: date | None
    amount: Decimal          # what the invoice was
    paid: Decimal            # amount - balance; what has been applied to it
    balance: Decimal         # what is still open
    aging_bucket: str | None
    vendor_job_no: str | None
    po_ref: str | None
    description: str | None
    classification: str | None
    matched_job_id: object | None
    first_seen_on: date | None   # earliest statement this invoice appeared on
    statements_seen: int         # how many statements it has ridden


@dataclass
class AccountChange:
    """What moved between the previous statement and the latest one."""
    previous_statement_date: date | None
    new_invoice_count: int = 0
    new_invoice_total: Decimal = ZERO
    cleared_count: int = 0
    cleared_total: Decimal = ZERO
    paid_down_count: int = 0
    paid_down_total: Decimal = ZERO

    @property
    def implied_payment_total(self) -> Decimal:
        """Cleared balances plus balance reductions — i.e. what the money did.

        DERIVED, never recorded. See the module docstring for what it can't
        tell apart (a credit memo reads as a payment; two payments in one gap
        read as one).
        """
        return self.cleared_total + self.paid_down_total


@dataclass
class VendorAccount:
    vendor_name: str
    vendor_code: str | None
    as_of: date | None
    statement_id: object
    open_balance: Decimal = ZERO
    open_line_count: int = 0
    original_total: Decimal = ZERO
    paid_to_date: Decimal = ZERO
    oldest_line_date: date | None = None
    aging: dict[str, Decimal] = field(default_factory=dict)
    lines: list[AccountLine] = field(default_factory=list)
    change: AccountChange | None = None
    statement_count: int = 0

    def days_oldest_open(self, today: date) -> int | None:
        if self.oldest_line_date is None:
            return None
        return (today - self.oldest_line_date).days


def _statements_for_vendor(db: Session) -> dict[tuple[str, str | None], list[VendorStatement]]:
    """Live statements grouped by ACCOUNT, newest first, one per period.

    Keyed on ``(vendor_name, vendor_code)`` rather than the name alone.
    ``vendor_code`` is our customer number with that supplier, and two codes are
    two separate accounts: merging them would make the diff compare one
    account's invoices against the other's, reading every line of the first as
    "cleared" and every line of the second as "new" — fabricating a five-figure
    payment that never happened. If a supplier ever renumbers an account this
    splits history into two cards instead, which is wrong in a way you can SEE.
    Given the choice, fail visibly.

    Statements sharing a ``statement_date`` are collapsed to the most recently
    created one. The manual upload path dedupes on content hash only — it never
    consults ``find_statement_by_period`` (the email path does) — so re-uploading
    a byte-different re-print of a month already on file really does create a
    twin row. Left alone, that twin becomes ``previous``, the diff compares a
    statement against itself, and a genuine chunk payment silently reports as
    "nothing moved". The reader refuses to be fooled by that rather than
    trusting the writer not to produce it.
    """
    rows = db.execute(
        select(VendorStatement)
        .where(VendorStatement.deleted_at.is_(None))
        .order_by(VendorStatement.statement_date.desc().nullslast(),
                  VendorStatement.created_at.desc())
    ).scalars().all()

    grouped: dict[tuple[str, str | None], list[VendorStatement]] = {}
    seen_periods: dict[tuple[str, str | None], set] = {}
    for row in rows:
        key = (row.vendor_name, row.vendor_code)
        # Undated statements have no period to collide on — keep them all
        # rather than collapsing every one into a single "None" period.
        if row.statement_date is not None:
            periods = seen_periods.setdefault(key, set())
            if row.statement_date in periods:
                continue  # a twin of a period we already took (newest wins)
            periods.add(row.statement_date)
        grouped.setdefault(key, []).append(row)
    return grouped


def _lines_for(db: Session, statement_id) -> list[VendorStatementLine]:
    return list(db.execute(
        select(VendorStatementLine)
        .where(VendorStatementLine.statement_id == statement_id)
        .order_by(VendorStatementLine.line_no.asc())
    ).scalars().all())


def _history_index(db: Session, statement_ids: list) -> dict[str, tuple[date | None, int]]:
    """Per invoice number: earliest statement date it appeared on, and how many
    statements it has appeared on. This is what turns "it's on the statement
    again" into "this has been open since January" — the question the repeated
    lines are actually asking."""
    if not statement_ids:
        return {}
    rows = db.execute(
        select(VendorStatementLine.vendor_invoice_no, VendorStatement.statement_date)
        .join(VendorStatement, VendorStatement.id == VendorStatementLine.statement_id)
        .where(VendorStatementLine.statement_id.in_(statement_ids))
    ).all()
    index: dict[str, tuple[date | None, int]] = {}
    for invoice_no, stmt_date in rows:
        first, count = index.get(invoice_no, (None, 0))
        if stmt_date is not None and (first is None or stmt_date < first):
            first = stmt_date
        index[invoice_no] = (first, count + 1)
    return index


def _diff(previous: list[VendorStatementLine], current: list[VendorStatementLine],
          previous_date: date | None) -> AccountChange:
    """What moved between two statements, by invoice number."""
    change = AccountChange(previous_statement_date=previous_date)
    prev_by_no = {ln.vendor_invoice_no: ln for ln in previous}
    cur_by_no = {ln.vendor_invoice_no: ln for ln in current}

    for invoice_no, cur in cur_by_no.items():
        prev = prev_by_no.get(invoice_no)
        if prev is None:
            # BALANCE, not amount: this is what the invoice ADDED to the
            # position. An invoice can arrive already part-paid (real data has
            # one first appearing at $6,543.52 with $4,000 applied), and
            # nothing distinguishes money applied during this gap from money
            # applied before the invoice first appeared — so that difference is
            # deliberately not claimed as a payment in this period.
            change.new_invoice_count += 1
            change.new_invoice_total += cur.balance or ZERO
        elif (cur.balance or ZERO) < (prev.balance or ZERO):
            change.paid_down_count += 1
            change.paid_down_total += (prev.balance or ZERO) - (cur.balance or ZERO)

    for invoice_no, prev in prev_by_no.items():
        if invoice_no not in cur_by_no:
            # Off the statement entirely = settled. (A vendor could also drop a
            # written-off line, which would read the same — see module notes.)
            change.cleared_count += 1
            change.cleared_total += prev.balance or ZERO

    return change


def build_vendor_accounts(db: Session) -> list[VendorAccount]:
    """One account position per vendor, newest statement first.

    Vendors are keyed by the name the parser recorded. Statements without a
    date sort last — an undated statement can't be trusted as "latest", so it
    never displaces a dated one.
    """
    accounts: list[VendorAccount] = []

    for (vendor_name, _code), statements in _statements_for_vendor(db).items():
        latest = statements[0]
        previous = statements[1] if len(statements) > 1 else None

        lines = _lines_for(db, latest.id)
        history = _history_index(db, [s.id for s in statements])

        account = VendorAccount(
            vendor_name=vendor_name,
            vendor_code=latest.vendor_code,
            as_of=latest.statement_date,
            statement_id=latest.id,
            statement_count=len(statements),
        )

        for ln in lines:
            amount = ln.amount or ZERO
            balance = ln.balance or ZERO
            first_seen, seen_count = history.get(ln.vendor_invoice_no, (None, 0))
            account.lines.append(AccountLine(
                invoice_no=ln.vendor_invoice_no,
                line_date=ln.line_date,
                amount=amount,
                # Clamped at zero: a balance above the original (a later charge
                # rolled onto the same invoice) is not "negative paid".
                paid=max(amount - balance, ZERO),
                balance=balance,
                aging_bucket=ln.aging_bucket,
                vendor_job_no=ln.vendor_job_no,
                po_ref=ln.po_ref,
                description=ln.description,
                classification=ln.classification,
                matched_job_id=ln.matched_job_id,
                first_seen_on=first_seen,
                statements_seen=seen_count,
            ))
            account.original_total += amount
            account.open_balance += balance
            account.paid_to_date += max(amount - balance, ZERO)
            if balance > ZERO:
                account.open_line_count += 1
                bucket = ln.aging_bucket or "current"
                account.aging[bucket] = account.aging.get(bucket, ZERO) + balance
            if ln.line_date is not None and (
                account.oldest_line_date is None or ln.line_date < account.oldest_line_date
            ):
                account.oldest_line_date = ln.line_date

        if previous is not None:
            account.change = _diff(
                _lines_for(db, previous.id), lines, previous.statement_date
            )

        # Oldest money first — that's the order the office chases it in.
        account.lines.sort(
            key=lambda row: (AGING_ORDER.index(row.aging_bucket)
                             if row.aging_bucket in AGING_ORDER else len(AGING_ORDER),
                             row.line_date or date.max)
        )
        accounts.append(account)

    accounts.sort(key=lambda a: a.open_balance, reverse=True)
    return accounts
