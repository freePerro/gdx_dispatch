"""SS-13 Slice B — pytest coverage for ``GET /auth/login?return_to=auto``.

Covers the login-picker auto-redirect branch added to
``gdx_dispatch.routers.auth.gateway.login_scaffold``:

* zero memberships → 302 ``/signup``
* exactly one membership → 302 ``/t/<slug>/``
* two or more memberships → 302 ``/login-picker``
* ``return_to`` absent → SS-12A scaffold payload (contract preserved)
* ``return_to`` present but not ``auto`` → scaffold payload (contract preserved)
* unauthenticated call to ``?return_to=auto`` → 401/403 (default
  ``OAuth2PasswordBearer`` on ``get_current_user`` refuses the request)

The test app is a bare ``FastAPI()`` with only the auth_gateway router
mounted and ``dependency_overrides`` injected for ``get_db`` and
``get_current_user`` — the same pattern as ``test_me_tenants.py``. This
keeps the redirect branch independently verifiable without booting the
full ``create_app()`` lifespan (which is what the SS-12A scaffold file
explicitly avoids for replay-budget reasons).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import gateway as auth_gateway
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.factories import (
    make_capability_set,
    make_identity,
    make_membership,
    make_tenant,
)


def _build_app(control_db, user_dict: dict | None):
    app = FastAPI()
    app.include_router(auth_gateway.router)
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


def test_login_auto_zero_memberships_redirects_to_signup(control_db):
    identity = make_identity(control_db, email="auto-zero@example.com")
    control_db.commit()

    app = _build_app(control_db, _fake_user(str(identity.id)))
    try:
        with TestClient(
            app, raise_server_exceptions=True, follow_redirects=False
        ) as client:
            resp = client.get("/auth/login", params={"return_to": "auto"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"] == "/signup"


def test_login_auto_single_membership_redirects_to_tenant(control_db):
    identity = make_identity(control_db, email="auto-single@example.com")
    tenant = make_tenant(control_db, slug="gw-auto-single", name="Auto Single Co")
    capset = make_capability_set(control_db, name="gw-capset-single")
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
        with TestClient(
            app, raise_server_exceptions=True, follow_redirects=False
        ) as client:
            resp = client.get("/auth/login", params={"return_to": "auto"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"] == "/t/gw-auto-single/"


def test_login_auto_multi_membership_redirects_to_picker(control_db):
    identity = make_identity(control_db, email="auto-multi@example.com")
    tenant_a = make_tenant(control_db, slug="gw-auto-a", name="Auto A")
    tenant_b = make_tenant(control_db, slug="gw-auto-b", name="Auto B")
    tenant_c = make_tenant(control_db, slug="gw-auto-c", name="Auto C (revoked)")
    capset = make_capability_set(control_db, name="gw-capset-multi")
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
        with TestClient(
            app, raise_server_exceptions=True, follow_redirects=False
        ) as client:
            resp = client.get("/auth/login", params={"return_to": "auto"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"] == "/login-picker"


def test_login_without_return_to_preserves_scaffold_payload(control_db):
    identity = make_identity(control_db, email="auto-noparam@example.com")
    control_db.commit()

    app = _build_app(control_db, _fake_user(str(identity.id)))
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/auth/login")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == auth_gateway.SCAFFOLD_MARKER
    assert body["endpoint"] == "login"
    assert body["slice"] == "ss12-a"


def test_login_with_non_auto_return_to_preserves_scaffold_payload(control_db):
    identity = make_identity(control_db, email="auto-noop@example.com")
    control_db.commit()

    app = _build_app(control_db, _fake_user(str(identity.id)))
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/auth/login", params={"return_to": "somewhere"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == auth_gateway.SCAFFOLD_MARKER
    assert body["endpoint"] == "login"


def test_login_auto_requires_auth(control_db):
    """Without the ``get_current_user`` override, the default OAuth2 bearer
    dependency must reject the unauthenticated request before the auto
    branch can call ``list_my_tenants``.
    """
    app = _build_app(control_db, user_dict=None)
    try:
        with TestClient(
            app, raise_server_exceptions=False, follow_redirects=False
        ) as client:
            resp = client.get("/auth/login", params={"return_to": "auto"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code in (401, 403), (resp.status_code, resp.text)
