"""Tests for the plugin-host app (ADR-013 step 3): discovery-driven mounting,
the reserved catalog routes, and that a plugin's own router is reachable.
Needs FastAPI → runs in the docker image.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.testclient import TestClient

from gdx_dispatch.plugin_api.manifest import PluginManifest
from gdx_dispatch.plugin_host.app import create_plugin_host


def _demo_plugin():
    r = APIRouter()

    @r.get("/ping")
    def ping():
        return {"pong": True}

    return PluginManifest(
        key="demo",
        name="Demo",
        tier="professional",
        router=r,
        ui={"screens": [{"type": "list", "title": "Demo Items"}]},
    )


def test_health_lists_no_plugins_when_empty():
    c = TestClient(create_plugin_host(plugins=[]))
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "plugins": []}


def test_catalog_lists_installed_plugin_with_ui():
    c = TestClient(create_plugin_host(plugins=[_demo_plugin()]))
    r = c.get("/api/plugins")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["key"] == "demo"
    assert items[0]["ui"]["screens"][0]["title"] == "Demo Items"


def test_ui_endpoint_returns_manifest():
    c = TestClient(create_plugin_host(plugins=[_demo_plugin()]))
    r = c.get("/api/plugins/demo/ui")
    assert r.status_code == 200
    assert r.json()["screens"][0]["type"] == "list"


def test_ui_endpoint_404_for_unknown():
    c = TestClient(create_plugin_host(plugins=[_demo_plugin()]))
    assert c.get("/api/plugins/nope/ui").status_code == 404


def test_plugin_router_is_mounted_and_reachable():
    c = TestClient(create_plugin_host(plugins=[_demo_plugin()]))
    r = c.get("/api/plugins/demo/ping")
    assert r.status_code == 200
    assert r.json() == {"pong": True}


def test_health_reflects_mounted_plugin():
    c = TestClient(create_plugin_host(plugins=[_demo_plugin()]))
    assert c.get("/health").json()["plugins"] == ["demo"]
