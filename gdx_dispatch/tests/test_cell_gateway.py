"""Cell-gateway webhook shim (core half of the cellcomms feed).

The relay tests drive the REAL plugin router over an in-process ASGI transport
— no mock captures. A mock would prove which arguments were passed; these
prove a phone's POST becomes a plug_cellcomms_* row.

Secret policy tests mirror test_inbound_comms's gate tests because the gate is
now the SAME code (core/webhook_auth.py): fail closed in prod with no secret,
enforce under unrecognised env names, off for dev/test names.
"""
from __future__ import annotations

import pathlib
import sys

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import database as core_database
from gdx_dispatch.core.tenant import get_company_id
from gdx_dispatch.routers import cell_gateway

_PLUGIN_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "plugins" / "gdx-plugin-cellcomms")
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

TENANT = "t-cell-gw"

SMS_EVENT = {
    "kind": "sms",
    "from": "+13205550134",
    "text": "hello from the phone",
    "sentStamp": "1758100000000",
}


@pytest.fixture
def stack(monkeypatch):
    """Core app (shim only) + plugin app (real cellcomms router), one DB."""
    from gdx_plugin_cellcomms.models import CellCall, CellMessage
    from gdx_plugin_cellcomms.router import router as plugin_router

    from gdx_dispatch.models.tenant_models import Customer

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for table in (Customer.__table__, CellMessage.__table__, CellCall.__table__):
        table.create(engine, checkfirst=True)
    # autoflush=False: EXACTLY core/database.py's SessionLocal config — the
    # 2026-09-18 re-audit caught autoflush=True fixtures masking a prod-only
    # IntegrityError path in the backfill.
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _db():
        db = TS()
        try:
            yield db
        finally:
            db.close()

    # The real plugin app, mounted exactly where plugin-host mounts it.
    plugin_app = FastAPI()
    plugin_app.include_router(plugin_router, prefix="/api/plugins/cellcomms")
    from gdx_dispatch.plugin_api import context as plugin_context

    plugin_app.dependency_overrides[plugin_context.get_plugin_db] = _db
    # get_plugin_context stays REAL — it must parse the headers the shim sends.

    # Core app carrying only the shim.
    core_app = FastAPI()
    core_app.include_router(cell_gateway.public_router)
    core_app.dependency_overrides[get_company_id] = lambda: TENANT
    from gdx_dispatch.core.database import get_db

    core_app.dependency_overrides[get_db] = _db

    # Point the shim's outbound client at the plugin app, in-process.
    transport = httpx.ASGITransport(app=plugin_app)
    monkeypatch.setattr(
        cell_gateway, "_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://plugin-host"),
    )
    monkeypatch.setattr(cell_gateway, "_plugin_host_url", lambda: "http://plugin-host")
    # log_audit_event_sync bootstraps the audit table on this throwaway engine.
    monkeypatch.setattr(core_database, "SessionLocal", TS, raising=False)

    return TestClient(core_app), TS


def test_secret_missing_config_fails_closed_in_prod(stack, monkeypatch):
    client, _ = stack
    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.delenv(cell_gateway.SECRET_ENV, raising=False)
    r = client.post("/api/cell-gateway/webhook", json=SMS_EVENT)
    assert r.status_code == 403


def test_secret_enforced_under_unrecognised_env(stack, monkeypatch):
    client, _ = stack
    monkeypatch.setenv("GDX_ENV", "prod-eu")  # nobody listed it → must enforce
    monkeypatch.delenv(cell_gateway.SECRET_ENV, raising=False)
    assert client.post("/api/cell-gateway/webhook", json=SMS_EVENT).status_code == 403


def test_wrong_secret_403(stack, monkeypatch):
    client, _ = stack
    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv(cell_gateway.SECRET_ENV, "right")
    r = client.post(
        "/api/cell-gateway/webhook", json=SMS_EVENT,
        headers={cell_gateway.SECRET_HEADER: "wrong"},
    )
    assert r.status_code == 403


def test_relay_lands_row_in_plugin_table_and_audits(stack, monkeypatch):
    from gdx_plugin_cellcomms.models import CellMessage

    from gdx_dispatch.core.audit import AuditLog

    client, TS = stack
    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv(cell_gateway.SECRET_ENV, "s3cret")

    r = client.post(
        "/api/cell-gateway/webhook", json=SMS_EVENT,
        headers={cell_gateway.SECRET_HEADER: "s3cret"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["kind"] == "sms"

    db = TS()
    try:
        row = db.execute(select(CellMessage)).scalars().one()
        assert row.company_id == TENANT          # stamped by the shim's header, not the phone
        assert row.body == "hello from the phone"
        audit = db.execute(select(AuditLog)).scalars().all()
        assert any(a.action == "cell_event_received" for a in audit)
        acted = next(a for a in audit if a.action == "cell_event_received")
        assert acted.user_id == "cell-gateway"
        # No PII in the audit trail: kind + source only.
        assert "+1320" not in str(acted.details)
        assert "hello" not in str(acted.details)
    finally:
        db.close()

    # Redelivery (nomad retries) → 200 duplicate, still one row.
    r2 = client.post(
        "/api/cell-gateway/webhook", json=SMS_EVENT,
        headers={cell_gateway.SECRET_HEADER: "s3cret"},
    )
    assert r2.status_code == 200 and r2.json()["status"] == "duplicate"
    db = TS()
    try:
        assert len(db.execute(select(CellMessage)).scalars().all()) == 1
    finally:
        db.close()


def test_bad_template_is_422_not_stored(stack, monkeypatch):
    client, TS = stack
    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv(cell_gateway.SECRET_ENV, "s3cret")
    r = client.post(
        "/api/cell-gateway/webhook", json={"kind": "pigeon"},
        headers={cell_gateway.SECRET_HEADER: "s3cret"},
    )
    assert r.status_code == 422


def test_oversized_body_413(stack, monkeypatch):
    client, _ = stack
    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv(cell_gateway.SECRET_ENV, "s3cret")
    r = client.post(
        "/api/cell-gateway/webhook",
        content=b"x" * (cell_gateway.MAX_BODY_BYTES + 1),
        headers={cell_gateway.SECRET_HEADER: "s3cret", "content-type": "application/json"},
    )
    assert r.status_code == 413


def test_plugin_host_down_is_502_so_nomad_retries(stack, monkeypatch):
    client, _ = stack
    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv(cell_gateway.SECRET_ENV, "s3cret")

    def _raise(request):
        raise httpx.ConnectError("plugin-host down", request=request)

    monkeypatch.setattr(
        cell_gateway, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_raise)),
    )
    r = client.post(
        "/api/cell-gateway/webhook", json=SMS_EVENT,
        headers={cell_gateway.SECRET_HEADER: "s3cret"},
    )
    assert r.status_code == 502
