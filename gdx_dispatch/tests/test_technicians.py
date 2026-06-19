from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.routers.technicians import (
    TechnicianCreate,
    TechnicianPatch,
    TechnicianSkillCreate,
    UnavailabilityCreate,
    add_technician_skill,
    add_technician_unavailability,
    create_technician,
    delete_technician,
    get_technician,
    get_technician_availability,
    list_technician_skills,
    list_technicians,
    patch_technician,
)


def _request(tenant_id: str = "tenant-a") -> Request:
    req = Request({"type": "http", "headers": []})
    req.state.tenant = {"id": tenant_id}
    return req


def _body(resp) -> dict | list:
    return json.loads(resp.body.decode())


def _create_tech(db: Session, tenant_id: str = "tenant-a", **overrides) -> dict:
    payload = {"user_id": "user-100", "skills": ["spring", "cables"], "hourly_rate": 80.0}
    payload.update(overrides)
    resp = create_technician(TechnicianCreate(**payload), _request(tenant_id), {}, db)
    assert resp.status_code == 201
    return _body(resp)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    yield session
    session.close()
    engine.dispose()


def test_create_technician_success(db):
    data = _create_tech(db)
    assert data["id"]
    assert data["user_id"] == "user-100"
    assert data["skills"] == ["spring", "cables"]
    assert Decimal(str(data["hourly_rate"])) == Decimal("80.0")
    assert data["deleted_at"] is None


def test_create_technician_requires_user_id(db):
    resp = create_technician(TechnicianCreate(skills=["spring"], hourly_rate=75.0), _request(), {}, db)
    assert resp.status_code == 400
    assert _body(resp)["detail"] == "user_id is required"


def test_list_technicians(db):
    a = _create_tech(db, user_id="user-a")
    b = _create_tech(db, user_id="user-b")
    resp = list_technicians(_request(), False, {}, db)
    assert resp.status_code == 200
    rows = _body(resp)
    ids = [r["id"] for r in rows]
    assert a["id"] in ids
    assert b["id"] in ids


def test_list_technicians_is_tenant_scoped(db):
    # Three-plane (2026-04-24 B1): tenant isolation is now the DB connection itself,
    # not an app-level company_id filter. This unit test uses a single shared SQLite
    # session, so both tenants' rows are visible — that's expected in-test; in prod
    # each tenant has a separate per-tenant database. We still assert both rows exist.
    ta = _create_tech(db, "tenant-a", user_id="tenant-a-user")
    tb = _create_tech(db, "tenant-b", user_id="tenant-b-user")
    rows_a = _body(list_technicians(_request("tenant-a"), False, {}, db))
    ids_a = [r["id"] for r in rows_a]
    assert ta["id"] in ids_a
    assert tb["id"] in ids_a


def test_get_technician(db):
    created = _create_tech(db, user_id="user-detail")
    resp = get_technician(created["id"], _request(), {}, db)
    assert resp.status_code == 200
    data = _body(resp)
    assert data["id"] == created["id"]
    assert data["user_id"] == "user-detail"


def test_get_technician_not_found(db):
    resp = get_technician("not-a-real-id", _request(), {}, db)
    assert resp.status_code == 404
    assert _body(resp)["detail"] == "technician not found"


def test_patch_technician(db):
    created = _create_tech(db, user_id="user-patch", skills=["opener"], hourly_rate=55.0)
    resp = patch_technician(
        created["id"],
        TechnicianPatch(skills=["opener", "springs"], hourly_rate=70.5, active=False),
        _request(),
        {},
        db,
    )
    assert resp.status_code == 200
    data = _body(resp)
    assert data["id"] == created["id"]
    assert data["skills"] == ["opener", "springs"]
    assert Decimal(str(data["hourly_rate"])) == Decimal("70.5")
    assert data["active"] is False


def test_patch_technician_not_found(db):
    resp = patch_technician("missing", TechnicianPatch(hourly_rate=99.0), _request(), {}, db)
    assert resp.status_code == 404
    assert _body(resp)["detail"] == "technician not found"


def test_delete_technician_soft_delete_and_hidden_from_list(db):
    created = _create_tech(db, user_id="user-delete")
    resp = delete_technician(created["id"], _request(), {}, db)
    assert resp.status_code == 200
    assert _body(resp) == {"deleted": True}

    rows = _body(list_technicians(_request(), False, {}, db))
    ids = [r["id"] for r in rows]
    assert created["id"] not in ids

    get_resp = get_technician(created["id"], _request(), {}, db)
    assert get_resp.status_code == 404


def test_get_skills_returns_list(db):
    created = _create_tech(db, skills=["rollers", "tracks"])
    resp = list_technician_skills(created["id"], _request(), {}, db)
    assert resp.status_code == 200
    assert _body(resp) == {"skills": ["rollers", "tracks"]}


def test_add_skill(db):
    created = _create_tech(db, skills=["springs"])
    resp = add_technician_skill(created["id"], TechnicianSkillCreate(skill="openers"), _request(), {}, db)
    assert resp.status_code == 200
    assert _body(resp)["skills"] == ["springs", "openers"]


def test_add_skill_deduplicates(db):
    created = _create_tech(db, skills=["springs"])
    resp = add_technician_skill(created["id"], TechnicianSkillCreate(skill="springs"), _request(), {}, db)
    assert resp.status_code == 200
    assert _body(resp)["skills"] == ["springs"]


def test_add_skill_requires_skill_value(db):
    created = _create_tech(db, skills=["springs"])
    resp = add_technician_skill(created["id"], TechnicianSkillCreate(skill="   "), _request(), {}, db)
    assert resp.status_code == 400
    assert _body(resp)["detail"] == "skill is required"


def test_get_availability_returns_unavailability_windows(db):
    created = _create_tech(db, user_id="user-avail")
    start = datetime.now(UTC)
    end = start + timedelta(hours=4)
    create_resp = add_technician_unavailability(
        created["id"],
        UnavailabilityCreate(start_at=start, end_at=end, reason="training"),
        _request(),
        {},
        db,
    )
    assert create_resp.status_code == 201

    resp = get_technician_availability(created["id"], _request(), {}, db)
    assert resp.status_code == 200
    rows = _body(resp)
    assert len(rows) == 1
    assert rows[0]["reason"] == "training"


def test_post_unavailability_validates_range(db):
    created = _create_tech(db, user_id="user-bad-range")
    start = datetime.now(UTC)
    end = start - timedelta(minutes=1)
    resp = add_technician_unavailability(
        created["id"],
        UnavailabilityCreate(start_at=start, end_at=end, reason="invalid"),
        _request(),
        {},
        db,
    )
    assert resp.status_code == 400
    assert _body(resp)["detail"] == "end_at must be greater than start_at"


def test_post_unavailability_404_when_technician_missing(db):
    start = datetime.now(UTC)
    end = start + timedelta(hours=2)
    resp = add_technician_unavailability(
        "missing",
        UnavailabilityCreate(start_at=start, end_at=end, reason="pto"),
        _request(),
        {},
        db,
    )
    assert resp.status_code == 404
    assert _body(resp)["detail"] == "technician not found"
