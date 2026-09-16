"""Customer statements.

Every fixture here is a shape prod actually holds — QuickBooks-era invoices
short of or over their payment rows, one QuickBooks payment recorded twice,
payments dated before their invoice — not the cause the author expected.
Four adversarial audits broke earlier versions of this arithmetic on real
data; these tests are what keeps each break from coming back.

Ledger-on cases drive the real payment, credit and refund routes. The rest
seed rows directly, the way the QuickBooks importer left them.
"""
from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import (
    AppSettings,
    Customer,
    CustomerContact,
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Job,
    OutboundEmail,
    Payment,
)
from gdx_dispatch.services.customer_statements import (
    STATEMENT_EARLIEST,
    StatementRange,
    StatementRangeError,
    build_statement,
    completed_years,
    render_statement_pdf,
    resolve_range,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "office-user"}
TODAY = date(2026, 9, 15)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    return tenant_db


def _customer(db, email="owner@example.com") -> Customer:
    c = Customer(name="Test Builder", email=email, company_id=COMPANY)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _invoice(
    db,
    customer,
    total,
    *,
    day,
    balance=None,
    status="sent",
    due=None,
    paid_at=None,
    job=None,
    billing_type="standard",
) -> Invoice:
    inv = Invoice(
        id=uuid4(),
        customer_id=customer.id,
        job_id=job.id if job else None,
        invoice_number=f"INV-{uuid4().hex[:6].upper()}",
        billing_type=billing_type,
        status=status,
        subtotal=Decimal(str(total)),
        tax_amount=Decimal("0"),
        total=Decimal(str(total)),
        balance_due=Decimal(str(total if balance is None else balance)),
        invoice_date=day,
        due_date=due,
        paid_at=paid_at,
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    # A line matching the total: routes that recalculate rebuild totals from lines.
    db.add(InvoiceLine(
        invoice_id=inv.id, description="Work", quantity=1, unit_price=Decimal(str(total)),
        line_total=Decimal(str(total)), taxable=False, company_id=COMPANY,
    ))
    db.commit()
    db.refresh(inv)
    return inv


def _payment(db, inv, amount, day, *, reference=None, voided=False) -> Payment:
    p = Payment(
        invoice_id=inv.id,
        amount=Decimal(str(amount)),
        method="check",
        payment_date=day,
        reference=reference,
        company_id=COMPANY,
        voided_at=datetime.now(UTC) if voided else None,
    )
    db.add(p)
    db.commit()
    return p


def _statement(db, customer, start, end=TODAY, today=TODAY):
    return build_statement(db, customer, StatementRange(start=start, end=end, preset="custom"), today=today)


def _warned(s) -> set[str]:
    return {w["invoice_number"] for w in s["warnings"] if w["kind"] == "records_dont_add_up"}


def _assert_identity(s):
    """The one equality that must hold for any range ending today."""
    if s["ends_today"]:
        assert s["ending_balance"] == s["total_unpaid"]
    assert sum(s["aging"].values()) == s["total_unpaid"]
    assert sum((r["balance"] for r in s["open_invoices"]), Decimal("0")) == s["total_unpaid"]


# ---------------------------------------------------------------------------
# Ranges (nothing before 2026-01-01)
# ---------------------------------------------------------------------------

def test_day_presets_start_no_earlier_than_the_floor_and_end_today():
    early = date(2026, 2, 15)
    assert resolve_range(early, preset="last_90") == StatementRange(STATEMENT_EARLIEST, early, "last_90")
    assert resolve_range(TODAY, preset="last_30").start == TODAY - timedelta(days=29)
    assert resolve_range(TODAY).preset == "last_90"
    assert resolve_range(TODAY, preset="ytd") == StatementRange(date(2026, 1, 1), TODAY, "ytd")


def test_year_presets_are_completed_years_from_the_floor():
    with pytest.raises(StatementRangeError):
        resolve_range(TODAY, preset="year_2026")  # not finished yet
    with pytest.raises(StatementRangeError):
        resolve_range(date(2027, 3, 1), preset="year_2025")  # before the floor
    assert resolve_range(date(2027, 3, 1), preset="year_2026") == StatementRange(
        date(2026, 1, 1), date(2026, 12, 31), "year_2026"
    )
    assert completed_years(TODAY, [date(2026, 5, 1)]) == []
    assert completed_years(date(2027, 1, 2), [date(2025, 5, 1), date(2026, 5, 1)]) == [2026]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2025, 12, 31), TODAY),  # before the floor
        (date(2026, 3, 1), TODAY + timedelta(days=1)),  # after today
        (date(2026, 3, 1), date(2026, 2, 1)),  # backwards
        (date(2026, 3, 1), None),  # half a range
    ],
)
def test_custom_ranges_that_break_a_rule_are_refused(start, end):
    with pytest.raises(StatementRangeError):
        resolve_range(TODAY, start=start, end=end)


