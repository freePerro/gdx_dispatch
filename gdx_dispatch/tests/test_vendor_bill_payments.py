"""Vendor bill payments + bank-match confirm effects (books-convergence T1).

The audited invariants under test:
- status is write-through DERIVED from payment records (void sticky; the
  migration backfill covers historical Mark-paid bills, tested via the same
  NOT-EXISTS semantics the SQL uses);
- the open-balance cap refuses double-booking across evidence streams;
- confirming a match whose only external is a single vendor bill records a
  payment IN the confirm transaction; re-confirm is a no-op; unconfirm voids
  exactly what confirm created (never twice); manual matches born confirmed
  run the same effects;
- multi-external / over-balance matches stay metadata-only with a loud note;
- match-created payments demand the unconfirm ceremony;
- broken_matches surfaces a voided match-created payment;
- create-expense-from-bank-line mints a canonical-category expense inside a
  confirmed match, and unconfirm deletes it only while unmodified;
- vendor-bill line confirms post P5 when the flag is on, skip pre-cutover.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest

from gdx_dispatch.models.tenant_models import Expense
from gdx_dispatch.modules.bank_feeds import statement_matching, statement_service
from gdx_dispatch.modules.bank_feeds.statement_models import (
    MATCH_CONFIRMED,
    MATCH_SUGGESTED,
    BankAccount,
    BankMatch,
    BankStatementLine,
)
from gdx_dispatch.modules.bank_feeds.statement_parsers import community_bank
from gdx_dispatch.modules.vendor_invoices.models import (
    STATUS_OPEN,
    STATUS_PAID,
    VendorBillPayment,
    VendorInvoice,
)
from gdx_dispatch.modules.vendor_invoices.payments import (
    PaymentError,
    open_balance,
    paid_total,
    record_payment,
    recompute_status,
    void_payment,
)
from gdx_dispatch.tests.test_bank_statement_import import checking_text

COMPANY = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def world(tenant_db, tmp_path, monkeypatch):
    """Statement evidence (checking fixture: debits 6/03 $100 / 6/08 $50 /
    6/15 $25, checks 1062 $75 + 1083 $40) + empty books to fill per-test."""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(community_bank, "extract_pdf_text", lambda b: b.decode("utf-8"))
    monkeypatch.setattr(community_bank, "extract_caption_images", lambda b: [])
    result = statement_service.import_statement(tenant_db, checking_text().encode(), "june.pdf")
    assert result["status"] == "imported"
    account = tenant_db.query(BankAccount).one()
    return tenant_db, account


def make_bill(db, total, invoice_date=date(2026, 6, 1), number=None):
    bill = VendorInvoice(
        vendor_name_raw="Sample Supplier",
        vendor_key=f"sample-{uuid.uuid4().hex[:8]}",
        invoice_number=number or f"INV{uuid.uuid4().hex[:8]}",
        invoice_date=invoice_date,
        total=Decimal(str(total)),
        subtotal=Decimal(str(total)),
    )
    db.add(bill)
    db.commit()
    return bill


def line_by_amount(db, cents):
    return db.query(BankStatementLine).filter(BankStatementLine.amount_cents == cents).first()


def run_matcher(db, account):
    return statement_matching.suggest_matches(db, account, date(2026, 6, 1), date(2026, 6, 30))


# ── derivation core ────────────────────────────────────────────────────


def test_record_partial_then_full_derives_status(tenant_db):
    bill = make_bill(tenant_db, "100.00")
    record_payment(tenant_db, bill, amount=Decimal("40.00"), paid_date=date(2026, 6, 5),
                   source="manual")
    tenant_db.commit()
    assert bill.status == STATUS_OPEN
    assert paid_total(tenant_db, bill) == Decimal("40.00")
    record_payment(tenant_db, bill, amount=Decimal("60.00"), paid_date=date(2026, 6, 9),
                   source="manual")
    tenant_db.commit()
    assert bill.status == STATUS_PAID
    assert open_balance(tenant_db, bill) == Decimal("0.00")


def test_overpay_refused(tenant_db):
    bill = make_bill(tenant_db, "50.00")
    with pytest.raises(PaymentError, match="open balance"):
        record_payment(tenant_db, bill, amount=Decimal("50.01"), paid_date=None, source="manual")
    record_payment(tenant_db, bill, amount=Decimal("50.00"), paid_date=None, source="manual")
    # A second stream witnessing the same settlement must not double-book.
    with pytest.raises(PaymentError, match="open balance"):
        record_payment(tenant_db, bill, amount=Decimal("0.01"), paid_date=None, source="manual")


def test_void_recomputes_and_is_idempotent(tenant_db):
    bill = make_bill(tenant_db, "75.00")
    payment = record_payment(tenant_db, bill, amount=Decimal("75.00"), paid_date=None,
                             source="manual")
    tenant_db.commit()
    assert bill.status == STATUS_PAID
    void_payment(tenant_db, payment, voided_by="tester")
    tenant_db.commit()
    assert bill.status == STATUS_OPEN
    stamp = payment.voided_at
    void_payment(tenant_db, payment, voided_by="someone-else")
    assert payment.voided_at == stamp  # no double-void restamp
    assert payment.voided_by == "tester"


def test_void_bill_refuses_payments_and_status_is_sticky(tenant_db):
    bill = make_bill(tenant_db, "75.00")
    bill.status = "void"
    tenant_db.commit()
    with pytest.raises(PaymentError, match="void"):
        record_payment(tenant_db, bill, amount=Decimal("10.00"), paid_date=None, source="manual")
    assert recompute_status(tenant_db, bill) == "void"


def test_backfill_semantics_paid_bill_never_reverts(tenant_db):
    """The migration mints a full-total synthetic payment for historically
    Mark-paid bills; with it, derivation keeps them paid."""
    bill = make_bill(tenant_db, "200.00")
    bill.status = STATUS_PAID  # historical state, pre-derivation
    tenant_db.commit()
    # migration equivalent
    tenant_db.add(VendorBillPayment(
        vendor_invoice_id=bill.id, amount=bill.total, paid_date=None,
        source="manual", reference="migrated: pre-existing paid status (date unknown)",
    ))
    tenant_db.commit()
    assert recompute_status(tenant_db, bill) == STATUS_PAID


# ── PATCH single-writer rule ───────────────────────────────────────────


def test_patch_paid_rejected_payments_endpoint_records(tenant_db):
    from fastapi import HTTPException

    from gdx_dispatch.routers.vendor_invoices import (
        InvoicePatch,
        PaymentCreateIn,
        patch_invoice,
        record_bill_payment,
    )

    class _Req:
        class _State:
            tenant = {"id": COMPANY}
        state = _State()

    user = {"sub": "tester"}
    bill = make_bill(tenant_db, "30.00")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(patch_invoice(bill.id, InvoicePatch(status="paid"), _Req(), user, tenant_db))
    assert exc.value.status_code == 409
    out = asyncio.run(record_bill_payment(
        bill.id, PaymentCreateIn(amount=Decimal("30.00"), paid_date=date(2026, 6, 10)),
        _Req(), user, tenant_db))
    assert out.status == STATUS_PAID
    assert out.paid_total == 30.0
    assert out.open_balance == 0.0


def test_payables_expose_open_balance_for_partials(tenant_db):
    """The A/P exposure surface must sum remaining balances, not bill
    totals — a partially-paid bill stays 'open' with a smaller true debt."""
    from gdx_dispatch.routers.vendor_invoices import list_payables

    bill = make_bill(tenant_db, "100.00", number="PARTIAL-1")
    record_payment(tenant_db, bill, amount=Decimal("40.00"), paid_date=None, source="manual")
    tenant_db.commit()
    rows = asyncio.run(list_payables({"sub": "t"}, tenant_db))
    row = next(r for r in rows if r.invoice_number == "PARTIAL-1")
    assert row.paid_total == 40.0
    assert row.open_balance == 60.0
    assert row.is_partial is True


# ── match confirm effects ──────────────────────────────────────────────


def confirmed_vendor_match(db, account, bill):
    """Run the matcher and confirm the R2 vendor-rung suggestion for the bill."""
    from gdx_dispatch.modules.bank_feeds.statement_models import BankMatchExternal

    run_matcher(db, account)
    match = None
    for m in db.query(BankMatch).filter(BankMatch.rule == "R2").all():
        ext = db.query(BankMatchExternal).filter(
            BankMatchExternal.match_id == m.id,
            BankMatchExternal.source_table == "vendor_invoices",
            BankMatchExternal.source_id == bill.id,
        ).first()
        if ext is not None:
            match = m
            break
    assert match is not None, "matcher produced no vendor-bill suggestion"
    statement_matching.set_match_status(db, match, MATCH_CONFIRMED, "tester")
    return match


def test_confirm_records_payment_and_unconfirm_voids(world):
    db, account = world
    bill = make_bill(db, "100.00", invoice_date=date(2026, 6, 1))
    match = confirmed_vendor_match(db, account, bill)

    payments = db.query(VendorBillPayment).filter(VendorBillPayment.match_id == match.id).all()
    assert len(payments) == 1
    payment = payments[0]
    assert payment.amount == Decimal("100.00")
    assert payment.source == "statement_match"
    assert payment.paid_date == date(2026, 6, 3)  # the BANK date, not invoice date
    db.refresh(bill)
    assert bill.status == STATUS_PAID

    # Re-confirm at service level: previous == confirmed → effects skipped.
    statement_matching.set_match_status(db, match, MATCH_CONFIRMED, "tester")
    assert db.query(VendorBillPayment).filter(
        VendorBillPayment.match_id == match.id, VendorBillPayment.voided_at.is_(None)
    ).count() == 1

    # Unconfirm voids it, exactly once — and DETACHES it (diff-audit
    # BLOCKER 1: a voided payment holding match_id pins the now-suggested
    # match against the suggest-wipe's hard DELETE → permanent FK crash).
    statement_matching.set_match_status(db, match, MATCH_SUGGESTED, "tester")
    db.refresh(payment)
    assert payment.voided_at is not None
    assert payment.match_id is None
    assert "unconfirmed from match" in (payment.reference or "")
    db.refresh(bill)
    assert bill.status == STATUS_OPEN

    # The seam the FK crash lived in: a suggest re-run over the same window
    # must wipe/rebuild the suggested match without tripping the FK.
    stats = run_matcher(db, account)
    assert stats["r2_vendor_invoices"] >= 1  # bill is open again, re-suggested


def test_manual_match_runs_effects(world):
    db, account = world
    bill = make_bill(db, "75.00", invoice_date=date(2026, 6, 10))
    line = line_by_amount(db, -7500)  # check 1062 $75
    assert line is not None
    match = statement_matching.create_manual_match(
        db, account, [line.id], [("vendor_invoices", bill.id)], None, None, "tester")
    assert match.status == MATCH_CONFIRMED
    live = db.query(VendorBillPayment).filter(
        VendorBillPayment.match_id == match.id, VendorBillPayment.voided_at.is_(None)).all()
    assert len(live) == 1
    db.refresh(bill)
    assert bill.status == STATUS_PAID


def test_multi_external_match_stays_metadata_only(world):
    db, account = world
    bill = make_bill(db, "60.00", invoice_date=date(2026, 6, 10))
    expense = Expense(vendor="X", amount=Decimal("15.00"), date=date(2026, 6, 12),
                      category="Fuel", company_id=COMPANY)
    db.add(expense)
    db.commit()
    line = line_by_amount(db, -7500)
    match = statement_matching.create_manual_match(
        db, account, [line.id],
        [("vendor_invoices", bill.id), ("expenses", expense.id)], None, None, "tester")
    assert db.query(VendorBillPayment).count() == 0
    assert "no payment auto-recorded" in (match.note or "")
    db.refresh(bill)
    assert bill.status == STATUS_OPEN


def test_overbalance_match_stays_metadata_only(world):
    db, account = world
    bill = make_bill(db, "40.00", invoice_date=date(2026, 6, 10))
    line = line_by_amount(db, -7500)  # $75 against a $40 bill
    match = statement_matching.create_manual_match(
        db, account, [line.id], [("vendor_invoices", bill.id)], None, None, "tester")
    assert db.query(VendorBillPayment).filter(VendorBillPayment.voided_at.is_(None)).count() == 0
    assert "no payment auto-recorded" in (match.note or "")


def test_match_created_payment_demands_unconfirm_ceremony(world):
    db, account = world
    bill = make_bill(db, "100.00", invoice_date=date(2026, 6, 1))
    match = confirmed_vendor_match(db, account, bill)
    payment = db.query(VendorBillPayment).filter(VendorBillPayment.match_id == match.id).one()
    with pytest.raises(PaymentError, match="unconfirm"):
        void_payment(db, payment, voided_by="tester")
    void_payment(db, payment, voided_by="tester", via_unconfirm=True)
    assert payment.voided_at is not None


def test_broken_match_surfaces_voided_created_payment(world):
    db, account = world
    bill = make_bill(db, "100.00", invoice_date=date(2026, 6, 1))
    match = confirmed_vendor_match(db, account, bill)
    payment = db.query(VendorBillPayment).filter(VendorBillPayment.match_id == match.id).one()
    void_payment(db, payment, voided_by="tester", via_unconfirm=True)
    db.commit()
    reports = statement_matching.build_reports(db, account, date(2026, 6, 1), date(2026, 6, 30))
    reasons = [d["reason"] for b in reports["broken_matches"] for d in b["dead_externals"]]
    assert "recorded bill payment voided" in reasons


def test_paid_bills_skip_auto_rung_but_stay_manual_candidates(world):
    """Auto suggestions skip paid bills (noise); the manual dialog still
    OFFERS them (diff-audit MUST-FIX 3) so the office can corroborate the
    bank debit for an already-recorded-paid bill — the open-balance cap
    makes that confirm metadata-only instead of a double-record."""
    db, account = world
    bill = make_bill(db, "100.00", invoice_date=date(2026, 6, 1))
    record_payment(db, bill, amount=Decimal("100.00"), paid_date=None, source="manual")
    db.commit()
    assert bill.status == STATUS_PAID
    stats = run_matcher(db, account)
    assert stats["r2_vendor_invoices"] == 0
    line = line_by_amount(db, -10000)
    candidates = statement_matching.manual_candidates(db, line)
    offered = [c for c in candidates["vendor_invoices"] if c["id"] == str(bill.id)]
    assert offered and offered[0]["status"] == STATUS_PAID
    # Corroborating match: confirmed, but metadata-only (cap refuses).
    match = statement_matching.create_manual_match(
        db, account, [line.id], [("vendor_invoices", bill.id)], None, None, "tester")
    assert match.status == MATCH_CONFIRMED
    assert db.query(VendorBillPayment).filter(
        VendorBillPayment.match_id == match.id, VendorBillPayment.voided_at.is_(None)
    ).count() == 0
    assert "no payment auto-recorded" in (match.note or "")


def test_deposit_matched_to_bill_records_nothing(world):
    """Money direction (diff-audit MUST-FIX 2): a deposit against a bill is
    a vendor refund/credit, never a payment."""
    db, account = world
    bill = make_bill(db, "500.00", invoice_date=date(2026, 6, 1))
    deposit = line_by_amount(db, 50000)  # deposit 6/02 $500
    match = statement_matching.create_manual_match(
        db, account, [deposit.id], [("vendor_invoices", bill.id)], None, None, "tester")
    assert db.query(VendorBillPayment).count() == 0
    assert "refund, not a payment" in (match.note or "")
    db.refresh(bill)
    assert bill.status == STATUS_OPEN


def test_confirm_refuses_dead_externals(world):
    """Diff-audit SHOULD-FIX 4: a reverted create-expense match keeps its
    (now soft-deleted) expense external — re-confirming it would settle the
    line with dead money."""
    db, account = world
    line = line_by_amount(db, -2500)
    out = create_expense_via_endpoint(db, account, line)
    match = db.get(BankMatch, uuid.UUID(out["id"]))
    statement_matching.set_match_status(db, match, MATCH_SUGGESTED, "tester")  # deletes expense
    with pytest.raises(ValueError, match="expense deleted"):
        statement_matching.set_match_status(db, match, MATCH_CONFIRMED, "tester")


def test_patch_void_refused_with_live_payments(tenant_db):
    from fastapi import HTTPException

    from gdx_dispatch.routers.vendor_invoices import InvoicePatch, patch_invoice

    class _R:
        class _State:
            tenant = {"id": COMPANY}
        state = _State()

    bill = make_bill(tenant_db, "80.00")
    record_payment(tenant_db, bill, amount=Decimal("30.00"), paid_date=None, source="manual")
    tenant_db.commit()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(patch_invoice(bill.id, InvoicePatch(status="void"), _R(), {"sub": "t"}, tenant_db))
    assert exc.value.status_code == 409


# ── create-expense-from-bank-line ──────────────────────────────────────


class _Req:
    class _State:
        tenant = {"id": COMPANY}
    state = _State()


def create_expense_via_endpoint(db, account, line, **overrides):
    from gdx_dispatch.modules.bank_feeds.router import (
        CreateExpenseFromLineIn,
        create_expense_from_line,
    )

    body = CreateExpenseFromLineIn(
        account_id=str(account.id),
        vendor=overrides.pop("vendor", "Fuel Stop"),
        category=overrides.pop("category", "fuel"),  # legacy vocab → canonical
        **overrides,
    )
    return create_expense_from_line(str(line.id), body, _Req(), {"sub": "tester"}, None, db)


def test_create_expense_from_line_and_unconfirm_deletes(world):
    db, account = world
    line = line_by_amount(db, -2500)  # debit 6/15 $25
    out = create_expense_via_endpoint(db, account, line)
    expense = db.get(Expense, uuid.UUID(out["expense_id"]))
    assert expense.category == "Fuel"  # canonicalized
    assert expense.amount == Decimal("25.00")
    assert expense.date == line.txn_date
    assert expense.source == "bank_match"

    match = db.get(BankMatch, uuid.UUID(out["id"]))
    assert match.status == MATCH_CONFIRMED
    assert match.created_expense_id == expense.id

    statement_matching.set_match_status(db, match, MATCH_SUGGESTED, "tester")
    db.refresh(expense)
    assert expense.deleted_at is not None  # unmodified → soft-deleted
    assert match.created_expense_id is None


def test_create_expense_from_line_modified_expense_detaches(world):
    db, account = world
    line = line_by_amount(db, -2500)
    out = create_expense_via_endpoint(db, account, line)
    expense = db.get(Expense, uuid.UUID(out["expense_id"]))
    expense.status = "approved"  # office did real work on it
    db.commit()
    match = db.get(BankMatch, uuid.UUID(out["id"]))
    statement_matching.set_match_status(db, match, MATCH_SUGGESTED, "tester")
    db.refresh(expense)
    assert expense.deleted_at is None  # survives
    assert match.created_expense_id is None
    assert "detached" in (match.note or "")


def test_create_expense_bad_category_422(world):
    from fastapi import HTTPException

    db, account = world
    line = line_by_amount(db, -2500)
    with pytest.raises(HTTPException) as exc:
        create_expense_via_endpoint(db, account, line, category="not-a-category")
    assert exc.value.status_code == 422


def test_create_expense_refuses_deposits(world):
    from fastapi import HTTPException

    db, account = world
    deposit = line_by_amount(db, 50000)  # deposit 6/02 $500
    with pytest.raises(HTTPException) as exc:
        create_expense_via_endpoint(db, account, deposit)
    assert exc.value.status_code == 422


def test_create_expense_from_line_posts_and_unconfirm_reverses_gl(world):
    """Flag-ON path for the Reconcile create-expense flow: creation posts
    P5; unconfirm (which soft-deletes the unmodified expense) reverses it —
    the expense's journal footprint nets to zero."""
    from sqlalchemy import select as _select

    from gdx_dispatch.modules.ledger.models import GlJournalEntry, GlJournalLine
    from gdx_dispatch.modules.ledger.service import ensure_gl_seed

    db, account = world
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = True
    db.commit()

    line = line_by_amount(db, -2500)
    out = create_expense_via_endpoint(db, account, line)
    expense_id = out["expense_id"]

    def entries():
        return db.scalars(_select(GlJournalEntry).where(
            GlJournalEntry.source_type == "expense",
            GlJournalEntry.source_id == expense_id,
        )).all()

    assert entries(), "create-expense-from-line must post P5 with the flag on"

    match = db.get(BankMatch, uuid.UUID(out["id"]))
    statement_matching.set_match_status(db, match, MATCH_SUGGESTED, "tester")

    entry_ids = [e.id for e in entries()]
    net = sum(
        int(l.amount_cents)
        for l in db.scalars(_select(GlJournalLine).where(
            GlJournalLine.entry_id.in_(entry_ids))).all()
    )
    assert net == 0, "unconfirm must leave a zero net journal footprint"


