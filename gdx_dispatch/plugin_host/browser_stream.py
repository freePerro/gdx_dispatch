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
import base64
import json
import logging
import os
import re
import tempfile
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_DEFAULT_HOSTS = "orderentry.chiohd.com,chiohd.b2clogin.com,hubx.chiohd.com"


def allowed_hosts() -> set[str]:
    raw = os.environ.get("PLUGIN_BROWSER_ALLOWED_HOSTS", _DEFAULT_HOSTS)
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in allowed_hosts()


# ── Remembered login (session persistence) ──────────────────────────────────
# The workspace used to start a FRESH browser every open, so the operator had to
# log into HubX each time. Now the post-login session (cookies/localStorage) is
# persisted per plugin key and reloaded on the next open — the operator stays
# signed in until the remote site expires the session, then just logs in once
# more. We deliberately do NOT store the username/password: scripting the Azure
# B2C password flow is brittle and can trip MFA (see the chi plugin's client),
# and a session file is a far smaller liability than a credential.
#
# At rest the state is Fernet-encrypted with the app's MASTER_ENCRYPTION_KEY
# (same key that guards QB OAuth tokens); keyless dev falls back to plaintext,
# mirroring core.pii. Default location is under the /plugins volume so it
# survives container recreation without a compose change.


def state_dir() -> str:
    d = os.environ.get("PLUGIN_BROWSER_STATE_DIR")
    if d:
        return d
    if os.path.isdir("/plugins"):  # the plugin-host volume (persists)
        return "/plugins/.browser-state"
    return os.path.join(tempfile.gettempdir(), "gdx-browser-state")  # dev


def _file_for(key: str, suffix: str) -> str | None:
    """Path for a plugin-keyed file, or None if the key yields nothing.
    The key is sanitized to [a-z0-9_-] so it can't traverse out of state_dir."""
    safe = re.sub(r"[^a-z0-9_-]", "", (key or "").lower())
    if not safe:
        return None
    return os.path.join(state_dir(), safe + suffix)


def state_file_for(key: str) -> str | None:
    return _file_for(key, ".session")


def creds_file_for(key: str) -> str | None:
    """The plugin's remembered login credentials ({username, password}) —
    autofilled into the remote sign-in form, same encryption as the session."""
    return _file_for(key, ".creds")


def _fernet():
    """The app's Fernet, or None in keyless dev. Lazy: this module must stay
    importable with no app context (see test_plugin_browser_stream.py)."""
    try:
        from gdx_dispatch.core import pii
        return getattr(pii, "_FERNET", None)
    except Exception:  # pragma: no cover - core not importable standalone
        return None


