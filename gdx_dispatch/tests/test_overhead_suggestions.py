"""Integration tests for /api/overhead/suggestions (ADR-016 Slice 2).

Stream-hint seeding: bank-detected RecurringStreams surface as draft overhead
suggestions; confirming one (POST with source_stream_id) drops it from the list.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
# Import RecurringStream so its table registers on TenantBase.metadata before create_all.
from gdx_dispatch.modules.forecasting.models import (
    STREAM_STATUS_ACTIVE,
    STREAM_STATUS_PAID_OFF,
    STREAM_STATUS_SUGGESTED,
    RecurringStream,
)
from gdx_dispatch.routers import overhead as overhead_router


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(overhead_router.router)
    app.dependency_overrides[overhead_router.get_db] = _override_db
    app.dependency_overrides[overhead_router.get_current_user] = lambda: {
        "sub": "test-user", "role": "admin", "tenant_id": "tenant-test",
    }
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc, SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_stream(SessionLocal, **kw):
    db = SessionLocal()
    defaults = dict(
        label="ACME Insurance",
        source="observed",
        payee_pattern="ACME INSURANCE",
        amount_min=180,
        amount_max=200,
        cadence="monthly",
        status=STREAM_STATUS_ACTIVE,
    )
    defaults.update(kw)
    s = RecurringStream(**defaults)
    db.add(s)
    db.commit()
    sid = str(s.id)
    db.close()
    return sid


def test_active_stream_appears_as_suggestion(ctx):
    tc, SL = ctx
    sid = _seed_stream(SL)
    body = tc.get("/api/overhead/suggestions").json()
    assert body["count"] == 1
    s = body["suggestions"][0]
    assert s["stream_id"] == sid
    assert s["suggested_amount"] == "190.00"           # midpoint of 180..200
    assert s["suggested_category"] == "insurance"      # keyword guess
    assert s["cadence"] == "monthly"


def test_paid_off_and_deleted_streams_excluded(ctx):
    tc, SL = ctx
    _seed_stream(SL, status=STREAM_STATUS_PAID_OFF, payee_pattern="OLD LOAN")
    import datetime as _dt
    _seed_stream(SL, deleted_at=_dt.datetime.now(_dt.timezone.utc), payee_pattern="GONE")
    _seed_stream(SL, status=STREAM_STATUS_SUGGESTED, label="Comcast", payee_pattern="COMCAST INTERNET")
    body = tc.get("/api/overhead/suggestions").json()
    labels = [s["payee_pattern"] for s in body["suggestions"]]
    assert labels == ["COMCAST INTERNET"]              # only the live one
    assert body["suggestions"][0]["suggested_category"] == "subscription"


def test_confirming_a_suggestion_removes_it_and_sets_source(ctx):
    tc, SL = ctx
    sid = _seed_stream(SL)
    # confirm it into the register
    r = tc.post("/api/overhead", json={
        "label": "ACME Insurance", "category": "insurance", "amount": "190.00",
        "cadence": "monthly", "start_date": "2025-01-01", "source_stream_id": sid,
    })
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["source"] == "seeded_from_stream"
    assert created["source_stream_id"] == sid
    # now it's no longer suggested
    assert tc.get("/api/overhead/suggestions").json()["count"] == 0


def test_double_confirm_same_stream_conflicts(ctx):
    tc, SL = ctx
    sid = _seed_stream(SL)
    base = {
        "label": "ACME Insurance", "amount": "190.00",
        "cadence": "monthly", "start_date": "2025-01-01", "source_stream_id": sid,
    }
    assert tc.post("/api/overhead", json=base).status_code == 201
    r2 = tc.post("/api/overhead", json=base)
    assert r2.status_code == 409


def test_manual_create_has_no_stream_link(ctx):
    tc, _ = ctx
    r = tc.post("/api/overhead", json={
        "label": "Shop rent", "amount": "2000.00", "cadence": "monthly", "start_date": "2025-01-01",
    })
    assert r.json()["source"] == "manual"
    assert r.json()["source_stream_id"] is None
