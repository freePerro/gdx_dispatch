"""The plugin-host FastAPI app.

On startup it discovers installed plugins (entry-point group `gdx.modules`),
mounts each one's router under `/api/plugins/<key>`, and exposes a small catalog
the core app / frontend reads. Per-request gating is the plugin's own
`require_module` dependency (checks the forwarded enabled-modules set) — the host
mounts every *installed* plugin; *enablement* is decided per request.

Reserved host routes (`/api/plugins`, `/api/plugins/<key>/ui`) are registered
BEFORE plugin routers so a plugin can't shadow them. Plugins must not define a
root `/ui` route.

See gdx_dispatch/docs/decisions/ADR-013-third-party-module-plugins.md.
"""
from __future__ import annotations

import logging
import os
import signal
import threading

from fastapi import FastAPI, HTTPException, WebSocket

from gdx_dispatch.plugin_api.discovery import discover_plugins

log = logging.getLogger(__name__)


def create_plugin_host(plugins=None) -> FastAPI:
    """Build the plugin-host app. `plugins` is injectable for tests; in
    production it defaults to live entry-point discovery."""
    if plugins is None:
        plugins = discover_plugins()
    catalog = {p.key: p for p in plugins}

    app = FastAPI(title="GDX Plugin Host")

    # --- reserved host routes (registered first so plugins can't shadow them) ---
    @app.get("/health")
    def health():
        return {"status": "ok", "plugins": sorted(catalog)}

    @app.get("/api/plugins")
    def list_plugins():
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
    async def browser_ws(ws: WebSocket, url: str):
        """Stream a headless browser to the operator (ADR-014). Internal-only —
        reached solely via the core proxy, which enforces owner role + consent
        before relaying here. `url` is allowlist-checked in stream_browser."""
        from gdx_dispatch.plugin_host.browser_stream import stream_browser

        await stream_browser(ws, url)

    @app.get("/api/plugins/{key}/ui")
    def plugin_ui(key: str):
        p = catalog.get(key)
        if p is None:
            raise HTTPException(status_code=404, detail=f"unknown plugin: {key}")
        return p.ui or {}

    # --- plugin routers ---
    for p in catalog.values():
        if p.router is not None:
            app.include_router(p.router, prefix=f"/api/plugins/{p.key}")
        log.info("plugin-host mounted key=%s name=%s router=%s",
                 p.key, p.name, p.router is not None)

    return app
