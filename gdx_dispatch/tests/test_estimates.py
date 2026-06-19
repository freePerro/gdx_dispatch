from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Job
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.estimates import router


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from sqlalchemy import text
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    # Create module grants tables needed by require_module
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
    _setup_db.execute(text("""
        INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)
        VALUES ('g1', 'tenant-test', 'estimates', datetime('now'), datetime('now'))
    """))
    _setup_db.execute(text("""
        INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
        VALUES ('g2', 'tenant-test', 'estimates', datetime('now'), datetime('now'))
    """))
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
        "role": "admin",
        "tenant_id": "tenant-test",
    }

    tc = TestClient(app, raise_server_exceptions=True)
    yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def _create_customer(client: TestClient, name: str = "Acme Customer") -> str:
    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        row = Customer(name=name, email="customer@example.com", company_id="tenant-test")
        db.add(row)
        db.commit()
        db.refresh(row)
        return str(row.id)
    finally:
        db.close()


def _create_job(client: TestClient, customer_id: str | None = None, title: str = "Garage Repair") -> str:
    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        row = Job(title=title, customer_id=UUID(customer_id) if customer_id else None, company_id="tenant-test")
        db.add(row)
        db.commit()
        db.refresh(row)
        return str(row.id)
    finally:
        db.close()


