"""Tests for the UX telemetry endpoint."""
from __future__ import annotations

import logging
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.ux_telemetry import router


_TEST_USER_ID = str(uuid4())
_TEST_TENANT_ID = str(uuid4())


def _make_client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": _TEST_TENANT_ID}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": _TEST_USER_ID,
        "sub": _TEST_USER_ID,
        "role": "admin",
        "tenant_id": _TEST_TENANT_ID,
    }
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()


def test_accepts_valid_batch_and_returns_204(client: TestClient, caplog):
    caplog.set_level(logging.INFO, logger="gdx_dispatch.ux_telemetry")
    r = client.post(
        "/api/audit/ux-event",
        json={
            "events": [
                {"name": "tour_started", "payload": {"tour_id": "owner-getting-started", "total_steps": 5}},
                {"name": "help_article_viewed", "payload": {"slug": "customers", "source": "search"}},
            ]
        },
    )
    assert r.status_code == 204, r.text
    # Both events should have been logged with attribution.
    logged = [rec.message for rec in caplog.records if "ux_telemetry" in rec.message]
    assert any("tour_started" in m for m in logged)
    assert any("help_article_viewed" in m for m in logged)
    assert any(_TEST_USER_ID in m for m in logged)


def test_empty_batch_is_ok(client: TestClient):
    r = client.post("/api/audit/ux-event", json={"events": []})
    assert r.status_code == 204


def test_unknown_event_names_dropped_silently(client: TestClient, caplog):
    caplog.set_level(logging.INFO, logger="gdx_dispatch.ux_telemetry")
    r = client.post(
        "/api/audit/ux-event",
        json={"events": [{"name": "rogue_event", "payload": {"x": 1}}]},
    )
    assert r.status_code == 204
    logged = [rec.message for rec in caplog.records if "ux_telemetry" in rec.message]
    assert not any("rogue_event" in m for m in logged)


def test_payload_truncated(client: TestClient, caplog):
    caplog.set_level(logging.INFO, logger="gdx_dispatch.ux_telemetry")
    huge = "x" * 5000
    r = client.post(
        "/api/audit/ux-event",
        json={"events": [{"name": "tour_step", "payload": {"big": huge}}]},
    )
    assert r.status_code == 204
    logged = [rec.message for rec in caplog.records if "tour_step" in rec.message]
    assert logged
    # Truncated to ~200 chars + envelope; full 5000-char string must not appear.
    assert huge not in logged[0]


def test_oversized_batch_rejected(client: TestClient):
    too_many = [{"name": "tour_step", "payload": {"i": i}} for i in range(60)]
    r = client.post("/api/audit/ux-event", json={"events": too_many})
    assert r.status_code in (413, 422)


def test_payload_is_logged_as_json(client: TestClient, caplog):
    """Payload must be JSON-formatted in the log line so prod log
    parsers can extract fields. Python's default %s on a dict emits
    single-quoted keys which break every JSON pipeline."""
    caplog.set_level(logging.INFO, logger="gdx_dispatch.ux_telemetry")
    r = client.post(
        "/api/audit/ux-event",
        json={"events": [{"name": "tour_started", "payload": {"tour_id": "owner-getting-started"}}]},
    )
    assert r.status_code == 204
    logged = [rec.message for rec in caplog.records if "tour_started" in rec.message]
    assert logged
    # JSON-formatted: double quotes, parseable by stdlib json.
    import json as _json
    msg = logged[0]
    payload_start = msg.index('payload=') + len('payload=')
    payload_str = msg[payload_start:]
    parsed = _json.loads(payload_str)
    assert parsed["tour_id"] == "owner-getting-started"
