"""The §11 delivery rail (2026-08-08 audit).

Before this rail, `verified_at` was enforced on exactly two MOBILE
endpoints while every desktop path delivered unverified drafts: one-click
send, bulk send, email-compose (which also composed VOIDED invoices,
minting pay tokens), Mark-as-Mailed (which fed drafts into the dunning
population), pay-link, send-receipt, and manual send-reminder. The public
/pay page rendered and charged DRAFTS outright.

The rule, pinned here: a DRAFT may not be delivered or paid until a human
verified it. Invoices already past draft cleared the gate at issue time —
re-sends and reminders on issued invoices are unaffected (no backfill
needed for pre-rail rows with verified_at NULL).
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.invoice_delivery import draft_needs_verification
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, JobPartNeeded, Payment
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    create_invoice,
    get_invoice_pay_link,
    invoice_email_compose,
    mark_invoice_sent,
    send_invoice,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        JobPartNeeded.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


USER = {"user_id": "user-1", "tenant_id": "tenant-1", "role": "admin"}


def _draft(db, *, total: float = 250.0) -> Invoice:
    job = Job(
        customer_id=uuid4(), title="Job", lifecycle_stage="completed",
        company_id="tenant-1",
    )
    db.add(job)
    db.commit()
    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id, customer_id=job.customer_id,
            line_items=[{"description": "Labor", "quantity": 1, "unit_price": total}],
        ),
        _=USER, db=db,
    )
    return db.get(Invoice, UUID(created["id"]))


def _assert_awaiting(excinfo) -> None:
    assert excinfo.value.status_code == 409
    detail = excinfo.value.detail
    assert (detail.get("code") if isinstance(detail, dict) else detail) == "awaiting_verification"


# ---------------------------------------------------------------------------
# Delivery endpoints refuse unverified drafts; verified drafts pass.
# ---------------------------------------------------------------------------


def test_send_refuses_unverified_draft_then_allows_after_verify(db) -> None:
    inv = _draft(db)
    with pytest.raises(HTTPException) as exc:
        send_invoice(invoice_id=inv.id, _=USER, db=db)
    _assert_awaiting(exc)
    db.refresh(inv)
    assert inv.status == "draft", "a blocked send must not have flipped status"

    inv.verified_at = datetime.now(UTC)
    db.commit()
    out = send_invoice(invoice_id=inv.id, _=USER, db=db)
    assert out["status"] == "sent"


def test_mark_sent_refuses_unverified_draft(db) -> None:
    inv = _draft(db)
    with pytest.raises(HTTPException) as exc:
        mark_invoice_sent(invoice_id=inv.id, _=USER, db=db)
    _assert_awaiting(exc)
    db.refresh(inv)
    assert inv.status == "draft"
    assert inv.sent_via is None


def test_email_compose_refuses_unverified_draft_and_void(db) -> None:
    inv = _draft(db)
    with pytest.raises(HTTPException) as exc:
        invoice_email_compose(invoice_id=inv.id, _=USER, db=db)
    _assert_awaiting(exc)

    inv.status = "void"
    inv.verified_at = datetime.now(UTC)
    db.commit()
    with pytest.raises(HTTPException) as exc2:
        invoice_email_compose(invoice_id=inv.id, _=USER, db=db)
    assert exc2.value.status_code == 409  # void — cannot be emailed at all


def test_pay_link_refuses_unverified_draft(db) -> None:
    inv = _draft(db)
    with pytest.raises(HTTPException) as exc:
        get_invoice_pay_link(invoice_id=inv.id, _=USER, db=db)
    _assert_awaiting(exc)


def test_issued_invoices_unaffected_without_verification_backfill(db) -> None:
    """Pre-rail rows: status already past draft, verified_at NULL — re-send
    must keep working (thousands of historical invoices)."""
    inv = _draft(db)
    inv.status = "sent"  # issued pre-rail
    db.commit()
    assert draft_needs_verification(inv) is False
    out = send_invoice(invoice_id=inv.id, _=USER, db=db)  # re-send
    assert out["status"] == "sent"


# ---------------------------------------------------------------------------
# Public /pay refuses drafts (render + resolve), with a non-revealing 404.
# ---------------------------------------------------------------------------


def test_public_pay_resolve_refuses_draft_with_404(db) -> None:
    from gdx_dispatch.core.payments import _resolve_public_invoice

    inv = _draft(db)
    inv.public_token = f"tok-{uuid4().hex}"
    inv.balance_due = Decimal("250.00")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _resolve_public_invoice(
            db, invoice_token=inv.public_token, invoice_id=None, op="create-intent"
        )
    assert exc.value.status_code == 404, "draft must read as not-found, never as payable"

    inv.status = "sent"
    db.commit()
    resolved = _resolve_public_invoice(
        db, invoice_token=inv.public_token, invoice_id=None, op="create-intent"
    )
    assert resolved.id == inv.id


# ---------------------------------------------------------------------------
# Manual reminders: issued unpaid invoices only.
# ---------------------------------------------------------------------------


def test_send_reminder_refuses_draft_and_paid(db) -> None:
    from types import SimpleNamespace

    from gdx_dispatch.routers.invoice_reminders import SendReminderIn, send_reminder

    req = SimpleNamespace(state=SimpleNamespace(tenant={"id": "tenant-1"}), client=None)
    inv = _draft(db)
    with pytest.raises(HTTPException) as exc:
        send_reminder(
            invoice_id=inv.id, request=req,
            payload=SendReminderIn(channel="email"), user=USER, db=db,
        )
    _assert_awaiting(exc)

    inv.status = "paid"
    inv.verified_at = datetime.now(UTC)
    db.commit()
    with pytest.raises(HTTPException) as exc2:
        send_reminder(
            invoice_id=inv.id, request=req,
            payload=SendReminderIn(channel="email"), user=USER, db=db,
        )
    assert exc2.value.status_code == 409
