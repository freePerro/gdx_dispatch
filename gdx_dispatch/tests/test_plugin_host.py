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


def test_health_is_liveness_always_200_even_when_degraded():
    # Liveness must NOT depend on plugin/DB desired-state: a degraded-but-serving
    # host that 503'd liveness would be killed into a not-serving one under k8s
    # (CrashLoopBackOff). Degradation belongs on /ready, not /health.
    c = TestClient(create_plugin_host(plugins=[_demo_plugin()],
                                      degraded=["gdx-plugin-chi-pricing==0.1.2"]))
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "plugins": ["demo"]}


def test_ready_is_503_degraded_when_a_desired_plugin_failed_to_install():
    # A desired plugin that didn't install must read as not-ready, not masquerade
    # as a healthy host serving a partial catalog (2026-06-29 outage).
    c = TestClient(create_plugin_host(plugins=[_demo_plugin()],
                                      degraded=["gdx-plugin-chi-pricing==0.1.2"]))
    r = c.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["plugins"] == ["demo"]
    assert body["missing"] == ["gdx-plugin-chi-pricing==0.1.2"]


def test_ready_is_200_when_everything_healthy():
    c = TestClient(create_plugin_host(plugins=[_demo_plugin()]))
    r = c.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "plugins": ["demo"]}


def test_stale_plugin_is_withheld_fail_closed():
    # Fail closed: a plugin loaded at the wrong version serves NOTHING — better an
    # absent pricing plugin than one quoting money off last week's logic.
    stale = {"demo": {"installed": "0.1.0", "desired": "0.2.0"}}
    c = TestClient(create_plugin_host(plugins=[_demo_plugin()], stale=stale))
    # excluded from the catalog (frontend sees it as unavailable)
    assert c.get("/api/plugins").json() == []
    # every sub-path of the stale plugin 503s with a clear reason
    r = c.get("/api/plugins/demo/ping")
    assert r.status_code == 503
    assert "stale" in r.json()["detail"].lower() or "degraded" in r.json()["detail"].lower()
    # the reserved /ui route must also fail closed (it's registered before the
    # catch-all, so it would otherwise 404 a stale key instead of 503ing).
    assert c.get("/api/plugins/demo/ui").status_code == 503
    # and the host reports not-ready, naming the stale plugin + versions
    rdy = c.get("/ready")
    assert rdy.status_code == 503
    assert rdy.json()["stale"]["demo"] == {"installed": "0.1.0", "desired": "0.2.0"}


def test_healthy_plugin_still_served_when_another_is_stale():
    def _other():
        from fastapi import APIRouter
        r = APIRouter()

        @r.get("/ok")
        def ok():
            return {"ok": True}
        return PluginManifest(key="good", name="Good", tier="business", router=r)

    stale = {"demo": {"installed": "0.1.0", "desired": "0.2.0"}}
    c = TestClient(create_plugin_host(plugins=[_demo_plugin(), _other()], stale=stale))
    assert [p["key"] for p in c.get("/api/plugins").json()] == ["good"]
    assert c.get("/api/plugins/good/ok").json() == {"ok": True}  # healthy one serves
    assert c.get("/api/plugins/demo/ping").status_code == 503     # stale one withheld


def _patch_boot(monkeypatch, *, reconcile_fn, discovered, desired=None, create_all=None):
    """Wire up main.build_app's collaborators for a boot-resilience test."""
    import gdx_dispatch.plugin_host.main as main
    monkeypatch.setattr(main, "reconcile", reconcile_fn)
    monkeypatch.setattr(main, "discover_with_dists", lambda: discovered)
    monkeypatch.setattr(main, "desired_versions", lambda db: desired or {})
    monkeypatch.setattr(main, "SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr(main.PluginBase.metadata, "create_all",
                        create_all or (lambda **k: None))
    monkeypatch.setattr(main, "reconcile_plugin_columns", lambda *a, **k: None)
    return main


