"""Statement ↔ books matching — suggest-and-confirm, and the four
verification reports that are this feature's actual product.

Rule ladder (adapted from GL Phase 2 §3.3 for operational records; first
hit wins, one suggestion per line, everything SUGGEST-ONLY — the office
confirms; nothing auto-confirms in this phase):

  R5  classification — bank-only reality: service charges → bank_fee,
      interest rows → interest, inter-account transfers (both grammars the
      corpus contains) → transfer. A confirmed classification removes the
      line from the exception reports without inventing a books record.
  R2  exact 1:1 — deposit ↔ single non-voided Payment of equal amount
      within ±3 business days, with NO competing candidate on either side
      (bipartite degree 1); debit/check ↔ single Expense (same test), else
      single open/paid VendorInvoice total within ±10 days (weak, 0.60).
  R3  deposit sweep — a deposit slip is a BATCH: find the unique subset
      (n≤12 candidates, k≤6) of unconsumed non-card payments in
      [date−7d, date+1d] summing exactly to the deposit. Ambiguity (two
      subsets) means NO suggestion — ambiguous money is manual by design.

Invariants: matches never mutate the matched records; every confirm is
reversible (unconfirm → suggested); a books record or line sits in at
most one non-rejected match (partial unique on the children's denormalized
``match_status``, synced here — single writer).

Money comparison is integer cents on both sides; Payment/Expense amounts
are Numeric dollars converted with Decimal, never float arithmetic.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Customer, Expense, Invoice, Payment
from gdx_dispatch.modules.bank_feeds.statement_models import (
    MATCH_CONFIRMED,
    MATCH_REJECTED,
    MATCH_SUGGESTED,
    SECTION_CHECK,
    SECTION_DEBIT,
    SECTION_DEPOSIT,
    SECTION_INTEREST,
    SECTION_SERVICE_CHARGE,
    BankAccount,
    BankMatch,
    BankMatchExternal,
    BankMatchLine,
    BankStatementLine,
)

try:
    from gdx_dispatch.modules.vendor_invoices.models import VendorInvoice
except ImportError:  # pragma: no cover — module optional
    VendorInvoice = None

# Both transfer grammars observed in the real corpus (§6 R5 note): the
# second wraps its counterpart line, so match on the ANCHOR text.
_TRANSFER_RES = (
    re.compile(r"^Transfer (?:from|to) x\d{4} to x\d{4}", re.IGNORECASE),
    re.compile(r"^Trnsfr (?:Frm|To) Act Ending in \d{4}", re.IGNORECASE),
)

R2_BUSINESS_DAYS = 3
R3_WINDOW_BACK_DAYS = 7
R3_WINDOW_FWD_DAYS = 1
R3_MAX_CANDIDATES = 12
R3_MAX_SUBSET = 6
VENDOR_INVOICE_WINDOW_DAYS = 10

# Card-family payments settle through the processor as batched payouts,
# not as per-payment deposit-slip money — excluded from R3 sweeps.
_PROCESSOR_METHODS = {"card", "credit", "credit_card", "debit_card", "stripe"}


def _cents(amount) -> int:
    return int(Decimal(str(amount)) * 100)


def _business_days_apart(a: date, b: date) -> int:
    lo, hi = min(a, b), max(a, b)
    days = 0
    cursor = lo
    while cursor < hi:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _anchor(line: BankStatementLine) -> str:
    return line.description.split("\n", 1)[0]


def _is_transfer(line: BankStatementLine) -> bool:
    text = _anchor(line).strip()
    return any(rx.match(text) for rx in _TRANSFER_RES)


# ── suggestion engine ──────────────────────────────────────────────────


def _matched_line_ids(db: Session) -> set:
    return set(db.scalars(
        select(BankMatchLine.line_id).where(BankMatchLine.match_status != MATCH_REJECTED)
    ).all())


def _used_externals(db: Session) -> set:
    return set(db.execute(
        select(BankMatchExternal.source_table, BankMatchExternal.source_id)
        .where(BankMatchExternal.match_status != MATCH_REJECTED)
    ).all())


def _rejected_signatures(db: Session, account: BankAccount) -> set:
    """(line-id-set, external-set) of every REJECTED match on the account —
    a re-run must not resurrect a pairing the office already rejected
    (stack-audit F2: rejection is sticky, not a per-run opinion)."""
    signatures = set()
    for match in db.scalars(
        select(BankMatch).where(
            BankMatch.bank_account_id == account.id,
            BankMatch.status == MATCH_REJECTED,
        )
    ).all():
        line_ids = frozenset(db.scalars(
            select(BankMatchLine.line_id).where(BankMatchLine.match_id == match.id)).all())
        externals = frozenset(
            (row[0], row[1]) for row in db.execute(
                select(BankMatchExternal.source_table, BankMatchExternal.source_id)
                .where(BankMatchExternal.match_id == match.id)).all()
        )
        signatures.add((line_ids, externals))
    return signatures


def _new_match(db: Session, account: BankAccount, rule: str, *, lines,
               externals=(), classification=None, confidence=None, note=None,
               rejected_signatures=None) -> BankMatch | None:
    if rejected_signatures is not None:
        signature = (frozenset(l.id for l in lines), frozenset(externals))
        if signature in rejected_signatures:
            return None
    match = BankMatch(
        bank_account_id=account.id, rule=rule, status=MATCH_SUGGESTED,
        classification=classification, confidence=confidence, note=note,
        created_by="matcher",
    )
    db.add(match)
    db.flush()
    for line in lines:
        db.add(BankMatchLine(match_id=match.id, line_id=line.id, match_status=MATCH_SUGGESTED))
    for source_table, source_id in externals:
        db.add(BankMatchExternal(match_id=match.id, source_table=source_table,
                                 source_id=source_id, match_status=MATCH_SUGGESTED))
    return match


def suggest_matches(db: Session, account: BankAccount, date_from: date, date_to: date) -> dict:
    """Re-run the ladder over [date_from, date_to]. Existing SUGGESTED
    matches touching the range are wiped first (they are derived data);
    confirmed and rejected matches are never touched.

    Concurrency (PR-3 audit findings): on Postgres an advisory xact lock
    serializes suggest runs per account, and every DELETE re-checks
    status='suggested' IN THE STATEMENT — a match the office confirms
    between our SELECT and DELETE survives (its status changed), instead
    of a confirmed reconciliation silently vanishing. A commit-time
    IntegrityError (a concurrent writer won an exclusivity index) rolls
    back and surfaces as ValueError, never a half-written run."""
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy import text as _sql_text

        db.execute(_sql_text(
            "SELECT pg_advisory_xact_lock(hashtext(:key))"
        ), {"key": f"bank_reconcile_{account.id}"})

    candidate_ids = list(db.scalars(
        select(BankMatch.id)
        .join(BankMatchLine, BankMatchLine.match_id == BankMatch.id)
        .join(BankStatementLine, BankStatementLine.id == BankMatchLine.line_id)
        .where(
            BankMatch.status == MATCH_SUGGESTED,
            BankMatch.bank_account_id == account.id,
            BankStatementLine.txn_date >= date_from,
            BankStatementLine.txn_date <= date_to,
        ).distinct()
    ).all())
    if candidate_ids:
        # Re-select WITH row locks and the status re-checked: anything the
        # office confirmed since the first SELECT drops out here, and a
        # concurrent confirm on the locked rows blocks until we commit.
        stale_ids = list(db.scalars(
            select(BankMatch.id).where(
                BankMatch.id.in_(candidate_ids),
                BankMatch.status == MATCH_SUGGESTED,
            ).with_for_update()
        ).all())
        if stale_ids:
            db.query(BankMatchLine).filter(
                BankMatchLine.match_id.in_(stale_ids)).delete(synchronize_session=False)
            db.query(BankMatchExternal).filter(
                BankMatchExternal.match_id.in_(stale_ids)).delete(synchronize_session=False)
            db.query(BankMatch).filter(
                BankMatch.id.in_(stale_ids)).delete(synchronize_session=False)
    db.flush()

    matched_lines = _matched_line_ids(db)
    used_externals = _used_externals(db)
    rejected_sigs = _rejected_signatures(db, account)

    lines = [
        line for line in db.scalars(
            select(BankStatementLine).where(
                BankStatementLine.bank_account_id == account.id,
                BankStatementLine.txn_date >= date_from,
                BankStatementLine.txn_date <= date_to,
            ).order_by(BankStatementLine.txn_date)
        ).all()
        if line.id not in matched_lines
    ]

    stats = {"classified": 0, "r2_payments": 0, "r3_sweeps": 0,
             "r2_expenses": 0, "r2_vendor_invoices": 0, "unmatched": 0}

    # ── R5 classification ──────────────────────────────────────────────
    remaining = []
    for line in lines:
        if line.section == SECTION_SERVICE_CHARGE:
            created = _new_match(db, account, "R5", lines=[line], classification="bank_fee",
                                 confidence=0.99, rejected_signatures=rejected_sigs)
        elif line.section == SECTION_INTEREST:
            created = _new_match(db, account, "R5", lines=[line], classification="interest",
                                 confidence=0.99, rejected_signatures=rejected_sigs)
        elif _is_transfer(line):
            created = _new_match(db, account, "R5", lines=[line], classification="transfer",
                                 confidence=0.95, rejected_signatures=rejected_sigs)
        else:
            remaining.append(line)
            continue
        if created is not None:
            stats["classified"] += 1
    lines = remaining

    deposits = [l for l in lines if l.section == SECTION_DEPOSIT]
    debits = [l for l in lines if l.section in (SECTION_DEBIT, SECTION_CHECK)]

    # ── R2 deposits: exact 1:1, bipartite degree 1 on both sides ───────
    window_lo = date_from - timedelta(days=R3_WINDOW_BACK_DAYS)
    window_hi = date_to + timedelta(days=R3_WINDOW_BACK_DAYS)
    payments = [
        p for p in db.scalars(
            select(Payment).where(
                Payment.voided_at.is_(None),
                Payment.payment_date >= window_lo,
                Payment.payment_date <= window_hi,
            )
        ).all()
        if ("payments", p.id) not in used_externals
    ]

    def r2_candidates(line):
        return [p for p in payments
                if _cents(p.amount) == line.amount_cents
                and _business_days_apart(p.payment_date, line.txn_date) <= R2_BUSINESS_DAYS]

    line_to_payments = {l.id: r2_candidates(l) for l in deposits}

    # The payment side of the degree check must see competitors the request
    # window (and this account) can't: a same-amount deposit a few days
    # outside the range — or on the other account — is a live rival for the
    # same payment (PR-3 audit: "unique" was only unique inside the query
    # window). Competitor pool = ALL unmatched deposit lines, any account,
    # over the padded window.
    pad = timedelta(days=R3_WINDOW_BACK_DAYS)
    competitor_deposits = [
        l for l in db.scalars(
            select(BankStatementLine).where(
                BankStatementLine.section == SECTION_DEPOSIT,
                BankStatementLine.txn_date >= date_from - pad,
                BankStatementLine.txn_date <= date_to + pad,
            )
        ).all()
        if l.id not in matched_lines and not _is_transfer(l)
    ]
    payment_to_lines: dict = {}
    for line in competitor_deposits:
        for p in payments:
            if _cents(p.amount) == line.amount_cents \
                    and _business_days_apart(p.payment_date, line.txn_date) <= R2_BUSINESS_DAYS:
                payment_to_lines.setdefault(p.id, []).append(line.id)

    r2_matched_payment_ids = set()
    still_open_deposits = []
    for line in deposits:
        candidates = line_to_payments[line.id]
        if len(candidates) == 1 and len(payment_to_lines[candidates[0].id]) == 1:
            payment = candidates[0]
            created = _new_match(db, account, "R2", lines=[line],
                                 externals=[("payments", payment.id)], confidence=0.95,
                                 rejected_signatures=rejected_sigs)
            if created is not None:
                r2_matched_payment_ids.add(payment.id)
                stats["r2_payments"] += 1
            else:
                # This exact pairing was rejected — a DIFFERENT pairing
                # (an R3 sweep) may still be right for this line.
                still_open_deposits.append(line)
        else:
            still_open_deposits.append(line)

    # ── R3 deposit sweep: unique subset-sum of unconsumed payments ─────
    sweep_pool = [p for p in payments
                  if p.id not in r2_matched_payment_ids
                  and (p.method or "").strip().lower() not in _PROCESSOR_METHODS]
    swept_payment_ids: set = set()
    r3_matched_line_ids: set = set()
    for line in still_open_deposits:
        window_start = line.txn_date - timedelta(days=R3_WINDOW_BACK_DAYS)
        window_end = line.txn_date + timedelta(days=R3_WINDOW_FWD_DAYS)
        candidates = [p for p in sweep_pool
                      if p.id not in swept_payment_ids
                      and window_start <= p.payment_date <= window_end]
        if not candidates or len(candidates) > R3_MAX_CANDIDATES:
            continue
        target = line.amount_cents
        hits = []
        for k in range(2, min(R3_MAX_SUBSET, len(candidates)) + 1):
            for combo in combinations(candidates, k):
                if sum(_cents(p.amount) for p in combo) == target:
                    hits.append(combo)
                    if len(hits) > 1:
                        break
            if len(hits) > 1:
                break
        if len(hits) == 1:
            combo = hits[0]
            created = _new_match(db, account, "R3", lines=[line],
                                 externals=[("payments", p.id) for p in combo], confidence=0.9,
                                 note=f"deposit sweep of {len(combo)} payments",
                                 rejected_signatures=rejected_sigs)
            if created is not None:
                swept_payment_ids.update(p.id for p in combo)
                r3_matched_line_ids.add(line.id)
                stats["r3_sweeps"] += 1

    # ── R2 debits: Expense exact, else VendorInvoice total (weak) ──────
    expenses = [
        e for e in db.scalars(
            select(Expense).where(
                Expense.deleted_at.is_(None),
                Expense.date >= window_lo,
                Expense.date <= window_hi,
            )
        ).all()
        if ("expenses", e.id) not in used_externals
    ]
    vendor_invoices = []
    if VendorInvoice is not None:
        vendor_invoices = [
            v for v in db.scalars(
                select(VendorInvoice).where(VendorInvoice.status != "void")
            ).all()
            if ("vendor_invoices", v.id) not in used_externals and v.invoice_date
        ]

    # Debit rungs get the SAME bipartite degree-1 discipline as deposits
    # (PR-3 audit: first-come assignment quietly matched an expense two
    # debit lines were competing for, and the untracked vendor-invoice rung
    # could emit the same bill twice — an IntegrityError killing the run).
    def _expense_hits(line):
        return [e for e in expenses
                if _cents(e.amount) == -line.amount_cents
                and _business_days_apart(e.date, line.txn_date) <= R2_BUSINESS_DAYS]

    def _vendor_hits(line):
        return [v for v in vendor_invoices
                if v.total is not None and _cents(v.total) == -line.amount_cents
                and abs((v.invoice_date - line.txn_date).days) <= VENDOR_INVOICE_WINDOW_DAYS]

    expense_to_lines: dict = {}
    vendor_to_lines: dict = {}
    for line in debits:
        for e in _expense_hits(line):
            expense_to_lines.setdefault(e.id, []).append(line.id)
        for v in _vendor_hits(line):
            vendor_to_lines.setdefault(v.id, []).append(line.id)

    for line in debits:
        expense_hits = _expense_hits(line)
        if len(expense_hits) == 1 and len(expense_to_lines[expense_hits[0].id]) == 1:
            created = _new_match(db, account, "R2", lines=[line],
                                 externals=[("expenses", expense_hits[0].id)], confidence=0.9,
                                 rejected_signatures=rejected_sigs)
            if created is not None:
                stats["r2_expenses"] += 1
                continue
        vendor_hits = _vendor_hits(line)
        if len(vendor_hits) == 1 and len(vendor_to_lines[vendor_hits[0].id]) == 1:
            created = _new_match(db, account, "R2", lines=[line],
                                 externals=[("vendor_invoices", vendor_hits[0].id)], confidence=0.6,
                                 note="matched on vendor invoice total — verify it was paid from this account",
                                 rejected_signatures=rejected_sigs)
            if created is not None:
                stats["r2_vendor_invoices"] += 1
                continue
        stats["unmatched"] += 1

    stats["unmatched"] += sum(1 for l in still_open_deposits if l.id not in r3_matched_line_ids)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001 — IntegrityError from a concurrent writer
        db.rollback()
        raise ValueError(
            "a concurrent reconciliation run changed matches while this one "
            "was working — nothing was saved; run Suggest matches again"
        ) from exc
    return stats


# ── lifecycle ──────────────────────────────────────────────────────────


def _sync_children(db: Session, match: BankMatch) -> None:
    for child in db.scalars(select(BankMatchLine).where(BankMatchLine.match_id == match.id)).all():
        child.match_status = match.status
    for child in db.scalars(select(BankMatchExternal).where(BankMatchExternal.match_id == match.id)).all():
        child.match_status = match.status


def set_match_status(db: Session, match: BankMatch, status: str, user_id: str | None = None) -> dict:
    from gdx_dispatch.core.audit import utcnow

    if status not in (MATCH_SUGGESTED, MATCH_CONFIRMED, MATCH_REJECTED):
        raise ValueError(f"invalid status {status!r}")
    match.status = status
    if status == MATCH_CONFIRMED:
        match.confirmed_by = user_id
        match.confirmed_at = utcnow()
    else:
        match.confirmed_by = None
        match.confirmed_at = None
    _sync_children(db, match)
    db.commit()
    return {"id": str(match.id), "status": match.status}


def create_manual_match(
    db: Session, account: BankAccount, line_ids: list[UUID],
    externals: list[tuple[str, UUID]], classification: str | None,
    note: str | None, user_id: str | None,
) -> BankMatch:
    """Office-confirmed manual match. Validates existence + exclusivity and
    creates it CONFIRMED (a human just asserted it). Amounts are NOT
    forced to balance — the office may match a partial reality — but the
    imbalance is computed into the note so it's never silent."""
    from gdx_dispatch.core.audit import utcnow

    if not line_ids:
        raise ValueError("at least one statement line is required")
    if not externals and not classification:
        raise ValueError("provide books records or a classification")

    matched = _matched_line_ids(db)
    lines = []
    for line_id in line_ids:
        line = db.get(BankStatementLine, line_id)
        if line is None or line.bank_account_id != account.id:
            raise ValueError(f"unknown statement line {line_id}")
        if line.id in matched:
            raise ValueError(f"line {line_id} already belongs to a match")
        lines.append(line)

    used = _used_externals(db)
    external_cents = 0
    validated = []
    for source_table, source_id in externals:
        model = {"payments": Payment, "expenses": Expense,
                 "vendor_invoices": VendorInvoice}.get(source_table)
        if model is None:
            raise ValueError(f"unknown source_table {source_table!r}")
        row = db.get(model, source_id)
        if row is None:
            raise ValueError(f"unknown {source_table} record {source_id}")
        if (source_table, source_id) in used:
            raise ValueError(f"{source_table} record {source_id} is already matched")
        amount = getattr(row, "amount", None) or getattr(row, "total", 0)
        external_cents += _cents(amount or 0)
        validated.append((source_table, source_id))

    line_cents = sum(abs(l.amount_cents) for l in lines)
    imbalance = line_cents - external_cents if validated else 0
    full_note = note or ""
    if validated and imbalance != 0:
        full_note = (full_note + f" [imbalance: lines {line_cents}¢ vs records {external_cents}¢]").strip()

    match = _new_match(db, account, "manual", lines=lines, externals=validated,
                       classification=classification, confidence=None, note=full_note or None)
    match.status = MATCH_CONFIRMED
    match.created_by = user_id
    match.confirmed_by = user_id
    match.confirmed_at = utcnow()
    # Stack-audit F1: sessions run autoflush=False, so without this flush
    # _sync_children's SELECT cannot see the pending children — they would
    # commit as 'suggested' under a 'confirmed' parent, the reports would
    # contradict each other about this match, and the void seam (which
    # trusts child match_status) would silently delete a confirmed match.
    db.flush()
    _sync_children(db, match)
    db.commit()
    return match


