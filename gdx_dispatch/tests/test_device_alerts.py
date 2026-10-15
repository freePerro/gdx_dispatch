"""Office alerts on the Android app — device token, revoke, bell long-poll.

The token is opaque and stored as a
SHA-256; every feed request re-checks the token row, its age, the user and
``nav.office`` against the database.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401
from gdx_dispatch.models.tenant_models import DeviceAlertRegistration, Notification, User
from gdx_dispatch.routers import device_alerts
from gdx_dispatch.routers.auth import get_current_user

TENANT_ID = "tenant-test"
HEADER = device_alerts.TOKEN_HEADER


class _Env:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        TenantBase.metadata.create_all(self.engine, checkfirst=True)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.Session() as s:
            s.execute(
                text(
                    "INSERT INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
                    "VALUES ('g1', :tid, 'communications', datetime('now'), datetime('now'))"
                ),
                {"tid": TENANT_ID},
            )
            s.commit()
        self.current_user: dict = {}
        app = FastAPI()

        @app.middleware("http")
        async def inject_tenant(request, call_next):
            request.state.tenant = {"id": TENANT_ID}
            return await call_next(request)

        app.include_router(device_alerts.router)

        def _db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[device_alerts.feed_session_factory] = lambda: self.Session
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.client = TestClient(app, raise_server_exceptions=True)

    def add_user(self, role: str, *, active: bool = True) -> str:
        uid = uuid4()
        with self.Session() as s:
            s.add(User(id=uid, email=f"{uid.hex[:8]}@example.com", role=role, active=active,
                       company_id=TENANT_ID))
            s.commit()
        return str(uid)

    def sign_in(self, uid: str, role: str) -> None:
        self.current_user = {"sub": uid, "user_id": uid, "role": role, "tenant_id": TENANT_ID}

    def mint(self, uid: str, role: str = "dispatcher") -> str:
        self.sign_in(uid, role)
        r = self.client.post("/api/notifications/device-alerts/token", json={"device_label": "Pixel"})
        assert r.status_code == 200, r.text
        return r.json()["token"]

    def bell(self, *, user_id: str | None = None, created: datetime | None = None, **kw) -> str:
        nid = uuid4().hex
        with self.Session() as s:
            s.add(Notification(
                id=nid, tenant_id=TENANT_ID, user_id=user_id,
                title=kw.get("title", "New text"), message=kw.get("message", "hi"),
                category=kw.get("category", "sms"), is_read=kw.get("is_read", 0),
                created_at=(created or datetime.now(UTC)).isoformat(),
                deleted_at=kw.get("deleted_at"),
            ))
            s.commit()
        return nid

    def feed(self, token: str, after: str | None = None, wait: int = 0, scope: str | None = None):
        params = {"wait": wait}
        if after:
            params["after"] = after
        if scope:
            params["scope"] = scope
        return self.client.get("/api/notifications/device-alerts/feed", params=params,
                               headers={HEADER: token})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(device_alerts, "POLL_INTERVAL_SECONDS", 0.05)
    return _Env()


def test_mint_stores_only_the_hash_and_audits(env):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    with env.Session() as s:
        row = s.execute(select(DeviceAlertRegistration)).scalar_one()
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in (row.token_hash, row.id)
        assert row.user_id == uid and row.device_label == "Pixel"
        actions = [a.action for a in s.execute(select(AuditLog)).scalars()]
    assert "device_alerts_enabled" in actions


def test_mint_names_whose_alerts_they_are(env):
    # The phone keeps this and turns alerts off when someone else signs in.
    uid = env.add_user("dispatcher")
    env.sign_in(uid, "dispatcher")
    r = env.client.post("/api/notifications/device-alerts/token", json={})
    assert r.status_code == 200
    assert r.json()["user_id"] == uid


def test_revoke_for_a_different_sign_in_is_recorded(env):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    r = env.client.post("/api/notifications/device-alerts/revoke", json={"reason": "user_changed"},
                        headers={HEADER: token})
    assert r.status_code == 200
    assert env.feed(token).status_code == 401
    with env.Session() as s:
        assert s.execute(select(DeviceAlertRegistration)).scalar_one().revoked_reason == "user_changed"


def test_every_server_reason_survives_the_phones_sanitiser():
    # The phone builds the revoke body by hand and strips characters first.
    # The first version stripped "_", so user_changed reached the server as
    # userchanged, was 422'd, and the token stayed live after the phone had
    # forgotten it. Gradle tests do not run in CI, so this guard does: it
    # applies the Java character class (same syntax in Python) to every
    # reason the server accepts.
    import re
    from pathlib import Path

    java = (Path(__file__).resolve().parents[1] / "frontend/android/app/src/main/java/com/gdx/dispatch"
            / "alerts/OfficeAlertsPlugin.java").read_text()
    strip = re.search(r'static String revokeBody\(String reason\) \{\s*return .*?reason\.replaceAll\("([^"]+)", ""\)',
                      java, re.S).group(1)
    pattern = next(m.pattern for m in device_alerts.DeviceAlertsRevokeRequest.model_fields["reason"].metadata
                   if hasattr(m, "pattern"))
    reasons = re.fullmatch(r"\^\((.*)\)\$", pattern).group(1).split("|")
    assert "user_changed" in reasons
    for reason in reasons:
        assert re.sub(strip, "", reason) == reason
        device_alerts.DeviceAlertsRevokeRequest(reason=re.sub(strip, "", reason))


def test_tech_cannot_mint(env):
    uid = env.add_user("technician")
    env.sign_in(uid, "technician")
    r = env.client.post("/api/notifications/device-alerts/token", json={})
    assert r.status_code == 403


def test_first_feed_call_returns_cursor_and_no_backlog(env):
    uid = env.add_user("dispatcher")
    env.bell(created=datetime.now(UTC) - timedelta(seconds=5))
    token = env.mint(uid)
    r = env.feed(token)
    assert r.status_code == 200
    assert r.json()["items"] == []
    assert r.json()["cursor"]


def test_feed_returns_new_bell_rows_for_me_and_broadcast_only(env):
    uid = env.add_user("dispatcher")
    other = env.add_user("dispatcher")
    token = env.mint(uid)
    cursor = env.feed(token).json()["cursor"]
    mine = env.bell(user_id=uid, title="Mine")
    broadcast = env.bell(title="All")
    env.bell(user_id=other, title="Theirs")
    env.bell(title="Read", is_read=1)
    env.bell(title="Deleted", deleted_at=datetime.now(UTC).isoformat())
    body = env.feed(token, after=cursor).json()
    assert {i["id"] for i in body["items"]} == {mine, broadcast}
    assert body["cursor"] > cursor


def test_planner_scope_sees_only_my_planner_rows(env):
    # GDX Planner polls with scope=planner: its own reminders, no office
    # broadcasts, nobody else's reminders.
    uid = env.add_user("dispatcher")
    other = env.add_user("dispatcher")
    token = env.mint(uid)
    cursor = env.feed(token, scope="planner").json()["cursor"]
    mine = env.bell(user_id=uid, title="Reminder: mine", category="planner_reminder")
    env.bell(user_id=other, title="Reminder: theirs", category="planner_reminder")
    env.bell(title="All", category="planner_reminder")  # a broadcast is never a reminder
    env.bell(user_id=uid, title="Mine, office", category="sms")
    env.bell(title="Broadcast", category="lead")
    env.bell(user_id=uid, title="Look-alike", category="plannerXreminder")  # "_" is literal
    body = env.feed(token, after=cursor, scope="planner").json()
    assert [i["id"] for i in body["items"]] == [mine]


def test_office_scope_leaves_planner_rows_to_gdx_planner(env):
    # The default feed drops planner_ rows so a reminder buzzes once, from
    # GDX Planner, not again from GDX Dispatch's office alerts.
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    cursor = env.feed(token).json()["cursor"]
    env.bell(user_id=uid, title="Reminder", category="planner_reminder")
    office = env.bell(user_id=uid, title="Text", category="sms")
    look_alike = env.bell(user_id=uid, title="Look-alike", category="plannerXreminder")
    body = env.feed(token, after=cursor).json()
    assert {i["id"] for i in body["items"]} == {office, look_alike}


def test_unknown_scope_is_422(env):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    assert env.feed(token, scope="everything").status_code == 422


def test_feed_overlap_resends_late_rows(env):
    """A row committed after the cursor was taken but stamped before it is
    still delivered: every response covers two minutes before the cursor."""
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    cursor = env.feed(token).json()["cursor"]
    late = env.bell(created=datetime.fromisoformat(cursor) - timedelta(seconds=30))
    body = env.feed(token, after=cursor).json()
    assert late in {i["id"] for i in body["items"]}
    old = env.bell(created=datetime.fromisoformat(cursor) - timedelta(minutes=5))
    assert old not in {i["id"] for i in env.feed(token, after=cursor).json()["items"]}


def test_feed_long_poll_wakes_on_new_row(env, monkeypatch):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    cursor = env.feed(token).json()["cursor"]
    calls = {"n": 0}
    real = device_alerts._has_newer

    def has_newer(*a):
        calls["n"] += 1
        if calls["n"] == 2:
            env.bell(title="arrived while waiting")
        return real(*a)

    monkeypatch.setattr(device_alerts, "_has_newer", has_newer)
    body = env.feed(token, after=cursor, wait=5).json()
    assert [i["title"] for i in body["items"]] == ["arrived while waiting"]
    assert calls["n"] == 2  # woke on the check that saw the row, not at the deadline


def test_unknown_token_is_401(env):
    assert env.feed("not-a-token").status_code == 401
    assert env.feed("").status_code == 401


def test_revoke_ends_delivery_and_audits(env):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    env.current_user = {}  # the app session is gone; the device token is enough
    r = env.client.post("/api/notifications/device-alerts/revoke", json={"reason": "logout"},
                        headers={HEADER: token})
    assert r.status_code == 200
    assert env.feed(token).status_code == 401
    # Idempotent.
    assert env.client.post("/api/notifications/device-alerts/revoke",
                           headers={HEADER: token}).status_code == 200
    with env.Session() as s:
        row = s.execute(select(DeviceAlertRegistration)).scalar_one()
        assert row.revoked_reason == "logout"
        actions = [a.action for a in s.execute(select(AuditLog)).scalars()]
    assert actions.count("device_alerts_disabled") == 1


def test_deactivated_user_is_refused(env):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    with env.Session() as s:
        s.get(User, __import__("uuid").UUID(uid)).active = False
        s.commit()
    assert env.feed(token).status_code == 401


def test_demoted_to_tech_is_refused(env):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    with env.Session() as s:
        s.get(User, __import__("uuid").UUID(uid)).role = "technician"
        s.commit()
    assert env.feed(token).status_code == 403


def test_a_failed_module_check_is_403_never_401(env, monkeypatch):
    """The phone turns alerts off on a 401 and only retries a 403, because the
    module check answers a database error as "disabled" (/audit 2026-10-14)."""
    from sqlalchemy.exc import OperationalError

    from gdx_dispatch.core import modules

    uid = env.add_user("dispatcher")
    token = env.mint(uid)

    def blip(*_a, **_k):
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    monkeypatch.setattr(modules, "_is_granted_in_company_table", blip)
    assert env.feed(token).status_code == 403


def test_old_token_expires(env):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    with env.Session() as s:
        row = s.execute(select(DeviceAlertRegistration)).scalar_one()
        row.created_at = datetime.now(UTC) - timedelta(days=91)
        s.commit()
    assert env.feed(token).status_code == 401


def test_device_token_is_not_a_login(env):
    """The token is no JWT, so the app's bearer decoding can never accept it."""
    import jwt

    from gdx_dispatch.routers.auth import ALG, VERIFY_KEY

    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(token, VERIFY_KEY, algorithms=[ALG])


