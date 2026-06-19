"""SS-35 slice D tests — /api/sar router."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import pii_registry
from gdx_dispatch.models.platform_ss35_additions import SS35Base
from gdx_dispatch.routers.sar import get_db, router


@pytest.fixture(autouse=True)
def _registry_reset():
    pii_registry.clear_registry()
    pii_registry.register_pii_field("identities", "email", "contact")
    pii_registry.register_pii_field("identities", "phone", "contact")
    yield
    pii_registry.clear_registry()


@pytest.fixture(autouse=True)
def _sar_build_ready(monkeypatch):
    # The SQLite harness has no RLS, so the silent-incomplete-export
    # condition the prod fence guards against cannot occur here. Enable
    # filing so the create→download flow stays covered. The fence itself
    # is pinned by test_request_sar_fenced_when_not_production_ready,
    # which unsets this in its own body.
    monkeypatch.setenv("SAR_BUILD_PRODUCTION_READY", "1")
    yield


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SS35Base.metadata.create_all(engine)
    md = MetaData()
    identities = Table(
        "identities", md,
        Column("id", String, primary_key=True),
        Column("email", String),
        Column("phone", String),
    )
    md.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    s.execute(insert(identities).values(id="i-1", email="a@x.com", phone="555-0100"))
    s.execute(insert(identities).values(id="i-2", email="b@x.com", phone="555-0200"))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _app(db, *, identity_id="i-1", role=None):
    app = FastAPI()

    # Principal still rides on request.state (set by auth middleware in
    # prod); the DB session flows through the get_db dependency override
    # — the SAME mechanism app.py wires to get_db, so this
    # harness exercises the real prod path. The prior request.state.db
    # injection masked the prod wiring gap (feedback_test_prod_token_parity).
    @app.middleware("http")
    async def inject(request: Request, call_next):
        request.state.principal_identity_id = identity_id
        request.state.principal_role = role
        return await call_next(request)

    app.dependency_overrides[get_db] = lambda: db
    app.include_router(router)
    return app


def test_self_sar_request_completes_synchronously(db):
    c = TestClient(_app(db, identity_id="i-1"))
    r = c.post("/api/sar/request", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert "sar_id" in body
    assert "download_url" in body
    assert "token=" in body["download_url"]


def test_on_behalf_sar_rejected_for_non_super_admin(db):
    c = TestClient(_app(db, identity_id="i-1", role="admin"))
    r = c.post("/api/sar/request", json={"target_identity_id": "i-2"})
    assert r.status_code == 403


def test_on_behalf_sar_accepted_for_super_admin(db):
    c = TestClient(_app(db, identity_id="u-super", role="super-admin"))
    r = c.post("/api/sar/request", json={"target_identity_id": "i-2"})
    assert r.status_code == 200, r.text
    sid = r.json()["sar_id"]
    r2 = c.get(f"/api/sar/{sid}/status")
    assert r2.status_code == 200
    assert r2.json()["target_identity_id"] == "i-2"


def test_download_returns_export_once(db):
    c = TestClient(_app(db, identity_id="i-1"))
    r = c.post("/api/sar/request", json={})
    dl = r.json()["download_url"]
    r1 = c.get(dl)
    assert r1.status_code == 200, r1.text
    assert r1.json()["identity_id"] == "i-1"
    # Second redemption 410
    r2 = c.get(dl)
    assert r2.status_code == 410


def test_download_rejects_bad_token(db):
    c = TestClient(_app(db, identity_id="i-1"))
    r = c.post("/api/sar/request", json={})
    sid = r.json()["sar_id"]
    r2 = c.get(f"/api/sar/{sid}/download?token=bogus.garbage")
    assert r2.status_code == 403


def test_status_hidden_from_other_identities(db):
    c1 = TestClient(_app(db, identity_id="i-1"))
    r = c1.post("/api/sar/request", json={})
    sid = r.json()["sar_id"]
    c2 = TestClient(_app(db, identity_id="i-3"))  # different identity
    r2 = c2.get(f"/api/sar/{sid}/status")
    assert r2.status_code == 404


def test_no_principal_401(db):
    app = FastAPI()
    # No principal on request.state → 401, even though the DB session
    # resolves and filing is enabled. Proves the auth gate fires before
    # both the DB touch and the production-ready fence.
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(router)
    c = TestClient(app)
    r = c.post("/api/sar/request", json={})
    assert r.status_code == 401


def test_request_sar_fenced_when_not_production_ready(db, monkeypatch):
    """Prod default: SAR_BUILD_PRODUCTION_READY unset → filing 501s so
    no silently-incomplete GDPR Art. 15 export is ever emitted. The
    autouse fixture sets the flag; this test removes it to assert the
    real prod posture. Auth still wins over the fence (separate test)."""
    monkeypatch.delenv("SAR_BUILD_PRODUCTION_READY", raising=False)
    c = TestClient(_app(db, identity_id="i-1"))
    r = c.post("/api/sar/request", json={})
    assert r.status_code == 501
    assert "not production-ready" in r.json()["detail"]
