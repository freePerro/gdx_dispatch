"""Integration tests for the QuickBooks httpx stack — replays Intuit v3
sandbox responses through respx so the *real* QBClient code runs.

Previous tests built ``QBClient`` via ``__new__`` and AsyncMock'd ``.query`` /
``.create``, which never exercised URL construction, the ``?requestid=``
idempotency key, the Intuit ``Fault`` envelope, the HTTP-status → typed-
exception mapping, or pagination through ``STARTPOSITION``. Three audits
flagged that gap; this file closes it.

The fixture corpus under ``gdx_dispatch/tests/fixtures/qb_intuit/`` mirrors Intuit's
documented v3 response shapes (Customer/Invoice/Payment/Item, Fault 429/
401-3200/400-6240/5010, OAuth refresh). The IdempotencyTracker simulates
Intuit's documented dedup-replay: duplicate ``?requestid=`` returns the
*same* response body with HTTP 200.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from gdx_dispatch.modules.quickbooks.client import (
    QBAPIError,
    QBAuthError,
    QBClient,
    QBRateLimitError,
)
from gdx_dispatch.modules.quickbooks.oauth import refresh_access_token
from gdx_dispatch.modules.quickbooks.sync import _extract_line_subtotal, _extract_tax_amount
from gdx_dispatch.tests.fixtures.qb_intuit.intuit_replay import (
    REALM_ID,
    SANDBOX_BASE,
    TOKEN_ENDPOINT,
    IdempotencyTracker,
    entity_url,
    fault_response,
    load_response,
    query_url,
)


def _run(coro):
    """Match the existing test file's asyncio.run wrapper pattern."""
    return asyncio.run(coro)


def _build_client() -> QBClient:
    return QBClient(
        access_token="sandbox-access-token",
        realm_id=REALM_ID,
        environment="sandbox",
    )


# ---------------------------------------------------------------------------
# URL construction — minorversion + requestid
# ---------------------------------------------------------------------------


def test_create_url_includes_minorversion_and_requestid():
    """S122-8 invariant: every POST mutation must carry both ``minorversion``
    and ``requestid``. The legacy ``Request-Id`` header form was silently
    ignored by Intuit — see ``QBClient._url`` docstring + the dedupe tools.

    Asserts on exact equality (not key-presence), so a regression that
    drops the requestid append fails with a clear message instead of a
    bare ``KeyError`` a future maintainer might "fix" with ``.get()``.
    """
    tracker = IdempotencyTracker(default_body=load_response("customer_create"))
    expected_rid = "11111111-2222-3333-4444-555555555555"

    with respx.mock(assert_all_called=False) as router:
        route = router.post(url__regex=rf"^{entity_url('Customer')}\?.*").mock(
            side_effect=tracker.respond,
        )

        async def go():
            async with _build_client() as qb:
                return await qb.create(
                    "Customer",
                    {"DisplayName": "New Customer"},
                    idempotency_key=expected_rid,
                )

        out = _run(go())

    assert route.called, "respx never saw the POST — URL match failed"
    assert out["Id"] == "100"
    captured = tracker.calls[0]
    parsed_query = parse_qs(urlparse(captured["url"]).query)
    minor = parsed_query.get("minorversion", [None])[0]
    rid = parsed_query.get("requestid", [None])[0]
    assert minor == "75", f"minorversion missing or wrong: {minor!r}"
    assert rid == expected_rid, f"requestid not appended to URL: {rid!r}"


def test_create_without_idempotency_key_omits_requestid():
    """When the caller doesn't pass an idempotency_key, no requestid is sent.
    Callers that don't need dedup (or that don't have a stable key yet)
    must not get a server-side dedup token by accident.
    """
    tracker = IdempotencyTracker(default_body=load_response("customer_create"))

    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=rf"^{entity_url('Customer')}\?.*").mock(
            side_effect=tracker.respond,
        )

        async def go():
            async with _build_client() as qb:
                return await qb.create("Customer", {"DisplayName": "New"})

        _run(go())

    parsed_query = parse_qs(urlparse(tracker.calls[0]["url"]).query)
    assert "minorversion" in parsed_query
    assert "requestid" not in parsed_query


# ---------------------------------------------------------------------------
# Idempotency replay — duplicate requestid returns same Id
# ---------------------------------------------------------------------------


