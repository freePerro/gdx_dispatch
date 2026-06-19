"""Sprint 1.x-S26 — admin/ai-settings router (auth=get_current_user)."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

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


def test_get_returns_state_shape(app_for_admin):
    with patch(
        "gdx_dispatch.routers.admin_ai_settings.get_settings_state",
        return_value={
            "key_set": False,
            "last_validated_at": None,
            "last_error": None,
        },
    ):
        r = app_for_admin.get("/api/admin/ai-settings")
    assert r.status_code == 200
    body = r.json()
    assert "key_set" in body
    assert "last_validated_at" in body
    assert "last_error" in body
    assert "key" not in body
    assert "llm_provider_key_enc" not in body


def test_put_sets_key_and_tests_it(app_for_admin):
    with patch("gdx_dispatch.routers.admin_ai_settings.set_key") as mock_set, patch(
        "gdx_dispatch.routers.admin_ai_settings.test_the_key",
        return_value={"ok": True, "error": None, "model": "claude-haiku-4-5", "latency_ms": 200},
    ):
        r = app_for_admin.put("/api/admin/ai-settings", json={"key": "sk-ant-test"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    mock_set.assert_called_once()


def test_delete_clears_key(app_for_admin):
    with patch("gdx_dispatch.routers.admin_ai_settings.clear_key") as mock_clear:
        r = app_for_admin.delete("/api/admin/ai-settings")
    assert r.status_code in (200, 204)
    mock_clear.assert_called_once()


def test_non_admin_denied_via_real_wrapper():
    """Wrapper raises 403 for tech role."""
    with pytest.raises(HTTPException) as exc_info:
        get_admin_principal_for_ai_settings(user=_tech_user())
    assert exc_info.value.status_code == 403


def test_admin_role_passes_wrapper():
    """Wrapper returns the user dict for admin role."""
    out = get_admin_principal_for_ai_settings(user=_admin_user())
    assert out["role"] == "admin"
