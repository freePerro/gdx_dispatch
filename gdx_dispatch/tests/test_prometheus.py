"""Tests for the Prometheus metrics endpoint and middleware."""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI, HTTPException
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


# ── fail-closed gate (2026-09-04) ───────────────────────────────────────────
# Every test above sets METRICS_TOKEN, so the suite could never fail for the
# defect that mattered: with the token unset the endpoint served the whole
# registry to anyone. These are the counterfactual — they fail against the
# old `if metrics_token:` gate.


@pytest.fixture()
def unconfigured_client(monkeypatch) -> TestClient:
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_metrics_refuses_when_token_unset(unconfigured_client: TestClient) -> None:
    """No METRICS_TOKEN configured => 503, never the registry."""
    resp = unconfigured_client.get("/metrics")
    assert resp.status_code == 503
    # Distinguish this from the prometheus_client-missing 503, or the test
    # would pass for the wrong reason in an env without the library.
    assert "METRICS_TOKEN" in resp.json()["detail"]
    assert "http_requests_total" not in resp.text


def test_metrics_refuses_when_token_unset_even_if_caller_sends_one(
    unconfigured_client: TestClient,
) -> None:
    """A caller cannot talk the server into serving an unconfigured endpoint."""
    resp = unconfigured_client.get("/metrics", headers={"x-metrics-token": "anything"})
    assert resp.status_code == 503
    assert "METRICS_TOKEN" in resp.json()["detail"]
    assert "http_requests_total" not in resp.text


def test_metrics_rejects_empty_token_header(client: TestClient) -> None:
    """Configured token + absent header => 401, not a pass-through."""
    assert client.get("/metrics").status_code == 401


def test_metrics_non_ascii_token_header_is_401_not_500() -> None:
    """A non-ASCII token header must not crash the endpoint.

    ``hmac.compare_digest`` raises TypeError on str operands with non-ASCII,
    and Starlette latin-1-decodes header bytes. httpx refuses to *send* such a
    header, so this drives the ASGI app directly — the only way to reach it.
    """
    import asyncio

    from gdx_dispatch.core.prometheus import metrics_endpoint

    class _Req:
        def __init__(self, value: str) -> None:
            self.headers = {"x-metrics-token": value}

    os.environ["METRICS_TOKEN"] = "test-secret"
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(metrics_endpoint(_Req("caf\xe9")))  # latin-1 'café'
        assert exc.value.status_code == 401
    finally:
        os.environ.pop("METRICS_TOKEN", None)
