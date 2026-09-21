"""Cell-gateway webhook shim (core half of the cellcomms feed).

The cellcomms plugin lives in gdx_dispatch_plugins (moved 2026-09-20); the
end-to-end proof that a phone's POST becomes a plug_cellcomms_* row runs
there, driving THIS shim over an in-process ASGI transport. What this file
proves is the shim's own contract, against a recording stub standing where
the plugin-host would be:

- the phone never chooses the tenant or the actor: the relay stamps both and
  forwards only the payload and its content-type — no other phone header,
  and never the secret;
- an accepted event leaves an audit row naming kind + source and NO PII;
- each upstream verdict maps to the phone's response — `duplicate` is a cheap
  200 (nomad retries at-least-once), a 422 passes through with its detail,
  a 5xx or an unreachable host is a 502 so nomad redelivers;
- a 404 — the plugin-host answering while the plugin is NOT installed, which
  is prod's default state until the owner installs the card — passes through
  and is not audited as received. Whether the shim should also log that drop
  (today it does not) is an open ruling in the plan's owed-before-prod-use;
  the test below pins the status and the no-audit rule, not the silence.

Secret policy tests mirror test_inbound_comms's gate tests because the gate is
the SAME code (core/webhook_auth.py): fail closed in prod with no secret,
enforce under unrecognised env names, off for dev/test names.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.tenant import get_company_id
from gdx_dispatch.plugin_api.context import H_MODULES, H_ROLE, H_TENANT, H_USER
from gdx_dispatch.routers import cell_gateway

TENANT = "t-cell-gw"

SMS_EVENT = {
    "kind": "sms",
    "from": "+13205550134",
    "text": "hello from the phone",
    "sentStamp": "1758100000000",
}


class _Upstream:
    """The plugin-host's ingest endpoint as a stub: records what the shim sent,
    answers with whatever the test sets."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.status_code = 200
        self.body: dict = {"status": "ok", "kind": "sms", "id": "m-1"}

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/api/plugins/cellcomms/ingest")
        async def ingest(request: Request) -> JSONResponse:
            self.received.append({"headers": dict(request.headers), "body": await request.body()})
            return JSONResponse(self.body, status_code=self.status_code)

        return app


