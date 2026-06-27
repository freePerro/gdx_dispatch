# ADR-015 — Custom catalogs & Catalog Packs (pluggable types + pricing)

**Status:** All slices implemented 2026-06-25/26 (designed with Doug). Builds on ADR-013
(third-party module plugins). See **Build status** at the end.

## Context

The catalog today is a **typed, hardcoded** system (Class Table Inheritance):

1. `custom_catalogs.product_class` is a discriminator; `custom_catalog_items` is a shared
   spine (sku, name, description, cost, price, category).
2. Each *typed* class gets its own SQL spec table. Only `DoorSpec`
   (`models/tenant_models.py`) exists. `door` items 1:1 a `door_specs` row.
3. The backend enum `PRODUCT_CLASSES = {parts, door, opener, spring, track, remote, labor}`
   (`routers/catalog.py`) lists seven, but only **`parts`** (spine-only) and **`door`** (with a
   spec table) are wired end-to-end. The other five have no spec table and no frontend entry.
4. Adding a class costs **three hand-edited files** (model + router + `frontend/src/catalog/types.js`)
   **plus a DB migration plus a deploy**. `tools/scaffold_product_class.py` prints that boilerplate
   but mutates nothing.

Two consequences drove this ADR:

- **A user noticed only "Doors" and "Parts" appear** when creating a catalog — because the
  frontend registry (`catalog/types.js`) only defines those two, even though the backend enum
  lists seven. The dropdown is built from that registry.
- **We want other companies, in other industries, to use this product.** An HVAC, electrical, or
  plumbing company cannot model their catalog without us writing code + shipping a release. That
  is a platform limitation, not a configuration gap.

The frontend is **already data-driven**: `CatalogView.vue` renders every form field and table
column from a field schema via `readField`/`writeField` (dotted paths, incl. `spec.*`). The only
thing hardcoded is *where the schema comes from* (a static JS object) and the *typed SQL spec
tables* on the backend.

Doug's framing, which this ADR adopts: **catalog pricing should be a plugin**, and we should beat
the industry rather than copy it.

## Two separate questions (they have different answers)

**Q1 — How do we store arbitrary per-type fields?** This is plumbing. The known patterns and
their measured trade-offs:

| Pattern | Cost for *our* shape |
| --- | --- |
| **Separate SQL table per type** (what we do now: `DoorSpec`) | Migration + deploy per type; "core tables constantly modified." Highest maintenance. |
| **EAV** (defs + values tables) | Multi-field filters become self-joins; "querying becomes a nightmare." Jira caps ~1,200 fields for this reason. |
| **JSONB column + field schema as data** | One column, no migration to add a type, no joins to read an item. Range-query indexing and DB-level type checks are the only real losses. |
| **Salesforce flex columns** (~500 generic `ValueN` + metadata dictionary) | "Hundreds of engineers over 20+ years." Reinventing a database. |
| **Schema-per-tenant** | Solves isolation/scale we don't have (we are single-tenant per ADR / single-tenant decision). |

For **one company** with a few thousand catalog items, JSONB's downsides do not bite: there is no
hot cross-field search path, and validation already lives in app code. JSONB is chosen as the
engine — not because it is common, but because the alternatives are objectively worse here. Being
contrarian on storage buys nothing.

**Q2 — How does another company get a catalog type that fits their industry?** This is the
product question, and where the industry is weak. Salesforce/HubSpot/Jira let a user rename fields
*inside their walled garden* — that makes one app configurable. None let you **package a whole
vertical** (its fields *and* its pricing brain) and hand it to another company. That is the
differentiator, and it maps directly onto the **plugin-host we already built in ADR-013**.

## Decisions

1. **Field storage is JSONB, schema-as-data.** Add `custom_catalogs.field_schema` (JSON: ordered
   list of field defs — `{name, label, type, section, required, options?}`) and
   `custom_catalog_items.attributes` (JSON: values). No new per-type SQL tables. No EAV.

2. **Three tiers, one storage engine.**
   - **Built-in** — `parts`, ships with the app (spine-only).
   - **No-code custom** — operator opens *New Catalog → Custom…* and defines fields in the UI.
     Covers most industries instantly, zero deploy. Default margin pricing.
   - **Catalog Pack** — an installable plugin (ADR-013) that contributes a type. Distributable
     across companies. This is the platform tier.

3. **Pricing is a pluggable strategy, resolved by id.** The pricing engine looks up a strategy for
   the catalog; **default = the existing margin/markup table** (`DEFAULT_PRICING_SETTINGS`). A pack
   may register a new strategy (e.g. duration-rounded labor, feed-based door pricing). Pricing
   logic — not just fields — is what makes a pack valuable; a type without pricing is a spreadsheet.

4. **A Catalog Pack is an ADR-013 plugin** whose manifest contributes
   `{type schema + pricing strategy + optional importer/data source}`. No new distribution
   channel: packs are pip packages discovered by `plugin-host`, installed in-app on the `/plugins`
   volume, with manifest-driven UI (no browser JS).