def test_unknown_preset_is_refused():
    with pytest.raises(StatementRangeError):
        resolve_range(TODAY, preset="last_7")


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------

def test_previous_balance_period_activity_and_ending(db):
    c = _customer(db)
    job = Job(
        customer_id=c.id, title="Two 16x8 doors, lot 14", lifecycle_stage="completed",
        dispatch_status="unassigned", billing_status="unbilled", company_id=COMPANY,
    )
    db.add(job)
    db.commit()
    old = _invoice(db, c, 500, day=date(2026, 3, 1), balance=0, status="paid")
    _payment(db, old, 500, date(2026, 3, 20))
    owed = _invoice(db, c, 800, day=date(2026, 5, 1), balance=300, job=job, due=date(2026, 5, 31))
    _payment(db, owed, 200, date(2026, 5, 10))
    _payment(db, owed, 300, date(2026, 7, 1))
    jobless = _invoice(db, c, 150, day=date(2026, 8, 1), due=date(2026, 8, 1))

    s = _statement(db, c, date(2026, 6, 1))
    assert s["previous_balance"] == Decimal("600.00")  # 1300 invoiced − 700 paid before June
    assert s["invoiced"] == Decimal("150.00")
    assert s["payments_and_credits_total"] == Decimal("300.00")
    assert s["ending_balance"] == Decimal("450.00")
    assert s["total_unpaid"] == Decimal("450.00")
    assert [r["invoice_number"] for r in s["invoices"]] == [jobless.invoice_number]
    open_by_number = {r["invoice_number"]: r for r in s["open_invoices"]}
    assert open_by_number[owed.invoice_number]["job_title"] == "Two 16x8 doors, lot 14"
    assert open_by_number[jobless.invoice_number]["job_title"] == ""
    assert s["warnings"] == []
    _assert_identity(s)


def test_payment_dated_before_its_invoice_is_credit_not_invented_debt(db):
    """v1 split the previous balance by invoice date and showed a customer who
    owes nothing $22,088.32. A deposit paid before its invoice is dated must
    land in the previous balance as credit."""
    c = _customer(db)
    inv = _invoice(db, c, 1000, day=date(2026, 4, 10), balance=0, status="paid")
    _payment(db, inv, 1000, date(2026, 4, 1))
    s = _statement(db, c, date(2026, 4, 5))
    assert s["previous_balance"] == Decimal("-1000.00")
    assert s["invoiced"] == Decimal("1000.00")
    assert s["payments_and_credits_total"] == Decimal("0.00")
    assert s["ending_balance"] == Decimal("0.00")
    _assert_identity(s)


