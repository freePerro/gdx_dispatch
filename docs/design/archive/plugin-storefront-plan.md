# In-App Plugin Storefront (the WordPress.org directory, single-tenant edition)

Status: **RELEASED v1.70.2** — deployed to prod and demo, and owner-confirmed
(Doug installed the n8n plugin through it on prod 2026-08-19). PR 0 (#365,
audit trail), S1 (catalog pipeline) and S2 (#369 storefront) are all on main:
`admin_plugins.py:273 GET /storefront`, `:298 POST /storefront/install`,
`PluginsAdminView.vue:26` Browse tab, `:71` update-available badge.
**Not built:** S3's catalog signature with a pinned key — §5 calls the
detached signature "V2 / phase later", so this is a deferral to confirm
rather than a miss. `plugins.gdxdispatch.com` HTTPS still rides a temporary
override pending the Pages cert.

## Goal

An owner opens **Settings → Plugins → Browse**, sees a curated catalog of
GDX plugins (name, description, version, permission chips with the existing
risk copy), and clicks **Install**. The app downloads the wheel server-side,
verifies it, and hands it to the exact install machinery that exists today.
Update = the same click when the catalog carries a newer version.

### The user and the walk

The named user is **the owner, at a desk, on desktop** (plugin admin is
owner-only by design; this is not a tech-on-a-phone or customer surface,
though the page must not break on mobile). Their complete path, with no
dead ends:

Plugins admin (already in nav) → Browse tab → card → **Install** (sees the
permissions first) → card flips to *pending restart* with the existing
Restart button right there → restart → consent dialog (existing) → card
shows *Running vX* with an **Open** link to the plugin's screen (the
dynamic `/plugins/:key` route). Every step lands somewhere; the feature is
done only when that walk completes in a real browser as a real owner.

