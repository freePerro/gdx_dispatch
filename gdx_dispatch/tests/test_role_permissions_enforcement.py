"""Sprint role-permissions 1.6 — enforcement tests for require_permission()."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.core.permissions import BUILTIN_ROLES, WILDCARD
from gdx_dispatch.models.tenant_models import TenantRole, UserRoleAssignment
from gdx_dispatch.routers.auth import get_current_user

TENANT_ID = "tenant-enforce"
USER_ID = "user-enforce"


def _build_app():
    """FastAPI app with two routes — one gated by jobs.read_all, one by payroll.write."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = Session()
    setup.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT
        )
    """))
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    state = {"role": "tech"}

    @app.middleware("http")
    async def inject_state(request, call_next):
        request.state.tenant = {"id": TENANT_ID}
        request.state.current_user = {
            "user_id": USER_ID,
            "sub": USER_ID,
            "role": state["role"],
            "tenant_id": TENANT_ID,
        }
        return await call_next(request)

    @app.get("/jobs", dependencies=[Depends(require_permission("jobs.read_all"))])
    def jobs_endpoint():
        return {"ok": "jobs"}

    @app.get("/payroll", dependencies=[Depends(require_permission("payroll.write"))])
    def payroll_endpoint():
        return {"ok": "payroll"}

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": USER_ID, "sub": USER_ID, "role": state["role"], "tenant_id": TENANT_ID,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    tc._Session = Session  # type: ignore[attr-defined]
    tc._state = state  # type: ignore[attr-defined]
    return tc


def _seed_role_with_perms(tc: TestClient, perms: list[str]) -> None:
    """Insert a TenantRole + UserRoleAssignment for the test user."""
    Session = tc._Session  # type: ignore[attr-defined]
    with Session() as db:
        # Wipe prior assignments/roles for repeatable tests
        db.query(UserRoleAssignment).delete()
        db.query(TenantRole).delete()
        role = TenantRole(
            id=uuid4(),
            company_id=TENANT_ID,
            name="custom",
            permissions=json.dumps(perms),
            is_system=False,
        )
        db.add(role)
        db.flush()
        db.add(
            UserRoleAssignment(
                id=uuid4(),
                company_id=TENANT_ID,
                user_id=USER_ID,
                role_id=role.id,
            )
        )
        db.commit()


@pytest.fixture()
def tc():
    client = _build_app()
    yield client
    client.app.dependency_overrides.clear()
    client._engine.dispose()  # type: ignore[attr-defined]


def test_legacy_owner_jwt_falls_back_to_builtin_when_db_empty(tc):
    """Bootstrap: a JWT-claimed owner with NO assigned role + no User row
    falls back to BUILTIN_ROLES["owner"] = wildcard. Once a UserRoleAssignment
    is written (or User.role is set), the DB takes over (see
    test_db_role_demotion_takes_effect_immediately)."""
    tc._state["role"] = "owner"  # type: ignore[attr-defined]
    assert tc.get("/jobs").status_code == 200
    assert tc.get("/payroll").status_code == 200


def test_legacy_admin_jwt_falls_back_to_builtin_when_db_empty(tc):
    tc._state["role"] = "admin"  # type: ignore[attr-defined]
    assert tc.get("/jobs").status_code == 200
    assert tc.get("/payroll").status_code == 200


def test_change_role_endpoint_syncs_assignment_immediately(tc):
    """When admin changes a user's legacy role via POST /api/users/{id}/role,
    the user_role_assignments row must be rewritten so the new RBAC layer
    picks up the change immediately. Without this sync, demoting an admin
    to tech leaves their old permissions intact (Doug-reported 2026-05-02).
    """
    from gdx_dispatch.routers.users import _sync_user_role_assignment
    from gdx_dispatch.models.tenant_models import TenantRole, UserRoleAssignment
    Session = tc._Session  # type: ignore[attr-defined]
    with Session() as db:
        # Seed both roles
        for name, perms in [("admin", ["*"]), ("technician", ["jobs.read_own"])]:
            db.add(TenantRole(
                id=uuid4(), company_id=TENANT_ID, name=name,
                permissions=json.dumps(perms), is_system=True,
            ))
        db.commit()
        # Initial assignment: admin
        admin = db.query(TenantRole).filter_by(name="admin").first()
        db.add(UserRoleAssignment(
            id=uuid4(), company_id=TENANT_ID, user_id=USER_ID, role_id=admin.id,
        ))
        db.commit()

        # Demote via the helper (mirrors what change_role does)
        _sync_user_role_assignment(db, TENANT_ID, USER_ID, "tech")
        db.commit()

        rows = db.query(UserRoleAssignment).filter_by(
            company_id=TENANT_ID, user_id=USER_ID
        ).all()
        assert len(rows) == 1, "should leave exactly one assignment"
        new_role = db.query(TenantRole).filter_by(id=rows[0].role_id).one()
        assert new_role.name == "technician", "legacy 'tech' must map to TenantRole 'technician'"


def test_db_role_demotion_takes_effect_immediately(tc):
    """Stale-JWT fix: even with JWT claiming role=admin, if the DB has the
    user assigned to a role with no permissions, they get 403. The DB is
    the truth — JWT claim cannot over-privilege."""
    tc._state["role"] = "admin"  # type: ignore[attr-defined]
    _seed_role_with_perms(tc, [])  # explicit DB assignment with no perms
    assert tc.get("/jobs").status_code == 403, "JWT-admin must NOT bypass DB role demotion"
    assert tc.get("/payroll").status_code == 403


def test_wildcard_grants_everything(tc):
    tc._state["role"] = "tech"  # type: ignore[attr-defined]
    _seed_role_with_perms(tc, [WILDCARD])
    assert tc.get("/jobs").status_code == 200
    assert tc.get("/payroll").status_code == 200


def test_specific_permission_grants_that_route_only(tc):
    tc._state["role"] = "tech"  # type: ignore[attr-defined]
    _seed_role_with_perms(tc, ["jobs.read_all"])
    assert tc.get("/jobs").status_code == 200
    r = tc.get("/payroll")
    assert r.status_code == 403
    assert "payroll.write" in r.json()["detail"]


def test_no_permissions_blocks_everything(tc):
    tc._state["role"] = "tech"  # type: ignore[attr-defined]
    _seed_role_with_perms(tc, [])
    assert tc.get("/jobs").status_code == 403
    assert tc.get("/payroll").status_code == 403


def test_permission_edit_takes_effect_immediately(tc):
    """No cross-request cache: editing the role grants applies on the very next call."""
    tc._state["role"] = "tech"  # type: ignore[attr-defined]
    _seed_role_with_perms(tc, [])
    assert tc.get("/jobs").status_code == 403

    # Update the user's role permissions in-place
    Session = tc._Session  # type: ignore[attr-defined]
    with Session() as db:
        row = db.query(TenantRole).first()
        row.permissions = json.dumps(["jobs.read_all"])
        db.commit()

    assert tc.get("/jobs").status_code == 200


@pytest.mark.parametrize("role_name,perms", list(BUILTIN_ROLES.items()))
def test_builtin_role_jobs_read_all_matches_catalog(tc, role_name, perms):
    """For each builtin role, /jobs (jobs.read_all) returns 200 iff the catalog grants it."""
    tc._state["role"] = role_name  # type: ignore[attr-defined]
    _seed_role_with_perms(tc, perms)
    expected = 200 if (WILDCARD in perms or "jobs.read_all" in perms) else 403
    assert tc.get("/jobs").status_code == expected, f"role={role_name} perms={perms}"
