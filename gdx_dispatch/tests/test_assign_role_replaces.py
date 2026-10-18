"""POST /api/role-permissions/users/{id}/roles replaces the user's assignment (GDXA-1464).

The resolver (core/modules.py::_load_user_permissions) reads one assignment
and prefers the row whose role name matches users.role. assign_role used to
add a row beside the old one, so a custom role assigned over a builtin one
never took effect while the UI toasted success. These run the real resolver
after the real endpoint.
"""
from __future__ import annotations

import json
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.modules import _load_user_permissions
from gdx_dispatch.models.tenant_models import TenantRole, User, UserRoleAssignment
from gdx_dispatch.tests.test_role_permissions import _act_as_admin, _make_client

TENANT = "tenant-test"


@pytest.fixture()
def client():
    # An owner acts: several cases grant or touch the admin/owner tier, which
    # assert_can_assign_role reserves to an owner (GDXA-1469).
    tc = _make_client(tenant_id=TENANT, actor_role="owner", actor_id="owner-1")
    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions", return_value=0):
        yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def _session(client):
    return sessionmaker(bind=client._engine)()


def _seed_user(client, role: str) -> str:
    uid = uuid4()
    db = _session(client)
    db.add(User(id=uid, company_id=TENANT, email=f"{uid.hex}@example.com", username=uid.hex, role=role, active=True))
    db.commit()
    db.close()
    return str(uid)


def _role_id(client, name: str) -> str:
    roles = client.get("/api/role-permissions/roles").json()
    return next(r["id"] for r in roles if r["name"] == name)


def _custom_role(client, name: str, perms: list[str]) -> str:
    r = client.post("/api/role-permissions/roles", json={"name": name, "permissions": perms})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _assign(client, user_id: str, role_id: str):
    return client.post(f"/api/role-permissions/users/{user_id}/roles", json={"role_id": role_id})


def _perms(client, user_id: str, jwt_role: str) -> set[str]:
    request = Request({"type": "http", "headers": [], "state": {}})
    request.state.tenant = {"id": TENANT}
    db = _session(client)
    try:
        return _load_user_permissions(db, request, {"user_id": user_id, "role": jwt_role})
    finally:
        db.close()


def _assigned_names(client, user_id: str) -> list[str]:
    db = _session(client)
    try:
        return list(db.execute(
            select(TenantRole.name)
            .join(UserRoleAssignment, UserRoleAssignment.role_id == TenantRole.id)
            .where(UserRoleAssignment.user_id == user_id)
        ).scalars())
    finally:
        db.close()


def _users_role(client, user_id: str) -> str:
    db = _session(client)
    try:
        return db.execute(select(User.role).where(User.id == UUID(user_id))).scalar_one()
    finally:
        db.close()


def test_custom_role_replaces_builtin_and_takes_effect(client):
    user_id = _seed_user(client, "technician")
    assert _assign(client, user_id, _role_id(client, "technician")).status_code == 201
    lead = _custom_role(client, "Lead", ["jobs.read_all", "customers.read_all"])

    r = _assign(client, user_id, lead)

    assert r.status_code == 201, r.text
    assert _assigned_names(client, user_id) == ["Lead"]
    assert _perms(client, user_id, "technician") == {"jobs.read_all", "customers.read_all"}
    # users.role stays on the builtin tier: it only ever holds builtin names.
    assert _users_role(client, user_id) == "technician"
    db = _session(client)
    audit = db.execute(select(AuditLog).where(AuditLog.action == "user_role_assigned")).scalars().all()[-1]
    db.close()
    details = audit.details if isinstance(audit.details, dict) else json.loads(audit.details)
    assert [a["role_name"] for a in details["removed_assignments"]] == ["technician"]
    assert details["role_name"] == "Lead"


@pytest.mark.parametrize("tier", ["admin", "owner"])
def test_custom_role_on_full_access_user_is_refused(client, tier):
    """users.role drives the JWT, require_role and the last-owner count, so a
    custom role under an admin/owner users.role would only half-narrow them."""
    _seed_user(client, "owner")  # keep a second owner so the last-owner guard is not what answers
    user_id = _seed_user(client, tier)
    assert _assign(client, user_id, _role_id(client, tier)).status_code == 201
    lead = _custom_role(client, "Lead", ["jobs.read_all"])

    r = _assign(client, user_id, lead)

    assert r.status_code == 400
    assert "full-access" in r.json()["detail"]
    assert _assigned_names(client, user_id) == [tier]


