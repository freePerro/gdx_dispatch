"""Invoice numbering — ONE generator (2026-08-08 audit).

There were FOUR generators: max-based (core/closeout_billing), count-based
(routers/invoices — reissued taken numbers whenever count and max diverged:
deleted rows, hex-format historical numbers), and two hex schemes on dead
endpoints. All live paths now delegate to core/closeout_billing's
high-water-mark generator; concurrent collisions retry once at the office
create path instead of raising a raw 500.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.closeout_billing import next_invoice_number
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, JobPartNeeded, Payment
from gdx_dispatch.routers.invoices import InvoiceCreateIn, create_invoice


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


def _seed_inv(db, number: str) -> Invoice:
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=number,
        subtotal=Decimal("10"), tax_amount=0, total=Decimal("10"),
        balance_due=Decimal("10"),
        status="draft",
        public_token=f"tok-{uuid4().hex}",
        company_id="tenant-1",
    )
    db.add(inv)
    db.commit()
    return inv


def test_hex_format_rows_do_not_derail_the_sequence(db) -> None:
    """The old latest-by-created_at read fell to the hex fallback the moment
    the NEWEST row wasn't sequential — the high-water mark must survive
    hex/imported neighbors."""
    _seed_inv(db, "INV-000041")
    _seed_inv(db, "INV-2608AB3F")   # hex-format (old dead-path scheme)
    _seed_inv(db, "1042")            # QB-imported style
    assert next_invoice_number(db) == "INV-000042"


def test_deleted_rows_do_not_cause_reissue(db) -> None:
    """The count-based generator's bug: 3 rows, delete 1 → count+1 == an
    already-taken number. The high-water mark doesn't care about count."""
    _seed_inv(db, "INV-000001")
    _seed_inv(db, "INV-000002")
    victim = _seed_inv(db, "INV-000003")
    db.delete(victim)
    db.commit()
    # count(*)==2 → the old scheme would have minted INV-000003... which a
    # restored backup / audit trail may still reference. High-water = 2
    # (victim hard-deleted) → INV-000003 is genuinely free here; seed a
    # SOFT-deleted 3 to pin the nastier case:
    from datetime import UTC, datetime
    soft = _seed_inv(db, "INV-000003")
    soft.deleted_at = datetime.now(UTC)
    db.commit()
    # count(live)==2, but INV-000003 EXISTS (soft-deleted, unique-held).
    # Old count-based scheme → INV-000003 → IntegrityError. Now:
    assert next_invoice_number(db) == "INV-000004"


def test_empty_table_starts_at_one(db) -> None:
    assert next_invoice_number(db) == "INV-000001"


def test_create_invoice_retries_once_on_number_collision(db, monkeypatch) -> None:
    """Two concurrent creates computing the same number: the second flush
    used to raise an uncaught IntegrityError → raw 500. Now it regenerates
    and retries once."""
    _seed_inv(db, "INV-000010")
    calls = {"n": 0}
    import gdx_dispatch.core.closeout_billing as cb
    real = cb.next_invoice_number

    def racy(session):
        calls["n"] += 1
        if calls["n"] == 1:
            return "INV-000010"  # the number a concurrent sibling just took
        return real(session)

    monkeypatch.setattr(cb, "next_invoice_number", racy)

    job = Job(
        customer_id=uuid4(), title="Job", lifecycle_stage="completed",
        company_id="tenant-1",
    )
    db.add(job)
    db.commit()
    out = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id),
        _={"user_id": "u1", "tenant_id": "tenant-1", "role": "admin"},
        db=db,
    )
    assert calls["n"] == 2
    assert out["invoice_number"] == "INV-000011"
