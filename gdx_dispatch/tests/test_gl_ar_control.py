"""GDXA-1181: the AR control tie (gl-phase3-trust-switch §3.2 criterion 3).

Open-invoice balances (each live invoice's ``balance_due``) must equal the
net of the AR control account, and every AR line must trace to an invoice.
Each test drifts the books one way the trial-balance zero-check cannot see,
through the real posting rules and a real SQLite session, and reads the
report back.

Not caught, by construction: a payment recorded on the wrong invoice whose
posting followed it. The row and the ledger agree, so only the customer
(through a statement) can tell.
"""
from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import Invoice, InvoiceAdjustment, InvoiceLine, Payment
from gdx_dispatch.modules.ledger import reports
from gdx_dispatch.modules.ledger.backfill import gl_ar_balance_cents
from gdx_dispatch.modules.ledger.engine import PostingEvent, PostingLine, post_for_event
from gdx_dispatch.modules.ledger.models import ROLE_AR, ROLE_OPENING_EQUITY, GlAccount
from gdx_dispatch.modules.ledger.router import report_ar_control
from gdx_dispatch.modules.ledger.rules import (
    ISSUED_STATUSES,
    RECEIVABLE_STATUSES,
    invoice_ar_balance_cents,
    post_credit_memo,
    post_payment_received,
    post_refund,
)
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status
from gdx_dispatch.routers.invoices import _recalculate_invoice

COMPANY = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.ledger_posting_enabled = True
    tenant_db.commit()
    return tenant_db


def _issued(
    db, amount: str, *, status: str = "sent", invoice_date: date = date(2026, 7, 8)
) -> Invoice:
    """A one-line invoice moved to ``status`` through the chokepoint."""
    total = Decimal(amount)
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="draft",
        subtotal=total,
        tax_amount=Decimal("0"),
        total=total,
        balance_due=total,
        invoice_date=invoice_date,
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=inv.id, description="repair", quantity=1,
            unit_price=total, line_total=total, company_id=COMPANY,
        )
    )
    db.flush()
    if status != "draft":
        transition_invoice_status(db, inv, status, actor="t")
        db.flush()
    return inv


def _payment(
    db, *, recorded_on: Invoice, posted_against: Invoice, amount: str,
    paid_on: date = date(2026, 7, 9),
) -> Payment:
    payment = Payment(
        id=uuid4(), invoice_id=recorded_on.id, amount=Decimal(amount),
        method="check", payment_date=paid_on, company_id=COMPANY,
    )
    db.add(payment)
    db.flush()
    post_payment_received(db, payment, posted_against, actor="t")
    db.flush()
    return payment


def test_ties_when_every_balance_matches_the_ledger(db):
    inv = _issued(db, "530.00")
    _payment(db, recorded_on=inv, posted_against=inv, amount="200.00")
    inv.balance_due = Decimal("330.00")
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert out["control_cents"] == 330_00
    assert out["subledger_cents"] == 330_00
    assert out["difference_cents"] == 0
    assert out["drift"] == []
    assert out["tied"] is True


def test_the_real_recalculation_ties_after_a_credit_memo_and_an_overpayment(db):
    # balance_due computed by the production recalc, not set by the test.
    inv = _issued(db, "100.00")
    memo = InvoiceAdjustment(
        id=uuid4(), invoice_id=inv.id, kind="credit_memo", amount=Decimal("20.00"),
        reason="goodwill", created_by="t", company_id=COMPANY,
    )
    db.add(memo)
    db.flush()
    post_credit_memo(db, memo, inv, actor="t")
    _payment(db, recorded_on=inv, posted_against=inv, amount="30.00")
    _recalculate_invoice(inv, db, actor="t")
    db.flush()
    assert inv.balance_due == Decimal("50.00")
    assert reports.ar_control(db, COMPANY)["tied"] is True

    # $90 on a $50 balance: $40 is customer credit (2300), not negative AR.
    _payment(db, recorded_on=inv, posted_against=inv, amount="90.00")
    _recalculate_invoice(inv, db, actor="t")
    db.flush()
    out = reports.ar_control(db, COMPANY)
    assert inv.balance_due == Decimal("0.00")
    assert (out["control_cents"], out["tied"]) == (0, True)


