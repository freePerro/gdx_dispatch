from __future__ import annotations

import json
import uuid
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

# What get_current_user returns for a signed-in user (GDXA-981: the handlers
# audit under this id and refuse a user dict without one).
_USER = {"user_id": "office-user-1", "tenant_id": "tenant-a", "role": "admin"}


def _request(tenant_id: str = "tenant-a") -> Request:
    req = Request({"type": "http", "headers": []})
    req.state.tenant = {"id": tenant_id}
    return req


def _body(resp) -> dict | list:
    return json.loads(resp.body.decode())


def _user(db: Session, label: str = "tech", **fields) -> str:
    """A real login to link a technician to (GDXA-1583: user_id must be one)."""
    from gdx_dispatch.models.tenant_models import User

    uid = uuid.uuid4()
    db.add(User(id=uid, company_id="tenant-a", email=f"{label}@example.com", name=label, **fields))
    db.commit()
    return str(uid)


def _create_tech(db: Session, tenant_id: str = "tenant-a", **overrides) -> dict:
    payload = {"skills": ["spring", "cables"], "hourly_rate": 80.0}
    payload.update(overrides)
    payload.setdefault("user_id", _user(db))
    resp = create_technician(TechnicianCreate(**payload), _request(tenant_id), _USER, db)
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
    uid = _user(db, "user-100")
    data = _create_tech(db, user_id=uid)
    assert data["id"]
    assert data["user_id"] == uid
    assert data["skills"] == ["spring", "cables"]
    assert Decimal(str(data["hourly_rate"])) == Decimal("80.0")
    assert data["deleted_at"] is None
    assert data["active"] is True


def test_create_technician_honours_an_unticked_active_box(db):
    """#683: TechniciansView sends `active` on create. The model dropped it and
    the handler hard-coded True, so a technician added as inactive was
    dispatchable anyway."""
    data = _create_tech(db, active=False)
    assert data["active"] is False
    assert _body(get_technician(data["id"], _request(), _USER, db))["active"] is False


def test_create_technician_requires_user_id(db):
    resp = create_technician(TechnicianCreate(skills=["spring"], hourly_rate=75.0), _request(), _USER, db)
    assert resp.status_code == 400
    assert _body(resp)["detail"] == "user_id is required"


def test_list_technicians(db):
    a = _create_tech(db, user_id=_user(db, "user-a"))
    b = _create_tech(db, user_id=_user(db, "user-b"))
    resp = list_technicians(_request(), False, _USER, db)
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
    ta = _create_tech(db, "tenant-a", user_id=_user(db, "tenant-a-user"))
    tb = _create_tech(db, "tenant-b", user_id=_user(db, "tenant-b-user"))
    rows_a = _body(list_technicians(_request("tenant-a"), False, _USER, db))
    ids_a = [r["id"] for r in rows_a]
    assert ta["id"] in ids_a
    assert tb["id"] in ids_a


def test_get_technician(db):
    uid = _user(db, "user-detail")
    created = _create_tech(db, user_id=uid)
    resp = get_technician(created["id"], _request(), _USER, db)
    assert resp.status_code == 200
    data = _body(resp)
    assert data["id"] == created["id"]
    assert data["user_id"] == uid


def test_get_technician_not_found(db):
    resp = get_technician("not-a-real-id", _request(), _USER, db)
    assert resp.status_code == 404
    assert _body(resp)["detail"] == "technician not found"


def test_patch_technician(db):
    created = _create_tech(db, user_id=_user(db, "user-patch"), skills=["opener"], hourly_rate=55.0)
    resp = patch_technician(
        created["id"],
        TechnicianPatch(skills=["opener", "springs"], hourly_rate=70.5, active=False),
        _request(),
        _USER,
        db,
    )
    assert resp.status_code == 200
    data = _body(resp)
    assert data["id"] == created["id"]
    assert data["skills"] == ["opener", "springs"]
    assert Decimal(str(data["hourly_rate"])) == Decimal("70.5")
    assert data["active"] is False


def test_patch_technician_not_found(db):
    resp = patch_technician("missing", TechnicianPatch(hourly_rate=99.0), _request(), _USER, db)
    assert resp.status_code == 404
    assert _body(resp)["detail"] == "technician not found"


def test_delete_technician_soft_delete_and_hidden_from_list(db):
    created = _create_tech(db, user_id=_user(db, "user-delete"))
    resp = delete_technician(created["id"], _request(), _USER, db)
    assert resp.status_code == 200
    assert _body(resp) == {"deleted": True}

    rows = _body(list_technicians(_request(), False, _USER, db))
    ids = [r["id"] for r in rows]
    assert created["id"] not in ids

    get_resp = get_technician(created["id"], _request(), _USER, db)
    assert get_resp.status_code == 404


def test_get_skills_returns_list(db):
    created = _create_tech(db, skills=["rollers", "tracks"])
    resp = list_technician_skills(created["id"], _request(), _USER, db)
    assert resp.status_code == 200
    assert _body(resp) == {"skills": ["rollers", "tracks"]}


def test_add_skill(db):
    created = _create_tech(db, skills=["springs"])
    resp = add_technician_skill(created["id"], TechnicianSkillCreate(skill="openers"), _request(), _USER, db)
    assert resp.status_code == 200
    assert _body(resp)["skills"] == ["springs", "openers"]


def test_add_skill_deduplicates(db):
    created = _create_tech(db, skills=["springs"])
    resp = add_technician_skill(created["id"], TechnicianSkillCreate(skill="springs"), _request(), _USER, db)
    assert resp.status_code == 200
    assert _body(resp)["skills"] == ["springs"]


