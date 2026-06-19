"""Admin-initiated user lockout — backend contract tests.

Covers `POST /api/users/{id}/lockout`, `POST /api/users/{id}/unlock`, and
`GET /api/users/{id}/lockout-info` added to gdx_dispatch/routers/users.py.

The login-block side of the contract (active=False rejected at /auth/login)
is verified upstream in the existing auth tests (Michael Tallman regression,
gdx_dispatch/routers/auth/core.py:240-249). These tests pin the lockout endpoints'
own behavior: state transition, owner/self guard, audit payload shape,
session revoke call, and the `require_permission("users.write")` HTTP gate.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import TenantRole, User, UserRoleAssignment
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.users import (
    LockoutIn,
    get_lockout_info,
    lockout_user,
    unlock_user,
)


TENANT_ID = "tenant-lock"
ADMIN_ID = "admin-1"
TARGET_ID = "target-1"
OWNER_ID = "owner-1"


def _fake_request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/users",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 0),
    }
    req = Request(scope)
    req.state.tenant = {"id": TENANT_ID}
    return req


def _admin() -> dict:
    return {"sub": ADMIN_ID, "tenant_id": TENANT_ID, "role": "admin"}


def _dispatcher_with_users_write() -> dict:
    """A dispatcher that a tenant has granted users.write to. The
    require_permission gate passes — only the role check in the handler
    stops them from locking out anyone."""
    return {"sub": "disp-1", "tenant_id": TENANT_ID, "role": "dispatch"}


def _user_row(user_id: str, role: str = "tech", active: bool = True) -> User:
    u = MagicMock(spec=User)
    u.id = user_id
    u.role = role
    u.active = active
    u.schedulable = False
    u.email = f"{user_id}@example.com"
    u.full_name = user_id
    u.name = user_id
    u.phone = ""
    u.route_start_address = ""
    u.created_at = None
    u.last_login_at = None
    u.updated_at = None
    return u


def _db_returning(u: User | None) -> MagicMock:
    exec_call = MagicMock()
    exec_call.scalar_one_or_none.return_value = u
    db = MagicMock(spec=Session)
    db.execute.return_value = exec_call
    return db


# -- role-relative authorization (audit critique #1 — privilege escalation) -


def test_lockout_blocked_for_dispatcher_with_users_write() -> None:
    """Foundational regression: require_permission("users.write") is NOT
    equivalent to "must be admin or above." Tenants can grant users.write
    to non-platform-locked roles. Without the in-handler role check, a
    dispatcher with users.write could lock out an admin — privilege
    escalation via permission-shape, not via the permission gate.
    """
    db = _db_returning(_user_row("admin-2", role="admin"))
    payload = LockoutIn(reason="terminated", notes=None)
    with pytest.raises(HTTPException) as exc:
        lockout_user("admin-2", payload, _fake_request(), _dispatcher_with_users_write(), db)
    assert exc.value.status_code == 403
    assert "admin" in exc.value.detail.lower() or "owner" in exc.value.detail.lower()


def test_unlock_blocked_for_dispatcher_with_users_write() -> None:
    """Mirror: unlock has the same elevation requirement as lockout."""
    db = _db_returning(_user_row(TARGET_ID, active=False))
    with pytest.raises(HTTPException) as exc:
        unlock_user(TARGET_ID, _fake_request(), _dispatcher_with_users_write(), db)
    assert exc.value.status_code == 403


def test_lockout_allowed_for_owner_role() -> None:
    """Owners are valid actors — they're just non-targets."""
    u = _user_row(TARGET_ID, role="tech", active=True)
    db = _db_returning(u)
    owner_actor = {"sub": "owner-7", "tenant_id": TENANT_ID, "role": "owner"}
    payload = LockoutIn(reason="terminated", notes=None)
    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions", return_value=1), \
         patch("gdx_dispatch.routers.users.log_audit_event_sync"):
        result = lockout_user(TARGET_ID, payload, _fake_request(), owner_actor, db)
    assert result["active"] is False


# -- lockout guards ---------------------------------------------------------


def test_lockout_blocks_owner_role() -> None:
    """Owners cannot be locked out — even by another owner. Avoids the
    last-owner edge case entirely and keeps the surface uniform."""
    db = _db_returning(_user_row(OWNER_ID, role="owner"))
    payload = LockoutIn(reason="security_incident", notes=None)
    with pytest.raises(HTTPException) as exc:
        lockout_user(OWNER_ID, payload, _fake_request(), _admin(), db)
    assert exc.value.status_code == 400
    assert "owner" in exc.value.detail.lower()


def test_lockout_blocks_self() -> None:
    """An admin cannot lock themselves out."""
    db = _db_returning(_user_row(ADMIN_ID, role="admin"))
    payload = LockoutIn(reason="other", notes=None)
    with pytest.raises(HTTPException) as exc:
        lockout_user(ADMIN_ID, payload, _fake_request(), _admin(), db)
    assert exc.value.status_code == 400
    assert "yourself" in exc.value.detail.lower()