def test_a_pre_cutover_invoice_ties_through_its_opening_anchor(db):
    settings = ensure_gl_seed(db, COMPANY)
    settings.cutover_month = date(2026, 7, 1)
    db.flush()
    era = _issued(db, "400.00", invoice_date=date(2026, 6, 15))
    _payment(db, recorded_on=era, posted_against=era, amount="150.00", paid_on=date(2026, 7, 20))
    _recalculate_invoice(era, db, actor="t")
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert era.balance_due == Decimal("250.00")
    assert (out["control_cents"], out["subledger_cents"], out["tied"]) == (250_00, 250_00, True)


def test_a_late_era_payment_beyond_its_anchor_is_not_tied(db):
    """GDXA-1223. A payment dated before cutover but recorded after it settles
    the opening anchor; the excess over the anchor posts nowhere, and
    balance_due floors at zero. Both sides read 0, yet $100 is on no ledger
    line, so the tie must fail and name it, as reconciliation_report does."""
    from gdx_dispatch.modules.ledger.backfill import reconciliation_report

    settings = ensure_gl_seed(db, COMPANY)
    settings.cutover_month = date(2026, 7, 1)
    db.flush()
    era = _issued(db, "100.00", invoice_date=date(2026, 6, 15))
    assert reports.ar_control(db, COMPANY)["control_cents"] == 100_00  # the anchor
    late = _payment(
        db, recorded_on=era, posted_against=era, amount="200.00", paid_on=date(2026, 6, 20)
    )
    _recalculate_invoice(era, db, actor="t")
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert era.balance_due == Decimal("0.00")
    assert (out["control_cents"], out["subledger_cents"], out["difference_cents"]) == (0, 0, 0)
    assert out["drift"] == [] and out["unattributed_cents"] == 0
    assert out["tied"] is False
    assert out["era_settlement_shortfall_cents"] == 100_00
    [row] = out["era_settlement_shortfalls"]
    assert (row["invoice_id"], row["payment_id"]) == (str(era.id), str(late.id))
    assert (row["amount_cents"], row["allocated_cents"], row["uncovered_cents"]) == (
        200_00, 100_00, 100_00,
    )
    assert row["refunded_cents"] == 0
    # One computation, two reports: they cannot disagree.
    assert reconciliation_report(db, COMPANY)["era_settlement_shortfalls"] == out[
        "era_settlement_shortfalls"
    ]
    assert out["era_settlement_shortfall_count"] == 1


def _era_overpaid(db) -> tuple[Invoice, Payment]:
    """A $100 era invoice paid $200 by a late-learned era payment: $100 of
    it posts nowhere (the GDXA-1223 shortfall)."""
    settings = ensure_gl_seed(db, COMPANY)
    settings.cutover_month = date(2026, 7, 1)
    db.flush()
    era = _issued(db, "100.00", invoice_date=date(2026, 6, 15))
    late = _payment(
        db, recorded_on=era, posted_against=era, amount="200.00", paid_on=date(2026, 6, 20)
    )
    _recalculate_invoice(era, db, actor="t")
    db.flush()
    return era, late


def _refund(db, invoice: Invoice, amount: str) -> InvoiceAdjustment:
    """A refund through the real posting rule, as process_refund records it."""
    refund = InvoiceAdjustment(
        id=uuid4(), invoice_id=invoice.id, kind="refund", amount=Decimal(amount),
        reason="overpaid", refund_method="check", created_by="t", company_id=COMPANY,
    )
    db.add(refund)
    db.flush()
    post_refund(db, refund, invoice, actor="t")
    db.flush()
    return refund


