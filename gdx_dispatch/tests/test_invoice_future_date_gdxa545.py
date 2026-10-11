"""GDXA-545: an invoice cannot be dated in the future.

Create and PATCH both took any ``invoice_date`` — 2099-01-01 included — and
create went on to derive the due date from it. Forward-dating books revenue
in a period that hasn't happened, so both now refuse a date past the SHOP's
calendar day with the same 422 wording the payment schema uses. Today, the
past, and no date at all are unchanged.
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from freezegun import freeze_time
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import AppSettings, Invoice, Job
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    InvoicePatchIn,
    create_invoice,
    patch_invoice,
)

# 03:30 UTC on 14 Oct is still the 13th in America/Chicago: the shop's today
# is the 13th, the UTC today the 14th.
_UTC_EARLY = "2026-10-14 03:30:00"
_SHOP_TODAY = date(2026, 10, 13)


def _user():
    return {"sub": "u-1", "user_id": "u-1", "role": "admin", "tenant_id": "tenant-test"}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    sess = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    sess.add(AppSettings(timezone="America/Chicago"))
    sess.commit()
    yield sess
    sess.close()
    engine.dispose()


def _job(db) -> Job:
    job = Job(
        customer_id=uuid4(), title="Install", description="",
        lifecycle_stage="estimate", dispatch_status="unassigned",
        billing_status="unbilled", company_id="tenant-test",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _create(db, job, **fields) -> dict:
    return create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id, customer_id=job.customer_id,
            line_items=[InvoiceLineCreateIn(description="Spring", quantity=1, unit_price=200.0)],
            **fields,
        ),
        _=_user(), db=db,
    )


def _invoice_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Invoice))


def test_create_refuses_a_future_invoice_date_and_writes_nothing(db):
    job = _job(db)
    with pytest.raises(HTTPException) as exc:
        _create(db, job, invoice_date=date(2099, 1, 1))
    assert exc.value.status_code == 422
    assert exc.value.detail == "invoice date cannot be in the future"
    db.rollback()
    assert _invoice_count(db) == 0


def test_create_refuses_tomorrow_on_the_shop_calendar(db):
    # The UTC day is already the 14th; the shop's is the 13th. The 14th is
    # tomorrow for the shop, and a UTC comparison would have let it through.
    job = _job(db)
    with freeze_time(_UTC_EARLY), pytest.raises(HTTPException) as exc:
        _create(db, job, invoice_date=_SHOP_TODAY + timedelta(days=1))
    assert exc.value.status_code == 422


def test_create_accepts_the_shop_today_and_a_backdate(db):
    with freeze_time(_UTC_EARLY):
        today = _create(db, _job(db), invoice_date=_SHOP_TODAY)
        past = _create(db, _job(db), invoice_date=date(2025, 12, 31))
        unset = _create(db, _job(db))
    assert today["invoice_date"] == _SHOP_TODAY.isoformat()
    assert past["invoice_date"] == "2025-12-31"
    assert unset["invoice_date"] == _SHOP_TODAY.isoformat()


def test_patch_refuses_a_future_invoice_date_and_changes_nothing(db):
    job = _job(db)
    with freeze_time(_UTC_EARLY):
        inv = _create(db, job, invoice_date=_SHOP_TODAY)
        audits_before = db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "patch_invoice")
        )
        with pytest.raises(HTTPException) as exc:
            patch_invoice(
                invoice_id=UUID(inv["id"]),
                payload=InvoicePatchIn(
                    invoice_date=_SHOP_TODAY + timedelta(days=1), notes="should not land",
                ),
                current_user=_user(), db=db,
            )
    assert exc.value.status_code == 422
    assert exc.value.detail == "invoice date cannot be in the future"
    db.rollback()
    db.expire_all()
    row = db.get(Invoice, UUID(inv["id"]))
    assert row.invoice_date == _SHOP_TODAY
    assert row.notes != "should not land"
    audits_after = db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "patch_invoice")
    )
    assert audits_after == audits_before


def test_patch_accepts_today_a_backdate_and_a_clear(db):
    job = _job(db)
    with freeze_time(_UTC_EARLY):
        inv = _create(db, job, invoice_date=date(2026, 9, 1))
        for value in (_SHOP_TODAY, date(2026, 1, 15), None):
            out = patch_invoice(
                invoice_id=UUID(inv["id"]), payload=InvoicePatchIn(invoice_date=value),
                current_user=_user(), db=db,
            )
            assert out["invoice_date"] == (value.isoformat() if value else None)


def test_patch_sending_back_an_unchanged_future_date_still_saves(db):
    # The detail screen sends the stored invoice_date on every header save.
    # A draft already dated ahead (written before this check; none on prod)
    # must still take a notes edit: only a date being changed is checked.
    job = _job(db)
    inv = _create(db, job, invoice_date=date(2026, 1, 15))
    db.get(Invoice, UUID(inv["id"])).invoice_date = date(2099, 1, 1)
    db.commit()
    out = patch_invoice(
        invoice_id=UUID(inv["id"]),
        payload=InvoicePatchIn(invoice_date=date(2099, 1, 1), notes="ok"),
        current_user=_user(), db=db,
    )
    assert out["notes"] == "ok"
    assert out["invoice_date"] == "2099-01-01"
    # Moving it to a different future date is still a change, and refused.
    with pytest.raises(HTTPException) as exc:
        patch_invoice(
            invoice_id=UUID(inv["id"]), payload=InvoicePatchIn(invoice_date=date(2099, 2, 1)),
            current_user=_user(), db=db,
        )
    assert exc.value.status_code == 422
