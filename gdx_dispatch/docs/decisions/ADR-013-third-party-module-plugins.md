# ADR-013 — Full-stack third-party module plugins

**Status:** Implemented 2026-06-24 (designed 2026-06-23 with Doug; reconstructed after a
context-cleared session, then refined through the model A/B/C comparison below). See
**Build status** at the end.

## Context

GDX modules today (`gdx_dispatch/modules/<key>/`) are **first-party and statically wired**.
A module becomes "real" at four build touchpoints:

1. **Registry** — a literal `MODULES = {…}` dict in `core/modules.py`.
2. **Router wiring** — a hand-written `app.include_router(...)` per module in `create_app()`.
3. **Schema** — one squashed Alembic baseline owns every table.
4. **Frontend** — a static `frontend/src/constants/modules.js` drives nav; the SPA is a
   compiled Vite bundle.

Per-tenant gating (`company_module_grants` + `require_module()`) is already dynamic and
accepts arbitrary keys — that layer needs no change.

We want **third-party** modules: full-stack (own backend + own screens), installable into
an operator's instance, without forking the app.

## Decisions

1. **No third-party JavaScript in the browser.** Plugin UI is **manifest-driven and
   server-rendered by the host**: a plugin declares its screens (lists, forms, detail
   pages, actions) as data; the host renders them with its own PrimeVue components.
2. **Distribution = pip packages**, discovered via Python entry-points. **Vetting is the
   operator's responsibility** — no signing, registry, or allowlist for us to build.
3. **Architecture = Model B: a single `plugin-host` container** runs *all* installed
   plugins, isolated from the core app (the VS Code "Extension Host" model). See below.
4. **In-app install is supported** — via a shared volume + a restart of *only* the
   plugin-host (the core web app keeps serving).

## Architecture: why Model B

Three models were considered:

| | A: in-process (pip in core) | **B: one plugin-host container** | C: container-per-plugin |
| --- | --- | --- | --- |
| Analog | WordPress, Discourse | **VS Code Extension Host** | Home Assistant add-ons |
| Core protected from plugin crash/leak/CPU | ❌ shares core process | ✅ separate process | ✅ |
| Plugin↔plugin isolation | ❌ | ❌ (share one container) | ✅ |
| Per-plugin overhead | none | **one container, flat** | N containers |
| Orchestration | none | **restart one container** | a Supervisor for N |
| In-app install | risky (exec in core) | ✅ clean | ✅ |
| Network hops core↔plugin | 0 | 1 | 1 + routing |

**B is chosen.** It keeps most of C's safety (core is protected; independent crash/reload
domain) at a fraction of the cost (one container, no per-plugin orchestration). Its one
weakness — plugins share a process, so a leak or a Python-dependency conflict between two
plugins affects all plugins (never core) — is acceptable given **operator-vetted plugins on
a single small box**, where plugin↔plugin isolation matters little and N-container overhead
matters a lot.

### How it works

```text
┌─────────────┐   /api/plugins/* (proxied, identity forwarded)   ┌──────────────┐
│  core app   │ ───────────────────────────────────────────────► │ plugin-host  │
│ (FastAPI)   │ ◄─── scoped API / DB role, manifests ──────────── │  (FastAPI)   │
└─────┬───────┘                                                   └──────┬───────┘
      │ migrates CORE schema (existing advisory-lock gate)               │ runs each enabled
      │                                                                  │ plugin's Alembic
      ▼                                                                  ▼ branch, then mounts
   Postgres  ◄──────────────────── shared DB ──────────────────────  plugin routers
                                                                  reads /plugins volume
```

- **`plugin-host` container** — a small FastAPI process that runs the entry-point
  `discover_plugins()` loop (below) and mounts every enabled plugin's `APIRouter`. The
  discovery code is identical to what ADR-013 first proposed for `create_app()`; it just
  runs *here* instead.
- **Proxy & auth** — the core app authenticates the user (JWT) as it does today, then
  proxies `/api/plugins/*` to `plugin-host` with an **internal token + forwarded principal**
  (user id, role, tenant, enabled modules). `plugin-host` is **internal-network only**,
  never exposed; it trusts the proxy's forwarded identity. Plugin routers still gate on
  `require_module(key)` using the forwarded context.
- **Trust boundary** — `plugin-host` gets a **scoped DB role / API token**, NOT the core
  app's full access. A rogue or buggy plugin's blast radius is the plugin-host, not core.
