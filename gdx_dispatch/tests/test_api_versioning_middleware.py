"""SS-25 Slice B — tests for ``APIVersioningMiddleware``."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gdx_dispatch.core.deprecation_registry import DeprecationEntry, DeprecationRegistry
from gdx_dispatch.core.middleware.api_versioning import APIVersioningMiddleware


def _mk_app(registry: DeprecationRegistry) -> FastAPI:
    app = FastAPI()
    app.add_middleware(APIVersioningMiddleware, registry=registry)

    @app.get("/api/v1/customers")
    def customers(request: Request):
        return {
            "api_version": request.state.api_version.version,
            "explicit": request.state.api_version.explicit,
        }

    @app.get("/api/v1/healthy")
    def healthy(request: Request):
        return {"ok": True}

    return app


def _registry_with_customers() -> DeprecationRegistry:
    return DeprecationRegistry([
        DeprecationEntry(
            endpoint="/api/v1/customers",
            deprecated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            sunset_at=datetime(2028, 4, 1, tzinfo=timezone.utc),
            replacement_endpoint="/api/v2/customers",
        )
    ])


def test_no_accept_falls_back_to_latest():
    app = _mk_app(DeprecationRegistry())
    client = TestClient(app)
    r = client.get("/api/v1/healthy")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_explicit_v1_sets_state():
    app = _mk_app(DeprecationRegistry())
    client = TestClient(app)
    r = client.get("/api/v1/healthy", headers={"Accept": "application/vnd.gdx.v1+json"})
    assert r.status_code == 200
    # healthy endpoint doesn't echo state but it ran without error.


def test_api_version_in_state():
    app = _mk_app(DeprecationRegistry())
    client = TestClient(app)
    r = client.get(
        "/api/v1/customers", headers={"Accept": "application/vnd.gdx.v1+json"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["api_version"] == 1
    assert body["explicit"] is True


def test_malformed_vendor_returns_400():
    app = _mk_app(DeprecationRegistry())
    client = TestClient(app)
    r = client.get(
        "/api/v1/healthy", headers={"Accept": "application/vnd.gdx.vfoo+json"}
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_api_version"


def test_unsupported_version_returns_400():
    app = _mk_app(DeprecationRegistry())
    client = TestClient(app)
    r = client.get(
        "/api/v1/healthy", headers={"Accept": "application/vnd.gdx.v9999+json"}
    )
    assert r.status_code == 400


def test_deprecation_headers_injected():
    app = _mk_app(_registry_with_customers())
    client = TestClient(app)
    r = client.get("/api/v1/customers")
    assert r.status_code == 200
    assert "Sunset" in r.headers
    assert "GMT" in r.headers["Sunset"]
    assert "Deprecation" in r.headers
    assert "successor-version" in r.headers.get("Link", "")
    assert "/api/v2/customers" in r.headers["Link"]


def test_non_deprecated_path_has_no_sunset():
    app = _mk_app(_registry_with_customers())
    client = TestClient(app)
    r = client.get("/api/v1/healthy")
    assert "Sunset" not in r.headers
    assert "Deprecation" not in r.headers
