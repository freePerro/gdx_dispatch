"""core/invoice_void.py — the one void, callable without HTTP (GDXA-1848).

GDXA-1847: the MCP `invoices.void` tool returned ``{"voided": ...}`` and changed
nothing, and its mock-only test passed on a handler that voided nothing. So
these call the shared function for real against a database and read back the
rows that LAND: the status, the balance, the released claim and the audit row.
Only the Celery enqueue is patched; it is the boundary to the broker.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, ensure_audit_table
from gdx_dispatch.core.invoice_void import InvoiceVoidRefused, void_invoice
from gdx_dispatch.models.tenant_models import Invoice, JobPartNeeded, Payment

TENANT = "tenant-void-shared"


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


@pytest.fixture
def invoice(db):
    inv = Invoice(
        id=uuid.uuid4(), customer_id=uuid.uuid4(), job_id=uuid.uuid4(),
        invoice_number="INV-VOID-SHARED", billing_type="standard",
        subtotal=Decimal("400.00"), tax_amount=Decimal("0"), total=Decimal("400.00"),
        balance_due=Decimal("400.00"), status="sent",
        invoice_date=datetime.now(UTC).date(), public_token=uuid.uuid4().hex,
        company_id=TENANT,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _voided_rows(db, invoice_id):
    return db.query(AuditLog).filter(
        AuditLog.action == "invoice_voided", AuditLog.entity_id == str(invoice_id)
    ).all()


def test_the_void_lands_with_its_actor_reason_and_release(db, invoice):
    part = JobPartNeeded(
        id=str(uuid.uuid4()), company_id=TENANT, job_id=str(invoice.job_id), part_name="Torsion spring",
        billed_invoice_id=invoice.id,
    )
    db.add(part)
    db.commit()

    with patch("gdx_dispatch.core.invoice_void.enqueue_stale_intent_sweep") as sweep:
        result = void_invoice(db, invoice.id, actor="office-user", reason="duplicate bill")

    assert result.already_void is False
    assert result.released_parts == 1
    sweep.assert_called_once()
    assert sweep.call_args[1] == {"why": "invoice_voided", "settled": True}

    db.expire_all()
    row = db.get(Invoice, invoice.id)
    assert row.status == "void"
    assert Decimal(str(row.balance_due)) == Decimal("0.00")
    assert db.get(JobPartNeeded, part.id).billed_invoice_id is None, (
        "the claimed part goes back to the unbilled checklist"
    )
    audits = _voided_rows(db, invoice.id)
    assert len(audits) == 1, "the void must leave its trail in the database"
    assert audits[0].user_id == "office-user", "a money mutation names its actor"
    assert audits[0].details["reason"] == "duplicate bill"
    assert audits[0].details["released_parts"] == 1


def test_a_live_payment_refuses_and_writes_nothing(db, invoice):
    db.add(Payment(
        invoice_id=invoice.id, amount=Decimal("50.00"), method="check", company_id=TENANT,
    ))
    db.commit()

    with patch("gdx_dispatch.core.invoice_void.enqueue_stale_intent_sweep") as sweep, \
            pytest.raises(InvoiceVoidRefused) as exc:
        void_invoice(db, invoice.id, actor="office-user")

    assert exc.value.status_code == 409
    sweep.assert_not_called()
    db.rollback()
    assert db.get(Invoice, invoice.id).status == "sent"
    assert _voided_rows(db, invoice.id) == []


def test_an_already_void_invoice_is_returned_untouched(db, invoice):
    with patch("gdx_dispatch.core.invoice_void.enqueue_stale_intent_sweep"):
        void_invoice(db, invoice.id, actor="office-user")
        again = void_invoice(db, invoice.id, actor="office-user")
    assert again.already_void is True
    assert again.invoice.status == "void"
    assert len(_voided_rows(db, invoice.id)) == 1, "a second void writes no second trail"


def test_a_missing_invoice_is_a_404(db):
    with pytest.raises(InvoiceVoidRefused) as exc:
        void_invoice(db, uuid.uuid4(), actor="office-user")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("actor", ["", "   ", None])
def test_an_anonymous_void_is_refused_before_anything_is_read(db, invoice, actor):
    with pytest.raises(ValueError):
        void_invoice(db, invoice.id, actor=actor)
    assert db.get(Invoice, invoice.id).status == "sent"


def test_a_string_id_voids_like_a_uuid(db, invoice):
    """The MCP tool hands over the id as a string; the route hands a UUID."""
    with patch("gdx_dispatch.core.invoice_void.enqueue_stale_intent_sweep"):
        result = void_invoice(db, str(invoice.id), actor="office-user")
    assert result.invoice.status == "void"
    assert len(_voided_rows(db, invoice.id)) == 1


def test_an_unparseable_id_is_a_404(db):
    with pytest.raises(InvoiceVoidRefused) as exc:
        void_invoice(db, "not-a-uuid", actor="office-user")
    assert exc.value.status_code == 404


def test_a_locked_period_refuses_and_leaves_the_session_clean(db, invoice):
    """The status flip happens before the ledger step can refuse. A caller that
    keeps its session and commits later must not land half a void."""
    from gdx_dispatch.modules.ledger.engine import PeriodLockedError

    with patch("gdx_dispatch.core.invoice_void.reverse_invoice_adjustments",
               side_effect=PeriodLockedError("2026-09 is closed")), \
            patch("gdx_dispatch.core.invoice_void.enqueue_stale_intent_sweep") as sweep, \
            pytest.raises(InvoiceVoidRefused) as exc:
        void_invoice(db, invoice.id, actor="office-user")

    assert exc.value.status_code == 409
    sweep.assert_not_called()
    db.commit()  # the caller carries on with its session
    db.expire_all()
    assert db.get(Invoice, invoice.id).status == "sent"
    assert _voided_rows(db, invoice.id) == []
