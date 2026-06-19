"""SS-32 slice F tests — /api/admin/spiffe router."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.core.spiffe.spire_trust_bundle import TrustBundleCache
from gdx_dispatch.core.spiffe.workload_capability_map import WorkloadCapabilityMap
from gdx_dispatch.routers.spiffe_admin import router

TD = "example.com"


def _bundle():
    return TrustBundleCache(
        endpoint="stub",
        fetcher=lambda _ep: {
            TD: {"x509_authorities": ["PEM1"], "jwt_authorities": [{"kid": "k1"}]}
        },
    )


def _caps():
    return WorkloadCapabilityMap.from_dict({"entries": []})


def _app(bundle=None, caps=None, *, role="super-admin", actor="u-super", broken=False):
    app = FastAPI()
    bundle = bundle or _bundle()
    if broken:
        bundle = TrustBundleCache(
            endpoint="x",
            fetcher=lambda _ep: (_ for _ in ()).throw(RuntimeError("down")),
        )
    caps = caps if caps is not None else _caps()
    sink = []

    @app.middleware("http")
    async def inject(request: Request, call_next):
        request.state.spiffe_bundle = bundle
        request.state.spiffe_capability_map = caps
        request.state.spiffe_event_sink = sink
        return await call_next(request)

    def _fake_principal():
        # ``actor`` is a short string handle in the old tests; stash it on
        # identity_id so the router's ``str(principal.identity_id)`` surfaces
        # it into emitted events unchanged.
        return SimpleNamespace(
            identity_id=actor,
            tenant_id="t1",
            principal_role=role,
            capabilities=(),
            is_super_admin=False,
            spiffe_id=None,
        )

    app.dependency_overrides[get_current_principal] = _fake_principal
    app.include_router(router)
    app.state._sink = sink
    app.state._bundle = bundle
    app.state._caps = caps
    return app


def _client(**kw):
    app = _app(**kw)
    return TestClient(app), app


def test_trust_bundle_requires_super_admin():
    c, _ = _client(role="admin")
    assert c.get("/api/admin/spiffe/trust-bundle").status_code == 403


def test_trust_bundle_snapshot_before_first_fetch():
    c, _ = _client()
    r = c.get("/api/admin/spiffe/trust-bundle")
    assert r.status_code == 200
    assert r.json()["cached"] is False


def test_trust_bundle_refresh_happy_path():
    c, app = _client()
    r = c.post("/api/admin/spiffe/trust-bundle/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["snapshot"]["cached"] is True
    # event emitted
    assert any(
        e["event"] == "gdx_dispatch.spiffe.trust_bundle_refreshed.v1"
        for e in app.state._sink
    )


def test_trust_bundle_refresh_propagates_failure_as_502():
    c, _ = _client(broken=True)
    r = c.post("/api/admin/spiffe/trust-bundle/refresh")
    assert r.status_code == 502


def test_list_workloads_empty():
    c, _ = _client()
    r = c.get("/api/admin/spiffe/workloads")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "workloads": []}


def test_register_workload_happy_path():
    c, app = _client()
    r = c.post(
        "/api/admin/spiffe/workloads",
        json={
            "spiffe_id_glob": "spiffe://example.com/agent/**",
            "capabilities": ["mcp:invoke"],
            "tenant_scope": "per-tenant",
            "metadata": {"note": "test"},
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["ok"] is True
    assert body["grant"]["spiffe_id_glob"] == "spiffe://example.com/agent/**"
    assert body["grant"]["capabilities"] == ["mcp:invoke"]
    # overlay actually took effect
    assert app.state._caps.resolve(
        "spiffe://example.com/agent/w1"
    ).capabilities == ("mcp:invoke",)
    # event emitted
    assert any(
        e["event"] == "gdx_dispatch.spiffe.workload_registered.v1"
        for e in app.state._sink
    )


def test_register_workload_rejects_bad_glob():
    c, _ = _client()
    r = c.post(
        "/api/admin/spiffe/workloads",
        json={
            "spiffe_id_glob": "not-a-uri",
            "capabilities": ["x"],
        },
    )
    assert r.status_code == 400


def test_register_workload_rejects_bad_caps():
    c, _ = _client()
    r = c.post(
        "/api/admin/spiffe/workloads",
        json={
            "spiffe_id_glob": "spiffe://example.com/agent/**",
            "capabilities": [1, 2],
        },
    )
    assert r.status_code == 400


def test_register_workload_rejects_bad_scope():
    c, _ = _client()
    r = c.post(
        "/api/admin/spiffe/workloads",
        json={
            "spiffe_id_glob": "spiffe://example.com/agent/**",
            "capabilities": ["x"],
            "tenant_scope": "weird",
        },
    )
    assert r.status_code == 400


def test_register_requires_super_admin():
    c, _ = _client(role="admin")
    r = c.post(
        "/api/admin/spiffe/workloads",
        json={
            "spiffe_id_glob": "spiffe://example.com/agent/**",
            "capabilities": ["x"],
        },
    )
    assert r.status_code == 403


def test_list_shows_registered_entry_afterward():
    c, _ = _client()
    c.post(
        "/api/admin/spiffe/workloads",
        json={
            "spiffe_id_glob": "spiffe://example.com/system/drain",
            "capabilities": ["event:drain"],
            "tenant_scope": "global",
        },
    )
    r = c.get("/api/admin/spiffe/workloads")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["workloads"][0]["tenant_scope"] == "global"