def test_lockout_blocks_already_locked() -> None:
    """Idempotency surface: re-locking a locked user returns 409, not a
    silent re-revoke."""
    db = _db_returning(_user_row(TARGET_ID, active=False))
    payload = LockoutIn(reason="terminated", notes=None)
    with pytest.raises(HTTPException) as exc:
        lockout_user(TARGET_ID, payload, _fake_request(), _admin(), db)
    assert exc.value.status_code == 409


def test_lockout_rejects_bad_reason() -> None:
    """Reason is a Literal — pydantic raises before the handler runs."""
    with pytest.raises(Exception):
        LockoutIn(reason="not_a_real_reason")  # type: ignore[arg-type]


# -- lockout happy path -----------------------------------------------------


def test_lockout_sets_active_false_and_revokes() -> None:
    """Happy path: state flips to active=False (the login gate's contract),
    revoke_user_sessions is called with reason="user_locked", and the audit
    row carries the reason + notes + revoked count."""
    u = _user_row(TARGET_ID, role="tech", active=True)
    db = _db_returning(u)
    payload = LockoutIn(reason="security_incident", notes="laptop missing")
    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions", return_value=2) as mock_revoke, \
         patch("gdx_dispatch.routers.users.log_audit_event_sync") as mock_audit:
        result = lockout_user(TARGET_ID, payload, _fake_request(), _admin(), db)
    assert u.active is False, "must flip the column the login gate reads"
    mock_revoke.assert_called_once()
    assert mock_revoke.call_args.kwargs.get("reason") == "user_locked"
    mock_audit.assert_called_once()
    audit_kwargs = mock_audit.call_args.kwargs
    assert audit_kwargs["action"] == "user_locked"
    assert audit_kwargs["entity_id"] == TARGET_ID
    details = audit_kwargs["details"]
    assert details["reason"] == "security_incident"
    assert details["notes"] == "laptop missing"
    assert details["sessions_revoked"] == 2
    assert details["locked_by"] == ADMIN_ID
    assert result["active"] is False


def test_lockout_normalizes_blank_notes_to_none() -> None:
    """Whitespace-only notes shouldn't pollute the audit details — store None."""
    u = _user_row(TARGET_ID)
    db = _db_returning(u)
    payload = LockoutIn(reason="terminated", notes="   ")
    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions", return_value=0), \
         patch("gdx_dispatch.routers.users.log_audit_event_sync") as mock_audit:
        lockout_user(TARGET_ID, payload, _fake_request(), _admin(), db)
    assert mock_audit.call_args.kwargs["details"]["notes"] is None


def test_lockout_survives_revoke_failure_and_surfaces_partial() -> None:
    """Redis flaking out cannot block the lockout — the row flip is the
    primary action and the access-token gate (_db_verify_user) reads
    `users.active` on every request, so the user is denied on next call
    regardless of Redis state. The audit row surfaces the partial failure
    as `sessions_revoked: "error"` so ops can tell "fully revoked" from
    "DB-only revoke pending Redis recovery." Asserting on the literal
    "error" sentinel — not "no exception raised" — pins the right
    invariant per the 2026-05-20 audit critique."""
    u = _user_row(TARGET_ID)
    db = _db_returning(u)
    payload = LockoutIn(reason="other", notes=None)
    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions", side_effect=RuntimeError("redis down")), \
         patch("gdx_dispatch.routers.users.log_audit_event_sync") as mock_audit:
        result = lockout_user(TARGET_ID, payload, _fake_request(), _admin(), db)
    assert u.active is False, "DB flip is the primary action — must stand"
    assert result["active"] is False
    assert mock_audit.call_args.kwargs["details"]["sessions_revoked"] == "error", \
        "partial failure must be visible in the audit trail, not silently zeroed"


# -- unlock -----------------------------------------------------------------


def test_unlock_flips_active_true() -> None:
    u = _user_row(TARGET_ID, active=False)
    db = _db_returning(u)
    with patch("gdx_dispatch.routers.users.log_audit_event_sync") as mock_audit:
        result = unlock_user(TARGET_ID, _fake_request(), _admin(), db)
    assert u.active is True
    assert result["active"] is True
    assert mock_audit.call_args.kwargs["action"] == "user_unlocked"
    assert mock_audit.call_args.kwargs["details"]["unlocked_by"] == ADMIN_ID


def test_unlock_rejects_already_active() -> None:
    db = _db_returning(_user_row(TARGET_ID, active=True))
    with pytest.raises(HTTPException) as exc:
        unlock_user(TARGET_ID, _fake_request(), _admin(), db)
    assert exc.value.status_code == 409