def test_idempotency_replay_returns_same_body_on_duplicate_requestid():
    """Documented Intuit behavior: a POST with a previously seen
    ``?requestid=`` returns HTTP 200 with the *original* response body.
    This is what S122-8 relies on for retry safety — a Celery retry
    after a socket-timeout must NOT create a twin entity.

    The tracker is seeded with TWO distinct fresh bodies. A correctly
    working idempotency layer must return ``Id=100`` on both POSTs
    (cache hit on the second). If the cache were bypassed — e.g., the
    URL stopped carrying ``?requestid=`` or the tracker stopped
    looking up rid — the second call would consume the next fresh body
    and return ``Id=999``. That observable difference is what gates
    this test against regression (auditor 2026-05-12 catch:
    same-dict-on-both-paths made the assertion tautological).
    """
    body_first = load_response("customer_create")
    body_second_if_fresh = {
        "Customer": {**body_first["Customer"], "Id": "999"},
        "time": body_first["time"],
    }
    tracker = IdempotencyTracker(fresh_bodies=[body_first, body_second_if_fresh])
    key = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=rf"^{entity_url('Customer')}\?.*").mock(
            side_effect=tracker.respond,
        )

        async def go():
            async with _build_client() as qb:
                first = await qb.create(
                    "Customer", {"DisplayName": "Dup"}, idempotency_key=key,
                )
                second = await qb.create(
                    "Customer", {"DisplayName": "Dup"}, idempotency_key=key,
                )
                return first, second

        first, second = _run(go())

    assert first["Id"] == "100", "first POST should return body_first"
    assert second["Id"] == "100", (
        "second POST with same requestid must REPLAY body_first (Id=100), not "
        f"consume body_second_if_fresh (Id=999). Got Id={second['Id']!r} — "
        "cache lookup or ?requestid= URL append likely broken."
    )
    assert len(tracker.calls) == 2
    assert tracker.calls[0]["request_id"] == key
    assert tracker.calls[1]["request_id"] == key
    # body_second_if_fresh stays unused because cache hit short-circuits.
    assert tracker._fresh_index == 1


def test_distinct_requestids_get_independent_responses():
    """Two distinct requestids must not collide in the dedup cache. Also
    asserts each captured URL carries its requestid — otherwise the test
    could pass under the mutation where ``?requestid=`` is dropped (the
    side-effect would still deliver sequential bodies, and a naive
    Id-only assertion would never notice).
    """
    body_a = {**load_response("customer_create")}
    body_a["Customer"] = {**body_a["Customer"], "Id": "201"}
    body_b = {**load_response("customer_create")}
    body_b["Customer"] = {**body_b["Customer"], "Id": "202"}

    tracker = IdempotencyTracker(fresh_bodies=[body_a, body_b])

    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=rf"^{entity_url('Customer')}\?.*").mock(side_effect=tracker.respond)

        async def go():
            async with _build_client() as qb:
                a = await qb.create("Customer", {"DisplayName": "A"}, idempotency_key="k-1")
                b = await qb.create("Customer", {"DisplayName": "B"}, idempotency_key="k-2")
                return a, b

        a, b = _run(go())

    assert a["Id"] == "201"
    assert b["Id"] == "202"
    # Each captured request must carry its requestid on the URL. Without
    # this, the test would still pass under the requestid-dropped mutation
    # because the IdempotencyTracker would happily walk the fresh_bodies
    # queue without ever consulting its cache.
    assert tracker.calls[0]["request_id"] == "k-1", (
        f"first POST missing ?requestid=k-1: {tracker.calls[0]['url']}"
    )
    assert tracker.calls[1]["request_id"] == "k-2", (
        f"second POST missing ?requestid=k-2: {tracker.calls[1]['url']}"
    )


# ---------------------------------------------------------------------------
# Response unwrap — Intuit envelope shapes
# ---------------------------------------------------------------------------


def test_create_response_unwrap_returns_entity_dict():
    """Intuit wraps single-entity creates as ``{Customer: {...}}``. QBClient
    must return the inner entity dict, not the envelope.
    """
    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=rf"^{entity_url('Customer')}\?.*").mock(
            return_value=httpx.Response(200, json=load_response("customer_create")),
        )

        async def go():
            async with _build_client() as qb:
                return await qb.create("Customer", {"DisplayName": "New"})

        out = _run(go())

    assert out["Id"] == "100"
    assert out["DisplayName"] == "New Customer"
    # Envelope key shouldn't leak through.
    assert "Customer" not in out


def test_query_unwrap_returns_entity_list():
    """Queries come back as ``{QueryResponse: {Customer: [...]}}``. QBClient
    must yield the inner list. Pagination breaks after a short page
    (2 rows < page_size 1000), so a single GET is sufficient — providing
    a second mock response would be dead scaffolding.
    """
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__regex=rf"^{query_url()}\?.*").mock(
            return_value=httpx.Response(200, json=load_response("customer_query")),
        )

        async def go():
            async with _build_client() as qb:
                return await qb.query("Customer")

        rows = _run(go())

    assert route.call_count == 1
    assert len(rows) == 2
    assert rows[0]["Id"] == "1"
    assert rows[0]["DisplayName"] == "Amy's Bird Sanctuary"
    assert rows[1]["Id"] == "2"


