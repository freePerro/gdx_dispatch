"""require_permission() unit tests — slice 1.2 of sprint_role_permissions."""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.core.permissions import WILDCARD
from gdx_dispatch.models.tenant_models import TenantRole, UserRoleAssignment


TENANT_ID = "11111111-1111-1111-1111-111111111111"


def _seed_role(db, name: str, perms: list[str]) -> TenantRole:
    role = TenantRole(
        id=uuid4(),
        company_id=TENANT_ID,
        name=name,
        permissions=json.dumps(perms),
        is_system=False,
    )
    db.add(role)
    db.commit()
    return role


def _assign(db, user_id: str, role: TenantRole) -> None:
    db.add(
        UserRoleAssignment(
            id=uuid4(),
            company_id=TENANT_ID,
            user_id=user_id,
            role_id=role.id,
            assigned_by="test",
        )
    )
    db.commit()


def _build_app(tenant_db, user: dict, *required: str) -> TestClient:
    app = FastAPI()

    def _override_db():
        yield tenant_db

    app.dependency_overrides[get_db] = _override_db

    @app.get("/probe", dependencies=[Depends(require_permission(*required))])
    async def _probe():
        return {"ok": True}

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = user
        request.state.tenant = {"id": TENANT_ID}
        return await call_next(request)

    return TestClient(app)


def test_owner_role_passes_without_db(tenant_db):
    client = _build_app(tenant_db, {"sub": "u-owner", "role": "owner"}, "jobs.write")
    r = client.get("/probe")
    assert r.status_code == 200, r.text


def test_admin_role_passes_without_db(tenant_db):
    client = _build_app(tenant_db, {"sub": "u-admin", "role": "admin"}, "jobs.delete")
    r = client.get("/probe")
    assert r.status_code == 200, r.text


def test_granted_permission_passes(tenant_db):
    role = _seed_role(tenant_db, "lead_tech", ["jobs.read_all", "jobs.write"])
    _assign(tenant_db, "u-tech", role)
    client = _build_app(tenant_db, {"sub": "u-tech", "role": "technician"}, "jobs.write")
    r = client.get("/probe")
    assert r.status_code == 200, r.text


def test_missing_permission_403s(tenant_db):
    role = _seed_role(tenant_db, "lead_tech", ["jobs.read_own"])
    _assign(tenant_db, "u-tech", role)
    client = _build_app(tenant_db, {"sub": "u-tech", "role": "technician"}, "jobs.write")
    r = client.get("/probe")
    assert r.status_code == 403
    assert "jobs.write" in r.text


def test_wildcard_in_assignment_passes(tenant_db):
    role = _seed_role(tenant_db, "all_access", [WILDCARD])
    _assign(tenant_db, "u-bot", role)
    client = _build_app(tenant_db, {"sub": "u-bot", "role": "viewer"}, "invoices.refund")
    r = client.get("/probe")
    assert r.status_code == 200, r.text


def test_no_assignment_falls_back_to_legacy_role(tenant_db):
    # technician builtin grants jobs.read_own
    client = _build_app(tenant_db, {"sub": "u-orphan", "role": "technician"}, "jobs.read_own")
    r = client.get("/probe")
    assert r.status_code == 200, r.text


def test_no_assignment_legacy_role_lacks_permission(tenant_db):
    # technician builtin does NOT grant invoices.refund
    client = _build_app(tenant_db, {"sub": "u-orphan", "role": "technician"}, "invoices.refund")
    r = client.get("/probe")
    assert r.status_code == 403


def test_per_request_cache_one_db_hit(tenant_db, monkeypatch):
    """Two require_permission gates on the same route hit the DB once."""
    role = _seed_role(tenant_db, "multi", ["jobs.write", "customers.write"])
    _assign(tenant_db, "u-multi", role)

    from gdx_dispatch.core import modules as _mod
    calls = {"n": 0}
    real_loader = _mod._load_user_permissions

    def _counting_loader(*args, **kwargs):
        calls["n"] += 1
        return real_loader(*args, **kwargs)

    monkeypatch.setattr(_mod, "_load_user_permissions", _counting_loader)

    from fastapi import Depends
    app = FastAPI()
    def _override_db():
        yield tenant_db
    app.dependency_overrides[get_db] = _override_db

    @app.get(
        "/probe",
        dependencies=[
            Depends(require_permission("jobs.write")),
            Depends(require_permission("customers.write")),
        ],
    )
    async def _probe():
        return {"ok": True}

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = {"sub": "u-multi", "role": "technician"}
        request.state.tenant = {"id": TENANT_ID}
        return await call_next(request)

    client = TestClient(app)
    r = client.get("/probe")
    assert r.status_code == 200, r.text
    assert calls["n"] == 1, f"expected 1 DB load, got {calls['n']}"