def test_paid_invoice_short_of_its_rows_shows_no_debt_and_dates_the_remainder_on_paid_at(db):
    """QuickBooks-era: paid, balance 0, rows short (a deposit never imported).
    paid_at is a midnight-UTC date-only stamp — read as that date, not the
    Central day before."""
    db.add(AppSettings(timezone="America/Chicago"))
    db.commit()
    c = _customer(db)
    inv = _invoice(
        db, c, 10000, day=date(2026, 2, 3), balance=0, status="paid",
        paid_at=datetime(2026, 2, 3, 0, 0, tzinfo=UTC),
    )
    _payment(db, inv, 1500, date(2026, 2, 20))
    s = _statement(db, c, date(2026, 2, 3))
    kinds = {(r["kind"], r["date"], r["amount"]) for r in s["payments_and_credits"]}
    assert ("unrecorded", "2026-02-03", Decimal("8500.00")) in kinds
    assert ("payment", "2026-02-20", Decimal("1500.00")) in kinds
    assert s["ending_balance"] == Decimal("0.00")
    later = _statement(db, c, date(2026, 3, 1))
    assert later["previous_balance"] == Decimal("0.00")
    assert inv.invoice_number in _warned(s)  # its records don't add up and it is dated in range
    assert later["warnings"] == []  # nothing about it can move a March figure
    _assert_identity(s)


def test_paid_invoice_over_its_rows_with_no_ledger_credit_shows_no_credit_and_is_warned(db):
    c = _customer(db)
    inv = _invoice(db, c, 1000, day=date(2026, 3, 1), balance=0, status="paid")
    _payment(db, inv, 700, date(2026, 3, 5))
    _payment(db, inv, 751, date(2026, 3, 9))
    s = _statement(db, c, date(2026, 3, 1))
    assert s["credit_on_account"] == Decimal("0.00")
    assert s["payments_and_credits_total"] == Decimal("1000.00")  # the excess is not counted
    assert s["ending_balance"] == Decimal("0.00")
    assert _warned(s) == {inv.invoice_number}
    _assert_identity(s)


def test_quickbooks_duplicate_before_start_and_genuine_payment_after_is_warned(db):
    """QuickBooks payments 2192/2465/2486: the full amount on one invoice AND
    the split on others. Capping in date order keeps the early copy and trims
    the genuine later payment — every figure agrees, so only the warning can
    tell the office."""
    c = _customer(db)
    inv = _invoice(db, c, 1000, day=date(2026, 1, 5), balance=0, status="paid")
    _payment(db, inv, 1000, date(2026, 1, 6), reference=None)  # the duplicate
    _payment(db, inv, 1000, date(2026, 2, 10), reference="qb:2465")  # the genuine one
    s = _statement(db, c, date(2026, 2, 1))
    assert _warned(s) == {inv.invoice_number}


def test_each_reach_clause_warns_on_its_own(db):
    c = _customer(db)
    start = date(2026, 6, 1)
    # 1. The invoice is dated on or after start; every record is before it.
    by_date = _invoice(db, c, 400, day=date(2026, 6, 10), balance=0, status="paid")
    _payment(db, by_date, 450, date(2026, 5, 20))
    # 2. A short invoice, every record before start, paid_at on or after it.
    by_remainder = _invoice(
        db, c, 900, day=date(2026, 5, 1), balance=0, status="paid",
        paid_at=datetime(2026, 6, 5, 0, 0, tzinfo=UTC),
    )
    _payment(db, by_remainder, 100, date(2026, 5, 2))
    # 3. An anomalous invoice dated before start with a non-zero record after.
    by_record = _invoice(db, c, 300, day=date(2026, 5, 1), balance=0, status="paid")
    _payment(db, by_record, 300, date(2026, 5, 3))
    _payment(db, by_record, 60, date(2026, 6, 20))
    # Control: anomalous, entirely before start — contributes exactly its balance.
    silent = _invoice(db, c, 200, day=date(2026, 4, 1), balance=0, status="paid")
    _payment(db, silent, 260, date(2026, 4, 2))

    s = _statement(db, c, start)
    assert _warned(s) == {by_date.invoice_number, by_remainder.invoice_number, by_record.invoice_number}
    _assert_identity(s)


def test_a_zero_amount_record_in_range_does_not_warn(db):
    """Three of the first reach test's six prod warnings came through $0.00
    rows dated in 2026 that could not move any figure."""
    c = _customer(db)
    inv = _invoice(db, c, 500, day=date(2026, 2, 1), balance=0, status="paid",
                   paid_at=datetime(2026, 2, 1, 0, 0, tzinfo=UTC))
    _payment(db, inv, 400, date(2026, 2, 2))
    _payment(db, inv, 0, date(2026, 5, 6))
    s = _statement(db, c, date(2026, 3, 1))
    assert s["warnings"] == []