def test_query_paginates_until_short_page():
    """QBO caps a single page at 1000 rows. ``QBClient.query`` must loop
    through ``STARTPOSITION`` until a short page comes back. Pre-fix code
    silently dropped rows past 1000.
    """
    page_1 = {
        "QueryResponse": {
            "Customer": [
                {"Id": str(i), "DisplayName": f"Cust {i}", "Active": True}
                for i in range(1, 1001)
            ],
            "startPosition": 1,
            "maxResults": 1000,
        },
    }
    page_2 = {
        "QueryResponse": {
            "Customer": [{"Id": "1001", "DisplayName": "Cust 1001", "Active": True}],
            "startPosition": 1001,
            "maxResults": 1,
        },
    }

    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__regex=rf"^{query_url()}\?.*").mock(
            side_effect=[
                httpx.Response(200, json=page_1),
                httpx.Response(200, json=page_2),
            ],
        )

        async def go():
            async with _build_client() as qb:
                return await qb.query("Customer")

        rows = _run(go())

    assert len(rows) == 1001
    assert route.call_count == 2
    # First call STARTPOSITION 1, second STARTPOSITION 1001.
    first_query = parse_qs(urlparse(str(route.calls[0].request.url)).query)["query"][0]
    second_query = parse_qs(urlparse(str(route.calls[1].request.url)).query)["query"][0]
    assert "STARTPOSITION 1 " in first_query
    assert "STARTPOSITION 1001 " in second_query


def test_query_empty_page_breaks_loop_cleanly():
    """Empty first page → empty list, single HTTP call."""
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__regex=rf"^{query_url()}\?.*").mock(
            return_value=httpx.Response(200, json=load_response("customer_query_empty")),
        )

        async def go():
            async with _build_client() as qb:
                return await qb.query("Customer")

        rows = _run(go())

    assert rows == []
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Error envelope mapping — HTTP status → typed exception
# ---------------------------------------------------------------------------


def test_429_raises_QBRateLimitError_with_throttle_detail():
    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=rf"^{entity_url('Customer')}\?.*").mock(
            return_value=fault_response("fault_429_throttle", 429),
        )

        async def go():
            async with _build_client() as qb:
                return await qb.create("Customer", {"DisplayName": "Throttled"})

        with pytest.raises(QBRateLimitError) as exc_info:
            _run(go())

    assert exc_info.value.status_code == 429
    assert "ThrottleExceeded" in str(exc_info.value)


def test_401_token_expired_raises_QBAuthError_with_3200_detail():
    """Intuit token-expired returns HTTP 401 with Fault.Error[].code=3200.
    QBClient maps 401 → QBAuthError; the 3200 detail flows through so
    the caller can route to a re-auth prompt vs a transient retry.
    """
    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=rf"^{entity_url('Customer')}\?.*").mock(
            return_value=fault_response("fault_401_token_expired", 401),
        )

        async def go():
            async with _build_client() as qb:
                return await qb.create("Customer", {"DisplayName": "Expired"})

        with pytest.raises(QBAuthError) as exc_info:
            _run(go())

    assert exc_info.value.status_code == 401
    assert "Token expired" in str(exc_info.value)


def test_400_duplicate_name_raises_QBAPIError_with_6240_detail():
    """Intuit duplicate-name validation returns HTTP 400 with code 6240.
    QBClient maps 400 → generic ``QBAPIError`` (callers branch on the
    detail string for now; F-13 in Phase 3 splits this into a typed
    error router).
    """
    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=rf"^{entity_url('Customer')}\?.*").mock(
            return_value=fault_response("fault_400_duplicate_name", 400),
        )

        async def go():
            async with _build_client() as qb:
                return await qb.create("Customer", {"DisplayName": "Existing"})

        with pytest.raises(QBAPIError) as exc_info:
            _run(go())

    assert not isinstance(exc_info.value, (QBAuthError, QBRateLimitError))
    assert exc_info.value.status_code == 400
    assert "Duplicate Name" in str(exc_info.value)


def test_400_stale_object_raises_QBAPIError_with_5010_detail():
    """5010 = SyncToken stale (concurrent edit). Re-add a proper retry-with-
    refetch path when the first update operation lands (S122-6 stayed
    DROPPED in Phase 1 because modular code has zero update calls today).
    Until then the test pins the current behavior: surfaces as QBAPIError
    with the 5010 detail so callers can branch on it.
    """
    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=rf"^{entity_url('Customer')}\?.*").mock(
            return_value=fault_response("fault_400_stale_object", 400),
        )

        async def go():
            async with _build_client() as qb:
                return await qb.update(
                    "Customer",
                    {"Id": "1", "SyncToken": "0", "DisplayName": "Stale"},
                )

        with pytest.raises(QBAPIError) as exc_info:
            _run(go())

    assert exc_info.value.status_code == 400
    assert "Stale Object" in str(exc_info.value)