def test_custom_role_edit_takes_effect_immediately(tenant_db):
    """A perm change between requests must reflect in the next request (no module-level cache)."""
    role = _seed_role(tenant_db, "edit_test", ["jobs.read_own"])
    _assign(tenant_db, "u-edit", role)

    client = _build_app(tenant_db, {"sub": "u-edit", "role": "technician"}, "jobs.write")
    assert client.get("/probe").status_code == 403

    role.permissions = json.dumps(["jobs.read_own", "jobs.write"])
    tenant_db.commit()

    assert client.get("/probe").status_code == 200


def test_no_required_keys_raises():
    with pytest.raises(ValueError):
        require_permission()


# ─── S114 regression: admin/owner snapshot can NEVER lock the user out ──────
# (closes D-S97-perm-snapshot)

def _seed_user(db, user_id, role: str) -> None:
    """Seed a User row so _load_user_permissions' DB-truth role lookup hits.
    Returns the user_id as the original string so callers can pass it
    unchanged to require_permission's `sub` claim."""
    from uuid import UUID
    from gdx_dispatch.models.tenant_models import User
    uid_obj = user_id if not isinstance(user_id, str) else UUID(user_id)
    db.add(User(
        id=uid_obj,
        email=f"{user_id}@test.local",
        name=str(user_id),
        role=role,
        company_id=TENANT_ID,
        active=True,
    ))
    db.commit()


def test_admin_snapshot_missing_new_key_still_passes(tenant_db):
    """Reproduces S97 D-item: admin TenantRole snapshot was taken at signup
    and has NOT been updated to include `pricing.labor_matrix.read` (added
    later). Resolver MUST ignore the stale snapshot for admin/owner and
    return the BUILTIN set."""
    user_id = str(uuid4())
    _seed_user(tenant_db, user_id, "admin")
    # Stale snapshot — missing the new permission entirely
    stale_admin = _seed_role(tenant_db, "admin", ["jobs.read_all", "customers.read"])
    _assign(tenant_db, user_id, stale_admin)

    client = _build_app(
        tenant_db,
        {"sub": user_id, "role": "admin"},
        "pricing.labor_matrix.read",
    )
    r = client.get("/probe")
    assert r.status_code == 200, (
        f"Admin must ALWAYS get BUILTIN permissions even with a stale snapshot — got {r.status_code} {r.text}"
    )


def test_owner_snapshot_missing_wildcard_still_passes(tenant_db):
    """Owner BUILTIN is wildcard. A snapshot that lost the wildcard MUST
    not lock owner out."""
    user_id = str(uuid4())
    _seed_user(tenant_db, user_id, "owner")
    stale_owner = _seed_role(tenant_db, "owner", ["jobs.read_all"])  # missing *
    _assign(tenant_db, user_id, stale_owner)

    client = _build_app(
        tenant_db,
        {"sub": user_id, "role": "owner"},
        "billing.write",
    )
    r = client.get("/probe")
    assert r.status_code == 200, (
        f"Owner must always get wildcard via BUILTIN — got {r.status_code} {r.text}"
    )


def test_non_admin_snapshot_still_authoritative(tenant_db):
    """Non-admin roles still respect the per-tenant snapshot — that's the
    whole point of customizable roles. Only admin/owner bypass it."""
    user_id = str(uuid4())
    _seed_user(tenant_db, user_id, "dispatcher")
    # Customized dispatcher with EXTRA permission beyond BUILTIN
    custom = _seed_role(tenant_db, "dispatcher_custom", ["jobs.write", "invoices.refund"])
    _assign(tenant_db, user_id, custom)

    client = _build_app(
        tenant_db,
        {"sub": user_id, "role": "dispatcher"},
        "invoices.refund",
    )
    r = client.get("/probe")
    assert r.status_code == 200


def test_admin_snapshot_with_billing_write_does_not_grant_it(tenant_db):
    """BUILTIN admin specifically EXCLUDES billing.write (only owner has it).
    A tenant who edited the admin snapshot to include billing.write must NOT
    actually get billing.write — admin/owner permissions are platform-defined,
    not tenant-editable. This is the inverse of the lock-out fix: the same
    rule prevents over-grant too."""
    user_id = str(uuid4())
    _seed_user(tenant_db, user_id, "admin")
    # Tenant tried to grant admin billing.write via the snapshot
    overgranted = _seed_role(tenant_db, "admin", ["billing.write"])
    _assign(tenant_db, user_id, overgranted)

    client = _build_app(
        tenant_db,
        {"sub": user_id, "role": "admin"},
        "billing.write",
    )
    r = client.get("/probe")
    assert r.status_code == 403, (
        "Admin BUILTIN excludes billing.write; snapshot must not be able to escalate."
    )