def test_open_invoices_list_every_unpaid_invoice_whatever_its_date(db):
    """The 2026 floor would otherwise leave a customer who owes only on an old
    invoice a statement with a balance and nothing to pay; a past range
    would hide invoices dated after its end."""
    c = _customer(db)
    old = _invoice(db, c, 1200, day=date(2026, 1, 3), due=date(2026, 1, 3))
    new = _invoice(db, c, 400, day=date(2026, 8, 1), due=date(2026, 8, 31))
    past = _statement(db, c, date(2026, 3, 1), end=date(2026, 4, 30))
    assert {r["invoice_number"] for r in past["open_invoices"]} == {old.invoice_number, new.invoice_number}
    assert past["previous_balance"] == Decimal("1200.00")
    assert past["ending_balance"] == Decimal("1200.00")  # balance at end of April
    assert past["total_unpaid"] == Decimal("1600.00")
    assert not past["ends_today"]
    for row in past["open_invoices"]:
        assert "pay_url" in row


def test_aging_buckets_partition_the_unpaid_balance(db):
    c = _customer(db)
    amounts = {
        None: 11, TODAY: 13, TODAY + timedelta(days=5): 17,
        TODAY - timedelta(days=30): 19, TODAY - timedelta(days=31): 23,
        TODAY - timedelta(days=90): 29, TODAY - timedelta(days=91): 31,
    }
    for due, amount in amounts.items():
        _invoice(db, c, amount, day=date(2026, 1, 2), due=due)
    s = _statement(db, c, date(2026, 1, 1))
    assert s["aging"] == {
        "current": Decimal("41.00"),
        "days_1_30": Decimal("19.00"),
        "days_31_60": Decimal("23.00"),
        "days_61_90": Decimal("29.00"),
        "over_90": Decimal("31.00"),
    }
    _assert_identity(s)


def test_drafts_voids_and_the_draft_payment_warning(db):
    c = _customer(db)
    draft = _invoice(db, c, 300, day=date(2026, 7, 1), status="draft")
    _payment(db, draft, 100, date(2026, 7, 2))
    deposit = _invoice(db, c, 500, day=date(2026, 7, 1), status="draft", billing_type="deposit")
    _payment(db, deposit, 500, date(2026, 7, 2))
    void = _invoice(db, c, 900, day=date(2026, 7, 1), balance=800, status="void")
    _payment(db, void, 100, date(2026, 7, 3))

    s = _statement(db, c, date(2026, 6, 1))
    assert s["invoices"] == [] and s["payments_and_credits"] == [] and s["open_invoices"] == []
    assert [(w["kind"], w["invoice_number"]) for w in s["warnings"]] == [
        ("payment_on_draft", draft.invoice_number)
    ]


def test_a_payment_dated_tomorrow_counts_on_today(db):
    """Card payments are written on the UTC day; after ~7pm Central that is tomorrow."""
    c = _customer(db)
    inv = _invoice(db, c, 250, day=date(2026, 9, 1), balance=0, status="paid")
    _payment(db, inv, 250, TODAY + timedelta(days=1))
    s = _statement(db, c, date(2026, 9, 1))
    assert s["payments_and_credits"][0]["date"] == TODAY.isoformat()
    assert s["ending_balance"] == Decimal("0.00")
    _assert_identity(s)


def test_an_invoice_dated_in_the_future_counts_as_today(db):
    c = _customer(db)
    _invoice(db, c, 75, day=TODAY + timedelta(days=10))
    s = _statement(db, c, date(2026, 9, 1))
    assert s["invoiced"] == Decimal("75.00")
    _assert_identity(s)


