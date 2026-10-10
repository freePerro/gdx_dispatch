"""GDXA-449: invoice PATCH refuses a stale edit screen.

Two office users open the same draft. Both screens loaded version 1. The
first save lands and moves the row to version 2; the second, still carrying
1, used to overwrite the first user's notes and dates without a trace. It now
gets 409 ``invoice_version_conflict`` and writes nothing. A PATCH that carries no
version is unchecked, as before.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    InvoiceLinePatchIn,
    InvoicePatchIn,
    add_invoice_line,
    create_invoice,
    delete_invoice_line,
    patch_invoice,
    patch_invoice_line,
)


def _user():
    return {"sub": "u-1", "user_id": "u-1", "role": "admin", "tenant_id": "tenant-test"}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = Session()
    yield sess
    sess.close()
    engine.dispose()


def _draft(db) -> dict:
    job = Job(
        customer_id=uuid4(),
        title="Install",
        description="",
        lifecycle_stage="estimate",
        dispatch_status="unassigned",
        billing_status="unbilled",
        company_id="tenant-test",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            line_items=[InvoiceLineCreateIn(description="Spring", quantity=1, unit_price=200.0)],
            invoice_date=date.today(),
        ),
        _=_user(),
        db=db,
    )


def _patch(db, inv_id: str, **fields) -> dict:
    return patch_invoice(
        invoice_id=UUID(inv_id), payload=InvoicePatchIn(**fields), current_user=_user(), db=db,
    )


def test_serialized_invoice_carries_its_version(db):
    inv = _draft(db)
    assert inv["version"] == 1


def test_second_stale_patch_is_refused_and_writes_nothing(db):
    inv = _draft(db)
    loaded = inv["version"]

    first = _patch(db, inv["id"], notes="from A", expected_version=loaded)
    assert first["version"] == loaded + 1
    audits_before = db.execute(select(AuditLog).where(AuditLog.action == "patch_invoice")).scalars().all()

    with pytest.raises(HTTPException) as exc:
        _patch(db, inv["id"], notes="from B", due_date=date(2030, 1, 1), expected_version=loaded)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "invoice_version_conflict"
    assert exc.value.detail["current_version"] == loaded + 1

    db.expire_all()
    row = db.get(Invoice, UUID(inv["id"]))
    assert row.notes == "from A"
    assert row.due_date != date(2030, 1, 1)
    assert row.version == loaded + 1
    audits_after = db.execute(select(AuditLog).where(AuditLog.action == "patch_invoice")).scalars().all()
    assert len(audits_after) == len(audits_before)


def test_successful_patch_audit_row_records_the_new_version(db):
    inv = _draft(db)
    out = _patch(db, inv["id"], notes="checked", expected_version=inv["version"])
    row = db.execute(
        select(AuditLog).where(AuditLog.action == "patch_invoice", AuditLog.entity_id == inv["id"])
    ).scalars().one()
    assert row.user_id == "u-1"
    assert row.details["version"] == out["version"]


def test_patch_without_a_version_is_unchecked(db):
    inv = _draft(db)
    _patch(db, inv["id"], notes="first")
    out = _patch(db, inv["id"], notes="second")
    assert out["notes"] == "second"


def test_fresh_version_after_a_save_is_accepted(db):
    inv = _draft(db)
    first = _patch(db, inv["id"], notes="one", expected_version=inv["version"])
    second = _patch(db, inv["id"], notes="two", expected_version=first["version"])
    assert second["notes"] == "two"
    assert second["version"] == first["version"] + 1


def test_a_line_write_moves_the_invoice_version(db):
    """Why InvoiceDetailView sends the header PATCH before its line writes:
    every line write bumps the invoice's version, so a header PATCH sent
    after the lines would be refused by the same save."""
    inv = _draft(db)
    line_id = db.execute(
        select(InvoiceLine.id).where(InvoiceLine.invoice_id == UUID(inv["id"]))
    ).scalar_one()
    patch_invoice_line(
        invoice_id=UUID(inv["id"]),
        line_id=line_id,
        payload=InvoiceLinePatchIn(unit_price=250.0),
        user=_user(),
        db=db,
    )
    db.expire_all()
    assert db.get(Invoice, UUID(inv["id"])).version == inv["version"] + 1
    with pytest.raises(HTTPException) as exc:
        _patch(db, inv["id"], notes="late", expected_version=inv["version"])
    assert exc.value.status_code == 409


def test_expected_version_must_be_positive():
    with pytest.raises(ValueError):
        InvoicePatchIn(expected_version=0)


def test_line_writes_report_the_version_they_left(db):
    """InvoiceDetailView follows the version write by write, so the retry of a
    save that failed halfway carries the token its own writes produced."""
    inv = _draft(db)
    inv_id = UUID(inv["id"])
    line_id = db.execute(
        select(InvoiceLine.id).where(InvoiceLine.invoice_id == inv_id)
    ).scalar_one()

    def row_version():
        db.expire_all()
        return db.get(Invoice, inv_id).version

    patched = patch_invoice_line(
        invoice_id=inv_id, line_id=line_id, payload=InvoiceLinePatchIn(unit_price=210.0),
        user=_user(), db=db,
    )
    assert patched["invoice_version"] == row_version()
    added = add_invoice_line(
        invoice_id=inv_id,
        payload=InvoiceLineCreateIn(description="Cable", quantity=1, unit_price=40.0),
        current_user=_user(), db=db,
    )
    assert added["invoice_version"] == row_version()
    deleted = delete_invoice_line(
        invoice_id=inv_id, line_id=UUID(added["id"]), user=_user(), db=db,
    )
    assert deleted["invoice"]["version"] == row_version()
    out = _patch(db, inv["id"], notes="retry", expected_version=row_version())
    assert out["notes"] == "retry"


def test_a_line_edit_that_leaves_the_totals_alone_still_moves_the_version(db):
    """A description-only line edit does not touch the invoices row's totals.
    The version must move anyway, or a stale screen passes the header check
    and its line writes overwrite the colleague's text."""
    inv = _draft(db)
    line_id = db.execute(
        select(InvoiceLine.id).where(InvoiceLine.invoice_id == UUID(inv["id"]))
    ).scalar_one()
    out = patch_invoice_line(
        invoice_id=UUID(inv["id"]),
        line_id=line_id,
        payload=InvoiceLinePatchIn(description="Torsion spring, colleague's wording"),
        user=_user(),
        db=db,
    )
    assert out["invoice_version"] == inv["version"] + 1
    with pytest.raises(HTTPException) as exc:
        _patch(db, inv["id"], notes="stale", expected_version=inv["version"])
    assert exc.value.status_code == 409


def test_a_no_change_header_patch_still_claims_the_invoice(db):
    """A line-only save sends the header unchanged, then writes lines. If that
    no-op header PATCH left the version alone, a second screen holding the
    same version would pass too and its line loop would overwrite ours."""
    inv = _draft(db)
    loaded = inv["version"]
    line_id = db.execute(
        select(InvoiceLine.id).where(InvoiceLine.invoice_id == UUID(inv["id"]))
    ).scalar_one()

    # Screen A: unchanged header (notes were already empty), then its line.
    a = _patch(db, inv["id"], notes=None, expected_version=loaded)
    assert a["version"] == loaded + 1
    patch_invoice_line(
        invoice_id=UUID(inv["id"]),
        line_id=line_id,
        payload=InvoiceLinePatchIn(description="from A"),
        user=_user(),
        db=db,
    )

    # Screen B loaded the same version and also changes only a line.
    with pytest.raises(HTTPException) as exc:
        _patch(db, inv["id"], notes=None, expected_version=loaded)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "invoice_version_conflict"
    db.expire_all()
    assert db.get(InvoiceLine, line_id).description == "from A"
