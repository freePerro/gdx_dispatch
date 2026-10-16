"""GDXA-1200 — one role vocabulary for every user-assignment path.

The admin invite (routers/admin_ops.py) kept its own literal tuple without
``sales``, and routers/users.py kept another without ``viewer``. The Settings
invite dialog and UsersView each offered a role their endpoint 422ed. Both now
read core/roles.ASSIGNABLE_ROLES.

The endpoint tests run the real routers against an in-memory SQLite database
built from the ORM (fixtures shared with the neighbouring suites); the stored
role and the audit row are read back from that database.
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from gdx_dispatch.core.permissions import BUILTIN_ROLES
from gdx_dispatch.core.roles import ASSIGNABLE_ROLES, ROLE_ALIASES
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.tests.test_silent_success_sweep import (  # noqa: F401
    _audit_rows,
    _RecordingRedis,
    ops_client,
)
from gdx_dispatch.tests.test_users_uuid_lookup_723 import _user, env  # noqa: F401

# ── the one constant ────────────────────────────────────────────────────────


def test_every_assignable_role_is_a_builtin_rbac_role():
    # A role outside the catalog would be stored on users.role with no
    # permission set behind it.
    assert set(ASSIGNABLE_ROLES) <= set(BUILTIN_ROLES)
    assert "sales" in ASSIGNABLE_ROLES and "viewer" in ASSIGNABLE_ROLES


def test_invite_and_users_router_read_the_shared_constant():
    from gdx_dispatch.routers import admin_ops, users

    assert users.VALID_ROLES is ASSIGNABLE_ROLES
    assert frozenset(ASSIGNABLE_ROLES) | {"user"} == admin_ops._INVITE_ROLES
    # The input pattern accepts exactly the assignable roles plus their short
    # legacy spellings, nothing more.
    pattern = re.compile(users._ROLE_INPUT_PATTERN)
    expected = set(ASSIGNABLE_ROLES) | {
        a for a, c in ROLE_ALIASES.items() if c in ASSIGNABLE_ROLES
    }
    for spelling in expected:
        assert pattern.fullmatch(spelling), spelling
    for outsider in ("accounting", "manager", "user", "superadmin", "sales|admin", ""):
        assert not pattern.fullmatch(outsider), outsider


# ── POST /api/admin/users/invite ───────────────────────────────────────────


@pytest.mark.parametrize("sent", ["sales", "Sales", " SALES "])
def test_invite_accepts_sales_and_stores_it(ops_client, monkeypatch, sent):  # noqa: F811
    client, admin_ops_mod = ops_client
    monkeypatch.setattr(admin_ops_mod, "_auth_redis", _RecordingRedis())

    r = client.post(
        "/api/admin/users/invite",
        json={"email": "new.sales@example.com", "role": sent},
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "sales"
    with client.SessionLocal() as db:
        row = db.execute(
            select(User).where(User.email == "new.sales@example.com")
        ).scalar_one()
        assert row.role == "sales"
    # Invariant #1: the audit row records the role that was granted.
    rows = _audit_rows(client.SessionLocal, "user_invited")
    assert [r.details["role"] for r in rows] == ["sales"]


# "user" is kept because the GDXA-1200 ruling said nothing that works today may
# break; it is not a BUILTIN_ROLES key, so such a user resolves no permissions
# (recorded as found-not-filed, not endorsed here).
@pytest.mark.parametrize(("sent", "stored"), [
    ("viewer", "viewer"), ("user", "user"), ("dispatcher", "dispatcher"),
    ("tech", "technician"),
])
def test_invite_keeps_every_role_it_accepted_before(ops_client, monkeypatch, sent, stored):  # noqa: F811
    client, admin_ops_mod = ops_client
    monkeypatch.setattr(admin_ops_mod, "_auth_redis", _RecordingRedis())

    r = client.post(
        "/api/admin/users/invite",
        json={"email": f"keep.{sent}@example.com", "role": sent},
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == stored


@pytest.mark.parametrize("sent", ["accounting", "manager", "superadmin", "root"])
def test_invite_still_refuses_a_role_outside_the_vocabulary(ops_client, monkeypatch, sent):  # noqa: F811
    client, admin_ops_mod = ops_client
    monkeypatch.setattr(admin_ops_mod, "_auth_redis", _RecordingRedis())

    r = client.post(
        "/api/admin/users/invite",
        json={"email": "bad.role@example.com", "role": sent},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "invalid role"
    with client.SessionLocal() as db:
        assert db.execute(
            select(User).where(User.email == "bad.role@example.com")
        ).first() is None
    assert _audit_rows(client.SessionLocal, "user_invited") == []


def test_an_admin_still_cannot_invite_an_owner(ops_client, monkeypatch):  # noqa: F811
    # The vocabulary grew; the owner-only grant of the admin tier did not move.
    client, admin_ops_mod = ops_client
    monkeypatch.setattr(admin_ops_mod, "_auth_redis", _RecordingRedis())

    r = client.post(
        "/api/admin/users/invite",
        json={"email": "would.be.owner@example.com", "role": "owner"},
    )
    assert r.status_code == 403, r.text


# ── /api/users (UsersView) ──────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["sales", "viewer"])
def test_users_router_changes_a_role_to_sales_or_viewer(env, role):  # noqa: F811
    tid = env["target_id"]
    r = env["client"].post(f"/api/users/{tid}/role", json={"role": role})
    assert r.status_code == 200, r.text
    assert _user(env["db"], tid).role == role


def test_users_router_creates_a_viewer(env):  # noqa: F811
    r = env["client"].post("/api/users", json={
        "email": "new.viewer@example.com", "name": "Val Viewer",
        "password": "Viewer-Passw0rd-1200", "role": "viewer",
    })
    assert r.status_code == 201, r.text
    with env["db"]() as db:
        row = db.execute(
            select(User).where(User.email == "new.viewer@example.com")
        ).scalar_one()
        assert row.role == "viewer"


@pytest.mark.parametrize("role", ["accounting", "user", "manager"])
def test_users_router_still_refuses_a_role_outside_the_vocabulary(env, role):  # noqa: F811
    tid = env["target_id"]
    r = env["client"].post(f"/api/users/{tid}/role", json={"role": role})
    assert r.status_code == 422, r.text
    assert _user(env["db"], tid).role == "technician"
