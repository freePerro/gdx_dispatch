# Custom Catalogs

Catalogs hold the products and services you sell. Every catalog has a **type**
(`product_class`) that decides which fields its items carry. Built-in types ship
with the app; **custom** types let you define your own fields with no code,
migration, or deploy — so a business in any industry (HVAC, electrical,
plumbing, …) can model its own catalog.

All three slices of [ADR-015](decisions/ADR-015-custom-catalogs-and-catalog-packs.md)
are implemented: no-code custom types (Slice 1), pluggable **pricing strategies**
(Slice 2), and installable **Catalog Packs** (Slice 3).

## Catalog types

| Type | Fields | Pricing |
| --- | --- | --- |
| **Parts** | SKU, name, description, cost, retail, category | Margin engine |
| **Doors** | Parts fields + full install spec (size, R-value, panel style, …) | Margin engine; unions with the CHI manufacturer feed |
| **Custom…** | Parts fields + **your own fields** | Margin engine |

Every item, regardless of type, always has the **spine** fields: SKU, name,
description, cost, retail price. A custom type adds fields on top of the spine.

## Creating a custom catalog (no code)

1. Go to **Catalogs → + New Catalog**.
2. Set **Catalog Type** to **Custom…**.
3. Give the catalog a **name** (e.g. "HVAC Units").
4. Under **Fields**, click **+ Add Field** for each attribute you want to track:
   - **Label** — what techs/office see (e.g. "Tonnage").
   - **Type** — Text, Long text, Number, Currency, Yes/No, Dropdown, or Date.
   - **Dropdown** also takes a comma-separated **Options** list.
   - **Required** — tick if the field must be filled in.
5. Click **Create**. The catalog now renders a form and table built from your
   fields — add items exactly like Parts/Doors.

You can define up to **50 fields** per custom catalog.

## Field types

| Type | Stored as | Item form input |
| --- | --- | --- |
| Text | string | single-line text |
| Long text | string | multi-line text |
| Number | number | numeric input |
| Currency | number | currency input |
| Yes/No | boolean | checkbox |
| Dropdown | string | select (from your options) |
| Date | string (`YYYY-MM-DD`) | date picker |

## How it's stored (for developers)

Schema-as-data, not a SQL table per type (see ADR-015 for why):

- **`custom_catalogs.field_schema`** (JSON) — the ordered field definitions for a
  `product_class='custom'` catalog. Each entry:
  `{name, label, type, section, required, options?}`. `name` is a safe slug
  (`^[a-z][a-z0-9_]{0,39}$`), unique within the catalog.
- **`custom_catalog_items.attributes`** (JSON) — the per-item values, keyed by
  field `name`. Coerced to the field's type on write; unknown keys are dropped.

Built-in types (`parts`, `door`) leave `field_schema` empty and use the frontend
registry / `door_specs` table as before. The door type keeps its typed table
because it must union with the CHI manufacturer feed — that is the one case a
typed table earns its keep.

### API

`POST /api/catalogs`
```json
{
  "name": "HVAC Units",
  "product_class": "custom",
  "field_schema": [
    { "label": "Tonnage", "name": "tonnage", "type": "number" },
    { "label": "Refrigerant", "name": "refrigerant", "type": "select",
      "options": ["R-410A", "R-32"] },
    { "label": "Energy Star", "name": "energy_star", "type": "checkbox" }
  ]
}
```
Invalid schemas are rejected with 422 (missing fields, unknown type, duplicate or
unsafe name, `select` without options).

`POST /api/catalogs/{id}/items`
```json
{ "sku": "AC-3T", "name": "3-Ton AC", "cost": 1200, "price": 2400,
  "attributes": { "tonnage": 3, "refrigerant": "R-410A", "energy_star": true } }
```
`PATCH` of an item's `attributes` merges into the existing values (send only the
fields you change). Both `GET` of a catalog and its items echo `field_schema` /
`attributes` so the UI can render dynamically.

### Validation rules

- A custom catalog needs **1–50** fields.
- Field `name`: starts with a lowercase letter; lowercase letters, digits,
  underscores only; ≤ 40 chars; unique per catalog. (The UI auto-derives it from
  the label.)
- Field `type` must be one of the seven supported types.
- `select` fields need at least one option.
- Item values are coerced (number/currency → float, Yes/No → bool, else string);
  values that can't be coerced or that aren't in the schema are silently dropped,
  so older clients keep working.

## Tests

- Backend: `gdx_dispatch/tests/test_catalog_custom_fields.py`
- Frontend: `gdx_dispatch/frontend/src/catalog/__tests__/types.spec.js`

## Pricing strategies (Slice 2)

Every catalog has a **pricing strategy** that turns an item's cost into its retail
price **when you save an item with the price left blank**. If you type a price, it
always wins. Pick the strategy in the New Catalog dialog.

Built-in strategies:

| Strategy | Retail from cost |
| --- | --- |
| **Manual** (default) | keep the price you enter (pre-ADR-015 behavior) |
| **Margin 50%** | cost ÷ (1 − 0.5) = 2× cost |
| **Markup 50%** | cost × 1.5 |
| **Keystone** | cost × 2 |

`GET /api/catalogs/pricing-strategies` lists them (plus any a Catalog Pack adds).
A catalog stores its choice in `custom_catalogs.pricing_strategy`; pack-contributed
strategies also store a declarative `{kind, params}` spec in
`custom_catalogs.pricing_config` so pricing is self-contained.

Strategies are **declarative** — a `{kind, params}` spec, not code. Kinds:
`manual`, `multiplier` (`{factor}`), `markup` (`{pct}`), `margin` (`{pct}`). This
is what lets a Catalog Pack contribute pricing without shipping code into the core
process (see below).

## Catalog Packs (Slice 3)

A **Catalog Pack** is an installable plugin ([ADR-013](decisions/ADR-013-third-party-module-plugins.md))
that contributes one or more catalog types — fields *and* pricing — as **data**, so
a whole industry vertical is shareable across companies. The reference pack
(`plugins/gdx-plugin-hvac/`) adds an "HVAC Units" type with a 40%-markup strategy.

How it works:

1. A pack declares `catalog_types` + `pricing_strategies` in its `PluginManifest`
   (no router, no models needed — a pack can be pure data).
2. The plugin-host serves them at `GET /api/plugins`; the core aggregates them at
   `GET /api/catalogs/pack-types`.
3. The New Catalog dialog lists each pack type with a **"(Pack)"** suffix.
4. Creating one **copies** the pack's `field_schema` + pricing onto the new catalog,
   so the catalog is self-contained: pricing runs in the core process from the
   copied declarative spec, and nothing from the pack package executes in core at
   pricing time (preserving ADR-013's process isolation).

A pack's `catalog_type` entry:

```python
{
  "key": "hvac_unit",
  "label": "HVAC Units",
  "field_schema": [ {"name": "tonnage", "label": "Tonnage", "type": "number"}, ... ],
  "pricing_strategy": {"id": "hvac_markup_40", "label": "HVAC Markup 40%",
                       "kind": "markup", "params": {"pct": 0.4}},
}
```

`importers` are reserved on the manifest for a later step (data-source importers a
pack offers); the field is surfaced but not yet wired.

## Tests (Slices 2–3)

- Backend: `tests/test_catalog_pricing_strategies.py`, `tests/test_catalog_packs.py`
- Frontend: `frontend/src/catalog/__tests__/types.spec.js` (pack-payload helper)
