"""SS-13 Slice A — pytest coverage for ``GET /api/me/tenants``.

Covers:

* empty memberships → ``[]``
* one active membership → one row
* two active + one revoked → exactly the two active rows (revoked filtered)
* missing auth override → 401/403 (the default ``get_current_user`` refuses
  unauthenticated requests)
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers import me as me_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.factories import (
    make_capability_set,
    make_identity,
    make_membership,
    make_tenant,
)


def _build_app(control_db, user_dict: dict | None):
    """Build an isolated FastAPI app with just the /api/me router + overrides.

    Passing ``user_dict=None`` skips the ``get_current_user`` override so the
    default OAuth2 bearer dependency runs and rejects the unauthenticated
    request.
    """
    app = FastAPI()
    app.include_router(me_router.router)
    app.dependency_overrides[get_db] = lambda: control_db
    if user_dict is not None:
        app.dependency_overrides[get_current_user] = lambda: user_dict
    return app


def _fake_user(identity_id: str) -> dict:
    return {
        "user_id": identity_id,
        "sub": identity_id,
        "role": "user",
        "tenant_id": "",
    }


def test_me_tenants_empty_no_memberships(control_db):
    identity = make_identity(control_db, email="empty@example.com")
    control_db.commit()

    app = _build_app(control_db, _fake_user(str(identity.id)))
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/me/tenants")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_me_tenants_single_membership(control_db):
    identity = make_identity(control_db, email="single@example.com")
    tenant = make_tenant(control_db, slug="me-t-single", name="Single Co")
    capset = make_capability_set(control_db, name="me-capset-single")
    make_membership(
        control_db,
        identity=identity,
        tenant=tenant,
        capability_set=capset,
        role="owner",
    )
    control_db.commit()

    app = _build_app(control_db, _fake_user(str(identity.id)))
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/me/tenants")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {"slug": "me-t-single", "name": "Single Co", "role": "owner"}
    ]


def test_me_tenants_multiple_memberships_and_revoked_filtered(control_db):
    identity = make_identity(control_db, email="multi@example.com")
    tenant_a = make_tenant(control_db, slug="me-t-multi-a", name="Multi A")
    tenant_b = make_tenant(control_db, slug="me-t-multi-b", name="Multi B")
    tenant_c = make_tenant(control_db, slug="me-t-multi-c", name="Multi C (revoked)")
    capset = make_capability_set(control_db, name="me-capset-multi")

    make_membership(
        control_db,
        identity=identity,
        tenant=tenant_a,
        capability_set=capset,
        role="owner",
    )
    make_membership(
        control_db,
        identity=identity,
        tenant=tenant_b,
        capability_set=capset,
        role="admin",
    )
    make_membership(
        control_db,
        identity=identity,
        tenant=tenant_c,
        capability_set=capset,
        role="tech",
        revoked_at=datetime.now(timezone.utc),
    )
    control_db.commit()

    app = _build_app(control_db, _fake_user(str(identity.id)))
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/me/tenants")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    slugs = {row["slug"] for row in body}
    assert slugs == {"me-t-multi-a", "me-t-multi-b"}, body
    assert "me-t-multi-c" not in slugs
    for row in body:
        assert set(row.keys()) == {"slug", "name", "role"}
    by_slug = {row["slug"]: row for row in body}
    assert by_slug["me-t-multi-a"]["role"] == "owner"
    assert by_slug["me-t-multi-a"]["name"] == "Multi A"
    assert by_slug["me-t-multi-b"]["role"] == "admin"
    assert by_slug["me-t-multi-b"]["name"] == "Multi B"


def test_me_tenants_requires_auth(control_db):
    """Without the ``get_current_user`` override, the OAuth2 bearer dep must refuse."""
    app = _build_app(control_db, user_dict=None)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/me/tenants")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code in (401, 403), (resp.status_code, resp.text)
