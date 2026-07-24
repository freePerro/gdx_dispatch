"""Tier 6 contract-gap fixes — write-path data loss
(docs/design/backend-vue-contract-gaps-2026-07-24.md).

Every test here writes the exact fields the UI has ALWAYS sent and asserts
they now persist — before this round, each PATCH toasted "Saved" while the
backend silently dropped the field.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401 — registers tables
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    Expense,
    Lead,
    ServiceAgreement,
    Technician,
)

TENANT = "tenant-test"


def _engine_and_sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _make_app(routers: list, role: str = "admin"):
    from gdx_dispatch.core.auth import get_current_user as core_gcu
    from gdx_dispatch.core.database import get_db as core_get_db
    from gdx_dispatch.routers.auth import get_current_user as routers_gcu

    engine, SessionLocal = _engine_and_sessions()
    app = FastAPI()

    @app.middleware("http")
    async def _tenant_shim(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": TENANT}
        return await call_next(request)

    for r in routers:
        app.include_router(r)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    user = {"sub": "u-1", "user_id": "u-1", "role": role, "tenant_id": TENANT, "email": "u@x"}
    app.dependency_overrides[core_get_db] = _override_db
    app.dependency_overrides[routers_gcu] = lambda: user
    app.dependency_overrides[core_gcu] = lambda: user
    return app, SessionLocal, engine


# ── 6.1 technicians: name/email/phone persist ──────────────────────────────


def test_technician_contact_fields_persist():
    from gdx_dispatch.routers import technicians as technicians_router

    app, SessionLocal, engine = _make_app([technicians_router.router])
    try:
        client = TestClient(app)
        created = client.post(
            "/api/technicians",
            json={"user_id": "amber.r", "name": "Amber Rosa", "email": "amber@x.com", "phone": "555-0101", "skills": ["springs"]},
        )
        assert created.status_code in (200, 201), created.text
        tech_id = created.json().get("id")

        db = SessionLocal()
        row = db.get(Technician, tech_id)
        assert row.name == "Amber Rosa"
        assert row.email == "amber@x.com"
        assert row.phone == "555-0101"
        db.close()

        patched = client.patch(
            f"/api/technicians/{tech_id}",
            json={"name": "Amber J. Rosa", "phone": "555-0102"},
        )
        assert patched.status_code == 200, patched.text
        db = SessionLocal()
        row = db.get(Technician, tech_id)
        assert row.name == "Amber J. Rosa"
        assert row.phone == "555-0102"
        assert row.email == "amber@x.com"  # untouched fields survive
        db.close()
    finally:
        engine.dispose()


# ── 6.2 expenses: the approval workflow persists ───────────────────────────


def test_expense_status_persists_and_serializes():
    from gdx_dispatch.routers import expenses as expenses_router

    app, SessionLocal, engine = _make_app([expenses_router.router])
    try:
        db = SessionLocal()
        exp = Expense(
            id=uuid4(), company_id=TENANT, vendor="Home Depot", amount=42.5,
            date=date(2026, 7, 1), category="Parts/Supplies",
        )
        db.add(exp)
        db.commit()
        exp_id = str(exp.id)
        db.close()

        client = TestClient(app)
        # The UI sends TitleCase — server lowercases into the column.
        r = client.patch(f"/api/expenses/{exp_id}", json={"status": "Approved"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"

        db = SessionLocal()
        assert db.get(Expense, UUID(exp_id)).status == "approved"
        db.close()

        # Reload path: list serializer emits it (the old bug: the list
        # re-derived everything as Draft because status was never sent).
        listed = client.get("/api/expenses").json()
        rows = listed if isinstance(listed, list) else listed.get("items", [])
        assert any(e.get("status") == "approved" for e in rows)

        assert client.patch(f"/api/expenses/{exp_id}", json={"status": "shouting"}).status_code == 422
    finally:
        engine.dispose()


# ── 6.5 leads: stage persists via the edit dialog PATCH ────────────────────


def test_lead_stage_patchable():
    from gdx_dispatch.routers import leads as leads_router

    app, SessionLocal, engine = _make_app([leads_router.router])
    try:
        db = SessionLocal()
        lead = Lead(id=uuid4(), company_id=TENANT, name="Warm Lead", stage="new")
        db.add(lead)
        db.commit()
        lead_id = str(lead.id)
        db.close()

        client = TestClient(app)
        r = client.patch(f"/api/leads/{lead_id}", json={"stage": "qualified"})
        assert r.status_code == 200, r.text

        db = SessionLocal()
        assert db.get(Lead, UUID(lead_id)).stage == "qualified"
        db.close()

        assert client.patch(f"/api/leads/{lead_id}", json={"stage": "vaporized"}).status_code == 422
    finally:
        engine.dispose()


# ── 6.8 service agreements: start_date/customer/template persist ───────────


def test_service_agreement_relink_and_redate():
    from gdx_dispatch.routers import service_agreements as sa_router

    app, SessionLocal, engine = _make_app([sa_router.router])
    try:
        db = SessionLocal()
        old_customer = uuid4()
        agreement = ServiceAgreement(
            id=uuid4(), company_id=TENANT, customer_id=old_customer,
            name="Annual tune-up",
            start_date=datetime(2026, 1, 1, tzinfo=UTC),
            end_date=datetime(2026, 12, 31, tzinfo=UTC),
            status="active",
        )
        db.add(agreement)
        db.commit()
        agreement_id = str(agreement.id)
        db.close()

        client = TestClient(app)
        new_customer = str(uuid4())
        r = client.patch(
            f"/api/service-agreements/{agreement_id}",
            json={"start_date": "2026-02-01T00:00:00Z", "customer_id": new_customer},
        )
        assert r.status_code == 200, r.text

        db = SessionLocal()
        row = db.get(ServiceAgreement, UUID(agreement_id))
        assert str(row.customer_id) == new_customer
        assert row.start_date.date() == date(2026, 2, 1)
        db.close()

        assert client.patch(
            f"/api/service-agreements/{agreement_id}", json={"customer_id": "not-a-uuid"}
        ).status_code == 422
    finally:
        engine.dispose()


# ── 6.9 appointments: job/customer relink persists ─────────────────────────


def test_appointment_relink_persists():
    from gdx_dispatch.routers import appointments as appt_router

    app, SessionLocal, engine = _make_app([appt_router.router])
    try:
        db = SessionLocal()
        appt = Appointment(
            id=uuid4(), company_id=TENANT, title="Visit",
            start_at=datetime.now(UTC) + timedelta(days=1),
            end_at=datetime.now(UTC) + timedelta(days=1, hours=1),
            status="scheduled",
        )
        db.add(appt)
        db.commit()
        appt_id = str(appt.id)
        db.close()

        client = TestClient(app)
        job_id = str(uuid4())
        customer_id = str(uuid4())
        r = client.patch(
            f"/api/appointments/{appt_id}",
            json={"job_id": job_id, "customer_id": customer_id},
        )
        assert r.status_code == 200, r.text

        db = SessionLocal()
        row = db.get(Appointment, UUID(appt_id))
        assert str(row.job_id) == job_id
        assert str(row.customer_id) == customer_id
        db.close()

        # Clearing unlinks; malformed refuses instead of silently unlinking
        assert client.patch(f"/api/appointments/{appt_id}", json={"job_id": None}).status_code == 200
        db = SessionLocal()
        assert db.get(Appointment, UUID(appt_id)).job_id is None
        db.close()
        assert client.patch(f"/api/appointments/{appt_id}", json={"customer_id": "nope"}).status_code == 422
    finally:
        engine.dispose()


def test_expense_create_honors_status():
    """Audit catch: the Log Expense dialog sends status on CREATE — without
    ExpenseCreate.status an expense logged as Approved landed as draft."""
    from gdx_dispatch.routers import expenses as expenses_router

    app, SessionLocal, engine = _make_app([expenses_router.router])
    try:
        client = TestClient(app)
        r = client.post(
            "/api/expenses",
            json={"vendor": "Fleet Farm", "amount": 12.0, "date": "2026-07-24",
                  "category": "Parts/Supplies", "status": "Approved"},
        )
        assert r.status_code in (200, 201), r.text
        assert r.json()["status"] == "approved"

        r2 = client.post(
            "/api/expenses",
            json={"vendor": "No Status Co", "amount": 5.0, "date": "2026-07-24",
                  "category": "Parts/Supplies"},
        )
        assert r2.status_code in (200, 201), r2.text
        assert r2.json()["status"] == "draft"
    finally:
        engine.dispose()