def load_state(path: str) -> dict | None:
    """Read + decrypt a saved session. Any failure (missing, corrupt, wrong
    key) returns None — the stream then starts fresh and the operator logs in."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        f = _fernet()
        if f is not None:
            try:
                raw = f.decrypt(raw)
            except Exception:
                pass  # plaintext-legacy / keyless-dev file — try it as JSON
        state = json.loads(raw)
        return state if isinstance(state, dict) else None
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("saved browser session unreadable (%s) — starting fresh", e)
        return None


def save_state(path: str, state: dict) -> None:
    """Encrypt + write a session atomically, private to the service user."""
    raw = json.dumps(state).encode()
    f = _fernet()
    if f is not None:
        raw = f.encrypt(raw)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


# Native <select> popups render in a separate compositor surface that
# Page.startScreencast does NOT capture — so dropdowns were invisible in the
# stream (Blazor's <InputSelect> emits a native <select>, so HubX hit this). This
# shim replaces the native popup with an in-page overlay (which the screencast
# DOES capture) and writes the value back + fires input/change so Blazor binding
# updates. Injected on every document via add_init_script.
_SELECT_SHIM = r"""
(() => {
  let ov = null;
  const close = () => { if (ov) { ov.remove(); ov = null; } };
  document.addEventListener('mousedown', (e) => {
    if (ov && ov.contains(e.target)) return;            // clicking an option
    const sel = e.target.closest && e.target.closest('select');
    close();
    if (!sel || sel.multiple || sel.disabled) return;
    e.preventDefault(); e.stopPropagation();
    const r = sel.getBoundingClientRect();
    ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;left:'+r.left+'px;top:'+r.bottom+'px;min-width:'+r.width+'px;max-height:260px;overflow:auto;background:#fff;border:1px solid #888;z-index:2147483647;font:14px sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.3)';
    Array.from(sel.options).forEach((o, i) => {
      const it = document.createElement('div');
      it.textContent = o.text;
      it.style.cssText = 'padding:4px 10px;cursor:pointer;white-space:nowrap;' + (i===sel.selectedIndex ? 'background:#1a73e8;color:#fff' : '');
      it.addEventListener('mousedown', (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        sel.value = o.value; sel.selectedIndex = i;
        sel.dispatchEvent(new Event('input', {bubbles:true}));
        sel.dispatchEvent(new Event('change', {bubbles:true}));
        close();
      });
      ov.appendChild(it);
    });
    document.body.appendChild(ov);
  }, true);
})();
"""


# Pick the largest <img>/<canvas> on the page (skipping icons/logos) — on a door
# view that's the door rendering. For an <img> we also return its source + natural
# size so we can fetch the ORIGINAL (full-res) image, not the scaled-down on-screen
# pixels; for a <canvas> we read its backing store directly.
_LARGEST_MEDIA_JS = """
() => {
  const els = [...document.querySelectorAll('img, canvas')];
  let best = null, area = 0;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 80 || r.height < 80) continue;     // skip icons / logos
    const a = r.width * r.height;
    if (a > area) { area = a; best = el; }
  }
  if (!best) return null;
  const r = best.getBoundingClientRect();
  const o = {kind: best.tagName.toLowerCase(),
             x: r.left + scrollX, y: r.top + scrollY, width: r.width, height: r.height};
  if (o.kind === 'img') { o.src = best.currentSrc || best.src || null;
                          o.nw = best.naturalWidth; o.nh = best.naturalHeight; }
  if (o.kind === 'canvas') { try { o.dataurl = best.toDataURL('image/png'); } catch (e) { o.dataurl = null; } }
  return o;
}
"""


async def _capture_door_image(page):
    """Best-effort, full-resolution picture of the door. Strategy, best first:
      * <img>: fetch the ORIGINAL source bytes through the page's session (full
        res — the on-screen render is scaled down and looks terrible blown up).
      * <canvas>: read its backing store via toDataURL (native resolution).
      * else: screenshot just the element's box, then the whole viewport.
    Returns (data_url | None, meta | None). meta.kind records which path ran so a
    bad pick can be tuned without guessing.
    """
    try:
        info = await page.evaluate(_LARGEST_MEDIA_JS)
        if not info:
            png = await page.screenshot()
            return "data:image/png;base64," + base64.b64encode(png).decode(), {"kind": "viewport"}

        kind = info.get("kind")
        if kind == "canvas" and info.get("dataurl"):
            return info["dataurl"], {"kind": "canvas", "w": round(info["width"]), "h": round(info["height"])}

        if kind == "img" and info.get("src"):
            src = info["src"]
            if src.startswith("data:"):                       # already inline — use as-is
                return src, {"kind": "img-data", "nw": info.get("nw"), "nh": info.get("nh")}
            if (info.get("nw") or 0) >= 120:                  # has a real source to fetch
                try:
                    from urllib.parse import urljoin
                    resp = await page.request.get(urljoin(page.url, src))
                    if resp.ok:
                        body = await resp.body()
                        mime = (resp.headers.get("content-type") or "image/png").split(";")[0].strip()
                        if body and mime.startswith("image/"):
                            return ("data:" + mime + ";base64," + base64.b64encode(body).decode(),
                                    {"kind": "img-src", "nw": info.get("nw"), "nh": info.get("nh"), "src": src})
                except Exception as e:
                    log.debug("img src fetch failed: %s", e)

        # Fallback: element box, then viewport.
        clip = {"x": max(info["x"], 0.0), "y": max(info["y"], 0.0),
                "width": info["width"], "height": info["height"]}
        png = await page.screenshot(clip=clip)
        return ("data:image/png;base64," + base64.b64encode(png).decode(),
                {"kind": str(kind) + "-clip", "w": round(info["width"]), "h": round(info["height"])})
    except Exception as e:
        log.debug("door image capture failed: %s", e)
        return None, None


async def stream_browser(ws, url: str, key: str = "") -> None:
    """Drive a headless page and relay screencast frames <-> input over `ws`.

    `ws` is a Starlette/FastAPI WebSocket (accept/send_text/receive_text/close).
    `key` is the plugin key — when given, the browser session is reloaded from /
    persisted to that plugin's saved state, so a HubX login survives across
    workspace opens (the "remember me" path).
    """
    if not host_allowed(url):
        await ws.close(code=4403)
        log.warning("browser-stream refused disallowed url host: %s", url)
        return
    await ws.accept()

    from playwright.async_api import async_playwright

    state_path = state_file_for(key)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--disable-dev-shm-usage"]
        )
        persist = None  # set once the context exists; called again on disconnect
        try:
            ctx_kwargs: dict = {"viewport": {"width": 1280, "height": 800}}
            saved = load_state(state_path) if state_path else None
            if saved:
                ctx_kwargs["storage_state"] = saved
            try:
                ctx = await browser.new_context(**ctx_kwargs)
            except Exception:
                # A malformed saved session must never brick the workspace.
                if "storage_state" not in ctx_kwargs:
                    raise
                log.warning("saved browser session rejected — starting fresh")
                del ctx_kwargs["storage_state"]
                ctx = await browser.new_context(**ctx_kwargs)

            async def _persist_session() -> None:
                if not state_path:
                    return
                try:
                    save_state(state_path, await ctx.storage_state())
                except Exception as e:
                    log.warning("could not persist browser session: %s", e)

            persist = _persist_session

            # Remembered credentials: whenever a page with a password field
            # shows up (the B2C sign-in after the saved session expired), fill
            # username + password so the operator only clicks "Sign in". FILL
            # ONLY — never auto-submit: a stale password that auto-submits in a
            # loop can lock the remote account, and any MFA step stays human.
            # Only allowlisted hosts can ever load here (nav guard), so the
            # fill can't leak the credential to an arbitrary site.
            creds_path = creds_file_for(key)
            creds = load_state(creds_path) if creds_path else None

            async def _autofill() -> None:
                if not creds or not (creds.get("username") or creds.get("password")):
                    return
                try:
                    pw_box = page.locator("input[type=password]:visible").first
                    await pw_box.wait_for(state="visible", timeout=4_000)
                except Exception:
                    return  # no sign-in form on this page — the common case
                try:
                    if creds.get("username"):
                        # B2C's ids first, then generic username/email inputs.
                        user_box = page.locator(
                            "#signInName, #email, input[type=email]:visible, "
                            "input[autocomplete=username]:visible, "
                            "input[type=text]:visible"
                        ).first
                        await user_box.fill(creds["username"], timeout=2_000)
                    if creds.get("password"):
                        await pw_box.fill(creds["password"], timeout=2_000)
                    log.info("browser-stream autofilled the sign-in form")
                except Exception as e:
                    log.debug("autofill skipped: %s", e)

            # Make native <select> dropdowns visible in the screencast (see shim).
            await ctx.add_init_script(_SELECT_SHIM)
            page = await ctx.new_page()
            # Try the autofill on every navigation — the sign-in page can appear
            # at connect (expired session) or mid-session (B2C re-auth bounce).
            page.on("domcontentloaded", lambda _p: asyncio.create_task(_autofill()))

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
                elif kind == "text":
                    # Typed printable chars + pasted text: insert as a literal
                    # string so punctuation isn't mis-mapped to a virtual key (e.g.
                    # "." was being sent as VK_DELETE). insertText fires
                    # input/beforeinput but NOT per-char keydown — fine for login
                    # forms (they read the final value). Cap length to bound a
                    # giant paste (the operator's own session, but still).
                    await cdp.send("Input.insertText", {"text": str(ev.get("text", ""))[:100_000]})
                elif kind == "capture":
                    # Read the live (already-logged-in) page's visible text + URL
                    # so a plugin can extract structured data from the page the
                    # operator is looking at — no second browser, no saved
                    # session. Generic: core ships the page text + URL; the
                    # plugin decides what to parse out of it. innerText (not
                    # innerHTML) keeps it small and is what the spec parser reads.
                    try:
                        txt = await page.evaluate("() => document.body.innerText")
                    except Exception as e:  # mid-navigation / detached frame
                        txt = ""
                        log.debug("capture innerText failed: %s", e)
                    image, image_meta = await _capture_door_image(page)
                    await ws.send_text(json.dumps(
                        {"type": "capture", "url": page.url, "text": str(txt)[:500_000],
                         "image": image, "image_meta": image_meta}))
                    # A capture proves a logged-in session — snapshot it now, in
                    # case the container dies before the disconnect save runs.
                    await _persist_session()
                elif kind == "key":
                    await cdp.send("Input.dispatchKeyEvent", ev["payload"])
                elif kind == "nav" and host_allowed(ev.get("url", "")):
                    await page.goto(ev["url"], wait_until="domcontentloaded")
                elif kind == "save_session":
                    # Dump the post-login session so the caller can persist it,
                    # and persist it server-side too (the remember-me file).
                    state = await ctx.storage_state()
                    await _persist_session()
                    await ws.send_text(json.dumps({"type": "session", "state": state}))
                elif kind == "close":
                    break
        finally:
            # Save the session on EVERY disconnect (close message, tab closed,
            # socket error) — this is what keeps the operator logged in next time.
            if persist is not None:
                await persist()
            await browser.close()
