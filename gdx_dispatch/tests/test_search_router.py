"""Tests for /api/search — the Ctrl+K global search endpoint.

2026-07-07: wired to the frontend palette for the first time, and matching
was widened. Pins the two bugs found during that work:
  - jobs matched on title ONLY, so searching a job number found nothing —
    and the payload returned the job's UUID as "number".
  - invoices matched invoice_number ONLY ("find Henning's invoice" failed).
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers import search as search_router

TENANT = "tenant-test"


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    # require_role reads request.state.current_user when get_current_user
    # isn't overridden (see core/modules.require_role docstring).
    @app.middleware("http")
    async def inject_user(request, call_next):
        request.state.current_user = {"sub": "test-user", "role": "admin", "tenant_id": TENANT}
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(search_router.router)
    app.dependency_overrides[search_router.get_db] = _override_db
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc, SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def _seed(db):
    customer = Customer(
        id=uuid4(),
        name="Henning Lumber Yard",
        phone="555-123-4567",
        email="office@henninglumber.example",
        company_id=TENANT,
    )
    other = Customer(id=uuid4(), name="Acme Doors", company_id=TENANT)
    db.add_all([customer, other])
    db.flush()

    job = Job(
        id=uuid4(),
        customer_id=customer.id,
        job_number="1042",
        title="Spring repair",
        company_id=TENANT,
    )
    deleted_job = Job(
        id=uuid4(),
        customer_id=customer.id,
        job_number="1043",
        title="Spring repair (duplicate)",
        company_id=TENANT,
    )
    db.add_all([job, deleted_job])
    db.flush()
    from datetime import datetime, timezone

    deleted_job.deleted_at = datetime.now(timezone.utc)

    invoice = Invoice(
        id=uuid4(),
        customer_id=customer.id,
        job_id=job.id,
        invoice_number="INV-2201",
        public_token=uuid4().hex,
        company_id=TENANT,
    )
    db.add(invoice)

    estimate = Estimate(
        id=uuid4(),
        customer_id=customer.id,
        estimate_number="EST-77",
        label="Glass upgrade",
        jobsite_address="123 Oak Street, Bluffton",
        public_token=uuid4().hex,
        company_id=TENANT,
    )
    db.add(estimate)
    db.commit()
    return {"customer": customer.id, "job": job.id, "invoice": invoice.id, "estimate": estimate.id}


def test_job_number_matches_and_payload_carries_real_number(client):
    tc, SessionLocal = client
    with SessionLocal() as db:
        ids = _seed(db)

    body = tc.get("/api/search", params={"q": "1042"}).json()
    assert [j["id"] for j in body["jobs"]] == [str(ids["job"])]
    # Regression: pre-fix "number" was the row UUID, not job_number.
    assert body["jobs"][0]["number"] == "1042"
    assert body["jobs"][0]["title"] == "Spring repair"


def test_customer_name_fans_out_to_jobs_invoices_estimates(client):
    tc, SessionLocal = client
    with SessionLocal() as db:
        ids = _seed(db)

    body = tc.get("/api/search", params={"q": "henning"}).json()
    assert [c["id"] for c in body["customers"]] == [str(ids["customer"])]
    assert [j["id"] for j in body["jobs"]] == [str(ids["job"])]
    assert [i["id"] for i in body["invoices"]] == [str(ids["invoice"])]
    assert [e["id"] for e in body["estimates"]] == [str(ids["estimate"])]
    assert body["jobs"][0]["customer_name"] == "Henning Lumber Yard"
    assert body["invoices"][0]["customer_name"] == "Henning Lumber Yard"


def test_customer_phone_and_invoice_number_match(client):
    tc, SessionLocal = client
    with SessionLocal() as db:
        _seed(db)

    by_phone = tc.get("/api/search", params={"q": "555-123"}).json()
    assert [c["name"] for c in by_phone["customers"]] == ["Henning Lumber Yard"]

    by_number = tc.get("/api/search", params={"q": "INV-2201"}).json()
    assert [i["number"] for i in by_number["invoices"]] == ["INV-2201"]


def test_estimate_matches_label_and_jobsite_address(client):
    tc, SessionLocal = client
    with SessionLocal() as db:
        _seed(db)

    by_label = tc.get("/api/search", params={"q": "glass upg"}).json()
    assert [e["number"] for e in by_label["estimates"]] == ["EST-77"]

    by_address = tc.get("/api/search", params={"q": "oak street"}).json()
    assert [e["number"] for e in by_address["estimates"]] == ["EST-77"]


def test_soft_deleted_rows_are_excluded(client):
    tc, SessionLocal = client
    with SessionLocal() as db:
        _seed(db)

    body = tc.get("/api/search", params={"q": "1043"}).json()
    assert body["jobs"] == []


def test_no_match_returns_empty_sections(client):
    tc, SessionLocal = client
    with SessionLocal() as db:
        _seed(db)

    body = tc.get("/api/search", params={"q": "zzz-nothing"}).json()
    assert body == {"jobs": [], "customers": [], "invoices": [], "estimates": []}


def _client_for_role(role):
    """A search-router test client whose middleware injects an arbitrary role.

    The module-level `client` fixture hardcodes role="admin"; these role-gate
    regressions need to vary it.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_user(request, call_next):
        request.state.current_user = {"sub": "test-user", "role": role, "tenant_id": TENANT}
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(search_router.router)
    app.dependency_overrides[search_router.get_db] = _override_db
    return TestClient(app, raise_server_exceptions=True), SessionLocal, engine, app


@pytest.mark.parametrize("role", ["technician", "tech", "dispatcher", "owner", "admin"])
def test_search_admits_canonical_and_legacy_role_spellings(role):
    """Regression (prod incident, 2026-07-10): migration 009 (#45)
    renamed users.role `tech`→`technician`, but the /api/search gate still
    listed only the legacy `"tech"` and require_role matched raw strings — so a
    migrated technician 403'd ("Insufficient role") on every keystroke of the
    mobile-jobs search box. require_role now normalizes both sides, so the
    canonical AND legacy spellings pass."""
    tc, SessionLocal, engine, app = _client_for_role(role)
    try:
        with SessionLocal() as db:
            _seed(db)
        resp = tc.get("/api/search", params={"q": "henning"})
        assert resp.status_code == 200, f"role {role!r} should pass the search gate"
        assert [c["name"] for c in resp.json()["customers"]] == ["Henning Lumber Yard"]
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_search_still_rejects_unlisted_role():
    """The normalize fix must not broaden access: a role that is neither on the
    gate's list nor an alias of one still 403s."""
    tc, _SessionLocal, engine, app = _client_for_role("nobody")
    try:
        resp = tc.get("/api/search", params={"q": "henning"})
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
