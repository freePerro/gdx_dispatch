"""PR1-billing-capture (2026-07-07) — regression tests for the four
cash-flow quick wins:

1. Collections AR aging: the status filter shipped capitalized
   ("Sent"/"Overdue"/"Partial") against the lowercase enum, so the report
   ALWAYS returned $0. Fixed to the receivable predicate (not draft/void,
   balance_due > 0) reading balance_due, not the deprecated amount_paid.
2. Zero-price invoice policy (F-75) was dead code — now wired into
   create_invoice and add_invoice_line (block → 422 before any write,
   warn → surfaced on the response).
3. /api/invoices/summary surfaces never-sent drafts (draft_count /
   draft_total) without polluting total_outstanding.
4. POST /invoices/{id}/send no longer resurrects voided invoices (409).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, JobPartNeeded, Payment
from gdx_dispatch.modules.catalog_policy.service import CatalogPolicy
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.collections import aging_report
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    PaymentCreateIn,
    add_invoice_line,
    billing_summary,
    create_invoice,
    record_payment,
    send_invoice,
)


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    for tbl in [
        Job.__table__,
        Estimate.__table__,
        EstimateLine.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        JobPartNeeded.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)

    TenantBase.metadata.create_all(bind=engine, checkfirst=True)

    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _current_user() -> dict[str, str]:
    return {"user_id": "user-1", "tenant_id": "tenant-1", "role": "admin"}


def _seed_invoice(
    db,
    *,
    status: str,
    balance: float,
    due_days_ago: int | None = 0,
    total: float | None = None,
) -> Invoice:
    """Directly seed an invoice with controlled status/balance/due_date.

    due_days_ago: positive = already overdue, negative = due in the future,
    None = no due date at all.
    """
    amount = total if total is not None else balance
    inv = Invoice(
        company_id="tenant-1",
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        billing_type="standard",
        sequence_number=1,
        subtotal=Decimal(str(amount)),
        tax_amount=Decimal("0"),
        total=Decimal(str(amount)),
        balance_due=Decimal(str(balance)),
        status=status,
        invoice_date=date.today(),
        due_date=(
            date.today() - timedelta(days=due_days_ago)
            if due_days_ago is not None
            else None
        ),
        public_token=uuid4().hex,
        locked=False,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


# --------------------------------------------------------------------------
# 1. Collections AR aging
# --------------------------------------------------------------------------


def test_aging_report_returns_real_receivables(tenant_db_session):
    """Pre-fix this returned $0 forever: the filter matched capitalized
    statuses that never exist in the DB. Now lowercase receivables land in
    the right buckets, sourced from balance_due."""
    db = tenant_db_session
    _seed_invoice(db, status="sent", balance=500.0, due_days_ago=10)  # current 0-30
    _seed_invoice(db, status="sent", balance=250.0, due_days_ago=45)  # 31-60
    _seed_invoice(db, status="sent", balance=125.0, due_days_ago=95)  # over 90

    out = aging_report(_=_current_user(), db=db)

    assert out["total_outstanding"] == 875.0
    by_label = {b["label"]: b for b in out["buckets"]}
    assert by_label["Current (0-30)"]["count"] == 1
    assert by_label["Current (0-30)"]["total"] == 500.0
    assert by_label["31-60 Days"]["count"] == 1
    assert by_label["31-60 Days"]["total"] == 250.0
    assert by_label["Over 90 Days"]["count"] == 1
    assert by_label["Over 90 Days"]["total"] == 125.0


def test_aging_report_excludes_non_receivables(tenant_db_session):
    """Drafts (not yet receivables), voids, settled invoices, and
    not-yet-due invoices must all stay out of aging."""
    db = tenant_db_session
    _seed_invoice(db, status="draft", balance=999.0, due_days_ago=40)
    _seed_invoice(db, status="void", balance=888.0, due_days_ago=40)
    _seed_invoice(db, status="paid", balance=0.0, due_days_ago=40, total=777.0)
    _seed_invoice(db, status="sent", balance=100.0, due_days_ago=-5)  # due in future
    _seed_invoice(db, status="sent", balance=50.0, due_days_ago=None)  # no due date

    out = aging_report(_=_current_user(), db=db)

    assert out["total_outstanding"] == 0.0
    assert all(b["count"] == 0 for b in out["buckets"])


def test_aging_report_uses_balance_due_not_amount_paid(tenant_db_session):
    """amount_paid is the deprecated field balance recomputation ignores —
    a partially-paid invoice must age by its balance_due remainder."""
    db = tenant_db_session
    inv = _seed_invoice(db, status="sent", balance=300.0, due_days_ago=35, total=1000.0)
    # Simulate stale legacy amount_paid that disagrees with balance_due.
    inv.amount_paid = Decimal("100.00")
    db.commit()

    out = aging_report(_=_current_user(), db=db)

    assert out["total_outstanding"] == 300.0
    by_label = {b["label"]: b for b in out["buckets"]}
    assert by_label["31-60 Days"]["invoices"][0]["amount_due"] == 300.0


# --------------------------------------------------------------------------
# 2. Zero-price invoice policy wiring (F-75)
# --------------------------------------------------------------------------


def test_create_invoice_zero_price_line_warns_by_default(monkeypatch, tenant_db_session):
    monkeypatch.setattr(
        "gdx_dispatch.routers.invoices.get_policy", lambda tid: CatalogPolicy()
    )
    data = create_invoice(
        payload=InvoiceCreateIn(
            customer_id=uuid4(),
            line_items=[
                {"description": "Mystery part", "quantity": 1, "unit_price": 0},
                {"description": "Real part", "quantity": 1, "unit_price": 50.0},
            ],
        ),
        _=_current_user(),
        db=tenant_db_session,
    )
    assert data["warnings"], "zero-price line must surface a warning"
    assert len(data["warnings"]) == 1
    assert "Mystery part" in data["warnings"][0]


def test_create_invoice_zero_price_block_writes_nothing(monkeypatch, tenant_db_session):
    """Block mode must 422 BEFORE any row lands — a half-created invoice
    would be its own leak."""
    monkeypatch.setattr(
        "gdx_dispatch.routers.invoices.get_policy",
        lambda tid: CatalogPolicy(block_zero_price_on_invoice=True),
    )
    with pytest.raises(HTTPException) as exc_info:
        create_invoice(
            payload=InvoiceCreateIn(
                customer_id=uuid4(),
                line_items=[{"description": "Freebie", "quantity": 1, "unit_price": 0}],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
    assert exc_info.value.status_code == 422
    assert (tenant_db_session.scalar(select(func.count(Invoice.id))) or 0) == 0


def test_create_invoice_priced_lines_skip_policy_read(monkeypatch, tenant_db_session):
    """Hot path: fully-priced invoices must not pay the control-plane
    policy read at all (and must return no warnings key)."""

    def _explode(tid):  # pragma: no cover - failure branch
        raise AssertionError("get_policy must not be called for priced lines")

    monkeypatch.setattr("gdx_dispatch.routers.invoices.get_policy", _explode)
    data = create_invoice(
        payload=InvoiceCreateIn(
            customer_id=uuid4(),
            line_items=[{"description": "Spring", "quantity": 2, "unit_price": 35.0}],
        ),
        _=_current_user(),
        db=tenant_db_session,
    )
    assert "warnings" not in data


def test_create_invoice_from_estimate_zero_price_warns(monkeypatch, tenant_db_session):
    """The estimate-copy branch is guarded too — a $0 estimate line is the
    classic 'included' line that leaks."""
    monkeypatch.setattr(
        "gdx_dispatch.routers.invoices.get_policy", lambda tid: CatalogPolicy()
    )
    db = tenant_db_session
    job = Job(
        customer_id=uuid4(),
        title="Door install",
        description="t",
        lifecycle_stage="completed",
        dispatch_status="done",
        billing_status="unbilled",
        company_id="tenant-1",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    est = Estimate(
        job_id=job.id,
        customer_id=job.customer_id,
        estimate_number=f"EST-{uuid4().hex[:8]}",
        label="Est",
        proposal_mode=False,
        total=Decimal("100.00"),
        status="accepted",
        public_token=uuid4().hex,
        company_id="tenant-1",
    )
    db.add(est)
    db.commit()
    db.refresh(est)
    db.add(
        EstimateLine(
            estimate_id=est.id,
            description="Install labor",
            quantity=1,
            unit_price=Decimal("100.00"),
            line_total=Decimal("100.00"),
            sort_order=1,
            company_id="tenant-1",
        )
    )
    db.add(
        EstimateLine(
            estimate_id=est.id,
            description="Hardware (included)",
            quantity=1,
            unit_price=Decimal("0"),
            line_total=Decimal("0"),
            sort_order=2,
            company_id="tenant-1",
        )
    )
    db.commit()

    data = create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, estimate_id=est.id),
        _=_current_user(),
        db=db,
    )
    assert data["warnings"]
    assert "Hardware (included)" in data["warnings"][0]


def test_add_invoice_line_zero_price_block(monkeypatch, tenant_db_session):
    monkeypatch.setattr(
        "gdx_dispatch.modules.catalog_policy.service.get_policy",
        lambda tid: CatalogPolicy(block_zero_price_on_invoice=True),
    )
    db = tenant_db_session
    created = create_invoice(
        payload=InvoiceCreateIn(
            customer_id=uuid4(),
            line_items=[{"description": "Spring", "quantity": 1, "unit_price": 60.0}],
        ),
        _=_current_user(),
        db=db,
    )
    with pytest.raises(HTTPException) as exc_info:
        add_invoice_line(
            UUID(created["id"]),
            payload=InvoiceLineCreateIn(description="Freebie", quantity=1, unit_price=0),
            _=_current_user(),
            db=db,
        )
    assert exc_info.value.status_code == 422
    assert (
        db.scalar(
            select(func.count(InvoiceLine.id)).where(
                InvoiceLine.invoice_id == UUID(created["id"])
            )
        )
        == 1
    ), "blocked line must not be written"


def test_add_invoice_line_zero_price_warns_by_default(monkeypatch, tenant_db_session):
    monkeypatch.setattr(
        "gdx_dispatch.modules.catalog_policy.service.get_policy",
        lambda tid: CatalogPolicy(),
    )
    db = tenant_db_session
    created = create_invoice(
        payload=InvoiceCreateIn(
            customer_id=uuid4(),
            line_items=[{"description": "Spring", "quantity": 1, "unit_price": 60.0}],
        ),
        _=_current_user(),
        db=db,
    )
    resp = add_invoice_line(
        UUID(created["id"]),
        payload=InvoiceLineCreateIn(description="Freebie", quantity=1, unit_price=0),
        _=_current_user(),
        db=db,
    )
    assert resp["warning"]
    assert "zero-price" in resp["warning"]


# --------------------------------------------------------------------------
# 3. Draft surfacing on the billing summary
# --------------------------------------------------------------------------


def test_billing_summary_surfaces_drafts_without_polluting_outstanding(tenant_db_session):
    db = tenant_db_session
    _seed_invoice(db, status="draft", balance=100.0, due_days_ago=0)
    _seed_invoice(db, status="draft", balance=200.0, due_days_ago=0)
    _seed_invoice(db, status="sent", balance=300.0, due_days_ago=5)

    out = billing_summary(request=None, _=_current_user(), db=db)

    assert out["draft_count"] == 2
    assert out["draft_total"] == 300.0
    # Drafts still excluded from receivables — that separation is the point.
    assert out["total_outstanding"] == 300.0


def test_billing_summary_zero_drafts(tenant_db_session):
    out = billing_summary(request=None, _=_current_user(), db=tenant_db_session)
    assert out["draft_count"] == 0
    assert out["draft_total"] == 0.0


# --------------------------------------------------------------------------
# 4. /send must not resurrect voided invoices
# --------------------------------------------------------------------------


def test_send_invoice_void_conflicts_and_stays_void(tenant_db_session):
    db = tenant_db_session
    inv = _seed_invoice(db, status="void", balance=0.0, due_days_ago=0, total=450.0)

    with pytest.raises(HTTPException) as exc_info:
        send_invoice(inv.id, _=_current_user(), db=db)

    assert exc_info.value.status_code == 409
    db.refresh(inv)
    assert inv.status == "void", "voided invoice must never re-enter AR"


def test_record_payment_on_void_conflicts_and_writes_nothing(tenant_db_session):
    """Audit catch: a payment against a void invoice ran
    _recalculate_invoice, which flips the invoice to "paid" once balance
    hits zero — the void resurrected into Paid This Month through the
    payment door. Must 409 with no Payment row."""
    db = tenant_db_session
    inv = _seed_invoice(db, status="void", balance=0.0, due_days_ago=0, total=450.0)

    with pytest.raises(HTTPException) as exc_info:
        record_payment(
            inv.id,
            payload=PaymentCreateIn(amount=450.0, method="check", date=date.today()),
            _=_current_user(),
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert (db.scalar(select(func.count(Payment.id))) or 0) == 0
    db.refresh(inv)
    assert inv.status == "void"


def test_invoice_balance_due_is_not_nullable():
    """The aging fix reads balance_due directly with no NULL fallback —
    that is only safe while the column stays NOT NULL. Pin the schema
    assumption so a future nullable-ization re-opens this decision."""
    assert Invoice.__table__.c.balance_due.nullable is False
