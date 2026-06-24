"""Tests for the plugin SDK surface (ADR-013 step 2): forwarded-identity context,
require_module gating, and PluginBase. Needs FastAPI/SQLAlchemy → runs in the
docker image, not host-standalone.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.plugin_api.base import PluginBase
from gdx_dispatch.plugin_api.context import (
    H_MODULES,
    H_ROLE,
    H_TENANT,
    H_USER,
    PluginContext,
    require_module,
)


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/items")
    def items(ctx: PluginContext = Depends(require_module("example"))):
        return {
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "role": ctx.role,
            "modules": sorted(ctx.enabled_modules),
        }

    return app


def _headers(tenant="t1", user="u1", role="admin", modules="example,other"):
    out = {}
    if tenant is not None:
        out[H_TENANT] = tenant
    if user is not None:
        out[H_USER] = user
    if role is not None:
        out[H_ROLE] = role
    if modules is not None:
        out[H_MODULES] = modules
    return out


def test_missing_tenant_is_400():
    c = TestClient(_app())
    r = c.get("/items", headers=_headers(tenant=None))
    assert r.status_code == 400


def test_module_not_enabled_is_403():
    c = TestClient(_app())
    r = c.get("/items", headers=_headers(modules="other,billing"))  # no 'example'
    assert r.status_code == 403
    assert "not enabled" in r.json()["detail"]


def test_enabled_module_passes_and_parses_context():
    c = TestClient(_app())
    r = c.get("/items", headers=_headers(modules="example, other"))  # spaces tolerated
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "t1"
    assert body["user_id"] == "u1"
    assert body["role"] == "admin"
    assert body["modules"] == ["example", "other"]


def test_empty_modules_header_means_nothing_enabled():
    c = TestClient(_app())
    r = c.get("/items", headers=_headers(modules=""))
    assert r.status_code == 403


def test_plugin_base_has_usable_metadata():
    # A plugin model inheriting PluginBase must register on the shared metadata
    # so the plugin-host migration phase can see it.
    from sqlalchemy import Column, Integer, String

    class _Widget(PluginBase):
        __tablename__ = "plug_test_widgets"
        id = Column(Integer, primary_key=True)
        name = Column(String(50))

    assert "plug_test_widgets" in PluginBase.metadata.tables
