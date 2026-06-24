"""
Tests for tenant module management APIs.
"""
from __future__ import annotations

from gdx_dispatch.tests.conftest import app_route_paths


def test_admin_modules_module_imports():
    """admin_modules router must import cleanly and expose a FastAPI APIRouter."""
    from fastapi import APIRouter

    from gdx_dispatch.core.admin_modules import router
    assert isinstance(router, APIRouter)


def test_module_grant_endpoint_registered():
    """App routes must include /api/admin/tenants/{tenant_id}/modules."""
    from gdx_dispatch.app import create_app
    app = create_app()
    paths = app_route_paths(app)
    assert any("/api/admin/tenants/{tenant_id}/modules" in p for p in paths), (
        f"Expected /api/admin/tenants/{{tenant_id}}/modules in routes. Found: {paths}"
    )