5. **`door` / `DoorSpec` becomes the reference Catalog Pack, not architecture.** Its typed
   `door_specs` table stays *only* as that pack's private storage — justified because custom doors
   must union with the CHI manufacturer feed (`chi_door_catalog`) on identical columns for the
   estimate read-path. That is a pack implementation detail, not the general mechanism. Everything
   else uses JSONB.

## Why this is better than the industry, not just different

- Industry framing: **custom fields** → one company's app gets slightly configurable.
- Our framing: **custom catalog types as installable, shareable packs** → the garage-door catalog
  we already perfected becomes a product another door company installs; an HVAC company installs an
  HVAC pack. The VS Code extension-marketplace model (already our plugin design) applied to
  verticals.
- Pricing-as-plugin is the load-bearing wall, not a bolt-on: the pack carries the pricing brain, so
  a vertical is genuinely turnkey, not just renamed columns.
- It reuses ADR-013's plugin-host, proxy, install flow, and manifest UI — no new infrastructure.

## Shapes (sketch)

**Catalog field schema** (stored on `custom_catalogs.field_schema`; also what a pack declares):

```json
[
  { "name": "tonnage",      "label": "Tonnage",   "type": "number",   "section": "spec" },
  { "name": "seer_rating",  "label": "SEER",      "type": "number",   "section": "spec" },
  { "name": "refrigerant",  "label": "Refrigerant","type": "select",  "section": "spec",
    "options": ["R-410A", "R-32"] }
]
```

The spine fields (sku/name/description/cost/price/category) are always present; the schema only
describes the *extra* attributes, which the API stores under `attributes`. The frontend builds
`formFields`/`tableColumns` from this exactly as `catalog/types.js` does today — the registry just
moves from a static JS object to API-provided data.

**Pricing strategy contract** (host-owned, in `gdx.plugin_api`):

```python
class PricingStrategy(Protocol):
    id: str
    def price(self, *, cost: Decimal, attributes: dict, settings: dict) -> Decimal: ...
```

Default registered strategy = `"margin"` (the current tiered-margin computation). A pack registers
additional strategies by id; a catalog records which strategy id it uses.

**Catalog Pack manifest** (extends the ADR-013 plugin manifest):

```python
manifest = PluginManifest(
    key="hvac",
    catalog_types=[CatalogType(key="hvac_unit", label="HVAC Units",
                               field_schema=[...], pricing="hvac_tonnage")],
    pricing_strategies=[HvacTonnagePricing()],
    importers=[...],   # optional manufacturer feed / CSV
)
```

## The hard problems

**Gone** (vs. the typed-table design): no migration/deploy to add a type; no three-file edit; no
five dormant half-wired classes; the scaffold tool is obsoleted for the common case.

**Remain:**

1. **No DB-level type safety on `attributes`.** Validation is app-layer, driven by the field
   schema (type coercion, required, option/min/max). Acceptable and standard; the schema is the
   contract.
2. **Schema evolution.** Renaming/removing a field, or tightening optional→required, must not
   break existing items when *viewed* (only when edited). The validation engine reconciles against
   the current schema at write time.
3. **The pricing-strategy surface is a versioned contract.** Like the ADR-013 plugin API, packs
   bind to it; keep it thin and gate on `requires: gdx>=X`.
4. **Search/reporting on attributes.** JSONB GIN covers equality; range/sort on a numeric attribute
   needs a functional index, added selectively only for fields we actually filter on. Not needed
   for v1 (catalog browse is by catalog, not cross-attribute search).
5. **Door pack migration.** `door`/`DoorSpec`/CHI-union must keep working unchanged while it is
   re-homed conceptually as the reference pack. v1 leaves the code in place and *describes* it as a
   pack; extraction into an actual installable package is a later step.

## Migration plan (slices)

1. **Slice 1 — no-code custom types.** Add `field_schema` + `attributes` JSON columns (migration
   with `to_regclass` guard per the migration-baseline rule). Serve schema from the API; frontend
   reads it instead of the hardcoded registry. *New Catalog → Custom…* with an in-UI field builder.
   → app becomes multi-industry, no deploy per type. `parts` and `door` keep working as built-ins.
2. **Slice 2 — pricing-strategy registry.** Pricing engine resolves a strategy id; default =
   margin table. Catalogs record their strategy. → pricing is pluggable.
3. **Slice 3 — Catalog Pack contract.** Extend the ADR-013 manifest with `catalog_types` +
   `pricing_strategies` + `importers`; `plugin-host` registers them on load. Refactor door/CHI into
   the first reference pack as proof. → platform tier.

Slice 1 alone delivers the user-visible goal. Slices 2–3 deliver the platform.

## Consequences

- Two new JSON columns (`custom_catalogs.field_schema`, `custom_catalog_items.attributes`) + a
  guarded migration.
