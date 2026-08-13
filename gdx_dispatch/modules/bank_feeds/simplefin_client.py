"""SimpleFIN Bridge client (sync httpx) — the feed's second provider.

The entire integration is: claim a one-time setup token → hold one Access
URL → issue authenticated ``GET /accounts``. No OAuth, no refresh, no
pagination (one response covers every account and its transactions).

Credential discipline (the plan's non-negotiable): the Access URL embeds
basic-auth userinfo. It is split ONCE at claim time — the stored/logged
base URL never carries credentials, and every request passes auth via
httpx's ``auth=`` parameter so no exception, log line, or Sentry event can
ever contain ``user:pass``. ``SimpleFINClient`` has no attribute holding
the joined URL.

Quota discipline: the bridge expects ≤24 requests/day and disables tokens
that persistently exceed it. The client only COUNTS its requests
(``requests_made``); enforcement (the 20/day ledger) lives in the service/
task layer where scheduled and manual fetches share one ledger.

Redirects are NOT followed: silently re-sending basic-auth to a host the
SSRF guard never vetted is exactly the bug class this module exists to
avoid. The bridge serves its API on the claimed host; a 3xx is an error.
"""
from __future__ import annotations

import base64
import binascii
import logging
import random
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from gdx_dispatch.core.ssrf_guard import validate_outbound_url

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
RETRY_MAX_ATTEMPTS = 3

# GET /accounts date windows are limited to 90 days by the bridge; the
# service layer sizes its windows to this.
MAX_WINDOW_DAYS = 90


class SimpleFINError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, body_snippet: str | None = None):
        self.status_code = status_code
        self.body_snippet = body_snippet
        super().__init__(message)


class SimpleFINAuthError(SimpleFINError):
    """403 from /accounts — access revoked, credentials wrong, or the
    bridge disabled the token (persistent quota abuse)."""


class SimpleFINClaimError(SimpleFINError):
    """403 claiming a setup token — already used. Per spec the user must be
    told the token may be compromised so they can disable it at the bridge."""


def decode_setup_token(setup_token: str) -> str:
    """Base64 setup token → claim URL. Validates https + SSRF guard.

    Raises ValueError with a user-safe message on malformed input.
    """
    text = (setup_token or "").strip()
    if not text:
        raise ValueError("Setup token is empty")
    try:
        claim_url = base64.b64decode(text, validate=True).decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("Setup token is not valid base64") from exc
    parts = urlsplit(claim_url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("Setup token must decode to an https URL")
    if parts.username or parts.password:
        raise ValueError("Claim URL must not contain credentials")
    validate_outbound_url(claim_url)
    return claim_url


def split_access_url(access_url: str) -> tuple[str, str, str]:
    """Access URL → (base_url_without_credentials, username, password).

    The split happens exactly once, right after the claim; nothing
    downstream ever sees the joined form.
    """
    parts = urlsplit((access_url or "").strip())
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("Access URL must be https")
    if not parts.username or parts.password is None:
        raise ValueError("Access URL is missing embedded credentials")
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    base_url = urlunsplit(("https", host, parts.path.rstrip("/"), "", ""))
    validate_outbound_url(base_url + "/")
    return base_url, parts.username, parts.password


def claim_access_url(setup_token: str, *, timeout: httpx.Timeout = DEFAULT_TIMEOUT) -> tuple[str, str, str]:
    """Claim the one-time setup token. Returns split (base_url, user, pass).

    The caller MUST persist the result before doing anything else that can
    fail — the setup token is burned the moment this returns.
    """
    claim_url = decode_setup_token(setup_token)
    try:
        resp = httpx.post(
            claim_url, headers={"Content-Length": "0"}, timeout=timeout, follow_redirects=False
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise SimpleFINError("Could not reach the SimpleFIN bridge to claim the token") from exc
    if resp.status_code == 403:
        raise SimpleFINClaimError(
            "The setup token was already claimed. If you did not use it, it may be "
            "compromised — disable it at the bridge and generate a new one.",
            status_code=403,
        )
    if not resp.is_success:
        raise SimpleFINError(
            f"Claim failed with HTTP {resp.status_code}",
            status_code=resp.status_code, body_snippet=resp.text[:200],
        )
    return split_access_url(resp.text)


class SimpleFINClient:
    """One instance per sync run. ``requests_made`` feeds the fetch ledger."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ):
        parts = urlsplit(base_url)
        if parts.scheme != "https" or parts.username or parts.password:
            raise ValueError("base_url must be https and credential-free")
        validate_outbound_url(base_url + "/")
        self.base_url = base_url.rstrip("/")
        self.requests_made = 0
        self._client = httpx.Client(
            auth=(username, password), timeout=timeout, follow_redirects=False
        )

    def __enter__(self) -> SimpleFINClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _backoff_seconds(self, attempt: int) -> float:
        delay = min(0.5 * (2**attempt), 30.0) + random.uniform(-0.25, 0.25)  # noqa: S311 — retry jitter, not crypto
        return max(0.01, delay)

    def get_accounts(
        self,
        *,
        start_date: int | None = None,
        end_date: int | None = None,
        balances_only: bool = False,
        account_ids: list[str] | None = None,
    ) -> dict:
        """``GET /accounts?version=2``. Dates are UNIX epoch seconds;
        ``end_date`` is EXCLUSIVE per spec. Pending transactions are never
        requested — posted-only ingest is the duplicate-safety decision
        (a pending txn can post under a different id).
        """
        params: list[tuple[str, Any]] = [("version", "2")]
        if start_date is not None:
            params.append(("start-date", int(start_date)))
        if end_date is not None:
            params.append(("end-date", int(end_date)))
        if balances_only:
            params.append(("balances-only", "1"))
        for acct in account_ids or []:
            params.append(("account", acct))

        url = f"{self.base_url}/accounts"
        attempt = 0
        while True:
            self.requests_made += 1
            try:
                resp = self._client.get(url, params=params, headers={"Accept": "application/json"})
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt >= RETRY_MAX_ATTEMPTS:
                    raise SimpleFINError("network failure for GET /accounts") from exc
                log.warning(
                    "simplefin retry attempt=%d/%d err=%s",
                    attempt + 1, RETRY_MAX_ATTEMPTS, type(exc).__name__,
                )
                time.sleep(self._backoff_seconds(attempt))
                attempt += 1
                continue

            if resp.status_code == 403:
                raise SimpleFINAuthError(
                    "SimpleFIN access denied (revoked, wrong credentials, or the "
                    "bridge disabled the token)", status_code=403,
                )
            if resp.status_code == 402:
                raise SimpleFINAuthError(
                    "SimpleFIN bridge subscription lapsed (payment required)", status_code=402,
                )
            if 300 <= resp.status_code < 400:
                raise SimpleFINError(
                    f"unexpected redirect (HTTP {resp.status_code}) — refusing to "
                    "forward credentials", status_code=resp.status_code,
                )
            if 500 <= resp.status_code < 600 and attempt < RETRY_MAX_ATTEMPTS:
                log.warning(
                    "simplefin retry attempt=%d/%d status=%d",
                    attempt + 1, RETRY_MAX_ATTEMPTS, resp.status_code,
                )
                time.sleep(self._backoff_seconds(attempt))
                attempt += 1
                continue
            if not resp.is_success:
                raise SimpleFINError(
                    f"GET /accounts -> HTTP {resp.status_code}",
                    status_code=resp.status_code, body_snippet=resp.text[:200],
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise SimpleFINError("GET /accounts returned non-JSON") from exc
            if not isinstance(data, dict):
                raise SimpleFINError("GET /accounts returned non-object JSON")
            return data