def _create_estimate(client: TestClient, **payload_overrides) -> dict:
    customer_id = payload_overrides.pop("customer_id", None) or _create_customer(client)
    payload = {
        "customer_id": customer_id,
        "label": "Initial Estimate",
        "notes": "Check spring and opener",
    }
    payload.update(payload_overrides)
    r = client.post("/api/estimates", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_estimate_with_job_derives_customer(client: TestClient):
    customer_id = _create_customer(client)
    job_id = _create_job(client, customer_id=customer_id)

    r = client.post(
        "/api/estimates",
        json={"job_id": job_id, "label": "Job Estimate", "notes": "Door panel replacement"},
    )

    assert r.status_code == 201, r.text
    data = r.json()
    assert data["job_id"] == job_id
    assert data["customer_id"] == customer_id
    assert data["status"] == "draft"
    assert data["total"] == 0.0


def test_create_estimate_with_customer_only(client: TestClient):
    customer_id = _create_customer(client)

    r = client.post(
        "/api/estimates",
        json={"customer_id": customer_id, "label": "Standalone", "notes": "No job yet"},
    )

    assert r.status_code == 201, r.text
    data = r.json()
    assert data["customer_id"] == customer_id
    assert data["job_id"] is None
    assert data["label"] == "Standalone"


def test_create_estimate_requires_job_or_customer(client: TestClient):
    r = client.post("/api/estimates", json={"label": "Invalid"})
    assert r.status_code == 400
    assert "job_id or customer_id" in r.json()["detail"]


def test_create_estimate_404_for_missing_job(client: TestClient):
    r = client.post(
        "/api/estimates",
        json={"job_id": str(uuid4()), "label": "Missing Job"},
    )
    assert r.status_code == 404


def test_create_estimate_404_for_missing_customer(client: TestClient):
    r = client.post(
        "/api/estimates",
        json={"customer_id": str(uuid4()), "label": "Missing Customer"},
    )
    assert r.status_code == 404


def test_list_estimates_and_filter_by_job(client: TestClient):
    customer_id = _create_customer(client)
    job_a = _create_job(client, customer_id=customer_id, title="Job A")
    job_b = _create_job(client, customer_id=customer_id, title="Job B")

    e1 = _create_estimate(client, job_id=job_a, customer_id=None, label="A1")
    _create_estimate(client, job_id=job_b, customer_id=None, label="B1")

    all_r = client.get("/api/estimates")
    assert all_r.status_code == 200, all_r.text
    assert len(all_r.json()) == 2

    filt_r = client.get("/api/estimates", params={"job_id": job_a})
    assert filt_r.status_code == 200, filt_r.text
    items = filt_r.json()
    assert len(items) == 1
    assert items[0]["id"] == e1["id"]


def test_get_estimate_includes_lines(client: TestClient):
    estimate = _create_estimate(client)

    line_r = client.post(
        f"/api/estimates/{estimate['id']}/lines",
        json={"description": "Spring", "quantity": 2, "unit_price": 120.5},
    )
    assert line_r.status_code == 201, line_r.text

    r = client.get(f"/api/estimates/{estimate['id']}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"] == estimate["id"]
    assert len(data["lines"]) == 1
    assert data["lines"][0]["description"] == "Spring"


def test_patch_estimate_updates_fields(client: TestClient):
    estimate = _create_estimate(client, label="Old", notes="Old note")

    r = client.patch(
        f"/api/estimates/{estimate['id']}",
        json={"label": "New", "notes": "Updated note"},
    )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["label"] == "New"
    assert data["notes"] == "Updated note"


def test_create_estimate_persists_description(client: TestClient):
    estimate = _create_estimate(client, description="Replace 16x7 steel door, paint white")
    r = client.get(f"/api/estimates/{estimate['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "Replace 16x7 steel door, paint white"


def test_line_category_round_trips_through_create_and_post(client: TestClient):
    customer_id = _create_customer(client)
    # create with nested line carrying a category
    r = client.post(
        "/api/estimates",
        json={
            "customer_id": customer_id,
            "label": "Cat test",
            "line_items": [
                {"category": "Springs", "description": "Torsion spring", "quantity": 1, "unit_price": 200},
            ],
        },
    )
    assert r.status_code == 201, r.text
    eid = r.json()["id"]

    g = client.get(f"/api/estimates/{eid}")
    assert g.status_code == 200
    lines = g.json().get("lines") or []
    assert lines and lines[0]["category"] == "Springs"

    # now post a fresh line with a different category — also persists.
    r2 = client.post(
        f"/api/estimates/{eid}/lines",
        json={"category": "Openers", "description": "LiftMaster", "quantity": 1, "unit_price": 450},
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["category"] == "Openers"


def test_patch_estimate_updates_description(client: TestClient):
    estimate = _create_estimate(client, description="Initial scope")
    r = client.patch(
        f"/api/estimates/{estimate['id']}",
        json={"description": "Revised scope: also new opener"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "Revised scope: also new opener"
    # Survives a reload.
    r2 = client.get(f"/api/estimates/{estimate['id']}")
    assert r2.json()["description"] == "Revised scope: also new opener"


def test_patch_estimate_rejects_missing_estimate(client: TestClient):
    r = client.patch(f"/api/estimates/{uuid4()}", json={"label": "X"})
    assert r.status_code == 404


def test_delete_estimate_soft_delete_and_excludes_from_list(client: TestClient):
    estimate = _create_estimate(client)

    r = client.delete(f"/api/estimates/{estimate['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    list_r = client.get("/api/estimates")
    assert list_r.status_code == 200
    assert all(e["id"] != estimate["id"] for e in list_r.json())

    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        stored = db.execute(select(Estimate).where(Estimate.id == UUID(estimate["id"]))).scalar_one()
        assert stored.deleted_at is not None
    finally:
        db.close()


def test_get_deleted_estimate_returns_404(client: TestClient):
    estimate = _create_estimate(client)
    del_r = client.delete(f"/api/estimates/{estimate['id']}")
    assert del_r.status_code == 200

    r = client.get(f"/api/estimates/{estimate['id']}")
    assert r.status_code == 404


def test_add_line_item_calculates_line_total_and_estimate_total(client: TestClient):
    estimate = _create_estimate(client)

    l1 = client.post(
        f"/api/estimates/{estimate['id']}/lines",
        json={"description": "Door Panel", "quantity": 2, "unit_price": 99.95},
    )
    assert l1.status_code == 201, l1.text
    assert l1.json()["line_total"] == pytest.approx(199.90)

    l2 = client.post(
        f"/api/estimates/{estimate['id']}/lines",
        json={"description": "Labor", "quantity": 1, "unit_price": 120.0},
    )
    assert l2.status_code == 201, l2.text

    est = client.get(f"/api/estimates/{estimate['id']}").json()
    assert est["total"] == pytest.approx(319.90)


def test_add_line_requires_description(client: TestClient):
    estimate = _create_estimate(client)
    r = client.post(
        f"/api/estimates/{estimate['id']}/lines",
        json={"description": "", "quantity": 1, "unit_price": 10},
    )
    assert r.status_code == 422


def test_add_line_rejects_nonpositive_quantity(client: TestClient):
    estimate = _create_estimate(client)
    r = client.post(
        f"/api/estimates/{estimate['id']}/lines",
        json={"description": "Invalid", "quantity": 0, "unit_price": 10},
    )
    assert r.status_code == 422


def test_patch_line_updates_values_and_recalculates_total(client: TestClient):
    estimate = _create_estimate(client)

    line_r = client.post(
        f"/api/estimates/{estimate['id']}/lines",
        json={"description": "Spring", "quantity": 1, "unit_price": 50},
    )
    line_id = line_r.json()["id"]

    upd = client.patch(
        f"/api/estimates/{estimate['id']}/lines/{line_id}",
        json={"quantity": 3, "unit_price": 45.0},
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["line_total"] == pytest.approx(135.0)

    est = client.get(f"/api/estimates/{estimate['id']}").json()
    assert est["total"] == pytest.approx(135.0)


def test_patch_line_404_for_missing_line(client: TestClient):
    estimate = _create_estimate(client)
    r = client.patch(
        f"/api/estimates/{estimate['id']}/lines/{uuid4()}",
        json={"quantity": 2},
    )
    assert r.status_code == 404


def test_delete_line_removes_and_recalculates_total(client: TestClient):
    estimate = _create_estimate(client)
    line1 = client.post(
        f"/api/estimates/{estimate['id']}/lines",
        json={"description": "Part A", "quantity": 1, "unit_price": 100},
    ).json()
    client.post(
        f"/api/estimates/{estimate['id']}/lines",
        json={"description": "Part B", "quantity": 1, "unit_price": 50},
    )

    r = client.delete(f"/api/estimates/{estimate['id']}/lines/{line1['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    est = client.get(f"/api/estimates/{estimate['id']}").json()
    assert len(est["lines"]) == 1
    assert est["total"] == pytest.approx(50.0)


def test_send_estimate_marks_sent(client: TestClient):
    estimate = _create_estimate(client)

    r = client.post(f"/api/estimates/{estimate['id']}/send")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "sent"
    assert data["sent_at"] is not None


def test_accept_estimate_marks_accepted(client: TestClient):
    estimate = _create_estimate(client)
    client.post(f"/api/estimates/{estimate['id']}/send")

    r = client.post(f"/api/estimates/{estimate['id']}/accept")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "accepted"
    assert data["accepted_at"] is not None


def test_accept_estimate_auto_creates_job(client: TestClient):
    """2026-05-13 directive: an accepted estimate becomes a Job automatically
    (replaces the manual 'Convert to Job' click). Estimate has a customer in
    this fixture; auto-convert should fire and the response includes the new
    job id, AND the new job lands in the 'Order Doors' holding area."""
    # Pre-seed the 'Order Doors' holding area — production tenants get this
    # via gdx_dispatch/tools/migrate_service_call_stage.py before deploy; the test has
    # to construct it explicitly. Without this row the auto-convert silently
    # falls back to holding_area_id=NULL, which is the failure mode the
    # auditor flagged.
    from gdx_dispatch.models.tenant_models import HoldingArea
    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        area = HoldingArea(
            id=str(uuid4()),
            company_id="tenant-test",
            name="Order Doors",
            color="#3b82f6",
            sort_order=3,
        )
        db.add(area); db.commit(); db.refresh(area)
        area_id = area.id
    finally:
        db.close()

    estimate = _create_estimate(client)
    client.post(f"/api/estimates/{estimate['id']}/send")

    r = client.post(f"/api/estimates/{estimate['id']}/accept")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "accepted"
    assert "auto_converted_job_id" in data
    assert "auto_convert_skipped" not in data

    job_id = UUID(data["auto_converted_job_id"])
    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.lifecycle_stage == "scheduled"
        assert job.status == "Scheduled"
        # Sold estimates auto-tag as installs (2026-05-13 directive).
        assert job.job_type == "Installation"
        # The auto-route is the whole point — verify the lane was hit, not
        # silently dropped.
        assert job.holding_area_id == area_id, (
            f"expected holding_area_id={area_id} (Order Doors), got {job.holding_area_id}"
        )
    finally:
        db.close()
    re_accept = client.post(f"/api/estimates/{estimate['id']}/accept")
    assert re_accept.status_code == 409


def test_accept_estimate_already_linked_skips_convert(client: TestClient):
    """An estimate created against an existing Job already has job_id set;
    accept must not double-create. The response surfaces the skip reason so
    the UI knows this isn't a silent failure."""
    job_id = _create_job(client, customer_id=_create_customer(client))
    estimate = _create_estimate(client, job_id=job_id)
    r = client.post(f"/api/estimates/{estimate['id']}/accept")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "accepted"
    assert data.get("auto_convert_skipped") == "already_linked"
    assert "auto_converted_job_id" not in data


def test_decline_estimate_marks_declined_and_reason(client: TestClient):
    estimate = _create_estimate(client)

    r = client.post(
        f"/api/estimates/{estimate['id']}/decline",
        json={"reason": "Price too high"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "declined"
    assert data["declined_at"] is not None
    assert data["declined_reason"] == "Price too high"


def test_cannot_accept_declined_estimate(client: TestClient):
    estimate = _create_estimate(client)
    decline = client.post(f"/api/estimates/{estimate['id']}/decline", json={"reason": "No"})
    assert decline.status_code == 200

    r = client.post(f"/api/estimates/{estimate['id']}/accept")
    assert r.status_code == 409


def test_cannot_decline_accepted_estimate(client: TestClient):
    estimate = _create_estimate(client)
    accept = client.post(f"/api/estimates/{estimate['id']}/accept")
    assert accept.status_code == 200

    r = client.post(f"/api/estimates/{estimate['id']}/decline", json={"reason": "late"})
    assert r.status_code == 409


def test_cannot_edit_finalized_estimate_or_lines(client: TestClient):
    estimate = _create_estimate(client)
    line = client.post(
        f"/api/estimates/{estimate['id']}/lines",
        json={"description": "Part", "quantity": 1, "unit_price": 10},
    ).json()
    accept = client.post(f"/api/estimates/{estimate['id']}/accept")
    assert accept.status_code == 200

    patch_est = client.patch(f"/api/estimates/{estimate['id']}", json={"label": "x"})
    assert patch_est.status_code == 409

    patch_line = client.patch(
        f"/api/estimates/{estimate['id']}/lines/{line['id']}",
        json={"quantity": 2},
    )
    assert patch_line.status_code == 409

    del_line = client.delete(f"/api/estimates/{estimate['id']}/lines/{line['id']}")
    assert del_line.status_code == 409


def test_send_accept_decline_404_for_missing_estimate(client: TestClient):
    missing = str(uuid4())
    assert client.post(f"/api/estimates/{missing}/send").status_code == 404
    assert client.post(f"/api/estimates/{missing}/accept").status_code == 404
    assert client.post(f"/api/estimates/{missing}/decline", json={"reason": "x"}).status_code == 404


def test_line_delete_404_for_missing_estimate(client: TestClient):
    r = client.delete(f"/api/estimates/{uuid4()}/lines/{uuid4()}")
    assert r.status_code == 404


def test_list_excludes_deleted_and_returns_latest_first(client: TestClient):
    e1 = _create_estimate(client, label="First")
    e2 = _create_estimate(client, label="Second")

    del_r = client.delete(f"/api/estimates/{e1['id']}")
    assert del_r.status_code == 200

    r = client.get("/api/estimates")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()]
    assert e1["id"] not in ids
    assert ids[0] == e2["id"]


def test_rounding_uses_two_decimal_places(client: TestClient):
    """unit_price quantizes to 2dp ROUND_HALF_UP; line_total = qty × stored unit_price.

    Sprint 1.0.5 corrected the prior off-by-one: 19.995 stores as 20.00 (round
    half up), so 3 × 20.00 = 60.00. Old behavior stored unit_price=20.00 but
    computed line_total from the raw 19.995 → 59.99, which violated the
    invariant `line_total == quantity × stored unit_price` (customer-visible
    math didn't reconcile)."""
    estimate = _create_estimate(client)

    line = client.post(
        f"/api/estimates/{estimate['id']}/lines",
        json={"description": "Rounding", "quantity": 3, "unit_price": 19.995},
    )
    assert line.status_code == 201, line.text
    body = line.json()
    assert body["unit_price"] == pytest.approx(20.00)
    assert body["line_total"] == pytest.approx(60.00)
    # Reconciliation invariant — what the customer sees must add up
    assert body["line_total"] == pytest.approx(body["quantity"] * body["unit_price"])

    est = client.get(f"/api/estimates/{estimate['id']}").json()
    assert est["total"] == pytest.approx(60.00)


def test_estimate_line_persisted_in_db(client: TestClient):
    estimate = _create_estimate(client)
    line = client.post(
        f"/api/estimates/{estimate['id']}/lines",
        json={"description": "Persisted", "quantity": 2, "unit_price": 10},
    ).json()

    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        row = db.execute(select(EstimateLine).where(EstimateLine.id == UUID(line["id"]))).scalar_one_or_none()
        assert row is not None
        assert str(row.estimate_id) == estimate["id"]
    finally:
        db.close()
