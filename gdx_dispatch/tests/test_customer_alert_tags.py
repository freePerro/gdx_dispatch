"""S1-A8 — customer-alert tag taxonomy: seed + admin CRUD.

Two test groups:
- seed: ``seed_default_customer_alert_tags`` is idempotent and respects
  prior tenant edits.
- crud: GET/POST/PUT/DELETE round-trip via the FastAPI test client,
  with audit-row assertions on every mutation.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.customer_alert_tags import (
    DEFAULT_CUSTOMER_ALERT_TAGS,
    seed_default_customer_alert_tags,
)
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.models.tenant_models import Tag, TagAssignment
from gdx_dispatch.routers.admin_customer_tags import router as tags_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db


TENANT = str(uuid4())
USER = str(uuid4())


def _admin():
    return {"user_id": USER, "tenant_id": TENANT, "role": "admin"}


@pytest.fixture
def db():
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def app_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()

    app = FastAPI()
    app.include_router(tags_router)
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user] = _admin
    app.dependency_overrides[require_permission("settings.write")] = lambda: {"ok": True}

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant_id = TENANT
        request.state.tenant = {"id": TENANT, "slug": "t"}
        request.state.user = _admin()
        return await call_next(request)

    client = TestClient(app)
    yield client, s
    s.close()
    engine.dispose()


# ── seed ──────────────────────────────────────────────────────────────


class TestSeed:
    def test_seeds_full_default_taxonomy_on_empty_db(self, db):
        inserted = seed_default_customer_alert_tags(db, TENANT)
        assert inserted == len(DEFAULT_CUSTOMER_ALERT_TAGS)
        names = {n for (n,) in db.query(Tag.name).filter(Tag.company_id == TENANT).all()}
        for name, _, _ in DEFAULT_CUSTOMER_ALERT_TAGS:
            assert name in names

    def test_idempotent_second_run(self, db):
        seed_default_customer_alert_tags(db, TENANT)
        again = seed_default_customer_alert_tags(db, TENANT)
        assert again == 0
        # Total tag count for this tenant unchanged.
        total = db.query(Tag).filter(Tag.company_id == TENANT).count()
        assert total == len(DEFAULT_CUSTOMER_ALERT_TAGS)

    def test_preserves_tenant_edits_to_existing_seed_rows(self, db):
        # Tenant pre-customized "dog_warning" before the seed runs (e.g.,
        # provisioning re-runs after a manual schema fix). The seed must
        # NOT clobber the customization.
        db.add(
            Tag(
                id=uuid4(),
                company_id=TENANT,
                name="dog_warning",
                color="#abcdef",
                description="custom desc",
            )
        )
        db.commit()
        seed_default_customer_alert_tags(db, TENANT)
        row = (
            db.query(Tag)
            .filter(Tag.company_id == TENANT, Tag.name == "dog_warning")
            .one()
        )
        assert row.color == "#abcdef"
        assert row.description == "custom desc"

    def test_per_tenant_isolation(self, db):
        seed_default_customer_alert_tags(db, "tenant-A")
        seed_default_customer_alert_tags(db, "tenant-B")
        a_count = db.query(Tag).filter(Tag.company_id == "tenant-A").count()
        b_count = db.query(Tag).filter(Tag.company_id == "tenant-B").count()
        assert a_count == len(DEFAULT_CUSTOMER_ALERT_TAGS)
        assert b_count == len(DEFAULT_CUSTOMER_ALERT_TAGS)


# ── CRUD ──────────────────────────────────────────────────────────────


def _audit(db, action: str | None = None) -> list[AuditLog]:
    q = db.query(AuditLog).filter(AuditLog.tenant_id == TENANT)
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.created_at.asc()).all()


class TestList:
    def test_returns_seeded_tags(self, app_and_db):
        client, db = app_and_db
        seed_default_customer_alert_tags(db, TENANT)
        body = client.get("/api/admin/customer-tags").json()
        names = {t["name"] for t in body["tags"]}
        for n, _, _ in DEFAULT_CUSTOMER_ALERT_TAGS:
            assert n in names

    def test_only_returns_calling_tenant(self, app_and_db):
        client, db = app_and_db
        seed_default_customer_alert_tags(db, TENANT)
        seed_default_customer_alert_tags(db, "other-tenant")
        body = client.get("/api/admin/customer-tags").json()
        # Calling tenant's tags only — never the other tenant's.
        assert len(body["tags"]) == len(DEFAULT_CUSTOMER_ALERT_TAGS)


class TestCreate:
    def test_create_happy_path(self, app_and_db):
        client, db = app_and_db
        r = client.post(
            "/api/admin/customer-tags",
            json={"name": "ladder_required", "color": "#123456", "description": "Bring the 16ft."},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "ladder_required"
        assert body["color"] == "#123456"
        rows = _audit(db, action="customer_tag.created")
        assert len(rows) == 1
        assert rows[0].entity_id == body["id"]

    def test_create_rejects_uppercase_name(self, app_and_db):
        client, _ = app_and_db
        r = client.post(
            "/api/admin/customer-tags",
            json={"name": "Ladder", "color": "#123456"},
        )
        assert r.status_code == 400

    def test_create_rejects_invalid_color(self, app_and_db):
        client, _ = app_and_db
        r = client.post(
            "/api/admin/customer-tags",
            json={"name": "ladder", "color": "red"},
        )
        assert r.status_code == 400

    def test_create_rejects_duplicate_name(self, app_and_db):
        client, db = app_and_db
        seed_default_customer_alert_tags(db, TENANT)
        r = client.post(
            "/api/admin/customer-tags",
            json={"name": "dog_warning", "color": "#000000"},
        )
        assert r.status_code == 409


class TestUpdate:
    def test_rename_round_trip(self, app_and_db):
        client, db = app_and_db
        seed_default_customer_alert_tags(db, TENANT)
        tag = db.query(Tag).filter(Tag.name == "dog_warning").one()
        r = client.put(
            f"/api/admin/customer-tags/{tag.id}",
            json={"name": "beware_of_dog"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "beware_of_dog"
        # Audit captured before/after.
        rows = _audit(db, action="customer_tag.updated")
        assert len(rows) == 1
        assert rows[0].details["before"]["name"] == "dog_warning"
        assert rows[0].details["after"]["name"] == "beware_of_dog"

    def test_recolor(self, app_and_db):
        client, db = app_and_db
        seed_default_customer_alert_tags(db, TENANT)
        tag = db.query(Tag).filter(Tag.name == "vip").one()
        r = client.put(
            f"/api/admin/customer-tags/{tag.id}",
            json={"color": "#aabbcc"},
        )
        assert r.status_code == 200
        assert r.json()["color"] == "#aabbcc"

    def test_404_unknown_tag(self, app_and_db):
        client, _ = app_and_db
        r = client.put(
            f"/api/admin/customer-tags/{uuid4()}",
            json={"color": "#aabbcc"},
        )
        assert r.status_code == 404

    def test_rename_to_existing_name_409(self, app_and_db):
        client, db = app_and_db
        seed_default_customer_alert_tags(db, TENANT)
        tag = db.query(Tag).filter(Tag.name == "vip").one()
        r = client.put(
            f"/api/admin/customer-tags/{tag.id}",
            json={"name": "dog_warning"},
        )
        assert r.status_code == 409


class TestDelete:
    def test_delete_with_no_assignments(self, app_and_db):
        client, db = app_and_db
        seed_default_customer_alert_tags(db, TENANT)
        tag = db.query(Tag).filter(Tag.name == "noise_sensitive").one()
        r = client.delete(f"/api/admin/customer-tags/{tag.id}")
        assert r.status_code == 200
        assert r.json()["assignments_removed"] == 0
        assert (
            db.query(Tag).filter(Tag.id == tag.id).count() == 0
        )  # hard delete per design

    def test_delete_cascades_assignments(self, app_and_db):
        client, db = app_and_db
        seed_default_customer_alert_tags(db, TENANT)
        tag = db.query(Tag).filter(Tag.name == "vip").one()
        # Two customers carry this tag.
        for _ in range(2):
            db.add(
                TagAssignment(
                    id=uuid4(),
                    company_id=TENANT,
                    tag_id=tag.id,
                    entity_type="customer",
                    entity_id=str(uuid4()),
                )
            )
        db.commit()
        r = client.delete(f"/api/admin/customer-tags/{tag.id}")
        assert r.status_code == 200
        assert r.json()["assignments_removed"] == 2
        # No orphan assignments left.
        assert (
            db.query(TagAssignment)
            .filter(TagAssignment.tag_id == tag.id)
            .count()
            == 0
        )
        rows = _audit(db, action="customer_tag.deleted")
        assert len(rows) == 1
        assert rows[0].details["assignments_removed"] == 2

    def test_delete_404_unknown(self, app_and_db):
        client, _ = app_and_db
        r = client.delete(f"/api/admin/customer-tags/{uuid4()}")
        assert r.status_code == 404