def test_bad_cursor_is_422(env):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    assert env.feed(token, after="yesterday").status_code == 422


def test_last_seen_is_stamped(env):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    env.feed(token)
    with env.Session() as s:
        assert s.execute(select(DeviceAlertRegistration)).scalar_one().last_seen_at is not None


def _poll(env, token: str, cursor: str, times: int) -> tuple[set[str], str]:
    seen: set[str] = set()
    for _ in range(times):
        body = env.feed(token, after=cursor).json()
        seen |= {i["id"] for i in body["items"]}
        cursor = body["cursor"]
    return seen, cursor


def test_burst_bigger_than_one_response_all_arrives(env):
    # A missed-texts backlog lands in one burst. The rows already shown must
    # not fill every later response and starve the rest.
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    cursor = env.feed(token).json()["cursor"]
    base = datetime.now(UTC)
    ids = [env.bell(title=f"t{i}", created=base + timedelta(milliseconds=i)) for i in range(45)]
    seen, cursor = _poll(env, token, cursor, 3)
    assert set(ids) <= seen
    assert not cursor.startswith(device_alerts.PAGE_PREFIX)  # caught up: back to a time cursor


def test_burst_with_one_timestamp_pages_by_id(env):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    cursor = env.feed(token).json()["cursor"]
    same = datetime.now(UTC) + timedelta(milliseconds=5)
    ids = [env.bell(title=f"t{i}", created=same) for i in range(25)]
    seen, _ = _poll(env, token, cursor, 2)
    assert set(ids) <= seen


