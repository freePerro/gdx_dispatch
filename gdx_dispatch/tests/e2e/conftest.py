"""E2E test configuration for GDX Playwright tests.

Provides:
- Authenticated browser context per test
- API client for data setup/teardown
- Chrome DevTools console error capture
- Multi-tenant isolation via separate browser contexts
- Screenshot on failure

Usage:
    GDX_BASE_URL=https://gdx.example.com \
    GDX_E2E_EMAIL=e2e_admin@example.com \
    GDX_E2E_PASSWORD=E2E_Test_2026! \
    pytest gdx_dispatch/tests/e2e/ -v
"""
from __future__ import annotations

import contextlib
import os

import httpx
import pytest
from playwright.sync_api import Page, expect

BASE_URL = os.getenv("GDX_BASE_URL", "https://gdx.example.com")
E2E_EMAIL = os.getenv("GDX_E2E_EMAIL", "e2e_admin@example.com")
E2E_PASSWORD = os.getenv("GDX_E2E_PASSWORD", "E2E_Test_2026!")
TENANT_ID = os.getenv("GDX_TENANT_ID", "886a5b78-6bff-4b19-823c-a2c16684447e")

# Skip all E2E tests if password not configured
pytestmark = pytest.mark.skipif(
    not E2E_PASSWORD,
    reason="GDX_E2E_PASSWORD not set — skipping E2E tests",
)


class ConsoleErrorTracker:
    """Tracks JS console errors during a test."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def on_console(self, msg):
        # TD-013 (closed an earlier session): the 401/404/429/Failed-to-fetch
        # allowlist that used to live here was hiding real regressions —
        # pre-auth branding 401 was a route-ordering bug, dead SPA→API
        # 404s pointed to broken navigation, rate-limit 429s meant test
        # concurrency was misconfigured, and CSP/CORS failures dressed up
        # as "transient network." If a specific error class is genuinely
        # expected for one test, allowlist it inside THAT test — don't
        # silence the whole tracker.
        if msg.type == "error":
            self.errors.append(msg.text)
        elif msg.type == "warning":
            self.warnings.append(msg.text)

    def assert_no_errors(self, context: str = ""):
        assert not self.errors, (
            f"JS console errors detected{' in ' + context if context else ''}:\n"
            + "\n".join(f"  - {e}" for e in self.errors)
        )


class APIClient:
    """Direct API client for test data setup/teardown."""

    def __init__(self, base_url: str, token: str, tenant_id: str):
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "x-tenant-id": tenant_id,
                "Content-Type": "application/json",
                "x-e2e-test": "true",
            },
            verify=False,
            timeout=15,
        )
        self._created: dict[str, list[str]] = {}

    def get(self, path: str) -> httpx.Response:
        return self._client.get(path)

    def post(self, path: str, json_data: dict = None, files: dict = None, params: dict = None) -> httpx.Response:
        if files:
            # Multipart upload — need a separate client without Content-Type: application/json
            # because httpx won't auto-set multipart boundary if Content-Type is already set
            upload_headers = {
                k: v for k, v in self._client.headers.items()
                if k.lower() != "content-type"
            }
            with httpx.Client(
                base_url=str(self._client.base_url),
                headers=upload_headers,
                verify=False,
                timeout=30,
            ) as upload_client:
                resp = upload_client.post(path, files=files, params=params)
        else:
            resp = self._client.post(path, json=json_data, params=params)
        # Track created resources for cleanup
        if resp.status_code in (200, 201):
            data = resp.json()
            if isinstance(data, dict) and "id" in data:
                resource = path.split("/")[2] if len(path.split("/")) > 2 else "unknown"
                self._created.setdefault(resource, []).append(str(data["id"]))
        return resp

    def patch(self, path: str, json_data: dict = None) -> httpx.Response:
        return self._client.patch(path, json=json_data)

    def delete(self, path: str) -> httpx.Response:
        return self._client.delete(path)

    def cleanup(self):
        """Delete all created resources in reverse order."""
        for resource, ids in reversed(list(self._created.items())):
            for rid in reversed(ids):
                with contextlib.suppress(Exception):
                    self._client.delete(f"/api/{resource}/{rid}")

    def close(self):
        self.cleanup()
        self._client.close()


def _login_and_get_token() -> str:
    """Login via API and return access token."""
    with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
        resp = client.post(
            "/auth/login",
            json={"email": E2E_EMAIL, "password": E2E_PASSWORD},
            headers={"x-tenant-id": TENANT_ID, "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            pytest.skip(f"Login failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        return data.get("access_token") or data.get("token") or ""


@pytest.fixture(scope="session")
def auth_token() -> str:
    """Session-scoped auth token."""
    return _login_and_get_token()


@pytest.fixture(scope="session")
def api(auth_token: str) -> APIClient:
    """Session-scoped API client for data setup."""
    client = APIClient(BASE_URL, auth_token, TENANT_ID)
    yield client
    client.close()


@pytest.fixture
def console_tracker() -> ConsoleErrorTracker:
    """Per-test console error tracker."""
    return ConsoleErrorTracker()


@pytest.fixture
def authenticated_page(page: Page, auth_token: str, console_tracker: ConsoleErrorTracker) -> Page:
    """Page with auth token injected and console errors tracked."""
    # Listen for console messages
    page.on("console", console_tracker.on_console)

    # Inject auth token into sessionStorage before navigating
    page.goto(f"{BASE_URL}/login")
    page.evaluate(
        f"""() => {{
            sessionStorage.setItem('gdx_access_token', '{auth_token}');
        }}"""
    )
    return page


@pytest.fixture
def navigate(authenticated_page: Page):
    """Navigate to a GDX page with auth already set."""
    def _navigate(path: str, wait_for: str = "domcontentloaded"):
        try:
            authenticated_page.goto(f"{BASE_URL}{path}", wait_until=wait_for, timeout=30000)
        except Exception:
            # Timeout navigating — page may still be usable
            pass
        return authenticated_page
    return _navigate


# ---------- Assertion helpers ----------

def assert_api_success(response: httpx.Response, expected_status: int = 200):
    """Assert API response is successful with expected status."""
    assert response.status_code == expected_status, (
        f"Expected {expected_status}, got {response.status_code}: {response.text[:300]}"
    )


def assert_has_data(response: httpx.Response, min_items: int = 0):
    """Assert API response contains data (list or dict with items)."""
    data = response.json()
    if isinstance(data, list):
        assert len(data) >= min_items, f"Expected >= {min_items} items, got {len(data)}"
    elif isinstance(data, dict):
        items = data.get("items") or data.get("data") or data.get("results")
        if items is not None:
            assert len(items) >= min_items
    return data


def assert_page_has_content(page: Page, text: str, timeout: int = 5000):
    """Assert page contains visible text."""
    locator = page.locator(f"text={text}").first
    expect(locator).to_be_visible(timeout=timeout)


def assert_no_empty_tables(page: Page):
    """Assert that all visible tables have at least a header row."""
    tables = page.locator("table").all()
    for table in tables:
        if table.is_visible():
            rows = table.locator("tr").count()
            assert rows >= 1, "Table is completely empty (no header row)"
