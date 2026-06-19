"""SS-25 Slice E — tests for ``/api/meta/*`` router."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.deprecation_registry import DeprecationEntry, DeprecationRegistry
from gdx_dispatch.routers import api_metadata


def _mk_app() -> FastAPI:
    app = FastAPI()
    app.include_router(api_metadata.router)
    return app


def test_versions_shape():
    client = TestClient(_mk_app())
    r = client.get("/api/meta/versions")
    assert r.status_code == 200
    data = r.json()
    assert data["supported"] == [1]
    assert data["latest"] == 1
    assert "vnd.gdx" in data["media_type_template"]


def test_deprecations_empty(monkeypatch):
    monkeypatch.setattr(api_metadata, "_registry", lambda:DeprecationRegistry())
    client = TestClient(_mk_app())
    r = client.get("/api/meta/deprecations")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0
    assert data["deprecations"] == []


def test_deprecations_populated(monkeypatch):
    reg = DeprecationRegistry([
        DeprecationEntry(
            endpoint="/api/v1/customers",
            deprecated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            sunset_at=datetime(2028, 4, 1, tzinfo=timezone.utc),
            replacement_endpoint="/api/v2/customers",
        )
    ])
    monkeypatch.setattr(api_metadata, "_registry", lambda:reg)
    client = TestClient(_mk_app())
    r = client.get("/api/meta/deprecations")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["deprecations"][0]
    assert row["endpoint"] == "/api/v1/customers"
    assert row["replacement_endpoint"] == "/api/v2/customers"
    assert "deprecated_at" in row and "sunset_at" in row
