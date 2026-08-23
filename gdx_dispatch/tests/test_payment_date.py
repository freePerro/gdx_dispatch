"""Payment-date recording (2026-07-30 plan) — backdated payments carry their
own day into paid_at, future dates are rejected, and the QB money-pull pause
blocks the payment/invoice back-flow during the QB phase-out.

Why paid_at threads through record_payment instead of MAX(payment_date):
invoices can settle to paid with ZERO payments (fully-credited via credit
memo), and an old partial payment must not backdate a much later settlement.
Only the payment that flips the invoice in the same request may carry the
stamp. The audit that caught the MAX() design lives in
docs/design/payment-date-recording-plan.md.
"""
from __future__ import annotations

import datetime as dt
import secrets
from datetime import UTC, date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine
from gdx_dispatch.modules.ledger.service import ensure_gl_seed
from gdx_dispatch.routers.invoices import (
    CreditMemoIn,
    PaymentCreateIn,
    issue_credit_memo,
    record_payment,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "tester"}


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    ensure_gl_seed(tenant_db, COMPANY)
    tenant_db.commit()
    return tenant_db


def _invoice(db, total="1000.00", status="sent"):
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status=status,
        subtotal=Decimal(total),
        tax_amount=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(total),
        invoice_date=dt.date(2026, 7, 1),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=inv.id, description="Work", quantity=1,
            unit_price=Decimal(total), line_total=Decimal(total), company_id=COMPANY,
        )
    )
    db.commit()
    db.refresh(inv)
    return inv


def _pay(db, inv, amount, **kw):
    return record_payment(inv.id, PaymentCreateIn(amount=amount, method="cash", **kw), _=USER, db=db)


# ─── paid_at threading ──────────────────────────────────────────────────


def test_backdated_zeroing_payment_stamps_paid_at_from_payment_date(db):
    """A May check recorded in July must read as paid in May — otherwise the
    QB-era backfill lands the whole catch-up in the current month's KPI."""
    inv = _invoice(db)
    backdate = date.today() - timedelta(days=400)  # reaches into last year

    _pay(db, inv, 1000.0, date=backdate)
    db.refresh(inv)

    assert inv.status == "paid"
    assert inv.paid_at is not None
    # Date-only stamp: the day is the payment's day, the time is midnight —
    # the "day known, minute not" convention QB sync writes and
    # formatStampDate renders.
    assert inv.paid_at.date() == backdate
    assert inv.paid_at.time() == dt.time.min


def test_same_day_zeroing_payment_keeps_precise_timestamp(db):
    inv = _invoice(db)

    before = dt.datetime.now(UTC).replace(tzinfo=None)
    _pay(db, inv, 1000.0, date=date.today())
    db.refresh(inv)

    assert inv.status == "paid"
    assert inv.paid_at is not None
    # Same-day payments keep the real now() the recalc set — not a
    # midnight stamp (unless the test itself runs at exactly 00:00 UTC,
    # in which case the date assertion still holds).
    paid_at = inv.paid_at.replace(tzinfo=None)
    assert paid_at >= before - timedelta(seconds=5)


def test_partial_backdated_payment_leaves_paid_at_unset(db):
    inv = _invoice(db)

    _pay(db, inv, 250.0, date=date.today() - timedelta(days=90))
    db.refresh(inv)

    assert inv.status != "paid"
    assert inv.paid_at is None


def test_backdated_payment_on_paid_invoice_does_not_move_paid_at(db):
    """Only the payment that FLIPS the invoice carries paid_at. A later
    (over)payment — even backdated — must not rewrite history."""
    inv = _invoice(db)
    _pay(db, inv, 1000.0, date=date.today())
    db.refresh(inv)
    original_paid_at = inv.paid_at
    assert original_paid_at is not None

    _pay(db, inv, 50.0, date=date.today() - timedelta(days=200))
    db.refresh(inv)

    assert inv.paid_at == original_paid_at


def test_fully_credited_settlement_still_stamps_paid_at_now(db):
    """The zero-payments settlement path (credit memo forgives the whole
    balance) is untouched: paid_at is the settlement moment, never NULL and
    never derived from payments that don't exist."""
    inv = _invoice(db)

    issue_credit_memo(str(inv.id), CreditMemoIn(amount=1000.0, reason="warranty"), db=db, _=USER)
    db.refresh(inv)

    assert inv.status == "paid"
    assert inv.paid_at is not None
    assert inv.paid_at.date() == dt.datetime.now(UTC).date()


# ─── future-date guard ──────────────────────────────────────────────────


def test_payment_date_accepts_past_including_last_year(db):
    # 2025 corrections are the point of the feature — no lower bound.
    payload = PaymentCreateIn(amount=10.0, method="check", date=date(2025, 3, 14))
    assert payload.date == date(2025, 3, 14)


