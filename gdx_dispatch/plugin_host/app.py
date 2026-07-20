"""The plugin-host FastAPI app.

On startup it discovers installed plugins (entry-point group `gdx.modules`),
mounts each one's router under `/api/plugins/<key>`, and exposes a small catalog
the core app / frontend reads. Per-request gating is the plugin's own
`require_module` dependency (checks the forwarded enabled-modules set) — the host
mounts every *installed* plugin; *enablement* is decided per request.

Reserved host routes (`/api/plugins`, `/api/plugins/<key>/ui`) are registered
BEFORE plugin routers so a plugin can't shadow them. Plugins must not define a
root `/ui` route.

Health model (2026-06-29 follow-up, k8s probe semantics):
  /health  LIVENESS  — 200 whenever the process is up and serving. Never depends
                       on plugin/DB desired-state; a liveness probe that checks
                       external deps would kill a degraded-but-serving host into
                       a not-serving one (CrashLoopBackOff).
  /ready   READINESS — 503 when desired-state isn't fully met (a plugin failed to
                       install, or loaded STALE and was withheld). This is the
                       signal the Docker healthcheck + operators watch.

Fail closed (scoped): a plugin loaded at the WRONG version (`stale`) serves
NOTHING over its live endpoints — dropped from the catalog and every
`/api/plugins/<key>/*` sub-path (incl. `/ui`) returns 503, so the core proxy
transparently fails closed. This guards plugins whose money path is LIVE — e.g.
chi-pricing captures a door's price and builds estimate lines through these
routes, so withholding them stops a stale build from quoting customers.

LIMITATION: it does NOT protect an ADR-015 pack whose pricing *strategy* was
copied into a core CustomCatalog at create time — that catalog prices from the
copied `pricing_strategy`/`pricing_config` in core (catalog._retail_for) and
never calls the plugin-host, so withholding the route changes nothing there.
Invalidating catalogs sourced from a withheld pack is tracked in ADR-017.

See gdx_dispatch/docs/decisions/ADR-013-third-party-module-plugins.md.
"""
from __future__ import annotations

import logging
import os
import signal
import threading

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import JSONResponse

from gdx_dispatch.plugin_api.discovery import discover_plugins

log = logging.getLogger(__name__)

_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def create_plugin_host(plugins=None, degraded=None, stale=None) -> FastAPI:
    """Build the plugin-host app. `plugins` is injectable for tests; in
    production it defaults to live entry-point discovery.

    `degraded` — desired specs that failed to install this boot (from reconcile).
    `stale`    — {plugin_key: {installed, desired}} for plugins loaded at the
                 wrong version; these are WITHHELD (fail closed), not served.
    Either being non-empty makes /ready report 503."""
    if plugins is None:
        plugins = discover_plugins()
    degraded = list(degraded or [])
    stale = dict(stale or {})
    # Only NON-stale plugins are served; a stale plugin is withheld so it can't
    # emit possibly-wrong data (pricing!) under an authoritative 200.
    catalog = {p.key: p for p in plugins if p.key not in stale}

    app = FastAPI(title="GDX Plugin Host")

    def _degraded_payload() -> dict:
        return {"status": "degraded", "plugins": sorted(catalog),
                "missing": sorted(degraded),
                "stale": {k: stale[k] for k in sorted(stale)}}

    # --- reserved host routes (registered first so plugins can't shadow them) ---
    @app.get("/health")
    def health():
        # Liveness only — always 200 if the process is up (see module docstring).
        return {"status": "ok", "plugins": sorted(catalog)}

    @app.get("/ready")
    def ready():
        # Readiness — 503 when a desired plugin is missing or was withheld stale.
        if degraded or stale:
            return JSONResponse(status_code=503, content=_degraded_payload())
        return {"status": "ok", "plugins": sorted(catalog)}

    @app.get("/api/plugins")
    def list_plugins():
        # Stale plugins are absent from the catalog so the frontend shows them as
        # unavailable rather than offering a possibly-stale plugin.
        return [
            {"key": p.key, "name": p.name, "tier": p.tier, "ui": p.ui,
             "permissions": list(getattr(p, "permissions", ())),
             # ADR-015 Catalog Pack contributions — DATA the core catalog reads.
             "catalog_types": list(getattr(p, "catalog_types", ())),
             "pricing_strategies": list(getattr(p, "pricing_strategies", ())),
             "importers": list(getattr(p, "importers", ()))}
            for p in catalog.values()
        ]

    @app.post("/internal/restart")
    def restart_self():
        """Exit the process so Docker (restart: unless-stopped) recreates the
        container — a fresh boot re-runs reconcile() + discovery, which is how
        in-app plugin installs/removals take effect. This is safe because
        plugin-host is a SEPARATE container from the core app: the app keeps
        serving while plugin-host cycles. Internal-only — no host port, reached
        only from the core app over the compose network (not under /api/plugins,
        so the public proxy can't route here). SIGTERM (not os._exit) lets
        uvicorn shut down gracefully; the 0.5s delay lets this response flush."""
        threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return {"status": "restarting"}

    @app.websocket("/internal/browser/ws")
    async def browser_ws(ws: WebSocket, url: str, key: str = ""):
        """Stream a headless browser to the operator (ADR-014). Internal-only —
        reached solely via the core proxy, which enforces auth + owner role +
        consent before relaying here. `url` is allowlist-checked in
        stream_browser; `key` scopes the remembered login to one plugin."""
        from gdx_dispatch.plugin_host.browser_stream import stream_browser

        await stream_browser(ws, url, key)

    @app.get("/api/plugins/{key}/ui")
    def plugin_ui(key: str):
        # This literal route is registered before the stale catch-all, so it must
        # itself fail closed for a withheld plugin (else /ui would 404, not 503).
        if key in stale:
            info = stale[key]
            raise HTTPException(
                status_code=503,
                detail=(f"plugin '{key}' is degraded: installed "
                        f"{info.get('installed')}, desired {info.get('desired')}. "
                        "Refusing to serve possibly-stale data."),
            )
        p = catalog.get(key)
        if p is None:
            raise HTTPException(status_code=404, detail=f"unknown plugin: {key}")
        return p.ui or {}

    # --- fail-closed shims for withheld stale plugins (before real routers) ---
    # Every sub-path of a stale plugin returns 503 with a clear reason, so the
    # core proxy propagates a definitive "refusing to serve stale" instead of a
    # confusing 404 or — worse — a 200 from outdated logic.
    for key in sorted(stale):
        info = stale[key]

        def _stale_handler(rest: str = "", _key=key, _info=info):
            raise HTTPException(
                status_code=503,
                detail=(f"plugin '{_key}' is degraded: installed "
                        f"{_info.get('installed')}, desired {_info.get('desired')}. "
                        "Refusing to serve possibly-stale data."),
            )

        app.add_api_route(f"/api/plugins/{key}", _stale_handler, methods=_PROXY_METHODS)
        app.add_api_route(f"/api/plugins/{key}/{{rest:path}}", _stale_handler,
                          methods=_PROXY_METHODS)
        log.error("plugin-host WITHHELD stale plugin key=%s installed=%s desired=%s",
                  key, info.get("installed"), info.get("desired"))

    # --- plugin routers (served, non-stale plugins only) ---
    for p in catalog.values():
        if p.router is not None:
            app.include_router(p.router, prefix=f"/api/plugins/{p.key}")
        log.info("plugin-host mounted key=%s name=%s router=%s",
                 p.key, p.name, p.router is not None)

    return app