- Catalog create/serialize/patch in `routers/catalog.py` gain a generic attributes path alongside
  the existing door-spec path (which stays).
- `catalog/types.js` shrinks to the spine + a fallback; the type registry becomes API/pack-driven.
- New host-owned `PricingStrategy` contract in `gdx.plugin_api`; the ADR-013 manifest grows
  `catalog_types`/`pricing_strategies`/`importers`.
- The seven-entry backend enum is retired in favour of data-defined types; the five dormant classes
  are not built as typed tables.

## Build status

**Slice 1 — no-code custom types — DONE (2026-06-25).**

- Model: `custom_catalogs.field_schema` (JSON) + `custom_catalog_items.attributes` (JSON)
  (`models/tenant_models.py`).
- Migration `005_custom_catalog_fields` — adds both columns, `to_regclass`-guarded (the tables
  are built by `create_all`, not the squashed baseline; see migration 003).
- Router (`routers/catalog.py`): `'custom'` product class; `_validate_field_schema` /
  `_coerce_attributes`; create/serialize/patch/add carry `field_schema` + `attributes`. Unknown
  attribute keys are dropped, not 400'd.
- Frontend: `catalog/types.js` gains `buildCustomClass` / `getCatalogClass` /
  `emptyItemForCatalog` (registry becomes data-driven for custom catalogs);
  `CatalogView.vue` gains the "Custom…" type, an in-dialog field builder, and select/checkbox/date
  item inputs.
- Tests: `tests/test_catalog_custom_fields.py` (backend), `frontend/src/catalog/__tests__/types.spec.js`.
- User docs: `docs/CUSTOM_CATALOGS.md` + a Catalogs section in `docs/USER_GUIDE.md`.

**Verified (2026-06-25):** migration 005 applied to the running local Postgres (columns present,
alembic head = 005); router round-trip smoke-tested against that DB (create custom catalog → add
item with coerced attributes → patch-merge → list); 87 catalog backend tests + 712 frontend tests
green; frontend production build clean.

**Slice 2 — pluggable pricing strategies — DONE (2026-06-26).**

- `core/pricing_strategies.py` — declarative registry (`manual`/`multiplier`/`markup`/`margin`);
  built-ins manual (default, passthrough), margin_50, markup_50, keystone; `register_pack_strategy`
  for pack-contributed strategies.
- Model: `custom_catalogs.pricing_strategy` + `pricing_config` (JSON); migration
  `006_catalog_pricing_strategy` (to_regclass-guarded).
- Router: applied in `add_catalog_item` when price is blank; `GET /api/catalogs/pricing-strategies`.
- Frontend: Pricing dropdown in the New Catalog dialog.
- Tests: `tests/test_catalog_pricing_strategies.py`.

**Slice 3 — Catalog Packs — DONE (2026-06-26).**

- `PluginManifest` gains `catalog_types` + `pricing_strategies` + `importers` (validated, stdlib-only).
- plugin-host `/api/plugins` serves them; core `GET /api/catalogs/pack-types` aggregates + registers
  declarative pack strategies.
- Creating a catalog from a pack type **copies** its `field_schema` + pricing onto the catalog →
  self-contained, no pack code in core at pricing time (ADR-013 isolation preserved).
- Reference pack `gdx-plugin-hvac` (data-only: HVAC type + 40%-markup strategy); lives in the
  [gdx_dispatch_plugins](https://github.com/freePerro/gdx_dispatch_plugins) repo.
- Frontend: pack types listed as "… (Pack)"; create expands the template.
- Tests: `tests/test_catalog_packs.py` + `frontend/src/catalog/__tests__/types.spec.js`.

**Verified (2026-06-26) — browser, throwaway containers, real DB.** Stood up a throwaway app +
plugin-host (HVAC pack installed via offline dist-info) against the live local Postgres; migrations
005+006 applied (head=006). Drove the UI with Playwright: created a **Keystone** catalog → item
cost $100, blank price → retail **$200** ✅; created a catalog from the **HVAC Units (Pack)** type →
item cost $1000 with Tonnage/Refrigerant/Energy-Star → retail **$1400** (pack's 40% markup, computed
in core) ✅. Backend: 41 slice-2/3 tests + full catalog suite green. Demo data + throwaway containers
cleaned up afterward.

**Note on cross-process pricing (refinement to the original sketch):** pack pricing is **declarative
data copied onto the catalog at creation**, not a code callable proxied to plugin-host. This keeps
catalog pricing fully in-core and self-contained (survives plugin-host downtime/restart) while
honoring ADR-013's "no plugin code in the core process". Code-based pack strategies (if ever needed)
remain a future extension via the plugin-host proxy.

## Next steps

- `importers` wiring (data-source importers a pack offers) — manifest field exists, not yet used.
- Optional: extract the existing first-party door/CHI integration into a Catalog Pack now that the
  contract exists (today door stays a built-in typed class; the HVAC pack is the reference).
