"""Tests for gdx_dispatch.routers.auth.admin_pats (SS-15 slice B)."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth.admin_pats import router


def RouterPrincipal(*, identity_id, tenant_id, capabilities=None):
    return SimpleNamespace(
        identity_id=identity_id,
        tenant_id=tenant_id,
        principal_role="tech",
        capabilities=tuple(capabilities or ()),
        is_super_admin=False,
    )
from gdx_dispatch.tests.factories.platform import (
    make_capability,
    make_capability_set,
    make_identity,
    make_membership,
    make_tenant,
)


@pytest.fixture
def client_and_db(control_db):
    app = FastAPI()
    app.include_router(router)

    def _override_db():
        try:
            yield control_db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db

    def _set_principal(principal) -> None:
        app.dependency_overrides[get_current_principal] = lambda: principal

    client = TestClient(app)
    return client, control_db, _set_principal


def _setup_admin_and_target(db, *, admin_caps=None):
    """Create a tenant, an admin identity (member), and a target identity (member)."""
    tenant = make_tenant(db)
    admin_identity = make_identity(db)
    target_identity = make_identity(db)
    # Both are members of the tenant.
    make_membership(db, identity=admin_identity, tenant=tenant, role="admin")
    make_membership(db, identity=target_identity, tenant=tenant, role="tech")
    db.commit()

    if admin_caps is None:
        admin_caps = [
            {"action": "admin", "resource_type": "tenant"},
            {"action": "read", "resource_type": "job"},
        ]
    principal = RouterPrincipal(
        identity_id=admin_identity.id,
        tenant_id=str(tenant.id),
        capabilities=admin_caps,
    )
    return principal, tenant, admin_identity, target_identity


def test_admin_mints_pat_for_target_in_tenant(client_and_db):
    client, db, set_principal = client_and_db
    principal, tenant, _admin, target = _setup_admin_and_target(db)

    capset = make_capability_set(db)
    cap = make_capability(db, capability_set=capset, action="read", resource_type="job")
    db.commit()

    set_principal(principal)
    resp = client.post(
        "/api/admin/pats",
        json={
            "target_identity_id": str(target.id),
            "name": "admin-issued",
            "capability_ids": [str(cap.id)],
            "expires_in_days": 30,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "admin-issued"
    assert body["target_identity_id"] == str(target.id)
    # Read-only → activates immediately and secret is released.
    assert body["status"] == "active"
    assert body["secret"].startswith("gdx_pat_live_")


def test_non_admin_cannot_mint_for_someone_else(client_and_db):
    client, db, set_principal = client_and_db
    # Caller lacks tenant:admin capability.
    principal, _tenant, _admin, target = _setup_admin_and_target(
        db, admin_caps=[{"action": "read", "resource_type": "job"}]
    )

    capset = make_capability_set(db)
    cap = make_capability(db, capability_set=capset, action="read", resource_type="job")
    db.commit()

    set_principal(principal)
    resp = client.post(
        "/api/admin/pats",
        json={
            "target_identity_id": str(target.id),
            "name": "x",
            "capability_ids": [str(cap.id)],
        },
    )
    assert resp.status_code == 403
    assert "tenant admin" in resp.json()["detail"]


def test_admin_cannot_mint_for_target_outside_tenant(client_and_db):
    client, db, set_principal = client_and_db
    principal, _tenant, _admin, _target = _setup_admin_and_target(db)

    # Outsider identity — no membership in admin's tenant.
    outsider = make_identity(db)
    capset = make_capability_set(db)
    cap = make_capability(db, capability_set=capset, action="read", resource_type="job")
    db.commit()

    set_principal(principal)
    resp = client.post(
        "/api/admin/pats",
        json={
            "target_identity_id": str(outsider.id),
            "name": "y",
            "capability_ids": [str(cap.id)],
        },
    )
    assert resp.status_code == 403
    assert "not in your tenant" in resp.json()["detail"]


def test_write_scope_pat_requires_approval_then_activates(client_and_db):
    client, db, set_principal = client_and_db
    principal, _tenant, _admin, target = _setup_admin_and_target(
        db,
        admin_caps=[
            {"action": "admin", "resource_type": "tenant"},
            {"action": "write", "resource_type": "job"},
        ],
    )

    capset = make_capability_set(db)
    cap = make_capability(db, capability_set=capset, action="write", resource_type="job")
    db.commit()

    set_principal(principal)
    resp = client.post(
        "/api/admin/pats",
        json={
            "target_identity_id": str(target.id),
            "name": "writer",
            "capability_ids": [str(cap.id)],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending_approval"
    # No secret released yet.
    assert "secret" not in body
    pat_id = body["id"]

    resp2 = client.post(f"/api/admin/pats/{pat_id}/approve")
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["status"] == "active"
    assert body2["approved"] is True
    assert body2["secret"].startswith("gdx_pat_live_")


def test_subset_violation_rejects(client_and_db):
    client, db, set_principal = client_and_db
    principal, _tenant, _admin, target = _setup_admin_and_target(
        db,
        admin_caps=[
            {"action": "admin", "resource_type": "tenant"},
            {"action": "read", "resource_type": "job"},
        ],
    )

    capset = make_capability_set(db)
    # Admin lacks "write:invoice" — should be refused.
    cap = make_capability(db, capability_set=capset, action="write", resource_type="invoice")
    db.commit()

    set_principal(principal)
    resp = client.post(
        "/api/admin/pats",
        json={
            "target_identity_id": str(target.id),
            "name": "z",
            "capability_ids": [str(cap.id)],
        },
    )
    assert resp.status_code == 403
    assert "cannot grant capability" in resp.json()["detail"]


def test_list_admin_pats_scoped_to_tenant_members(client_and_db):
    client, db, set_principal = client_and_db
    principal, _tenant, _admin, target = _setup_admin_and_target(db)

    capset = make_capability_set(db)
    cap = make_capability(db, capability_set=capset, action="read", resource_type="job")
    db.commit()

    set_principal(principal)
    mint = client.post(
        "/api/admin/pats",
        json={
            "target_identity_id": str(target.id),
            "name": "list-me",
            "capability_ids": [str(cap.id)],
        },
    )
    assert mint.status_code == 201

    resp = client.get("/api/admin/pats")
    assert resp.status_code == 200
    body = resp.json()
    # Envelope shape: {items: [...], meta: {...}}. See gdx_dispatch.core.pagination.
    assert "items" in body and "meta" in body
    names = {r["name"] for r in body["items"]}
    assert "list-me" in names
    assert body["meta"]["total"] >= 1
    assert body["meta"]["offset"] == 0
    assert body["meta"]["limit"] == 50


def test_admin_revoke_pat(client_and_db):
    client, db, set_principal = client_and_db
    principal, _tenant, _admin, target = _setup_admin_and_target(db)

    capset = make_capability_set(db)
    cap = make_capability(db, capability_set=capset, action="read", resource_type="job")
    db.commit()

    set_principal(principal)
    mint = client.post(
        "/api/admin/pats",
        json={
            "target_identity_id": str(target.id),
            "name": "kill-me",
            "capability_ids": [str(cap.id)],
        },
    )
    pat_id = mint.json()["id"]

    resp = client.delete(f"/api/admin/pats/{pat_id}")
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True


def test_missing_target_identity_id_rejected(client_and_db):
    client, db, set_principal = client_and_db
    principal, _tenant, _admin, _target = _setup_admin_and_target(db)
    set_principal(principal)
    resp = client.post("/api/admin/pats", json={"name": "oops", "capability_ids": []})
    assert resp.status_code == 400


def test_name_length_capped(client_and_db):
    """0.9-s A6: name >128 chars rejected in-request."""
    client, db, set_principal = client_and_db
    principal, _tenant, _admin, target = _setup_admin_and_target(db)
    db.commit()
    set_principal(principal)
    resp = client.post(
        "/api/admin/pats",
        json={
            "target_identity_id": str(target.id),
            "name": "x" * 300,
            "capability_ids": [],
        },
    )
    assert resp.status_code == 400
    assert "128" in resp.json()["detail"]


def test_capability_ids_size_capped(client_and_db):
    """0.9-s A5: capability_ids list size >1000 rejected."""
    import uuid
    client, db, set_principal = client_and_db
    principal, _tenant, _admin, target = _setup_admin_and_target(db)
    db.commit()
    set_principal(principal)
    resp = client.post(
        "/api/admin/pats",
        json={
            "target_identity_id": str(target.id),
            "name": "x",
            "capability_ids": [str(uuid.uuid4()) for _ in range(1001)],
        },
    )
    assert resp.status_code == 400
    assert "1000" in resp.json()["detail"]


def test_bad_target_uuid_rejected(client_and_db):
    client, db, set_principal = client_and_db
    principal, _tenant, _admin, _target = _setup_admin_and_target(db)
    set_principal(principal)
    resp = client.post(
        "/api/admin/pats",
        json={"target_identity_id": "not-a-uuid", "name": "x", "capability_ids": []},
    )
    assert resp.status_code == 400


def test_approve_already_active_is_idempotent(client_and_db):
    client, db, set_principal = client_and_db
    principal, _tenant, _admin, target = _setup_admin_and_target(db)

    capset = make_capability_set(db)
    cap = make_capability(db, capability_set=capset, action="read", resource_type="job")
    db.commit()
    set_principal(principal)

    mint = client.post(
        "/api/admin/pats",
        json={
            "target_identity_id": str(target.id),
            "name": "ro",
            "capability_ids": [str(cap.id)],
        },
    )
    pat_id = mint.json()["id"]
    # Read-only → already active.
    resp = client.post(f"/api/admin/pats/{pat_id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


def test_approve_unknown_pat_404(client_and_db):
    client, db, set_principal = client_and_db
    principal, _tenant, _admin, _target = _setup_admin_and_target(db)
    set_principal(principal)
    resp = client.post(f"/api/admin/pats/{uuid4()}/approve")
    assert resp.status_code == 404


def test_pat_state_survives_mock_restart(client_and_db):
    """Sprint 0.9-f: PAT status + pending secret persist across a session
    restart because they live on real ORM columns (AccessToken.status,
    AccessToken.metadata_json), not an in-memory shim.
    """
    from sqlalchemy.orm import sessionmaker

    from gdx_dispatch.models.platform_extensions import AccessToken

    client, db, set_principal = client_and_db
    principal, _tenant, _admin, target = _setup_admin_and_target(
        db,
        admin_caps=[
            {"action": "admin", "resource_type": "tenant"},
            {"action": "write", "resource_type": "job"},
        ],
    )

    capset = make_capability_set(db)
    cap = make_capability(db, capability_set=capset, action="write", resource_type="job")
    db.commit()

    set_principal(principal)
    resp = client.post(
        "/api/admin/pats",
        json={
            "target_identity_id": str(target.id),
            "name": "restart-survivor",
            "capability_ids": [str(cap.id)],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending_approval"
    assert "secret" not in body  # pending secret is held server-side
    pat_id = UUID(body["id"])

    # Simulate process restart: close the active ORM session and open a
    # new one off the same engine (equivalent to the DB connection being
    # reestablished by a freshly-booted worker process).
    engine = db.get_bind()
    db.close()
    new_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        reloaded = new_session.get(AccessToken, pat_id)
        assert reloaded is not None, "PAT must persist across restart"
        # Status survives restart — proves it's on the real column, not
        # a process-local dict.
        assert reloaded.status == "pending_approval"
        # _pending_secret survives restart in metadata_json.
        assert reloaded.metadata_json is not None
        assert reloaded.metadata_json.get("_pending_secret", "").startswith(
            "gdx_pat_live_"
        )
    finally:
        new_session.close()