def test_a_refunded_era_overpayment_ties(db):
    """GDXA-1247. The $100 that posted nowhere went back to the customer, so
    no money is missing: the shortfall nets to zero and the book ties."""
    from gdx_dispatch.modules.ledger.backfill import reconciliation_report

    era, _late = _era_overpaid(db)
    assert reports.ar_control(db, COMPANY)["tied"] is False
    _refund(db, era, "100.00")

    out = reports.ar_control(db, COMPANY)
    assert (out["control_cents"], out["subledger_cents"]) == (0, 0)
    assert out["era_settlement_shortfalls"] == []
    assert out["era_settlement_shortfall_cents"] == 0
    assert out["era_settlement_refunded_cents"] == 100_00
    assert out["tied"] is True
    report = reconciliation_report(db, COMPANY)
    assert report["era_settlement_shortfalls"] == []
    assert report["totals"]["era_settlement_refunded_cents"] == 100_00


def test_a_partly_refunded_era_overpayment_shows_the_remainder(db):
    era, late = _era_overpaid(db)
    _refund(db, era, "40.00")

    out = reports.ar_control(db, COMPANY)
    [row] = out["era_settlement_shortfalls"]
    assert row["payment_id"] == str(late.id)
    assert (row["allocated_cents"], row["refunded_cents"], row["uncovered_cents"]) == (
        100_00, 40_00, 60_00,
    )
    assert out["era_settlement_shortfall_cents"] == 60_00
    assert out["era_settlement_refunded_cents"] == 40_00
    assert out["tied"] is False


def test_a_reversed_refund_does_not_net_the_shortfall(db):
    """A refund whose entry was reversed has returned nothing to the books."""
    from gdx_dispatch.modules.ledger.engine import reverse_entry
    from gdx_dispatch.modules.ledger.models import GlJournalEntry

    era, _late = _era_overpaid(db)
    refund = _refund(db, era, "100.00")
    [entry] = db.scalars(
        select(GlJournalEntry).where(
            GlJournalEntry.source_type == "adjustment",
            GlJournalEntry.source_id == str(refund.id),
        )
    ).all()
    reverse_entry(db, entry, created_by="t")
    db.flush()

    out = reports.ar_control(db, COMPANY)
    [row] = out["era_settlement_shortfalls"]
    assert (row["refunded_cents"], row["uncovered_cents"]) == (0, 100_00)
    assert out["era_settlement_refunded_cents"] == 0
    assert out["tied"] is False


def test_a_refund_of_customer_credit_does_not_net_the_shortfall(db):
    """A later $50 payment on the paid era invoice is all 2300 credit; a $50
    refund retires that credit (post_refund is credit-first). The $100 that
    posted nowhere is still missing."""
    era, _late = _era_overpaid(db)
    _payment(db, recorded_on=era, posted_against=era, amount="50.00", paid_on=date(2026, 7, 20))
    _refund(db, era, "50.00")

    out = reports.ar_control(db, COMPANY)
    [row] = out["era_settlement_shortfalls"]
    assert (row["refunded_cents"], row["uncovered_cents"]) == (0, 100_00)
    assert out["tied"] is False


def test_shortfall_rows_are_capped_largest_first_and_the_count_and_total_are_not(
    db, monkeypatch
):
    """GDXA-1244. The payload lists at most AR_SHORTFALL_ROW_LIMIT shortfalls,
    largest uncovered first, like drift; the count and the cents total cover
    every one, so the cap changes what is listed and never whether it ties."""
    monkeypatch.setattr(reports, "AR_SHORTFALL_ROW_LIMIT", 2)
    settings = ensure_gl_seed(db, COMPANY)
    settings.cutover_month = date(2026, 7, 1)
    db.flush()
    excess = {}
    for paid in ("130.00", "250.00", "190.00"):  # each on a $100 anchor
        era = _issued(db, "100.00", invoice_date=date(2026, 6, 15))
        _payment(db, recorded_on=era, posted_against=era, amount=paid, paid_on=date(2026, 6, 20))
        _recalculate_invoice(era, db, actor="t")
        excess[str(era.id)] = int(Decimal(paid) * 100) - 100_00
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert out["era_settlement_shortfall_count"] == 3
    assert out["era_settlement_shortfall_cents"] == 30_00 + 150_00 + 90_00
    assert [r["uncovered_cents"] for r in out["era_settlement_shortfalls"]] == [150_00, 90_00]
    assert all(excess[r["invoice_id"]] == r["uncovered_cents"] for r in out["era_settlement_shortfalls"])
    assert out["tied"] is False