Why now: the plugin event platform shipped in **v1.66.0** (PR #343, merged
and released 2026-08-18) — third parties can build event-reacting plugins —
but today installing one means "obtain a wheel file, upload it, restart." A
storefront makes the ecosystem *legible* to a non-technical owner, and it's
the funnel surface for the Hostinger one-click story ("deploy GDX, then add
the n8n Automations plugin from the store"). It also answers an open
distribution question directly: `gdx-plugin-n8n` currently needs a PyPI
publish for the in-app install-by-name path to find it — a catalog wheel
URL makes PyPI unnecessary.

Per Doug (2026-08-17): **adoption-first, monetize later.** V1 catalog is
free plugins only. The legal groundwork for paid/proprietary plugins already
shipped (AGPLv3 + §7 plugin exception, #339, `PLUGIN-EXCEPTION.md`) — this
plan keeps a hook for them but builds no billing.

## What already exists (audit 2026-08-18 — all shipped)

The install mechanics, permission model, consent gate, and licensing
carve-out all exist. **The storefront is a fourth section on
`PluginsAdminView.vue` feeding the two existing POST endpoints.**

| Piece | Where |
| --- | --- |
| Install by index name → desired state | `POST /api/admin/plugins` → `plugin_registry` (`gdx_dispatch/routers/admin_plugins.py:128`) |
| Install by wheel upload → artifact | `POST /api/admin/plugins/upload` → `plugin_artifact` (bytes + sha256) (`admin_plugins.py:65-101`) |
| Materialization | plugin-host boot `reconcile()` → `pip install --target /plugins` (`gdx_dispatch/plugin_host/reconcile.py:357,377`); sha256 re-verified before install (`reconcile.py:329`) |
| Restart trigger | `POST /api/admin/plugins/restart` → host `/internal/restart` (`admin_plugins.py:214-234`) |
| Owner-only RBAC | `_OWNER_ROLES = {owner, superadmin}` on every route (`admin_plugins.py:45-51`); `admin` deliberately excluded |
| Consent gate (ADR-014) | per-permission owner grants, records exact list at grant time (`gdx_dispatch/core/plugin_consent.py:50,107`) |
| Permission vocabulary + risk copy | closed set `{browser, events, schedules, services}` with owner-facing copy (`gdx_dispatch/plugin_api/manifest.py:18-41`) |
| Stale-version fail-closed | on-disk ≠ desired → endpoints withheld (`reconcile.py:277-307`) — already handles the update swap window |
| Admin screen | `frontend/src/views/PluginsAdminView.vue` — already a 3-table reconciliation view (uploaded / desired / running) |
| Public plugin collection | `freePerro/gdx_dispatch_plugins` repo — independent wheel-buildable packages, manifest contract test migrated there |

What does **not** exist anywhere: a remote catalog/index, any code that
fetches a plugin list, signature/publisher verification (sha256 is
self-asserted integrity only), or static plugin metadata (the manifest is a
Python dataclass — you must *import*, i.e. run, a plugin to read it:
`plugin_api/manifest.py:44`). ADR-013:29 consciously deferred all of this
("vetting is the operator's responsibility").

## The trust model — the decision that shapes everything

**DECIDED (Doug, 2026-08-18): V1 is a curated catalog, and the
`gdx_dispatch_plugins` repository IS the curation authority** — a plugin is
in the store if and only if it is merged into that repo and released by its
CI. Only release CI writes the catalog.

This matters because ADR-013's Model B accepts **no plugin↔plugin
isolation** (all plugins share the plugin-host process), justified by
"operator-vetted plugins." A storefront shifts vetting from the operator to
*us* — which is compatible with Model B **only while every catalog entry is
first-party or reviewed by us**. Open third-party submissions are
explicitly out of scope and gated on plugin↔plugin isolation plus real
publisher signing (see Non-goals).

The two proprietary plugins (one never committed anywhere; chi-pricing —
git-ignored) are untouched: they keep using the manual upload path. The
store adds a path; it removes none.

## Architecture

### 1. Catalog publishing pipeline (gdx_dispatch_plugins repo, CI-owned)

- On tag, CI builds each plugin's wheel and attaches it to a GitHub
  Release.
- CI generates **`catalog.json`**: per plugin — `key`, `name`, `version`,
  `description`, `author`, `tier`, `permissions`, `requires` (core version
  constraint, e.g. `gdx>=1.66`), `wheel_url`, `sha256`, `license: free`,
  plus a top-level `schema_version`.
- **Static metadata extraction**: CI imports the manifest *in the CI
  sandbox* (safe there — that's where code may run) and snapshots it into
  the catalog entry, validated by the manifest-shape contract test already
  in the repo. Core never has to execute a plugin to display it.
- Hosting (decided): `plugins.gdxdispatch.com` — GitHub Pages on the
  plugins repo + CNAME — serves `catalog.json`; wheels remain GitHub
  Release assets. One env knob in core: `GDX_PLUGIN_CATALOG_URL`
  (default baked in).
- Catalog v1 contents: `gdx-plugin-example`, `gdx-plugin-hvac`,
  `gdx-plugin-n8n` (moved to the plugins repo, PR #1 merged 2026-08-18).
  `gdx-plugin-eventlog` stays in core as the dev reference (decided) and
  is not listed.

### 2. The dependency contract (resolves ADR-017 for store installs)

plugin-host has **no egress in production** (`reconcile.py:34-37`; the
2026-06-29 pip-hang outage). ADR-017 states the trilemma: live install /
no egress / deterministic boots — pick two. Its Option C (internal PyPI
mirror) is heavy. The store sidesteps the whole trilemma:

- **Store installs are wheel-only** (no sdist — no `setup.py` executing at
  install time), and
- **CI enforces `Requires-Dist ⊆ vendored-set`**: the allowlist of
  packages pre-vendored in the plugin-host image is generated from the
  image (pip freeze) and published as `vendored.json` next to the catalog,
  versioned with core releases.

This turns chi-pricing's "declare no deps because playwright is vendored"
trick from fragile folklore into a **checked, published contract** that
third-party authors can build against. `pip install` of such a wheel from
the local `/plugins` artifact needs zero network. ADR-017 Option C stays
deferred; nothing here blocks it later.

### 3. Core-side store service (new endpoints in `routers/admin_plugins.py`, owner-only like the rest)

- **`GET /api/admin/plugins/storefront`** — the *app* (which has egress;
  plugin-host does not) fetches `catalog.json` (httpx, short timeout,
  size-capped, schema-validated, ~15-min in-process cache — same pattern as
  the 60s live-catalog cache in `core/plugin_permissions.py:131-164`),
  then merges install state per entry: installed version (from
  `plugin_registry`/`plugin_artifact`), running version (live plugin-host
  catalog), `update_available`. Unknown catalog fields ignored; newer
  `schema_version` major → refuse with a clear "update GDX" message.
- **`POST /api/admin/plugins/storefront/install {key, version}`** — server
  resolves the wheel URL **from the verified catalog entry only** (never
  client-supplied — SSRF pinning: https only, host allowlist = catalog
  domain + GitHub release hosts), streams the download with the existing
  50 MB cap-plus-one-byte pattern (`admin_plugins.py:41,80-84`), verifies
  sha256 **against the catalog**, and inserts into `plugin_artifact`
  (`uploaded_by = "storefront:<user>"`). From that row onward it *is* the
  existing upload path: desired-state row, restart prompt, consent dialog —
  all unchanged.
- **Auditable (invariant #1)**: the install/update endpoint calls
  `log_audit_event()` with acting user, plugin key, version, and sha256 —
  who installed what, when, verifiable later. See the Auditability section:
  the *existing* plugin-admin mutations don't do this today and get fixed
  first.
- **No silent or fake success**: install success means "artifact recorded,
  **pending restart**" — the UI must say exactly that, never "Installed ✓"
  as if the code were live. *Running* is only claimed from plugin-host's
  live catalog (the existing "Running now" source). Zero fabricated states.
- **Zero DDL**: no new tables, no migration — the store writes the existing
  `plugin_artifact` + `plugin_registry` rows only. Existing rows untouched.
- **Reachability check (hard rule)**: every new endpoint sits behind
  `_require_owner`; nothing here is unauthenticated. The only public
  artifact is the catalog itself, which is public by design and contains no
  secrets or per-instance data.
- **Update** = same endpoint with the newer version; reconcile's
  stale-version fail-closed already covers the swap window (a stale
  pricing plugin must not quote money — that guarantee carries over).
- Egress note (known trap): before first prod use, confirm the app
  container can reach the catalog host — check the Cloudflare origin
  firewall / VPS ufw first when it can't.

### 4. Browse tab (`PluginsAdminView.vue`)

- Fourth section: **Browse**. Cards: name, author, tier badge,
  description, catalog version + install state ("Installed v0.3.1" /
  "Update to v0.4.0"), and **permission chips reusing the existing
  per-permission risk copy — shown *before* install** (DECIDED, Doug
  2026-08-18: required, not optional). A real safety upgrade over today —
  consent is use-time; today nothing shows permissions until after code is
  on disk.
- Install button → confirmation dialog that lists the permissions and
  keeps the honest line from `docs/plugin_file_install.md`: installing
  executes code. **Known sharp edge: this dialog must be a real modal with
  explicit buttons (PrimeVue Dialog), NOT `useDestructiveConfirm`** — that
  composable auto-accepts silently (issue #215), which here would turn
  Install into unconfirmed one-click code execution and skip the permission
  display Doug required. Then the normal restart + consent flow takes over.
- After install the card shows *pending restart* next to the existing
  Restart button; after restart it shows *Running vX* plus an **Open** link
  into the plugin's screen — no dead ends, and no state shown that the
  plugin-host hasn't confirmed.
- Catalog unreachable → the Browse tab alone shows "store unreachable";
  the other three sections are untouched (degrade, don't die — same
  philosophy as plugin-host boot).
- An "update available" count can badge the Plugins nav entry later
  (Sprint 3).

### 5. Catalog authenticity (phase now / phase later)

- **V1 chain**: HTTPS to a domain we control + per-wheel sha256 in the
  catalog + only release CI writes the catalog. Honest statement: this
  protects against tampered wheels and MITM, not against catalog-host
  compromise.
- **V2**: detached signature over `catalog.json` (minisign or sigstore),
  public key pinned in core; closes the catalog-host-compromise hole.
  Deliberately **not** per-publisher wheel signatures until open
  submissions exist — no publishers to authenticate yet.

### 6. Paid plugins (deferred hook — not built now)

- Catalog schema reserves `license: free|paid` and listing-only entries
  (no `wheel_url`, a "contact" CTA). V1 renders free entries only.
- When monetization happens: gated download URLs or runtime license keys —
  either fits the same catalog + artifact path. Billing is out of scope
  here. The AGPL §7 exception already permits proprietary plugins.

## Auditability (invariant #1) — including a pre-existing gap found 2026-08-18

Every state-changing storefront action must answer *who did it, what
changed, when*: install, update, and (existing) restart + consent.

**Found during this plan's CLAUDE.md review pass:**
`routers/admin_plugins.py` contains **zero `log_audit_event()` calls**
while its own docstrings (lines 6, 73) claim "Owner-only + audited" —
verified by grep 2026-08-18. So today, a plugin install/upload/restart/
consent-grant leaves **no audit trail**, on the surface where installing
is literally code execution. That's both an invariant-#1 violation and
comment drift. Per the packaging default this is its own small, focused
fix — **PR 0 below** — not bundled into the storefront feature PR. The
storefront's new endpoints then follow the same (now real) convention.

## Non-goals (explicit)

- **Open third-party submissions.** Breaks ADR-013 Model B's shared-process
  assumption; needs plugin↔plugin isolation, publisher signing, and a
  review workflow. Traction-gated, like Gap 3 in the n8n plan.
- **Unattended auto-update.** Updates are owner-clicked. (The single-tenant
  stance means there's no fleet operator to consent on the owner's behalf.)
- **sdist in the store.** Wheel-only. Manual sdist upload stays available
  where operator judgment applies.
- **Ratings/reviews/telemetry**, and **ADR-017's internal mirror**.

## Sprints

0. **PR 0 — audit-trail repair** (small, standalone, ships first): add
   `log_audit_event()` to every existing mutation in
   `routers/admin_plugins.py` (register, upload, delete-if-any, restart,
   consent grant/revoke) and fix the drifted "audited" docstrings. Sibling
   sweep in the same PR: grep every router for mutation endpoints missing
   audit calls on the plugin/webhook surfaces; report scope + result.
1. **S1 — pipeline** (plugins repo): CI builds wheels on tag, runs the
   manifest contract test, extracts static metadata, generates
   `vendored.json` + `catalog.json`, publishes. Pick + wire the catalog
   host (`plugins.gdxdispatch.com`: DNS record + Pages). First catalog:
   example, hvac, n8n.
   Also fix the plugins-repo README's `docs/plugin_file_install.md`
   pointer to the full in-repo path.
2. **S2 — core**: the two storefront endpoints + tests (schema validation,
   SSRF pinning, sha256, size cap, state merge, `requires` filtering,
   audit events), Browse tab, install→restart→consent E2E.
3. **S3 — hardening + polish**: catalog signature with pinned key,
   update-available badge, developer-guide chapter ("how to get listed":
   wheel-only, vendored-set rule, manifest contract test).

Each sprint runs the working-agreement pipeline: adversarial audit of the
plan before code; full test matrix with every FAIL/SKIP enumerated by name
plus the lint ratchet checked against baseline; verification on a
throwaway container in a real headed browser **as a real owner account,
light and dark mode, desktop and mobile**; sibling sweep for the bug class
touched; and after deploy, the walk on prod is the finish line — the full
Browse→Install→restart→consent→Open path with a real plugin, evidence
attached.

## Decided (Doug, 2026-08-18) — no product questions remain open

- **Curation source**: the catalog comes from the `gdx_dispatch_plugins`
  repository — merged + CI-released there ⇔ listed in the store.
- **Permissions before install**: yes — the store must show the
  permissions a plugin will ask for before the owner installs it.
- **Catalog serving URL**: `plugins.gdxdispatch.com` (GitHub Pages on the
  plugins repo + CNAME); the app's baked-in default, overridable via
  `GDX_PLUGIN_CATALOG_URL`.
- **Eventlog stays dev-only in core** as the reference example — it is not
  listed in the store. Catalog v1 = example, hvac, n8n.
- **Tier badges are cosmetic in v1** — displayed, gating nothing (the app
  has no instance-tier concept to enforce against yet).
- **V1 catalog is free-only** — no paid or "contact" listings until
  monetization is actually designed; the schema's `license` field stays
  reserved.

Remaining open items are implementation-side, not decisions: create the
DNS record for `plugins.gdxdispatch.com`, enable Pages on the plugins
repo, and the sprint work itself.
