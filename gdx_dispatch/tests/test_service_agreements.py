"""Tests for the service_agreements router."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.service_agreements import router


def _make_client(tenant_id: str = "tenant-test") -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "sub": "user-1",
        "role": "admin",
        "tenant_id": tenant_id,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _agreement_payload(**overrides) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    base = {
        "customer_id": str(uuid4()),
        "name": "Annual Maintenance",
        "start_date": _iso(now),
        "end_date": _iso(now + timedelta(days=365)),
        "price": 299.0,
        "services_included": ["Spring inspection", "Lube service"],
        "notes": "Signed on-site",
    }
    base.update(overrides)
    return base


def _template_payload(**overrides) -> dict:
    base = {
        "name": "Gold Maintenance",
        "description": "Annual preventive maintenance",
        "default_duration_months": 12,
        "default_price": 299.0,
        "services_included": ["Spring inspection", "Lube service", "Safety check"],
    }
    base.update(overrides)
    return base


def test_create_template(client: TestClient):
    r = client.post("/api/service-agreements/templates", json=_template_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["name"] == "Gold Maintenance"
    assert data["default_duration_months"] == 12
    assert data["default_price"] == 299.0
    assert data["services_included"] == [
        "Spring inspection",
        "Lube service",
        "Safety check",
    ]
    assert data["company_id"] == "tenant-test"

    listed = client.get("/api/service-agreements/templates").json()
    assert len(listed) == 1
    assert listed[0]["id"] == data["id"]


def test_create_agreement_from_template(client: TestClient):
    tpl = client.post(
        "/api/service-agreements/templates", json=_template_payload()
    ).json()
    payload = _agreement_payload(template_id=tpl["id"], name="Gold for Jane")
    r = client.post("/api/service-agreements", json=payload)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["template_id"] == tpl["id"]
    assert data["status"] == "active"
    assert data["name"] == "Gold for Jane"
    assert data["company_id"] == "tenant-test"


def test_list_agreements_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        r1 = c1.post("/api/service-agreements", json=_agreement_payload(name="A-plan"))
        assert r1.status_code == 201, r1.text
        r2 = c2.post("/api/service-agreements", json=_agreement_payload(name="B-plan"))
        assert r2.status_code == 201, r2.text

        list_a = c1.get("/api/service-agreements").json()
        list_b = c2.get("/api/service-agreements").json()
        assert len(list_a) == 1 and list_a[0]["name"] == "A-plan"
        assert len(list_b) == 1 and list_b[0]["name"] == "B-plan"

        cross = c1.get(f"/api/service-agreements/{list_b[0]['id']}")
        assert cross.status_code == 404
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_expiring_filter(client: TestClient):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    near = client.post(
        "/api/service-agreements",
        json=_agreement_payload(
            name="Expires in 10 days",
            start_date=_iso(now - timedelta(days=355)),
            end_date=_iso(now + timedelta(days=10)),
        ),
    ).json()
    far = client.post(
        "/api/service-agreements",
        json=_agreement_payload(
            name="Expires in 60 days",
            start_date=_iso(now - timedelta(days=305)),
            end_date=_iso(now + timedelta(days=60)),
        ),
    ).json()

    r = client.get("/api/service-agreements/expiring", params={"days": 30})
    assert r.status_code == 200, r.text
    rows = r.json()
    ids = {row["id"] for row in rows}
    assert near["id"] in ids
    assert far["id"] not in ids


def test_cancel_agreement(client: TestClient):
    created = client.post(
        "/api/service-agreements", json=_agreement_payload()
    ).json()
    r = client.post(f"/api/service-agreements/{created['id']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"

    # Second cancel = 400
    r2 = client.post(f"/api/service-agreements/{created['id']}/cancel")
    assert r2.status_code == 400


def test_reject_end_before_start(client: TestClient):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = _agreement_payload(
        start_date=_iso(now + timedelta(days=10)),
        end_date=_iso(now + timedelta(days=5)),
    )
    r = client.post("/api/service-agreements", json=payload)
    assert r.status_code == 422


def test_patch_status(client: TestClient):
    created = client.post(
        "/api/service-agreements", json=_agreement_payload()
    ).json()
    r = client.patch(
        f"/api/service-agreements/{created['id']}",
        json={"status": "expired", "price": 399.0},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "expired"
    assert data["price"] == 399.0

    # Invalid status rejected by pattern
    bad = client.patch(
        f"/api/service-agreements/{created['id']}",
        json={"status": "bogus"},
    )
    assert bad.status_code == 422
