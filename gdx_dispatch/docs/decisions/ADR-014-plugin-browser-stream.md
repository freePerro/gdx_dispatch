# ADR-014 — Consent-gated browser stream for plugins

Status: Accepted (2026-06-25)
Builds on: [ADR-013](ADR-013-third-party-module-plugins.md) (plugin-host model)

## Context

Some integrations have **no API**. The motivating case is a supplier order-entry
portal (C.H.I. HubX) built as a Blazor Server app: pricing/specs are pushed over
a SignalR WebSocket as DOM diffs, there is no REST endpoint, and login is Azure
AD B2C — which needs a *human*. On a headless VPS there is no screen for that
human to log in on.

ADR-013 deliberately forbids plugins shipping browser JavaScript (manifest-driven
UI only). That keeps untrusted plugins safe but also blocks any "let me log into
this site from inside GDX" capability.

## Decision

Add a **core-provided, consent-gated browser stream**. The plugin does **not**
ship JS; core renders the capability. A plugin only *declares* it needs the
`browser` permission; an owner must consent before it can be used.

How it works:

1. **plugin-host** runs a headless Chromium and streams it over a WebSocket
   (`/internal/browser/ws`) using **CDP screencast** — JPEG frames out,
   `Input.dispatch*` events in. The remote site **never executes in the operator's
   page**; we ship pixels and forward input coordinates. An **allowlist**
   (`PLUGIN_BROWSER_ALLOWED_HOSTS`) prevents this from becoming an open proxy/SSRF.
2. **core** splits auth so the socket never hand-rolls authorization (which would
   skip the real gate stack — revocation denylist, DB user-verify, DB role
   overlay, tenant match — and reopen known bypass classes):
   - `POST /api/plugins/_browser/ticket` runs over HTTP, so the **full
     `get_current_user` gate stack** applies. It then checks **owner/superadmin**
     role, that the plugin *currently* declares `browser` (live catalog re-fetch,
     not a stale row), **recorded consent** (`plugin_consent`), and the URL
     allowlist — then mints a short-lived (~30s) signed ticket bound to (key, url).
   - WS `/api/plugins/_browser/ws?ticket=…` validates only the ticket (signature +
     expiry + scope), re-checks the allowlist, then relays. So a revoked or
     DB-demoted owner can't open a stream with a stale token.
3. **frontend** `BrowserStream.vue` draws frames to an `<img>` and forwards
   mouse/keyboard. It runs trusted *core* JS — no plugin code executes in the
   browser, so ADR-013's no-plugin-JS rule stands.
4. **manifest** gains `permissions: tuple[str,...]`; only `"browser"` is known
   today. `admin_plugins` exposes the declared permissions + risk text and records
   owner consent for exactly the permissions declared at consent time.

## Why screencast over embedding/VNC

Screencast means the remote DOM/JS never runs in GDX — the operator sees an image
and clicks coordinates. That removes the XSS surface of iframing a third-party
site and the heaviness of a VNC server, while needing no X server (headless).

## Security properties

- **Consent + capability gate, not consent alone.** Owner-only; consent recorded
  per declared permission so a later-added permission isn't silently covered.
- **No remote execution in GDX** (pixels + input only).
- **Allowlist** host check in core (ticket issue) and plugin-host, and on **every
  top-level navigation** inside the stream (route interception aborts off-allowlist
  redirects / JS `location=` / meta-refresh) so an allowlisted-but-hostile page
  can't pivot the server browser off-allowlist.
- **Short-lived signed ticket** for the socket; the heavy auth runs over HTTP.
- **Internal-only** plugin-host socket; reachable solely via the authed core proxy.

## Consequences

- Enables Phase 2 (in-app login) and is the substrate for a later full embedded
  workspace (navigate + capture), which only extends the same stream.
- The plugin-host now needs a browser image (`docker/Dockerfile.plugin-host`,
  Playwright base) and `ipc: host` + non-root `pwuser` (sandbox intact, no
  `--no-sandbox`).
- Input forwarding is keyboard/mouse only; this is for occasional operator use
  (login), not high-FPS interaction. One browser per connection (a warm pool is a
  later optimization).

## Residual risk (follow-up)

The nav guard is navigation-only by design (so pages still render), so a
**sub-resource** request (`fetch`/XHR/`<img>`) from an allowlisted-but-hostile
page to an internal host (e.g. `fetch('http://169.254.169.254/...')`) still
*egresses* from plugin-host — CORS stops the page reading the response, but the
request fires (blind/state-changing SSRF). The threat model already assumes a
semi-trusted target (owner-only, consent-gated, allowlisted entry) and no
response readback, so this does not block adoption. **Follow-up:** harden at the
container/network layer — deny the plugin-host egress to link-local (169.254/16)
and RFC1918 ranges — rather than by widening the predicate (which would break
legitimate sub-resources). Tracked as the next hardening step for this feature.