def test_credit_memo_created_late_on_dec_31_central_is_dated_dec_31(db):
    db.add(AppSettings(timezone="America/Chicago"))
    db.commit()
    c = _customer(db)
    inv = _invoice(db, c, 100, day=date(2026, 12, 1), balance=60)
    db.add(InvoiceAdjustment(
        invoice_id=inv.id, kind="credit_memo", amount=Decimal("40"), reason="goodwill",
        company_id=COMPANY, created_at=datetime(2027, 1, 1, 5, 0, tzinfo=UTC),  # 11pm Central
    ))
    db.commit()
    s = _statement(db, c, date(2026, 12, 1), end=date(2026, 12, 31), today=date(2027, 1, 10))
    assert [(r["kind"], r["date"]) for r in s["payments_and_credits"]] == [("credit_memo", "2026-12-31")]
    assert s["ending_balance"] == Decimal("60.00")


def test_full_stripe_refund_reopens_the_invoice_and_the_payment_leaves_every_range(db):
    from gdx_dispatch.core.payments import _reverse_recorded_payment

    c = _customer(db)
    inv = _invoice(db, c, 400, day=date(2026, 8, 1), balance=0, status="paid")
    _payment(db, inv, 400, date(2026, 8, 2), reference="pi_test_refund")
    _reverse_recorded_payment(db, "pi_test_refund", "charge.refunded")
    db.commit()
    s = _statement(db, c, date(2026, 8, 1))
    assert s["payments_and_credits"] == []
    assert s["total_unpaid"] == Decimal("400.00")
    assert s["warnings"] == []
    _assert_identity(s)


# ---------------------------------------------------------------------------
# Ledger on: booked overpayment, applied credit, refund — real routes
# ---------------------------------------------------------------------------

def _enable_ledger(db):
    from gdx_dispatch.modules.ledger.service import ensure_gl_seed

    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = True
    db.commit()


def _issued(db, customer, total, day):
    from gdx_dispatch.modules.ledger.service import transition_invoice_status

    inv = _invoice(db, customer, total, day=day, status="draft")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    return inv


def test_booked_overpayment_is_credit_not_a_warning_and_applying_it_counts_once(db):
    from gdx_dispatch.routers.invoices import (
        ApplyCreditIn,
        PaymentCreateIn,
        apply_customer_credit,
        record_payment,
    )

    _enable_ledger(db)
    c = _customer(db)
    first = _issued(db, c, 100, date(2026, 7, 1))
    record_payment(
        first.id,
        PaymentCreateIn(amount=150, method="cash", date=date(2026, 7, 2), allow_overpayment=True),
        _=USER, db=db,
    )
    s = _statement(db, c, date(2026, 7, 1))
    assert s["credit_on_account"] == Decimal("50.00")
    assert s["warnings"] == []
    # Capped in the arithmetic, but the customer's copy says what they paid.
    [row] = s["payments_and_credits"]
    assert row["amount"] == Decimal("100.00")
    assert row["note"] == "$150.00 received; $50.00 held as credit on account"
    _assert_identity(s)

    second = _issued(db, c, 80, date(2026, 7, 10))
    apply_customer_credit(second.id, ApplyCreditIn(amount=50.0), db=db, _=USER)
    s = _statement(db, c, date(2026, 7, 1))
    assert s["credit_on_account"] == Decimal("0.00")
    assert s["total_unpaid"] == Decimal("30.00")
    assert s["warnings"] == []
    counted = sorted((r["kind"], r["amount"]) for r in s["payments_and_credits"])
    assert counted == [("credit_applied", Decimal("50.00")), ("payment", Decimal("100.00"))]
    _assert_identity(s)


def test_refunded_overpayment_leaves_credit_on_account(db):
    from gdx_dispatch.routers.invoices import PaymentCreateIn, RefundIn, process_refund, record_payment

    _enable_ledger(db)
    c = _customer(db)
    inv = _issued(db, c, 100, date(2026, 7, 1))
    record_payment(
        inv.id,
        PaymentCreateIn(amount=150, method="cash", date=date(2026, 7, 2), allow_overpayment=True),
        _=USER, db=db,
    )
    process_refund(str(inv.id), RefundIn(amount=50, reason="overpaid", refund_method="check"), db=db, _=USER)
    s = _statement(db, c, date(2026, 7, 1))
    assert s["credit_on_account"] == Decimal("0.00")
    assert [r["amount"] for r in s["refunds"]] == [Decimal("50.00")]
    assert s["total_unpaid"] == Decimal("0.00")
    _assert_identity(s)


