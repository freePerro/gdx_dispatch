"""Cloudflare Turnstile siteverify helper.

Public docs: https://developers.cloudflare.com/turnstile/get-started/server-side-validation/

Behavior:
- Posts the token to challenges.cloudflare.com/turnstile/v0/siteverify
  (form-encoded body — JSON also accepted, form chosen for parity with curl examples).
- Fail-open if TURNSTILE_SECRET is unset (dev/preview environments without
  Turnstile configured). Production is expected to set the secret; missing-secret
  fail-open IS the documented dev path in the sprint plan.
- Returns (ok: bool, error_codes: list[str]). Network errors and malformed
  responses return (False, ["network-error"|"bad-response"]) — never raise.
- If TURNSTILE_HOSTNAME is set, the response hostname is pinned (forgery check).
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT_SECONDS = 5.0


async def verify_turnstile(
    token: str | None,
    remote_ip: str | None,
    *,
    expected_hostname: str | None = None,
) -> tuple[bool, list[str]]:
    """Validate a Turnstile token against Cloudflare siteverify.

    Returns (ok, error_codes). ok is True when:
      - TURNSTILE_SECRET is unset (fail-open dev), OR
      - siteverify returns success=true AND hostname matches expected (when set).
    """
    secret = os.environ.get("TURNSTILE_SECRET")
    if not secret:
        return True, []

    if not token:
        return False, ["missing-input-response"]

    data: dict[str, str] = {"secret": secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            r = await client.post(SITEVERIFY_URL, data=data)
    except httpx.HTTPError as exc:
        logger.warning("turnstile siteverify network error: %s", exc)
        return False, ["network-error"]

    try:
        body = r.json()
    except ValueError:
        logger.warning("turnstile siteverify returned non-JSON, status=%s", r.status_code)
        return False, ["bad-response"]

    success = bool(body.get("success", False))
    error_codes = list(body.get("error-codes") or [])

    pinned_host = expected_hostname or os.environ.get("TURNSTILE_HOSTNAME")
    if success and pinned_host:
        seen_host = body.get("hostname")
        if seen_host != pinned_host:
            logger.warning(
                "turnstile hostname mismatch: got=%r expected=%r", seen_host, pinned_host
            )
            return False, ["hostname-mismatch", *error_codes]

    return success, error_codes