def test_drift_rows_are_capped_largest_first_and_the_count_is_not(db, monkeypatch):
    """GDXA-1181's drift cap, which shipped without a test (GDXA-1244)."""
    monkeypatch.setattr(reports, "AR_DRIFT_ROW_LIMIT", 2)
    for edited in ("540.00", "600.00", "580.00"):  # each on a $530 invoice
        inv = _issued(db, "530.00")
        inv.balance_due = Decimal(edited)
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert out["drift_invoice_count"] == 3
    assert [r["drift_cents"] for r in out["drift"]] == [-70_00, -50_00]
    assert out["difference_cents"] == -(10_00 + 70_00 + 50_00)


def test_balance_edited_without_a_repost_is_named(db):
    inv = _issued(db, "530.00")
    inv.balance_due = Decimal("600.00")  # the row moved, the ledger did not
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert out["tied"] is False
    assert out["difference_cents"] == -70_00
    [row] = out["drift"]
    assert row["invoice_number"] == inv.invoice_number
    assert (row["open_balance_cents"], row["ledger_ar_cents"], row["drift_cents"]) == (
        600_00, 530_00, -70_00,
    )


def test_an_issued_invoice_that_never_posted_is_named(db):
    _issued(db, "100.00")
    # Issued while posting was off and never backfilled: the row is sent,
    # the ledger never heard of it.
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = False
    db.flush()
    orphan = _issued(db, "250.00")
    settings.ledger_posting_enabled = True
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert out["control_cents"] == 100_00
    assert out["subledger_cents"] == 350_00
    [row] = out["drift"]
    assert row["invoice_id"] == str(orphan.id)
    assert row["ledger_ar_cents"] == 0


def test_offsetting_drift_on_two_invoices_nets_to_zero_and_still_fails(db):
    a = _issued(db, "300.00")
    b = _issued(db, "300.00")
    pay = _payment(db, recorded_on=a, posted_against=a, amount="100.00")
    # The office's balances say the $100 came off B, but the only posting that
    # relieved AR is sourced from A's payment: a balance moved between
    # invoices with no entry behind it.
    a.balance_due = Decimal("300.00")
    b.balance_due = Decimal("200.00")
    db.flush()

    out = reports.ar_control(db, COMPANY)
    # Attribution follows the payment row: A's ledger relief is A's, so A
    # owes 300 on the row but 200 on the ledger, and B the reverse. The
    # totals agree; only the per-invoice walk sees it.
    assert pay.invoice_id == a.id
    assert out["difference_cents"] == 0
    assert out["tied"] is False
    assert out["era_settlement_shortfall_cents"] == 0
    by_id = {r["invoice_id"]: r["drift_cents"] for r in out["drift"]}
    assert by_id == {str(a.id): -100_00, str(b.id): 100_00}


def test_ar_that_no_invoice_claims_is_reported_by_source(db):
    _issued(db, "100.00")
    post_for_event(
        db,
        PostingEvent(
            company_id=COMPANY, source_type="manual", source_id=str(uuid4()),
            event="adjust", effective_at=date(2026, 7, 10),
            lines=(
                PostingLine(amount_cents=45_00, role=ROLE_AR, memo="hand entry"),
                PostingLine(amount_cents=-45_00, role=ROLE_OPENING_EQUITY, memo="hand entry"),
            ),
            created_by="t",
        ),
    )
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert out["drift"] == []
    assert out["unattributed_cents"] == 45_00
    assert out["unattributed_by_source"] == [{"source_type": "manual", "amount_cents": 45_00}]
    assert out["difference_cents"] == 45_00
    assert out["tied"] is False


