"""Tests for gdx_dispatch.routers.auth.pats_support — the /api/capabilities/available
and /api/admin/tenant-members endpoints that unblock SettingsApiKeys.vue
and TenantAdminApiKeys.vue."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.platform import Membership
from gdx_dispatch.routers.auth.pats_support import router
from gdx_dispatch.tests.factories.platform import (
    make_capability_set,
    make_identity,
    make_tenant,
)


def _principal(*, identity_id, tenant_id, role="admin", capabilities=()):
    return SimpleNamespace(
        identity_id=identity_id,
        tenant_id=tenant_id,
        principal_role=role,
        capabilities=tuple(capabilities),
        is_super_admin=False,
    )


@pytest.fixture
def client(control_db):
    app = FastAPI()
    app.include_router(router)

    def _override_db():
        yield control_db

    app.dependency_overrides[get_db] = _override_db

    def _set_principal(principal):
        app.dependency_overrides[get_current_principal] = lambda: principal

    return TestClient(app), control_db, _set_principal


def test_capabilities_available_returns_principal_caps(client):
    c, db, set_principal = client
    identity = make_identity(db)
    tenant = make_tenant(db)
    db.commit()
    caps = (("read", "job"), ("write", "job"))
    set_principal(_principal(identity_id=identity.id, tenant_id=tenant.id, capabilities=caps))

    resp = c.get("/api/capabilities/available")
    assert resp.status_code == 200
    body = resp.json()
    assert {"action": "read", "resource_type": "job"} in body
    assert {"action": "write", "resource_type": "job"} in body
    assert len(body) == 2


def test_capabilities_available_empty_when_principal_has_none(client):
    c, db, set_principal = client
    identity = make_identity(db)
    tenant = make_tenant(db)
    db.commit()
    set_principal(_principal(identity_id=identity.id, tenant_id=tenant.id, capabilities=()))

    resp = c.get("/api/capabilities/available")
    assert resp.status_code == 200
    assert resp.json() == []


def test_tenant_members_admin_sees_members_of_own_tenant(client):
    c, db, set_principal = client
    tenant_a = make_tenant(db, slug="tenant-a")
    tenant_b = make_tenant(db, slug="tenant-b")
    cap_set = make_capability_set(db)
    admin = make_identity(db, email="admin@a.example")
    member_a = make_identity(db, email="a@a.example", display_name="Alice A")
    member_b = make_identity(db, email="b@b.example", display_name="Bob B")
    db.flush()
    db.add(Membership(identity_id=admin.id, tenant_id=tenant_a.id, role="admin", capability_set_id=cap_set.id))
    db.add(Membership(identity_id=member_a.id, tenant_id=tenant_a.id, role="tech", capability_set_id=cap_set.id))
    db.add(Membership(identity_id=member_b.id, tenant_id=tenant_b.id, role="tech", capability_set_id=cap_set.id))
    db.commit()

    set_principal(_principal(identity_id=admin.id, tenant_id=tenant_a.id, role="admin"))
    resp = c.get("/api/admin/tenant-members")
    assert resp.status_code == 200
    body = resp.json()
    ids = {row["identity_id"] for row in body}
    assert str(member_a.id) in ids
    assert str(admin.id) in ids          # admin's own membership is in their tenant too
    assert str(member_b.id) not in ids   # tenant-b member must not leak
    alice = next(r for r in body if r["email"] == "a@a.example")
    assert alice["display_name"] == "Alice A"
    assert alice["role"] == "tech"


def test_tenant_members_non_admin_gets_403(client):
    c, db, set_principal = client
    tenant = make_tenant(db)
    identity = make_identity(db)
    db.commit()
    set_principal(_principal(identity_id=identity.id, tenant_id=tenant.id, role="tech"))

    resp = c.get("/api/admin/tenant-members")
    assert resp.status_code == 403


def test_tenant_members_excludes_revoked_and_deleted(client):
    c, db, set_principal = client
    from datetime import datetime, timezone
    tenant = make_tenant(db)
    cap_set = make_capability_set(db)
    admin = make_identity(db, email="admin@x.example")
    revoked_m = make_identity(db, email="revoked@x.example")
    deleted_i = make_identity(db, email="deleted@x.example")
    db.flush()
    db.add(Membership(identity_id=admin.id, tenant_id=tenant.id, role="admin", capability_set_id=cap_set.id))
    db.add(Membership(
        identity_id=revoked_m.id, tenant_id=tenant.id, role="tech",
        capability_set_id=cap_set.id, revoked_at=datetime.now(timezone.utc),
    ))
    db.add(Membership(identity_id=deleted_i.id, tenant_id=tenant.id, role="tech", capability_set_id=cap_set.id))
    deleted_i.deleted_at = datetime.now(timezone.utc)
    db.commit()

    set_principal(_principal(identity_id=admin.id, tenant_id=tenant.id, role="admin"))
    resp = c.get("/api/admin/tenant-members")
    assert resp.status_code == 200
    emails = {row["email"] for row in resp.json()}
    assert "revoked@x.example" not in emails
    assert "deleted@x.example" not in emails
    assert "admin@x.example" in emails