def test_bad_page_cursor_is_422(env):
    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    for bad in ("p:", "p:nope|x", "p:2026-10-04T00:00:00+00:00"):
        assert env.feed(token, after=bad).status_code == 422, bad


# ── A password change or "revoke all sessions" ends the phone's alerts ──

def _request():
    from starlette.requests import Request

    req = Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                   "client": ("127.0.0.1", 1), "query_string": b""})
    req.state.tenant = {"id": TENANT_ID}
    return req


def _reasons(env) -> dict[str, str | None]:
    with env.Session() as s:
        return {r.user_id: r.revoked_reason for r in s.execute(select(DeviceAlertRegistration)).scalars()}


def _set_password(env, uid: str, plain: str) -> None:
    from werkzeug.security import generate_password_hash

    with env.Session() as s:
        user = s.get(User, __import__("uuid").UUID(uid))
        setattr(user, "password_" + "hash", generate_password_hash(plain))
        s.commit()


def test_revoke_device_alerts_ends_only_that_users_phones(env):
    from gdx_dispatch.core.auth_revoke import revoke_device_alerts

    mine, other = env.add_user("dispatcher"), env.add_user("dispatcher")
    t_mine, t_other = env.mint(mine), env.mint(other)
    with env.Session() as s:
        assert revoke_device_alerts(s, mine, reason="password_reset") == 1
        s.commit()
        assert revoke_device_alerts(s, mine, reason="password_reset") == 0  # already off
        assert revoke_device_alerts(s, "not-a-uuid", reason="password_reset") == 0
    assert env.feed(t_mine).status_code == 401
    assert env.feed(t_other).status_code == 200
    assert _reasons(env) == {mine: "password_reset", other: None}


