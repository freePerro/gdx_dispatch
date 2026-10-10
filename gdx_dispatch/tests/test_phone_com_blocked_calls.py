"""P2.6 — blocked calls client CRUD."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db, get_tenant_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.phone_com import router as pc_router
from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient
from gdx_dispatch.tests.conftest import production_sessionmaker

_VID = 1000000


def _envelope(items):
    return {"filters": {}, "sort": {}, "total": len(items),
            "limit": 25, "offset": None, "items": items}


@respx.mock
def test_list_blocked_calls_paginates():
    items = [
        {"id": 1, "name": "Spam Bot", "number": "+15551112222"},
        {"id": 2, "name": "Robocaller", "number": "+15553334444"},
    ]
    route = respx.get(f"{BASE_URL}/accounts/{_VID}/blocked-calls").mock(
        return_value=httpx.Response(200, json=_envelope(items))
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.list_blocked_calls()
    assert out["total"] == 2
    assert route.called


@respx.mock
def test_create_blocked_call_posts_canonical_body():
    route = respx.post(f"{BASE_URL}/accounts/{_VID}/blocked-calls").mock(
        return_value=httpx.Response(200, json={"id": 99, "number": "+15555550100"})
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    out = c.create_blocked_call(name="Spam", number="+15555550100")
    body = json.loads(route.calls.last.request.read())
    assert body == {
        "name": "Spam", "number": "+15555550100",
        "direction": "in", "action": "block",
    }
    assert out["id"] == 99


@respx.mock
def test_delete_blocked_call_calls_correct_path():
    route = respx.delete(f"{BASE_URL}/accounts/{_VID}/blocked-calls/99").mock(
        return_value=httpx.Response(204)
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    c.delete_blocked_call(blocked_call_id=99)
    assert route.called


# ── routes: a refused audit row (GDXA-476) ──────────────────────────────


class _FakeClient:
    def create_blocked_call(self, **kw):
        return {"id": 99, **kw}

    def delete_blocked_call(self, *, blocked_call_id):
        return None


@pytest.fixture
def blocked_app(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.tables["audit_logs"].create(engine)
    sm = production_sessionmaker(engine)

    @contextmanager
    def _client(*_a):
        yield _FakeClient()

    monkeypatch.setattr(pc_router, "_get_phone_com_client", _client)

    def _db():
        s = sm()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(pc_router.router)
    app.dependency_overrides[require_module("phone_com")] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": str(uuid4()), "role": "admin", "tenant_id": str(uuid4()),
    }
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_tenant_db] = _db
    return TestClient(app), engine, sm


def _call_both(client):
    r1 = client.post("/api/phone-com/blocked-calls",
                     json={"name": "Spam", "number": "+15555550100"})
    r2 = client.delete("/api/phone-com/blocked-calls/99")
    return r1, r2


def test_blocked_call_routes_write_their_audit_rows(blocked_app):
    client, _, sm = blocked_app
    r1, r2 = _call_both(client)
    assert (r1.status_code, r2.status_code) == (201, 204)
    s = sm()
    assert sorted(a.action for a in s.query(AuditLog).all()) == [
        "phone_com.blocked_call.created", "phone_com.blocked_call.deleted",
    ]
    s.close()


def test_blocked_call_routes_survive_a_refused_audit_row(blocked_app, caplog):
    """The block already exists at Phone.com when the audit runs; a refused
    trail row is logged by name and the route keeps its success status."""
    client, engine, _ = blocked_app
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TRIGGER audit_logs_refuse_insert BEFORE INSERT ON audit_logs "
            "BEGIN SELECT RAISE(ABORT, 'audit storage refuses this row'); END;"
        ))
    caplog.set_level(logging.ERROR)
    r1, r2 = _call_both(client)
    assert (r1.status_code, r2.status_code) == (201, 204)
    failed = [rec.getMessage() for rec in caplog.records
              if rec.getMessage().startswith("audit_best_effort_failed")]
    assert len(failed) == 2
    assert "phone_com.blocked_call.created" in failed[0]
    assert "phone_com.blocked_call.deleted" in failed[1]
