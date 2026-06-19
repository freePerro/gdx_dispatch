"""Network request audit — discovers all API routes from OpenAPI spec and verifies none return 500.

Fetches /openapi.json, extracts all GET endpoints, and hits each one.
Any 500 response is a test failure.

Usage:
    GDX_BASE_URL=https://gdx.example.com \
    GDX_E2E_EMAIL=e2e_admin@example.com \
    GDX_E2E_PASSWORD=E2E_Test_2026! \
    pytest gdx_dispatch/tests/e2e/test_network_audit.py -v
"""
from __future__ import annotations

import logging

import pytest

from gdx_dispatch.tests.e2e.conftest import APIClient

pytestmark = [pytest.mark.e2e]

log = logging.getLogger(__name__)

# Path patterns that require path parameters — skip or substitute
# e.g. /api/jobs/{job_id} needs a real ID; we skip these
SKIP_PATTERNS = {"{", "/auth/", "/webhook", "/ws", "/openapi.json", "/docs", "/redoc"}


def _is_skippable(path: str) -> bool:
    """Return True if the path should be skipped (has path params, auth, websocket, etc.)."""
    return any(pat in path for pat in SKIP_PATTERNS)


class TestNetworkAudit:
    """Discover all routes from OpenAPI spec and verify no 500s."""

    def test_openapi_spec_is_reachable(self, api: APIClient):
        """The OpenAPI spec itself must be accessible."""
        resp = api.get("/openapi.json")
        assert resp.status_code == 200, (
            f"OpenAPI spec returned {resp.status_code}: {resp.text[:200]}"
        )
        data = resp.json()
        assert "paths" in data, "OpenAPI spec missing 'paths' key"
        assert len(data["paths"]) > 0, "OpenAPI spec has no paths"

    def test_no_500_on_any_get_endpoint(self, api: APIClient):
        """Every GET endpoint in the OpenAPI spec must not return 500."""
        resp = api.get("/openapi.json")
        if resp.status_code != 200:
            pytest.skip(f"OpenAPI spec unavailable: {resp.status_code}")

        spec = resp.json()
        paths = spec.get("paths", {})

        failures = []
        tested = 0
        skipped = 0

        for path, methods in paths.items():
            if "get" not in methods:
                continue
            if _is_skippable(path):
                skipped += 1
                continue

            tested += 1
            try:
                endpoint_resp = api.get(path)
                if endpoint_resp.status_code == 500:
                    body = endpoint_resp.text[:200] if endpoint_resp.text else "(empty)"
                    failures.append(f"GET {path} -> 500: {body}")
            except Exception as exc:
                failures.append(f"GET {path} -> exception: {exc}")

        log.info("Network audit: tested=%d, skipped=%d, failures=%d", tested, skipped, len(failures))

        assert not failures, (
            f"{len(failures)}/{tested} GET endpoint(s) returned 500:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )

    def test_api_prefix_endpoints_only(self, api: APIClient):
        """All /api/* GET endpoints must not return 500."""
        resp = api.get("/openapi.json")
        if resp.status_code != 200:
            pytest.skip(f"OpenAPI spec unavailable: {resp.status_code}")

        spec = resp.json()
        paths = spec.get("paths", {})

        failures = []
        tested = 0

        for path, methods in paths.items():
            if not path.startswith("/api/"):
                continue
            if "get" not in methods:
                continue
            if _is_skippable(path):
                continue

            tested += 1
            try:
                endpoint_resp = api.get(path)
                if endpoint_resp.status_code == 500:
                    body = endpoint_resp.text[:200] if endpoint_resp.text else "(empty)"
                    failures.append(f"GET {path} -> 500: {body}")
            except Exception as exc:
                failures.append(f"GET {path} -> exception: {exc}")

        assert tested > 0, "No /api/* GET endpoints found in OpenAPI spec"
        assert not failures, (
            f"{len(failures)}/{tested} /api/* endpoint(s) returned 500:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )
