"""Banno Consumer API client (sync httpx).

Auth: a ``token_provider`` callable supplies the current access token and
performs a locked refresh when needed — Banno access tokens live 600
seconds, shorter than a real backfill, so the client re-acquires the token
before requests near expiry and once after any 401 (audited plan B2).

Retry policy: jittered exponential retry (≤3) on connect errors and 5xx,
adapted from ``phone_com.client._request_with_retry``. DELIBERATE
divergence: HTTP 429 raises ``BannoRateLimitError`` immediately — the
Celery task layer owns rate-limit backoff so a 429 can't silently burn
the in-request retry budget (QB precedent).

Pagination: offset pages OVERLAP by ``PAGE_OVERLAP`` rows and backfill
windows overlap by a day (service layer) — a transaction inactivated mid-
pagination shifts rows across page boundaries, and idempotent upserts
make the re-delivered overlap free (audited plan S7).
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Iterator

import httpx

from gdx_dispatch.core.ssrf_guard import validate_outbound_url

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
PAGE_LIMIT = 500
PAGE_OVERLAP = 50
RETRY_MAX_ATTEMPTS = 3

TokenProvider = Callable[..., str]  # kwargs: stale_token: str | None


class BannoAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, body_snippet: str | None = None):
        self.status_code = status_code
        self.body_snippet = body_snippet
        super().__init__(message)


class BannoRateLimitError(BannoAPIError):
    """HTTP 429 — task layer owns the backoff."""


class BannoAuthError(BannoAPIError):
    """401 after a refresh-once retry."""


class BannoDocumentsUnavailable(BannoAPIError):
    """Documents ability disabled / user not enrolled (HTTP 403)."""


class BannoClient:
    def __init__(
        self,
        fi_host: str,
        token_provider: TokenProvider,
        *,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ):
        validate_outbound_url(f"https://{fi_host}/")
        self.fi_host = fi_host
        self._token_provider = token_provider
        self._token = token_provider()
        self._base = f"https://{fi_host}/a/consumer/api/v0"
        self._client = httpx.Client(base_url=self._base, timeout=timeout)

    def __enter__(self) -> BannoClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── request plumbing ───────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _backoff_seconds(self, attempt: int) -> float:
        delay = min(0.5 * (2**attempt), 30.0) + random.uniform(-0.25, 0.25)  # noqa: S311 — retry jitter, not crypto
        return max(0.01, delay)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        refreshed_once = False
        attempt = 0
        while True:
            try:
                resp = self._client.request(method, path, headers=self._headers(), **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt >= RETRY_MAX_ATTEMPTS:
                    raise BannoAPIError(f"network failure for {method} {path}") from exc
                log.warning(
                    "banno retry attempt=%d/%d %s %s err=%s",
                    attempt + 1, RETRY_MAX_ATTEMPTS, method, path, type(exc).__name__,
                )
                time.sleep(self._backoff_seconds(attempt))
                attempt += 1
                continue

            if resp.status_code == 429:
                raise BannoRateLimitError(
                    f"rate limited: {method} {path}", status_code=429,
                    body_snippet=resp.text[:200],
                )
            if resp.status_code == 401:
                # Token may have expired mid-run (600s lifetime) or been
                # revoked. Refresh once through the locked provider path,
                # then retry the request exactly once.
                if refreshed_once:
                    raise BannoAuthError(
                        f"unauthorized after refresh: {method} {path}", status_code=401,
                    )
                log.info("banno_401_refreshing_token host=%s path=%s", self.fi_host, path)
                self._token = self._token_provider(stale_token=self._token)
                refreshed_once = True
                continue
            if 500 <= resp.status_code < 600 and attempt < RETRY_MAX_ATTEMPTS:
                log.warning(
                    "banno retry attempt=%d/%d %s %s status=%d",
                    attempt + 1, RETRY_MAX_ATTEMPTS, method, path, resp.status_code,
                )
                time.sleep(self._backoff_seconds(attempt))
                attempt += 1
                continue
            return resp

    def _get_json(self, path: str, *, params: Any = None) -> dict:
        resp = self._request("GET", path, params=params)
        if not resp.is_success:
            raise BannoAPIError(
                f"GET {path} -> HTTP {resp.status_code}",
                status_code=resp.status_code, body_snippet=resp.text[:200],
            )
        data = resp.json()
        if not isinstance(data, dict):
            raise BannoAPIError(f"GET {path} returned non-object JSON")
        return data

    # Ensure the token used for long gaps between calls is still valid —
    # the provider fast-paths when it is, so this is one DB read when fresh.
    def refresh_token_if_needed(self) -> None:
        self._token = self._token_provider()

    # ── accounts + transactions ────────────────────────────────────────

    def get_accounts(self, user_id: str) -> dict:
        """Returns {"accounts": [...], "inactivatedAccountIds": [...]}."""
        return self._get_json(f"/users/{user_id}/accounts")

    def get_transactions_page(
        self,
        user_id: str,
        account_id: str,
        *,
        offset: int = 0,
        limit: int = PAGE_LIMIT,
        since: str | None = None,
        until: str | None = None,
        updated_since: str | None = None,
    ) -> dict:
        if updated_since and (since or until):
            raise ValueError("updatedSince cannot be combined with since/until")
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if updated_since:
            params["updatedSince"] = updated_since
        return self._get_json(
            f"/users/{user_id}/accounts/{account_id}/transactions", params=params
        )

    def iter_transaction_pages(
        self,
        user_id: str,
        account_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        updated_since: str | None = None,
    ) -> Iterator[dict]:
        """Yields raw page dicts (transactions + inactivatedTransactionIds).

        Offset advances by ``PAGE_LIMIT - PAGE_OVERLAP`` so a row shifted
        across a page boundary by a concurrent mutation is still seen.
        Terminates on the first short page.
        """
        offset = 0
        while True:
            self.refresh_token_if_needed()
            page = self.get_transactions_page(
                user_id, account_id,
                offset=offset, limit=PAGE_LIMIT,
                since=since, until=until, updated_since=updated_since,
            )
            yield page
            txns = page.get("transactions") or []
            if len(txns) < PAGE_LIMIT:
                return
            offset += PAGE_LIMIT - PAGE_OVERLAP

    # ── on-demand core refresh ─────────────────────────────────────────

    def trigger_fetch(self, user_id: str) -> str | None:
        resp = self._request("PUT", f"/users/{user_id}/fetch")
        if not resp.is_success:
            log.warning("banno_fetch_trigger_failed status=%d", resp.status_code)
            return None
        try:
            return str(resp.json().get("taskId") or "") or None
        except Exception:  # noqa: BLE001
            return None

    def wait_for_fetch(
        self, user_id: str, task_id: str, *, timeout_s: float = 30.0, poll_interval_s: float = 2.0
    ) -> bool:
        """Poll the task until TaskEnded. Timeout → False; the sync proceeds
        with cached data — a fetch-poll timeout must never fail a sync."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                data = self._get_json(f"/users/{user_id}/tasks/{task_id}")
            except BannoAPIError:
                return False
            for event in data.get("events") or []:
                if str(event.get("type") or "").endswith("TaskEnded"):
                    return True
            time.sleep(poll_interval_s)
        return False

    # ── documents (statements) ─────────────────────────────────────────

    def get_documents_institution_settings(self, user_id: str) -> dict:
        """Institution documents settings. Raises BannoDocumentsUnavailable
        on 403 (ability off / user not enrolled)."""
        resp = self._request("GET", f"/users/{user_id}/documents/settings/institution")
        if resp.status_code == 403:
            raise BannoDocumentsUnavailable(
                "documents not available for this user/institution", status_code=403
            )
        if not resp.is_success:
            raise BannoAPIError(
                f"documents settings -> HTTP {resp.status_code}",
                status_code=resp.status_code, body_snippet=resp.text[:200],
            )
        data = resp.json()
        settings = data.get("settings") if isinstance(data, dict) else None
        if not isinstance(settings, dict):
            raise BannoAPIError("documents settings response malformed")
        return settings

    def list_documents(
        self,
        user_id: str,
        *,
        start_date: str,
        end_date: str,
        account_ids: list[str] | None = None,
    ) -> list[dict]:
        """List document metadata. Default is UNFILTERED (captures
        user-scoped docs with empty accountIds); pass account_ids only when
        the unfiltered call 400s on >maximumConcurrentAccounts accounts."""
        params: list[tuple[str, str]] = [("startDate", start_date), ("endDate", end_date)]
        for acct in account_ids or []:
            params.append(("accountId", acct))
        resp = self._request("GET", f"/users/{user_id}/documents/all", params=params)
        if resp.status_code == 403:
            raise BannoDocumentsUnavailable("documents listing forbidden", status_code=403)
        if not resp.is_success:
            raise BannoAPIError(
                f"documents list -> HTTP {resp.status_code}",
                status_code=resp.status_code, body_snippet=resp.text[:200],
            )
        data = resp.json()
        docs = data.get("documents") if isinstance(data, dict) else None
        return docs if isinstance(docs, list) else []

    def download_document(self, user_id: str, document_id: str) -> tuple[bytes, str]:
        """Returns (pdf_bytes, content_type)."""
        resp = self._request("GET", f"/users/{user_id}/documents/{document_id}")
        if not resp.is_success:
            raise BannoAPIError(
                f"document download -> HTTP {resp.status_code}",
                status_code=resp.status_code, body_snippet=resp.text[:200],
            )
        return resp.content, resp.headers.get("content-type", "application/pdf")
