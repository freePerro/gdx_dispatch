"""Tests for the per-tenant feature flags router."""
from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.routers import feature_flags as ff_router
from gdx_dispatch.routers.feature_flags import FeatureFlag, FeatureFlagIn


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Create the feature_flags table + audit_logs (TenantBase metadata).
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _admin() -> dict[str, str]:
    return {"sub": "admin-1", "user_id": "admin-1", "email": "admin@test.io", "role": "admin"}


def _request(tenant_id: str = "tenant-a") -> Request:
    req = Request({"type": "http", "headers": []})
    req.state.tenant = {"id": tenant_id, "slug": tenant_id}
    return req


def test_create_flag(db_session: Session):
    payload = FeatureFlagIn(name="new_dashboard", enabled=True, description="roll out v2 dashboard")
    data = ff_router.upsert_feature_flag(
        payload=payload,
        request=_request(),
        user=_admin(),
        db=db_session,
    )
    assert data["name"] == "new_dashboard"
    assert data["enabled"] is True
    assert data["description"] == "roll out v2 dashboard"
    assert data["updated_by"] == "admin@test.io"
    assert data["id"]


def test_list_flags_tenant_scoped(db_session: Session):
    ff_router.upsert_feature_flag(
        payload=FeatureFlagIn(name="alpha", enabled=True),
        request=_request("tenant-a"),
        user=_admin(),
        db=db_session,
    )
    ff_router.upsert_feature_flag(
        payload=FeatureFlagIn(name="beta", enabled=False),
        request=_request("tenant-b"),
        user=_admin(),
        db=db_session,
    )

    rows_a = ff_router.list_feature_flags(request=_request("tenant-a"), _=_admin(), db=db_session)
    rows_b = ff_router.list_feature_flags(request=_request("tenant-b"), _=_admin(), db=db_session)

    names_a = {r["name"] for r in rows_a}
    names_b = {r["name"] for r in rows_b}
    assert names_a == {"alpha"}
    assert names_b == {"beta"}
    # No leakage across tenants
    assert "beta" not in names_a
    assert "alpha" not in names_b


def test_enable_disable(db_session: Session):
    ff_router.upsert_feature_flag(
        payload=FeatureFlagIn(name="toggle_me", enabled=False),
        request=_request(),
        user=_admin(),
        db=db_session,
    )

    enabled = ff_router.enable_feature_flag(
        name="toggle_me", request=_request(), user=_admin(), db=db_session
    )
    assert enabled["enabled"] is True

    disabled = ff_router.disable_feature_flag(
        name="toggle_me", request=_request(), user=_admin(), db=db_session
    )
    assert disabled["enabled"] is False

    # Missing flag → 404
    with pytest.raises(HTTPException) as exc:
        ff_router.enable_feature_flag(
            name="does_not_exist", request=_request(), user=_admin(), db=db_session
        )
    assert exc.value.status_code == 404


def test_delete_flag(db_session: Session):
    ff_router.upsert_feature_flag(
        payload=FeatureFlagIn(name="ephemeral", enabled=True),
        request=_request(),
        user=_admin(),
        db=db_session,
    )

    result = ff_router.delete_feature_flag(
        name="ephemeral", request=_request(), user=_admin(), db=db_session
    )
    assert result is None

    rows = ff_router.list_feature_flags(request=_request(), _=_admin(), db=db_session)
    assert all(r["name"] != "ephemeral" for r in rows)

    with pytest.raises(HTTPException) as exc:
        ff_router.delete_feature_flag(
            name="ephemeral", request=_request(), user=_admin(), db=db_session
        )
    assert exc.value.status_code == 404


def test_name_validation():
    # Spaces rejected
    with pytest.raises(ValidationError):
        FeatureFlagIn(name="has spaces", enabled=True)
    # Uppercase rejected
    with pytest.raises(ValidationError):
        FeatureFlagIn(name="HasCaps", enabled=True)
    # Empty rejected
    with pytest.raises(ValidationError):
        FeatureFlagIn(name="", enabled=True)
    # Valid: lower+digits+underscore
    ok = FeatureFlagIn(name="ok_flag_123", enabled=True)
    assert ok.name == "ok_flag_123"


def test_router_has_expected_paths():
    paths = {route.path for route in ff_router.router.routes}
    assert "/api/tenant/feature-flags" in paths
    assert "/api/tenant/feature-flags/{name}/enable" in paths
    assert "/api/tenant/feature-flags/{name}/disable" in paths
    assert "/api/tenant/feature-flags/{name}" in paths


def test_missing_tenant_context_rejected(db_session: Session):
    req = Request({"type": "http", "headers": []})
    # Do not set request.state.tenant — should 400
    with pytest.raises(HTTPException) as exc:
        ff_router.list_feature_flags(request=req, _=_admin(), db=db_session)
    assert exc.value.status_code == 400


def test_unique_per_tenant_upsert(db_session: Session):
    """Upserting same name should update, not duplicate."""
    a = ff_router.upsert_feature_flag(
        payload=FeatureFlagIn(name="once", enabled=False, description="first"),
        request=_request(),
        user=_admin(),
        db=db_session,
    )
    b = ff_router.upsert_feature_flag(
        payload=FeatureFlagIn(name="once", enabled=True, description="second"),
        request=_request(),
        user=_admin(),
        db=db_session,
    )
    assert a["id"] == b["id"]
    assert b["enabled"] is True
    assert b["description"] == "second"

    rows = db_session.query(FeatureFlag).filter(FeatureFlag.name == "once").all()
    assert len(rows) == 1