MANUAL_CANDIDATE_WINDOW_DAYS = 30


def manual_candidates(db: Session, line: BankStatementLine) -> dict:
    """Books records the manual-match dialog offers for one line:
    unconsumed, within ±30 days, NO amount filter — the human judges.
    Deposits offer payments; debits/checks offer expenses + vendor bills."""
    used = _used_externals(db)
    lo = line.txn_date - timedelta(days=MANUAL_CANDIDATE_WINDOW_DAYS)
    hi = line.txn_date + timedelta(days=MANUAL_CANDIDATE_WINDOW_DAYS)

    out = {"payments": [], "expenses": [], "vendor_invoices": []}
    if line.section == SECTION_DEPOSIT:
        payments = [p for p in db.scalars(
            select(Payment).where(
                Payment.voided_at.is_(None),
                Payment.payment_date >= lo, Payment.payment_date <= hi,
            ).order_by(Payment.payment_date)
        ).all() if ("payments", p.id) not in used]
        invoices = {i.id: i for i in db.scalars(
            select(Invoice).where(Invoice.id.in_({p.invoice_id for p in payments}))
        ).all()} if payments else {}
        customers = {c.id: c for c in db.scalars(
            select(Customer).where(Customer.id.in_({i.customer_id for i in invoices.values()}))
        ).all()} if invoices else {}
        for p in payments:
            invoice = invoices.get(p.invoice_id)
            customer = customers.get(invoice.customer_id) if invoice else None
            out["payments"].append({
                "id": str(p.id), "payment_date": p.payment_date.isoformat(),
                "amount_cents": _cents(p.amount), "method": p.method,
                "reference": p.reference,
                "invoice_number": invoice.invoice_number if invoice else None,
                "customer_name": getattr(customer, "name", None),
            })
    else:
        out["expenses"] = [
            {"id": str(e.id), "date": e.date.isoformat(), "amount_cents": _cents(e.amount),
             "vendor": e.vendor, "category": e.category}
            for e in db.scalars(
                select(Expense).where(
                    Expense.deleted_at.is_(None),
                    Expense.date >= lo, Expense.date <= hi,
                ).order_by(Expense.date)
            ).all() if ("expenses", e.id) not in used
        ]
        if VendorInvoice is not None:
            out["vendor_invoices"] = [
                {"id": str(v.id), "invoice_date": v.invoice_date.isoformat(),
                 "total_cents": _cents(v.total or 0), "vendor_key": v.vendor_key,
                 "invoice_number": v.invoice_number, "status": v.status}
                for v in db.scalars(select(VendorInvoice).where(VendorInvoice.status != "void")).all()
                if ("vendor_invoices", v.id) not in used and v.invoice_date
                and lo <= v.invoice_date <= hi
            ]
    return out