- **Migration split** — core app migrates core (unchanged single-migrator advisory lock).
  `plugin-host`, *after core is healthy*, runs each enabled plugin's Alembic branch against
  the shared DB under its **own** advisory lock, then mounts. Tables namespaced `plug_<key>_*`.
- **Frontend** — unchanged from Decision 1: `plugin-host` serves UI manifests; the host SPA
  renders them. No plugin JS in the browser.

### In-app install (the shared-volume flow)

Installed plugins live on a writable **`/plugins` volume** mounted into `plugin-host`. A
registry table is the operator's *desired* set; the volume is the materialized cache.

1. Owner opens **System Admin → Plugins → Install**, enters a package + version (owner-only,
   audited). → writes a `plugin_registry` row.
2. `plugin-host` **reconciles on (re)start**: `pip install --target=/plugins <pkg>==<ver>`
   for each desired row (the `/plugins` dir is on its `PYTHONPATH`), then discovers + mounts.
3. The core web app **never restarts** — only `plugin-host` blips; `/api/plugins/*` is
   briefly unavailable, the rest of the app keeps serving.
4. **Enable/disable** is just a `company_module_grants` row — no restart at all.

This gives the WordPress in-app experience with the blast radius confined to `plugin-host`,
and no runtime code-exec in the core process. (Offline/air-gapped installs need a reachable
package index — note for operators.)

### Plugin package shape (sketch)

```text
gdx-plugin-foo/
  pyproject.toml        # [project.entry-points."gdx.modules"] foo = "gdx_plugin_foo:manifest"
  gdx_plugin_foo/
    manifest.py         # key, name, tier, router, migrations path, requires="gdx>=X", ui screens
    router.py           # APIRouter; deps use the forwarded require_module("foo")
    models.py           # tables namespaced plug_foo_*, on the host-provided PluginBase
    ui.py               # declarative screens (lists/forms/actions) the host renders
    migrations/         # plugin-owned Alembic branch
```

## Importing plugins — the discovery path

The "import" is `entry_points().load()` — pip registers it, this finds it. It runs in
`plugin-host` in **two phases** (app mount + migration), so discovery lives in one shared
helper:

```python
# gdx_dispatch/plugin_api/discovery.py
from importlib.metadata import entry_points

def discover_plugins() -> list["LoadedPlugin"]:
    out = []
    for ep in entry_points(group="gdx.modules"):   # pip-registered → found here
        manifest = ep.load()                        # <-- THE import
        if not compatible(manifest.requires):       # "gdx>=X" gate
            log.warning("plugin %s skipped: requires %s", ep.name, manifest.requires)
            continue
        out.append(LoadedPlugin(key=ep.name, manifest=manifest))
    return out
```

**Mount phase** (`plugin-host` app startup):

```python
for p in discover_plugins():
    if not enabled(p.key):            # company_module_grants
        continue
    app.include_router(p.manifest.router)
    register_ui_manifest(p.key, p.manifest.ui)
```

**Migration phase** (`plugin-host` entrypoint, after core is healthy):

```python
from gdx_dispatch.plugin_api.discovery import discover_plugins
plugins = discover_plugins()
config.set_main_option("version_locations",
    " ".join(p.manifest.migrations_path for p in plugins))
for p in plugins:
    p.manifest.import_models()        # land tables on the shared PluginBase metadata
# then: alembic upgrade head, under plugin-host's own advisory lock
```

**Why plugins can't just "define models":** the codebase has multiple declarative bases and
*no global registry* (`core/pii.py:231`). So `gdx.plugin_api` must hand plugins a canonical
`PluginBase` to inherit, and the migration phase imports their models onto its metadata.

## The hard problems

**Gone** (via the decisions): no browser-side plugin code → no frontend trust plane, no
XSS-via-plugin, no Module Federation/version coupling. No signing/registry/allowlist. And
plugins no longer share the **core** process — a crash/leak is contained to `plugin-host`.

**Remain:**

1. **Plugin↔plugin fault sharing.** One process per host means a leak or dependency conflict
   between plugins affects all plugins (not core). Accepted for operator-vetted, small scale.
2. **A versioned plugin API contract.** Plugins bind to host internals (auth context, DB
   session, `require_module`, `PluginBase`) → that surface becomes public. Expose a thin,
   stable `gdx.plugin_api`; gate on `requires: gdx>=X` at load; refuse incompatible plugins
   loudly.
3. **Migration ownership.** Plugin tables must not collide (namespace prefix), must run after
   core, under their own lock. `update.sh`/entrypoint ordering: db → core app (migrates core)
   → core healthy → plugin-host (migrates plugins) → workers.