def test_fault_envelope_missing_falls_back_to_response_text():
    """Some Intuit error responses arrive as HTML or non-JSON. ``_raise_for_
    status`` falls back to the first 200 chars of ``resp.text`` so the
    caller still gets *something* readable.
    """
    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=rf"^{entity_url('Customer')}\?.*").mock(
            return_value=httpx.Response(503, text="Service Unavailable: gateway"),
        )

        async def go():
            async with _build_client() as qb:
                return await qb.create("Customer", {"DisplayName": "x"})

        with pytest.raises(QBAPIError) as exc_info:
            _run(go())

    assert exc_info.value.status_code == 503
    assert "Service Unavailable" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Invoice tax extraction — proves the wire-shape -> domain-value path
# ---------------------------------------------------------------------------


def test_pull_invoice_tax_amount_parsed_from_TxnTaxDetail():
    """S122-2 (T3): Intuit puts invoice tax at ``TxnTaxDetail.TotalTax``,
    not on individual lines. Pre-fix code parsed line.SalesItemLineDetail.
    TaxCodeRef (no amount there) and stored 0 — the GDX-18 NULL-tax scar.

    This test pulls the invoice through the real httpx stack, then
    feeds the row through ``_extract_tax_amount`` to prove the extracted
    field flows through.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=rf"^{query_url()}\?.*").mock(
            return_value=httpx.Response(200, json=load_response("invoice_query_with_tax")),
        )

        async def go():
            async with _build_client() as qb:
                return await qb.query("Invoice")

        rows = _run(go())

    assert len(rows) == 1
    raw = rows[0]
    assert raw["TxnTaxDetail"]["TotalTax"] == 10.00
    assert _extract_tax_amount(raw) == Decimal("10")


def test_pull_invoice_subtotal_excludes_discount_and_shipping_lines():
    """S122-2 auditor catch round-2: ``subtotal = total - tax_amount`` was
    wrong. When an invoice has DiscountLine or ShippingLine, ``TotalAmt``
    includes those — but the item-line subtotal must not. ``_extract_line_
    subtotal`` sums only ``SalesItemLineDetail`` + ``ItemBasedExpenseLine
    Detail`` lines.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=rf"^{query_url()}\?.*").mock(
            return_value=httpx.Response(
                200, json=load_response("invoice_query_with_discount_and_shipping"),
            ),
        )

        async def go():
            async with _build_client() as qb:
                return await qb.query("Invoice")

        rows = _run(go())

    raw = rows[0]
    # TotalAmt is 115 (100 item - 10 discount + 15 shipping + 10 tax... QBO arithmetic varies).
    # What matters: the item-only subtotal is 100, not 105 (TotalAmt - tax) and
    # not 90 (item + discount).
    assert _extract_line_subtotal(raw) == Decimal("100")
    assert _extract_tax_amount(raw) == Decimal("10")


# ---------------------------------------------------------------------------
# OAuth refresh — request shape + response parse
# ---------------------------------------------------------------------------


def test_oauth_refresh_request_uses_basic_auth_and_grant_type(monkeypatch):
    """The Intuit token endpoint expects Basic auth with client_id:client_secret
    and a form body with ``grant_type=refresh_token``. A regression here
    silently drops every prod tenant's refresh.
    """
    monkeypatch.setenv("QB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("QB_CLIENT_SECRET", "test-client-secret")

    with respx.mock(assert_all_called=False) as router:
        route = router.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(200, json=load_response("oauth_refresh")),
        )

        out = _run(refresh_access_token("old-refresh-token"))

    assert route.called
    req = route.calls[0].request
    assert req.headers["Authorization"].startswith("Basic ")
    # base64("test-client-id:test-client-secret") = "dGVzdC1jbGllbnQtaWQ6dGVzdC1jbGllbnQtc2VjcmV0"
    assert req.headers["Authorization"] == "Basic dGVzdC1jbGllbnQtaWQ6dGVzdC1jbGllbnQtc2VjcmV0"
    assert req.headers["Content-Type"] == "application/x-www-form-urlencoded"

    body = parse_qs(req.content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["old-refresh-token"]

    # Response parses through as-is.
    assert out["access_token"].startswith("eyJ")
    assert out["refresh_token"].startswith("AB117")
    assert out["expires_in"] == 3600


def test_oauth_refresh_failure_raises_QBAuthError(monkeypatch):
    monkeypatch.setenv("QB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("QB_CLIENT_SECRET", "test-client-secret")

    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"}),
        )

        with pytest.raises(QBAuthError) as exc_info:
            _run(refresh_access_token("revoked-refresh-token"))

    assert "Token refresh failed" in str(exc_info.value)