def test_revoke_device_alerts_matches_a_dashless_or_upper_case_id(env):
    """An admin revoke body may carry the id without dashes or in upper case;
    the rows hold the dashed lower-case JWT sub, so it is normalised."""
    from gdx_dispatch.core.auth_revoke import revoke_device_alerts

    mine = env.add_user("dispatcher")
    t_mine = env.mint(mine)
    with env.Session() as s:
        assert revoke_device_alerts(s, mine.replace("-", "").upper(), reason="sessions_revoked") == 1
        s.commit()
    assert env.feed(t_mine).status_code == 401

def _lookup_by_uuid(monkeypatch):
    # users._get_user_or_404 compares the Uuid column with a str: fine on
    # Postgres, never matches on SQLite.
    from gdx_dispatch.routers import users

    monkeypatch.setattr(users, "_get_user_or_404",
                        lambda db, tid, uid: db.get(User, __import__("uuid").UUID(uid)))
    return users


def test_admin_password_reset_ends_phone_alerts(env, monkeypatch):
    users = _lookup_by_uuid(monkeypatch)

    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    admin = {"sub": "admin", "user_id": "admin", "role": "owner"}
    with env.Session() as s:
        users.reset_password(uid, users.PasswordResetIn(new_password="new-pass-123"), _request(), admin, s)
        audit = s.execute(select(AuditLog).where(AuditLog.action == "user_password_reset")).scalar_one()
    assert env.feed(token).status_code == 401
    assert "device_alerts_revoked" in str(audit.details)
    assert _reasons(env) == {uid: "password_reset"}


