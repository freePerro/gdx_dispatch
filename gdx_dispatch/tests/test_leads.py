"""Tests for the leads router (landing leads + sales pipeline)."""
from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.leads import Lead, router

# Prod-parity: `sub`/`user_id` are UUIDs in production. require_permission
# (added in D-leads-authz-sweep) does SELECT users.role WHERE users.id=:id
# against a UUID column — a literal "viewer-1" string crashes the UUID
# bind processor on SQLite. Route shape-load-bearing ids through these
# constants, never inline literals (feedback_centralized_test_identifiers).
_ADMIN_UID = "00000000-0000-0000-0000-0000000000a1"
_VIEWER_UID = "00000000-0000-0000-0000-0000000000e1"


def _make_client(tenant_id: str = "tenant-test", create_customers_table: bool = True) -> TestClient:
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
            VALUES (:id, :tid, 'customers', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g1-{tenant_id}", "tid": tenant_id},
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'customers', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
    if create_customers_table:
        setup.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    name TEXT,
                    email TEXT,
                    phone TEXT,
                    address TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
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
        "user_id": _ADMIN_UID,
        "sub": _ADMIN_UID,
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


# ---------------------------------------------------------------------------
# Landing leads
# ---------------------------------------------------------------------------


def test_create_landing_lead(client: TestClient):
    r = client.post(
        "/api/landing-leads",
        json={
            "name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "555-0100",
            "source": "website",
            "message": "Broken spring",
            "utm_campaign": "spring-promo",
            "utm_source": "google",
            "utm_medium": "cpc",
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["name"] == "Jane Doe"
    assert data["status"] == "new"
    assert data["company_id"] == "tenant-test"
    assert data["utm_campaign"] == "spring-promo"
    assert data["contacted_at"] is None


def test_landing_lead_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        c1.post("/api/landing-leads", json={"name": "A-only", "source": "website"})
        c2.post("/api/landing-leads", json={"name": "B-only", "source": "website"})
        list_a = c1.get("/api/landing-leads").json()
        list_b = c2.get("/api/landing-leads").json()
        assert len(list_a) == 1
        assert len(list_b) == 1
        assert list_a[0]["name"] == "A-only"
        assert list_b[0]["name"] == "B-only"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_update_landing_lead_status_sets_contacted_at(client: TestClient):
    created = client.post(
        "/api/landing-leads", json={"name": "Test", "source": "website"}
    ).json()
    r = client.patch(
        f"/api/landing-leads/{created['id']}/status",
        json={"status": "contacted"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "contacted"
    assert data["contacted_at"] is not None


def test_delete_landing_lead_soft_deletes_and_hides_from_list(client: TestClient):
    created = client.post(
        "/api/landing-leads",
        json={"name": "Spam Bot", "email": "spam@example.com", "source": "website"},
    ).json()
    ll_id = created["id"]

    # Visible before delete
    before = client.get("/api/landing-leads").json()
    assert any(r["id"] == ll_id for r in before)

    # Delete with spam reason
    r = client.delete(f"/api/landing-leads/{ll_id}?reason=spam")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == ll_id
    assert body["deleted_at"]

    # Hidden after delete (deleted_at IS NULL filter excludes it)
    after = client.get("/api/landing-leads").json()
    assert not any(r["id"] == ll_id for r in after)


def test_delete_landing_lead_404_when_not_found(client: TestClient):
    import uuid as _uuid
    r = client.delete(f"/api/landing-leads/{_uuid.uuid4()}")
    assert r.status_code == 404


def test_delete_landing_lead_idempotent_second_call_is_404(client: TestClient):
    """Audit §3 + idempotency: a second DELETE on an already-soft-deleted row
    must 404 (not silently 200) so the UI doesn't double-toast and the audit
    log doesn't get a misleading second 'deleted' entry."""
    created = client.post(
        "/api/landing-leads",
        json={"name": "Twice", "source": "website"},
    ).json()
    ll_id = created["id"]

    r1 = client.delete(f"/api/landing-leads/{ll_id}?reason=spam")
    assert r1.status_code == 200

    r2 = client.delete(f"/api/landing-leads/{ll_id}?reason=spam")
    assert r2.status_code == 404


def test_delete_landing_lead_rejects_invalid_reason(client: TestClient):
    """Audit §2: reason is Literal['spam','manual'] — anything else 422."""
    created = client.post(
        "/api/landing-leads",
        json={"name": "Bad Reason", "source": "website"},
    ).json()
    ll_id = created["id"]

    r = client.delete(f"/api/landing-leads/{ll_id}?reason=<script>alert(1)</script>")
    assert r.status_code == 422, r.text

    # And the row is still visible (not deleted on the reject path)
    after = client.get("/api/landing-leads").json()
    assert any(row["id"] == ll_id for row in after)


def test_delete_landing_lead_defaults_reason_to_manual(client: TestClient):
    """No `reason` param → defaults to 'manual'. Pinned by audit §2."""
    created = client.post(
        "/api/landing-leads",
        json={"name": "Default Reason", "source": "website"},
    ).json()
    r = client.delete(f"/api/landing-leads/{created['id']}")
    assert r.status_code == 200, r.text


def _as_viewer(tc):
    """Swap the auth override to a read-only viewer (leads.read only)."""
    tc.app.dependency_overrides[get_current_user] = lambda: {
        "user_id": _VIEWER_UID,
        "sub": _VIEWER_UID,
        "role": "viewer",
        "tenant_id": "tenant-test",
    }


def test_delete_landing_lead_403_for_unauthorized_role():
    """D-leads-authz-sweep: viewer (leads.read only) must NOT soft-delete
    a landing lead. Seed as admin (leads.write), then swap to viewer for
    the gated DELETE. HTTP-level per feedback_require_min_role_is_broken."""
    tc = _make_client(tenant_id="tenant-test")
    try:
        created = tc.post(
            "/api/landing-leads",
            json={"name": "viewer-cannot-delete-me", "source": "website"},
        ).json()
        _as_viewer(tc)
        r = tc.delete(f"/api/landing-leads/{created['id']}?reason=spam")
        assert r.status_code == 403, r.text
    finally:
        tc.app.dependency_overrides.clear()
        tc._engine.dispose()  # type: ignore[attr-defined]


def test_delete_lead_403_for_unauthorized_role():
    """D-leads-authz-sweep: DELETE /api/leads/{id} is a destructive
    soft-delete of a pipeline Lead. A viewer must get 403, same OWASP
    API5 BFLA gate as DELETE /api/landing-leads. Seed as admin, swap to
    viewer. HTTP-level per feedback_require_min_role_is_broken."""
    tc = _make_client(tenant_id="tenant-test")
    try:
        created = tc.post("/api/leads", json=_lead_payload()).json()
        _as_viewer(tc)
        r = tc.delete(f"/api/leads/{created['id']}")
        assert r.status_code == 403, r.text
    finally:
        tc.app.dependency_overrides.clear()
        tc._engine.dispose()  # type: ignore[attr-defined]


def test_leads_authz_matrix_boundaries():
    """D-leads-authz-sweep matrix: viewer reads but cannot write;
    technician cannot even read. Pins the per-verb permission model
    HTTP-level so an inert gate or BUILTIN_ROLES drift is caught."""
    tc = _make_client(tenant_id="tenant-test")
    try:
        created = tc.post("/api/leads", json=_lead_payload()).json()  # admin seed
        # viewer: read OK, write/delete 403
        _as_viewer(tc)
        assert tc.get("/api/leads").status_code == 200
        assert tc.get(f"/api/leads/{created['id']}").status_code == 200
        assert tc.post("/api/leads", json=_lead_payload(name="nope")).status_code == 403
        assert tc.patch(f"/api/leads/{created['id']}", json={"notes": "x"}).status_code == 403
        # technician: excluded from the pipeline entirely — read 403
        tc.app.dependency_overrides[get_current_user] = lambda: {
            "user_id": _VIEWER_UID, "sub": _VIEWER_UID,
            "role": "technician", "tenant_id": "tenant-test",
        }
        assert tc.get("/api/leads").status_code == 403
    finally:
        tc.app.dependency_overrides.clear()
        tc._engine.dispose()  # type: ignore[attr-defined]


def test_convert_landing_lead_to_lead(client: TestClient):
    created = client.post(
        "/api/landing-leads",
        json={
            "name": "Promote Me",
            "email": "promote@example.com",
            "phone": "555-0200",
            "source": "google-ads",
            "message": "Need new opener",
        },
    ).json()
    r = client.post(f"/api/landing-leads/{created['id']}/convert-to-lead")
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["name"] == "Promote Me"
    assert lead["email"] == "promote@example.com"
    assert lead["landing_lead_id"] == created["id"]
    assert lead["stage"] == "new"
    assert lead["company_id"] == "tenant-test"


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


def _lead_payload(**overrides) -> dict:
    base = {
        "name": "Acme Corp",
        "email": "contact@acme.test",
        "phone": "555-0300",
        "address": "123 Main St",
        "stage": "new",
        "estimated_value": 1500.0,
        "source": "referral",
        "assigned_to": "rep-alice",
        "notes": "Spring broke on their garage",
    }
    base.update(overrides)
    return base


def test_create_lead(client: TestClient):
    r = client.post("/api/leads", json=_lead_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["name"] == "Acme Corp"
    assert data["stage"] == "new"
    assert data["estimated_value"] == 1500.0
    assert data["company_id"] == "tenant-test"
    assert data["assigned_to"] == "rep-alice"


def test_advance_stage(client: TestClient):
    created = client.post("/api/leads", json=_lead_payload()).json()
    r = client.post(
        f"/api/leads/{created['id']}/advance-stage",
        json={"stage": "contacted"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["stage"] == "contacted"
    assert data["last_contact_at"] is not None

    r2 = client.post(
        f"/api/leads/{created['id']}/advance-stage",
        json={"stage": "quoted"},
    )
    assert r2.status_code == 200
    assert r2.json()["stage"] == "quoted"


def test_bad_stage_rejected(client: TestClient):
    created = client.post("/api/leads", json=_lead_payload()).json()
    r = client.post(
        f"/api/leads/{created['id']}/advance-stage",
        json={"stage": "totally-invalid"},
    )
    assert r.status_code == 422


def test_pipeline_summary_returns_counts(client: TestClient):
    client.post("/api/leads", json=_lead_payload(name="L1", stage="new"))
    client.post("/api/leads", json=_lead_payload(name="L2", stage="new"))
    client.post("/api/leads", json=_lead_payload(name="L3", stage="qualified"))
    client.post("/api/leads", json=_lead_payload(name="L4", stage="quoted"))

    r = client.get("/api/leads/pipeline-summary")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["new"] == 2
    assert data["qualified"] == 1
    assert data["quoted"] == 1
    assert data["won"] == 0
    assert data["lost"] == 0


def test_soft_delete_lead(client: TestClient):
    created = client.post("/api/leads", json=_lead_payload()).json()
    r = client.delete(f"/api/leads/{created['id']}")
    assert r.status_code == 204

    gone = client.get(f"/api/leads/{created['id']}")
    assert gone.status_code == 404

    listed = client.get("/api/leads").json()
    assert all(l["id"] != created["id"] for l in listed)

    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        row = db.execute(
            select(Lead).where(Lead.id == UUID(created["id"]))
        ).scalar_one()
        assert row.deleted_at is not None
    finally:
        db.close()


def test_convert_to_customer_graceful_when_customers_table_missing():
    # With centralized ORM models, create_all() always creates the customers
    # table. This test now verifies conversion succeeds (the missing-table
    # scenario is no longer possible).
    tc = _make_client(tenant_id="tenant-nocust", create_customers_table=False)
    try:
        created = tc.post("/api/leads", json=_lead_payload(name="No Cust Table")).json()
        r = tc.post(f"/api/leads/{created['id']}/convert-to-customer")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["converted"] is True
        assert data["customer_id"] is not None
        assert data["lead_id"] == created["id"]
    finally:
        tc.app.dependency_overrides.clear()
        tc._engine.dispose()  # type: ignore[attr-defined]