def test_builtin_role_replaces_custom_and_syncs_users_role(client):
    user_id = _seed_user(client, "technician")
    dispatcher = _role_id(client, "dispatcher")  # the list seeds builtins only into an empty table
    lead = _custom_role(client, "Lead", ["jobs.read_all", "customers.read_all"])
    assert _assign(client, user_id, lead).status_code == 201

    r = _assign(client, user_id, dispatcher)

    assert r.status_code == 201, r.text
    assert _assigned_names(client, user_id) == ["dispatcher"]
    assert _users_role(client, user_id) == "dispatcher"
    db = _session(client)
    snapshot = db.execute(select(TenantRole.permissions).where(TenantRole.name == "dispatcher")).scalar_one()
    db.close()
    assert _perms(client, user_id, "technician") == set(json.loads(snapshot))


def test_reassigning_the_same_role_clears_a_stale_sibling(client):
    """Users carrying two rows from before the fix get repaired by
    re-saving their intended role, instead of the early idempotent return."""
    user_id = _seed_user(client, "technician")
    tech = _role_id(client, "technician")
    lead = _custom_role(client, "Lead", ["jobs.read_all"])
    db = _session(client)
    for rid in (tech, lead):
        db.add(UserRoleAssignment(company_id=TENANT, user_id=user_id, role_id=UUID(rid)))
    db.commit()
    db.close()

    assert _assign(client, user_id, lead).status_code == 201

    assert _assigned_names(client, user_id) == ["Lead"]
    assert _perms(client, user_id, "technician") == {"jobs.read_all"}


def test_two_owners_cannot_both_take_a_custom_role(client):
    """The audit's bypass: each owner still counted the other as an owner
    while both lost owner permissions."""
    first = _seed_user(client, "owner")
    second = _seed_user(client, "owner")
    lead = _custom_role(client, "Lead", ["jobs.read_all"])

    assert _assign(client, first, lead).status_code == 400
    assert _assign(client, second, lead).status_code == 400
    assert _assigned_names(client, first) == [] and _assigned_names(client, second) == []


def test_demoting_the_last_owner_to_a_builtin_role_is_refused(client):
    user_id = _seed_user(client, "owner")
    assert _assign(client, user_id, _role_id(client, "owner")).status_code == 201

    r = _assign(client, user_id, _role_id(client, "admin"))

    assert r.status_code == 400
    assert "last owner" in r.json()["detail"]
    assert _assigned_names(client, user_id) == ["owner"]
    assert _users_role(client, user_id) == "owner"


def test_demoting_one_of_two_owners_is_allowed(client):
    _seed_user(client, "owner")
    user_id = _seed_user(client, "owner")

    assert _assign(client, user_id, _role_id(client, "admin")).status_code == 201
    assert _assigned_names(client, user_id) == ["admin"]
    assert _users_role(client, user_id) == "admin"


def test_admin_replaces_a_role_on_a_non_admin_user(client):
    """The everyday path: an admin, not an owner, swaps a technician's role."""
    _act_as_admin(client)
    user_id = _seed_user(client, "technician")
    assert _assign(client, user_id, _role_id(client, "technician")).status_code == 201

    r = _assign(client, user_id, _role_id(client, "dispatcher"))

    assert r.status_code == 201, r.text
    assert _assigned_names(client, user_id) == ["dispatcher"]


def test_admin_cannot_strip_an_admin_row_by_replacing_it(client):
    """users.role and the rows can disagree; the replace deletes the admin row,
    which unassign_role refuses an admin, so assign_role must refuse it too."""
    user_id = _seed_user(client, "dispatcher")
    db = _session(client)
    db.add(UserRoleAssignment(company_id=TENANT, user_id=user_id, role_id=UUID(_role_id(client, "admin"))))
    db.commit()
    db.close()
    _act_as_admin(client)

    r = _assign(client, user_id, _role_id(client, "dispatcher"))

    assert r.status_code == 403, r.text
    assert _assigned_names(client, user_id) == ["admin"]
