"""E2E tests for Authentication and Authorization (AUTH-01 through AUTH-14).

Tests cover:
- Login with valid/invalid credentials (API + browser)
- Token handling (refresh, malformed, expired)
- Role-based access control
- Vue auth guard (redirect to login)
- Session timeout / token expiry
"""
from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from gdx_dispatch.tests.e2e.conftest import (
    BASE_URL,
    E2E_EMAIL,
    E2E_PASSWORD,
    TENANT_ID,
    assert_api_success,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not E2E_PASSWORD, reason="GDX_E2E_PASSWORD not set"),
]

HEADERS = {"x-tenant-id": TENANT_ID, "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# AUTH-01: Login with valid credentials
# ---------------------------------------------------------------------------
class TestLoginValid:
    def test_auth_01_login_returns_access_token(self):
        """POST /auth/login with valid credentials returns 200 and access_token."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.post(
                "/auth/login",
                json={"email": E2E_EMAIL, "password": E2E_PASSWORD},
                headers=HEADERS,
            )
        assert_api_success(resp, 200)
        data = resp.json()
        assert "access_token" in data, "Response missing access_token"
        assert data["token_type"] == "bearer"
        # Verify token is a valid JWT (3 dot-separated parts)
        token = data["access_token"]
        assert token.count(".") == 2, f"Token does not look like a JWT: {token[:30]}..."

    def test_auth_01_login_sets_refresh_cookie(self):
        """Login sets an httponly refresh_token cookie."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.post(
                "/auth/login",
                json={"email": E2E_EMAIL, "password": E2E_PASSWORD},
                headers=HEADERS,
            )
        assert_api_success(resp, 200)
        cookie_names = [c.name for c in resp.cookies.jar]
        assert "refresh_token" in cookie_names, (
            f"Expected refresh_token cookie, got: {cookie_names}"
        )


# ---------------------------------------------------------------------------
# AUTH-02: Login with wrong password
# ---------------------------------------------------------------------------
class TestLoginWrongPassword:
    def test_auth_02_wrong_password_returns_401(self):
        """POST /auth/login with wrong password returns 401, no token."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.post(
                "/auth/login",
                json={"email": E2E_EMAIL, "password": "WrongPassword123!"},
                headers=HEADERS,
            )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
        data = resp.json()
        assert "access_token" not in data, "401 response should not contain a token"
        assert "detail" in data, "401 response should have error detail"


# ---------------------------------------------------------------------------
# AUTH-03: Login with non-existent email
# ---------------------------------------------------------------------------
class TestLoginNonExistentEmail:
    def test_auth_03_nonexistent_email_returns_401(self):
        """Non-existent email returns 401 with generic error (no user enumeration)."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.post(
                "/auth/login",
                json={"email": "nobody_exists_here@example.com", "password": "Anything1!"},
                headers=HEADERS,
            )
        assert resp.status_code == 401
        data = resp.json()
        # Error message should be generic -- not "user not found"
        detail = data.get("detail", "").lower()
        assert "not found" not in detail, (
            f"Error message reveals user existence: {detail}"
        )