def test_add_skill_requires_skill_value(db):
    created = _create_tech(db, skills=["springs"])
    resp = add_technician_skill(created["id"], TechnicianSkillCreate(skill="   "), _request(), _USER, db)
    assert resp.status_code == 400
    assert _body(resp)["detail"] == "skill is required"


def test_get_availability_returns_unavailability_windows(db):
    created = _create_tech(db, user_id=_user(db, "user-avail"))
    start = datetime.now(UTC)
    end = start + timedelta(hours=4)
    create_resp = add_technician_unavailability(
        created["id"],
        UnavailabilityCreate(start_at=start, end_at=end, reason="training"),
        _request(),
        _USER,
        db,
    )
    assert create_resp.status_code == 201

    resp = get_technician_availability(created["id"], _request(), _USER, db)
    assert resp.status_code == 200
    rows = _body(resp)
    assert len(rows) == 1
    assert rows[0]["reason"] == "training"


def test_post_unavailability_validates_range(db):
    created = _create_tech(db, user_id=_user(db, "user-bad-range"))
    start = datetime.now(UTC)
    end = start - timedelta(minutes=1)
    resp = add_technician_unavailability(
        created["id"],
        UnavailabilityCreate(start_at=start, end_at=end, reason="invalid"),
        _request(),
        _USER,
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
        _USER,
        db,
    )
    assert resp.status_code == 404
    assert _body(resp)["detail"] == "technician not found"


def test_a_write_without_a_user_id_is_refused_before_it_writes(db):
    """GDXA-981: the audit row used to fall back to "system" when there was no
    user. Now a write with no user id is a 401 and leaves no row behind."""
    from fastapi import HTTPException

    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.models.tenant_models import Technician

    with pytest.raises(HTTPException) as exc:
        create_technician(TechnicianCreate(user_id=_user(db, "anon")), _request(), {}, db)
    assert exc.value.status_code == 401
    assert db.query(Technician).count() == 0
    assert db.query(AuditLog).count() == 0


# ── GDXA-1583: a technician row links to a real login ──────────────────────


@pytest.mark.parametrize("raw", ["walk683.inactive.tech", "amber@x.com", str(uuid.uuid4())])
def test_create_refuses_a_user_id_that_is_not_a_user(db, raw):
    """The New Technician form fell back to the email or a name slug, which no
    per-tech join (assignments, locations, day close) can ever match. A slug,
    an email and a well-formed UUID with no user behind it are all refused,
    and nothing is written."""
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.models.tenant_models import Technician

    resp = create_technician(TechnicianCreate(user_id=raw, name="Ghost"), _request(), _USER, db)
    assert resp.status_code == 400
    assert _body(resp)["detail"] == "user_id must be the id of an existing user"
    assert db.query(Technician).count() == 0
    assert db.query(AuditLog).count() == 0


def test_create_refuses_a_soft_deleted_user(db):
    uid = _user(db, "gone", deleted_at=datetime.now(UTC))
    resp = create_technician(TechnicianCreate(user_id=uid, name="Gone"), _request(), _USER, db)
    assert resp.status_code == 400


def test_create_stores_the_dashed_id_the_joins_compare_against(db):
    """Per-tech joins compare Technician.user_id to str(users.id), the dashed
    form; an undashed or upper-case id is stored canonical, so it still joins."""
    uid = _user(db, "pat")
    data = _create_tech(db, user_id=uid.replace("-", "").upper())
    assert data["user_id"] == uid


def test_patch_refuses_relinking_to_a_user_that_does_not_exist(db):
    from gdx_dispatch.models.tenant_models import Technician

    uid = _user(db, "linked")
    created = _create_tech(db, user_id=uid)
    resp = patch_technician(created["id"], TechnicianPatch(user_id="pat.tech"), _request(), _USER, db)
    assert resp.status_code == 400
    assert _body(resp)["detail"] == "user_id must be the id of an existing user"
    db.expire_all()
    assert db.get(Technician, created["id"]).user_id == uid


def test_patch_relinks_an_old_slug_row_to_a_real_user(db):
    """A row stored before the fix holds a slug; picking its login repairs it
    and the audit row records the id that was stored."""
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.models.tenant_models import Technician

    now = datetime.now(UTC)
    db.add(Technician(id=str(uuid.uuid4()), company_id="tenant-a", user_id="walk683.inactive.tech",
                      name="Walk", created_at=now, updated_at=now))
    db.commit()
    tech = db.query(Technician).one()
    uid = _user(db, "walk")
    resp = patch_technician(tech.id, TechnicianPatch(user_id=uid), _request(), _USER, db)
    assert resp.status_code == 200
    assert _body(resp)["user_id"] == uid
    audit = db.query(AuditLog).filter(AuditLog.action == "technician_updated").one()
    details = audit.details if isinstance(audit.details, dict) else json.loads(audit.details)
    assert details["user_id"] == uid


def test_an_edit_keeping_the_link_works_after_the_login_was_deleted(db):
    """Deleting a user soft-deletes the login and leaves its technician row.
    Re-sending the stored user_id is no change, so the office can still
    deactivate the departed tech; a new link is still checked."""
    from gdx_dispatch.models.tenant_models import User

    uid = _user(db, "departed")
    created = _create_tech(db, user_id=uid)
    db.get(User, uuid.UUID(uid)).deleted_at = datetime.now(UTC)
    db.commit()
    resp = patch_technician(created["id"], TechnicianPatch(user_id=uid, active=False), _request(), _USER, db)
    assert resp.status_code == 200, resp.body
    assert _body(resp)["active"] is False
    other = str(uuid.uuid4())
    resp = patch_technician(created["id"], TechnicianPatch(user_id=other), _request(), _USER, db)
    assert resp.status_code == 400