@pytest.fixture
def stack(monkeypatch):
    """Core app carrying only the shim, relaying to the recording stub."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AuditLog.__table__.create(engine, checkfirst=True)
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _db():
        db = TS()
        try:
            yield db
        finally:
            db.close()

    upstream = _Upstream()
    core_app = FastAPI()
    core_app.include_router(cell_gateway.public_router)
    core_app.dependency_overrides[get_company_id] = lambda: TENANT
    core_app.dependency_overrides[get_db] = _db

    transport = httpx.ASGITransport(app=upstream.app())
    monkeypatch.setattr(cell_gateway, "_client", lambda: httpx.AsyncClient(transport=transport))
    monkeypatch.setattr(cell_gateway, "_plugin_host_url", lambda: "http://plugin-host")

    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv(cell_gateway.SECRET_ENV, "s3cret")
    return TestClient(core_app), TS, upstream


def _post(client, payload=SMS_EVENT, **extra_headers):
    return client.post(
        "/api/cell-gateway/webhook", json=payload,
        headers={cell_gateway.SECRET_HEADER: "s3cret", **extra_headers},
    )


def _audit_rows(TS):
    db = TS()
    try:
        return db.execute(select(AuditLog)).scalars().all()
    finally:
        db.close()


# --- the door -----------------------------------------------------------------

def test_secret_missing_config_fails_closed_in_prod(stack, monkeypatch):
    client, _, upstream = stack
    monkeypatch.delenv(cell_gateway.SECRET_ENV, raising=False)
    assert client.post("/api/cell-gateway/webhook", json=SMS_EVENT).status_code == 403
    assert upstream.received == []


def test_secret_enforced_under_unrecognised_env(stack, monkeypatch):
    client, _, _ = stack
    monkeypatch.setenv("GDX_ENV", "prod-eu")  # nobody listed it → must enforce
    monkeypatch.delenv(cell_gateway.SECRET_ENV, raising=False)
    assert client.post("/api/cell-gateway/webhook", json=SMS_EVENT).status_code == 403


def test_wrong_secret_403(stack):
    client, _, upstream = stack
    r = client.post(
        "/api/cell-gateway/webhook", json=SMS_EVENT,
        headers={cell_gateway.SECRET_HEADER: "wrong"},
    )
    assert r.status_code == 403
    assert upstream.received == []  # refused at the door, never relayed


def test_empty_body_422_not_relayed(stack):
    client, _, upstream = stack
    r = client.post(
        "/api/cell-gateway/webhook", content=b"   ",
        headers={cell_gateway.SECRET_HEADER: "s3cret", "content-type": "application/json"},
    )
    assert r.status_code == 422
    assert upstream.received == []


def test_oversized_body_413(stack):
    client, _, upstream = stack
    r = client.post(
        "/api/cell-gateway/webhook",
        content=b"x" * (cell_gateway.MAX_BODY_BYTES + 1),
        headers={cell_gateway.SECRET_HEADER: "s3cret", "content-type": "application/json"},
    )
    assert r.status_code == 413
    assert upstream.received == []


# --- the relay ----------------------------------------------------------------

def test_relay_stamps_core_context_and_forwards_nothing_from_the_phone(stack):
    client, _, upstream = stack
    # A phone (or anyone holding the secret) trying to pick the tenant or
    # smuggle headers through: none of it reaches the plugin.
    r = _post(client, **{H_TENANT: "t-evil", H_USER: "owner", "X-Forwarded-For": "203.0.113.9"})
    # (content-type IS copied from the phone — the plugin needs it to parse.)
    assert r.status_code == 200, r.text
    assert r.json() == upstream.body  # the plugin's verdict passes through untouched

    [call] = upstream.received
    assert json.loads(call["body"]) == SMS_EVENT  # payload relayed byte for byte
    h = call["headers"]
    assert h[H_TENANT.lower()] == TENANT  # company_id() decides, not the phone
    assert h[H_USER.lower()] == "cell-gateway"
    assert h[H_ROLE.lower()] == "webhook"
    assert h[H_MODULES.lower()] == ""
    assert h["content-type"] == "application/json"
    assert cell_gateway.SECRET_HEADER.lower() not in h  # the secret stays in core
    assert "x-forwarded-for" not in h


def test_accepted_event_is_audited_without_pii(stack):
    client, TS, upstream = stack
    upstream.body = {"status": "ok", "kind": "call", "id": "c-42"}
    assert _post(client).status_code == 200

    [row] = [a for a in _audit_rows(TS) if a.action == "cell_event_received"]
    assert row.user_id == "cell-gateway"
    assert row.tenant_id == TENANT
    assert row.entity_type == "cell_call"
    assert row.entity_id == "c-42"  # points at the stored event, by the plugin's id
    assert row.details["kind"] == "call"
    assert row.details["source"] == "nomad-gateway"
    # Texts are PII: neither the number nor the body may reach the trail.
    assert "+1320" not in str(row.details)
    assert "hello" not in str(row.details)


def test_duplicate_is_a_cheap_200_and_not_audited(stack):
    """nomad retries at-least-once; the plugin answers `duplicate` for a
    redelivery and the shim neither errors nor audits the event twice."""
    client, TS, upstream = stack
    upstream.body = {"status": "duplicate", "kind": "sms", "id": "m-1"}
    r = _post(client)
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert _audit_rows(TS) == []


def test_plugin_422_passes_through_with_its_detail(stack):
    """A misconfigured forwarding rule on the phone gets the plugin's reason."""
    client, TS, upstream = stack
    upstream.status_code, upstream.body = 422, {"detail": "unknown kind 'pigeon'"}
    r = _post(client, payload={"kind": "pigeon"})
    assert r.status_code == 422
    assert r.json()["detail"] == "unknown kind 'pigeon'"
    assert _audit_rows(TS) == []


def test_plugin_not_installed_404_passes_through_untraced(stack):
    """The plugin-host is up but nothing is mounted at /api/plugins/cellcomms —
    prod's state until the owner installs the plugin. The phone gets the 404
    back, and an event that never landed must not be audited as received."""
    client, TS, upstream = stack
    upstream.status_code, upstream.body = 404, {"detail": "Not Found"}
    r = _post(client)
    assert r.status_code == 404
    assert _audit_rows(TS) == []


def test_plugin_5xx_is_502_so_nomad_retries(stack):
    client, TS, upstream = stack
    upstream.status_code, upstream.body = 500, {"detail": "boom"}
    assert _post(client).status_code == 502
    assert _audit_rows(TS) == []


def test_plugin_host_down_is_502_so_nomad_retries(stack, monkeypatch):
    client, TS, _ = stack

    def _raise(request):
        raise httpx.ConnectError("plugin-host down", request=request)

    monkeypatch.setattr(
        cell_gateway, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_raise)),
    )
    assert _post(client).status_code == 502
    assert _audit_rows(TS) == []
