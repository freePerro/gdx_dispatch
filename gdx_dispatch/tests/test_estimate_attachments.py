from __future__ import annotations

import io
import os
import tempfile
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.estimates import router


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    _setup_db = Session()
    _setup_db.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT
        )
    """))
    _setup_db.execute(text("""
        CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key)
        )
    """))
    _setup_db.execute(text(
        "INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at) "
        "VALUES ('g1', 'tenant-test', 'estimates', datetime('now'), datetime('now'))"
    ))
    _setup_db.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
        "VALUES ('g2', 'tenant-test', 'estimates', datetime('now'), datetime('now'))"
    ))
    _setup_db.commit()
    _setup_db.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "sub": "user-1",
        "name": "Tester",
        "role": "admin",
        "tenant_id": "tenant-test",
    }

    tc = TestClient(app, raise_server_exceptions=True)
    yield tc
    app.dependency_overrides.clear()


def _create_estimate(client: TestClient) -> str:
    with next(client.app.dependency_overrides[get_db]()) as db:
        c = Customer(name="Acme", email="x@y.com", company_id="tenant-test")
        db.add(c)
        db.commit()
        db.refresh(c)
        cid = str(c.id)
    r = client.post("/api/estimates", json={"customer_id": cid, "label": "Test"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_upload_list_download_delete_attachment(client: TestClient):
    eid = _create_estimate(client)

    # Upload a tiny PNG.
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    r = client.post(
        f"/api/estimates/{eid}/attachments",
        files={"file": ("photo.png", io.BytesIO(png), "image/png")},
    )
    assert r.status_code == 201, r.text
    att = r.json()
    assert att["original_name"] == "photo.png"
    assert att["content_type"] == "image/png"
    assert att["file_size"] == len(png)
    assert att["download_url"].endswith(f"/{att['id']}/download")

    # List shows it.
    r = client.get(f"/api/estimates/{eid}/attachments")
    assert r.status_code == 200
    assert any(a["id"] == att["id"] for a in r.json())

    # Download returns the bytes.
    r = client.get(f"/api/estimates/{eid}/attachments/{att['id']}/download")
    assert r.status_code == 200
    assert r.content == png

    # Delete removes it from the list.
    r = client.delete(f"/api/estimates/{eid}/attachments/{att['id']}")
    assert r.status_code == 200
    r = client.get(f"/api/estimates/{eid}/attachments")
    assert all(a["id"] != att["id"] for a in r.json())


def test_upload_rejects_unsupported_mime(client: TestClient):
    eid = _create_estimate(client)
    r = client.post(
        f"/api/estimates/{eid}/attachments",
        files={"file": ("x.exe", io.BytesIO(b"MZ"), "application/x-msdownload")},
    )
    assert r.status_code == 415


def test_upload_to_missing_estimate_404(client: TestClient):
    r = client.post(
        "/api/estimates/00000000-0000-0000-0000-000000000000/attachments",
        files={"file": ("a.png", io.BytesIO(b"x"), "image/png")},
    )
    assert r.status_code == 404
