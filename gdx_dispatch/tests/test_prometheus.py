"""Tests for the Prometheus metrics endpoint and middleware."""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from gdx_dispatch.core.prometheus import (
    UNMATCHED_ENDPOINT,
    _endpoint_label,
    prometheus_middleware,
    router,
    track_db_query,
)


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
    # Make a request first to populate metrics. The x-tenant-id header is
    # multi-tenant residue and must NOT drive the label any more.
    client.get("/api/test", headers={"x-tenant-id": "tenant-1"})

    resp = client.get("/metrics", headers={"x-metrics-token": "test-secret"})
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
    assert "tenant-1" not in resp.text
    assert 'tenant_id="-"' in resp.text


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


# ---------------------------------------------------------------------------
# Label cardinality (#597) — the registry is in-memory, never evicted, and
# reset only by a redeploy, so a label value driven by untrusted input is an
# unbounded memory leak driven by whoever wants to drive it.
# ---------------------------------------------------------------------------

@pytest.fixture()
def routed_client(monkeypatch, tmp_path) -> TestClient:
    """An app shaped like the REAL one, which is the whole point of the fixture.

    An earlier version claimed "parameterised routes and a SPA catch-all" and
    registered no catch-all — so it tested a shape production does not have.
    In prod, junk URLs match `/{full_path:path}` and never reach the sentinel.
    It also needs a StaticFiles mount and a plain Starlette Route, because only
    FastAPI's APIRoute sets `scope["route"]`: without them the label helper's
    mount and plain-route branches have no coverage at all.
    """
    monkeypatch.setenv("METRICS_TOKEN", "test-secret")
    (tmp_path / "index-abc123.js").write_text("console.log(1)")

    app = FastAPI()
    app.middleware("http")(prometheus_middleware)
    app.include_router(router)

    @app.get("/api/jobs/{job_id}")
    def one_job(job_id: str):
        return {"id": job_id}

    @app.get("/api/jobs")
    def jobs():
        return []

    async def plain_route(request):
        return PlainTextResponse("mcp")

    app.router.routes.append(Route("/mcp", plain_route))
    app.router.routes.append(Mount("/assets", StaticFiles(directory=str(tmp_path))))

    # Registered LAST, exactly like app.py:1946 — it matches anything left.
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        return {"spa": full_path}

    return TestClient(app)


def _endpoint_labels(client: TestClient) -> set[str]:
    body = client.get("/metrics", headers={"x-metrics-token": "test-secret"}).text
    out = set()
    for line in body.splitlines():
        if line.startswith("http_requests_total") and 'endpoint="' in line:
            out.add(line.split('endpoint="', 1)[1].split('"', 1)[0])
    return out


def test_a_parameterised_route_reports_its_template_not_the_id(routed_client) -> None:
    """`/api/jobs/{job_id}`, once — not one series per job id.

    The old normalizer only collapsed a segment longer than 20 characters or
    shaped like a uuid, so a short id (`/api/jobs/42`) minted its own series.
    """
    for job_id in ("42", "abc", "00000000-0000-0000-0000-000000000000", "x" * 40):
        routed_client.get(f"/api/jobs/{job_id}")

    labels = _endpoint_labels(routed_client)
    assert "/api/jobs/{job_id}" in labels
    assert "/api/jobs/42" not in labels
    assert "/api/jobs/abc" not in labels


def test_unmatched_paths_collapse_to_one_series(routed_client) -> None:
    """THE #597 regression. Scanner traffic is unauthenticated and endless.

    Measured on 24h of real production paths (2026-09-10): 1,075 distinct paths
    produced 770 distinct label values under the old normalizer, 257 of them
    probes like `/.git/config`. Here 60 junk paths must produce ONE label.

    That label is the SPA catch-all's template, NOT the sentinel — in this app
    the catch-all matches first, which is what production actually does. The
    assertion is on the property (one bucket, no per-URL series), not on which
    bucket, because asserting the sentinel would have quietly tested a shape
    prod does not have.
    """
    probes = [
        "/.git/config", "/.git/HEAD", "/.aws/credentials", "/1.php",
        "/wp-admin/setup-config.php", "/xmlrpc.php", "/vendor/phpunit/phpunit",
        "/cgi-bin/luci", "/actuator/health", "/../../../../etc/passwd",
    ]
    probes += [f"/scan-{i}" for i in range(50)]
    before = _endpoint_labels(routed_client)
    for p in probes:
        routed_client.get(p)

    labels = _endpoint_labels(routed_client)
    assert len(labels - before) <= 1, f"{len(probes)} junk paths minted {len(labels - before)} series"
    leaked = {lab for lab in labels if "scan-" in lab or "php" in lab or "git" in lab}
    assert leaked == set(), f"junk paths still mint their own series: {sorted(leaked)[:5]}"


def test_a_static_asset_is_not_pooled_with_scanner_junk(routed_client) -> None:
    """Only FastAPI's APIRoute sets `scope["route"]`; a StaticFiles Mount does
    not. Reading route alone labelled every asset `<unmatched>`, putting real
    200s in the same bucket as 404 noise — bounded, but useless.
    """
    routed_client.get("/assets/index-abc123.js")
    routed_client.get("/assets/does-not-exist.js")

    labels = _endpoint_labels(routed_client)
    assert "/assets/*" in labels
    assert "/assets/index-abc123.js" not in labels   # …and not one per filename


def test_a_plain_starlette_route_gets_its_own_bucket(routed_client) -> None:
    """`/mcp` is a deliberately plain Starlette Route (mcp_mount.py), so it
    sets no `route` either. It must not fall in with the unmatched junk."""
    routed_client.get("/mcp")
    labels = _endpoint_labels(routed_client)
    assert any(lab.startswith("<route:") for lab in labels), labels
    assert UNMATCHED_ENDPOINT not in labels


def test_the_label_set_is_bounded_by_the_route_table(routed_client) -> None:
    """Cardinality must not grow with traffic — that is the whole property.

    200 distinct never-seen URLs must add no label beyond the sentinel.
    """
    routed_client.get("/api/jobs")
    before = _endpoint_labels(routed_client)

    for i in range(200):
        routed_client.get(f"/never-registered/{i}/{i * 7}")

    after = _endpoint_labels(routed_client)
    assert len(after) <= len(before) + 1, sorted(after - before)[:5]


def test_the_helper_falls_back_when_no_route_matched() -> None:
    """Unit-level: no `route` in scope must never raise, and never echo the path."""
    from starlette.requests import Request

    def _req(scope_extra: dict) -> Request:
        return Request({"type": "http", "method": "GET", "path": "/whatever",
                        "headers": [], **scope_extra})

    assert _endpoint_label(_req({})) == UNMATCHED_ENDPOINT
    assert _endpoint_label(_req({"route": None})) == UNMATCHED_ENDPOINT
    assert _endpoint_label(_req({"route": object()})) == UNMATCHED_ENDPOINT

    class _R:
        path = "/api/jobs/{job_id}"

    assert _endpoint_label(_req({"route": _R()})) == "/api/jobs/{job_id}"
