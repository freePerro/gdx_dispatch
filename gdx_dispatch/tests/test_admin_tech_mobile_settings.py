"""Admin tech-mobile settings router (S1-Z4 + S1-Z5).

Tests the GET / PUT / DELETE flow against a real in-memory tenant DB,
using FastAPI ``dependency_overrides`` to swap out auth, the tenant
session dependency, and the audit hook.

S1-Z5 audit assertion: every successful PUT and DELETE writes exactly
one ``audit_logs`` row with action ``tech_mobile_settings.changed`` /
``tech_mobile_settings.reset``, before/after recorded in ``details``.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.routers.admin_tech_mobile_settings import router as tm_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db


TID = str(uuid4())
UID = str(uuid4())


def _admin_user() -> dict:
    return {"user_id": UID, "tenant_id": TID, "role": "admin"}


@pytest.fixture
def app_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # One Session shared across the test request lifetime; FastAPI's
    # dependency_overrides bypasses the generator-based teardown so we
    # close it manually below.
    db = SessionLocal()

    app = FastAPI()
    app.include_router(tm_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = _admin_user
    # Bypass permission gate; tested separately in test_settings_write_denied.
    app.dependency_overrides[require_permission("settings.write")] = lambda: {"ok": True}

    # Inject tenant_id onto request.state so the audit helper can find it.
    @app.middleware("http")
    async def _stamp_tenant(request, call_next):
        request.state.tenant_id = TID
        request.state.tenant = {"id": TID, "slug": "test"}
        request.state.user = _admin_user()
        return await call_next(request)

    client = TestClient(app)
    yield client, db
    db.close()
    engine.dispose()


def _audit_rows(db, action: str | None = None) -> list[AuditLog]:
    q = db.query(AuditLog)
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.created_at.asc()).all()


# ── GET ───────────────────────────────────────────────────────────────


class TestGet:
    def test_returns_catalog_overrides_resolved(self, app_and_db):
        client, _ = app_and_db
        r = client.get("/api/admin/feature-settings/tech-mobile")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "catalog" in body
        assert "overrides" in body
        assert "resolved" in body
        # Catalog is a list, sorted by phase.
        assert isinstance(body["catalog"], list)
        assert all("key" in row and "default" in row for row in body["catalog"])
        # No overrides yet — resolved equals catalog defaults.
        assert body["overrides"] == {}
        assert body["resolved"]["tech_mobile.gps_retention_days"] == 45
        assert body["resolved"]["tech_mobile.drive_time_provider"] == "google"

    def test_reflects_existing_overrides(self, app_and_db):
        client, db = app_and_db
        db.add(
            AppSettings(
                tenant_mobile_settings={
                    "tech_mobile.gps_retention_days": 90,
                }
            )
        )
        db.commit()
        r = client.get("/api/admin/feature-settings/tech-mobile")
        body = r.json()
        assert body["overrides"] == {"tech_mobile.gps_retention_days": 90}
        assert body["resolved"]["tech_mobile.gps_retention_days"] == 90


# ── PUT ───────────────────────────────────────────────────────────────


class TestPut:
    def test_creates_app_settings_row_when_missing(self, app_and_db):
        client, db = app_and_db
        assert db.query(AppSettings).first() is None
        r = client.put(
            "/api/admin/feature-settings/tech-mobile",
            json={"key": "tech_mobile.gps_retention_days", "value": 90},
        )
        assert r.status_code == 200, r.text
        row = db.query(AppSettings).first()
        assert row is not None
        assert row.tenant_mobile_settings == {"tech_mobile.gps_retention_days": 90}

    def test_updates_existing_override(self, app_and_db):
        client, db = app_and_db
        db.add(
            AppSettings(
                tenant_mobile_settings={"tech_mobile.gps_retention_days": 60}
            )
        )
        db.commit()
        r = client.put(
            "/api/admin/feature-settings/tech-mobile",
            json={"key": "tech_mobile.gps_retention_days", "value": 200},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {
            "ok": True,
            "key": "tech_mobile.gps_retention_days",
            "before": 60,
            "after": 200,
        }
        db.expire_all()
        row = db.query(AppSettings).first()
        assert row.tenant_mobile_settings["tech_mobile.gps_retention_days"] == 200

    def test_rejects_unknown_key(self, app_and_db):
        client, _ = app_and_db
        r = client.put(
            "/api/admin/feature-settings/tech-mobile",
            json={"key": "tech_mobile.bogus", "value": 1},
        )
        assert r.status_code == 400
        assert "unknown setting" in r.json()["detail"]

    def test_rejects_out_of_bounds_int(self, app_and_db):
        client, _ = app_and_db
        r = client.put(
            "/api/admin/feature-settings/tech-mobile",
            json={"key": "tech_mobile.gps_retention_days", "value": 9999},
        )
        assert r.status_code == 400
        assert "out of bounds" in r.json()["detail"]

    def test_rejects_invalid_enum(self, app_and_db):
        client, _ = app_and_db
        r = client.put(
            "/api/admin/feature-settings/tech-mobile",
            json={"key": "tech_mobile.drive_time_provider", "value": "waze"},
        )
        assert r.status_code == 400
        assert "not in" in r.json()["detail"]

    def test_rejects_bool_disguised_as_int(self, app_and_db):
        client, _ = app_and_db
        r = client.put(
            "/api/admin/feature-settings/tech-mobile",
            json={"key": "tech_mobile.gps_retention_days", "value": True},
        )
        assert r.status_code == 400
        assert "expected int" in r.json()["detail"]


# ── PUT — Z5 audit ────────────────────────────────────────────────────


class TestPutAudit:
    def test_audit_row_written_on_change(self, app_and_db):
        client, db = app_and_db
        client.put(
            "/api/admin/feature-settings/tech-mobile",
            json={"key": "tech_mobile.gps_retention_days", "value": 90},
        )
        rows = _audit_rows(db, action="tech_mobile_settings.changed")
        assert len(rows) == 1
        row = rows[0]
        assert str(row.tenant_id) == TID
        assert str(row.user_id) == UID
        assert row.entity_type == "tenant_mobile_settings"
        assert row.entity_id == "tech_mobile.gps_retention_days"
        assert row.details["before"] is None
        assert row.details["after"] == 90

    def test_audit_includes_before_value(self, app_and_db):
        client, db = app_and_db
        # Seed an existing override so 'before' is non-null.
        db.add(
            AppSettings(
                tenant_mobile_settings={"tech_mobile.gps_retention_days": 60}
            )
        )
        db.commit()
        client.put(
            "/api/admin/feature-settings/tech-mobile",
            json={"key": "tech_mobile.gps_retention_days", "value": 200},
        )
        rows = _audit_rows(db, action="tech_mobile_settings.changed")
        assert len(rows) == 1
        assert rows[0].details["before"] == 60
        assert rows[0].details["after"] == 200

    def test_no_audit_row_when_validation_rejects(self, app_and_db):
        client, db = app_and_db
        client.put(
            "/api/admin/feature-settings/tech-mobile",
            json={"key": "tech_mobile.gps_retention_days", "value": 99999},
        )
        # No tech_mobile_settings audit row should land for a rejected
        # write — the catalog validator runs before the persistence + audit.
        rows = _audit_rows(db, action="tech_mobile_settings.changed")
        assert rows == []


# ── DELETE ────────────────────────────────────────────────────────────


class TestDelete:
    def test_removes_override(self, app_and_db):
        client, db = app_and_db
        db.add(
            AppSettings(
                tenant_mobile_settings={
                    "tech_mobile.gps_retention_days": 90,
                    "tech_mobile.drive_time_provider": "off",
                }
            )
        )
        db.commit()
        r = client.delete(
            "/api/admin/feature-settings/tech-mobile/tech_mobile.gps_retention_days"
        )
        assert r.status_code == 200
        assert r.json() == {
            "ok": True,
            "key": "tech_mobile.gps_retention_days",
            "before": 90,
            "after": None,
        }
        db.expire_all()
        row = db.query(AppSettings).first()
        assert "tech_mobile.gps_retention_days" not in row.tenant_mobile_settings
        assert row.tenant_mobile_settings["tech_mobile.drive_time_provider"] == "off"

    def test_audit_row_on_reset(self, app_and_db):
        client, db = app_and_db
        db.add(
            AppSettings(
                tenant_mobile_settings={"tech_mobile.gps_retention_days": 90}
            )
        )
        db.commit()
        client.delete(
            "/api/admin/feature-settings/tech-mobile/tech_mobile.gps_retention_days"
        )
        rows = _audit_rows(db, action="tech_mobile_settings.reset")
        assert len(rows) == 1
        assert rows[0].entity_id == "tech_mobile.gps_retention_days"
        assert rows[0].details["before"] == 90
        assert rows[0].details["after"] is None

    def test_delete_missing_override_is_noop(self, app_and_db):
        client, db = app_and_db
        r = client.delete("/api/admin/feature-settings/tech-mobile/tech_mobile.gps_retention_days")
        assert r.status_code == 200
        # No-op deletes don't emit an audit row.
        assert _audit_rows(db, action="tech_mobile_settings.reset") == []
