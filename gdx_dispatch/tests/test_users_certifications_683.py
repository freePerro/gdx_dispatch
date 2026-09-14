"""#683 — the Users edit dialog's Certifications field actually saves.

UsersView has always sent `certifications` on PATCH /api/users/{id}, and
`users.certifications` is a real column. `UserPatchIn` never declared the key,
so Pydantic dropped it: a typed value vanished, a certifications-only edit was
refused as "No fields to update", and `_serialize` never returned the column,
so the dialog reopened blank either way.

These run the real handler against an ORM-built SQLite schema: no mocked
session. The value has to come back out of the database. The handler gets the
id as a `uuid.UUID`: SQLite's `Uuid` bind cannot take the dashed string the
HTTP path carries (Postgres can), and the drop being tested happens in the
body model, not the path.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.routers.users import UserPatchIn, _serialize, update_user

TENANT = "tenant-683"


def _request() -> Request:
    req = Request({"type": "http", "method": "PATCH", "path": "/api/users/x", "headers": [], "query_string": b"", "client": ("127.0.0.1", 0)})
    req.state.tenant = {"id": TENANT}
    return req


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


def _make_user(db) -> User:
    u = User(id=uuid.uuid4(), company_id=TENANT, email="tech683@example.com", full_name="Tech 683", role="technician")
    db.add(u)
    db.commit()
    return u


def test_certifications_is_declared_on_the_patch_model():
    assert UserPatchIn(certifications="IDA certified").model_dump(exclude_unset=True) == {"certifications": "IDA certified"}


def test_a_certifications_only_edit_is_stored_returned_and_audited(db):
    u = _make_user(db)

    out = update_user(u.id, UserPatchIn(certifications="  IDA Certified Door Systems Technician  "), _request(), {"sub": "admin-1"}, db)

    assert out["certifications"] == "IDA Certified Door Systems Technician"
    db.expire_all()
    stored = db.execute(select(User.certifications).where(User.id == u.id)).scalar_one()
    assert stored == "IDA Certified Door Systems Technician"
    audit = db.execute(select(AuditLog).where(AuditLog.action == "user_updated")).scalars().all()
    assert len(audit) == 1
    assert "certifications" in (audit[0].details or {}).get("changed_fields", [])


def test_the_dialog_reopens_with_the_saved_value(db):
    u = _make_user(db)
    update_user(u.id, UserPatchIn(certifications="Liftmaster dealer"), _request(), {"sub": "admin-1"}, db)
    db.expire_all()
    assert _serialize(db.get(User, u.id))["certifications"] == "Liftmaster dealer"


def test_null_or_blank_clears_it(db):
    u = _make_user(db)
    update_user(u.id, UserPatchIn(certifications="Liftmaster dealer"), _request(), {"sub": "admin-1"}, db)
    update_user(u.id, UserPatchIn(certifications=None), _request(), {"sub": "admin-1"}, db)
    db.expire_all()
    assert db.get(User, u.id).certifications is None
    update_user(u.id, UserPatchIn(certifications="x"), _request(), {"sub": "admin-1"}, db)
    update_user(u.id, UserPatchIn(certifications="   "), _request(), {"sub": "admin-1"}, db)
    db.expire_all()
    assert db.get(User, u.id).certifications is None
    assert _serialize(db.get(User, u.id))["certifications"] == ""


def test_an_edit_that_omits_it_leaves_it_alone(db):
    u = _make_user(db)
    update_user(u.id, UserPatchIn(certifications="Liftmaster dealer"), _request(), {"sub": "admin-1"}, db)
    update_user(u.id, UserPatchIn(phone="555-0100"), _request(), {"sub": "admin-1"}, db)
    db.expire_all()
    assert db.get(User, u.id).certifications == "Liftmaster dealer"
