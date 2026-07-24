"""Tier 8 — the audit-log page is reachable AND functional
(docs/design/backend-vue-contract-gaps-2026-07-24.md).

The nav entry was revived (its only link lived in the retired
AdminSettingsView). The adversarial audit caught that the viewer's contract
(limit/offset + five filters + chain_integrity) never matched the backend
(page/page_size only) — pagination was a treadmill and every filter a no-op.
This asserts the aligned contract.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, _payload_json

TENANT = "tenant-test"


def _row_hash(prev_hash, *, tenant_id, actor, action, entity_type, entity_id, details, request_id):
    row_data = f"{tenant_id}:{actor}:{action}:{entity_type}:{entity_id}:{_payload_json(details)}:{request_id}"
    return hashlib.sha256(f"{prev_hash}{row_data}".encode()).hexdigest()


def _make_app(SessionLocal):
    from gdx_dispatch.core.auth import get_current_user as core_gcu
    from gdx_dispatch.core.database import get_db as core_get_db
    from gdx_dispatch.routers import admin_ops
    from gdx_dispatch.routers.auth import get_current_user as routers_gcu

    app = FastAPI()
    app.include_router(admin_ops.router)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    admin = {"sub": "admin-1", "user_id": "admin-1", "role": "admin", "tenant_id": TENANT}
    app.dependency_overrides[core_get_db] = _override_db
    app.dependency_overrides[routers_gcu] = lambda: admin
    app.dependency_overrides[core_gcu] = lambda: admin
    return app


def _seed_chain(db, n=5, *, tenant_id=TENANT):
    """Seed a valid hash chain of n rows so chain_integrity can pass."""
    prev = ""
    base = datetime(2026, 7, 1, tzinfo=UTC)
    for i in range(n):
        actor = "admin-1"
        action = "login" if i % 2 == 0 else "customer_updated"
        entity_type = "auth" if i % 2 == 0 else "customer"
        entity_id = f"e{i}"
        details = {"i": i}
        h = _row_hash(prev, tenant_id=tenant_id, actor=actor, action=action,
                      entity_type=entity_type, entity_id=entity_id, details=details, request_id=None)
        db.add(AuditLog(
            id=uuid4(), tenant_id=tenant_id, user_id=actor, actor_id=actor,
            action=action, event_type=action, entity_type=entity_type, entity_id=entity_id,
            details=details, payload=details, row_hash=h, hash=h, prev_hash=prev,
            created_at=base + timedelta(minutes=i),
        ))
        prev = h
    db.commit()


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_limit_offset_paginates_and_chain_intact():
    engine, SessionLocal = _engine()
    try:
        db = SessionLocal()
        _seed_chain(db, 5)
        db.close()

        client = TestClient(_make_app(SessionLocal))
        r = client.get("/api/admin/audit-log?limit=2&offset=0")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2
        assert body["limit"] == 2 and body["offset"] == 0
        # Chain badge is populated (the whole point of the nav revival) and,
        # for an untampered seed, intact.
        assert body["chain_integrity"]["valid"] is True

        # offset actually moves the window (the old handler ignored it)
        first_page_ids = [i["id"] for i in body["items"]]
        r2 = client.get("/api/admin/audit-log?limit=2&offset=2")
        second_page_ids = [i["id"] for i in r2.json()["items"]]
        assert set(first_page_ids).isdisjoint(second_page_ids)
    finally:
        engine.dispose()


def test_filters_are_applied():
    engine, SessionLocal = _engine()
    try:
        db = SessionLocal()
        _seed_chain(db, 6)
        db.close()

        client = TestClient(_make_app(SessionLocal))
        # resource_type maps to entity_type; only "customer" rows come back
        r = client.get("/api/admin/audit-log?resource_type=customer&limit=50")
        items = r.json()["items"]
        assert items and all(i["entity_type"] == "customer" for i in items)

        # action filter matches action OR legacy event_type
        r2 = client.get("/api/admin/audit-log?action=login&limit=50")
        assert all(i["action"] == "login" for i in r2.json()["items"])

        # since/until narrow the window
        r3 = client.get("/api/admin/audit-log?since=2026-07-01T00:03:00Z&limit=50")
        assert r3.status_code == 200
        assert r3.json()["total"] < 6
    finally:
        engine.dispose()


def test_chain_break_is_detected():
    engine, SessionLocal = _engine()
    try:
        db = SessionLocal()
        _seed_chain(db, 4)
        # Tamper: rewrite one row's details without fixing its hash.
        row = db.query(AuditLog).order_by(AuditLog.created_at.asc()).offset(1).first()
        row.details = {"tampered": True}
        db.commit()
        db.close()

        client = TestClient(_make_app(SessionLocal))
        body = client.get("/api/admin/audit-log?limit=50").json()
        assert body["chain_integrity"]["valid"] is False
        assert body["chain_integrity"]["break_at"] is not None
    finally:
        engine.dispose()


def test_page_page_size_backcompat_still_works():
    engine, SessionLocal = _engine()
    try:
        db = SessionLocal()
        _seed_chain(db, 5)
        db.close()

        client = TestClient(_make_app(SessionLocal))
        body = client.get("/api/admin/audit-log?page=1&page_size=3").json()
        assert len(body["items"]) == 3
        assert body["page"] == 1
    finally:
        engine.dispose()
