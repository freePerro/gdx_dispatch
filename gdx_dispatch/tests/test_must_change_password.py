"""GDXA-1003: must_change_password is enforced server-side and cleared.

The flag used to be written (bootstrap owner, admin-created invitees) and
echoed by /auth/login, and read by nothing else: a flagged account could use
the log-printed bootstrap password forever, and nothing ever cleared it.

These run the real users and auth routers against a real SQLite database with
real signed access tokens; ``_db_verify_user`` reads the row through a patched
``app_engine``, which is the path production takes.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash

from gdx_dispatch.core import database as database_mod
from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.routers import users as users_router
from gdx_dispatch.routers.auth import core as auth_core
from gdx_dispatch.tests.conftest import make_fresh_db

TID = "00000000-0000-0000-0000-0000000000aa"
OLD_PW = "bootstrap-printed-pw"
NEW_PW = "a-new-password-123"


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


@pytest.fixture()
def env(monkeypatch) -> Iterator[tuple[TestClient, sessionmaker, _FakeRedis]]:
    engine = make_fresh_db()
    # _db_verify_user imports app_engine at call time; point it at this DB.
    monkeypatch.setattr(database_mod, "app_engine", engine)
    monkeypatch.setenv("AUTH_DB_VERIFY_ENABLED", "1")
    fake_redis = _FakeRedis()
    monkeypatch.setattr(auth_core, "redis", fake_redis)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    app = FastAPI()
    app.state.denylist = Denylist(redis_client=None)

    @app.middleware("http")
    async def _tenant(request: Request, call_next):
        # db_url present → _db_verify_user does the real lookup.
        request.state.tenant = {"id": TID, "db_url": "sqlite://"}
        return await call_next(request)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.include_router(users_router.router)
    app.include_router(auth_core.router)
    with TestClient(app) as tc:
        yield tc, Session, fake_redis
    engine.dispose()


def _make_user(Session, *, flagged: bool | None, email: str = "owner@example.com") -> str:
    uid = uuid.uuid4()
    with Session() as s:
        s.add(User(
            id=uid, username=email, email=email, full_name="Owner",
            password_hash=generate_password_hash(OLD_PW), role="owner",
            company_id=TID, active=True, must_change_password=flagged,
        ))
        s.commit()
    return str(uid)


def _bearer(uid: str) -> dict[str, str]:
    token, _ = auth_core._issue(uid, TID, "owner", 900, "access")
    return {"Authorization": f"Bearer {token}"}


def _flag(Session, uid: str) -> bool | None:
    with Session() as s:
        return s.execute(select(User.must_change_password).where(User.id == uuid.UUID(uid))).scalar_one()


def _audit_details(Session, action: str) -> list[dict]:
    with Session() as s:
        rows = s.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()
        return [r.details or {} for r in rows]


def test_flagged_session_is_refused_with_the_contract_code(env) -> None:
    tc, Session, _ = env
    uid = _make_user(Session, flagged=True)
    r = tc.get("/api/users", headers=_bearer(uid))
    assert r.status_code == 403, r.text
    # The {code, message} shape is one of the two the GDXA-1004 SPA handler accepts.
    assert r.json()["detail"]["code"] == "password_change_required"
    r = tc.get("/api/users/me/permissions", headers=_bearer(uid))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "password_change_required"


def test_flagged_session_reaches_the_allowlist(env) -> None:
    tc, Session, _ = env
    uid = _make_user(Session, flagged=True)
    r = tc.get("/api/users/me", headers=_bearer(uid))
    assert r.status_code == 200, r.text
    assert r.json()["must_change_password"] is True


def test_change_password_clears_the_flag_and_unlocks(env) -> None:
    tc, Session, _ = env
    uid = _make_user(Session, flagged=True)
    headers = _bearer(uid)
    r = tc.post("/api/users/me/change-password", headers=headers,
                json={"current_password": OLD_PW, "new_password": NEW_PW})
    assert r.status_code == 200, r.text
    assert _flag(Session, uid) is False
    # Same access token, next request: released.
    assert tc.get("/api/users", headers=headers).status_code == 200
    assert tc.get("/api/users/me", headers=headers).json()["must_change_password"] is False
    assert {"must_change_password_cleared": True, "device_alerts_revoked": 0} in _audit_details(Session, "user_self_password_changed")


def test_wrong_current_password_leaves_the_flag(env) -> None:
    tc, Session, _ = env
    uid = _make_user(Session, flagged=True)
    r = tc.post("/api/users/me/change-password", headers=_bearer(uid),
                json={"current_password": "not-it-at-all", "new_password": NEW_PW})
    assert r.status_code == 400
    assert _flag(Session, uid) is True


def test_reset_link_clears_the_flag_for_an_invited_user(env) -> None:
    """admin_ops invites carry a random placeholder password and the flag;
    the reset link is their only way in, and it must not leave them flagged."""
    tc, Session, fake_redis = env
    uid = _make_user(Session, flagged=True, email="invitee@example.com")
    fake_redis.setex("pw_reset:invite-token-0001", 3600, f"{uid}|{TID}")
    r = tc.post("/auth/reset-password", json={"token": "invite-token-0001", "new_password": NEW_PW})
    assert r.status_code == 200, r.text
    assert _flag(Session, uid) is False
    with Session() as s:
        pw_hash = s.execute(select(User.password_hash).where(User.id == uuid.UUID(uid))).scalar_one()
    assert check_password_hash(pw_hash, NEW_PW)
    assert tc.get("/api/users", headers=_bearer(uid)).status_code == 200
    assert {"must_change_password_cleared": True, "device_alerts_revoked": 0} in _audit_details(Session, "password_reset_success")


@pytest.mark.parametrize("flag", [False, None])
def test_unflagged_user_is_unaffected(env, flag) -> None:
    """The demo seed clears the flag to False; older rows may hold NULL."""
    tc, Session, _ = env
    uid = _make_user(Session, flagged=flag)
    assert tc.get("/api/users", headers=_bearer(uid)).status_code == 200
    assert tc.get("/api/users/me", headers=_bearer(uid)).json()["must_change_password"] is False


def test_login_still_works_for_a_flagged_user_and_reports_the_flag(env) -> None:
    tc, Session, fake_redis = env
    fake_redis.sadd = lambda *a, **k: None  # refresh-family bookkeeping
    fake_redis.expire = lambda *a, **k: None
    _make_user(Session, flagged=True)
    r = tc.post("/auth/login", json={"email": "owner@example.com", "password": OLD_PW})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["must_change_password"] is True
