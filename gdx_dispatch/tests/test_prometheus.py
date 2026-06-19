"""Tests for the Prometheus metrics endpoint and middleware."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.prometheus import prometheus_middleware, router, track_db_query


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("METRICS_TOKEN", "test-secret")

    app = FastAPI()
    app.middleware("http")(prometheus_middleware)
    app.include_router(router)

    @app.get("/api/test")
    def test_endpoint():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(app)


def test_metrics_endpoint_returns_prometheus_format(client: TestClient) -> None:
    # Make a request first to populate metrics
    client.get("/api/test", headers={"x-tenant-id": "tenant-1"})

    resp = client.get("/metrics", headers={"x-metrics-token": "test-secret"})
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
    assert "tenant-1" in resp.text


def test_metrics_endpoint_rejects_bad_token(client: TestClient) -> None:
    resp = client.get("/metrics", headers={"x-metrics-token": "wrong"})
    assert resp.status_code == 401


def test_metrics_skips_health_and_metrics_paths(client: TestClient) -> None:
    client.get("/health")
    resp = client.get("/metrics", headers={"x-metrics-token": "test-secret"})
    assert resp.status_code == 200
    # /health should not appear in metrics (it's in SKIP_PATHS)
    assert 'endpoint="/health"' not in resp.text


def test_track_db_query_context_manager() -> None:
    with track_db_query("select"):
        pass  # Just verify it doesn't crash


def test_request_duration_tracked(client: TestClient) -> None:
    client.get("/api/test")
    resp = client.get("/metrics", headers={"x-metrics-token": "test-secret"})
    assert "http_request_duration_seconds" in resp.text
