# Plugin browser stream (Phase 2)

How a plugin uses the consent-gated browser stream (see
[ADR-014](decisions/ADR-014-plugin-browser-stream.md)). Use it when an
integration has no API and needs a human in a real browser (e.g. an Azure B2C
login on a headless server).

## 1. Declare the permission

```python
manifest = PluginManifest(
    key="chipricing",
    name="CHI Pricing",
    permissions=("browser",),   # owner must consent before it can be used
    ui=UI,
)
```

## 2. Add a `browser` screen to the UI manifest

```python
UI = {"screens": [
    {"type": "browser", "title": "Connect HubX",
     "url": "https://orderentry.chiohd.com/"}
]}
```

The host renders `BrowserStream.vue` for it. The `url` host must be on the
allowlist (`PLUGIN_BROWSER_ALLOWED_HOSTS`, default: HubX domains).

## 3. Owner consents

Owner-only endpoints:

- `GET /api/admin/plugins/{key}/permissions` → declared permissions + risk text +
  whether already consented.
- `POST /api/admin/plugins/{key}/consent` → records consent for the permissions
  the plugin declares *now*.

Until consent exists, the stream proxy closes the socket (code 1008).

## 4. Operating it

- The operator opens the screen, sees the live browser, logs in, and clicks
  **Save login session** — the stream returns the Playwright `storageState`, which
  the plugin persists (encrypted) and reuses for headless reads.
- Frames are JPEG (pixels only — the remote site never executes in GDX). Mouse and
  keyboard are forwarded as CDP input events.

## Deployment

The plugin-host must run the browser-capable image:

```yaml
plugin-host:
  build: { context: ../.., dockerfile: gdx_dispatch/docker/Dockerfile.plugin-host }
  ipc: host          # shared-memory; Chromium OOMs without it
  # image runs as non-root pwuser → Chromium sandbox stays on (no --no-sandbox)
```
