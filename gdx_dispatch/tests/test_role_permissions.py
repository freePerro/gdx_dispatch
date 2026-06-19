"""Tests for the role_permissions router (fine-grained RBAC)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.role_permissions import (
    BUILTIN_ROLES,
    router,
)


def _make_client(
    tenant_id: str = "tenant-test",
    actor_role: str = "admin",
    actor_id: str = "u1",
) -> TestClient:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
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
    setup.execute(text("""
        CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key)
        )
    """))
    setup.execute(
        text("INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at) "
             "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"),
        {"id": f"g1-{tenant_id}", "tid": tenant_id},
    )
    setup.execute(
        text("INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
             "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    actor = {
        "user_id": actor_id, "sub": actor_id, "role": actor_role, "tenant_id": tenant_id,
    }

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        # _resolve_request_user reads request.state.current_user first, so
        # this is what determines the actor's identity for require_permission.
        request.state.current_user = actor
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: actor

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def test_list_auto_seeds_builtins(client):
    r = client.get("/api/role-permissions/roles")
    assert r.status_code == 200, r.text
    roles = r.json()
    names = {r["name"] for r in roles}
    assert names == set(BUILTIN_ROLES.keys())
    for role in roles:
        assert role["is_system"] is True
    # calling twice is idempotent
    r2 = client.get("/api/role-permissions/roles")
    assert len(r2.json()) == len(BUILTIN_ROLES)


def test_create_custom_role(client):
    payload = {
        "name": "Field Lead",
        "description": "Senior tech with extra permissions",
        "permissions": ["jobs.read_all", "jobs.write", "customers.read_all"],
    }
    r = client.post("/api/role-permissions/roles", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Field Lead"
    assert body["permissions"] == ["jobs.read_all", "jobs.write", "customers.read_all"]
    assert body["is_system"] is False


def test_name_pattern_rejects_leading_digit(client):
    r = client.post(
        "/api/role-permissions/roles",
        json={"name": "1bad", "permissions": []},
    )
    assert r.status_code == 422


def test_unknown_permission_rejected(client):
    r = client.post(
        "/api/role-permissions/roles",
        json={"name": "Weird", "permissions": ["jobs.blowup"]},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("role_name", ["owner", "admin"])
def test_patch_platform_locked_role_returns_400(client, role_name):
    """owner + admin are platform-contract roles. The resolver in
    gdx_dispatch/core/modules.py (S97) ignores DB drift for these, so a save would
    silently no-op — the router 400s instead and tells the user to clone."""
    roles = client.get("/api/role-permissions/roles").json()
    target = next(r for r in roles if r["name"] == role_name)
    assert target["is_platform_locked"] is True
    r = client.patch(
        f"/api/role-permissions/roles/{target['id']}",
        json={"description": "tampered", "permissions": ["jobs.read_all"]},
    )
    assert r.status_code == 400
    assert "platform-contract" in r.json()["detail"]
    # GET shows the unchanged row.
    after = client.get(f"/api/role-permissions/roles/{target['id']}").json()
    assert after["description"] == target["description"]
    assert after["permissions"] == target["permissions"]


@pytest.mark.parametrize(
    "role_name",
    ["dispatcher", "technician", "sales", "accounting", "viewer"],
)
def test_patch_editable_seeded_role_persists(client, role_name):
    """The 5 non-platform-locked system roles ARE tenant-editable. Description
    + permissions (within the caller's own grant set) persist."""
    roles = client.get("/api/role-permissions/roles").json()
    target = next(r for r in roles if r["name"] == role_name)
    assert target["is_system"] is True
    assert target["is_platform_locked"] is False
    # Permissions the test actor (admin) is allowed to confer.
    new_perms = ["jobs.read_all", "customers.read_all"]
    r = client.patch(
        f"/api/role-permissions/roles/{target['id']}",
        json={"description": "tenant-customized", "permissions": new_perms},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == role_name  # canonical name preserved
    assert body["description"] == "tenant-customized"
    assert body["permissions"] == new_perms
    assert body["is_system"] is True
    after = client.get(f"/api/role-permissions/roles/{target['id']}").json()
    assert after["permissions"] == new_perms
    assert after["name"] == role_name


def test_patch_seeded_role_rename_returns_422(client):
    """Renames on seeded rows fail loud, not silently."""
    roles = client.get("/api/role-permissions/roles").json()
    viewer = next(r for r in roles if r["name"] == "viewer")
    r = client.patch(
        f"/api/role-permissions/roles/{viewer['id']}",
        json={"name": "RENAMED"},
    )
    assert r.status_code == 422
    assert "seeded" in r.json()["detail"]


# --- Delegation cap: caller can't grant permissions they don't hold ----------
# The test actor is the admin built-in (legacy_role="admin", no assignment) —
# resolver path 5 yields BUILTIN_ROLES["admin"] = _all_except("billing.write").
# So billing.write and "*" are out-of-set and should be rejected.

def test_admin_cannot_grant_billing_write_via_seeded_role(client):
    """Audit-found escalation: admin can't add billing.write to viewer and
    then assign users through it, because the conferral itself is barred."""
    roles = client.get("/api/role-permissions/roles").json()
    viewer = next(r for r in roles if r["name"] == "viewer")
    existing = viewer["permissions"]
    r = client.patch(
        f"/api/role-permissions/roles/{viewer['id']}",
        json={"permissions": [*existing, "billing.write"]},
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "billing.write" in detail
    # State unchanged.
    after = client.get(f"/api/role-permissions/roles/{viewer['id']}").json()
    assert "billing.write" not in after["permissions"]


def test_wildcard_cannot_be_granted_via_api(client):
    """Defense in depth: WILDCARD (`*`) is not in AVAILABLE_PERMISSIONS, so
    pydantic's _must_be_known validator 422s before the delegation cap even
    sees the payload. The only way `*` lands in a role's perms list is the
    BUILTIN_ROLES["owner"] seed at provisioning time."""
    roles = client.get("/api/role-permissions/roles").json()
    dispatcher = next(r for r in roles if r["name"] == "dispatcher")
    r = client.patch(
        f"/api/role-permissions/roles/{dispatcher['id']}",
        json={"permissions": ["*"]},
    )
    assert r.status_code == 422


def test_admin_cannot_create_custom_role_with_billing_write(client):
    """Delegation cap applies to create_role too — otherwise an admin could
    spin up a fresh custom role with billing.write and route users through it."""
    r = client.post(
        "/api/role-permissions/roles",
        json={"name": "ShadowOwner", "permissions": ["billing.write"]},
    )
    assert r.status_code == 403


def test_owner_can_grant_billing_write():
    """Owner (wildcard) bypasses the delegation cap — they can grant anything."""
    tc = _make_client(tenant_id="tenant-owner-test", actor_role="owner", actor_id="owner-1")
    try:
        roles = tc.get("/api/role-permissions/roles").json()
        viewer = next(r for r in roles if r["name"] == "viewer")
        existing = viewer["permissions"]
        r = tc.patch(
            f"/api/role-permissions/roles/{viewer['id']}",
            json={"permissions": [*existing, "billing.write"]},
        )
        assert r.status_code == 200, r.text
        assert "billing.write" in r.json()["permissions"]
    finally:
        tc.app.dependency_overrides.clear()
        tc._engine.dispose()  # type: ignore[attr-defined]


def test_reset_role_restores_builtin_defaults(client):
    """POST /roles/{id}/reset on a customized seeded role reverts permissions
    + description to the canonical BUILTIN_ROLES seed. Previously untested."""
    from gdx_dispatch.core.permissions import BUILTIN_DESCRIPTIONS, BUILTIN_ROLES
    roles = client.get("/api/role-permissions/roles").json()
    dispatcher = next(r for r in roles if r["name"] == "dispatcher")
    # Customize.
    client.patch(
        f"/api/role-permissions/roles/{dispatcher['id']}",
        json={"description": "drifted", "permissions": ["jobs.read_all"]},
    )
    # Reset.
    r = client.post(f"/api/role-permissions/roles/{dispatcher['id']}/reset")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["description"] == BUILTIN_DESCRIPTIONS["dispatcher"]
    assert sorted(body["permissions"]) == sorted(BUILTIN_ROLES["dispatcher"])
    assert body["is_system"] is True


def test_reset_custom_role_returns_400(client):
    """Reset is for seeded roles only — calling it on a custom role 400s."""
    created = client.post(
        "/api/role-permissions/roles",
        json={"name": "Custom", "permissions": ["jobs.read_all"]},
    ).json()
    r = client.post(f"/api/role-permissions/roles/{created['id']}/reset")
    assert r.status_code == 400


def test_delete_builtin_role_returns_400(client):
    roles = client.get("/api/role-permissions/roles").json()
    viewer = next(r for r in roles if r["name"] == "viewer")
    r = client.delete(f"/api/role-permissions/roles/{viewer['id']}")
    assert r.status_code == 400


def test_assign_role_to_user(client):
    roles = client.get("/api/role-permissions/roles").json()
    dispatcher = next(r for r in roles if r["name"] == "dispatcher")
    r = client.post(
        "/api/role-permissions/users/user-42/roles",
        json={"role_id": dispatcher["id"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user_id"] == "user-42"
    assert body["role_id"] == dispatcher["id"]
    assert body["role_name"] == "dispatcher"

    r2 = client.get("/api/role-permissions/users/user-42/roles")
    assert r2.status_code == 200
    assigned = r2.json()
    assert len(assigned) == 1
    assert assigned[0]["role_name"] == "dispatcher"


def test_assign_is_idempotent(client):
    roles = client.get("/api/role-permissions/roles").json()
    tech = next(r for r in roles if r["name"] == "technician")
    r1 = client.post(
        "/api/role-permissions/users/user-9/roles",
        json={"role_id": tech["id"]},
    )
    assert r1.status_code == 201
    first_id = r1.json()["id"]
    r2 = client.post(
        "/api/role-permissions/users/user-9/roles",
        json={"role_id": tech["id"]},
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == first_id

    listing = client.get("/api/role-permissions/users/user-9/roles").json()
    assert len(listing) == 1


def test_unassign_role(client):
    roles = client.get("/api/role-permissions/roles").json()
    admin = next(r for r in roles if r["name"] == "admin")
    client.post(
        "/api/role-permissions/users/user-7/roles",
        json={"role_id": admin["id"]},
    )
    r = client.delete(f"/api/role-permissions/users/user-7/roles/{admin['id']}")
    assert r.status_code == 204
    listing = client.get("/api/role-permissions/users/user-7/roles").json()
    assert listing == []


def test_audit_round_trip_create_update_delete(client):
    """Slice 4.1 — confirm before/after_permissions land in audit_logs.details."""
    from gdx_dispatch.core.audit import AuditLog
    Session = client._engine  # type: ignore[attr-defined]
    # Create
    r = client.post(
        "/api/role-permissions/roles",
        json={"name": "AuditMe", "permissions": ["jobs.read_all"]},
    )
    assert r.status_code == 201, r.text
    role_id = r.json()["id"]
    # Update
    r2 = client.patch(
        f"/api/role-permissions/roles/{role_id}",
        json={"permissions": ["jobs.read_all", "jobs.write"]},
    )
    assert r2.status_code == 200
    # Delete
    r3 = client.delete(f"/api/role-permissions/roles/{role_id}")
    assert r3.status_code == 204

    from sqlalchemy.orm import sessionmaker as _sm
    Sess = _sm(bind=Session, autoflush=False, autocommit=False)
    with Sess() as db:
        events = db.query(AuditLog).filter(AuditLog.entity_id == role_id).order_by(AuditLog.created_at).all()
    actions = [e.action for e in events]
    assert actions == ["role_created", "role_updated", "role_deleted"]
    created, updated, deleted = events
    assert created.details["before_permissions"] is None
    assert "jobs.read_all" in created.details["after_permissions"]
    assert "jobs.read_all" in updated.details["before_permissions"]
    assert "jobs.write" in updated.details["after_permissions"]
    assert "jobs.write" in deleted.details["before_permissions"]
    assert deleted.details["after_permissions"] is None
    # IP is captured (TestClient uses 'testclient' as host)
    for e in events:
        assert e.ip_address  # non-empty


def test_permission_audit_endpoint_filters_and_caps(client):
    """Slice 4.4 — /api/admin/permission-audit returns only role_* actions."""
    # Generate a few audit-worthy events
    r = client.post(
        "/api/role-permissions/roles",
        json={"name": "Auditable", "permissions": ["jobs.read_all"]},
    )
    role_id = r.json()["id"]
    client.patch(
        f"/api/role-permissions/roles/{role_id}",
        json={"permissions": ["jobs.read_all", "jobs.write"]},
    )

    feed = client.get("/api/admin/permission-audit?limit=50")
    assert feed.status_code == 200, feed.text
    body = feed.json()
    assert isinstance(body, list)
    # Every returned row's action starts with role_
    assert all(row["action"].startswith("role_") for row in body)
    # And we see our two actions
    actions = {row["action"] for row in body}
    assert "role_created" in actions
    assert "role_updated" in actions
    # before/after permissions present in details
    update_row = next(r for r in body if r["action"] == "role_updated")
    assert "before_permissions" in update_row["details"]
    assert "after_permissions" in update_row["details"]

    # Cap: limit > 500 still returns ≤ 500
    capped = client.get("/api/admin/permission-audit?limit=99999")
    assert capped.status_code == 200
    assert len(capped.json()) <= 500


def test_permission_audit_endpoint_registered():
    """Slice 4.4 — endpoint is wired through the router (gate is settings.write)."""
    from gdx_dispatch.routers.role_permissions import router as rp_router
    paths = {getattr(r, "path", "") for r in rp_router.routes}
    assert "/api/admin/permission-audit" in paths
    audit_route = next(r for r in rp_router.routes if getattr(r, "path", "") == "/api/admin/permission-audit")
    # Dependency uses require_permission("settings.write") at handler level
    # (the gate is the `_` Depends in the signature, not router-level deps).
    handler = audit_route.endpoint
    assert handler.__name__ == "list_permission_audit"


def test_rate_limit_blocks_second_write_within_window(client, monkeypatch):
    """Slice 4.2 — second privileged write within 1s gets 429 with Retry-After."""
    from gdx_dispatch.routers import role_permissions as rp_mod

    # Stub the rate limiter so we don't depend on Redis. First call OK,
    # subsequent calls False (rate limit hit).
    calls = {"n": 0}

    class _Stub:
        async def check(self, *_a, **_k):
            calls["n"] += 1
            return calls["n"] <= 1

    monkeypatch.setattr(
        "gdx_dispatch.core.rate_limiter.rate_limiter", _Stub(), raising=True
    )
    # Re-import path target inside the function — patch module attribute too
    monkeypatch.setattr(rp_mod, "_privileged_write_rate_limit", rp_mod._privileged_write_rate_limit)

    r1 = client.post("/api/role-permissions/roles", json={"name": "R1", "permissions": []})
    assert r1.status_code in (201, 409)
    r2 = client.post("/api/role-permissions/roles", json={"name": "R2", "permissions": []})
    assert r2.status_code == 429, r2.text
    assert r2.headers.get("retry-after") == "1"


def test_rate_limit_fail_open_on_redis_outage(client, monkeypatch, caplog):
    """Slice 4.2 — Redis outage logs WARN but does not 5xx the request."""
    import logging

    class _Broken:
        async def check(self, *_a, **_k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(
        "gdx_dispatch.core.rate_limiter.rate_limiter", _Broken(), raising=True
    )
    with caplog.at_level(logging.WARNING, logger="gdx_dispatch.routers.role_permissions"):
        r = client.post("/api/role-permissions/roles", json={"name": "FailOpen", "permissions": []})
    assert r.status_code in (201, 409), r.text
    assert any("rate_limit_unavailable" in m for m in caplog.messages)


def test_tenant_scope():
    tc_a = _make_client("tenant-a")
    tc_b = _make_client("tenant-b")
    try:
        # Seed builtins in both
        roles_a = tc_a.get("/api/role-permissions/roles").json()
        tc_b.get("/api/role-permissions/roles").json()

        # Create a custom role in A
        tc_a.post(
            "/api/role-permissions/roles",
            json={"name": "Secret A", "permissions": ["jobs.read_own"]},
        )

        names_a = {r["name"] for r in tc_a.get("/api/role-permissions/roles").json()}
        names_b = {r["name"] for r in tc_b.get("/api/role-permissions/roles").json()}
        assert "Secret A" in names_a
        assert "Secret A" not in names_b

        # Assign a role in A, confirm B doesn't see it
        admin_a = next(r for r in roles_a if r["name"] == "admin")
        tc_a.post(
            "/api/role-permissions/users/shared-user/roles",
            json={"role_id": admin_a["id"]},
        )
        a_list = tc_a.get("/api/role-permissions/users/shared-user/roles").json()
        b_list = tc_b.get("/api/role-permissions/users/shared-user/roles").json()
        assert len(a_list) == 1
        assert b_list == []
    finally:
        tc_a.app.dependency_overrides.clear()
        tc_a._engine.dispose()
        tc_b.app.dependency_overrides.clear()
        tc_b._engine.dispose()