# ---------------------------------------------------------------------------
# PDF and HTTP
# ---------------------------------------------------------------------------

def test_pdf_renders_for_real_and_has_no_warnings_or_dead_pay_links(db, monkeypatch):
    from gdx_dispatch.core.pdf_generator import _JINJA_ENV, _default_branding
    from gdx_dispatch.routers.pdf import _branding_payload
    from gdx_dispatch.services.customer_statements import KIND_LABELS

    monkeypatch.delenv("GDX_PUBLIC_BASE_URL", raising=False)
    c = _customer(db)
    inv = _invoice(db, c, 1000, day=date(2026, 5, 1), balance=0, status="paid")
    _payment(db, inv, 1200, date(2026, 5, 2))  # anomalous: a warning exists
    _invoice(db, c, 640, day=date(2026, 8, 1), due=date(2026, 8, 31))
    s = _statement(db, c, date(2026, 5, 1))
    assert s["warnings"]

    pdf = render_statement_pdf(db, s)
    assert pdf[:5] == b"%PDF-" and len(pdf) > 1000

    page = _JINJA_ENV.get_template("statement_pdf.html").render(
        s=s, branding=_default_branding(_branding_payload(db)), kind_labels=KIND_LABELS,
    )
    assert "$640.00" in page and "Total unpaid balance" in page
    assert "don't add up" not in page  # warnings are for the office only
    assert "Pay online" not in page  # Stripe is not configured, so no dead link


def test_get_statement_rejects_a_range_before_the_floor(db):
    from gdx_dispatch.routers.customer_statements import get_statement

    c = _customer(db)
    with pytest.raises(HTTPException) as exc:
        get_statement(c.id, preset=None, start=date(2025, 12, 1), end=date(2026, 1, 31), _=USER, db=db)
    assert exc.value.status_code == 422


def test_get_statement_offers_presets_and_the_default_recipient(db):
    from gdx_dispatch.routers.customer_statements import get_statement

    c = _customer(db)
    _invoice(db, c, 100, day=date(2026, 3, 1))
    out = get_statement(c.id, preset=None, start=None, end=None, _=USER, db=db)
    assert out["range"]["preset"] == "last_90"
    keys = [p["key"] for p in out["presets"]]
    assert keys[:4] == ["last_30", "last_60", "last_90", "ytd"] and keys[-1] == "custom"
    assert out["default_recipient"]["email"] == "owner@example.com"
    assert isinstance(out["total_unpaid"], float)


def _audit_rows(db, customer):
    from gdx_dispatch.core.audit import AuditLog

    return db.execute(
        select(AuditLog).where(AuditLog.entity_type == "customer", AuditLog.entity_id == str(customer.id))
    ).scalars().all()


def test_send_with_no_mail_transport_reports_the_skip_and_leaves_both_trails(db):
    from gdx_dispatch.routers.customer_statements import StatementSendIn, send_statement

    c = _customer(db)
    inv = _invoice(db, c, 1000, day=date(2026, 8, 1), balance=0, status="paid")
    _payment(db, inv, 1100, date(2026, 8, 2))  # warned
    out = send_statement(
        c.id, StatementSendIn(start=date(2026, 8, 1), end=date(2026, 9, 15)), user=USER, db=db
    )
    assert out["email_sent"] is False and out["email_skip_reason"]

    emails = db.execute(select(OutboundEmail).where(OutboundEmail.entity_id == str(c.id))).scalars().all()
    assert [(e.kind, e.entity_type) for e in emails] == [("statement", "customer")]

    rows = _audit_rows(db, c)
    assert [r.action for r in rows] == ["statement_not_sent"]
    assert rows[0].user_id == "office-user"
    assert rows[0].tenant_id == COMPANY  # the Activity feeds filter on it
    details = rows[0].details
    assert details["warnings"] == [{"kind": "records_dont_add_up", "invoice_number": inv.invoice_number}]
    assert details["invoices"][0]["invoice_number"] == inv.invoice_number
    assert set(details["aging"]) == {"current", "days_1_30", "days_31_60", "days_61_90", "over_90"}


