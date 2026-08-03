"""PWA service worker and manifest support for GDX."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

PWARouter = APIRouter(tags=["pwa"])

# Canonical PWA assets ship with the Vite build (frontend/public/ → dist/).
# The static/ kill-switch survives only as a fallback for backend-only
# images with no frontend build — see service_worker() below.
_SW_DIST_PATH = Path(__file__).parent.parent / "frontend" / "dist" / "sw.js"
_SW_KILL_SWITCH_PATH = Path(__file__).parent.parent / "static" / "sw.js"
_MANIFEST_DIST_PATH = Path(__file__).parent.parent / "frontend" / "dist" / "manifest.webmanifest"


@PWARouter.get("/pwa/version")
async def pwa_version() -> JSONResponse:
    return JSONResponse(
        content={
            "version": os.getenv("APP_VERSION", "dev"),
            "build_time": os.getenv("BUILD_TIME", "unknown"),
        }
    )


@PWARouter.get("/manifest.json")
async def pwa_manifest() -> Response:
    """Serve the PWA manifest at the legacy /manifest.json URL.

    Canonical source is the Vite-built frontend/dist/manifest.webmanifest
    (index.html links /manifest.webmanifest, served by the SPA catch-all);
    this route keeps the old URL working for anything that cached it. The
    inline fallback covers backend-only deployments with no frontend build.
    """
    if _MANIFEST_DIST_PATH.exists():
        content = _MANIFEST_DIST_PATH.read_text(encoding="utf-8")
        return Response(content=content, media_type="application/manifest+json")
    manifest = {
        "name": "GDX Dispatch",
        "short_name": "GDX",
        "start_url": "/mobile",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0e1525",
        "theme_color": "#121c2f",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    return Response(content=json.dumps(manifest), media_type="application/manifest+json")


# Keep legacy path for backwards compatibility
@PWARouter.get("/pwa/manifest.json")
async def pwa_manifest_legacy() -> Response:
    return await pwa_manifest()


@PWARouter.get("/sw.js")
async def service_worker() -> Response:
    """Serve the service worker from the root path (root scope).

    Preference order:
    1. frontend/dist/sw.js — the real SW (Web Push display + notification
       click). It deliberately has NO fetch handler and NO precache; the
       2026-04-11 stale-chunk incident came from a cache-first SW, and this
       route was serving the kill-switch below until 2026-08-03, silently
       shadowing the push SW the tech-mobile sprint shipped in dist/.
    2. static/sw.js — kill-switch fallback for backend-only images: no
       frontend build means no push SW, so dismantle whatever is installed.
    3. Inline kill-switch if even the static file is missing.
    """
    headers = {
        "Service-Worker-Allowed": "/",
        "Cache-Control": "no-cache, no-store, must-revalidate",
    }
    for _sw_path in (_SW_DIST_PATH, _SW_KILL_SWITCH_PATH):
        if _sw_path.exists():
            return Response(
                content=_sw_path.read_text(encoding="utf-8"),
                media_type="application/javascript",
                headers=headers,
            )
    # Fallback SW — same kill-switch behavior as gdx_dispatch/static/sw.js so the
    # legacy PWA gets dismantled even if the static file is missing.
    fallback = (
        "// GDX Service Worker — kill-switch (fallback)\n"
        "self.addEventListener('install', () => self.skipWaiting());\n"
        "self.addEventListener('activate', (event) => {\n"
        "  event.waitUntil((async () => {\n"
        "    try {\n"
        "      const keys = await caches.keys();\n"
        "      await Promise.all(keys.map((k) => caches.delete(k)));\n"
        "    } catch (_) {}\n"
        "    try { await self.registration.unregister(); } catch (_) {}\n"
        "    try {\n"
        "      const ws = await self.clients.matchAll({ type: 'window' });\n"
        "      ws.forEach((c) => { if ('navigate' in c) c.navigate(c.url); });\n"
        "    } catch (_) {}\n"
        "  })());\n"
        "});\n"
    )
    return Response(content=fallback, media_type="application/javascript", headers=headers)
