"""Intuit QBO v3 respx replay fixture.

The legacy QB tests build a ``QBClient`` via ``__new__`` and replace ``.query``
and ``.create`` with ``AsyncMock`` — that bypasses the entire httpx layer,
so no test ever exercises URL construction, the ``?requestid=`` idempotency
key, the Intuit ``Fault`` error envelope, HTTP-status → typed-exception
mapping, or pagination. Three audits (deep, third-pass, S122-8) flagged
this as the largest remaining test gap.

This module fixes it by routing a real ``QBClient`` through ``respx`` mock
transport and returning Intuit-shape v3 response bodies. The
``IdempotencyTracker`` mimics Intuit's documented dedup-replay behavior:
a duplicate ``?requestid=`` UUID within ~24h returns the *same* response
body with HTTP 200, not a 4xx. That is exactly the behavior S122-8 relies
on for retry safety — and now we test it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
PROD_BASE = "https://quickbooks.api.intuit.com"
TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

# Intuit sandbox realm IDs are ~16-digit numerics; this is a stable shape,
# not a live realm.
REALM_ID = "9341454816930000"

_RESPONSES_DIR = Path(__file__).parent / "responses"


def load_response(name: str) -> dict[str, Any]:
    """Load a canned Intuit v3 response body by stem (no .json suffix)."""
    return json.loads((_RESPONSES_DIR / f"{name}.json").read_text())


def entity_url(entity: str, realm_id: str = REALM_ID, *, base: str = SANDBOX_BASE) -> str:
    """Build the URL path respx should match (no query string).

    QBClient routes everything through ``/v3/company/<realm>/<entity>``
    with the entity name lower-cased — see ``QBClient._url``.
    """
    return f"{base}/v3/company/{realm_id}/{entity.lower()}"


def query_url(realm_id: str = REALM_ID, *, base: str = SANDBOX_BASE) -> str:
    """The QBO query endpoint (used by ``QBClient.query``)."""
    return f"{base}/v3/company/{realm_id}/query"


class IdempotencyTracker:
    """Mimics Intuit's ``?requestid=`` server-side dedup-replay.

    Intuit caches the response keyed by ``(realm_id, requestid)`` for ~24h.
    Duplicate POSTs in that window return HTTP 200 with the *original* body.
    A request without ``?requestid=`` is never cached.

    Designed so a regression that bypasses cache lookup (or that drops
    ``requestid`` from the URL) is OBSERVABLE in tests. To make replay
    distinguishable from fresh-create, pass ``fresh_bodies=`` with at least
    two distinct response bodies. The first fresh call consumes ``fresh_
    bodies[0]`` and caches it under the requestid; a subsequent call with
    the SAME requestid must return ``fresh_bodies[0]`` again — NOT
    ``fresh_bodies[1]``. If you only pass ``default_body``, both cache-hit
    and cache-miss return the same dict and the replay assertion is
    tautological (auditor 2026-05-12 catch).

    Use as the ``side_effect`` of a respx POST route::

        tracker = IdempotencyTracker(fresh_bodies=[body_a, body_b])
        router.post(...).mock(side_effect=tracker.respond)
    """

    def __init__(
        self,
        *,
        fresh_bodies: list[dict[str, Any]] | None = None,
        default_body: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> None:
        self._cache: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self._fresh_bodies = list(fresh_bodies) if fresh_bodies else []
        self._default_body = default_body or {}
        self._status_code = status_code
        self._fresh_index = 0

    @staticmethod
    def request_id(request: httpx.Request) -> str | None:
        params = parse_qs(urlparse(str(request.url)).query)
        values = params.get("requestid") or []
        return values[0] if values else None

    def respond(self, request: httpx.Request) -> httpx.Response:
        rid = self.request_id(request)
        try:
            body = json.loads(request.content.decode() or "{}")
        except Exception:
            body = {}
        self.calls.append({"request_id": rid, "body": body, "url": str(request.url)})

        if rid and rid in self._cache:
            # Idempotency replay: return the SAME body the requestid first saw.
            return httpx.Response(200, json=self._cache[rid])

        # Fresh request. Consume the next queued body, or fall back to default.
        if self._fresh_bodies:
            idx = min(self._fresh_index, len(self._fresh_bodies) - 1)
            fresh_body = self._fresh_bodies[idx]
            self._fresh_index += 1
        else:
            fresh_body = self._default_body

        if rid:
            self._cache[rid] = fresh_body
        return httpx.Response(self._status_code, json=fresh_body)


def fault_response(name: str, status_code: int) -> httpx.Response:
    """Build a typed httpx.Response from a canned Intuit Fault fixture."""
    return httpx.Response(status_code, json=load_response(name))
