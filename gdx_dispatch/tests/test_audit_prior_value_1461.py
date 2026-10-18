"""GDXA-1461 — an audited mutation that overwrites or discards state records
the prior value in its audit row.

* change_role rewrote user_role_assignments and kept no trace of a custom
  role it dropped; a same-role POST (the Users role dropdown) dropped it too.
* update_user / update_me listed every field the dialog sent, changed or not,
  with no prior values.
* admin_ops.update_role_permissions and deactivate_user, and
  role_permissions.assign_role / update_role, overwrote a value the audit row
  did not keep.

Real handlers against an ORM-built SQLite schema, no mocked session; every
test reads the audit row back. Session revocation is stubbed (it needs Redis
and is not what is under test). Handlers get the user id as a `uuid.UUID`:
SQLite's `Uuid` bind cannot take the dashed string the HTTP path carries.
"""
from __future__ import annotations

import json
import uuid
from datetime import time
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.models.tenant_models import RolePermission, TenantRole, User, UserRoleAssignment

TENANT = "tenant-1461"
OWNER = {"sub": "owner-1461", "user_id": "owner-1461", "role": "owner", "tenant_id": TENANT}


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/x", "headers": [], "query_string": b"", "client": ("127.0.0.1", 0)})
    req.state.tenant = {"id": TENANT}
    return req


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions", return_value=0):
        yield session
    session.close()
    engine.dispose()


def _audits(db, action: str) -> list[AuditLog]:
    db.expire_all()
    return list(db.execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at)).scalars())


def _role(db, name: str, *, system: bool = True) -> TenantRole:
    r = TenantRole(id=uuid.uuid4(), company_id=TENANT, name=name, permissions=json.dumps(["jobs.read"]), is_system=system)
    db.add(r)
    db.commit()
    return r


def _user(db, role: str = "technician", **kw) -> User:
    u = User(id=uuid.uuid4(), company_id=TENANT, email=f"{uuid.uuid4().hex[:8]}@example.com", full_name="Tess Tech", role=role, **kw)
    db.add(u)
    db.commit()
    return u


def _assign(db, u: User, role: TenantRole) -> None:
    db.add(UserRoleAssignment(company_id=TENANT, user_id=str(u.id), role_id=role.id))
    db.commit()


def _assigned_role_ids(db, u: User) -> set[uuid.UUID]:
    db.expire_all()
    return set(db.execute(select(UserRoleAssignment.role_id).where(UserRoleAssignment.user_id == str(u.id))).scalars())


# ── change_role ─────────────────────────────────────────────────────────────


def test_a_role_change_names_the_custom_assignment_it_drops(db):
    from gdx_dispatch.routers.users import RoleChangeIn, change_role

    tech, dispatcher = _role(db, "technician"), _role(db, "dispatcher")
    lead = _role(db, "Lead Tech", system=False)
    u = _user(db)
    _assign(db, u, tech)
    _assign(db, u, lead)

    change_role(u.id, RoleChangeIn(role="dispatch"), _request(), OWNER, db)

    assert _assigned_role_ids(db, u) == {dispatcher.id}
    (row,) = _audits(db, "user_role_changed")
    assert row.details["old_role"] == "technician"
    assert row.details["new_role"] == "dispatcher"
    # The builtin row being replaced is listed too; the custom one is the point.
    assert sorted(row.details["dropped_assignments"], key=lambda d: d["role_name"]) == [
        {"role_id": str(lead.id), "role_name": "Lead Tech"},
        {"role_id": str(tech.id), "role_name": "technician"},
    ]


def test_a_same_role_post_leaves_the_custom_assignment_and_writes_nothing(db):
    from gdx_dispatch.routers.users import RoleChangeIn, change_role

    tech = _role(db, "technician")
    lead = _role(db, "Lead Tech", system=False)
    u = _user(db)
    _assign(db, u, tech)
    _assign(db, u, lead)

    # The dropdown posts the short form; it normalizes to the stored role.
    change_role(u.id, RoleChangeIn(role="tech"), _request(), OWNER, db)

    assert _assigned_role_ids(db, u) == {tech.id, lead.id}
    assert _audits(db, "user_role_changed") == []


def test_a_same_role_post_still_repairs_a_stale_builtin_assignment(db):
    """users.role says technician but the only assignment is admin's, which
    the resolver trusts: re-posting technician must rewrite it, keep the
    custom role, and leave a trace."""
    from gdx_dispatch.routers.users import RoleChangeIn, change_role

    tech, admin = _role(db, "technician"), _role(db, "admin")
    lead = _role(db, "Lead Tech", system=False)
    u = _user(db)
    _assign(db, u, admin)
    _assign(db, u, lead)

    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions", return_value=2) as revoke:
        change_role(u.id, RoleChangeIn(role="technician"), _request(), OWNER, db)

    assert _assigned_role_ids(db, u) == {tech.id, lead.id}
    revoke.assert_called_once_with(str(u.id), reason="role_changed")
    (row,) = _audits(db, "user_role_changed")
    assert row.details == {
        "old_role": "technician",
        "new_role": "technician",
        "dropped_assignments": [{"role_id": str(admin.id), "role_name": "admin"}],
        "sessions_revoked": 2,
    }


