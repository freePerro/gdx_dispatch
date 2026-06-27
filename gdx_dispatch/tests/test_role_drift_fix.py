"""Role ↔ assignment drift fixes (2026-06-26).

An owner whose users.role was elevated out-of-band but whose RBAC assignment
stayed `admin` was silently resolved to admin perms. Two guards:
  - the resolver PREFERS the assignment whose name matches users.role, so a
    stale assignment can't shadow the intended role;
  - assign_role keeps users.role in sync with a builtin-role assignment, so the
    sanctioned RBAC UI can't create the drift in the first place.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.modules import _load_user_permissions
from gdx_dispatch.core.permissions import WILDCARD
from gdx_dispatch.models.tenant_models import TenantRole, User, UserRoleAssignment

TENANT = "tenant-drift"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _role(db, name, perms):
    r = TenantRole(id=uuid4(), company_id=TENANT, name=name, permissions=json.dumps(perms), is_system=True)
    db.add(r)
    db.flush()
    return r


def _assign(db, user_id, role):
    db.add(UserRoleAssignment(id=uuid4(), company_id=TENANT, user_id=user_id, role_id=role.id))


def _req():
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": TENANT}),
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def test_resolver_prefers_assignment_matching_users_role(db):
    # users.role=owner, but a STALE admin assignment also exists alongside the
    # owner one. The resolver must prefer the owner assignment (the match), not
    # pick nondeterministically.
    uid = str(uuid4())
    admin = _role(db, "admin", ["jobs.read_all"])  # no wildcard
    owner = _role(db, "owner", [WILDCARD])
    _assign(db, uid, admin)
    _assign(db, uid, owner)
    db.commit()

    perms = _load_user_permissions(db, _req(), {"user_id": uid, "role": "owner"})
    assert perms == {WILDCARD}  # owner wins, not the stale admin assignment


def test_resolver_returns_assignment_perms_when_only_mismatch_exists(db):
    # users.role=owner but ONLY an admin assignment exists — deliberate behavior
    # is to resolve to the assignment (no owner snapshot to use); the fix logs a
    # drift warning but must not crash and must still return a usable set.
    uid = str(uuid4())
    admin = _role(db, "admin", ["jobs.read_all"])
    _assign(db, uid, admin)
    db.commit()

    perms = _load_user_permissions(db, _req(), {"user_id": uid, "role": "owner"})
    assert perms == {"jobs.read_all"}  # admin snapshot; drift logged, not silent


def test_assign_role_syncs_users_role_for_builtin(db):
    from gdx_dispatch.routers.role_permissions import AssignRoleIn, assign_role

    uid = uuid4()
    db.add(User(id=uid, email="t@e.co", username="t", role="technician", company_id=TENANT))
    owner = _role(db, "owner", [WILDCARD])
    db.commit()

    actor = {"user_id": str(uuid4()), "role": "owner"}
    assign_role(str(uid), AssignRoleIn(role_id=str(owner.id)), _req(), actor, db)

    refreshed = db.get(User, uid)
    assert refreshed.role == "owner"  # users.role synced to the assigned builtin role