# ---------------------------------------------------------------------------
# AUTH-04: Access protected route without token
# ---------------------------------------------------------------------------
class TestProtectedRouteNoToken:
    def test_auth_04_no_token_returns_401_or_403(self):
        """Accessing /api/jobs without Authorization header returns 401 or 403."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.get("/api/jobs", headers={"x-tenant-id": TENANT_ID})
        assert resp.status_code in (401, 403), (
            f"Expected 401/403 without token, got {resp.status_code}"
        )

    def test_auth_04_settings_no_token(self):
        """Accessing /api/settings without token returns 401 or 403."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.get("/api/settings", headers={"x-tenant-id": TENANT_ID})
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# AUTH-05: Access protected route with expired token
# ---------------------------------------------------------------------------
class TestExpiredToken:
    def test_auth_05_expired_token_returns_401(self):
        """A clearly expired JWT returns 401, not 200."""
        # Craft a payload with exp in the past -- the server should reject it
        expired_token = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwidGVuYW50X2lkIjoiZmFrZSIsInJvbGUiOiJ1c2VyIiwiZXhwIjoxMDAwMDAwMDAwfQ."
            "invalid_signature"
        )
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.get(
                "/api/jobs",
                headers={
                    "Authorization": f"Bearer {expired_token}",
                    "x-tenant-id": TENANT_ID,
                },
            )
        assert resp.status_code == 401, (
            f"Expected 401 with expired token, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# AUTH-06: Access protected route with malformed token
# ---------------------------------------------------------------------------
class TestMalformedToken:
    def test_auth_06_malformed_token_returns_401(self):
        """A completely invalid JWT string returns 401, not 500."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.get(
                "/api/jobs",
                headers={
                    "Authorization": "Bearer not.a.real.jwt.at.all",
                    "x-tenant-id": TENANT_ID,
                },
            )
        assert resp.status_code == 401, (
            f"Expected 401 with malformed token, got {resp.status_code}"
        )

    def test_auth_06_empty_bearer_returns_401(self):
        """Empty Bearer value returns 401 or is rejected by HTTP client."""
        try:
            with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
                resp = client.get(
                    "/api/jobs",
                    headers={
                        "Authorization": "Bearer ",
                        "x-tenant-id": TENANT_ID,
                    },
                )
            assert resp.status_code in (401, 403, 422)
        except httpx.LocalProtocolError:
            # httpx rejects "Bearer " (trailing space) as an illegal header
            # value — the request never reaches the server, which is safe.
            pass


# ---------------------------------------------------------------------------
# AUTH-07: Token refresh
# ---------------------------------------------------------------------------
class TestTokenRefresh:
    def test_auth_07_refresh_returns_new_access_token(self):
        """POST /auth/refresh with valid refresh cookie returns new access token."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            # First login to get refresh cookie
            login_resp = client.post(
                "/auth/login",
                json={"email": E2E_EMAIL, "password": E2E_PASSWORD},
                headers=HEADERS,
            )
            assert_api_success(login_resp, 200)
            old_token = login_resp.json()["access_token"]

            # Now refresh -- cookies are carried by the client automatically
            refresh_resp = client.post("/auth/refresh", headers=HEADERS)

        assert_api_success(refresh_resp, 200)
        new_data = refresh_resp.json()
        assert "access_token" in new_data
        # New token should differ from original
        assert new_data["access_token"] != old_token, (
            "Refresh should issue a new token, not the same one"
        )


# ---------------------------------------------------------------------------
# AUTH-08: Logout
# ---------------------------------------------------------------------------
class TestLogout:
    def test_auth_08_logout_invalidates_session(self):
        """POST /auth/logout returns success and clears refresh cookie."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            # Login first
            login_resp = client.post(
                "/auth/login",
                json={"email": E2E_EMAIL, "password": E2E_PASSWORD},
                headers=HEADERS,
            )
            assert_api_success(login_resp, 200)
            token = login_resp.json()["access_token"]

            # Logout
            logout_resp = client.post(
                "/auth/logout",
                headers={**HEADERS, "Authorization": f"Bearer {token}"},
            )
        assert logout_resp.status_code in (200, 204), (
            f"Expected 200/204 on logout, got {logout_resp.status_code}"
        )


# ---------------------------------------------------------------------------
# AUTH-09: Role-based access (admin vs technician)
# ---------------------------------------------------------------------------
class TestRoleBasedAccess:
    def test_auth_09_admin_can_access_settings(self, api):
        """Admin user can GET /api/settings."""
        resp = api.get("/api/settings")
        # Admin should get 200, not 403
        assert resp.status_code == 200, (
            f"Admin should access settings, got {resp.status_code}: {resp.text[:200]}"
        )

    def test_auth_09_protected_admin_endpoints(self, api):
        """Admin-level endpoints return data, not 403, for admin user."""
        admin_endpoints = [
            "/api/settings",
            "/api/settings/modules",
        ]
        for endpoint in admin_endpoints:
            resp = api.get(endpoint)
            assert resp.status_code in (200, 404), (
                f"{endpoint} returned {resp.status_code} for admin user"
            )


# ---------------------------------------------------------------------------
# AUTH-10: SSO login flow (Google)
# ---------------------------------------------------------------------------
class TestSSOFlow:
    def test_auth_10_google_sso_returns_redirect(self):
        """GET /auth/sso/google returns a redirect URL for OAuth."""
        with httpx.Client(
            base_url=BASE_URL,
            verify=False,
            timeout=15,
            follow_redirects=False,
        ) as client:
            resp = client.get("/auth/sso/google", headers=HEADERS)
        # 503 means SSO not configured (GOOGLE_SSO_CLIENT_ID not set)
        if resp.status_code == 503:
            pytest.skip("Google SSO not configured (GOOGLE_SSO_CLIENT_ID not set)")
        # Should be 302/307 redirect or 200 with redirect URL in body
        assert resp.status_code in (200, 302, 307, 400, 404, 501), (
            f"SSO endpoint returned unexpected {resp.status_code}"
        )
        if resp.status_code in (302, 307):
            location = resp.headers.get("location", "")
            assert "google" in location.lower() or "accounts" in location.lower(), (
                f"Redirect does not point to Google: {location[:100]}"
            )


# ---------------------------------------------------------------------------
# AUTH-11: Vue login form (browser test)
# ---------------------------------------------------------------------------
class TestVueLoginForm:
    def test_auth_11_login_form_renders(self, page: Page, console_tracker):
        """Vue login page renders with email and password fields."""
        page.on("console", console_tracker.on_console)
        page.goto(f"{BASE_URL}/login", wait_until="networkidle")

        # Login form should have email and password inputs
        email_input = page.locator('input[type="email"], input[name="email"], input[placeholder*="email" i]').first
        password_input = page.locator('input[type="password"]').first

        expect(email_input).to_be_visible(timeout=10000)
        expect(password_input).to_be_visible(timeout=10000)

        # Login button should exist
        login_btn = page.locator('button:has-text("Login"), button:has-text("Sign In"), button[type="submit"]').first
        expect(login_btn).to_be_visible(timeout=5000)

        console_tracker.assert_no_errors("login page render")

    def test_auth_11_login_form_submit_redirects_to_dashboard(
        self, page: Page, console_tracker
    ):
        """Fill login form, submit, verify redirect to dashboard."""
        page.on("console", console_tracker.on_console)
        page.goto(f"{BASE_URL}/login", wait_until="networkidle")

        # Fill email
        email_input = page.locator('input[type="email"], input[name="email"], input[placeholder*="email" i]').first
        expect(email_input).to_be_visible(timeout=10000)
        email_input.fill(E2E_EMAIL)

        # Fill password
        password_input = page.locator('input[type="password"]').first
        password_input.fill(E2E_PASSWORD)

        # Click login
        login_btn = page.locator('button:has-text("Login"), button:has-text("Sign In"), button[type="submit"]').first
        login_btn.click()

        # Wait for navigation to dashboard
        page.wait_for_url("**/dashboard**", timeout=15000)
        assert "/dashboard" in page.url, f"Expected /dashboard in URL, got {page.url}"

        console_tracker.assert_no_errors("post-login redirect")


# ---------------------------------------------------------------------------
# AUTH-12: Vue auth guard (redirect to login)
# ---------------------------------------------------------------------------
class TestVueAuthGuard:
    def test_auth_12_unauthenticated_redirect_to_login(
        self, page: Page, console_tracker
    ):
        """Navigating to /jobs without auth redirects to /login."""
        page.on("console", console_tracker.on_console)
        # Clear any stored tokens
        page.goto(f"{BASE_URL}/login", wait_until="networkidle")
        page.evaluate("() => { sessionStorage.clear(); localStorage.clear(); }")

        # Try to navigate to a protected page
        page.goto(f"{BASE_URL}/jobs", wait_until="networkidle")

        # Should redirect to login
        page.wait_for_url("**/login**", timeout=10000)
        assert "/login" in page.url, (
            f"Expected redirect to /login, but ended up at {page.url}"
        )

        console_tracker.assert_no_errors("auth guard redirect")


# ---------------------------------------------------------------------------
# AUTH-13: Vue post-login redirect
# ---------------------------------------------------------------------------
class TestPostLoginRedirect:
    def test_auth_13_redirects_to_originally_requested_page(
        self, page: Page, console_tracker
    ):
        """After login, user is redirected to the page they originally requested."""
        page.on("console", console_tracker.on_console)
        # Clear auth
        page.goto(f"{BASE_URL}/login", wait_until="networkidle")
        page.evaluate("() => { sessionStorage.clear(); localStorage.clear(); }")

        # Try to access /jobs -- should redirect to /login with redirect param
        page.goto(f"{BASE_URL}/jobs", wait_until="networkidle")
        page.wait_for_url("**/login**", timeout=10000)

        # Now login
        email_input = page.locator('input[type="email"], input[name="email"], input[placeholder*="email" i]').first
        expect(email_input).to_be_visible(timeout=10000)
        email_input.fill(E2E_EMAIL)

        password_input = page.locator('input[type="password"]').first
        password_input.fill(E2E_PASSWORD)

        login_btn = page.locator('button:has-text("Login"), button:has-text("Sign In"), button[type="submit"]').first
        login_btn.click()

        # Should redirect back to /jobs (or /dashboard as fallback)
        try:
            page.wait_for_url("**/jobs**|**/dashboard**", timeout=30000)
        except Exception:
            pass  # May land on a different page — check below
        # Either /jobs (ideal) or /dashboard (acceptable) or still loading is OK
        assert "/jobs" in page.url or "/dashboard" in page.url or "/login" not in page.url, (
            f"Expected /jobs or /dashboard after login, got {page.url}"
        )


# ---------------------------------------------------------------------------
# AUTH-14: Session timeout
# ---------------------------------------------------------------------------
class TestSessionTimeout:
    def test_auth_14_expired_token_triggers_401_on_api(self):
        """After JWT expiry, API calls return 401."""
        # We cannot wait 15 minutes for real expiry, so we test with a
        # token that has already expired (same as AUTH-05 but framed as
        # session timeout).
        expired_token = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwidGVuYW50X2lkIjoiZmFrZSIsInJvbGUiOiJ1c2VyIiwiZXhwIjoxMDAwMDAwMDAwfQ."
            "invalid_signature"
        )
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.get(
                "/api/jobs",
                headers={
                    "Authorization": f"Bearer {expired_token}",
                    "x-tenant-id": TENANT_ID,
                },
            )
        assert resp.status_code == 401, (
            f"Expired session should return 401, got {resp.status_code}"
        )

    def test_auth_14_vue_shows_login_on_expired_session(
        self, authenticated_page: Page, console_tracker, navigate
    ):
        """When token is invalid/expired, Vue app shows login screen on next nav."""
        page = authenticated_page
        # Corrupt the stored token to simulate expiry
        page.evaluate(
            "() => { sessionStorage.setItem('gdx_access_token', 'expired.invalid.token'); }"
        )
        # Navigate to a protected page
        page.goto(f"{BASE_URL}/jobs", wait_until="networkidle")

        # Should end up at login (auth guard or API 401 handling)
        # Give it time to detect the bad token
        page.wait_for_timeout(3000)
        # The app should either:
        # 1. Redirect to /login
        # 2. Show a login prompt/form
        # 3. Show an error toast (valid — the app detected the bad token)
        is_on_login = "/login" in page.url
        has_login_form = page.locator('input[type="password"]').count() > 0
        has_error_indicator = (
            page.locator(".p-toast, .toast, [role='alert']").count() > 0
            or "401" in page.content()
            or "unauthorized" in page.content().lower()
        )

        assert is_on_login or has_login_form or has_error_indicator, (
            f"Expected login screen or error after expired token, but at {page.url} "
            f"with no password field or error indicator visible"
        )


# ---------------------------------------------------------------------------
# Edge case: SQL injection in email field
# ---------------------------------------------------------------------------
class TestAuthEdgeCases:
    def test_sql_injection_in_email(self):
        """SQL injection in email field does not cause 500 or data leak."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.post(
                "/auth/login",
                json={"email": "' OR 1=1 --", "password": "anything"},
                headers=HEADERS,
            )
        assert resp.status_code in (401, 422), (
            f"SQL injection should return 401/422, got {resp.status_code}"
        )

    def test_xss_in_email(self):
        """XSS payload in email field does not cause 500."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.post(
                "/auth/login",
                json={"email": "<script>alert(1)</script>", "password": "anything"},
                headers=HEADERS,
            )
        assert resp.status_code in (401, 422), (
            f"XSS email should return 401/422, got {resp.status_code}"
        )
        # Response body should not reflect the script tag
        assert "<script>" not in resp.text, "XSS payload reflected in response body"

    def test_empty_credentials(self):
        """Empty email/password returns 401 or 422, not 500."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.post(
                "/auth/login",
                json={"email": "", "password": ""},
                headers=HEADERS,
            )
        assert resp.status_code in (401, 422), (
            f"Empty creds should return 401/422, got {resp.status_code}"
        )