@pytest.mark.parametrize(
    ("email", "to_email", "reason"),
    [("", None, "customer_has_no_email"), ("owner@example.com", "not-an-address", "invalid_recipient_email")],
)
def test_send_without_a_usable_recipient_is_refused_and_audited(db, email, to_email, reason):
    from gdx_dispatch.routers.customer_statements import StatementSendIn, send_statement

    c = _customer(db, email=email)
    out = send_statement(c.id, StatementSendIn(to_email=to_email), user=USER, db=db)
    assert out == {"email_sent": False, "pdf_attached": False, "to_email": None, "email_skip_reason": reason}
    assert [r.action for r in _audit_rows(db, c)] == ["statement_not_sent"]


def test_routes_demand_the_right_permission():
    from gdx_dispatch.tests.test_invoice_money_permissions import _required_keys_by_route

    required = _required_keys_by_route()
    assert "invoices.read_all" in required["GET /api/customers/{customer_id}/statement"]
    assert "invoices.read_all" in required["GET /api/customers/{customer_id}/statement/pdf"]
    assert "invoices.send" in required["POST /api/customers/{customer_id}/statement/send"]


def test_pay_links_reach_the_pdf_and_the_email_and_credit_is_mentioned(db, monkeypatch):
    """Both of the customer's ways to pay: the PDF's link and the email body's
    (the phone path). A test that only checked a link was absent could not
    fail if every link vanished."""
    from gdx_dispatch.core.pdf_generator import _JINJA_ENV, _default_branding
    from gdx_dispatch.routers.customer_statements import _email_html
    from gdx_dispatch.routers.pdf import _branding_payload
    from gdx_dispatch.services.customer_statements import KIND_LABELS, to_json

    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://pay.example.invalid")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "not-a-real-key-presence-only")
    c = _customer(db)
    inv = _invoice(db, c, 640, day=date(2026, 8, 1), due=date(2026, 8, 31))
    s = _statement(db, c, date(2026, 8, 1))
    url = f"https://pay.example.invalid/pay/{inv.public_token}"
    assert [r["pay_url"] for r in s["open_invoices"]] == [url]

    page = _JINJA_ENV.get_template("statement_pdf.html").render(
        s=s, branding=_default_branding(_branding_payload(db)), kind_labels=KIND_LABELS,
    )
    assert f'href="{url}"' in page

    data = to_json(s)
    _subject, body = _email_html(db, data, "Pat")
    assert url in body and inv.invoice_number in body and "$640.00" in body
    assert "Credit on account" not in body
    data["credit_on_account"] = 25.0
    assert "Credit on account: $25.00" in _email_html(db, data, "Pat")[1]


def test_send_without_a_typed_address_goes_to_the_primary_contact(db):
    from gdx_dispatch.routers.customer_statements import StatementSendIn, send_statement

    c = _customer(db)
    db.add(CustomerContact(
        company_id=COMPANY, customer_id=c.id, name="Bob Smith", email="bob@builder.example.invalid", is_primary=True,
    ))
    db.commit()
    send_statement(c.id, StatementSendIn(start=date(2026, 8, 1), end=date(2026, 9, 15)), user=USER, db=db)
    [email] = db.execute(select(OutboundEmail).where(OutboundEmail.entity_id == str(c.id))).scalars().all()
    assert email.to_email == "bob@builder.example.invalid"
    assert email.recipient_source == "primary_contact"
    assert "Hi Bob," in email.body_html


def test_a_failure_while_producing_the_statement_still_leaves_an_audit_row(db, monkeypatch):
    from gdx_dispatch.routers import customer_statements as router
    from gdx_dispatch.routers.customer_statements import StatementSendIn, send_statement

    def boom(*_a, **_k):
        raise RuntimeError("weasyprint fell over")

    monkeypatch.setattr(router, "render_statement_pdf", boom)
    c = _customer(db)
    out = send_statement(c.id, StatementSendIn(start=date(2026, 8, 1), end=date(2026, 9, 15)), user=USER, db=db)
    assert out["email_sent"] is False and out["email_skip_reason"] == "exception"
    rows = _audit_rows(db, c)
    assert [r.action for r in rows] == ["statement_not_sent"]
    assert rows[0].details["skip_reason"] == "exception"


