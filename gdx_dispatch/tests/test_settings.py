from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.routers import settings as settings_router
from gdx_dispatch.routers.settings import BrandingPatchIn, SettingsPatchIn


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from sqlalchemy import text as _text
    AppSettings.__table__.create(bind=engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    # Create module grants table used by enable/disable module endpoints
    db.execute(_text("""
        CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            module_key TEXT NOT NULL,
            granted_at TEXT,
            created_at TEXT,
            expires_at TEXT,
            UNIQUE(company_id, module_key)
        )
    """))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _admin() -> dict[str, str]:
    return {"user_id": "admin-1", "tenant_id": "tenant-test", "role": "admin"}


def _tech() -> dict[str, str]:
    return {"user_id": "tech-1", "tenant_id": "tenant-test", "role": "technician"}


def _request(tier: str = "starter", slug: str = "tenant-test") -> Request:
    request = Request({"type": "http", "headers": []})
    request.state.tenant = {
        "id": "tenant-test",
        "slug": slug,
        "subscription_tier": tier,
        "subscription_status": "trialing",
    }
    return request


def test_get_settings_defaults(db_session: Session):
    data = settings_router.get_settings(current_user=_admin(), db=db_session)
    assert data["company_name"] == ""
    assert data["timezone"] == "America/New_York"
    assert data["enabled_modules"] == []
    assert data["notification_preferences"] == {}
    assert data["integrations"] == {"quickbooks": False, "stripe": False, "twilio": False}


def test_patch_settings_creates_row(db_session: Session):
    payload = SettingsPatchIn(
        company_name="Acme HVAC",
        address="123 Main St",
        phone="555-0101",
        email="ops@acme.test",
        logo="https://cdn.example.com/logo.png",
        timezone="America/Chicago",
    )
    data = settings_router.patch_settings(payload=payload, current_user=_admin(), db=db_session)
    assert data["company_name"] == "Acme HVAC"
    assert data["address"] == "123 Main St"
    assert data["phone"] == "555-0101"
    assert data["email"] == "ops@acme.test"
    assert data["logo"] == "https://cdn.example.com/logo.png"
    assert data["timezone"] == "America/Chicago"


def test_patch_settings_is_partial_update(db_session: Session):
    settings_router.patch_settings(
        payload=SettingsPatchIn(company_name="Old Name", phone="555-0000", timezone="America/Denver"),
        current_user=_admin(),
        db=db_session,
    )
    data = settings_router.patch_settings(
        payload=SettingsPatchIn(company_name="New Name"),
        current_user=_admin(),
        db=db_session,
    )
    assert data["company_name"] == "New Name"
    assert data["phone"] == "555-0000"
    assert data["timezone"] == "America/Denver"


def test_get_modules_defaults_empty(db_session: Session):
    data = settings_router.get_modules(request=_request(), current_user=_admin(), db=db_session)
    keys = {item["key"] for item in data["modules"]}
    assert "jobs" in keys
    assert "inventory" in keys


def test_enable_module_adds_once(db_session: Session):
    request = _request(tier="professional")
    r1 = settings_router.enable_module(request=request, key="quickbooks", current_user=_admin(), db=db_session)
    r2 = settings_router.enable_module(request=request, key="quickbooks", current_user=_admin(), db=db_session)
    assert r1 == {"status": "enabled", "key": "quickbooks"}
    assert r2 == {"status": "enabled", "key": "quickbooks"}


def test_disable_module_removes(db_session: Session):
    request = _request(tier="professional")
    settings_router.enable_module(request=request, key="inventory", current_user=_admin(), db=db_session)
    data = settings_router.disable_module(request=request, key="inventory", current_user=_admin(), db=db_session)
    assert data == {"status": "disabled", "key": "inventory"}


def test_disable_persists_across_get_for_gdx_tenant(db_session: Session):
    """D101 regression — an earlier session, 2026-04-25.

    Reproduces the exact F1 click-walk failure: on the GDX tenant slug,
    `get_modules` used to re-grant every missing module on every call,
    so the admin disable was effectively a no-op (toggle off → toggle on
    by the next read). Assert disable now sticks across a subsequent GET.
    """
    request = _request(tier="business", slug="gdx")
    settings_router.enable_module(
        request=request, key="warranties", current_user=_admin(), db=db_session
    )
    pre_get = settings_router.get_modules(request=request, current_user=_admin(), db=db_session)
    pre_warranties = next(m for m in pre_get["modules"] if m["key"] == "warranties")
    assert pre_warranties["enabled"] is True

    settings_router.disable_module(
        request=request, key="warranties", current_user=_admin(), db=db_session
    )
    post_get = settings_router.get_modules(request=request, current_user=_admin(), db=db_session)
    post_warranties = next(m for m in post_get["modules"] if m["key"] == "warranties")
    assert post_warranties["enabled"] is False, (
        "GDX autohealer regression — get_modules must not resurrect a deleted grant"
    )
    second_get = settings_router.get_modules(request=request, current_user=_admin(), db=db_session)
    second_warranties = next(m for m in second_get["modules"] if m["key"] == "warranties")
    assert second_warranties["enabled"] is False, "disable must stick across multiple GETs"



def test_get_notifications_defaults(db_session: Session):
    data = settings_router.get_notification_preferences(current_user=_admin(), db=db_session)
    assert data == {"notification_preferences": {}}


def test_patch_notifications_updates_preferences(db_session: Session):
    payload = {
        "job_created_email": True,
        "invoice_paid_sms": False,
        "daily_summary": True,
    }
    data = settings_router.patch_notification_preferences(payload=payload, current_user=_admin(), db=db_session)
    assert data == {"notification_preferences": payload}

    read_back = settings_router.get_notification_preferences(current_user=_admin(), db=db_session)
    assert read_back == {"notification_preferences": payload}


def test_get_integrations_returns_only_active(db_session: Session):
    settings_router.patch_settings(
        payload=SettingsPatchIn(integrations={"quickbooks": True, "stripe": False, "twilio": True}),
        current_user=_admin(),
        db=db_session,
    )
    data = settings_router.list_integrations(current_user=_admin(), db=db_session)
    assert data["integrations"] == {"quickbooks": True, "stripe": False, "twilio": True}
    assert set(data["active_integrations"]) == {"quickbooks", "twilio"}


@pytest.mark.anyio
async def test_get_branding_defaults(db_session: Session):
    mock_request = SimpleNamespace(state=SimpleNamespace(tenant={"id": "tenant-test"}))
    data = await settings_router.get_branding(request=mock_request, current_user=_admin(), db=db_session)
    assert data["company_name"] == ""
    assert data["logo_url"] == ""
    assert data["primary_color"] == "#0f172a"
    assert data["accent_color"] == "#2563eb"


def test_patch_branding_updates_fields(db_session: Session):
    data = settings_router.patch_branding(
        payload=BrandingPatchIn(
            company_name="North Star Mechanical",
            logo="https://cdn.example.com/brand.png",
            primary_color="#111827",
            secondary_color="#22c55e",
        ),
        current_user=_admin(),
        db=db_session,
    )
    assert data["company_name"] == "North Star Mechanical"
    assert data["logo_url"] == "https://cdn.example.com/brand.png"
    assert data["primary_color"] == "#111827"
    assert data["accent_color"] == "#22c55e"


def test_non_admin_is_forbidden(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        settings_router.get_settings(current_user=_tech(), db=db_session)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin access required"


def test_router_has_expected_paths():
    paths = {route.path for route in settings_router.router.routes}
    assert "/api/settings" in paths
    assert "/api/settings/modules" in paths
    assert "/api/settings/modules/{key}/enable" in paths
    assert "/api/settings/modules/{key}/disable" in paths
    assert "/api/settings/notifications" in paths
    assert "/api/settings/integrations" in paths
    assert "/api/settings/branding" in paths


def test_app_registers_settings_routes():
    app_py = (settings_router.__file__ or "").replace("/routers/settings.py", "/app.py")
    with open(app_py, encoding="utf-8") as f:
        source = f.read()
    assert "from gdx_dispatch.routers import settings as settings_router" in source
    assert "app.include_router(settings_router.router if hasattr(settings_router, \"router\") else settings_router)" in source