def test_payment_date_accepts_today():
    payload = PaymentCreateIn(amount=10.0, method="check", date=dt.datetime.now(UTC).date())
    assert payload.date == dt.datetime.now(UTC).date()


def test_payment_date_rejects_tomorrow():
    # No forward slack: the company zone (America/Chicago) is behind UTC,
    # so a legitimate client stamp can never exceed the UTC day. Tomorrow
    # is a post-dated check, not received cash.
    with pytest.raises(ValidationError):
        PaymentCreateIn(amount=10.0, method="check", date=dt.datetime.now(UTC).date() + timedelta(days=1))


# ─── QB money-pull pause ────────────────────────────────────────────────
# Real table, real rows — no monkeypatched reader. The audit caught the
# first version passing "open by default" only because the column didn't
# exist and the error was swallowed; these tests exercise the actual
# schema surface migration 049 + the control-model fix provide.


def _create_settings_table(db):
    from gdx_dispatch.control.models import TenantSettings

    TenantSettings.__table__.create(bind=db.get_bind(), checkfirst=True)


def _seed_settings(db, paused: bool):
    from sqlalchemy import text as _sql

    _create_settings_table(db)
    db.execute(
        _sql(
            "INSERT INTO tenant_settings (tenant_id, qb_money_pull_paused) "
            "VALUES (:tid, :p)"
        ),
        {"tid": COMPANY, "p": paused},
    )
    db.commit()


def test_money_pull_gate_open_with_no_settings_row(db):
    from gdx_dispatch.modules.quickbooks.sync import _assert_money_pull_allowed

    _create_settings_table(db)
    _assert_money_pull_allowed(COMPANY, db, "payment")  # no row → allowed


def test_money_pull_gate_open_when_flag_off(db):
    from gdx_dispatch.modules.quickbooks.sync import _assert_money_pull_allowed

    _seed_settings(db, paused=False)
    _assert_money_pull_allowed(COMPANY, db, "payment")


def test_money_pull_gate_blocks_when_paused(db):
    from gdx_dispatch.modules.quickbooks.sync import (
        QBPullDisabledError,
        _assert_money_pull_allowed,
    )

    _seed_settings(db, paused=True)
    with pytest.raises(QBPullDisabledError, match="phase-out"):
        _assert_money_pull_allowed(COMPANY, db, "payment")
    with pytest.raises(QBPullDisabledError, match="phase-out"):
        _assert_money_pull_allowed(COMPANY, db, "invoice")


def test_pause_reader_missing_schema_reads_open(db):
    # A schema that predates 049 cannot have the flag ON — the reader must
    # treat "no such table/column" as open, not as a read failure.
    from gdx_dispatch.core.settings_flags import qb_money_pull_paused

    assert qb_money_pull_paused(COMPANY, db) is False  # table not created here


def test_pause_reader_fails_closed_on_unexpected_error(db):
    # Any OTHER read failure pauses the pull: a blocked sync is a retry; an
    # overwritten payment date is not.
    from gdx_dispatch.core.settings_flags import qb_money_pull_paused

    class _BrokenSession:
        def execute(self, *a, **kw):
            raise RuntimeError("connection reset")

    assert qb_money_pull_paused(COMPANY, _BrokenSession()) is True


# ─── /api/workflow/flags round trip ─────────────────────────────────────


def test_workflow_flags_round_trip_includes_pause(db):
    """GET seeds the row and returns all 9 flags; PATCH persists the pause.
    First real coverage of this endpoint — the control-model drift (047,
    then 049) kept the column out of every create_all schema until now."""
    from types import SimpleNamespace

    from gdx_dispatch.control.models import Tenant, TenantSettings
    from gdx_dispatch.modules.workflow.router import WorkflowFlags, get_flags, update_flags

    Tenant.__table__.create(bind=db.get_bind(), checkfirst=True)
    TenantSettings.__table__.create(bind=db.get_bind(), checkfirst=True)
    request = SimpleNamespace(state=SimpleNamespace(tenant={"id": COMPANY}))
    admin = {"role": "admin", "tenant_id": COMPANY, "sub": "tester"}

    flags = get_flags(request=request, user=admin, db=db)
    assert flags["qb_money_pull_paused"] is False

    updated = update_flags(
        payload=WorkflowFlags(qb_money_pull_paused=True),
        request=request,
        user=admin,
        db=db,
    )
    assert updated["qb_money_pull_paused"] is True

    # And the sync gate actually respects what the endpoint wrote.
    from gdx_dispatch.modules.quickbooks.sync import (
        QBPullDisabledError,
        _assert_money_pull_allowed,
    )

    with pytest.raises(QBPullDisabledError, match="phase-out"):
        _assert_money_pull_allowed(COMPANY, db, "payment")
