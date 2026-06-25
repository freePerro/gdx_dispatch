"""Phase 2: stream a headless Chromium into the app over a WebSocket.

Why this exists: HubX (and sites like it) are Blazor Server with no API and an
Azure B2C login that needs a *human* — impossible on a headless VPS. This streams
the plugin-host's headless Chromium to the browser via CDP screencast (JPEG
frames out, input events in), so an operator can log in from inside GDX. See
ADR-014.

Security:
  * The remote site never executes in the operator's page — we ship JPEG frames
    and forward input coordinates. No remote DOM/JS touches GDX.
  * ALLOWED_HOSTS is an allowlist so this can't be turned into an open proxy /
    SSRF. Default is HubX only; override with PLUGIN_BROWSER_ALLOWED_HOSTS.
  * Reached only through the core proxy, which enforces auth + owner role +
    recorded consent before a socket ever opens here.

ponytail: one browser per connection (login is rare, single-operator). A warm
pool is a later optimization, not needed to prove the path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_DEFAULT_HOSTS = "orderentry.chiohd.com,chiohd.b2clogin.com,hubx.chiohd.com"


def allowed_hosts() -> set[str]:
    raw = os.environ.get("PLUGIN_BROWSER_ALLOWED_HOSTS", _DEFAULT_HOSTS)
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in allowed_hosts()


def nav_should_block(url: str, is_navigation: bool) -> bool:
    """True if a navigation must be aborted as off-allowlist (SSRF guard).

    Applies to ANY frame — main frame, iframes, and popups — because an
    allowlisted-but-hostile page can pivot via `<iframe src=http://169.254…>` or
    `window.open(...)`. Only http(s) navigations egress to a host; about:/data:/
    blob: have no network host so they pass (the next real navigation is caught).
    Non-navigation sub-resource requests pass so pages still render.
    """
    if not is_navigation:
        return False
    if urlparse(url).scheme not in ("http", "https"):
        return False
    return not host_allowed(url)


async def stream_browser(ws, url: str) -> None:
    """Drive a headless page and relay screencast frames <-> input over `ws`.

    `ws` is a Starlette/FastAPI WebSocket (accept/send_text/receive_text/close).
    """
    if not host_allowed(url):
        await ws.close(code=4403)
        log.warning("browser-stream refused disallowed url host: %s", url)
        return
    await ws.accept()

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--disable-dev-shm-usage"]
        )
        try:
            ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await ctx.new_page()

            # SSRF hardening: block any TOP-LEVEL navigation off the allowlist —
            # redirects, JS `location=`, meta-refresh. Sub-resources pass so pages
            # still render. Prevents an allowlisted-but-hostile page from pivoting
            # the server browser to an internal/arbitrary host.
            async def _guard(route):
                req = route.request
                try:
                    # ANY frame (main, iframe, popup) — not just main_frame, or an
                    # embedded <iframe src=http://169.254.169.254/> would egress.
                    if nav_should_block(req.url, req.is_navigation_request()):
                        log.warning("browser-stream blocked off-allowlist nav: %s", req.url)
                        await route.abort()
                        return
                    await route.continue_()
                except Exception:
                    await route.abort()

            await ctx.route("**/*", _guard)
            await page.goto(url, wait_until="domcontentloaded")
            cdp = await ctx.new_cdp_session(page)

            # Coalesce frames: never let more than one send be in flight, so a slow
            # client can't pile up unbounded tasks. We still ack every frame so
            # Chromium keeps emitting; we just skip pushing while busy (live view —
            # the next frame supersedes it anyway).
            sending = False

            async def _ack(session_id: str) -> None:
                try:
                    await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
                except Exception as e:
                    log.debug("screencast ack failed: %s", e)

            async def _send(params: dict) -> None:
                nonlocal sending
                sending = True
                try:
                    await ws.send_text(json.dumps({"type": "frame", "data": params["data"]}))
                except Exception as e:  # client gone / socket closing
                    log.debug("frame send failed (client likely gone): %s", e)
                finally:
                    sending = False
                    await _ack(params["sessionId"])

            def _on_frame(params: dict) -> None:
                if sending:
                    asyncio.create_task(_ack(params["sessionId"]))  # keep frames flowing
                else:
                    asyncio.create_task(_send(params))

            cdp.on("Page.screencastFrame", _on_frame)
            await cdp.send("Page.startScreencast",
                           {"format": "jpeg", "quality": 55,
                            "maxWidth": 1280, "maxHeight": 800})

            while True:
                ev = json.loads(await ws.receive_text())
                kind = ev.get("type")
                if kind == "mouse":
                    await cdp.send("Input.dispatchMouseEvent", ev["payload"])
                elif kind == "key":
                    await cdp.send("Input.dispatchKeyEvent", ev["payload"])
                elif kind == "nav" and host_allowed(ev.get("url", "")):
                    await page.goto(ev["url"], wait_until="domcontentloaded")
                elif kind == "save_session":
                    # Dump the post-login session so the caller can persist it.
                    state = await ctx.storage_state()
                    await ws.send_text(json.dumps({"type": "session", "state": state}))
                elif kind == "close":
                    break
        finally:
            await browser.close()