# ── GL symmetry on vendor-bill confirm ─────────────────────────────────


def test_vendor_bill_confirm_posts_p5_when_flag_on(tenant_db):
    from gdx_dispatch.modules.ledger.models import GlJournalEntry
    from gdx_dispatch.modules.ledger.service import ensure_gl_seed
    from gdx_dispatch.modules.vendor_invoices.confirm import confirm_line
    from gdx_dispatch.modules.vendor_invoices.models import VendorInvoiceLine

    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.ledger_posting_enabled = True
    tenant_db.commit()

    bill = make_bill(tenant_db, "50.00", invoice_date=date(2026, 6, 20))
    line = VendorInvoiceLine(
        vendor_invoice_id=bill.id, line_no=0, kind="item",
        description="widget", quantity=Decimal("1"),
        unit_cost=Decimal("50"), line_total=Decimal("50.00"),
    )
    tenant_db.add(line)
    tenant_db.commit()

    confirm_line(tenant_db, bill, line, disposition="overhead",
                 company_id=COMPANY, actor_id="tester")
    tenant_db.commit()

    entry = tenant_db.query(GlJournalEntry).filter(
        GlJournalEntry.source_type == "expense",
        GlJournalEntry.source_id == str(line.expense_id),
    ).first()
    assert entry is not None, "vendor-bill expense must post P5 like a manual one"