def test_reasserting_the_builtin_role_is_not_reported_as_dropped(db):
    from gdx_dispatch.routers.users import RoleChangeIn, change_role

    _role(db, "technician")
    dispatcher = _role(db, "dispatcher")
    u = _user(db)
    # users.role says technician but the assignment already points at the
    # target role: nothing is lost by the rewrite.
    _assign(db, u, dispatcher)

    change_role(u.id, RoleChangeIn(role="dispatcher"), _request(), OWNER, db)

    assert _assigned_role_ids(db, u) == {dispatcher.id}
    (row,) = _audits(db, "user_role_changed")
    assert row.details["dropped_assignments"] == []


# ── update_user / update_me ─────────────────────────────────────────────────


def _dialog_payload(u: User, **over) -> dict:
    """What UsersView sends on every save: every field, changed or not."""
    body = {
        "name": u.full_name, "email": u.email, "phone": u.phone,
        "route_start_address": u.route_start_address, "certifications": u.certifications,
        "shift_start": u.shift_start, "shift_end": u.shift_end, "workdays": u.workdays,
        "vacation_allowance_hours": u.vacation_allowance_hours,
    }
    body.update(over)
    return body


def test_a_full_dialog_save_records_only_the_changed_fields_with_both_values(db):
    from gdx_dispatch.routers.users import UserPatchIn, update_user

    u = _user(db, phone="555-0100", shift_start=time(7, 0), workdays=31)
    update_user(u.id, UserPatchIn(**_dialog_payload(u, phone="555-0199", shift_start="08:30")), _request(), OWNER, db)

    (row,) = _audits(db, "user_updated")
    assert row.details == {
        "changed_fields": ["phone", "shift_start"],
        "phone": {"from": "555-0100", "to": "555-0199"},
        "shift_start": {"from": "07:00:00", "to": "08:30:00"},
    }
    db.expire_all()
    assert db.get(User, u.id).phone == "555-0199"


def test_an_unchanged_dialog_save_writes_no_audit_row(db):
    from gdx_dispatch.routers.users import UserPatchIn, update_user

    u = _user(db, phone="555-0100", vacation_allowance_hours=40)
    stamp = u.updated_at
    out = update_user(u.id, UserPatchIn(**_dialog_payload(u, vacation_allowance_hours="40.00")), _request(), OWNER, db)

    assert out["phone"] == "555-0100"
    assert _audits(db, "user_updated") == []
    db.expire_all()
    assert db.get(User, u.id).updated_at == stamp


def test_a_self_edit_records_the_prior_value(db):
    from gdx_dispatch.routers.users import SelfPatchIn, update_me

    u = _user(db, phone="555-0100")
    me = {"sub": str(u.id), "user_id": str(u.id), "role": "technician", "tenant_id": TENANT}
    update_me(SelfPatchIn(name=u.full_name, phone="555-0142"), _request(), me, db)
    update_me(SelfPatchIn(name=u.full_name, phone="555-0142"), _request(), me, db)

    (row,) = _audits(db, "user_self_updated")
    assert row.details == {"changed_fields": ["phone"], "phone": {"from": "555-0100", "to": "555-0142"}}


# ── admin_ops ───────────────────────────────────────────────────────────────


def test_admin_permission_overwrite_keeps_the_old_set(db):
    from gdx_dispatch.routers.admin_ops import RolePermissionsUpdate, update_role_permissions

    db.add(RolePermission(role="dispatcher", permissions=json.dumps(["jobs.read", "jobs.write"])))
    db.commit()
    update_role_permissions(RolePermissionsUpdate(role="dispatcher", permissions=["jobs.read"]), OWNER, db, _request())

    (row,) = _audits(db, "permissions_changed")
    assert row.details == {"permissions": ["jobs.read"], "old_permissions": ["jobs.read", "jobs.write"]}


def test_admin_deactivate_records_the_prior_active_flag(db):
    from gdx_dispatch.routers.admin_ops import deactivate_user

    u = _user(db, active=True)
    deactivate_user(u.id, OWNER, db, _request())

    (row,) = _audits(db, "user_deactivated")
    assert row.details == {"active": False, "old_active": True}


# ── role_permissions ────────────────────────────────────────────────────────


def test_assigning_a_builtin_role_records_the_users_role_it_overwrites(db):
    from gdx_dispatch.routers.role_permissions import AssignRoleIn, assign_role

    dispatcher = _role(db, "dispatcher")
    u = _user(db, role="technician")
    assign_role(str(u.id), AssignRoleIn(role_id=str(dispatcher.id)), _request(), OWNER, db)

    db.expire_all()
    assert db.get(User, u.id).role == "dispatcher"
    (row,) = _audits(db, "user_role_assigned")
    assert row.details["old_users_role"] == "technician"
    assert row.details["new_users_role"] == "dispatcher"


def test_renaming_a_custom_role_keeps_the_old_name_and_description(db):
    from gdx_dispatch.routers.role_permissions import RolePatch, update_role

    lead = _role(db, "Lead Tech", system=False)
    lead.description = "Runs the install crew"
    db.commit()
    update_role(lead.id, RolePatch(name="Crew Lead", description="Leads a crew"), _request(), OWNER, db)

    (row,) = _audits(db, "role_updated")
    assert row.details["before_name"] == "Lead Tech"
    assert row.details["name"] == "Crew Lead"
    assert row.details["before_description"] == "Runs the install crew"
    assert row.details["after_description"] == "Leads a crew"
