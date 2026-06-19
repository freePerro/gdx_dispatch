"""Load testing for GDX FastAPI endpoints.

Uses httpx directly (no external dependency on locust) so it can run in
the standard pytest suite.  Marked @pytest.mark.load so it is skipped
during normal unit-test runs.

Usage:
    GDX_BASE_URL=https://dev.example.com \
    GDX_E2E_EMAIL=admin@test.com GDX_E2E_PASSWORD=<pw> \
    pytest gdx_dispatch/tests/test_36_load_test.py -v -m load
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
from dataclasses import dataclass, field

import httpx
import pytest

BASE_URL = os.getenv("GDX_BASE_URL", "https://dev.example.com")
E2E_EMAIL = os.getenv("GDX_E2E_EMAIL", "admin@example.com")
E2E_PASSWORD = os.getenv("GDX_E2E_PASSWORD", "")
TENANT_ID = os.getenv("GDX_TENANT_ID", "886a5b78-6bff-4b19-823c-a2c16684447e")

CONCURRENT_USERS = int(os.getenv("GDX_LOAD_USERS", "10"))
DURATION_SECONDS = int(os.getenv("GDX_LOAD_DURATION", "30"))

ENDPOINTS = [
    ("/health", 1),
    ("/api/dashboard/stats", 3),
    ("/api/jobs", 5),
    ("/api/customers", 3),
    ("/api/estimates", 2),
    ("/api/invoices", 2),
]


@dataclass
class EndpointResult:
    url: str
    status: int
    latency_ms: float
    error: str | None = None


@dataclass
class LoadTestReport:
    total_requests: int = 0
    total_errors: int = 0
    latencies: dict[str, list[float]] = field(default_factory=dict)
    status_codes: dict[str, dict[int, int]] = field(default_factory=dict)

    def record(self, result: EndpointResult) -> None:
        self.total_requests += 1
        if result.error or result.status >= 500:
            self.total_errors += 1
        self.latencies.setdefault(result.url, []).append(result.latency_ms)
        self.status_codes.setdefault(result.url, {})
        self.status_codes[result.url][result.status] = (
            self.status_codes[result.url].get(result.status, 0) + 1
        )

    @property
    def error_rate_pct(self) -> float:
        return (self.total_errors / max(self.total_requests, 1)) * 100

    def p95(self, url: str) -> float:
        lat = sorted(self.latencies.get(url, [0]))
        idx = int(len(lat) * 0.95)
        return lat[min(idx, len(lat) - 1)]

    def summary(self) -> str:
        lines = [
            f"Total requests: {self.total_requests}",
            f"Total errors:   {self.total_errors} ({self.error_rate_pct:.1f}%)",
            "",
        ]
        for url in sorted(self.latencies):
            lat = self.latencies[url]
            lines.append(
                f"  {url:30s}  reqs={len(lat):4d}  "
                f"avg={statistics.mean(lat):.0f}ms  "
                f"p95={self.p95(url):.0f}ms  "
                f"max={max(lat):.0f}ms"
            )
        return "\n".join(lines)


async def _login(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/api/auth/login",
        json={"email": E2E_EMAIL, "password": E2E_PASSWORD},
        headers={"x-tenant-id": TENANT_ID, "Content-Type": "application/json"},
    )
    data = resp.json()
    return data.get("access_token") or data.get("token") or ""


async def _worker(
    client: httpx.AsyncClient,
    token: str,
    report: LoadTestReport,
    stop_event: asyncio.Event,
    weighted_endpoints: list[str],
) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "x-tenant-id": TENANT_ID,
    }
    idx = 0
    while not stop_event.is_set():
        url = weighted_endpoints[idx % len(weighted_endpoints)]
        idx += 1
        t0 = time.monotonic()
        try:
            resp = await client.get(url, headers=headers)
            latency = (time.monotonic() - t0) * 1000
            report.record(EndpointResult(url=url, status=resp.status_code, latency_ms=latency))
        except Exception as exc:
            latency = (time.monotonic() - t0) * 1000
            report.record(
                EndpointResult(url=url, status=0, latency_ms=latency, error=str(exc))
            )
        # Small jitter between requests
        await asyncio.sleep(0.1)


@pytest.mark.load
@pytest.mark.anyio
async def test_load_performance() -> None:
    """Run concurrent load and assert p95 < 2s, error rate < 5%."""
    # Build weighted endpoint list
    weighted: list[str] = []
    for url, weight in ENDPOINTS:
        weighted.extend([url] * weight)

    report = LoadTestReport()

    async with httpx.AsyncClient(
        base_url=BASE_URL, verify=False, timeout=15
    ) as client:
        token = await _login(client)
        assert token, "Login failed — cannot run load test"

        stop = asyncio.Event()
        workers = [
            asyncio.create_task(_worker(client, token, report, stop, weighted))
            for _ in range(CONCURRENT_USERS)
        ]

        await asyncio.sleep(DURATION_SECONDS)
        stop.set()
        await asyncio.gather(*workers, return_exceptions=True)

    print("\n" + report.summary())

    # Assertions
    assert report.total_requests > 0, "No requests were made"
    assert report.error_rate_pct < 5, f"Error rate {report.error_rate_pct:.1f}% >= 5%"

    for url, _weight in ENDPOINTS:
        assert url in report.latencies, f"{url} was never called"
        p95 = report.p95(url)
        assert p95 < 2000, f"{url} p95 latency {p95:.0f}ms >= 2000ms"