def test_void_and_draft_owe_nothing(db):
    _issued(db, "100.00")
    voided = _issued(db, "70.00")
    transition_invoice_status(db, voided, "void", actor="t")
    voided.balance_due = Decimal("0")
    draft = _issued(db, "80.00", status="draft")

    out = reports.ar_control(db, COMPANY)
    assert out["subledger_cents"] == 100_00
    assert out["control_cents"] == 100_00
    assert out["tied"] is True

    # GDXA-1246: record_payment refuses only a void, so a draft can take a
    # payment. It posts a lone AR credit (no issuance behind it). The tie stays
    # red; the row says why and shows the balance the draft itself carries.
    _payment(db, recorded_on=draft, posted_against=draft, amount="30.00")
    draft.balance_due = Decimal("50.00")
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert out["subledger_cents"] == 100_00
    assert out["control_cents"] == 70_00
    assert out["tied"] is False
    assert out["drift"] == [
        {
            "invoice_id": str(draft.id),
            "invoice_number": draft.invoice_number,
            "status": "draft",
            "deleted": False,
            "open_balance_cents": 0,
            "ledger_ar_cents": -30_00,
            "drift_cents": -30_00,
            "balance_due_cents": 50_00,
            "reason": "payment_on_draft",
        }
    ]


def test_ar_on_a_void_or_deleted_invoice_shows_its_balance_but_is_not_a_draft_payment(db):
    """The sweep of GDXA-1246's class: any row whose expected balance is
    zeroed by status or deletion. A void (balance_due 0 by void_invoice) and a
    soft-deleted issued invoice still carrying AR show their own balance_due;
    neither is labelled a draft payment."""
    voided = _issued(db, "70.00")
    _payment(db, recorded_on=voided, posted_against=voided, amount="20.00")
    transition_invoice_status(db, voided, "void", actor="t")
    voided.balance_due = Decimal("0")
    deleted = _issued(db, "40.00")
    deleted.deleted_at = deleted.created_at or date(2026, 7, 10)
    db.flush()

    out = reports.ar_control(db, COMPANY)
    rows = {r["invoice_id"]: r for r in out["drift"]}
    assert out["tied"] is False
    assert rows[str(voided.id)]["ledger_ar_cents"] == -20_00
    assert rows[str(voided.id)]["balance_due_cents"] == 0
    assert rows[str(deleted.id)]["ledger_ar_cents"] == 40_00
    assert rows[str(deleted.id)]["balance_due_cents"] == 40_00
    assert "reason" not in rows[str(voided.id)]
    assert "reason" not in rows[str(deleted.id)]


def test_a_deleted_draft_with_a_payment_shows_its_balance_but_no_reason(db):
    draft = _issued(db, "80.00", status="draft")
    _payment(db, recorded_on=draft, posted_against=draft, amount="30.00")
    draft.balance_due = Decimal("50.00")
    draft.deleted_at = date(2026, 7, 10)
    db.flush()

    (row,) = reports.ar_control(db, COMPANY)["drift"]
    assert row["deleted"] is True
    assert row["ledger_ar_cents"] == -30_00
    assert row["balance_due_cents"] == 50_00
    assert "reason" not in row


def test_a_live_drift_row_carries_no_balance_due_or_reason(db):
    inv = _issued(db, "100.00")
    inv.balance_due = Decimal("90.00")
    db.flush()

    (row,) = reports.ar_control(db, COMPANY)["drift"]
    assert "balance_due_cents" not in row
    assert "reason" not in row


def test_endpoint_returns_the_report(db):
    _issued(db, "100.00")
    out = report_ar_control(db=db, user={"tenant_id": COMPANY, "sub": "tester"}, _perm=None)
    assert out["ledger_posting_enabled"] is True
    assert out["control_cents"] == 100_00
    assert out["tied"] is True


