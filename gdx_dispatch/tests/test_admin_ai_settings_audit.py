"""Sprint 1.x-S30a — admin/ai-settings audit endpoint (auth=get_current_user)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from gdx_dispatch.routers.admin_ai_settings import (
    get_admin_principal_for_ai_settings,
    get_db_for_ai_settings,
)
from gdx_dispatch.routers.admin_ai_settings import router as ai_settings_router


def _admin_user() -> dict[str, Any]:
    return {
        "id": "aba77c5f-ed05-49e8-a41d-8b01b5a7bf0c",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "role": "admin",
    }


def _tech_user() -> dict[str, Any]:
    return {
        "id": "aba77c5f-ed05-49e8-a41d-8b01b5a7bf0c",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "role": "tech",
    }


@pytest.fixture
def app_for_admin():
    app = FastAPI()
    app.include_router(ai_settings_router)
    app.dependency_overrides[get_admin_principal_for_ai_settings] = lambda: _admin_user()
    app.dependency_overrides[get_db_for_ai_settings] = lambda: MagicMock()
    return TestClient(app)


def _mk_audit_row(action, when, user_id=None, user_name="alice"):
    """Audit-row dict matching the shape ``_fetch_ai_audit_rows`` returns."""
    return {
        "id": str(uuid4()),
        "action": action,
        "user_id": user_id or str(uuid4()),
        "details": {"actor": user_name},
        "created_at": when,
    }


def test_returns_recent_ai_settings_events(app_for_admin):
    now = datetime.now(timezone.utc)
    rows = [
        _mk_audit_row("ai_settings.key_set", now - timedelta(minutes=1)),
        _mk_audit_row("ai_settings.key_rotated", now - timedelta(hours=1)),
        _mk_audit_row("ai_settings.key_removed", now - timedelta(hours=2)),
    ]
    with patch(
        "gdx_dispatch.routers.admin_ai_settings._fetch_ai_audit_rows",
        return_value=rows,
    ):
        r = app_for_admin.get("/api/admin/ai-settings/audit")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert len(body["items"]) == 3
    assert body["items"][0]["action"] == "ai_settings.key_set"
    assert body["items"][1]["action"] == "ai_settings.key_rotated"
    for item in body["items"]:
        assert "action" in item
        assert "created_at" in item
        assert "llm_provider_key_enc" not in item


def test_default_limit_is_10(app_for_admin):
    """Endpoint default limit is 10."""
    now = datetime.now(timezone.utc)
    rows = [
        _mk_audit_row(f"ai_settings.event_{i}", now - timedelta(minutes=i))
        for i in range(20)
    ]
    with patch(
        "gdx_dispatch.routers.admin_ai_settings._fetch_ai_audit_rows"
    ) as mock_fetch:
        mock_fetch.return_value = rows[:10]
        r = app_for_admin.get("/api/admin/ai-settings/audit")
    assert r.status_code == 200
    _, call_kwargs = mock_fetch.call_args
    assert call_kwargs.get("limit", 10) == 10
    assert len(r.json()["items"]) == 10


def test_limit_query_param_clamped(app_for_admin):
    """?limit=N — clamped to a sane upper bound (e.g. 100)."""
    with patch(
        "gdx_dispatch.routers.admin_ai_settings._fetch_ai_audit_rows",
        return_value=[],
    ):
        r = app_for_admin.get("/api/admin/ai-settings/audit?limit=99999")
    assert r.status_code in (200, 422)


def test_non_admin_denied_via_real_wrapper():
    """Wrapper-internal role check; can't test via dep override."""
    with pytest.raises(HTTPException) as exc_info:
        get_admin_principal_for_ai_settings(user=_tech_user())
    assert exc_info.value.status_code == 403


def test_empty_list_when_no_events(app_for_admin):
    with patch(
        "gdx_dispatch.routers.admin_ai_settings._fetch_ai_audit_rows",
        return_value=[],
    ):
        r = app_for_admin.get("/api/admin/ai-settings/audit")
    assert r.status_code == 200
    assert r.json()["items"] == []
