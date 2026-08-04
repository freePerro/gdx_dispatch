"""
gdx_dispatch/tests/test_39_tax_settings.py — Tax module endpoint tests.

History: this file used to assert 404 for /api/tax/* on the premise that "no
tax routes exist" — but the fixture built an empty FastAPI() (its only
include_router was try/except-wrapped around a module that had been deleted),
so every request 404'd by construction while the real, mounted tax module
(gdx_dispatch/modules/tax/) shipped with zero endpoint coverage. Rewritten
2026-08-03 against the real router: /api/tax/config (GET/PATCH),
/api/tax/exemptions (GET/POST/DELETE), /api/tax/resolve.

The sales-tax *report* lives in routers/reports.py and is covered by
test_reports.py, not here.
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption
from gdx_dispatch.modules.tax.router import router as tax_router
from gdx_dispatch.routers.auth import get_current_user

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ADMIN = {"id": "user-admin", "role": "admin", "tenant_id": "tenant-test"}
_TECH = {"id": "user-tech", "role": "tech", "tenant_id": "tenant-test"}


@pytest.fixture()
def make_client():
    """Factory: TestClient over the real tax router with an isolated in-memory
    DB. ``make_client(user)`` overrides auth with that user; ``make_client(None)``
    leaves the real get_current_user dependency in place (for 401 checks).
    The engine is shared across clients from one factory so admin/tech clients
    in a single test see the same data."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TaxConfig.__table__.create(bind=engine, checkfirst=True)
    TaxExemption.__table__.create(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    clients: list[TestClient] = []

    def _make(user: dict | None = _ADMIN) -> TestClient:
        app = FastAPI()
        app.include_router(tax_router)
        app.dependency_overrides[get_db] = _override_db
        if user is not None:
            app.dependency_overrides[get_current_user] = lambda: user
        tc = TestClient(app, raise_server_exceptions=False)
        clients.append(tc)
        return tc

    yield _make

    for tc in clients:
        tc.app.dependency_overrides.clear()
    engine.dispose()


# ---------------------------------------------------------------------------
# Reachability — the routes must exist on the REAL app
# ---------------------------------------------------------------------------


def test_tax_routes_mounted_in_real_app():
    """app.py wraps router imports in try/except with an empty-router fallback,
    so a broken import would silently 404 the whole tax surface. Assert the
    routes are actually present on the production app object."""
    from gdx_dispatch.app import create_app
    from gdx_dispatch.tests.conftest import app_route_paths

    paths = app_route_paths(create_app())
    for path in ("/api/tax/config", "/api/tax/exemptions", "/api/tax/resolve"):
        assert path in paths, f"{path} missing from the real app's route table"


# ---------------------------------------------------------------------------
# /api/tax/config
# ---------------------------------------------------------------------------


def test_get_config_creates_default(make_client):
    """Any authenticated role can read; first read mints the default row."""
    resp = make_client(_TECH).get("/api/tax/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Default"
    assert body["default_rate"] == 0.0
    assert body["tax_labor"] is False
    assert body["configured_at"] is None


def test_patch_config_requires_admin(make_client):
    resp = make_client(_TECH).patch("/api/tax/config", json={"default_rate": 0.07})
    assert resp.status_code == 403


def test_patch_config_updates_and_persists(make_client):
    admin = make_client(_ADMIN)
    resp = admin.patch(
        "/api/tax/config",
        json={"default_rate": 0.0738, "tax_labor": True, "name": "MN default"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_rate"] == pytest.approx(0.0738)
    assert body["tax_labor"] is True
    assert body["name"] == "MN default"
    assert body["configured_at"] is not None

    # Persisted, not just echoed
    again = admin.get("/api/tax/config")
    assert again.status_code == 200
    assert again.json()["default_rate"] == pytest.approx(0.0738)


def test_patch_config_rejects_out_of_range_rate(make_client):
    resp = make_client(_ADMIN).patch("/api/tax/config", json={"default_rate": 1.5})
    assert resp.status_code == 422


def test_unauthenticated_config_read_rejected(make_client):
    resp = make_client(None).get("/api/tax/config")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# /api/tax/exemptions + /api/tax/resolve
# ---------------------------------------------------------------------------


def test_list_exemptions_requires_admin(make_client):
    resp = make_client(_TECH).get("/api/tax/exemptions")
    assert resp.status_code == 403


def test_exemption_lifecycle_zeroes_then_restores_rate(make_client):
    """POST an exemption → resolve returns 0 for that customer; DELETE it →
    resolve returns the configured default again."""
    admin = make_client(_ADMIN)
    admin.patch("/api/tax/config", json={"default_rate": 0.0738})
    customer_id = str(uuid4())

    created = admin.post(
        "/api/tax/exemptions",
        json={"customer_id": customer_id, "reason": "non-profit", "certificate_id": "ST3-123"},
    )
    assert created.status_code == 201
    exemption = created.json()
    assert exemption["customer_id"] == customer_id
    assert exemption["exempt"] is True

    listed = admin.get("/api/tax/exemptions")
    assert listed.status_code == 200
    assert [e["id"] for e in listed.json()] == [exemption["id"]]

    exempt_rate = admin.get("/api/tax/resolve", params={"customer_id": customer_id})
    assert exempt_rate.status_code == 200
    assert exempt_rate.json()["rate"] == 0.0

    deleted = admin.delete(f"/api/tax/exemptions/{exemption['id']}")
    assert deleted.status_code == 204

    restored = admin.get("/api/tax/resolve", params={"customer_id": customer_id})
    assert restored.json()["rate"] == pytest.approx(0.0738)


def test_create_exemption_rejects_bad_customer_id(make_client):
    resp = make_client(_ADMIN).post("/api/tax/exemptions", json={"customer_id": "not-a-uuid"})
    assert resp.status_code == 400


def test_delete_exemption_unknown_is_404(make_client):
    admin = make_client(_ADMIN)
    assert admin.delete("/api/tax/exemptions/not-a-uuid").status_code == 404
    assert admin.delete(f"/api/tax/exemptions/{uuid4()}").status_code == 404


def test_resolve_without_customer_returns_default_rate(make_client):
    admin = make_client(_ADMIN)
    admin.patch("/api/tax/config", json={"default_rate": 0.0875})
    resp = admin.get("/api/tax/resolve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rate"] == pytest.approx(0.0875)
    assert body["rate_pct"] == pytest.approx(8.75)


def test_expired_exemption_does_not_zero_rate(make_client):
    """exempt_until in the past → the exemption no longer applies."""
    admin = make_client(_ADMIN)
    admin.patch("/api/tax/config", json={"default_rate": 0.0738})
    customer_id = str(uuid4())
    created = admin.post(
        "/api/tax/exemptions",
        json={
            "customer_id": customer_id,
            "exempt_until": (date.today() - timedelta(days=1)).isoformat(),
        },
    )
    assert created.status_code == 201

    resp = admin.get("/api/tax/resolve", params={"customer_id": customer_id})
    assert resp.json()["rate"] == pytest.approx(0.0738)
