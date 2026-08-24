"""Payment plans are real, optional, and honest (M39).

The old endpoint computed a schedule, persisted NOTHING, and returned a
plan_id that did not exist. Doug 2026-08-24: "we don't do payment plans. but
the option for it should be there for it to be turned on and functional."
So: off by default with an honest refusal, persisted rows when on, exact
rounding, one active plan per invoice, cancellable, audited.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, ensure_audit_table
from gdx_dispatch.models.tenant_models import (
    AppSettings,
    Customer,
    Invoice,
    PaymentPlan,
    PaymentPlanInstallment,
)

TENANT = "tenant-m39"
OFFICE = {"user_id": "office-user", "email": "office@example.com", "role": "admin"}
START = date(2026, 9, 1)


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


def _invoice(db, *, total="900.00"):
    cust = Customer(id=uuid.uuid4(), name="Plan Customer", company_id=TENANT)
    db.add(cust)
    inv = Invoice(
        id=uuid.uuid4(), customer_id=cust.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}", billing_type="standard",
        sequence_number=1, subtotal=Decimal(total), tax_amount=Decimal("0"),
        total=Decimal(total), balance_due=Decimal(total), status="sent",
        public_token=uuid.uuid4().hex, company_id=TENANT,
    )
    db.add(inv)
    db.commit()
    return inv


def _enable(db):
    row = AppSettings(payment_plans_enabled=True)
    db.add(row)
    db.commit()


def _create(db, inv, n=3, start=START):
    from gdx_dispatch.routers.invoices import PaymentPlanIn, create_payment_plan

    return create_payment_plan(
        str(inv.id), PaymentPlanIn(num_installments=n, start_date=start), db=db, _=OFFICE
    )


def test_off_by_default_refuses_honestly_and_writes_nothing(db):
    """THE DECISION: default OFF at this deployment — an honest 409, never a
    phantom plan_id."""
    inv = _invoice(db)
    with pytest.raises(HTTPException) as exc:
        _create(db, inv)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "payment_plans_disabled"
    assert db.query(PaymentPlan).count() == 0
    assert db.query(AuditLog).filter(AuditLog.action == "payment_plan_created").count() == 0


def test_on_persists_a_real_plan(db):
    """THE FIX: the returned plan_id names a row that exists, with its
    installments."""
    _enable(db)
    inv = _invoice(db)
    out = _create(db, inv, n=3)
    plan = db.get(PaymentPlan, uuid.UUID(out["plan_id"]))
    assert plan is not None, "the returned plan_id must name a persisted row"
    rows = db.query(PaymentPlanInstallment).filter(
        PaymentPlanInstallment.plan_id == plan.id
    ).order_by(PaymentPlanInstallment.seq).all()
    assert len(rows) == 3
    assert [r.seq for r in rows] == [1, 2, 3]
    assert sum(Decimal(str(r.amount)) for r in rows) == Decimal("900.00")
    assert rows[0].due_date == START
    row = db.query(AuditLog).filter(AuditLog.action == "payment_plan_created").first()
    assert row is not None and row.details["plan_id"] == out["plan_id"]


def test_rounding_last_installment_absorbs_the_remainder(db):
    _enable(db)
    inv = _invoice(db, total="100.00")
    out = _create(db, inv, n=3)
    amounts = [i["amount"] for i in out["installments"]]
    assert amounts == [33.33, 33.33, 33.34]
    assert round(sum(amounts), 2) == 100.00


def test_one_active_plan_per_invoice(db):
    _enable(db)
    inv = _invoice(db)
    _create(db, inv)
    with pytest.raises(HTTPException) as exc:
        _create(db, inv)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "plan_exists"
    assert db.query(PaymentPlan).count() == 1


def test_get_returns_enabled_plus_the_persisted_schedule(db):
    """ONE call tells the view everything (audit round 2: reading the toggle
    from admin-gated /api/settings hid the feature from non-admin office)."""
    from gdx_dispatch.routers.invoices import get_payment_plan

    _enable(db)
    inv = _invoice(db)
    out = _create(db, inv, n=4)
    got = get_payment_plan(str(inv.id), db=db, _=OFFICE)
    assert got["enabled"] is True
    assert got["plan"]["plan_id"] == out["plan_id"]
    assert len(got["plan"]["installments"]) == 4


def test_get_reports_disabled_and_no_plan_without_erroring(db):
    from gdx_dispatch.routers.invoices import get_payment_plan

    inv = _invoice(db)
    assert get_payment_plan(str(inv.id), db=db, _=OFFICE) == {"enabled": False, "plan": None}
    _enable(db)
    assert get_payment_plan(str(inv.id), db=db, _=OFFICE) == {"enabled": True, "plan": None}


def test_cancel_is_soft_audited_and_reopens_the_slot(db):
    from gdx_dispatch.routers.invoices import cancel_payment_plan

    _enable(db)
    inv = _invoice(db)
    first = _create(db, inv)
    cancel_payment_plan(str(inv.id), db=db, _=OFFICE)
    plan = db.get(PaymentPlan, uuid.UUID(first["plan_id"]))
    assert plan.status == "cancelled"
    assert plan.cancelled_at is not None and plan.cancelled_by == "office-user"
    assert db.query(AuditLog).filter(AuditLog.action == "payment_plan_cancelled").count() == 1
    # the schedule row survives (soft), and a corrected plan can be made
    second = _create(db, inv)
    assert second["plan_id"] != first["plan_id"]


def test_zero_total_invoice_refused(db):
    _enable(db)
    inv = _invoice(db, total="0.00")
    with pytest.raises(HTTPException) as exc:
        _create(db, inv)
    assert exc.value.status_code == 422


def test_settings_patch_persists_the_toggle(db):
    """The toggle must be PATCHable through the real settings endpoint —
    otherwise the Settings card writes to a field the allowlist drops."""
    from gdx_dispatch.routers.settings import SettingsPatchIn, patch_settings

    out = patch_settings(
        payload=SettingsPatchIn(payment_plans_enabled=True),
        request=None,
        db=db,
        current_user=OFFICE,
    )
    assert out["payment_plans_enabled"] is True
    row = db.query(AppSettings).first()
    assert row is not None and row.payment_plans_enabled is True



# ── audit round 2 ──────────────────────────────────────────────────────────

def test_tiny_total_cannot_mint_a_negative_installment(db):
    """Reproduced by the audit: $0.10 over 12 → eleven 1-cent rows and a
    PERSISTED −1¢ installment. Refuse instead."""
    from gdx_dispatch.models.tenant_models import PaymentPlan

    _enable(db)
    inv = _invoice(db, total="0.10")
    with pytest.raises(HTTPException) as exc:
        _create(db, inv, n=12)
    assert exc.value.status_code == 422
    assert db.query(PaymentPlan).count() == 0


def test_only_issued_invoices_can_carry_a_plan(db):
    _enable(db)
    inv = _invoice(db)
    inv.status = "draft"
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _create(db, inv)
    assert exc.value.status_code == 409


def test_plan_schedules_the_remaining_receivable_not_the_printed_total(db):
    """A part-paid invoice plans what is still OWED."""
    from gdx_dispatch.models.tenant_models import Payment

    _enable(db)
    inv = _invoice(db, total="900.00")
    db.add(Payment(id=uuid.uuid4(), invoice_id=inv.id, amount=Decimal("300.00"),
                   method="check", company_id=TENANT))
    inv.balance_due = Decimal("600.00")
    db.commit()
    out = _create(db, inv, n=3)
    assert out["total_amount"] == 600.00
    assert [i["amount"] for i in out["installments"]] == [200.0, 200.0, 200.0]


def test_installment_statuses_derive_from_money_that_arrived(db):
    """Nothing writes installment rows after create — the displayed status
    derives at read time: covered once cumulative paid reaches the slice,
    overdue past due_date, else pending."""
    from gdx_dispatch.models.tenant_models import Payment
    from gdx_dispatch.routers.invoices import get_payment_plan

    _enable(db)
    inv = _invoice(db, total="900.00")
    _create(db, inv, n=3, start=date(2026, 7, 1))  # first two due in the past
    db.add(Payment(id=uuid.uuid4(), invoice_id=inv.id, amount=Decimal("300.00"),
                   method="check", company_id=TENANT))
    db.commit()
    got = get_payment_plan(str(inv.id), db=db, _=OFFICE)
    statuses = [i["status"] for i in got["plan"]["installments"]]
    assert statuses[0] == "covered", "the paid $300 covers the first slice"
    assert statuses[1] == "overdue", "due 2026-07-31, unpaid, today is past it"
    assert statuses[2] == "pending"


def test_the_mutations_carry_the_invoices_write_gate(db):
    """Audit round 2: the new mutations must not join the 18/20-ungated pile.
    Route-level pin that FAILS if the dependency is removed (counterfactual-
    verified) — the functions are called directly elsewhere in this file, so
    only the router wiring can carry the gate."""
    from gdx_dispatch.routers.invoices import router

    gated = 0
    for route in router.routes:
        if getattr(route, "path", "") == "/api/invoices/{invoice_id}/payment-plan" and            set(getattr(route, "methods", ())) & {"POST", "DELETE"}:
            deps = [d.call for d in route.dependant.dependencies]
            names = {getattr(c, "__qualname__", "") for c in deps}
            assert any("require_permission" in n for n in names), (
                f"{route.methods} /payment-plan lost its permission gate"
            )
            gated += 1
    assert gated == 2, f"expected POST+DELETE gated, saw {gated}"


def test_voiding_the_invoice_cancels_its_active_plan(db):
    """A void kills what is owed — an active schedule on a void invoice
    would be a standing lie (audit round 2)."""
    from gdx_dispatch.models.tenant_models import PaymentPlan
    from gdx_dispatch.routers.invoices import void_invoice

    _enable(db)
    inv = _invoice(db)
    out = _create(db, inv)
    void_invoice(inv.id, _=OFFICE, db=db)
    plan = db.get(PaymentPlan, uuid.UUID(out["plan_id"]))
    assert plan.status == "cancelled"
    assert plan.cancelled_at is not None
    row = (db.query(AuditLog)
           .filter(AuditLog.action == "payment_plan_cancelled").first())
    assert row is not None and row.details.get("why") == "invoice voided"
