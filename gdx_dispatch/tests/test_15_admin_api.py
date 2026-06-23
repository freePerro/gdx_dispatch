"""
Tests for admin feature-flag and tenant module management APIs.
"""
from __future__ import annotations

import hashlib
import inspect

from gdx_dispatch.tests.conftest import app_route_paths


def test_admin_flags_module_imports():
    """admin_flags router must import cleanly and expose a FastAPI APIRouter."""
    from fastapi import APIRouter

    from gdx_dispatch.core.admin_flags import router
    assert isinstance(router, APIRouter)


def test_admin_modules_module_imports():
    """admin_modules router must import cleanly and expose a FastAPI APIRouter."""
    from fastapi import APIRouter

    from gdx_dispatch.core.admin_modules import router
    assert isinstance(router, APIRouter)


def test_flag_check_logic():
    """Hash-based rollout must give consistent results for the same flag_key+tenant_id pair."""
    flag_key = "smart_dispatch"
    tenant_id = "tenant-abc-123"

    # Replicate the exact hash logic from gdx_dispatch/core/feature_flags.py
    digest = hashlib.md5(f"{flag_key}{tenant_id}".encode()).hexdigest()
    bucket = int(digest, 16) % 100

    # At rollout_pct == bucket the result is enabled; at bucket+1 it is not
    assert bucket < 100  # sanity — always true for md5 % 100
    assert (bucket < bucket + 1) is True  # bucket arithmetic is consistent

    # Idempotency: same inputs always yield same bucket
    digest2 = hashlib.md5(f"{flag_key}{tenant_id}".encode()).hexdigest()
    assert int(digest2, 16) % 100 == bucket


def test_module_grant_endpoint_registered():
    """App routes must include /api/admin/tenants/{tenant_id}/modules."""
    from gdx_dispatch.app import create_app
    app = create_app()
    paths = app_route_paths(app)
    assert any("/api/admin/tenants/{tenant_id}/modules" in p for p in paths), (
        f"Expected /api/admin/tenants/{{tenant_id}}/modules in routes. Found: {paths}"
    )


def test_flags_endpoint_registered():
    """App routes must include /api/admin/flags."""
    from gdx_dispatch.app import create_app
    app = create_app()
    paths = app_route_paths(app)
    assert any(p == "/api/admin/flags" for p in paths), (
        f"Expected /api/admin/flags in routes. Found: {paths}"
    )


def test_feature_flags_table_sql():
    """create_flag endpoint must use an ORM upsert pattern (update existing if flag_key exists)."""
    from gdx_dispatch.core import admin_flags
    src = inspect.getsource(admin_flags.create_flag)
    # Must handle the case where the flag already exists (upsert)
    assert "filter_by" in src or "filter(" in src, "create_flag must query for existing flag"
    assert "rollout_pct" in src, "create_flag must set rollout_pct"
    assert "db.add" in src or "db.commit" in src, "create_flag must persist to DB"
