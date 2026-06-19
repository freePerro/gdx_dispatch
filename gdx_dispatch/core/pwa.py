"""PWA service worker and manifest support for GDX."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

PWARouter = APIRouter(tags=["pwa"])

_SW_JS_PATH = Path(__file__).parent.parent / "static" / "sw.js"
_MANIFEST_PATH = Path(__file__).parent.parent / "templates" / "manifest.json"


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
    """Serve PWA manifest from /manifest.json (root path required by PWA spec)."""
    if _MANIFEST_PATH.exists():
        content = _MANIFEST_PATH.read_text(encoding="utf-8")
        return Response(content=content, media_type="application/manifest+json")
    # Fallback inline manifest
    manifest = {
        "name": "DispatchApp",
        "short_name": "GDX",
        "start_url": "/dashboard",
        "display": "standalone",
        "background_color": "#1e40af",
        "theme_color": "#1e40af",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    return Response(content=json.dumps(manifest), media_type="application/manifest+json")


# Keep legacy path for backwards compatibility
@PWARouter.get("/pwa/manifest.json")
async def pwa_manifest_legacy() -> Response:
    return await pwa_manifest()


@PWARouter.get("/sw.js")
async def service_worker() -> Response:
    """Serve service worker from root path /sw.js (required by browsers for full scope)."""
    if _SW_JS_PATH.exists():
        content = _SW_JS_PATH.read_text(encoding="utf-8")
        return Response(
            content=content,
            media_type="application/javascript",
            headers={
                "Service-Worker-Allowed": "/",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
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
    return Response(
        content=fallback,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )
