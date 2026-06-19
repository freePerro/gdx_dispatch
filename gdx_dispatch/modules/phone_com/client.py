"""Phone.com API v4 client — Bearer auth, jittered exponential backoff retries.

Sync httpx client for token validation + account lookup. Used by the admin
Settings save+test flow and the per-tenant key_storage cache step.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import random
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlparse

import httpx

from gdx_dispatch.core.ssrf_guard import OutboundURLBlocked, validate_outbound_url

log = logging.getLogger("gdx_dispatch.modules.phone_com.client")

BASE_URL = "https://api.phone.com/v4"
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class PhoneComAPIError(RuntimeError):
    """Phone.com API returned a non-success response."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body_snippet: str | None = None,
    ):
        self.status_code = status_code
        self.body_snippet = body_snippet
        super().__init__(message)


class PhoneComClient:
    """Sync Phone.com v4 client. Instantiate per request; cheap to construct."""

    def __init__(
        self,
        token: str,
        voip_id: int | None = None,
        base_url: str = BASE_URL,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    ):
        self._token = token
        self.voip_id = voip_id
        self.base_url = base_url.rstrip("/")
        self.retry_max_attempts = 5
        self._timeout = (
            timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        )
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self._timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )

    def __enter__(self) -> PhoneComClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _token_id(self) -> str:
        return hashlib.sha256(self._token.encode()).hexdigest()[:8]

    def _backoff_seconds(self, attempt: int) -> float:
        delay = min(0.5 * (2**attempt), 30.0) + random.uniform(-0.25, 0.25)  # noqa: S311 — retry jitter, not crypto
        return max(0.01, delay)

    def _request_with_retry(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.retry_max_attempts + 1):
            try:
                resp = self._client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt >= self.retry_max_attempts:
                    raise
                log.warning(
                    "phone_com retry attempt=%d/%d %s %s err=%s",
                    attempt + 1, self.retry_max_attempts, method, url, type(exc).__name__,
                )
                time.sleep(self._backoff_seconds(attempt))
                continue

            retryable = resp.status_code == 429 or 500 <= resp.status_code < 600
            if retryable and attempt < self.retry_max_attempts:
                log.warning(
                    "phone_com retry attempt=%d/%d %s %s status=%d",
                    attempt + 1, self.retry_max_attempts, method, url, resp.status_code,
                )
                time.sleep(self._backoff_seconds(attempt))
                continue
            return resp

        raise PhoneComAPIError(f"max retries exhausted for {method} {url}")

    # ── P3.10: OAuth (account-scoped client management + token exchange) ─

    @staticmethod
    def exchange_auth_code(
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        base_url: str = BASE_URL,
    ) -> dict[str, Any]:
        """``POST /v4/oauth/access-token`` with grant_type=authorization_code.
        Static — no token required to call (this IS how you mint one).
        """
        body = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as cli:
            resp = cli.post(f"{base_url.rstrip('/')}/oauth/access-token", json=body)
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com /oauth/access-token {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    @staticmethod
    def refresh_access_token(
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        base_url: str = BASE_URL,
    ) -> dict[str, Any]:
        body = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as cli:
            resp = cli.post(f"{base_url.rstrip('/')}/oauth/access-token", json=body)
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com /oauth/access-token refresh {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    def list_oauth_clients(
        self, *, voip_id: int | None = None, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        return self._get_paginated(
            f"/accounts/{vid}/oauth/clients", {"limit": limit, "offset": offset},
        )

    def get_oauth_client(
        self, *, voip_id: int | None = None, client_id: str,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        resp = self._request_with_retry(
            "GET", f"/accounts/{vid}/oauth/clients/{client_id}",
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com get_oauth_client {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    def list_oauth_client_redirect_uris(
        self,
        *,
        voip_id: int | None = None,
        client_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        return self._get_paginated(
            f"/accounts/{vid}/oauth/clients/{client_id}/redirect-uris",
            {"limit": limit, "offset": offset},
        )

    def create_oauth_client_redirect_uri(
        self, *, voip_id: int | None = None, client_id: str, redirect_uri: str,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        resp = self._request_with_retry(
            "POST",
            f"/accounts/{vid}/oauth/clients/{client_id}/redirect-uris",
            json={"redirect_uri": redirect_uri},
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com create_redirect_uri {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    def get_access_token_details(self) -> dict[str, Any] | None:
        """``GET /v4/oauth/access-token/details`` — returns scope/expires/etc.
        for the *current* bearer token. Returns None on 4xx (token type
        doesn't expose introspection), raises on 5xx."""
        resp = self._request_with_retry("GET", "/oauth/access-token/details")
        if resp.is_success:
            return resp.json()
        if 400 <= resp.status_code < 500:
            log.info(
                "phone_com get_access_token_details %d (likely permanent-token; introspection not exposed)",
                resp.status_code,
            )
            return None
        raise PhoneComAPIError(
            f"phone_com /oauth/access-token/details {resp.status_code}",
            status_code=resp.status_code,
            body_snippet=resp.text[:500],
        )

    def get_account(self) -> dict[str, Any]:
        """GET /v4/accounts → first item of items[]."""
        resp = self._request_with_retry("GET", "/accounts")
        if not resp.is_success:
            snippet = resp.text[:500]
            log.error(
                "phone_com get_account failed status=%d token_id=%s",
                resp.status_code, self._token_id(),
            )
            raise PhoneComAPIError(
                f"phone_com /accounts {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=snippet,
            )
        items = resp.json().get("items") or []
        if not items:
            raise PhoneComAPIError(
                "phone_com /accounts returned no items",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        log.info("phone_com get_account ok account_id=%s", items[0].get("id"))
        return items[0]

    def test_token(self) -> dict[str, Any]:
        """Validate token; never raises. Returns shape doc'd in pc-s2 AC."""
        token_id = self._token_id()
        t0 = time.perf_counter()
        try:
            acct = self.get_account()
        except PhoneComAPIError as exc:
            latency = int((time.perf_counter() - t0) * 1000)
            # Surface Phone.com's actual error message (e.g.
            # "oauth2.access_denied — explicit deny in identity policy")
            # rather than just "401" — the body is what tells Doug whether
            # to mint a different token type.
            err_parts: list[str] = []
            if exc.status_code:
                err_parts.append(str(exc.status_code))
            err_parts.append(str(exc))
            if exc.body_snippet:
                # Try to extract Phone.com's @error.@message; fall back to raw.
                try:
                    import json as _json
                    body = _json.loads(exc.body_snippet)
                    pc_err = (body.get("@error") or {}).get("@message")
                    pc_code = (body.get("@error") or {}).get("@code")
                    if pc_err:
                        suffix = f"{pc_code}: {pc_err}" if pc_code else pc_err
                        err_parts.append(f"— {suffix}")
                except Exception:  # noqa: BLE001
                    err_parts.append(f"— {exc.body_snippet[:200]}")
            err = " ".join(err_parts)
            log.error("phone_com test_token fail token_id=%s err=%s", token_id, err)
            return {
                "ok": False, "account_name": None, "voip_id": None,
                "latency_ms": latency, "error": err,
            }
        except Exception as exc:  # noqa: BLE001
            latency = int((time.perf_counter() - t0) * 1000)
            log.error("phone_com test_token unexpected token_id=%s err=%s", token_id, exc)
            return {
                "ok": False, "account_name": None, "voip_id": None,
                "latency_ms": latency,
                "error": f"{type(exc).__name__}: {exc}",
            }
        latency = int((time.perf_counter() - t0) * 1000)
        log.info("phone_com test_token ok token_id=%s latency_ms=%d", token_id, latency)
        return {
            "ok": True,
            "account_name": acct.get("name"),
            "voip_id": acct.get("id"),
            "features": acct.get("features"),
            "latency_ms": latency,
            "error": None,
        }

    # ── pc-s3: list endpoints + pagination ───────────────────────────────

    _PAGINATE_HARD_CAP = 10_000

    def _resolve_voip_id(self, override: int | None) -> int:
        vid = override if override is not None else self.voip_id
        if vid is None:
            raise PhoneComAPIError("voip_id required")
        return vid

    def _get_paginated(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        resp = self._request_with_retry("GET", path, params=clean)
        if not resp.is_success:
            snippet = resp.text[:500]
            log.error("phone_com %s failed status=%d", path, resp.status_code)
            raise PhoneComAPIError(
                f"phone_com {path} {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=snippet,
            )
        return resp.json()

    def list_calls(
        self,
        *,
        voip_id: int | None = None,
        from_epoch: int | None = None,
        to_epoch: int | None = None,
        extension_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
        mode: str = "full",
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        params: dict[str, Any] = {"limit": limit, "offset": offset, "mode": mode}
        if from_epoch is not None:
            params["filters[start_time]"] = f"gt:{from_epoch}"
        if to_epoch is not None:
            # Phone.com supports multiple filters under the same key; second wins last
            # in respx params, so use a paired list-syntax param to keep both.
            params.setdefault("filters[start_time]", "")
            params["filters[start_time]"] = (
                f"{params['filters[start_time]']},lt:{to_epoch}"
                if params["filters[start_time]"] else f"lt:{to_epoch}"
            )
        if extension_id is not None:
            params["filters[extension]"] = f"eq:{extension_id}"
        return self._get_paginated(f"/accounts/{vid}/call-logs", params)

    def list_messages(
        self,
        *,
        voip_id: int | None = None,
        from_epoch: int | None = None,
        to_epoch: int | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if from_epoch is not None:
            params["filters[created_at]"] = f"gt:{from_epoch}"
        if to_epoch is not None:
            params.setdefault("filters[created_at]", "")
            params["filters[created_at]"] = (
                f"{params['filters[created_at]']},lt:{to_epoch}"
                if params["filters[created_at]"] else f"lt:{to_epoch}"
            )
        if direction is not None:
            params["filters[direction]"] = f"eq:{direction}"
        return self._get_paginated(f"/accounts/{vid}/messages", params)

    def list_extensions(
        self,
        *,
        voip_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        return self._get_paginated(
            f"/accounts/{vid}/extensions",
            {"limit": limit, "offset": offset},
        )

    # ── P2.9: conversations ─────────────────────────────────────────────

    def patch_conversation(
        self,
        *,
        voip_id: int | None = None,
        extension_id: int,
        conversation_id: str,
        read: bool | None = None,
    ) -> dict[str, Any]:
        """PATCH a conversation. Phone.com keys conversation read-state
        per-extension since each extension has its own inbox view."""
        vid = self._resolve_voip_id(voip_id)
        payload: dict[str, Any] = {}
        if read is not None:
            payload["read"] = read
        if not payload:
            raise PhoneComAPIError("patch_conversation: nothing to patch")
        path = (
            f"/accounts/{vid}/extensions/{extension_id}/conversations/{conversation_id}"
        )
        resp = self._request_with_retry("PATCH", path, json=payload)
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com patch_conversation {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    # ── P2.8: contacts ──────────────────────────────────────────────────

    def list_contacts(
        self, *, voip_id: int | None = None, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        return self._get_paginated(
            f"/accounts/{vid}/contacts", {"limit": limit, "offset": offset},
        )

    def create_contact(
        self,
        *,
        voip_id: int | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        company: str | None = None,
        phone_numbers: list[dict[str, str]] | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a Phone.com contact. ``phone_numbers`` is a list of
        ``{"number": "+15555550100", "type": "mobile"|"work"|"home"}``.
        ``external_id`` is our Customer.id — Phone.com surfaces it in
        their contact-detail UI and we use it on subsequent updates.
        """
        vid = self._resolve_voip_id(voip_id)
        payload: dict[str, Any] = {}
        if first_name is not None:
            payload["first_name"] = first_name
        if last_name is not None:
            payload["last_name"] = last_name
        if company is not None:
            payload["company"] = company
        if phone_numbers:
            payload["phone_numbers"] = phone_numbers
        if external_id is not None:
            payload["external_id"] = external_id
        resp = self._request_with_retry(
            "POST", f"/accounts/{vid}/contacts", json=payload,
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com create_contact {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    # ── P2.6: blocked calls ─────────────────────────────────────────────

    def list_blocked_calls(
        self, *, voip_id: int | None = None, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        return self._get_paginated(
            f"/accounts/{vid}/blocked-calls", {"limit": limit, "offset": offset},
        )

    def create_blocked_call(
        self,
        *,
        voip_id: int | None = None,
        name: str,
        number: str,
        direction: str = "in",
        action: str = "block",
    ) -> dict[str, Any]:
        """Block a number from calling in. Phone.com schema accepts:
        ``{"name": str, "number": E.164, "direction": "in"|"out", "action": "block"|"voicemail"|...}``.
        """
        vid = self._resolve_voip_id(voip_id)
        payload = {
            "name": name, "number": number, "direction": direction, "action": action,
        }
        resp = self._request_with_retry(
            "POST", f"/accounts/{vid}/blocked-calls", json=payload,
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com create_blocked_call {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    def delete_blocked_call(
        self, *, voip_id: int | None = None, blocked_call_id: int,
    ) -> None:
        vid = self._resolve_voip_id(voip_id)
        resp = self._request_with_retry(
            "DELETE", f"/accounts/{vid}/blocked-calls/{blocked_call_id}",
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com delete_blocked_call {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )

    # ── P3.11: call-reports (server-computed analytics) ─────────────────

    def get_call_report(
        self,
        *,
        voip_id: int | None = None,
        from_epoch: int | None = None,
        to_epoch: int | None = None,
        extension_id: int | None = None,
    ) -> dict[str, Any]:
        """``GET /v4/accounts/{voip_id}/call-reports`` — Phone.com's own
        rolled-up analytics. We use this to reconcile against
        ``phone_com_stats_daily`` and log drift for ops review."""
        vid = self._resolve_voip_id(voip_id)
        path = (
            f"/accounts/{vid}/extensions/{extension_id}/call-reports"
            if extension_id is not None
            else f"/accounts/{vid}/call-reports"
        )
        params: dict[str, Any] = {}
        if from_epoch is not None:
            params["filters[start_time]"] = f"gt:{from_epoch}"
        if to_epoch is not None:
            params.setdefault("filters[start_time]", "")
            params["filters[start_time]"] = (
                f"{params['filters[start_time]']},lt:{to_epoch}"
                if params["filters[start_time]"] else f"lt:{to_epoch}"
            )
        return self._get_paginated(path, params)

    def list_phone_numbers(
        self,
        *,
        voip_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        return self._get_paginated(
            f"/accounts/{vid}/phone-numbers",
            {"limit": limit, "offset": offset},
        )

    # ── P2.7: faxes ─────────────────────────────────────────────────────

    def list_faxes(
        self,
        *,
        voip_id: int | None = None,
        from_epoch: int | None = None,
        to_epoch: int | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if from_epoch is not None:
            params["filters[created_at]"] = f"gt:{from_epoch}"
        if to_epoch is not None:
            params.setdefault("filters[created_at]", "")
            params["filters[created_at]"] = (
                f"{params['filters[created_at]']},lt:{to_epoch}"
                if params["filters[created_at]"] else f"lt:{to_epoch}"
            )
        if direction is not None:
            params["filters[direction]"] = f"eq:{direction}"
        return self._get_paginated(f"/accounts/{vid}/fax", params)

    def get_fax(self, *, voip_id: int | None = None, fax_id: int) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        resp = self._request_with_retry("GET", f"/accounts/{vid}/fax/{fax_id}")
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com get_fax {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    def stream_fax_pdf(
        self, *, voip_id: int | None = None, fax_id: int,
    ) -> tuple[Iterator[bytes], str]:
        """Stream the PDF download. Returns (chunk-iterator, content_type).
        Phone.com 200s with the PDF bytes inline at /v4/accounts/{voip_id}/fax/{id}/download.
        """
        vid = self._resolve_voip_id(voip_id)
        url = f"{self.base_url}/accounts/{vid}/fax/{fax_id}/download"
        return self.stream_url(url, requires_auth=True)

    # ── pc-s4: send_message + binary streaming ──────────────────────────

    _SMS_MAX_BODY = 1600

    def send_message(
        self,
        *,
        voip_id: int | None = None,
        from_number: str,
        to_number: str,
        body: str,
        media_urls: list[str] | None = None,
        extension_id: int | None = None,
    ) -> dict[str, Any]:
        if len(body) > self._SMS_MAX_BODY:
            raise PhoneComAPIError(
                f"body too long for P2P SMS: {len(body)} > {self._SMS_MAX_BODY}",
            )
        vid = self._resolve_voip_id(voip_id)
        path = (
            f"/accounts/{vid}/extensions/{extension_id}/messages"
            if extension_id is not None
            else f"/accounts/{vid}/messages"
        )
        payload: dict[str, Any] = {"from": from_number, "to": to_number, "text": body}
        if media_urls:
            payload["media_urls"] = media_urls
        resp = self._request_with_retry("POST", path, json=payload)
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com {path} {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    def stream_url(
        self,
        url: str,
        *,
        requires_auth: bool = False,
        chunk_size: int = 8192,
    ) -> tuple[Iterator[bytes], str]:
        """Stream a binary URL. Returns (chunk-iterator, content_type)."""
        # SSRF guard: the URL originates from (unsigned) webhook payloads, so it is
        # attacker-influenced. Block private/loopback/link-local/metadata targets,
        # and only attach the Phone.com bearer to genuine phone.com hosts so the
        # credential can never be exfiltrated to an attacker-chosen host.
        try:
            validate_outbound_url(url)
        except OutboundURLBlocked as exc:
            raise PhoneComAPIError(f"phone_com stream_url blocked url={url}: {exc}") from exc
        host = (urlparse(url).hostname or "").lower()
        if requires_auth and not (host == "phone.com" or host.endswith(".phone.com")):
            requires_auth = False  # never send the bearer to a non-phone.com host
        headers = {"Authorization": f"Bearer {self._token}"} if requires_auth else {}
        # Use a fresh httpx.Client for streaming to avoid the base-URL prefix.
        client = httpx.Client(timeout=self._timeout)
        resp = client.get(url, headers=headers)
        if not resp.is_success:
            client.close()
            raise PhoneComAPIError(
                f"phone_com stream_url {resp.status_code} url={url}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        ctype = resp.headers.get("content-type") or "audio/wav"

        def _iter() -> Iterator[bytes]:
            try:
                yield from resp.iter_bytes(chunk_size=chunk_size)
            finally:
                client.close()

        return _iter(), ctype

    def _try_stream(
        self, url: str, *, requires_auth: bool, chunk_size: int = 8192,
    ) -> tuple[Iterator[bytes], str] | None:
        """Probe a URL with a HEAD; return None on 4xx so caller can fallback.
        cp_urls are short-lived presigned and 400 once expired."""
        try:
            return self.stream_url(url, requires_auth=requires_auth, chunk_size=chunk_size)
        except PhoneComAPIError as exc:
            if exc.status_code and 400 <= exc.status_code < 500:
                log.info(
                    "phone_com stream %s returned %d, falling back",
                    url[:60], exc.status_code,
                )
                return None
            raise

    def stream_voicemail_audio(
        self, call_log_row: dict[str, Any]
    ) -> tuple[Iterator[bytes], str]:
        cp = (call_log_row.get("voicemail_cp_url") or "").strip()
        authed = (call_log_row.get("voicemail_url") or "").strip()
        # Try cp_url first (no Bearer needed), fall back to authed url on
        # 4xx (cp_urls are presigned + short-lived).
        if cp:
            r = self._try_stream(cp, requires_auth=False)
            if r is not None:
                return r
        if authed:
            return self.stream_url(authed, requires_auth=True)
        raise PhoneComAPIError("voicemail row has no playable url")

    def stream_call_recording(
        self, call_log_row: dict[str, Any]
    ) -> tuple[Iterator[bytes], str] | None:
        cp = (call_log_row.get("call_recording_cp_url") or "").strip()
        authed = (call_log_row.get("call_recording_url") or "").strip()
        if not cp and not authed:
            return None
        if cp:
            r = self._try_stream(cp, requires_auth=False)
            if r is not None:
                return r
        if authed:
            return self.stream_url(authed, requires_auth=True)
        return None

    # ── pc-s5: webhook (callback + listener) management ─────────────────

    _RESERVED_HOST_SUBSTRING = "tools.phone.com"

    def _callbacks_path(self, voip_id: int) -> str:
        return f"/accounts/{voip_id}/integrations/events/callbacks"

    def _listeners_path(self, voip_id: int) -> str:
        return f"/accounts/{voip_id}/integrations/events/listeners"

    def register_callback(
        self,
        *,
        voip_id: int | None = None,
        name: str,
        url: str,
        method: str = "POST",
        timeout: int = 10,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        payload = {
            "name": name,
            "mode": "HTTPS",
            "config": {
                "url": url,
                "method": method,
                "headers": None,
                "timeout": timeout,
            },
        }
        resp = self._request_with_retry("POST", self._callbacks_path(vid), json=payload)
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com register_callback {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    def list_callbacks(
        self, *, voip_id: int | None = None, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        return self._get_paginated(
            self._callbacks_path(vid), {"limit": limit, "offset": offset}
        )

    def delete_callback(self, *, voip_id: int | None = None, callback_id: int) -> None:
        vid = self._resolve_voip_id(voip_id)
        resp = self._request_with_retry(
            "DELETE", f"{self._callbacks_path(vid)}/{callback_id}",
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com delete_callback {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )

    def create_listener(
        self,
        *,
        voip_id: int | None = None,
        callback_id: int,
        version: str = "1.0.0",
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        resp = self._request_with_retry(
            "POST",
            self._listeners_path(vid),
            json={"callback_id": callback_id, "version": version},
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com create_listener {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    def list_listeners(
        self, *, voip_id: int | None = None, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        return self._get_paginated(
            self._listeners_path(vid), {"limit": limit, "offset": offset}
        )

    def delete_listener(self, *, voip_id: int | None = None, listener_id: int) -> None:
        vid = self._resolve_voip_id(voip_id)
        resp = self._request_with_retry(
            "DELETE", f"{self._listeners_path(vid)}/{listener_id}",
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com delete_listener {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )

    # ── P1.2: PATCH callbacks/listeners in place ─────────────────────────

    def patch_callback(
        self,
        *,
        voip_id: int | None = None,
        callback_id: int,
        url: str | None = None,
        method: str | None = None,
        timeout: int | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """PATCH a callback's config without delete+recreate. Preserves the
        callback_id so the listener binding survives a URL/secret rotation."""
        vid = self._resolve_voip_id(voip_id)
        cfg: dict[str, Any] = {}
        if url is not None:
            cfg["url"] = url
        if method is not None:
            cfg["method"] = method
        if timeout is not None:
            cfg["timeout"] = timeout
        payload: dict[str, Any] = {}
        if cfg:
            payload["config"] = cfg
        if enabled is not None:
            payload["enabled"] = enabled
        if not payload:
            raise PhoneComAPIError("patch_callback: nothing to patch")
        resp = self._request_with_retry(
            "PATCH", f"{self._callbacks_path(vid)}/{callback_id}", json=payload,
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com patch_callback {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    def patch_listener(
        self,
        *,
        voip_id: int | None = None,
        listener_id: int,
        version: str | None = None,
        callback_id: int | None = None,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        payload: dict[str, Any] = {}
        if version is not None:
            payload["version"] = version
        if callback_id is not None:
            payload["callback_id"] = callback_id
        if not payload:
            raise PhoneComAPIError("patch_listener: nothing to patch")
        resp = self._request_with_retry(
            "PATCH", f"{self._listeners_path(vid)}/{listener_id}", json=payload,
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com patch_listener {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    # ── P1.1: listener filters ──────────────────────────────────────────

    # We only care about phone.call, phone.message, phone.voicemail, phone.fax
    # (P2.7 adds fax). Filtering at the edge cuts webhook traffic for everything
    # else (account.updated, extension.changed, etc.).
    DEFAULT_LISTENER_EVENT_TYPES = (
        "phone.call",
        "phone.message",
        "phone.voicemail",
        "phone.fax",
    )

    def _filters_path(self, voip_id: int, listener_id: int) -> str:
        return f"/accounts/{voip_id}/integrations/events/listeners/{listener_id}/filters"

    def list_listener_filters(
        self, *, voip_id: int | None = None, listener_id: int,
        limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        return self._get_paginated(
            self._filters_path(vid, listener_id), {"limit": limit, "offset": offset},
        )

    def create_listener_filter(
        self,
        *,
        voip_id: int | None = None,
        listener_id: int,
        field: str,
        operator: str = "in",
        value: Any,
    ) -> dict[str, Any]:
        """Create a filter on a listener. Phone.com's filter shape:
        ``{"field": "type", "operator": "in", "value": [...]}``.
        """
        vid = self._resolve_voip_id(voip_id)
        payload = {"field": field, "operator": operator, "value": value}
        resp = self._request_with_retry(
            "POST", self._filters_path(vid, listener_id), json=payload,
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com create_listener_filter {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )
        return resp.json()

    def delete_listener_filter(
        self, *, voip_id: int | None = None, listener_id: int, filter_id: int,
    ) -> None:
        vid = self._resolve_voip_id(voip_id)
        resp = self._request_with_retry(
            "DELETE", f"{self._filters_path(vid, listener_id)}/{filter_id}",
        )
        if not resp.is_success:
            raise PhoneComAPIError(
                f"phone_com delete_listener_filter {resp.status_code}",
                status_code=resp.status_code,
                body_snippet=resp.text[:500],
            )

    def ensure_listener_event_filter(
        self,
        *,
        voip_id: int | None = None,
        listener_id: int,
        event_types: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, Any]:
        """Idempotent: ensure exactly one ``field=type, operator=in`` filter
        exists on this listener with the given event_types. If a matching
        filter already exists we do nothing; otherwise we drop any other
        ``type`` filters and create the canonical one.

        Returns ``{"filter_id": int, "created": bool, "event_types": [...]}``.
        """
        vid = self._resolve_voip_id(voip_id)
        wanted = list(event_types or self.DEFAULT_LISTENER_EVENT_TYPES)
        existing = self.list_listener_filters(voip_id=vid, listener_id=listener_id)
        match_id: int | None = None
        stale_ids: list[int] = []
        for f in (existing.get("items") or []):
            if f.get("field") == "type":
                if (
                    f.get("operator") == "in"
                    and sorted(f.get("value") or []) == sorted(wanted)
                ):
                    match_id = f.get("id")
                else:
                    stale_ids.append(f.get("id"))
        if match_id is not None:
            for sid in stale_ids:
                with contextlib.suppress(PhoneComAPIError):
                    self.delete_listener_filter(
                        voip_id=vid, listener_id=listener_id, filter_id=sid,
                    )
            return {"filter_id": match_id, "created": False, "event_types": wanted}
        for sid in stale_ids:
            with contextlib.suppress(PhoneComAPIError):
                self.delete_listener_filter(
                    voip_id=vid, listener_id=listener_id, filter_id=sid,
                )
        created = self.create_listener_filter(
            voip_id=vid, listener_id=listener_id,
            field="type", operator="in", value=wanted,
        )
        return {
            "filter_id": created.get("id"),
            "created": True,
            "event_types": wanted,
        }

    def ensure_webhook(
        self,
        *,
        voip_id: int | None = None,
        name: str,
        url: str,
        event_types: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, Any]:
        if self._RESERVED_HOST_SUBSTRING in url.lower():
            raise PhoneComAPIError(
                f"refusing to register callback under {self._RESERVED_HOST_SUBSTRING} — reserved"
            )
        vid = self._resolve_voip_id(voip_id)
        existing = self.list_callbacks(voip_id=vid)
        for cb in existing.get("items") or []:
            cfg = cb.get("config") or {}
            if cfg.get("url") == url and cb.get("enabled", True):
                listeners = self.list_listeners(voip_id=vid)
                listener_id = next(
                    (
                        listener.get("id")
                        for listener in (listeners.get("items") or [])
                        if listener.get("callback_id") == cb["id"]
                    ),
                    None,
                )
                filter_state: dict[str, Any] | None = None
                if listener_id is not None:
                    try:
                        filter_state = self.ensure_listener_event_filter(
                            voip_id=vid,
                            listener_id=listener_id,
                            event_types=event_types,
                        )
                    except Exception as exc:  # noqa: BLE001 — best-effort
                        log.warning("ensure_listener_event_filter skipped: %s", exc)
                return {
                    "callback_id": cb["id"],
                    "listener_id": listener_id,
                    "created": False,
                    "filter": filter_state,
                }
        new_cb = self.register_callback(voip_id=vid, name=name, url=url)
        new_listener = self.create_listener(voip_id=vid, callback_id=new_cb["id"])
        filter_state = None
        try:
            filter_state = self.ensure_listener_event_filter(
                voip_id=vid,
                listener_id=new_listener["id"],
                event_types=event_types,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning("ensure_listener_event_filter skipped: %s", exc)
        return {
            "callback_id": new_cb["id"],
            "listener_id": new_listener["id"],
            "created": True,
            "filter": filter_state,
        }

    def disconnect_webhook(
        self, *, voip_id: int | None = None, callback_id: int,
    ) -> dict[str, Any]:
        vid = self._resolve_voip_id(voip_id)
        callbacks = self.list_callbacks(voip_id=vid)
        target = next(
            (cb for cb in (callbacks.get("items") or []) if cb.get("id") == callback_id),
            None,
        )
        if target:
            cfg_url = ((target.get("config") or {}).get("url") or "").lower()
            if self._RESERVED_HOST_SUBSTRING in cfg_url:
                raise PhoneComAPIError(
                    f"refusing to delete callback under {self._RESERVED_HOST_SUBSTRING} — reserved"
                )
        listeners = self.list_listeners(voip_id=vid)
        deleted_listener_ids: list[int] = []
        for listener in (listeners.get("items") or []):
            if listener.get("callback_id") == callback_id:
                self.delete_listener(voip_id=vid, listener_id=listener["id"])
                deleted_listener_ids.append(listener["id"])
        self.delete_callback(voip_id=vid, callback_id=callback_id)
        return {
            "deleted_listeners": deleted_listener_ids,
            "deleted_callback": callback_id,
        }

    def paginate(
        self,
        method: Callable[..., dict[str, Any]],
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        """Walk offset through total for any list_* method. Cap 10000."""
        kwargs.setdefault("limit", 50)
        kwargs.setdefault("offset", 0)
        seen = 0
        while True:
            page = method(**kwargs)
            items = page.get("items") or []
            total = page.get("total") or 0
            if total > self._PAGINATE_HARD_CAP:
                raise PhoneComAPIError(
                    f"phone_com paginate safety cap: total={total} > {self._PAGINATE_HARD_CAP}",
                )
            if not items:
                return
            for item in items:
                yield item
                seen += 1
                if seen >= self._PAGINATE_HARD_CAP:
                    raise PhoneComAPIError(
                        f"phone_com paginate safety cap: yielded {seen} items",
                    )
            kwargs["offset"] = kwargs["offset"] + len(items)
            if kwargs["offset"] >= total:
                return
