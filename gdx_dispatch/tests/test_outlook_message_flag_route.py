"""PATCH /api/outlook/messages/{id}/flag — Microsoft first, mirror on success.

The Outlook *pin* has no Graph surface; the follow-up flag is the stand-in
that syncs both ways. This route is the GDX→Outlook half.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.outlook.folders_router import router as folders_router
from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
from gdx_dispatch.modules.outlook.models import OutlookMessage
from gdx_dispatch.routers.auth import get_current_user

UID, TID = uuid4(), uuid4()
MOD = "gdx_dispatch.modules.outlook.folders_router"


def _user():
    return {"user_id": str(UID), "tenant_id": str(TID), "role": "admin"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    app = FastAPI()
    app.include_router(folders_router)
    tdb = MagicMock()
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = lambda: tdb
    app.dependency_overrides[require_module("email")] = lambda: None
    return TestClient(app), tdb


def _account():
    a = MagicMock()
    a.id = uuid4()
    return a


def _msg(account):
    m = OutlookMessage()
    m.id = uuid4()
    m.account_id = account.id
    m.graph_message_id = "graph-abc"
    m.is_flagged = False
    return m


@contextmanager
def _gc_ctx(gc):
    yield gc


def test_flag_writes_microsoft_then_mirror_and_audits(app):
    client, tdb = app
    account = _account()
    msg = _msg(account)
    tdb.get.return_value = msg
    gc = MagicMock()
    with patch(f"{MOD}._account_for_user", return_value=account), \
         patch(f"{MOD}.with_outlook_client", return_value=_gc_ctx(gc)), \
         patch(f"{MOD}.log_audit_event_sync") as audit:
        r = client.patch(f"/api/outlook/messages/{msg.id}/flag", json={"is_flagged": True})
    assert r.status_code == 200, r.text
    gc.set_message_flag.assert_called_once_with("graph-abc", flagged=True)
    assert msg.is_flagged is True
    tdb.commit.assert_called_once()
    kw = audit.call_args.kwargs
    assert kw["action"] == "outlook.message.flag"
    assert kw["entity_id"] == str(msg.id)
    assert kw["user_id"] == UID


def test_unflag_uses_unflag_action(app):
    client, tdb = app
    account = _account()
    msg = _msg(account)
    msg.is_flagged = True
    tdb.get.return_value = msg
    gc = MagicMock()
    with patch(f"{MOD}._account_for_user", return_value=account), \
         patch(f"{MOD}.with_outlook_client", return_value=_gc_ctx(gc)), \
         patch(f"{MOD}.log_audit_event_sync") as audit:
        r = client.patch(f"/api/outlook/messages/{msg.id}/flag", json={"is_flagged": False})
    assert r.status_code == 200
    gc.set_message_flag.assert_called_once_with("graph-abc", flagged=False)
    assert msg.is_flagged is False
    assert audit.call_args.kwargs["action"] == "outlook.message.unflag"


def test_graph_failure_leaves_mirror_untouched(app):
    """Counterfactual: if the mirror were written first, a Graph 5xx would
    show a flag here that Outlook never got."""
    client, tdb = app
    account = _account()
    msg = _msg(account)
    tdb.get.return_value = msg
    gc = MagicMock()
    gc.set_message_flag.side_effect = OutlookGraphAPIError(503, "down")
    with patch(f"{MOD}._account_for_user", return_value=account), \
         patch(f"{MOD}.with_outlook_client", return_value=_gc_ctx(gc)), \
         patch(f"{MOD}.log_audit_event_sync") as audit:
        r = client.patch(f"/api/outlook/messages/{msg.id}/flag", json={"is_flagged": True})
    assert r.status_code == 502
    assert msg.is_flagged is False
    tdb.commit.assert_not_called()
    audit.assert_not_called()


def test_other_accounts_message_is_404(app):
    client, tdb = app
    account = _account()
    msg = _msg(_account())  # belongs to someone else
    tdb.get.return_value = msg
    gc = MagicMock()
    with patch(f"{MOD}._account_for_user", return_value=account), \
         patch(f"{MOD}.with_outlook_client", return_value=_gc_ctx(gc)):
        r = client.patch(f"/api/outlook/messages/{msg.id}/flag", json={"is_flagged": True})
    assert r.status_code == 404
    gc.set_message_flag.assert_not_called()