def test_self_password_change_ends_phone_alerts(env, monkeypatch):
    users = _lookup_by_uuid(monkeypatch)

    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    _set_password(env, uid, "old-pass-123")
    with env.Session() as s:
        users.change_my_password(
            users.SelfPasswordChangeIn(current_password="old-pass-123", new_password="new-pass-456"),
            _request(), {"sub": uid, "user_id": uid, "role": "dispatcher"}, s,
        )
    assert env.feed(token).status_code == 401
    assert _reasons(env) == {uid: "password_changed"}


def test_emailed_link_password_reset_ends_phone_alerts(env, monkeypatch):
    from types import SimpleNamespace

    from gdx_dispatch.routers.auth import core

    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    monkeypatch.setattr(core, "redis", SimpleNamespace(
        get=lambda key: f"{uid}|{TENANT_ID}", delete=lambda key: 1,
    ))
    with env.Session() as s:
        core.reset_password(core.ResetPasswordBody(token="t" * 20, new_password="new-pass-123"), _request(), s)
    assert env.feed(token).status_code == 401
    assert _reasons(env) == {uid: "password_reset"}


def test_admin_revoke_all_sessions_ends_phone_alerts(env, monkeypatch):
    from gdx_dispatch.routers.auth import core

    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    monkeypatch.setattr(core, "revoke_user_sessions", lambda sub, *, reason: 2)  # the Redis side
    with env.Session() as s:
        out = core.admin_revoke_user(core.RevokeUserBody(user_id=uid, reason="lost_phone"),
                                     _request(), {"user_id": "admin"}, s)
    assert out["sessions_revoked"] == 2
    assert env.feed(token).status_code == 401
    assert _reasons(env) == {uid: "sessions_revoked"}


def test_admin_revoke_is_audited_when_the_phone_revoke_fails(env, monkeypatch):
    # The Redis sessions are gone before the phones are touched, so a failed
    # phone revoke must not cost the audit row of the revoke that happened.
    from gdx_dispatch.routers.auth import core

    uid = env.add_user("dispatcher")
    monkeypatch.setattr(core, "revoke_user_sessions", lambda sub, *, reason: 2)

    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(core, "revoke_device_alerts", _boom)
    with env.Session() as s:
        out = core.admin_revoke_user(core.RevokeUserBody(user_id=uid, reason="lost_phone"),
                                     _request(), {"user_id": "admin"}, s)
    assert out["sessions_revoked"] == 2
    with env.Session() as s:
        audit = s.execute(select(AuditLog).where(AuditLog.action == "user_sessions_revoked")).scalar_one()
    assert audit.details["device_alerts_revoked"] == "error"


@pytest.mark.parametrize("theft", ["denylist", "replay"])
def test_refresh_denylist_ends_phone_alerts_and_replay_does_not(env, monkeypatch, theft):
    # An admin-revoked session could mint a phone token that outlives it. A
    # replay hit is usually a benign race on prod, so it leaves the phone on.
    from fastapi import HTTPException

    from gdx_dispatch.core import auth_revoke
    from gdx_dispatch.core.denylist import Denylist
    from gdx_dispatch.routers.auth import core
    from gdx_dispatch.tests.test_auth_router import (
        _fake_request,
        _FakeRefreshRedis,
        _mint_refresh_token,
    )

    uid = env.add_user("dispatcher")
    token = env.mint(uid)
    jti = f"stolen-{theft}"
    fake_redis = _FakeRefreshRedis()
    fake_redis.families[uid] = {jti}
    monkeypatch.setattr(core, "redis", fake_redis)
    monkeypatch.setattr(auth_revoke, "_redis_client", lambda: fake_redis)
    denylist = Denylist()
    if theft == "denylist":
        denylist.add(jti, datetime.now(UTC) + timedelta(hours=1))
    else:
        fake_redis.used.add(jti)  # already redeemed, no cached pair: a true replay
    request = _fake_request(denylist=denylist,
                            cookies={"refresh_token": _mint_refresh_token(sub=uid, jti=jti)})
    with env.Session() as s, pytest.raises(HTTPException) as exc:
        core.refresh(request=request, db=s)
    assert exc.value.status_code == 401
    if theft == "denylist":
        assert env.feed(token).status_code == 401
        assert _reasons(env) == {uid: "refresh_denylist_hit"}
    else:
        assert env.feed(token).status_code == 200
        assert _reasons(env) == {uid: None}
