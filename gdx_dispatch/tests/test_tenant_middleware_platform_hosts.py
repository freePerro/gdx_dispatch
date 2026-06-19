"""TenantMiddleware single-tenant pin behavior.

GDXDispatch is single-tenant (genesis collapse, Phase A): TenantMiddleware
no longer resolves a tenant from the subdomain / ``x-tenant-id`` header
against a control plane. Every request — regardless of Host — is pinned to
the one tenant, and ``request.state.tenant`` is always set. The old
multi-tenant contract (platform-host bypass, unknown-tenant 404, trial 402)
has been removed; these tests lock in the replacement.
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gdx_dispatch.core.tenant import TenantMiddleware, single_tenant


class _StubSession:
    def close(self):
        pass


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TenantMiddleware)

    @app.get("/api/me/tenants")
    def me_tenants(request: Request):
        return {"tenant_id": request.state.tenant["id"]}

    @app.get("/api/jobs")
    def jobs(request: Request):
        return {"tenant": request.state.tenant}

    return app


def test_middleware_never_queries_control_plane():
    """Single-tenant: the middleware must not look a tenant up — any host."""
    app = _build_app()
    with patch(
        "gdx_dispatch.core.tenant._lookup_tenant",
        side_effect=AssertionError("single-tenant middleware must not query tenants"),
    ):
        with TestClient(app) as client:
            for host in ("app.example.com", "gdx.example.com", "localhost"):
                r = client.get("/api/me/tenants", headers={"Host": host})
                assert r.status_code == 200, r.text
                assert r.json() == {"tenant_id": single_tenant()["id"]}


def test_every_request_gets_the_pinned_tenant_on_state():
    """``request.state.tenant`` is always the single tenant, for every route."""
    app = _build_app()
    with TestClient(app) as client:
        r = client.get("/api/jobs", headers={"Host": "anything.example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["tenant"] == single_tenant()


def test_unknown_host_no_longer_404s():
    """The removed multi-tenant path 404'd unknown tenants; now it pins one."""
    app = _build_app()
    with TestClient(app) as client:
        r = client.get("/api/me/tenants", headers={"Host": "unknown.example.com"})
    assert r.status_code == 200, r.text
    assert r.json() == {"tenant_id": single_tenant()["id"]}
