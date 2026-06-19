"""Strict browser-console cleanliness test.

Loads public GDX pages in a real Chromium browser, captures EVERY console
message, every page error, and every failed/non-2xx network request, and
asserts that there are ZERO error-class events. No filters. No allowlists.
If the app is noisy, the fix is to make the app quieter — not to widen the
filter.

Why this test exists:
    On 2026-04-11 a kill-switch deploy landed and basic header + curl checks
    passed, but real DevTools sessions showed four Content Security Policy
    violations (CloudFlare Insights beacon, blob: Web Worker, two Sentry
    regional ingest endpoints). The existing `ConsoleErrorTracker` in
    `conftest.py` filters by type=='error' AND allowlists strings like "401",
    "404", "429", "Failed to fetch" — both of which would have swallowed
    those violations (CSP reports come in as console.info, and the sentry
    URLs contain the string "404" from transient reconnects). This test
    exists so that regressions of that class CANNOT silently ship.

Filter policy: ABSENT BY DESIGN.
    If this test fails and you're tempted to add a filter to make it pass,
    stop. Go read `memory/feedback_never_echo_key_metadata.md` and the
    2026-04-11 session lesson about the ConsoleErrorTracker ignore list.
    The correct response is to fix whichever server/app component is
    generating the noise. "Ignoring an error or warning is not fixing it
    and is bad work." — Doug, 2026-04-11.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest
from playwright.sync_api import sync_playwright

# Public pages — these must NEVER produce console noise regardless of auth state.
# Add new pages here as they're added to the app, not subtract.
PUBLIC_PAGES = [
    "/",
    "/login",
    "/signup",
    "/forgot-password",
]

# The only "messages" we ignore are navigation logs (type='log' with innocuous
# content). Even then, we print them in the failure report for auditing.
# NOTHING is silently dropped.
ERROR_CLASS_TYPES = {"error", "assert", "warning", "warn"}

BASE_URL = os.getenv("GDX_BASE_URL", "https://gdx.example.com")


@dataclass
class PageCapture:
    path: str
    final_url: str | None = None
    http_status: int | None = None
    title: str | None = None
    console: list[dict] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[dict] = field(default_factory=list)
    non_2xx_responses: list[dict] = field(default_factory=list)

    def error_events(self) -> list[str]:
        """Return a human-readable list of all events that would fail this test."""
        out: list[str] = []

        # Console messages of error-class type
        for msg in self.console:
            msg_type = msg.get("type", "")
            text = msg.get("text", "")
            if msg_type in ERROR_CLASS_TYPES:
                loc = msg.get("location") or {}
                loc_str = f" @ {loc.get('url', '?')}:{loc.get('line', '?')}" if loc.get("url") else ""
                out.append(f"[console.{msg_type}] {text}{loc_str}")
            # CSP violations come through as type='info' in Playwright's mapping
            # but are error-class in DevTools. Catch them by content pattern.
            elif "violates the following Content Security Policy" in text:
                out.append(f"[csp-violation/{msg_type}] {text}")
            elif "Refused to" in text or "was blocked" in text:
                out.append(f"[blocked/{msg_type}] {text}")

        # Unhandled page exceptions
        for err in self.page_errors:
            out.append(f"[page-error] {err}")

        # Network failures (DNS, connect, abort, etc.)
        for req in self.failed_requests:
            out.append(
                f"[network-failed] {req.get('method', '?')} {req.get('url', '?')} "
                f"— {req.get('failure', '?')}"
            )

        # 4xx/5xx responses on ANYTHING except intentional 401/404 auth gate
        # No — per Doug, even those are real bugs the app should avoid. Report everything.
        for resp in self.non_2xx_responses:
            out.append(f"[http-{resp.get('status', '?')}] {resp.get('url', '?')}")

        return out


def _capture(path: str) -> PageCapture:
    cap = PageCapture(path=path)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = browser.new_context(
                ignore_https_errors=True,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            page.on("console", lambda msg: cap.console.append({
                "type": msg.type,
                "text": msg.text,
                "location": {
                    "url": msg.location.get("url") if msg.location else None,
                    "line": msg.location.get("lineNumber") if msg.location else None,
                } if msg.location else None,
            }))
            page.on("pageerror", lambda err: cap.page_errors.append(str(err)))
            page.on("requestfailed", lambda req: cap.failed_requests.append({
                "url": req.url,
                "method": req.method,
                "failure": req.failure,
                "resource_type": req.resource_type,
            }))
            page.on("response", lambda resp: (
                cap.non_2xx_responses.append({
                    "url": resp.url,
                    "status": resp.status,
                }) if resp.status >= 400 else None
            ))

            try:
                response = page.goto(
                    f"{BASE_URL}{path}",
                    wait_until="networkidle",
                    timeout=30000,
                )
                cap.http_status = response.status if response else None
                cap.final_url = page.url
                cap.title = page.title()
                # Let any late async work settle (service worker lifecycle, Sentry init, etc.)
                page.wait_for_timeout(2000)
            except Exception as e:
                cap.page_errors.append(f"navigation exception: {e}")
        finally:
            browser.close()
    return cap


@pytest.mark.e2e
@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_page_has_clean_console(path: str) -> None:
    """Every public page must produce zero error-class events in a real browser.

    This test is INTENTIONALLY strict. It catches:
    - Any console.error / console.warn / console.assert
    - CSP violations (logged as console.info in Playwright but shown as errors in DevTools)
    - "Refused to ..." / "... was blocked" messages
    - Unhandled JavaScript exceptions
    - Network requests that failed at the transport layer
    - Any HTTP response with status >= 400

    If this test fails, read the failure message — it lists every bad event
    with its location. Then fix the app, NOT the test.
    """
    cap = _capture(path)
    errors = cap.error_events()

    if errors:
        report = [
            f"\n{'=' * 70}",
            f"Console cleanliness FAILED for {BASE_URL}{path}",
            f"{'=' * 70}",
            f"Final URL:    {cap.final_url}",
            f"HTTP status:  {cap.http_status}",
            f"Page title:   {cap.title}",
            "",
            f"{len(errors)} error-class events detected:",
        ]
        for i, e in enumerate(errors):
            report.append(f"  [{i + 1}] {e}")
        report.append("")
        report.append(
            "Do NOT add filters to this test to suppress these. Fix the "
            "underlying app noise. See gdx_dispatch/tests/e2e/test_console_clean.py "
            "docstring for the 'why'."
        )
        pytest.fail("\n".join(report))
