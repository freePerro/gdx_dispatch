# Per-plugin authorization + a mobile estimate editor

Status: **BUILT** 2026-08-11. Two corrections the adversarial audit forced, both
recorded below where they bit: the proxy gate was fake (§ "The choke point"),
and the mobile editor plan was wrong about how estimate lines persist
(§ "Part 2").

Two asks, one loop: a tenant needs to say who may use each plugin, and a tech
needs somewhere on a phone to put what a plugin captured.

## Why now

`/api/plugins/*` is authorized by authentication alone. `proxy_to_plugin_host`
(`gdx_dispatch/routers/plugins_proxy.py`) depends on `get_current_user` and
nothing else; it forwards `X-GDX-Role` but the only far-side gate is
`require_module(...)` (`plugin_api/context.py`), which checks the **tenant's**
module grant, not the user's role. Neither the CHI nor Midland routers read
`ctx.role`. Any logged-in user can POST to any plugin route today.

Separately, `estimate_source` ("Add captured door") exists only in
`views/EstimateView.vue` (3050 lines, desktop). `MobileEstimatesView.vue` lists
and views estimates but cannot edit lines — its New button pushes to the desktop
`/estimates/new`. So a door captured in the field is finished at a desk.

---

## Part 1 — per-plugin authorization

### Model: reuse the existing RBAC. No new tables, no migration.

The tenant RBAC already exists: `TenantRole.permissions` is a JSON list on a
per-company row, `UserRoleAssignment` binds users to roles, `require_permission`
enforces, and `RolePermissionsView` edits it. Plugin authorization becomes
**permission keys**, not a parallel system.

Two layers:

| Key | Source | Meaning |
|---|---|---|
| `plugins.read` / `plugins.write` | **static**, added to `core/permissions.py` | "any installed plugin" |
| `plugin.<key>.read` / `plugin.<key>.write` | **dynamic**, one pair per installed plugin | that plugin only |

A request is allowed when the user holds the per-plugin key **or** the
blanket key for that action.

Why both: `BUILTIN_ROLES["admin"]` is `_all_except("billing.write")`, computed at
import time from the **static** `AVAILABLE_PERMISSIONS`. A purely dynamic scheme
would leave admin without any plugin key and lock admins out of their own
plugins — the exact failure `core/permissions.py` says must never happen. The
static pair lands in admin's builtin set for free; owner has `WILDCARD`.
Technicians get neither by default, which is the point.

### The choke point — and the hole the audit found in it

In `proxy_to_plugin_host`, before forwarding. Method → action: `GET`/`HEAD`/
`OPTIONS` → `read`, everything else → `write`.

**The gate as first designed was fake.** The plugin key was to be derived from
the first path segment while the upstream URL was built separately — and httpx
resolves dot segments per RFC 3986. Verified locally:

```text
chipricing/../midland/quote-lines
  -> http://plugin-host:8000/api/plugins/midland/quote-lines
chipricing/../../../internal/browser/credentials
  -> http://plugin-host:8000/internal/browser/credentials
```

The second one is plugin-host's internal saved-login store — owner-only and
consent-gated at the browser_proxy door, reachable here with nothing but a
login. **This predates the per-plugin work**: today's proxy forwards any path
after `get_current_user` with no validation at all.

So the path is validated *before* the URL is built, and both the plugin key and
the URL come from that one validated value. Empty, `.` and `..` segments are
**refused, not sanitized** — rewriting a traversal would send a request the
caller never made. An unidentifiable plugin key fails closed.

Reserved paths (`_browser/*`) keep their existing owner+consent gate
(`routers/browser_proxy.py`), which is registered ahead of this catch-all;
anything arriving here under that prefix bypassed that door and is refused
outright rather than graded against a plugin permission.

`GET /api/plugins` (the catalog list itself) stays readable by any authenticated
user: the nav needs it to know what exists, and it returns no plugin data.

### Catalog exposure

`GET /api/role-permissions/permissions/catalog` gains the dynamic entries
(category `plugins`) so `RolePermissionsView` renders checkboxes with no
frontend change. Sourced from the plugin-host catalog, best-effort with a short
timeout — if plugin-host is down the endpoint still returns the static keys.

### The trap this must not fall into

`RoleIn`/`RolePatch` validate `permissions` against `AVAILABLE_PERMISSIONS` and
**drop unknown keys**. If plugin keys are only known when plugin-host answers,
then saving any role while plugin-host is down would silently strip every
per-plugin grant the tenant had. So validation accepts the `plugin.<key>.<action>`
**shape** by pattern, independent of whether the catalog could be fetched.

### Nav + route

Plugin nav entries currently carry no `permission` field and bypass the
module-enablement filter, so a user who cannot use a plugin still sees it and
gets 403s. Entries get `permission: plugin.<key>.read`; the `/plugins/:key`
route gets a matching guard.

---

## Part 2 — the estimate editor on a phone

**A separate mobile editor was the wrong answer** (Doug: *"why cannot it not be
just like the desktop with a mobile layout"*). He is right, and the audit
independently found the same thing from the data side:

- `EstimateView` is already lazily loaded (`router/index.js`), so a phone
  downloads it only when opening an estimate — the bundle argument was empty.
- The actual breakage is one declaration: `.line-item-row` is a grid of nine
  fixed tracks (`64px 120px minmax(160px,1fr) 70px 110px 110px 80px 90px 36px`,
  ~840px). The existing mobile block already hid `.line-item-header` and then
  never restacked the row — half a fix, so the Total column sat off-screen.
- A reduced editor would have been a **second pricing path**. `EstimatePatchIn`
  has no `line_items` field at all: a PATCH of `line_items` returns 200 and
  writes nothing (the shape behind the $0.00 estimates on prod). Lines persist
  through `POST/PATCH/DELETE /{id}/lines/...`. And a line POSTed with
  `unit_price` but no `cost` takes the manual branch in `estimates.py`, leaving
  `cost_snapshot`/`margin_pct_snapshot` NULL — a line whose margin can never be
  measured and which is invisible to the margin report.

So: **one view, one layout change.** Below 768px each line becomes a
label/value card. Same margin engine, same cost/override handling, same tax,
same `estimate_source` "Add captured door" — which is the whole point, since a
door captured in the field has to land somewhere.

Field labels are `display: none` on desktop, which also removes them from the
grid (a `display:none` element is not a grid item), so the desktop row is
unchanged. `MobileEstimatesView` keeps the list/close-out job and gains an Edit
button into the editor.

This also dissolved the open question the design had flagged — how a mobile
line should get its price. Reusing the desktop view means there is nothing new
to decide.

---

## Verification

Done:

- backend permission matrix on the proxy — unpermitted user denied, per-plugin
  grant scoped to its own plugin, read not conferring write, blanket grant
  covering every plugin (the admin case), owner wildcard, catalog list still
  open, `_browser/*` refused
- traversal refused both as a unit (`_clean_subpath`) and end-to-end against the
  running app through a **raw ASGI scope** — a test client normalizes `../`
  before it leaves, so a TestClient-based test proves nothing here
- the guard was neutered on purpose to confirm those 12 tests go red
- validator: shape-based, so a `plugin.*` grant survives a role save taken while
  plugin-host is unreachable
- full backend suite 5703 passed / 0 failed (7 shards); frontend 1525 passed;
  ux gate clean; production build clean

Open:

- browser walk at a phone viewport on real data — layout is CSS-only and jsdom
  applies no media queries, so no unit test can stand in for it
