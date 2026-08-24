"""Bulk Mark-Paid stops posting stale client-side balances (money-audit M32).

`BillingView` posted `amount: balance` from the row loaded into the browser,
possibly minutes earlier. User A records a $400 check; User B's stale tab bulk
Mark-Paids the $1,000 it still shows → $1,400 recorded against a $1,000
invoice, clamped invisible. The audit's prescription — "a server-side pay
remaining mode that takes no amount … removes the client from the arithmetic
entirely" — is what this pins: `pay_remaining: true` derives
total − credits − live payments inside the transaction.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, ensure_audit_table
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Payment

TENANT = "tenant-m32"
OFFICE = {"id": "office-user", "email": "office@example.com", "role": "admin"}


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    ensure_audit_table(session)
    yield session
    session.close()
    engine.dispose()


def _invoice(db, total="1000.00"):
    inv = Invoice(
        id=uuid.uuid4(), customer_id=uuid.uuid4(), job_id=uuid.uuid4(),
        invoice_number=f"INV-M32-{uuid.uuid4().hex[:6]}", billing_type="standard",
        subtotal=Decimal(total), tax_amount=Decimal("0"), total=Decimal(total),
        balance_due=Decimal(total), status="sent",
        invoice_date=datetime.now(UTC).date(), public_token=uuid.uuid4().hex,
        company_id=TENANT,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(
        id=uuid.uuid4(), invoice_id=inv.id, description="Install",
        quantity=1, unit_price=Decimal(total), line_total=Decimal(total),
        company_id=TENANT,
    ))
    db.commit()
    db.refresh(inv)
    return inv


def _pay(db, inv, **kw):
    from unittest.mock import patch

    from gdx_dispatch.routers.invoices import PaymentCreateIn, record_payment

    with patch("gdx_dispatch.routers.invoices.enqueue_stale_intent_sweep"):
        return record_payment(
            invoice_id=inv.id,
            payload=PaymentCreateIn(method="check", date=date.today(), **kw),
            _=OFFICE, db=db,
        )


def test_the_stale_tab_cannot_overpay(db):
    """THE FIX, in the audit's own scenario. $400 lands first; the stale
    tab's bulk Mark-Paid then records $600 — not the $1,000 it displayed."""
    inv = _invoice(db)
    _pay(db, inv, amount=400.0)
    _pay(db, inv, pay_remaining=True)

    rows = db.execute(
        select(Payment).where(Payment.invoice_id == inv.id, Payment.voided_at.is_(None))
    ).scalars().all()
    assert sorted(float(p.amount) for p in rows) == [400.0, 600.0], (
        "the server must derive the remaining balance, not trust the browser"
    )


def test_credits_reduce_what_remains(db):
    """total − credit memos − payments: the same arithmetic the GL gate
    trusts, so pay_remaining can never mint the negative-AR overpayment the
    gate exists to stop."""
    from gdx_dispatch.models.tenant_models import InvoiceAdjustment

    inv = _invoice(db)
    db.add(InvoiceAdjustment(
        id=uuid.uuid4(), invoice_id=inv.id, company_id=TENANT,
        kind="credit_memo", amount=Decimal("250.00"), reason="goodwill",
        created_by="office-user",
    ))
    db.commit()
    _pay(db, inv, pay_remaining=True)

    row = db.execute(
        select(Payment).where(Payment.invoice_id == inv.id, Payment.voided_at.is_(None))
    ).scalar_one()
    assert float(row.amount) == 750.00


def test_nothing_remaining_is_a_409_not_a_zero_payment(db):
    from fastapi import HTTPException

    inv = _invoice(db)
    _pay(db, inv, amount=1000.0)
    with pytest.raises(HTTPException) as exc:
        _pay(db, inv, pay_remaining=True)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "nothing_remaining"


def test_both_ways_of_saying_how_much_is_a_contradiction():
    from gdx_dispatch.routers.invoices import PaymentCreateIn

    with pytest.raises(Exception, match="not both"):
        PaymentCreateIn(amount=100.0, pay_remaining=True, method="check")


def test_neither_is_not_a_silent_zero():
    from gdx_dispatch.routers.invoices import PaymentCreateIn

    with pytest.raises(Exception, match="amount is required"):
        PaymentCreateIn(method="check")


def test_the_plain_amount_path_is_unchanged(db):
    inv = _invoice(db)
    _pay(db, inv, amount=123.45)
    row = db.execute(select(Payment).where(Payment.invoice_id == inv.id)).scalar_one()
    assert float(row.amount) == 123.45


def test_the_trail_says_the_server_did_the_math(db):
    inv = _invoice(db)
    _pay(db, inv, pay_remaining=True)
    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "payment_recorded")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row is not None
    assert row.details.get("pay_remaining") is True


def test_the_bulk_ui_sends_the_mode_not_an_amount():
    """BillingView's bulk path must carry pay_remaining and NO amount — the
    client is out of the arithmetic. Source-pin; the server behavior is
    proven above, and the deletion counterfactual bites."""
    import pathlib

    src = pathlib.Path(
        "gdx_dispatch/frontend/src/views/BillingView.vue"
    ).read_text()
    i = src.index("bulk mark-paid")
    window = src[max(0, i - 700):i]
    assert "pay_remaining: true" in window
    assert "amount: balance" not in window
    # Review catch: the pin passed with the api.post commented out. The
    # request itself must be live in the same window.
    assert "await api.post(`/api/invoices/${inv.id}/payments`" in window

def test_qb_legacy_settled_invoice_cannot_be_recharged(db):
    """Review catch: an imported invoice settled off-book carries
    balance_due=0 with NO Payment rows — the sum-derivation would happily
    re-charge its full total. balance_due <= 0 is an unambiguous refusal."""
    from fastapi import HTTPException

    inv = _invoice(db)
    inv.balance_due = 0  # settled off-book; no Payment rows exist
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _pay(db, inv, pay_remaining=True)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "nothing_remaining"


def test_the_derivation_locks_the_invoice_row():
    """Review catch: the first draft claimed "cannot overpay by construction"
    over a READ COMMITTED read. The invoice row must be locked for the
    derivation. Source-pin ordered before the derivation; the deletion
    counterfactual bites."""
    import pathlib as _p

    src = _p.Path(
        __import__("gdx_dispatch.routers.invoices", fromlist=["__file__"]).__file__
    ).read_text()
    i = src.index("if payload.pay_remaining:")
    j = src.index("_remaining = _remaining_receivable(invoice, db)", i)
    assert "with_for_update()" in src[i:j]


def test_one_arithmetic_serves_gate_and_derivation():
    """The M8 lesson: duplicated money arithmetic drifts. Both consumers must
    call the shared helper."""
    import pathlib as _p

    src = _p.Path(
        __import__("gdx_dispatch.routers.invoices", fromlist=["__file__"]).__file__
    ).read_text()
    assert src.count("_remaining_receivable(invoice, db)") >= 2


def test_ui_compat_forwards_the_mode(db):
    """Review catch: the compat shim's extra="allow" silently swallowed
    pay_remaining — {amount, pay_remaining:true} recorded the stale amount
    and audited pay_remaining:false. Forwarded, the core XOR now rejects the
    contradiction loudly."""
    import pathlib as _p

    src = _p.Path(
        __import__("gdx_dispatch.routers.ui_compat", fromlist=["__file__"]).__file__
    ).read_text()
    i = src.index("body = _PaymentCreateIn(")
    assert '"pay_remaining": True' in src[i:i+600]

