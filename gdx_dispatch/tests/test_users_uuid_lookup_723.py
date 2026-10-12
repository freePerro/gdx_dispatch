"""GDXA-723 — users/auth look a user up by a UUID, not by the str id.

``users.id`` is a Uuid column. users._get_user_or_404, /auth/reset-password
and /auth/refresh bound the str id straight against it: SQLite's bind calls
``.hex`` on the value and raised StatementError, so /me, self-update,
change-password, reset-password, lock and delete could not resolve anyone on
SQLite, and a malformed id was a 500 on either dialect instead of a 404.

Everything here runs the real routers against a real in-memory SQLite
database built from the ORM — the lookups are real, nothing stands in for the
users row. Only the Redis-backed session revoke is recorded, not executed.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import fakeredis
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from werkzeug.security import check_password_hash, generate_password_hash

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.routers.auth import core as auth_core
from gdx_dispatch.routers.auth import get_current_user

TENANT = "72372372-3723-4723-8723-723723723723"
OLD_PASSWORD = "Old-Passw0rd-723"
NEW_PASSWORD = "New-Passw0rd-723"


@pytest.fixture()
def env(monkeypatch):
    from gdx_dispatch.core import auth_revoke
    from gdx_dispatch.routers import users as users_mod

    engine = create_engine(
        "sqlite://", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    admin_id, target_id = uuid4(), uuid4()
    with SessionLocal() as db:
        db.add(User(id=admin_id, username="Pat Admin", email="pat.admin@example.com",
                    role="admin", company_id=TENANT, active=True,
                    password_hash=generate_password_hash(OLD_PASSWORD)))
        db.add(User(id=target_id, username="Terry Tech", email="terry.tech@example.com",
                    role="technician", company_id=TENANT, active=True,
                    password_hash="old-hash"))
        db.commit()

    app = FastAPI()

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(users_mod.router)
    app.include_router(auth_core.router)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    # `sub` is what login mints: str(user.id), lower-case and dashed.
    login = {"user_id": str(admin_id), "sub": str(admin_id), "tenant_id": TENANT, "role": "admin"}
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: dict(login)
    app.dependency_overrides[require_module("jobs")] = lambda: None

    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(auth_core, "redis", fake_redis)
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", "memory")
    revoked: list[str] = []
    monkeypatch.setattr(auth_revoke, "revoke_user_sessions",
                        lambda sub, reason="": revoked.append(sub) or 1)

    yield {
        "client": TestClient(app, raise_server_exceptions=True), "db": SessionLocal,
        "redis": fake_redis, "revoked": revoked,
        "admin_id": admin_id, "target_id": target_id,
    }
    app.dependency_overrides.clear()
    engine.dispose()


def _user(SessionLocal, uid: UUID) -> User:
    with SessionLocal() as db:
        return db.get(User, uid)


def _audit_ids(SessionLocal, action: str) -> list[str]:
    with SessionLocal() as db:
        return [r.entity_id for r in db.execute(select(AuditLog).where(AuditLog.action == action)).scalars()]


def test_me_resolves_the_logged_in_user(env):
    r = env["client"].get("/api/users/me")
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "pat.admin@example.com"


def test_self_update_writes_the_row(env):
    r = env["client"].patch("/api/users/me", json={"phone": "555-0723"})
    assert r.status_code == 200, r.text
    assert _user(env["db"], env["admin_id"]).phone == "555-0723"


def test_change_my_password_writes_the_row(env):
    r = env["client"].post("/api/users/me/change-password", json={
        "current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD,
    })
    assert r.status_code == 200, r.text
    assert check_password_hash(_user(env["db"], env["admin_id"]).password_hash, NEW_PASSWORD)


def test_admin_reset_password_writes_the_row(env):
    tid = env["target_id"]
    r = env["client"].post(f"/api/users/{tid}/reset-password", json={"new_password": NEW_PASSWORD})
    assert r.status_code == 200, r.text
    assert check_password_hash(_user(env["db"], tid).password_hash, NEW_PASSWORD)
    assert _audit_ids(env["db"], "user_password_reset") == [str(tid)]


def test_lockout_locks_the_row_and_revokes_the_session_family(env):
    tid = env["target_id"]
    r = env["client"].post(f"/api/users/{tid}/lockout", json={"reason": "terminated"})
    assert r.status_code == 200, r.text
    assert _user(env["db"], tid).active is False
    assert env["revoked"] == [str(tid)]


def test_delete_soft_deletes_the_row(env):
    tid = env["target_id"]
    r = env["client"].delete(f"/api/users/{tid}")
    assert r.status_code == 200, r.text
    assert _user(env["db"], tid).deleted_at is not None
    assert env["revoked"] == [str(tid)]


@pytest.mark.parametrize(("method", "path", "body"), [
    ("get", "/api/users/{id}", None),
    ("patch", "/api/users/{id}", {"phone": "1"}),
    ("post", "/api/users/{id}/role", {"role": "dispatch"}),
    ("post", "/api/users/{id}/lockout", {"reason": "terminated"}),
    ("post", "/api/users/{id}/unlock", None),
    ("get", "/api/users/{id}/lockout-info", None),
    ("post", "/api/users/{id}/toggle-schedulable", None),
    ("post", "/api/users/{id}/reset-password", {"new_password": NEW_PASSWORD}),
    ("post", "/api/users/{id}/send-reset-link", None),
    ("delete", "/api/users/{id}", None),
])
def test_a_malformed_id_is_a_404_not_a_500(env, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    r = getattr(env["client"], method)(path.format(id="not-a-uuid"), **kwargs)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "User not found"


@pytest.mark.parametrize("spell", [str.upper, lambda s: s.replace("-", "")])
@pytest.mark.parametrize(("method", "path", "body", "refusal"), [
    ("post", "/api/users/{id}/role", {"role": "dispatch"}, "Cannot change your own role"),
    ("post", "/api/users/{id}/lockout", {"reason": "terminated"}, "Cannot lock yourself out"),
    ("delete", "/api/users/{id}", None, "Cannot delete your own account"),
])
def test_another_spelling_of_your_own_id_is_still_you(env, spell, method, path, body, refusal):
    # The path now accepts any UUID spelling, so the self-guards compare the
    # row's id, not the path string, with the token's sub.
    kwargs = {"json": body} if body is not None else {}
    r = getattr(env["client"], method)(path.format(id=spell(str(env["admin_id"]))), **kwargs)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == refusal
    assert env["revoked"] == []


def test_another_spelling_revokes_and_audits_the_canonical_id(env):
    # The revoke is keyed refresh_family:{str(users.id)}; revoking under the
    # path's spelling would leave the locked user's sessions alive.
    tid = env["target_id"]
    r = env["client"].post(f"/api/users/{str(tid).upper()}/lockout", json={"reason": "terminated"})
    assert r.status_code == 200, r.text
    assert env["revoked"] == [str(tid)]
    assert _audit_ids(env["db"], "user_locked") == [str(tid)]
    info = env["client"].get(f"/api/users/{str(tid).replace('-', '')}/lockout-info")
    assert info.status_code == 200, info.text


def test_auth_reset_password_resolves_the_stored_user(env):
    tid = env["target_id"]
    env["redis"].set("pw_reset:tok-723-gdxa-reset", f"{tid}|{TENANT}")
    r = env["client"].post("/auth/reset-password", json={"token": "tok-723-gdxa-reset", "new_password": NEW_PASSWORD})
    assert r.status_code == 200, r.text
    assert check_password_hash(_user(env["db"], tid).password_hash, NEW_PASSWORD)


def test_auth_reset_password_with_a_malformed_stored_id_is_a_400(env):
    env["redis"].set("pw_reset:tok-bad-gdxa-reset", f"not-a-uuid|{TENANT}")
    r = env["client"].post("/auth/reset-password", json={"token": "tok-bad-gdxa-reset", "new_password": NEW_PASSWORD})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "User not found."


def _refresh_cookie(env, sub: str) -> None:
    token, jti = auth_core._issue(sub, TENANT, "admin", auth_core.REFRESH_TTL, "refresh")
    env["redis"].sadd(f"refresh_family:{sub}", jti)
    env["client"].cookies.set("refresh_token", token)


def test_refresh_resolves_the_user_row(env):
    _refresh_cookie(env, str(env["admin_id"]))
    r = env["client"].post("/auth/refresh")
    assert r.status_code == 200, r.text


def test_refresh_with_a_non_uuid_sub_is_a_401_that_revokes_the_family(env):
    _refresh_cookie(env, "not-a-uuid")
    r = env["client"].post("/auth/refresh")
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "User no longer eligible to refresh"
    assert env["redis"].smembers("refresh_family:not-a-uuid") == set()


def test_permission_check_reads_the_role_from_the_users_row(env, caplog):
    # core.modules resolves the role from users.role before the JWT claim. A
    # str bind raised on SQLite, was swallowed and logged, and the token's
    # role decided the request instead.
    import logging

    with caplog.at_level(logging.ERROR, logger="gdx_dispatch.core.modules"):
        r = env["client"].post(f"/api/users/{env['target_id']}/toggle-schedulable")
    assert r.status_code == 200, r.text
    assert not [rec for rec in caplog.records if "user_role_lookup_failed" in rec.getMessage()]


def test_a_demoted_user_with_a_stale_admin_token_is_refused(env):
    # The row says technician, the token still says admin: users.role wins. If
    # the lookup cannot bind (or never runs), the token's admin role lets it through.
    with env["db"]() as db:
        db.get(User, env["admin_id"]).role = "technician"
        db.commit()
    r = env["client"].post(f"/api/users/{env['target_id']}/toggle-schedulable")
    assert r.status_code == 403, r.text
