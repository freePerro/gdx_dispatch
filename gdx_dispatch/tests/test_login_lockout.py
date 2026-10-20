"""GDXA-1995: per-account login lockout on users.locked_until.

The login gate read ``locked_until`` but nothing ever set it, so there was no
lockout. These drive the real /auth/login against a real SQLite database (the
backend that hands ``locked_until`` back naive) and read the row and the audit
log back, not a mock.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash

from gdx_dispatch.core import database as database_mod
from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.routers import users as users_router
from gdx_dispatch.routers.auth import core as auth_core
from gdx_dispatch.tests.conftest import make_fresh_db

TID = "00000000-0000-0000-0000-0000000000aa"
EMAIL = "tech@example.com"
PW = "the-right-password"
LIMIT = auth_core.LOGIN_LOCK_THRESHOLD


class _FakeRedis:
    def sadd(self, *_a) -> None: ...
    def expire(self, *_a) -> None: ...


@pytest.fixture()
def env(monkeypatch) -> Iterator[tuple[TestClient, sessionmaker]]:
    engine = make_fresh_db()
    # The admin unlock tests authenticate a real token; _db_verify_user reads
    # the row through app_engine, so point it at this DB.
    monkeypatch.setattr(database_mod, "app_engine", engine)
    monkeypatch.setenv("AUTH_DB_VERIFY_ENABLED", "1")
    monkeypatch.setattr(auth_core, "redis", _FakeRedis())
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    app = FastAPI()
    app.state.denylist = Denylist(redis_client=None)

    @app.middleware("http")
    async def _tenant(request: Request, call_next):
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
        yield tc, Session
    engine.dispose()


def _make_user(Session, **overrides) -> uuid.UUID:
    uid = uuid.uuid4()
    fields = dict(
        id=uid, username=EMAIL, email=EMAIL, full_name="Tech",
        password_hash=generate_password_hash(PW), role="technician",
        company_id=TID, active=True,
    )
    fields.update(overrides)
    with Session() as s:
        s.add(User(**fields))
        s.commit()
    return uid


def _row(Session, uid: uuid.UUID) -> User:
    with Session() as s:
        return s.get(User, uid)


def _audits(Session, action: str) -> list[AuditLog]:
    with Session() as s:
        return list(s.execute(select(AuditLog).where(AuditLog.action == action)).scalars())


def _login(tc: TestClient, password: str, email: str = EMAIL):
    return tc.post("/auth/login", json={"email": email, "password": password})


def test_tenth_failure_locks_and_the_eleventh_attempt_is_refused_even_with_the_right_password(env) -> None:
    tc, Session = env
    uid = _make_user(Session)

    for i in range(1, LIMIT + 1):
        r = _login(tc, f"wrong-{i}")
        assert r.status_code == 401, r.text
        if i < LIMIT:
            assert _row(Session, uid).failed_login_count == i
            assert _row(Session, uid).locked_until is None

    row = _row(Session, uid)
    assert row.locked_until is not None, "the 10th failure must set locked_until"
    locked_until = row.locked_until.replace(tzinfo=UTC) if row.locked_until.tzinfo is None else row.locked_until
    remaining = locked_until - datetime.now(UTC)
    assert timedelta(minutes=14) < remaining <= timedelta(minutes=15)
    assert row.failed_login_count == 0
    locked = _audits(Session, "login_locked")
    assert len(locked) == 1
    assert locked[0].entity_id == str(uid)
    assert locked[0].details["failed_attempts"] == LIMIT

    # The 11th attempt, with the CORRECT password, is refused.
    r = _login(tc, PW)
    assert r.status_code == 401, r.text
    assert "access_token" not in r.json()
    assert [a.details["reason"] for a in _audits(Session, "login_blocked")] == ["user_locked"]
    # A wrong 11th is refused too and does not count or extend the lock.
    assert _login(tc, "wrong-11").status_code == 401
    assert _row(Session, uid).failed_login_count == 0
    assert _row(Session, uid).locked_until == row.locked_until
    assert _audits(Session, "login_success") == []


def test_refusal_body_does_not_reveal_whether_the_account_exists(env) -> None:
    # The body only: response timing still differs (an unknown or locked
    # account skips the password hash), which predates GDXA-1995.
    tc, Session = env
    _make_user(Session)
    wrong = _login(tc, "wrong")
    unknown = _login(tc, "wrong", email="nobody@example.com")
    for i in range(LIMIT - 1):
        _login(tc, f"wrong-{i}")
    locked = _login(tc, PW)
    unknown_after = _login(tc, PW, email="nobody@example.com")
    bodies = {(r.status_code, r.text) for r in (wrong, unknown, locked, unknown_after)}
    assert bodies == {(401, '{"detail":"Invalid credentials"}')}


def test_unknown_email_is_never_counted_or_locked(env) -> None:
    tc, Session = env
    for _ in range(LIMIT + 1):
        assert _login(tc, "wrong", email="nobody@example.com").status_code == 401
    assert _audits(Session, "login_locked") == []


def test_success_clears_the_count_and_audits_the_reset(env) -> None:
    tc, Session = env
    uid = _make_user(Session)
    for i in range(LIMIT - 1):
        _login(tc, f"wrong-{i}")
    assert _row(Session, uid).failed_login_count == LIMIT - 1

    r = _login(tc, PW)
    assert r.status_code == 200, r.text
    assert _row(Session, uid).failed_login_count == 0
    resets = _audits(Session, "login_lockout_reset")
    assert len(resets) == 1 and resets[0].details["failed_attempts"] == LIMIT - 1

    # One more wrong password starts again from 1, not from 10: no lock.
    _login(tc, "wrong")
    assert _row(Session, uid).failed_login_count == 1
    assert _row(Session, uid).locked_until is None


def test_clean_login_writes_no_reset_row(env) -> None:
    tc, Session = env
    _make_user(Session)
    assert _login(tc, PW).status_code == 200
    assert _audits(Session, "login_lockout_reset") == []


def test_a_lapsed_lock_lets_the_right_password_in_and_is_cleared(env) -> None:
    tc, Session = env
    uid = _make_user(Session, locked_until=datetime.now(UTC) - timedelta(seconds=1))
    r = _login(tc, PW)
    assert r.status_code == 200, r.text
    assert _row(Session, uid).locked_until is None
    assert len(_audits(Session, "login_lockout_reset")) == 1


def _admin(Session) -> dict[str, str]:
    uid = _make_user(Session, email="owner@example.com", username="owner@example.com", role="owner")
    token, _ = auth_core._issue(str(uid), TID, "owner", 900, "access")
    return {"Authorization": f"Bearer {token}"}


def test_the_users_page_shows_the_lock_and_its_unlock_lifts_it(env) -> None:
    tc, Session = env
    uid = _make_user(Session)
    headers = _admin(Session)
    for i in range(LIMIT):
        _login(tc, f"wrong-{i}")
    assert _login(tc, PW).status_code == 401

    listed = {u["id"]: u for u in tc.get("/api/users", headers=headers).json()}
    assert listed[str(uid)]["active"] is True
    assert listed[str(uid)]["locked_until"], "the Users page cannot show a lock it is not sent"

    r = tc.post(f"/api/users/{uid}/unlock", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["locked_until"] is None
    assert _row(Session, uid).locked_until is None
    unlocked = _audits(Session, "user_unlocked")
    assert len(unlocked) == 1 and unlocked[0].details["login_lock_cleared"]

    assert _login(tc, PW).status_code == 200


def test_unlock_still_refuses_an_active_unlocked_user(env) -> None:
    tc, Session = env
    uid = _make_user(Session)
    headers = _admin(Session)
    assert tc.post(f"/api/users/{uid}/unlock", headers=headers).status_code == 409
    assert _audits(Session, "user_unlocked") == []
