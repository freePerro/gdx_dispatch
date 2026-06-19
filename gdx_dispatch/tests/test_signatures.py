"""Tests for the signatures router (in-person + remote signing flows)."""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.signatures import DocumentSignature, admin_router, public_router


def _make_client(
    tenant_id: str = "tenant-test",
    user_sub: str = "user-1",
    user_role: str = "admin",
    engine=None,
) -> TestClient:
    if engine is None:
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
            CREATE TABLE IF NOT EXISTS tenant_module_grants (
                id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT
            )
            """
        )
    )
    setup.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS company_module_grants (
                id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT,
                UNIQUE(company_id, module_key)
            )
            """
        )
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'estimates', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g1-{tenant_id}", "tid": tenant_id},
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'estimates', datetime('now'), datetime('now'))
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

    # Public routes first so `/api/signatures/token/{token}` matches before the
    # admin `/api/signatures/{document_type}/{document_id}` path.
    app.include_router(public_router)
    app.include_router(admin_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": user_sub,
        "sub": user_sub,
        "role": user_role,
        "tenant_id": tenant_id,
        "email": f"{user_sub}@example.com",
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    tc._session = Session  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


# A tiny 1x1 transparent PNG base64 (just as plausible signature payload)
SIG_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
    "nGNgAAIAAAUAAarVyFEAAAAASUVORK5CYII="
)


def test_in_person_signature_creates_signed_record(client: TestClient):
    doc_id = str(uuid4())
    r = client.post(
        "/api/signatures",
        json={
            "document_type": "estimate",
            "document_id": doc_id,
            "signature_data": SIG_PNG_B64,
            "signed_by": "Jane Customer",
            "signed_by_email": "jane@example.com",
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["status"] == "signed"
    assert data["signed_at"] is not None
    assert data["signed_by"] == "Jane Customer"
    assert data["document_type"] == "estimate"
    assert data["document_id"] == doc_id
    assert data["company_id"] == "tenant-test"


def test_list_pending_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a", user_sub="ua")
    c2 = _make_client(tenant_id="tenant-b", user_sub="ub")
    try:
        # Tenant A creates a pending remote request
        r1 = c1.post(
            "/api/signatures/request-remote",
            json={
                "document_type": "invoice",
                "document_id": "doc-A",
                "customer_email": "a@example.com",
            },
        )
        assert r1.status_code == 201, r1.text
        # Tenant B creates its own
        r2 = c2.post(
            "/api/signatures/request-remote",
            json={
                "document_type": "invoice",
                "document_id": "doc-B",
                "customer_email": "b@example.com",
            },
        )
        assert r2.status_code == 201

        list_a = c1.get("/api/signatures/pending").json()
        list_b = c2.get("/api/signatures/pending").json()
        assert len(list_a) == 1 and list_a[0]["document_id"] == "doc-A"
        assert len(list_b) == 1 and list_b[0]["document_id"] == "doc-B"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_request_remote_generates_token_and_expiry(client: TestClient):
    before = utcnow()
    r = client.post(
        "/api/signatures/request-remote",
        json={
            "document_type": "work_order",
            "document_id": "wo-123",
            "customer_email": "cust@example.com",
            "expires_days": 7,
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["token"] and len(data["token"]) >= 40
    assert data["status"] == "pending"
    assert data["signing_url"].endswith(data["token"])
    # Parse expires_at — should be ~7 days out
    from datetime import datetime
    expires = datetime.fromisoformat(data["expires_at"])
    delta = expires - before
    assert timedelta(days=6, hours=23) <= delta <= timedelta(days=7, hours=1)


def test_public_get_by_token_returns_document(client: TestClient):
    created = client.post(
        "/api/signatures/request-remote",
        json={
            "document_type": "estimate",
            "document_id": "est-99",
            "customer_email": "c@example.com",
        },
    ).json()
    token = created["token"]

    # Strip auth overrides to prove public access doesn't need a logged-in user
    r = client.get(f"/api/signatures/token/{token}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["document_type"] == "estimate"
    assert data["document_id"] == "est-99"
    assert data["status"] == "pending"
    # Public view must NOT leak company_id / signature_data
    assert "company_id" not in data
    assert "signature_data" not in data


def test_public_sign_by_token_marks_signed(client: TestClient):
    created = client.post(
        "/api/signatures/request-remote",
        json={
            "document_type": "completion",
            "document_id": "job-42",
            "customer_email": "c@example.com",
        },
    ).json()
    token = created["token"]

    r = client.post(
        f"/api/signatures/token/{token}",
        json={
            "signature_data": SIG_PNG_B64,
            "signed_by": "Remote Customer",
            "signed_by_email": "c@example.com",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "signed"

    # Verify via admin endpoint
    listed = client.get("/api/signatures/completion/job-42").json()
    assert len(listed) == 1
    assert listed[0]["status"] == "signed"
    assert listed[0]["signed_by"] == "Remote Customer"
    assert listed[0]["signature_data"] == SIG_PNG_B64


def test_public_sign_expired_token_returns_404(client: TestClient):
    created = client.post(
        "/api/signatures/request-remote",
        json={
            "document_type": "estimate",
            "document_id": "exp-1",
            "customer_email": "x@example.com",
        },
    ).json()
    token = created["token"]

    # Force expiry directly in the DB
    Session = client._session  # type: ignore[attr-defined]
    s = Session()
    try:
        row = s.query(DocumentSignature).filter(DocumentSignature.token == token).one()
        row.token_expires_at = utcnow() - timedelta(days=1)
        s.commit()
    finally:
        s.close()

    r = client.post(
        f"/api/signatures/token/{token}",
        json={"signature_data": SIG_PNG_B64, "signed_by": "Late Cust"},
    )
    assert r.status_code == 404


def test_public_sign_reused_token_returns_404(client: TestClient):
    created = client.post(
        "/api/signatures/request-remote",
        json={
            "document_type": "invoice",
            "document_id": "inv-1",
            "customer_email": "y@example.com",
        },
    ).json()
    token = created["token"]

    r1 = client.post(
        f"/api/signatures/token/{token}",
        json={"signature_data": SIG_PNG_B64, "signed_by": "First"},
    )
    assert r1.status_code == 200

    r2 = client.post(
        f"/api/signatures/token/{token}",
        json={"signature_data": SIG_PNG_B64, "signed_by": "Second"},
    )
    assert r2.status_code == 404


def test_admin_cancel_signature(client: TestClient):
    created = client.post(
        "/api/signatures/request-remote",
        json={
            "document_type": "estimate",
            "document_id": "cancel-me",
            "customer_email": "z@example.com",
        },
    ).json()
    sig_id = created["id"]

    r = client.post(f"/api/signatures/{sig_id}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"

    # Pending list should no longer contain it
    pending = client.get("/api/signatures/pending").json()
    assert all(s["id"] != sig_id for s in pending)


def test_document_type_validation(client: TestClient):
    r = client.post(
        "/api/signatures",
        json={
            "document_type": "not_a_type",
            "document_id": "x",
            "signature_data": SIG_PNG_B64,
            "signed_by": "Jane",
        },
    )
    assert r.status_code == 422
