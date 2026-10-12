"""Tests for the tenant onboarding wizard router."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.onboarding import ONBOARDING_STEPS, router


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
            "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"
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


def test_get_state_creates_default(client: TestClient):
    r = client.get("/api/onboarding/state")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["company_id"] == "tenant-test"
    assert data["current_step"] == "profile"
    assert data["completed_steps"] == []
    assert data["catalog_seeded"] is False
    assert data["demo_data_loaded"] is False
    assert data["completed_at"] is None


def test_advance_step(client: TestClient):
    # Initial state starts at "profile"
    client.get("/api/onboarding/state")
    r = client.post("/api/onboarding/step", json={"step": "catalog"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["current_step"] == "catalog"
    assert "profile" in data["completed_steps"]

    r2 = client.post("/api/onboarding/step", json={"step": "technicians"})
    assert r2.status_code == 200
    assert r2.json()["current_step"] == "technicians"
    assert "catalog" in r2.json()["completed_steps"]


def test_invalid_step_rejected(client: TestClient):
    r = client.post("/api/onboarding/step", json={"step": "bogus"})
    assert r.status_code == 422


def test_seed_catalog_idempotent(client: TestClient):
    r1 = client.post("/api/onboarding/seed-catalog")
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    # Either tables exist (items_created>0) or schema missing — both are acceptable
    # per spec. But state should advance consistently on success path.
    if d1.get("seeded"):
        assert d1["items_created"] >= 1
        # Second call is a no-op
        r2 = client.post("/api/onboarding/seed-catalog")
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["items_created"] == 0
        assert d2.get("already_seeded") is True
    else:
        # schema missing — should not crash and report error
        assert "error" in d1


def test_complete_sets_timestamp(client: TestClient):
    r = client.post("/api/onboarding/complete")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["current_step"] == "done"
    assert data["completed_at"] is not None
    assert "done" in data["completed_steps"]


def test_checklist_shape(client: TestClient):
    r = client.get("/api/onboarding/checklist")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == len(ONBOARDING_STEPS)
    for entry in data:
        assert set(entry.keys()) >= {"step", "label", "completed"}
        assert entry["step"] in ONBOARDING_STEPS
        assert isinstance(entry["completed"], bool)


def test_checklist_patch_toggles_step(client: TestClient):
    r = client.patch(
        "/api/onboarding/checklist",
        json={"step": "catalog", "completed": True},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    catalog_entry = next(e for e in data if e["step"] == "catalog")
    assert catalog_entry["completed"] is True

    r2 = client.patch(
        "/api/onboarding/checklist",
        json={"step": "catalog", "completed": False},
    )
    assert r2.status_code == 200
    catalog_entry2 = next(e for e in r2.json() if e["step"] == "catalog")
    assert catalog_entry2["completed"] is False


def test_demo_data_does_not_crash(client: TestClient):
    r = client.post("/api/onboarding/demo-data")
    assert r.status_code == 200
    data = r.json()
    # Either loaded or schema missing — both OK
    assert "customers" in data
    assert "jobs" in data
    assert "invoices" in data

    r2 = client.post("/api/onboarding/clear-demo")
    assert r2.status_code == 200
    d2 = r2.json()
    assert "customers" in d2


def _invoice_with_token(monkeypatch) -> None:
    """The demo invoice is built without ``public_token`` (NOT NULL), so every
    demo load rolls back as ``load_failed`` on main (found 2026-10-12, GDXA-834,
    left for the maintainer to rule on). Supply one so the jobs are observable."""
    from uuid import uuid4

    import gdx_dispatch.routers.onboarding as onboarding
    from gdx_dispatch.models.tenant_models import Invoice

    monkeypatch.setattr(onboarding, "Invoice", lambda **kw: Invoice(public_token=uuid4().hex, **kw))


def _demo_job_numbers(tc: TestClient) -> dict[str, str | None]:
    with tc._engine.connect() as c:  # type: ignore[attr-defined]
        rows = c.execute(
            text(
                "SELECT c.name, j.job_number FROM jobs j JOIN customers c ON c.id = j.customer_id "
                "WHERE j.title = 'DEMO - Garage door tune-up'"
            )
        ).all()
    return {name: number for name, number in rows}


def test_demo_jobs_take_a_number_from_the_tenant_counter(client: TestClient, monkeypatch):
    """GDXA-834: each demo job gets the next number from next_job_number, in
    its own session committed before the job, with its customer's name. The
    allocator is faked here because it locks with FOR UPDATE, which SQLite
    rejects; its own behaviour is pinned in test_job_number_uniqueness."""
    import gdx_dispatch.core.database as database
    import gdx_dispatch.modules.numbering.service as numbering

    calls: list[tuple[str, str | None]] = []
    commits: list[bool] = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            commits.append(True)

    def fake_next(cdb, tenant_id, *, customer_name=None):
        calls.append((tenant_id, customer_name))
        return f"JOB-T-{len(calls):03d}"

    _invoice_with_token(monkeypatch)
    monkeypatch.setattr(database, "SessionLocal", _Session)
    monkeypatch.setattr(numbering, "next_job_number", fake_next)

    r = client.post("/api/onboarding/demo-data")
    assert r.status_code == 200, r.text
    assert r.json()["jobs"] == 3
    assert [t for t, _ in calls] == ["tenant-test"] * 3
    assert len(commits) == 3
    assert _demo_job_numbers(client) == {
        "DEMO - Acme Residential": "JOB-T-001",
        "DEMO - Smith Family": "JOB-T-002",
        "DEMO - Johnson Estates": "JOB-T-003",
    }
    assert [name for _, name in calls] == [
        "DEMO - Acme Residential", "DEMO - Smith Family", "DEMO - Johnson Estates",
    ]


def test_a_demo_load_that_fails_takes_no_number(client: TestClient, monkeypatch):
    """Unpatched, the demo invoice has no public_token and the load rolls
    back. Allocation commits the counter in its own session, so it must not
    have run: a failed load that took numbers would burn them for good."""
    import gdx_dispatch.modules.numbering.service as numbering

    calls: list[str] = []
    monkeypatch.setattr(numbering, "next_job_number", lambda *a, **k: calls.append("x") or "JOB-X")

    r = client.post("/api/onboarding/demo-data")
    assert r.status_code == 200, r.text
    assert r.json()["loaded"] is False
    assert calls == []


def test_a_numbering_failure_leaves_the_demo_job_unnumbered_not_unmade(client: TestClient, monkeypatch):
    import gdx_dispatch.modules.numbering.service as numbering

    def boom(*_a, **_k):
        raise RuntimeError("counter unavailable")

    _invoice_with_token(monkeypatch)
    monkeypatch.setattr(numbering, "next_job_number", boom)
    r = client.post("/api/onboarding/demo-data")
    assert r.status_code == 200, r.text
    assert r.json()["jobs"] == 3
    numbers = _demo_job_numbers(client)
    assert len(numbers) == 3 and set(numbers.values()) == {None}


def test_tenant_scope():
    a = _make_client(tenant_id="tenant-a")
    b = _make_client(tenant_id="tenant-b")
    try:
        a.post("/api/onboarding/step", json={"step": "catalog"})
        state_a = a.get("/api/onboarding/state").json()
        state_b = b.get("/api/onboarding/state").json()
        assert state_a["company_id"] == "tenant-a"
        assert state_b["company_id"] == "tenant-b"
        assert state_a["current_step"] == "catalog"
        assert state_b["current_step"] == "profile"  # untouched
    finally:
        a.app.dependency_overrides.clear()
        b.app.dependency_overrides.clear()
        a._engine.dispose()  # type: ignore[attr-defined]
        b._engine.dispose()  # type: ignore[attr-defined]