def test_vendor_bill_confirm_skips_pre_cutover_era(tenant_db):
    from gdx_dispatch.modules.ledger.models import GlJournalEntry
    from gdx_dispatch.modules.ledger.service import ensure_gl_seed
    from gdx_dispatch.modules.vendor_invoices.confirm import confirm_line
    from gdx_dispatch.modules.vendor_invoices.models import VendorInvoiceLine

    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.ledger_posting_enabled = True
    settings.cutover_month = date(2026, 6, 1)
    tenant_db.commit()

    bill = make_bill(tenant_db, "50.00", invoice_date=date(2026, 5, 15))  # pre-cutover
    line = VendorInvoiceLine(
        vendor_invoice_id=bill.id, line_no=0, kind="item",
        description="widget", quantity=Decimal("1"),
        unit_cost=Decimal("50"), line_total=Decimal("50.00"),
    )
    tenant_db.add(line)
    tenant_db.commit()

    confirm_line(tenant_db, bill, line, disposition="overhead",
                 company_id=COMPANY, actor_id="tester")
    tenant_db.commit()

    assert tenant_db.query(GlJournalEntry).filter(
        GlJournalEntry.source_type == "expense",
        GlJournalEntry.source_id == str(line.expense_id),
    ).first() is None, "pre-cutover era expenses belong to the QBO-era books"


# ── QB schedule health ─────────────────────────────────────────────────


def test_record_scheduled_run_tracks_success_and_reads(tenant_db):
    from gdx_dispatch.modules.quickbooks.banking import (
        get_or_create_schedule,
        record_scheduled_run,
        schedule_dict,
    )

    record_scheduled_run(tenant_db, status="error", error="boom", reads=7)
    s = get_or_create_schedule(tenant_db)
    assert s.last_success_at is None
    assert s.last_run_reads == 7
    record_scheduled_run(tenant_db, status="ok", reads=12)
    s = get_or_create_schedule(tenant_db)
    assert s.last_success_at is not None
    out = schedule_dict(s)
    assert out["last_run_reads"] == 12
    assert out["stale"] is False  # manual frequency never stales


def test_schedule_stale_when_success_old(tenant_db):
    from datetime import datetime, timedelta, timezone

    from gdx_dispatch.modules.quickbooks.banking import (
        FREQ_DAILY,
        get_or_create_schedule,
        schedule_dict,
    )

    s = get_or_create_schedule(tenant_db)
    s.frequency = FREQ_DAILY
    s.last_success_at = datetime.now(timezone.utc) - timedelta(days=3)
    tenant_db.commit()
    assert schedule_dict(s)["stale"] is True
