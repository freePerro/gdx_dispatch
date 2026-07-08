"""Phase 2 / s13 — verify Microsoft Graph webhook validation + notification routing."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.modules.outlook.webhook_router import router as wh_router


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(wh_router)
    return TestClient(app)


def test_validation_handshake_echoes_token(app):
    r = app.get("/api/webhooks/outlook/gdx/abc-state?validationToken=hello-microsoft")
    assert r.status_code == 200
    assert r.text == "hello-microsoft"
    assert r.headers["content-type"].startswith("text/plain")


def test_validation_handshake_400_when_missing_token(app):
    r = app.get("/api/webhooks/outlook/gdx/abc-state")
    assert r.status_code == 400


def test_validation_handshake_via_post_echoes_token(app):
    """Graph sends the validation handshake as a POST with an EMPTY body.

    2026-07-08 prod catch: only the GET route echoed the token; the POST
    route tried request.json() on the empty body and 400'd
    (JSONDecodeError), so Graph's subscription create always failed with
    "Notification endpoint must respond with 200 OK"."""
    r = app.post("/api/webhooks/outlook/gdx/abc-state?validationToken=hello-microsoft")
    assert r.status_code == 200
    assert r.text == "hello-microsoft"
    assert r.headers["content-type"].startswith("text/plain")


def test_post_with_no_events_returns_202(app):
    r = app.post("/api/webhooks/outlook/gdx/abc-state", json={"value": []})
    assert r.status_code == 202


def test_post_unknown_tenant_404(app):
    sess = MagicMock()
    sess.query.return_value.filter.return_value.one_or_none.return_value = None
    sess.close = MagicMock()
    with patch("gdx_dispatch.modules.outlook.webhook_router.SessionLocal", return_value=sess):
        r = app.post(
            "/api/webhooks/outlook/unknown/state",
            json={"value": [{"clientState": "state", "subscriptionId": "id"}]},
        )
    assert r.status_code == 404


def test_post_clientState_mismatch_skips_event(app):
    """Path client_state="abc" but payload clientState="xyz" → no enqueue."""
    tenant = MagicMock()
    tenant.id = uuid4()
    tenant.slug = "gdx"
    cdb_sess = MagicMock()
    cdb_sess.query.return_value.filter.return_value.one_or_none.return_value = tenant

    with patch("gdx_dispatch.modules.outlook.webhook_router.SessionLocal", return_value=cdb_sess), \
         patch("gdx_dispatch.modules.outlook.webhook_router._open_tenant_session"), \
         patch("gdx_dispatch.modules.outlook.webhook_router._enqueue_sync") as enq:
        r = app.post(
            "/api/webhooks/outlook/gdx/abc",
            json={"value": [{"clientState": "wrong", "subscriptionId": "sub1"}]},
        )
    assert r.status_code == 202
    enq.assert_not_called()


def test_post_enqueues_one_per_valid_event(app):
    tenant = MagicMock()
    tenant.id = uuid4()
    tenant.slug = "gdx"
    cdb_sess = MagicMock()
    cdb_sess.query.return_value.filter.return_value.one_or_none.return_value = tenant

    sub = MagicMock(); sub.account_id = uuid4()
    account = MagicMock(); account.id = uuid4()
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = sub
    tdb.get.return_value = account

    with patch("gdx_dispatch.modules.outlook.webhook_router.SessionLocal", return_value=cdb_sess), \
         patch("gdx_dispatch.modules.outlook.webhook_router._open_tenant_session", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.webhook_router._enqueue_sync") as enq:
        r = app.post(
            "/api/webhooks/outlook/gdx/abc",
            json={"value": [
                {"clientState": "abc", "subscriptionId": "s1"},
                {"clientState": "abc", "subscriptionId": "s2"},
            ]},
        )
    assert r.status_code == 202
    assert enq.call_count == 2
