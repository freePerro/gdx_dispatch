"""Tests for the GPS technician location tracking router."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.gps import router


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
            VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g1-{tenant_id}", "tid": tenant_id},
    )
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
    tc._SessionLocal = Session  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def _ping(**overrides) -> dict:
    base = {
        "lat": 39.7392,
        "lng": -104.9903,
        "accuracy_meters": 8.5,
        "speed_mph": 32.1,
        "heading_deg": 180,
        "battery_percent": 87,
    }
    base.update(overrides)
    return base


def test_post_location_ping(client: TestClient):
    r = client.post("/api/gps/technicians/tech-42/location", json=_ping())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["tech_id"] == "tech-42"
    assert data["company_id"] == "tenant-test"
    assert data["lat"] == pytest.approx(39.7392, rel=1e-5)
    assert data["lng"] == pytest.approx(-104.9903, rel=1e-5)
    assert data["heading_deg"] == 180
    assert data["battery_percent"] == 87
    assert data["recorded_at"] is not None


def test_live_view_returns_recent_techs(client: TestClient):
    now = datetime.now(timezone.utc)
    recent = now - timedelta(minutes=1)
    stale = now - timedelta(minutes=10)

    r1 = client.post(
        "/api/gps/technicians/tech-recent/location",
        json=_ping(recorded_at=recent.isoformat()),
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/gps/technicians/tech-stale/location",
        json=_ping(recorded_at=stale.isoformat(), lat=40.0, lng=-105.0),
    )
    assert r2.status_code == 201

    live = client.get("/api/gps/technicians/live")
    assert live.status_code == 200, live.text
    data = live.json()
    tech_ids = {row["tech_id"] for row in data}
    assert "tech-recent" in tech_ids
    assert "tech-stale" not in tech_ids
    recent_row = next(r for r in data if r["tech_id"] == "tech-recent")
    assert recent_row["age_seconds"] is not None
    assert 0 <= recent_row["age_seconds"] <= 300


def test_history_returns_all_pings_for_date(client: TestClient):
    today = datetime.now(timezone.utc).date()
    # Anchor "today" pings at today-noon so they land on today's UTC date
    # regardless of wall-clock hour. Subtracting from `now` flaked when the
    # test ran within 25 minutes of UTC midnight.
    today_noon = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
    # Three pings today, one yesterday.
    for minute_off in (5, 15, 25):
        ts = today_noon - timedelta(minutes=minute_off)
        client.post(
            "/api/gps/technicians/tech-hist/location",
            json=_ping(recorded_at=ts.isoformat()),
        )
    yesterday = datetime.combine(
        today - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    ) + timedelta(hours=12)
    client.post(
        "/api/gps/technicians/tech-hist/location",
        json=_ping(recorded_at=yesterday.isoformat()),
    )

    r = client.get(
        "/api/gps/technicians/tech-hist/history",
        params={"date": today.isoformat()},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["tech_id"] == "tech-hist"
    assert data["count"] == 3
    assert len(data["points"]) == 3
    # Chronological ordering
    recorded = [p["recorded_at"] for p in data["points"]]
    assert recorded == sorted(recorded)


def test_route_optimize_stub_returns_input_order(client: TestClient):
    payload = {"tech_id": "tech-42", "job_ids": ["job-a", "job-b", "job-c"]}
    r = client.post("/api/gps/route-optimize", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["tech_id"] == "tech-42"
    assert data["optimized_order"] == ["job-a", "job-b", "job-c"]


def test_cleanup_old_pings(client: TestClient):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=30)
    recent = now - timedelta(days=1)
    client.post(
        "/api/gps/technicians/tech-clean/location",
        json=_ping(recorded_at=old.isoformat()),
    )
    client.post(
        "/api/gps/technicians/tech-clean/location",
        json=_ping(recorded_at=recent.isoformat()),
    )

    cutoff = (now - timedelta(days=7)).date().isoformat()
    r = client.request(
        "DELETE",
        "/api/gps/technicians/tech-clean/history",
        params={"before": cutoff},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["deleted_count"] == 1

    # Remaining recent ping still visible in today's history.
    today = now.date().isoformat()
    h = client.get("/api/gps/technicians/tech-clean/history", params={"date": today})
    # If recent is yesterday, today is empty; check yesterday as well.
    yesterday = (now - timedelta(days=1)).date().isoformat()
    h_y = client.get("/api/gps/technicians/tech-clean/history", params={"date": yesterday})
    total = h.json()["count"] + h_y.json()["count"]
    assert total == 1


def test_lat_out_of_range_rejected(client: TestClient):
    r = client.post(
        "/api/gps/technicians/tech-42/location",
        json=_ping(lat=91.0),
    )
    assert r.status_code == 422


def test_tenant_scope():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        r1 = c1.post("/api/gps/technicians/tech-shared/location", json=_ping())
        assert r1.status_code == 201
        r2 = c2.post(
            "/api/gps/technicians/tech-shared/location",
            json=_ping(lat=40.0, lng=-105.0),
        )
        assert r2.status_code == 201

        today = datetime.now(timezone.utc).date().isoformat()
        h1 = c1.get(
            "/api/gps/technicians/tech-shared/history", params={"date": today}
        ).json()
        h2 = c2.get(
            "/api/gps/technicians/tech-shared/history", params={"date": today}
        ).json()
        assert h1["count"] == 1
        assert h2["count"] == 1
        assert h1["points"][0]["company_id"] == "tenant-a"
        assert h2["points"][0]["company_id"] == "tenant-b"
        # Each tenant only sees its own live row.
        live1 = c1.get("/api/gps/technicians/live").json()
        live2 = c2.get("/api/gps/technicians/live").json()
        assert len(live1) == 1 and len(live2) == 1
        assert live1[0]["lat"] != live2[0]["lat"]
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]
