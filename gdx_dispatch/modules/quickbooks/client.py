"""Async httpx client for the QuickBooks Online REST API v3.

Replaces the synchronous python-quickbooks SDK with direct REST calls.
Handles auth headers, minor version, base URL selection, and error mapping.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

# Intuit deprecated minor versions 1-74 on 2025-08-01.
QB_MINOR_VERSION = 75

QB_BASE_URLS = {
    "production": "https://quickbooks.api.intuit.com",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
}

TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


class QBAPIError(Exception):
    """Raised when the QuickBooks API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str, response_body: dict[str, Any] | None = None):
        self.status_code = status_code
        self.detail = detail
        self.response_body = response_body or {}
        super().__init__(f"QB API {status_code}: {detail}")


class QBRateLimitError(QBAPIError):
    """Raised on HTTP 429 — caller should retry with backoff."""

    def __init__(self, detail: str = "Rate limited by QuickBooks"):
        super().__init__(429, detail)


class QBAuthError(QBAPIError):
    """Raised on HTTP 401 — token likely expired or revoked."""

    def __init__(self, detail: str = "QuickBooks authentication failed"):
        super().__init__(401, detail)


class QBClient:
    """Async httpx client for QuickBooks Online API v3.

    Usage::

        async with QBClient(access_token="...", realm_id="123") as qb:
            customers = await qb.query("Customer")
            new_customer = await qb.create("Customer", {"DisplayName": "Alice"})
    """

    def __init__(
        self,
        *,
        access_token: str,
        realm_id: str,
        environment: str | None = None,
        minor_version: int = QB_MINOR_VERSION,
        timeout: float = 30.0,
    ):
        self.access_token = access_token
        self.realm_id = realm_id
        env = environment or os.getenv("QB_ENVIRONMENT", "production")
        self.base_url = QB_BASE_URLS.get(env, QB_BASE_URLS["production"])
        self.minor_version = minor_version
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> QBClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _url(self, entity: str, entity_id: str = "", *, idempotency_key: str | None = None) -> str:
        """Build a QBO API URL. S122-8 (C1): when caller passes
        ``idempotency_key`` it gets appended as the ``requestid`` query param.
        Intuit dedupes server-side on this UUID for ~24h, so Celery retries
        on 5xx or socket timeout no longer create twin entities. The legacy
        kernel set ``Request-Id`` as an HTTP HEADER — which Intuit ignores —
        producing the duplicate-customer / duplicate-payment scars repaired
        by gdx_dispatch/tools/dedupe_qb_customers.py + dedupe_qb_payments.py.
        """
        path = f"/v3/company/{self.realm_id}/{entity.lower()}"
        if entity_id:
            path = f"{path}/{entity_id}"
        url = f"{path}?minorversion={self.minor_version}"
        if idempotency_key:
            url = f"{url}&requestid={quote(idempotency_key, safe='')}"
        return url

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.is_success:
            return
        try:
            body = resp.json()
        except Exception:
            logging.getLogger(__name__).exception("_raise_for_status caught exception")
            body = {}

        detail = ""
        # Intuit error format: {"Fault": {"Error": [{"Message": "...", "Detail": "..."}]}}
        fault = body.get("Fault", {})
        errors = fault.get("Error", [])
        if errors:
            detail = "; ".join(
                f"{e.get('Message', '')}: {e.get('Detail', '')}" for e in errors
            )
        if not detail:
            detail = resp.text[:200]

        if resp.status_code == 429:
            raise QBRateLimitError(detail)
        if resp.status_code == 401:
            raise QBAuthError(detail)
        raise QBAPIError(resp.status_code, detail, body)

    async def query(self, entity: str, where: str = "", max_results: int = 1000) -> list[dict[str, Any]]:
        """Run a QB query and return ALL matching entities, paginating via STARTPOSITION.

        QBO caps a single page at 1000 rows. Pre-fix this method issued one query and
        silently dropped any rows past row 1000 — at scale that meant unsynced
        customers/invoices and counters that lied. Loop until a short page comes back.

        ``max_results`` is the per-page size (capped at 1000 by QBO), not the total.
        """
        page_size = max(1, min(int(max_results), 1000))
        out: list[dict[str, Any]] = []
        start = 1
        while True:
            stmt = f"SELECT * FROM {entity}"
            if where:
                stmt += f" WHERE {where}"
            stmt += f" STARTPOSITION {start} MAXRESULTS {page_size}"

            url = f"/v3/company/{self.realm_id}/query?query={quote(stmt)}&minorversion={self.minor_version}"
            resp = await self._client.get(url)
            self._raise_for_status(resp)

            data = resp.json()
            query_resp = data.get("QueryResponse", {})
            page = query_resp.get(entity, [])
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break
            start += page_size
        return out

    async def create(
        self, entity: str, payload: dict[str, Any], *, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create an entity in QuickBooks. Returns the created entity dict.
        S122-8 (C1): passing ``idempotency_key`` adds ``?requestid=`` so
        Intuit dedupes retries server-side for ~24h.
        """
        resp = await self._client.post(
            self._url(entity, idempotency_key=idempotency_key), json=payload,
        )
        self._raise_for_status(resp)
        data = resp.json()
        return data.get(entity, data)

    async def read(self, entity: str, entity_id: str) -> dict[str, Any]:
        """Read a single entity by ID."""
        resp = await self._client.get(self._url(entity, entity_id))
        self._raise_for_status(resp)
        data = resp.json()
        return data.get(entity, data)

    async def update(
        self, entity: str, payload: dict[str, Any], *, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Update an entity. Payload must include Id and SyncToken.
        S122-8 (C1): see ``create``.
        """
        resp = await self._client.post(
            self._url(entity, idempotency_key=idempotency_key), json=payload,
        )
        self._raise_for_status(resp)
        data = resp.json()
        return data.get(entity, data)

    async def delete(
        self, entity: str, payload: dict[str, Any], *, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Delete (void) an entity. Payload must include Id and SyncToken.
        S122-8 (C1): see ``create``.
        """
        url = self._url(entity, idempotency_key=idempotency_key) + "&operation=delete"
        resp = await self._client.post(url, json=payload)
        self._raise_for_status(resp)
        return resp.json()
