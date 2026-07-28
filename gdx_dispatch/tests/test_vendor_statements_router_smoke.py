"""Every vendor-statements endpoint is actually callable.

This file exists because of a real production break. A merge dropped an import
line from the router, and `/api/vendor-statements/on-order` shipped to main
raising NameError on its first call. Nothing caught it:

- ``create_app()`` succeeded — Python resolves names in a function BODY when the
  function runs, not when the module imports, so a missing import is invisible
  to an import smoke test.
- The unit tests exercised ``build_on_order`` and friends directly, never
  through the route, so the router's own namespace was never executed.

Both prior PRs named that second gap in their own Test Gap section and shipped
anyway. Calling each endpoint once closes it: the assertion is only "this does
not explode", which is exactly the class of bug that got through.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.routers.vendor_statements import router

TID = uuid4()


def _user():
    return {"user_id": str(uuid4()), "sub": str(uuid4()), "tenant_id": str(TID), "role": "admin"}


@pytest.fixture
def client(tenant_db, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = lambda: tenant_db
    # require_permission is a dependency FACTORY — each call makes a new
    # callable, so the override has to be keyed per permission string.
    for perm in ("vendor_statements.read", "vendor_statements.write"):
        app.dependency_overrides[require_permission(perm)] = lambda: None
    return TestClient(app, raise_server_exceptions=True)


@pytest.mark.parametrize("path", [
    "/api/vendor-statements",
    "/api/vendor-statements/accounts",
    "/api/vendor-statements/on-order",
])
def test_read_endpoints_respond_on_an_empty_database(client, path):
    """A NameError inside the handler surfaces as a 500 here — which is the
    whole point. An empty DB is enough; the bug is in the namespace, not the
    data."""
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_a_literal_path_is_not_swallowed_by_the_uuid_route(client):
    """`/accounts` and `/on-order` are declared before `/{statement_id}`. If
    that ordering regresses they parse as a UUID and 422 instead."""
    for path in ("/api/vendor-statements/accounts", "/api/vendor-statements/on-order"):
        assert client.get(path).status_code != 422


def test_job_suggestions_404s_for_an_unknown_order(client):
    """Exercises the suggestion handler's namespace without needing an order."""
    response = client.get(f"/api/vendor-statements/orders/{uuid4()}/job-suggestions")
    assert response.status_code == 404


def test_confirm_job_404s_for_an_unknown_order(client):
    response = client.post(
        f"/api/vendor-statements/orders/{uuid4()}/confirm-job",
        json={"job_id": str(uuid4())},
    )
    assert response.status_code == 404


def test_statement_detail_404s_for_an_unknown_id(client):
    assert client.get(f"/api/vendor-statements/{uuid4()}").status_code == 404


def test_every_route_in_this_router_has_been_reached_by_a_test():
    """Guards the guard: a new endpoint added without a smoke test here would
    reintroduce exactly the hole this file was written to close."""
    covered = {
        "/api/vendor-statements",
        "/api/vendor-statements/accounts",
        "/api/vendor-statements/on-order",
        "/api/vendor-statements/{statement_id}",
        "/api/vendor-statements/orders/{order_id}/job-suggestions",
        "/api/vendor-statements/orders/{order_id}/confirm-job",
        # Multipart upload and the line PATCH are covered by their own suites.
        "/api/vendor-statements/upload",
        "/api/vendor-statements/{statement_id}/lines/{line_id}",
    }
    actual = {r.path for r in router.routes}
    missing = actual - covered
    assert not missing, (
        f"new endpoint(s) with no smoke test: {sorted(missing)} — add one, or a "
        "missing import there will reach production as a NameError again"
    )