# ── the four verification reports ──────────────────────────────────────


def build_reports(db: Session, account: BankAccount, date_from: date, date_to: date) -> dict:
    """The product: what the bank saw that the books didn't, and what the
    books claim that the bank never saw. CONFIRMED matches settle a line
    or a payment; suggestions are surfaced but settle nothing."""
    lines = db.scalars(
        select(BankStatementLine).where(
            BankStatementLine.bank_account_id == account.id,
            BankStatementLine.txn_date >= date_from,
            BankStatementLine.txn_date <= date_to,
        ).order_by(BankStatementLine.txn_date)
    ).all()

    confirmed_line_ids = set(db.scalars(
        select(BankMatchLine.line_id).where(BankMatchLine.match_status == MATCH_CONFIRMED)
    ).all())
    suggested_line_ids = set(db.scalars(
        select(BankMatchLine.line_id).where(BankMatchLine.match_status == MATCH_SUGGESTED)
    ).all())

    def line_out(line):
        return {
            "id": str(line.id),
            "txn_date": line.txn_date.isoformat(),
            "amount_cents": line.amount_cents,
            "description": _anchor(line),
            "section": line.section,
            "check_number": line.check_number,
            "has_suggestion": line.id in suggested_line_ids,
        }

    unmatched_deposits = [line_out(l) for l in lines
                          if l.section == SECTION_DEPOSIT and l.id not in confirmed_line_ids]
    unmatched_debits = [line_out(l) for l in lines
                        if l.section in (SECTION_DEBIT, SECTION_CHECK)
                        and l.id not in confirmed_line_ids]

    # Books side: payments in range never confirmed against ANY line, on
    # ANY account — settlement is deliberately bank-wide, not per-account:
    # a payment deposited into the savings account is verified reality, and
    # scoping settlement to the selected account would falsely re-flag it
    # here (PR-3 audit raised this; global is the intended semantics and
    # the payload says so via ``payments_scope``).
    confirmed_payment_ids = {
        source_id for source_table, source_id in db.execute(
            select(BankMatchExternal.source_table, BankMatchExternal.source_id)
            .where(BankMatchExternal.match_status == MATCH_CONFIRMED)
        ).all() if source_table == "payments"
    }
    payments = db.scalars(
        select(Payment).where(
            Payment.voided_at.is_(None),
            Payment.payment_date >= date_from,
            Payment.payment_date <= date_to,
        ).order_by(Payment.payment_date)
    ).all()
    invoice_ids = {p.invoice_id for p in payments}
    invoices = {i.id: i for i in db.scalars(
        select(Invoice).where(Invoice.id.in_(invoice_ids))).all()} if invoice_ids else {}
    customer_ids = {i.customer_id for i in invoices.values()}
    customers = {c.id: c for c in db.scalars(
        select(Customer).where(Customer.id.in_(customer_ids))).all()} if customer_ids else {}

    def payment_out(p):
        invoice = invoices.get(p.invoice_id)
        customer = customers.get(invoice.customer_id) if invoice else None
        return {
            "id": str(p.id),
            "payment_date": p.payment_date.isoformat(),
            "amount_cents": _cents(p.amount),
            "method": p.method,
            "reference": p.reference,
            "invoice_number": invoice.invoice_number if invoice else None,
            "customer_name": getattr(customer, "name", None),
        }

    unmatched_payments = [payment_out(p) for p in payments if p.id not in confirmed_payment_ids]

    # Date drift: confirmed line↔payment pairs where the recorded date
    # differs from the bank date — verifies the payment-date backfill.
    drift = []
    confirmed_matches = db.scalars(
        select(BankMatch).where(
            BankMatch.bank_account_id == account.id,
            BankMatch.status == MATCH_CONFIRMED,
        )
    ).all()
    for match in confirmed_matches:
        match_lines = [db.get(BankStatementLine, ml.line_id) for ml in db.scalars(
            select(BankMatchLine).where(BankMatchLine.match_id == match.id)).all()]
        match_lines = [l for l in match_lines if l and date_from <= l.txn_date <= date_to]
        if not match_lines:
            continue
        bank_date = min(l.txn_date for l in match_lines)
        for ext in db.scalars(
                select(BankMatchExternal).where(BankMatchExternal.match_id == match.id)).all():
            if ext.source_table != "payments":
                continue
            payment = db.get(Payment, ext.source_id)
            if payment is None:
                continue
            days = (payment.payment_date - bank_date).days
            if days != 0:
                drift.append({
                    "payment_id": str(payment.id),
                    "payment_date": payment.payment_date.isoformat(),
                    "bank_date": bank_date.isoformat(),
                    "drift_days": days,
                    "amount_cents": _cents(payment.amount),
                    "match_id": str(match.id),
                })

    return {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "payments_scope": "bank_wide",
        "unmatched_deposits": unmatched_deposits,
        "unmatched_deposits_total_cents": sum(l["amount_cents"] for l in unmatched_deposits),
        "unmatched_payments": unmatched_payments,
        "unmatched_payments_total_cents": sum(p["amount_cents"] for p in unmatched_payments),
        "date_drift": drift,
        "unmatched_debits": unmatched_debits,
        "unmatched_debits_total_cents": sum(-l["amount_cents"] for l in unmatched_debits),
    }
