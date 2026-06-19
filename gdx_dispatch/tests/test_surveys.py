"""Tests for the surveys router (admin + public NPS/CSAT flow)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.surveys import (
    SurveySend,
    admin_router,
    public_router,
)


def _make_client(tenant_id: str = "tenant-test", user_sub: str = "user-1") -> TestClient:
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

    app.include_router(public_router)
    app.include_router(admin_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": user_sub,
        "sub": user_sub,
        "role": "admin",
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


# ---------------------------------------------------------------------------
# Admin — templates + sends
# ---------------------------------------------------------------------------


def test_create_template(client: TestClient):
    r = client.post(
        "/api/surveys/templates",
        json={
            "name": "Post-Job NPS",
            "kind": "nps",
            "question": "How likely are you to recommend us?",
            "follow_up_question": "Why?",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Post-Job NPS"
    assert body["kind"] == "nps"
    assert body["active"] is True

    listing = client.get("/api/surveys/templates").json()
    assert len(listing) == 1
    assert listing[0]["id"] == body["id"]


def test_send_survey_generates_token_and_expiry(client: TestClient):
    tpl = client.post(
        "/api/surveys/templates",
        json={"name": "NPS", "kind": "nps", "question": "Score us"},
    ).json()

    r = client.post(
        "/api/surveys/send",
        json={
            "template_id": tpl["id"],
            "recipient_email": "cust@example.com",
            "expires_days": 14,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["token"] and len(body["token"]) >= 32
    assert body["public_url"].endswith(body["token"])
    assert body["kind"] == "nps"
    assert body["expires_at"]


def test_list_responses_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a", user_sub="ua")
    c2 = _make_client(tenant_id="tenant-b", user_sub="ub")
    try:
        # Tenant A creates+sends+responds.
        tpl_a = c1.post(
            "/api/surveys/templates",
            json={"name": "A", "kind": "nps", "question": "q"},
        ).json()
        send_a = c1.post(
            "/api/surveys/send",
            json={"template_id": tpl_a["id"], "recipient_email": "a@ex.com"},
        ).json()
        r = c1.post(f"/api/surveys/public/{send_a['token']}", json={"score": 10})
        assert r.status_code == 201

        list_a = c1.get("/api/surveys/responses").json()
        list_b = c2.get("/api/surveys/responses").json()
        assert len(list_a) == 1
        assert list_a[0]["score"] == 10
        assert list_b == []
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_public_get_by_token_returns_question(client: TestClient):
    tpl = client.post(
        "/api/surveys/templates",
        json={
            "name": "NPS",
            "kind": "nps",
            "question": "How likely to recommend?",
            "follow_up_question": "What could we improve?",
        },
    ).json()
    send = client.post("/api/surveys/send", json={"template_id": tpl["id"]}).json()

    # Public endpoint — no auth needed, but our override is still active; it's ignored for public_router.
    r = client.get(f"/api/surveys/public/{send['token']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["question"] == "How likely to recommend?"
    assert body["follow_up_question"] == "What could we improve?"
    assert body["kind"] == "nps"


def test_public_submit_response_marks_responded(client: TestClient):
    tpl = client.post(
        "/api/surveys/templates",
        json={"name": "NPS", "kind": "nps", "question": "q"},
    ).json()
    send = client.post("/api/surveys/send", json={"template_id": tpl["id"]}).json()

    r = client.post(
        f"/api/surveys/public/{send['token']}",
        json={"score": 9, "comment": "Great service"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["score"] == 9

    # Verify send row has responded_at populated.
    Session = client._session  # type: ignore[attr-defined]
    with Session() as s:
        row = s.execute(
            select(SurveySend).where(SurveySend.id.isnot(None))
        ).scalar_one()
        assert row.responded_at is not None


def test_public_submit_rejects_expired_token(client: TestClient):
    tpl = client.post(
        "/api/surveys/templates",
        json={"name": "NPS", "kind": "nps", "question": "q"},
    ).json()
    send = client.post("/api/surveys/send", json={"template_id": tpl["id"]}).json()

    # Force expiry in the past.
    Session = client._session  # type: ignore[attr-defined]
    with Session() as s:
        row = s.execute(select(SurveySend)).scalar_one()
        row.expires_at = utcnow() - timedelta(days=1)
        s.commit()

    r = client.get(f"/api/surveys/public/{send['token']}")
    assert r.status_code == 404
    r2 = client.post(f"/api/surveys/public/{send['token']}", json={"score": 5})
    assert r2.status_code == 404


def test_public_submit_rejects_reused_token(client: TestClient):
    tpl = client.post(
        "/api/surveys/templates",
        json={"name": "NPS", "kind": "nps", "question": "q"},
    ).json()
    send = client.post("/api/surveys/send", json={"template_id": tpl["id"]}).json()

    r1 = client.post(f"/api/surveys/public/{send['token']}", json={"score": 8})
    assert r1.status_code == 201
    r2 = client.post(f"/api/surveys/public/{send['token']}", json={"score": 7})
    assert r2.status_code == 404
    r3 = client.get(f"/api/surveys/public/{send['token']}")
    assert r3.status_code == 404


def test_csat_score_out_of_range_rejected(client: TestClient):
    tpl = client.post(
        "/api/surveys/templates",
        json={"name": "CSAT", "kind": "csat", "question": "Rate us 1-5"},
    ).json()
    send = client.post("/api/surveys/send", json={"template_id": tpl["id"]}).json()

    r = client.post(f"/api/surveys/public/{send['token']}", json={"score": 8})
    assert r.status_code == 422


def test_metrics_nps_calculation(client: TestClient):
    tpl = client.post(
        "/api/surveys/templates",
        json={"name": "NPS", "kind": "nps", "question": "q"},
    ).json()

    # Seed 5 responses: 2 promoters (10, 9), 2 passives (8, 7), 1 detractor (3).
    # NPS = (2/5 * 100) - (1/5 * 100) = 40 - 20 = 20.
    for score in [10, 9, 8, 7, 3]:
        send = client.post("/api/surveys/send", json={"template_id": tpl["id"]}).json()
        r = client.post(f"/api/surveys/public/{send['token']}", json={"score": score})
        assert r.status_code == 201

    metrics = client.get("/api/surveys/metrics?days=30").json()
    assert metrics["total_sent"] == 5
    assert metrics["total_responded"] == 5
    assert metrics["response_rate"] == 1.0
    assert metrics["nps_score"] == 20.0
    assert metrics["nps_sample_size"] == 5


def test_tenant_scope():
    c1 = _make_client(tenant_id="tenant-a", user_sub="ua")
    c2 = _make_client(tenant_id="tenant-b", user_sub="ub")
    try:
        c1.post(
            "/api/surveys/templates",
            json={"name": "A tpl", "kind": "nps", "question": "q"},
        )
        c2.post(
            "/api/surveys/templates",
            json={"name": "B tpl", "kind": "csat", "question": "q"},
        )
        list_a = c1.get("/api/surveys/templates").json()
        list_b = c2.get("/api/surveys/templates").json()
        assert len(list_a) == 1 and list_a[0]["name"] == "A tpl"
        assert len(list_b) == 1 and list_b[0]["name"] == "B tpl"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]
