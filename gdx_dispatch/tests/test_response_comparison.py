from __future__ import annotations

import httpx
import pytest

from gdx_dispatch.tools.compare_responses import (
    DEFAULT_ENDPOINTS,
    compare_json_payloads,
    normalize_payload,
    run_comparison,
)


def _transport(
    by_path: dict[str, httpx.Response | Exception],
    seen: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        result = by_path[request.url.path]
        if isinstance(result, Exception):
            raise result
        return result

    return httpx.MockTransport(_handler)


@pytest.mark.anyio
async def test_matching_responses_have_no_field_diffs() -> None:
    body = {"ok": True, "items": [{"id": 1, "name": "A"}]}

    flask_client = httpx.AsyncClient(
        transport=_transport({"/api/health": httpx.Response(200, json=body)}),
        base_url="https://flask.example",
    )
    fastapi_client = httpx.AsyncClient(
        transport=_transport({"/api/health": httpx.Response(200, json=body)}),
        base_url="https://fastapi.example",
    )

    try:
        report = await run_comparison(
            flask_url="https://flask.example",
            fastapi_url="https://fastapi.example",
            endpoints=["/api/health"],
            flask_client=flask_client,
            fastapi_client=fastapi_client,
        )
    finally:
        await flask_client.aclose()
        await fastapi_client.aclose()

    assert report == [
        {
            "endpoint": "/api/health",
            "flask_status": 200,
            "fastapi_status": 200,
            "field_diffs": [],
        }
    ]


@pytest.mark.anyio
async def test_missing_field_is_reported() -> None:
    diffs = compare_json_payloads(
        {"customer": {"name": "Jane", "email": "jane@example.com"}},
        {"customer": {"name": "Jane"}},
    )

    assert {
        "path": "$.customer.email",
        "issue": "missing_in_fastapi",
        "flask_value": "jane@example.com",
        "fastapi_value": None,
    } in diffs


@pytest.mark.anyio
async def test_extra_field_in_fastapi_is_reported() -> None:
    diffs = compare_json_payloads(
        {"customer": {"name": "Jane"}},
        {"customer": {"name": "Jane", "email": "jane@example.com"}},
    )

    assert {
        "path": "$.customer.email",
        "issue": "missing_in_flask",
        "flask_value": None,
        "fastapi_value": "jane@example.com",
    } in diffs


@pytest.mark.anyio
async def test_different_scalar_values_are_reported() -> None:
    diffs = compare_json_payloads({"count": 10}, {"count": 11})

    assert diffs == [
        {
            "path": "$.count",
            "issue": "value_mismatch",
            "flask_value": 10,
            "fastapi_value": 11,
        }
    ]


@pytest.mark.anyio
async def test_different_types_are_reported() -> None:
    diffs = compare_json_payloads({"count": "10"}, {"count": 10})

    assert diffs == [
        {
            "path": "$.count",
            "issue": "type_mismatch",
            "flask_type": "str",
            "fastapi_type": "int",
            "flask_value": "10",
            "fastapi_value": 10,
        }
    ]


@pytest.mark.anyio
async def test_status_code_mismatch_is_reported() -> None:
    flask_client = httpx.AsyncClient(
        transport=_transport({"/api/jobs": httpx.Response(200, json={"jobs": []})}),
        base_url="https://flask.example",
    )
    fastapi_client = httpx.AsyncClient(
        transport=_transport({"/api/jobs": httpx.Response(500, json={"detail": "boom"})}),
        base_url="https://fastapi.example",
    )

    try:
        report = await run_comparison(
            flask_url="https://flask.example",
            fastapi_url="https://fastapi.example",
            endpoints=["/api/jobs"],
            flask_client=flask_client,
            fastapi_client=fastapi_client,
        )
    finally:
        await flask_client.aclose()
        await fastapi_client.aclose()

    assert report[0]["flask_status"] == 200
    assert report[0]["fastapi_status"] == 500
    assert {
        "path": "$",
        "issue": "status_code_mismatch",
        "flask_value": 200,
        "fastapi_value": 500,
    } in report[0]["field_diffs"]


@pytest.mark.anyio
async def test_timeout_handling_is_reported() -> None:
    flask_client = httpx.AsyncClient(
        transport=_transport({"/api/customers": httpx.Response(200, json={"customers": []})}),
        base_url="https://flask.example",
    )
    fastapi_client = httpx.AsyncClient(
        transport=_transport({"/api/customers": httpx.ReadTimeout("timed out")}),
        base_url="https://fastapi.example",
    )

    try:
        report = await run_comparison(
            flask_url="https://flask.example",
            fastapi_url="https://fastapi.example",
            endpoints=["/api/customers"],
            flask_client=flask_client,
            fastapi_client=fastapi_client,
        )
    finally:
        await flask_client.aclose()
        await fastapi_client.aclose()

    assert report[0]["flask_status"] == 200
    assert report[0]["fastapi_status"] is None
    assert any(diff["issue"] == "fastapi_request_error" for diff in report[0]["field_diffs"])


@pytest.mark.anyio
async def test_normalization_ignores_timestamps_and_uuids() -> None:
    flask_payload = {
        "id": "6f6f5c4c-2da8-4b7e-bfd0-60293f8f773d",
        "created_at": "2026-04-02T10:10:10Z",
        "updatedAt": "2026-04-02T10:10:11+00:00",
        "nested": [{"timestamp": "2026-01-01T12:30:00Z", "job_uuid": "f7b126d1-f915-4f67-9dac-b567431cae77"}],
    }
    fastapi_payload = {
        "id": "42f4e0e6-4705-4685-b09e-0ad24155a66d",
        "created_at": "2026-03-01T09:00:00Z",
        "updatedAt": "2026-03-01T09:00:01+00:00",
        "nested": [{"timestamp": "2026-03-03T01:02:03Z", "job_uuid": "0a31d8f3-56be-46d5-b981-a3e2bb64df8a"}],
    }

    assert compare_json_payloads(normalize_payload(flask_payload), normalize_payload(fastapi_payload)) == []


@pytest.mark.anyio
async def test_shared_auth_token_and_cookie_used_for_both_servers() -> None:
    flask_seen: list[httpx.Request] = []
    fastapi_seen: list[httpx.Request] = []

    flask_client = httpx.AsyncClient(
        transport=_transport({"/api/health": httpx.Response(200, json={"ok": True})}, seen=flask_seen),
        base_url="https://flask.example",
    )
    fastapi_client = httpx.AsyncClient(
        transport=_transport({"/api/health": httpx.Response(200, json={"ok": True})}, seen=fastapi_seen),
        base_url="https://fastapi.example",
    )

    try:
        await run_comparison(
            flask_url="https://flask.example",
            fastapi_url="https://fastapi.example",
            auth_token="secret-token",
            session_cookie="session-id-123",
            endpoints=["/api/health"],
            flask_client=flask_client,
            fastapi_client=fastapi_client,
        )
    finally:
        await flask_client.aclose()
        await fastapi_client.aclose()

    assert flask_seen[0].headers["Authorization"] == "Bearer secret-token"
    assert fastapi_seen[0].headers["Authorization"] == "Bearer secret-token"
    assert "session=session-id-123" in flask_seen[0].headers["Cookie"]
    assert "session=session-id-123" in fastapi_seen[0].headers["Cookie"]


def test_default_endpoints_match_requested_scope() -> None:
    assert DEFAULT_ENDPOINTS == [
        "/api/health",
        "/api/jobs",
        "/api/customers",
        "/api/reports/summary",
    ]