# -- lockout-info -----------------------------------------------------------


def test_lockout_info_returns_latest_audit_row() -> None:
    """The Locked badge's click-to-reveal popover calls this — it should
    return the most recent `user_locked` audit row's reason, notes, actor,
    and timestamp."""
    from datetime import datetime, timezone

    u = _user_row(TARGET_ID, active=False)
    audit_row = MagicMock()
    audit_row.details = {
        "reason": "policy_violation",
        "notes": "shared password",
        "locked_by": ADMIN_ID,
    }
    audit_row.created_at = datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc)

    # First select() returns user (existence check); second returns audit row
    user_call = MagicMock()
    user_call.scalar_one_or_none.return_value = u
    audit_call = MagicMock()
    audit_call.scalar_one_or_none.return_value = audit_row
    db = MagicMock(spec=Session)
    db.execute.side_effect = [user_call, audit_call]

    result = get_lockout_info(TARGET_ID, _fake_request(), _admin(), db)
    assert result["reason"] == "policy_violation"
    assert result["notes"] == "shared password"
    assert result["locked_by"] == ADMIN_ID
    assert result["locked_at"].startswith("2026-05-20T14:00")


def test_lockout_info_404_when_no_history() -> None:
    u = _user_row(TARGET_ID)
    user_call = MagicMock()
    user_call.scalar_one_or_none.return_value = u
    audit_call = MagicMock()
    audit_call.scalar_one_or_none.return_value = None
    db = MagicMock(spec=Session)
    db.execute.side_effect = [user_call, audit_call]
    with pytest.raises(HTTPException) as exc:
        get_lockout_info(TARGET_ID, _fake_request(), _admin(), db)
    assert exc.value.status_code == 404


# -- HTTP-level permission gate --------------------------------------------
# Per feedback_require_min_role_is_broken: always pair a route gate with an
# HTTP-level test, not just a service-layer unit test. The gate is
# `require_permission("users.write")`; this test mounts the real users
# router and asserts a viewer (no users.write) 403s on /lockout.


def _build_http_app():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = SessionLocal()
    setup.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT
        )
    """))
    # Grant the `jobs` module so require_module passes (the users router
    # depends on it at the router level).
    setup.execute(text("""
        INSERT INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)
        VALUES (:id, :tid, 'jobs', '2026-01-01T00:00:00', '2026-01-01T00:00:00')
    """), {"id": str(uuid4()), "tid": TENANT_ID})
    setup.commit()
    setup.close()

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    # Import lazily so module-level side effects don't fire at collection
    from gdx_dispatch.routers.users import router as users_router

    state = {"role": "viewer"}

    app = FastAPI()

    @app.middleware("http")
    async def inject_state(request, call_next):
        request.state.tenant = {"id": TENANT_ID}
        request.state.current_user = {
            "user_id": ADMIN_ID, "sub": ADMIN_ID,
            "role": state["role"], "tenant_id": TENANT_ID,
        }
        return await call_next(request)

    app.include_router(users_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": ADMIN_ID, "sub": ADMIN_ID,
        "role": state["role"], "tenant_id": TENANT_ID,
    }
    tc = TestClient(app, raise_server_exceptions=False)
    tc._state = state  # type: ignore[attr-defined]
    tc._Session = SessionLocal  # type: ignore[attr-defined]
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


def _seed_role(tc: TestClient, perms: list[str]) -> None:
    Session = tc._Session  # type: ignore[attr-defined]
    with Session() as db:
        db.query(UserRoleAssignment).delete()
        db.query(TenantRole).delete()
        role = TenantRole(
            id=uuid4(), company_id=TENANT_ID, name="custom",
            permissions=json.dumps(perms), is_system=False,
        )
        db.add(role)
        db.flush()
        db.add(UserRoleAssignment(
            id=uuid4(), company_id=TENANT_ID, user_id=ADMIN_ID, role_id=role.id,
        ))
        db.commit()


@pytest.fixture()
def http_tc():
    tc = _build_http_app()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def test_lockout_route_403_without_users_write(http_tc) -> None:
    """A viewer (no users.write permission) must 403 at the route layer,
    not slip past to the handler. Mirrors the
    feedback_require_min_role_is_broken HTTP-level test discipline."""
    _seed_role(http_tc, perms=["jobs.read_all"])  # viewer-shape, no users.write
    resp = http_tc.post(
        f"/api/users/{TARGET_ID}/lockout",
        json={"reason": "terminated", "notes": None},
    )
    assert resp.status_code == 403, resp.text


def test_unlock_route_403_without_users_write(http_tc) -> None:
    _seed_role(http_tc, perms=["jobs.read_all"])
    resp = http_tc.post(f"/api/users/{TARGET_ID}/unlock")
    assert resp.status_code == 403, resp.text
