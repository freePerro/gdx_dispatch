"""GDXA-296 — the assistant's "today" is the tenant's zone, not hardcoded Eastern.

At 2026-10-07 04:30Z it is still 2026-10-06 in Chicago and already
2026-10-07 in New York; the old ``_ET`` constant told a Central shop
"tomorrow" for the last hour of its day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.routers.ai as ai_mod
from gdx_dispatch.core import pay_periods
from gdx_dispatch.core.pay_periods import shop_tz_name_from_settings
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.routers.ai import _business_tz, get_current_principal_for_ai, get_db_for_ai
from gdx_dispatch.routers.ai import router as ai_router

INSTANT = datetime(2026, 10, 7, 4, 30, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return INSTANT.astimezone(tz) if tz is not None else INSTANT.replace(tzinfo=None)


@dataclass
class _StubAdmin:
    identity_id: Any = field(default_factory=uuid4)
    tenant_id: str = "11111111-1111-1111-1111-111111111111"
    principal_role: str = "admin"
    capabilities: tuple = (("*", "*"),)
    auth_kind: str = "session"
    actor_type: str = "human"
    delegated_by_user_id: str | None = None
    is_super_admin: bool = True
    is_restricted: bool = False


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AppSettings.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _set_tz(db, name: str) -> None:
    db.add(AppSettings(timezone=name))
    db.commit()


def _fresh_session(db):
    """A second session on the same engine: ``Session.info`` caches the zone."""
    return sessionmaker(bind=db.get_bind())()


def _system_prompt_for(db) -> str:
    app = FastAPI()
    app.include_router(ai_router)
    app.dependency_overrides[get_current_principal_for_ai] = lambda: _StubAdmin()
    app.dependency_overrides[get_db_for_ai] = lambda: db
    block = MagicMock()
    block.type = "text"
    block.text = "ok"
    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [block]
    with patch.object(ai_mod, "get_key", return_value="sk-ant-test"), \
         patch.object(ai_mod, "get_client") as mock_get_client, \
         patch.object(ai_mod, "list_tools_for_principal", return_value=[]), \
         patch.object(ai_mod, "datetime", _FrozenDatetime):
        fake_client = MagicMock()
        fake_client.messages.create.return_value = resp
        mock_get_client.return_value = fake_client
        r = TestClient(app).post("/api/ai/ask", json={"question": "what's on today?"})
    assert r.status_code == 200, r.text
    return fake_client.messages.create.call_args.kwargs["system"]


def test_chicago_tenant_gets_chicago_date_at_0430z(db):
    _set_tz(db, "America/Chicago")
    prompt = _system_prompt_for(db)
    assert "Tuesday, October 6, 2026 (2026-10-06, America/Chicago)" in prompt
    assert "2026-10-07" not in prompt
    assert "Eastern" not in prompt


def test_new_york_tenant_still_gets_new_york_date(db):
    _set_tz(db, "America/New_York")
    prompt = _system_prompt_for(db)
    assert "(2026-10-07, America/New_York)" in prompt


def test_no_settings_row_uses_the_shared_shop_default(db):
    # GDXA-1621: the missing-row default lives in core.pay_periods only; the
    # assistant must say whatever every other "today" in the app says.
    expected = shop_tz_name_from_settings(_fresh_session(db))
    assert str(_business_tz(db)) == expected


def test_business_tz_env_no_longer_overrides_the_shop_zone(db, monkeypatch):
    monkeypatch.setenv("GDX_BUSINESS_TZ", "America/Denver")
    assert str(_business_tz(db)) != "America/Denver"
    _set_tz(db, "America/Chicago")
    assert str(_business_tz(_fresh_session(db))) == "America/Chicago"


def test_zone_read_is_cached_with_the_shared_reader(db):
    _set_tz(db, "America/Chicago")
    assert str(_business_tz(db)) == "America/Chicago"
    assert db.info["gdx_shop_timezone"] == "America/Chicago"


def test_invalid_tenant_zone_falls_back_and_says_so(db, caplog, monkeypatch):
    # GDXA-1618: an unusable stored zone gets the shared shop default, and the
    # shared reader is the one that says so (it warns once per process).
    monkeypatch.setattr(pay_periods, "_WARNED", set())
    _set_tz(db, "Not/AZone")
    with caplog.at_level("WARNING", logger=pay_periods.__name__):
        assert str(_business_tz(db)) == pay_periods.DEFAULT_SHOP_TZ
    assert "shop_timezone_unset" in caplog.text and "Not/AZone" in caplog.text


def test_failed_settings_read_falls_back_and_logs(caplog):
    broken = MagicMock()
    broken.info = {}
    broken.query.side_effect = RuntimeError("no such table: app_settings")
    with caplog.at_level("ERROR", logger=ai_mod.log.name):
        assert str(_business_tz(broken)) == "UTC"
    assert "ai_tenant_timezone_read_failed" in caplog.text


def test_real_failed_settings_read_falls_back_to_utc(db):
    # The real failure, not a mock: the table is gone and the read raises.
    # SQLite never poisons a transaction, so this cannot prove the
    # contained_read savepoint; it proves the fallback on a real error.
    AppSettings.__table__.drop(db.get_bind())
    assert str(_business_tz(db)) == "UTC"
    assert db.execute(text("select 1")).scalar() == 1