# ---------------------------------------------------------------------------
# GDXA-1231: one rule, stated once. The per-invoice AR the tie attributes
# must be the per-invoice AR the void settlement and the cutover
# reconciliation read, line for line, including with AR on a deactivated
# account. The app cannot get there today (patch_account refuses to
# deactivate a system account, and the seed never re-adds a role an inactive
# account holds), so the test builds it by hand, as a direct DB edit would.
# ---------------------------------------------------------------------------


def test_receivable_statuses_are_the_issued_statuses_plus_overdue_only():
    assert set(RECEIVABLE_STATUSES) - set(ISSUED_STATUSES) == {"overdue"}
    assert set(ISSUED_STATUSES) <= set(RECEIVABLE_STATUSES)


def _memo(db, inv: Invoice, amount: str) -> InvoiceAdjustment:
    memo = InvoiceAdjustment(
        id=uuid4(), invoice_id=inv.id, kind="credit_memo", amount=Decimal(amount),
        reason="goodwill", created_by="t", company_id=COMPANY,
    )
    db.add(memo)
    db.flush()
    post_credit_memo(db, memo, inv, actor="t")
    db.flush()
    return memo


def _replace_ar_account(db) -> tuple[GlAccount, GlAccount]:
    (old,) = db.scalars(
        select(GlAccount).where(GlAccount.role == ROLE_AR, GlAccount.active.is_(True))
    ).all()
    old.active = False
    db.flush()
    new = GlAccount(
        code="1201", name="Accounts Receivable (new)", type="asset", role=ROLE_AR,
        is_system=True, company_id=COMPANY,
    )
    db.add(new)
    db.flush()
    return old, new


def _ledger_ar_by_invoice(db, invoices: list[Invoice]) -> dict[str, int]:
    """ar_control's per-invoice ledger AR, for every invoice: a sentinel
    balance drifts each row so the report names its ledger_ar_cents."""
    for inv in invoices:
        inv.balance_due = Decimal("999999.99")
    db.flush()
    out = reports.ar_control(db, COMPANY)
    assert out["drift_invoice_count"] == len(invoices)
    return {r["invoice_id"]: r["ledger_ar_cents"] for r in out["drift"]}


def test_ar_control_and_invoice_ar_balance_attribute_every_invoice_alike(db):
    a = _issued(db, "500.00")
    b = _issued(db, "300.00")
    _memo(db, a, "40.00")
    _payment(db, recorded_on=a, posted_against=a, amount="100.00")
    _payment(db, recorded_on=b, posted_against=b, amount="300.00")

    by_report = _ledger_ar_by_invoice(db, [a, b])
    assert by_report == {
        str(a.id): invoice_ar_balance_cents(db, a),
        str(b.id): invoice_ar_balance_cents(db, b),
    }
    assert by_report == {str(a.id): 360_00, str(b.id): 0}


def test_a_replaced_ar_account_still_counts_for_both_the_tie_and_the_invoice(db):
    # Decided (GDXA-1231): a receivable on a deactivated AR account is still
    # owed, so it counts. Both reads sum every AR-role account; before this,
    # invoice_ar_balance_cents read only the active one and lost A's $460 on
    # the old account.
    a = _issued(db, "500.00")
    _memo(db, a, "40.00")
    old, new = _replace_ar_account(db)
    _payment(db, recorded_on=a, posted_against=a, amount="100.00")  # lands on `new`
    b = _issued(db, "300.00")  # wholly on `new`
    _memo(db, b, "25.00")

    by_report = _ledger_ar_by_invoice(db, [a, b])
    assert by_report == {
        str(a.id): invoice_ar_balance_cents(db, a),
        str(b.id): invoice_ar_balance_cents(db, b),
    }
    assert by_report == {str(a.id): 360_00, str(b.id): 275_00}
    # The cutover reconciliation's control total sums the same lines.
    assert gl_ar_balance_cents(db, COMPANY) == reports.ar_control(db, COMPANY)["control_cents"]
    assert gl_ar_balance_cents(db, COMPANY) == 635_00