def test_the_note_lands_on_the_payment_the_ledger_booked_as_credit(db):
    """The office records a check under its check date after a card payment
    already settled the invoice. The ledger books the check as credit; the
    statement must say so on the check, not on whichever payment is dated last."""
    from gdx_dispatch.routers.invoices import PaymentCreateIn, record_payment

    _enable_ledger(db)
    c = _customer(db)
    inv = _issued(db, c, 100, date(2026, 7, 1))
    record_payment(inv.id, PaymentCreateIn(amount=100, method="card", date=date(2026, 7, 5)), _=USER, db=db)
    record_payment(
        inv.id,
        PaymentCreateIn(amount=60, method="check", date=date(2026, 7, 3), allow_overpayment=True),
        _=USER, db=db,
    )
    s = _statement(db, c, date(2026, 7, 1))
    rows = {r["method"]: r for r in s["payments_and_credits"]}
    assert rows["check"]["amount"] == Decimal("0.00")
    assert rows["check"]["note"] == "$60.00 received; $60.00 held as credit on account"
    assert rows["card"]["amount"] == Decimal("100.00") and rows["card"]["note"] is None
    assert s["credit_on_account"] == Decimal("60.00") and s["warnings"] == []
    _assert_identity(s)


def test_a_credit_memo_before_an_overpaying_check_is_still_shown(db):
    from gdx_dispatch.routers.invoices import CreditMemoIn, PaymentCreateIn, issue_credit_memo, record_payment

    _enable_ledger(db)
    c = _customer(db)
    inv = _issued(db, c, 100, date(2026, 7, 1))
    issue_credit_memo(str(inv.id), CreditMemoIn(amount=30, reason="goodwill"), db=db, _=USER)
    record_payment(
        inv.id,
        PaymentCreateIn(amount=100, method="check", date=date(2026, 7, 2), allow_overpayment=True),
        _=USER, db=db,
    )
    s = _statement(db, c, date(2026, 7, 1))
    kinds = sorted((r["kind"], r["amount"], r["note"]) for r in s["payments_and_credits"])
    assert kinds == [
        ("credit_memo", Decimal("30.00"), None),
        ("payment", Decimal("70.00"), "$100.00 received; $30.00 held as credit on account"),
    ]
    assert s["credit_on_account"] == Decimal("30.00") and s["warnings"] == []
    _assert_identity(s)


def test_a_failed_email_log_write_still_leaves_an_audit_row(db):
    """The real send path writes its own email-log row and swallows a failure
    to write it, leaving the session unusable WITHOUT raising. Replacing the
    send with something that raises would test a failure the code never has;
    this breaks the real write instead."""
    from sqlalchemy import event

    from gdx_dispatch.routers.customer_statements import StatementSendIn, send_statement

    def null_subject(_mapper, _conn, target):
        target.subject = None  # NOT NULL — the insert fails inside _record_outbound

    event.listen(OutboundEmail, "before_insert", null_subject)
    try:
        c = _customer(db)
        out = send_statement(c.id, StatementSendIn(start=date(2026, 8, 1), end=date(2026, 9, 15)), user=USER, db=db)
    finally:
        event.remove(OutboundEmail, "before_insert", null_subject)
    assert out["email_sent"] is False
    assert [r.action for r in _audit_rows(db, c)] == ["statement_not_sent"]


def test_a_negative_payment_row_is_warned_not_hidden(db):
    c = _customer(db)
    inv = _invoice(db, c, 100, day=date(2026, 8, 1), balance=0, status="paid")
    _payment(db, inv, 150, date(2026, 8, 2))
    _payment(db, inv, -50, date(2026, 8, 3))  # records sum to the settled amount
    s = _statement(db, c, date(2026, 8, 1))
    assert _warned(s) == {inv.invoice_number}
    _assert_identity(s)