4. **The manifest UI vocabulary is a surface we own.** Its expressiveness caps what plugin UIs
   can be (list/table, detail, form + validation, action buttons → endpoints, nav placement).
   Start minimal, grow as real plugins need it. UI the manifest can't express is out of scope
   by design — the escape hatch is contributing the capability to the host, not shipping JS.
5. **Runtime install trust.** In-app install pulls + runs external code (in plugin-host).
   Owner-only, audited, scoped DB role; blast radius is plugin-host. Operator vets the package.

## Install ≠ enable

- **Install** — adds a plugin: in-app (registry row → plugin-host pip-installs to the
  `/plugins` volume → plugin-host restarts) OR host-side (bake into the plugin-host image).
- **Enable** — turns an installed plugin on: a `company_module_grants` row, owner-only,
  audited, **no restart**.

The in-app admin surface is a toggle over installed plugins plus an install form — not an
arbitrary uploader, and never executes plugin code inside the **core** app.

## Consequences

- New `plugin-host` service in the compose stack (+ selfhost overlay), `/plugins` volume,
  scoped DB role, internal-only network.
- Core app gains a `/api/plugins/*` proxy with identity forwarding.
- `update.sh`/entrypoint gain the core→plugins migration ordering.
- We own and version `gdx.plugin_api` (incl. `PluginBase`), the UI-manifest schema, and a
  `plugin_registry` table.
- Owner-only install/enable/disable flows + audit.

## Build status — DONE (2026-06-24)

All six design steps landed, plus deployment + app wiring:

1. ✅ `gdx.plugin_api` surface (`PluginBase`, manifest types, forwarded-identity context).
2. ✅ `plugin-host` container + `/api/plugins/*` proxy with identity forwarding (client `X-GDX-*`
   stripped, server-authoritative values injected).
3. ✅ UI-manifest schema v0 + host renderer (`PluginScreen.vue` / `usePluginScreen.js`).
4. ✅ `discover_plugins()` + mount/migration phases (`create_all`; per-plugin Alembic when a
   plugin needs schema *changes*).