class _FakeDB:
    def close(self):
        pass


def test_build_app_degrades_instead_of_dying_when_reconcile_crashes(monkeypatch):
    # The 2026-06-29 outage: an unguarded reconcile() exception aborted the whole
    # boot. Now boot must survive it, still serve already-installed plugins, and
    # report the failure as degraded on /ready — never take the surface down.
    def _boom():
        raise RuntimeError("DB down / bad DDL / network hang")

    main = _patch_boot(monkeypatch, reconcile_fn=_boom,
                       discovered=[(_demo_plugin(), "demo_dist", "0.1.0")])
    app = main.build_app()  # must NOT raise
    c = TestClient(app)
    assert c.get("/health").status_code == 200          # liveness up
    r = c.get("/ready")
    assert r.status_code == 503                          # not-ready, loudly
    assert r.json()["plugins"] == ["demo"]              # installed plugin still served
    assert any("reconcile-error" in m for m in r.json()["missing"])
    assert c.get("/api/plugins/demo/ping").json() == {"pong": True}  # router reachable


def test_build_app_degrades_on_schema_failure_but_still_serves(monkeypatch):
    from gdx_dispatch.plugin_host.reconcile import ReconcileResult

    def _bad_create_all(**k):
        raise RuntimeError("create_all blew up")

    main = _patch_boot(monkeypatch, reconcile_fn=lambda: ReconcileResult([], []),
                       discovered=[(_demo_plugin(), "demo_dist", "0.1.0")],
                       create_all=_bad_create_all)
    r = TestClient(main.build_app()).get("/ready")
    assert r.status_code == 503
    assert any("schema-error" in m for m in r.json()["missing"])
    assert r.json()["plugins"] == ["demo"]


def test_build_app_withholds_stale_plugin_detected_at_boot(monkeypatch):
    # End-to-end: desired 0.2.0 but the installed dist is 0.1.0 → build_app must
    # withhold the plugin (fail closed) and report it stale on /ready.
    from gdx_dispatch.plugin_host.reconcile import ReconcileResult

    main = _patch_boot(monkeypatch, reconcile_fn=lambda: ReconcileResult([], []),
                       discovered=[(_demo_plugin(), "demo_dist", "0.1.0")],
                       desired={"demo_dist": "0.2.0"})
    c = TestClient(main.build_app())
    assert c.get("/api/plugins").json() == []           # withheld from catalog
    assert c.get("/api/plugins/demo/ping").status_code == 503
    assert c.get("/ready").json()["stale"]["demo"]["installed"] == "0.1.0"


def test_build_app_healthy_when_everything_succeeds(monkeypatch):
    from gdx_dispatch.plugin_host.reconcile import ReconcileResult

    main = _patch_boot(monkeypatch, reconcile_fn=lambda: ReconcileResult([], []),
                       discovered=[(_demo_plugin(), "demo_dist", "0.1.0")],
                       desired={"demo_dist": "0.1.0"})  # installed == desired
    c = TestClient(main.build_app())
    assert c.get("/health").json() == {"status": "ok", "plugins": ["demo"]}
    assert c.get("/ready").status_code == 200


def test_restart_endpoint_schedules_sigterm_without_dying(monkeypatch):
    # The route schedules a SIGTERM via threading.Timer; patch Timer so the
    # test process is NOT actually killed, and assert it responds + arms it.
    import gdx_dispatch.plugin_host.app as host_app

    armed = {}

    class _FakeTimer:
        def __init__(self, delay, fn):
            armed["delay"] = delay

        def start(self):
            armed["started"] = True

    monkeypatch.setattr(host_app.threading, "Timer", _FakeTimer)
    c = TestClient(create_plugin_host(plugins=[]))
    r = c.post("/internal/restart")
    assert r.status_code == 200
    assert r.json() == {"status": "restarting"}
    assert armed.get("started") is True
