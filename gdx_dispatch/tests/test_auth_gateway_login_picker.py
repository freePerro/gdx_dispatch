"""SS-13 Slice B — pytest coverage for ``GET /auth/login?return_to=auto``.

Covers:

* 0 memberships → 302 redirect to ``/signup``
* 1 membership  → 302 redirect to ``/t/<slug>/``
* 2+ memberships → 302 redirect to ``/login-picker`` (revoked rows filtered
  by the Slice A ``list_my_tenants`` logic this handler delegates to)
* ``return_to`` absent → scaffold payload preserved (SS-12A contract)
* ``return_to`` set to a non-"auto" value → scaffold payload preserved
* Missing auth override → 401/403 (the default ``get_current_user`` bearer
  dep refuses unauthenticated callers on the auto path)

Uses the same isolated-``FastAPI()``-plus-``TestClient`` pattern as
``test_me_tenants.py`` so the scaffold file's "do not ``create_app()`` in
tests" precedence rule still holds. The scaffold's direct-coroutine
contract (``asyncio.run(login_scaffold())``) is separately exercised by
``test_auth_gateway_scaffold.py`` — this file deliberately covers the
HTTP path that the scaffold tests skipped.
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
    """Isolated FastAPI app with just the auth_gateway router + overrides.

    Passing ``user_dict=None`` skips the ``get_current_user`` override so the
    default OAuth2 bearer dependency runs and rejects the unauthenticated
    request — matching the ``test_me_tenants.py`` pattern verbatim.
    """
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


def test_login_return_to_auto_zero_tenants_redirects_to_signup(control_db):
    identity = make_identity(control_db, email="picker-zero@example.com")
    control_db.commit()

    app = _build_app(control_db, _fake_user(str(identity.id)))
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/auth/login",
                params={"return_to": "auto"},
                follow_redirects=False,
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 302, (resp.status_code, resp.text)
    assert resp.headers["location"] == "/signup"


def test_login_return_to_auto_single_tenant_redirects_to_subdomain(control_db):
    identity = make_identity(control_db, email="picker-single@example.com")
    tenant = make_tenant(control_db, slug="picker-t-single", name="Single Co")
    capset = make_capability_set(control_db, name="picker-capset-single")
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
            resp = client.get(
                "/auth/login",
                params={"return_to": "auto"},
                follow_redirects=False,
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 302, (resp.status_code, resp.text)
    assert resp.headers["location"] == "/t/picker-t-single/"


def test_login_return_to_auto_multiple_tenants_redirects_to_picker(control_db):
    identity = make_identity(control_db, email="picker-multi@example.com")
    tenant_a = make_tenant(control_db, slug="picker-t-multi-a", name="Multi A")
    tenant_b = make_tenant(control_db, slug="picker-t-multi-b", name="Multi B")
    tenant_c = make_tenant(
        control_db, slug="picker-t-multi-c", name="Multi C (revoked)"
    )
    capset = make_capability_set(control_db, name="picker-capset-multi")
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
    # Revoked membership must be filtered by the Slice A list_my_tenants logic
    # so the picker count is 2 (not 3) and we still hit the multi branch.
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
            resp = client.get(
                "/auth/login",
                params={"return_to": "auto"},
                follow_redirects=False,
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 302, (resp.status_code, resp.text)
    assert resp.headers["location"] == "/login-picker"


def test_login_without_return_to_preserves_scaffold_payload(control_db):
    """The SS-12A scaffold contract must survive the Slice B additions.

    The scaffold tests assert the marker via direct coroutine call; this
    test confirms the same contract is preserved when the handler is
    reached through the FastAPI request stack (query param absent).
    """
    identity = make_identity(control_db, email="picker-scaffold@example.com")
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


def test_login_return_to_other_value_preserves_scaffold_payload(control_db):
    """Only the literal string ``auto`` triggers the picker branch.

    A stray ``return_to`` value (e.g., a full URL from a future caller)
    must fall through to the scaffold payload so callers that predate
    Slice B keep the SS-12A marker contract.
    """
    identity = make_identity(control_db, email="picker-other@example.com")
    control_db.commit()

    app = _build_app(control_db, _fake_user(str(identity.id)))
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/auth/login",
                params={"return_to": "/dashboard"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == auth_gateway.SCAFFOLD_MARKER
    assert body["endpoint"] == "login"


def test_login_return_to_auto_requires_auth(control_db):
    """Without the ``get_current_user`` override, the bearer dep refuses.

    The auto branch needs an authenticated identity to look up memberships;
    the FastAPI request stack evaluates ``Depends(get_current_user)``
    before entering the handler, so the missing-token case surfaces as
    401/403 before ``list_my_tenants`` is ever called.
    """
    app = _build_app(control_db, user_dict=None)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(
                "/auth/login",
                params={"return_to": "auto"},
                follow_redirects=False,
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code in (401, 403), (resp.status_code, resp.text)