5. ✅ `plugin_registry` table + in-app install/reconcile on the `/plugins` volume.
6. ✅ Reference plugin (`gdx-plugin-example`) — since extracted to the
   [gdx_dispatch_plugins](https://github.com/freePerro/gdx_dispatch_plugins) repo.

**Deployment (compose).** `plugin-host` is a service that reuses the app image with a different
command (`uvicorn gdx_dispatch.plugin_host.main:app`), shares the `*app-env` anchor, skips
migrations/bootstrap (the app owns the schema), and mounts a `gdx_plugins:/plugins` volume. It is
internal-only — no host port; the core app proxies to it over the compose network. The selfhost
overlay pulls the published image for it like the other services.

**App surface.** `/plugins/:key` route renders any installed plugin's manifest; the `/api/plugins`
catalog feeds a "Plugins" nav category; owner-only install UI lives at `/admin/plugins`
(`PluginsAdminView.vue`) over `/api/admin/plugins`.

**How "restart only the plugin-host" is implemented (no docker socket, no sidecar).** The install
UI records intent in `plugin_registry`; applying it needs a process restart so boot re-runs
`reconcile()` (pip-install) + discovery. Because plugin-host is a *separate* container from the
core app, it restarts its **own process**: `POST /internal/restart` (internal-only, not under
`/api/plugins`) schedules a self-`SIGTERM`; Docker's `restart: unless-stopped` recreates the
container; the core app keeps serving throughout. Owner-only `POST /api/admin/plugins/restart`
fronts it, and the UI polls `/api/plugins` until plugin-host answers again. (Contrast: *app*
self-update still needs the separate updater sidecar, because the app can't recreate itself
mid-request.)

**Verified (2026-06-24).** The register → `pip install --target /plugins` → `discover_plugins()`
(entry-point group `gdx.modules`) → mount → catalog/UI loop was run end-to-end in the app image as
`appuser` against the real `gdx-plugin-example`: `/plugins` is writable, the package installs, and
its manifest surfaces in `/api/plugins`, `/health`, and the UI endpoint. (Concern that `--target`
installs are invisible to `importlib.metadata` entry-points is **false** — `--target` writes
`.dist-info`, and `entry_points()` reads it once `/plugins` is on `sys.path`, which `reconcile()`
ensures before discovery.) `plugin-host` has a `/health` healthcheck so a wedge is visible.

**Not yet exercised on a live multi-container deploy:** the owner-clicks-install → registry row →
restart → reconcile pip-install → recreate cycle on a running compose stack (verified per-step, not
as one live sequence). **Known follow-up (when real plugins exist):** a bad plugin that imports
fine but fails `metadata.create_all` is unguarded (per-plugin *import* is already skipped in
`load_manifests`); add a failure-count / quarantine so it can't crash-loop. Not built now — no
third-party plugins exist yet, and the healthcheck surfaces a wedge.

## Addendum (2026-06-30) — richer declarative UI vocabulary

Decision #1 (no third-party JS; host renders the manifest with its own PrimeVue) is
**unchanged and reaffirmed**. Building the first real full-stack plugin showed the *manifest vocabulary* — not the model — was the
limit: create forms could only render text inputs, and the host only rendered one
`list` screen. Both were widened. Module Federation / plugin-shipped browser code
stays **rejected** — the widening is still pure declaration.

**Multiple list screens.** `usePluginScreen` now keeps rows **per** `list` screen
(keyed by endpoint) and routes each create form to *its* screen's create endpoint.
A plugin can declare several tables (e.g. a data list + an editable-settings list)
without them sharing rows or mis-posting. `rows` stays a back-compat computed
(first list) for existing single-list callers.

**Typed create fields.** A `create.fields[]` entry carries a `type`:
- `text` (default) → InputText, as before.
- `number` → InputNumber; honors `default`, `min`.
- `select` → PrimeVue Select. Options are static (`options: [{value,label}]`) or
  fetched from a plugin `options_endpoint`. `filter: true` gives client-side
  typeahead. `depends_on: ["otherField"]` makes the field **clear + refetch** its
  options when a field it depends on changes (e.g. a size list keyed on the chosen
  model — `"/api/plugins/<key>/sizes?model={model}"`).
- `autocomplete` — **reserved**, not implemented (a filterable `select` covers our
  ~600-option case; add server-side typeahead only when a list is too big to ship).

**Security invariant for manifest-named endpoints (important).** The host fetches a
plugin-declared endpoint (`options_endpoint`, and existing `endpoint`s) **with the
logged-in user's session**. So a manifest string is an instruction to the host's
authed client, and is constrained accordingly, all in `usePluginScreen`:
1. **Same-plugin only** — refuse any endpoint not prefixed `/api/plugins/<thisKey>/`.
   Blocks a manifest from steering the host's authed fetch at another plugin's
   namespace, a core route, or an absolute (off-site) URL. (This also hardens the
   pre-existing `screen.endpoint` fetch, which was previously unvalidated.)
2. **Encoded interpolation** — `{field}` bindings are `encodeURIComponent`'d before
   substitution (no query/path injection via a field value).
3. **Race-guarded** — a superseded dependent-options response is dropped, so fast
   model→model changes can't leave stale sizes shown.

The component (`PluginScreen.vue`) carries **no** security logic — it's all in the
composable, unit-tested (namespace rejection, encoding, race-drop).

**Explicitly deferred (YAGNI / avoid speculative generality):**
- **`compute_endpoint` / live totals** — a preview-as-you-type protocol (its own
  debounce/race/error surface). Not built; server round-trip per keystroke is the
  cost, and the dropdowns already remove the typo/invalid-size failure modes.
- **General component-tree SDUI** (a plugin composing arbitrary host components in
  JSON). Powerful but a real subsystem: a per-component prop allow-list (PrimeVue
  passthrough/`pt` and HTML-rendering props are injection sinks), a binding/event
  model, and a re-audit tax on every PrimeVue upgrade. **Do not build from one
  plugin** — let the shape emerge from ≥3 real plugins, then its own ADR.
- If a plugin ever genuinely needs bespoke interactive UI, the safe path is a
  **sandboxed iframe** webview (postMessage bridge, `frame-ancestors` CSP) — the
  ADR-014 streamed-browser is for *external* no-API sites, not plugin-authored UI —
  **never** Module Federation.

**Defense-in-depth note (pre-install scan).** A static scan of a plugin *package*
before install (banned imports, `eval`/`subprocess`/egress, unpinned deps,
obfuscation — reuse `bandit`/`pip-audit`/`semgrep`) is worth adding as a gate,
most valuable on the **backend** package where the plugin actually executes Python.
It is a **complement** to isolation, not a substitute: static analysis can't prove
absence of malice (runtime fetch-and-exec defeats it), so it never replaces the
no-browser-JS + scoped-DB-role + require_module model.

**Where:** `frontend/src/composables/usePluginScreen.js`,
`frontend/src/components/PluginScreen.vue`. First consumer: an internal estimator plugin (dependent dropdowns + a number field).
