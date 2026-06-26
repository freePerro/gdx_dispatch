from __future__ import annotations

import csv
import logging
import re
import httpx
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal
from io import StringIO
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session, selectinload

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core import pricing_strategies
from gdx_dispatch.models.pricing_engine import PricingTierSet
from gdx_dispatch.models.tenant_models import CustomCatalog, CustomCatalogItem, DoorSpec
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)


def _audit_ids(user: dict, request: Request | None) -> tuple[str, str]:
    tenant_id = ""
    if request is not None:
        tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
        tenant_id = str(tenant.get("id") or "")
    user_id = str(user.get("sub") or user.get("user_id") or user.get("id") or "system")
    return tenant_id, user_id

router = APIRouter(tags=["catalog"], dependencies=[Depends(require_module("inventory"))])

# Back-compat exports for existing unit tests that import pricing objects from this module.
DEFAULT_PRICING_SETTINGS: dict[str, object] = {
    "margins": {
        "standard": {
            "retail": 0.50,
            "contractor": 0.35,
            "wholesale": 0.25,
        },
        "premium": {
            "retail": 0.60,
            "contractor": 0.45,
            "wholesale": 0.30,
        },
    },
    "tiers": [
        {"name": "retail", "min_cost": 0, "max_cost": 4999, "default_margin_type": "standard"},
        {"name": "commercial", "min_cost": 5000, "max_cost": 9999999, "default_margin_type": "premium"},
    ],
    "volume_discounts": [
        {"min_items": 5, "discount_pct": 2},
        {"min_items": 10, "discount_pct": 4},
        {"min_items": 20, "discount_pct": 7},
    ],
}
_PRICING_SETTINGS: dict[str, object] = deepcopy(DEFAULT_PRICING_SETTINGS)


ALLOWED_SOURCES = {"manual", "chi", "qb"}

# Sprint typed-catalogs — Class Table Inheritance product classes.
# 'parts' is the legacy default; 'door' has a DoorSpec table; the rest are
# placeholders for the scaffolder to fill in (opener_specs, spring_specs, …).
# ADR-015 — 'custom' is the no-code escape hatch: the catalog carries its own
# `field_schema` and items store values in `attributes` (JSON), so any industry
# can define a catalog type with no migration or deploy.
PRODUCT_CLASSES = {"parts", "door", "opener", "spring", "track", "remote", "labor", "custom"}
PRODUCT_CLASSES_WITH_SPEC = {"door"}

# ADR-015 — field types a custom catalog may declare. Maps 1:1 to a frontend
# input component; keep in sync with frontend/src/catalog/types.js FIELD_INPUTS.
CUSTOM_FIELD_TYPES = {"text", "textarea", "number", "currency", "checkbox", "select", "date"}
# Field names must be safe JSON keys / dotted-path segments (no prototype walk).
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

# Door spec field whitelist — keep in sync with DoorSpec model. The scaffolder
# regenerates this list when new spec tables are added.
DOOR_SPEC_FIELDS = (
    "manufacturer", "model_number", "door_type", "sales_talking_point",
    "width", "height", "color", "insulation_type", "r_value", "panel_style",
    "section_construction", "section_thickness_in", "section_sides",
    "section_material", "window_option", "window_rows", "window_type",
    "finish_type", "high_lift", "high_lift_in", "web_source_url",
)

# Virtual catalog sentinels — surface manufacturer feed tables (chi_door_catalog,
# chi_parts_catalog) as pickable read-only catalogs in the Catalogs page so a
# tenant can browse what they already have instead of only seeing their custom
# catalogs. Sentinel UUIDs are deterministic + namespaced so client-side
# routing recognizes them; they never collide with real custom_catalogs UUIDs
# (those are uuid4-random, with non-zero high bits).
VIRTUAL_CHI_DOORS_ID = "00000000-0000-0000-0000-0000c1d00000"
VIRTUAL_CHI_PARTS_ID = "00000000-0000-0000-0000-0000c1ba0000"
VIRTUAL_CATALOG_IDS = {VIRTUAL_CHI_DOORS_ID, VIRTUAL_CHI_PARTS_ID}


def _virtual_catalog_payload(virtual_id: str, count: int) -> dict[str, object]:
    """Synthesize a CatalogGroup-shaped dict for a CHI feed table."""
    if virtual_id == VIRTUAL_CHI_DOORS_ID:
        return {
            "id": virtual_id,
            "name": "CHI Doors",
            "source": "chi",
            "source_system": "chi",
            "product_class": "door",
            "read_only": True,
            "item_count": count,
            "created_at": None,
            "updated_at": None,
        }
    return {
        "id": virtual_id,
        "name": "CHI Parts",
        "source": "chi",
        "source_system": "chi",
        "product_class": "parts",
        "read_only": True,
        "item_count": count,
        "created_at": None,
        "updated_at": None,
    }


def _list_virtual_catalogs(db: Session) -> list[dict[str, object]]:
    """Return synthetic CHI catalog entries when the underlying tables exist
    and have rows. Uses raw SQL to avoid ORM-relationship requirements and
    silently no-ops if the table isn't present in this tenant."""
    out: list[dict[str, object]] = []
    for virtual_id, table in (
        (VIRTUAL_CHI_DOORS_ID, "chi_door_catalog"),
        (VIRTUAL_CHI_PARTS_ID, "chi_parts_catalog"),
    ):
        try:
            count = db.execute(
                text(f"SELECT count(*) FROM {table} WHERE is_active = true")
            ).scalar() or 0
        except Exception:
            count = 0
        if count:
            out.append(_virtual_catalog_payload(virtual_id, int(count)))
    return out


def _virtual_catalog_items(virtual_id: str, search: str | None,
                           page: int, per_page: int, db: Session) -> dict[str, object]:
    """Items endpoint shim for the synthetic CHI catalogs.

    Maps chi_door_catalog / chi_parts_catalog rows to the CatalogItem
    serialized shape so CatalogView.vue's table renders without changes.
    Read-only — no POST/PATCH/DELETE wiring.
    """
    if virtual_id == VIRTUAL_CHI_DOORS_ID:
        table = "chi_door_catalog"
        # Drop the COALESCE-to-zero so a NULL sell_price stays NULL.
        # 2026-05-09 (S111): the prior `COALESCE(sell_price, 0)` made every
        # CHI Door whose `sell_price` was unpopulated render as "$0.00 Retail"
        # in /catalog — financially scary even though estimates compute the
        # real price via the pricing engine at line-add time. Returning NULL
        # lets the engine-fallback below populate it with the actual computed
        # retail (cost × tier margin), matching what an estimate will show.
        spec_select = (
            "manufacturer, model_number, door_type, sales_talking_point, "
            "width, height, color, insulation_type, r_value, panel_style, "
            "section_construction, section_thickness_in, section_sides, "
            "section_material, window_option, window_rows, window_type, "
            "finish_type, high_lift, high_lift_in, web_source_url, "
            "sku, description, sell_price AS price, cost, id, brand"
        )
        product_class = "door"
    else:
        table = "chi_parts_catalog"
        spec_select = (
            "sku, name, description, brand, manufacturer, model, part_type, "
            "sell_price AS price, cost, id"
        )
        product_class = "parts"

    where = ["is_active = true"]
    params: dict[str, object] = {}
    if search:
        # LOWER+LIKE for cross-dialect (sqlite tests) compatibility — semantically
        # equivalent to ILIKE on Postgres.
        params["q"] = f"%{search.strip().lower()}%"
        if virtual_id == VIRTUAL_CHI_DOORS_ID:
            where.append(
                "(LOWER(sku) LIKE :q OR LOWER(description) LIKE :q "
                "OR LOWER(model_number) LIKE :q OR LOWER(brand) LIKE :q)"
            )
        else:
            where.append(
                "(LOWER(sku) LIKE :q OR LOWER(name) LIKE :q "
                "OR LOWER(description) LIKE :q OR LOWER(brand) LIKE :q)"
            )
    where_sql = " AND ".join(where)

    total = int(db.execute(
        text(f"SELECT count(*) FROM {table} WHERE {where_sql}"), params
    ).scalar() or 0)
    rows = db.execute(
        text(f"SELECT {spec_select} FROM {table} WHERE {where_sql} "
             f"ORDER BY sku LIMIT :lim OFFSET :off"),
        {**params, "lim": per_page, "off": (page - 1) * per_page},
    ).mappings().all()

    # Fall-back retail computation: when a CHI catalog row has no
    # sell_price (the common case — CHI imports cost-only), compute the
    # tenant's retail from cost × default tier margin via the pricing
    # engine. Otherwise the table would show "$0.00 Retail" which is
    # both inaccurate and alarming. We hydrate settings ONCE per request
    # and reuse for every row. On any pricing-config error, leave price
    # null so the frontend renders "—" rather than a fabricated number.
    pricing_settings = None
    pricing_customer = None
    pricing_status = "ok"  # "ok" | "not_configured" | "error"
    pricing_status_message: str | None = None
    pricing_category_for_class = "doors" if virtual_id == VIRTUAL_CHI_DOORS_ID else "parts"
    try:
        from decimal import Decimal as _D  # local import to avoid module-load cost on cold imports
        from gdx_dispatch.services.pricing_engine import (
            CustomerView,
            PricingConfigError,
            hydrate_settings_from_db,
            price_line,
        )
        try:
            pricing_settings = hydrate_settings_from_db(db)
            pricing_customer = CustomerView(pricing_class="retail", margin_override_pct=None)  # type: ignore[arg-type]
            # Sanity check: ensure the (category, retail) tier set exists for this catalog.
            # If admins haven't seeded tiers for "doors" or "parts" yet, the engine
            # will still hydrate but the tier-lookup will fail per row. Detect early
            # so the response surfaces a single, actionable status to the frontend.
            if pricing_settings.tier_sets.get((pricing_category_for_class, "retail")) is None:
                pricing_status = "not_configured"
                pricing_status_message = (
                    f"No retail margin tier configured for '{pricing_category_for_class}'. "
                    "Set one in Settings → Margin Tiers; otherwise catalog retail prices "
                    "stay blank and estimates fall back to manual entry."
                )
                pricing_settings = None  # disable fallback so frontend shows "—"
        except PricingConfigError as cfg_err:
            pricing_status = "not_configured"
            pricing_status_message = str(cfg_err)
            pricing_settings = None
    except Exception:  # noqa: BLE001 — engine import failed, treat as error not blocker
        pricing_settings = None
        pricing_status = "error"
        pricing_status_message = "Pricing engine unavailable; retail will display as —"

    def _computed_price(cost_val) -> tuple[float | None, bool]:
        """Return (price, is_computed). Computed price comes from the engine
        when sell_price is null; pass-through when sell_price is set."""
        if cost_val in (None, "") or pricing_settings is None or pricing_customer is None:
            return None, False
        try:
            res = price_line(
                cost=_D(str(cost_val)),
                pricing_category=pricing_category_for_class,
                customer=pricing_customer,
                settings=pricing_settings,
            )
            return round(float(res.sell), 2), True
        except PricingConfigError:
            return None, False
        except Exception:  # noqa: BLE001
            return None, False

    items: list[dict[str, object]] = []
    for r in rows:
        explicit_price = _to_float(r.get("price")) if r.get("price") is not None else None
        cost_value = _to_float(r.get("cost")) if r.get("cost") is not None else None
        if explicit_price is not None:
            final_price = explicit_price
            price_source = "catalog"
        else:
            final_price, was_computed = _computed_price(cost_value)
            price_source = "computed" if was_computed else None
        if virtual_id == VIRTUAL_CHI_DOORS_ID:
            spec = {f: r.get(f) for f in DOOR_SPEC_FIELDS if f in r}
            items.append({
                "id": str(r["id"]),
                "catalog_id": virtual_id,
                "sku": r.get("sku"),
                "name": r.get("description") or r.get("model_number") or r.get("sku") or "",
                "description": r.get("description"),
                "description_display": r.get("description") or r.get("model_number") or "",
                "cost": cost_value,
                "price": final_price,
                "price_source": price_source,
                "category": None,
                "product_class": product_class,
                "active": True,
                "qb_item_id": None,
                "created_at": None,
                "updated_at": None,
                "spec": {k: float(v) if isinstance(v, Decimal) else v for k, v in spec.items()},
                "read_only": True,
            })
        else:
            items.append({
                "id": str(r["id"]),
                "catalog_id": virtual_id,
                "sku": r.get("sku"),
                "name": r.get("name") or r.get("sku") or "",
                "description": r.get("description"),
                "description_display": r.get("description") or r.get("name") or "",
                "cost": cost_value,
                "price": final_price,
                "price_source": price_source,
                "category": r.get("part_type"),
                "product_class": product_class,
                "active": True,
                "qb_item_id": None,
                "created_at": None,
                "updated_at": None,
                "read_only": True,
            })
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        # S113 — surface pricing-engine status so the frontend can show a
        # one-time admin banner when retail computation isn't possible.
        "pricing_status": pricing_status,
        "pricing_status_message": pricing_status_message,
    }


def _money(value: float | Decimal) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _to_float(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value or 0)


def _serialize_catalog(catalog: CustomCatalog) -> dict[str, object]:
    source = (catalog.source_system or "manual").strip().lower()
    product_class = (getattr(catalog, "product_class", None) or "parts").strip().lower()
    return {
        "id": str(catalog.id),
        "name": catalog.name,
        "source": source,
        "source_system": source,
        "product_class": product_class,
        # ADR-015 — drives the dynamic form/table for custom catalogs; empty for
        # built-in classes (which the frontend registry covers).
        "field_schema": getattr(catalog, "field_schema", None) or [],
        # ADR-015 Slice 2 — pricing strategy applied to items saved without a price.
        "pricing_strategy": getattr(catalog, "pricing_strategy", None) or "manual",
        "pricing_config": getattr(catalog, "pricing_config", None) or {},
        # #50 — whole-catalog enable/disable (default true for legacy rows).
        "active": bool(getattr(catalog, "active", True)),
        "created_at": catalog.created_at.isoformat() if catalog.created_at else None,
        "updated_at": catalog.updated_at.isoformat() if catalog.updated_at else None,
    }


def _serialize_door_spec(spec: DoorSpec | None) -> dict[str, object] | None:
    if spec is None:
        return None
    out: dict[str, object] = {}
    for field in DOOR_SPEC_FIELDS:
        value = getattr(spec, field, None)
        if isinstance(value, Decimal):
            out[field] = float(value)
        else:
            out[field] = value
    return out


def _serialize_item(item: CustomCatalogItem) -> dict[str, object]:
    product_class = (getattr(item, "product_class", None) or "parts").strip().lower()
    out: dict[str, object] = {
        "id": str(item.id),
        "catalog_id": str(item.catalog_id),
        "sku": item.sku,
        "name": item.name,
        # F-74 / 2026-04-29 — render fallback. The raw column stays NULL
        # for back-compat; `description_display` is what UI/invoice line
        # surfaces should consume. Toggle controlled by tenant policy
        # (catalog_render_name_when_desc_empty), default ON.
        "description": item.description,
        "description_display": (item.description or "").strip() or item.name,
        "cost": _to_float(item.cost),
        "price": _to_float(item.price),
        "category": item.category,
        "vendor": getattr(item, "vendor", None),
        "pricing_category": item.pricing_category,
        "product_class": product_class,
        "active": bool(item.active),
        "qb_item_id": item.qb_item_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        # ADR-015 — user-defined field values for custom catalogs; {} otherwise.
        "attributes": getattr(item, "attributes", None) or {},
    }
    if product_class == "door":
        out["spec"] = _serialize_door_spec(getattr(item, "door_spec", None))
    return out


def _get_catalog_or_404(catalog_id: UUID, db: Session, include_items: bool = False) -> CustomCatalog:
    stmt = select(CustomCatalog).where(CustomCatalog.id == catalog_id, CustomCatalog.deleted_at.is_(None))
    if include_items:
        stmt = stmt.options(
            selectinload(CustomCatalog.items).selectinload(CustomCatalogItem.door_spec)
        )
    row = db.execute(stmt).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Catalog not found")
    return row


def _get_item_or_404(catalog_id: UUID, item_id: UUID, db: Session) -> CustomCatalogItem:
    row = db.execute(
        select(CustomCatalogItem).where(
            CustomCatalogItem.id == item_id,
            CustomCatalogItem.catalog_id == catalog_id,
            CustomCatalogItem.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Catalog item not found")
    return row


def _validate_source(value: str) -> str:
    source = value.strip().lower()
    if source not in ALLOWED_SOURCES:
        raise ValueError("source must be one of: manual, chi, qb")
    return source


# ---------------------------------------------------------------------------
# ADR-015 — custom catalog field schema. A 'custom' catalog declares its own
# fields as data; we validate the schema on create and coerce item values
# against it on write. No per-type SQL table, no migration, no deploy.
# ---------------------------------------------------------------------------

def _validate_field_schema(schema: list[dict] | None) -> list[dict]:
    """Clean + validate a custom catalog's field definitions.

    Each entry: {name, label, type[, section, required, options]}. `name` must be
    a safe slug (unique within the catalog); `type` must be a known input type;
    `select` requires a non-empty `options` list. Returns the normalized list.
    Raises ValueError (→ 422) on any malformed entry.
    """
    if not schema:
        raise ValueError("a custom catalog needs at least one field")
    if len(schema) > 50:
        raise ValueError("a custom catalog supports at most 50 fields")
    cleaned: list[dict] = []
    seen: set[str] = set()
    for raw in schema:
        if not isinstance(raw, dict):
            raise ValueError("each field must be an object")
        name = str(raw.get("name", "")).strip().lower()
        if not _FIELD_NAME_RE.match(name):
            raise ValueError(
                f"field name {name!r} must start with a letter and use only "
                "lowercase letters, digits, or underscores (max 40 chars)"
            )
        if name in seen:
            raise ValueError(f"duplicate field name {name!r}")
        seen.add(name)
        ftype = str(raw.get("type", "text")).strip().lower()
        if ftype not in CUSTOM_FIELD_TYPES:
            raise ValueError(
                f"field type {ftype!r} must be one of {sorted(CUSTOM_FIELD_TYPES)}"
            )
        label = str(raw.get("label", "")).strip() or name.replace("_", " ").title()
        entry: dict[str, object] = {
            "name": name,
            "label": label[:80],
            "type": ftype,
            "section": str(raw.get("section", "attributes")).strip()[:40] or "attributes",
            "required": bool(raw.get("required", False)),
        }
        if ftype == "select":
            options = [str(o).strip() for o in (raw.get("options") or []) if str(o).strip()]
            if not options:
                raise ValueError(f"select field {name!r} needs at least one option")
            entry["options"] = options[:100]
        cleaned.append(entry)
    return cleaned


def _retail_for(catalog: "CustomCatalog", cost, price) -> float:
    """ADR-015 Slice 2 — resolve an item's retail price.

    An explicit non-zero price always wins. Otherwise the catalog's pricing
    strategy computes from cost ('manual'/no-cost → fall back to the entered
    price, else cost). Shared by single-add and bulk-import so the same item
    prices identically no matter how it's created.
    """
    if price:
        return float(price)
    if cost:
        computed = pricing_strategies.compute_price(
            getattr(catalog, "pricing_strategy", None),
            cost,
            config=getattr(catalog, "pricing_config", None),
        )
        if computed is not None:
            return float(computed)
    return float(price if price is not None else (cost or 0))


def _coerce_attributes(schema: list[dict], raw: dict | None) -> dict:
    """Project arbitrary client input onto the catalog's declared fields.

    Unknown keys are dropped (defense, not a 400 — keeps older clients working);
    each known value is coerced to its field type. Missing keys are simply absent.
    """
    if not raw or not isinstance(raw, dict):
        return {}
    by_name = {f["name"]: f for f in (schema or [])}
    out: dict[str, object] = {}
    for name, field in by_name.items():
        if name not in raw:
            continue
        value = raw[name]
        if value is None or value == "":
            continue
        ftype = field["type"]
        try:
            if ftype in ("number", "currency"):
                out[name] = float(value)
            elif ftype == "checkbox":
                out[name] = bool(value)
            else:
                out[name] = str(value)
        except (TypeError, ValueError):
            # Skip an uncastable value rather than 500 — the UI validates too.
            continue
    return out


class CatalogCreateIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=200)
    source: str = Field(default="manual", min_length=1, max_length=60, alias="source_system")
    product_class: str = Field(default="parts", max_length=40)
    # ADR-015 — required when product_class='custom', ignored otherwise.
    field_schema: list[dict] | None = None
    # ADR-015 Slice 2 — pricing strategy id (default 'manual'); pricing_config is
    # a declarative {kind, params} spec, set when creating from a Catalog Pack type.
    pricing_strategy: str = Field(default="manual", max_length=40)
    pricing_config: dict | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("name cannot be blank")
        return trimmed

    @field_validator("source")
    @classmethod
    def _validate_source_field(cls, value: str) -> str:
        return _validate_source(value)

    @field_validator("product_class")
    @classmethod
    def _validate_product_class(cls, value: str) -> str:
        v = (value or "parts").strip().lower()
        if v not in PRODUCT_CLASSES:
            raise ValueError(f"product_class must be one of {sorted(PRODUCT_CLASSES)}")
        return v

    @model_validator(mode="after")
    def _validate_schema_for_class(self) -> "CatalogCreateIn":
        # Custom catalogs must declare a valid field schema; built-in classes
        # carry none (they use the frontend registry / typed spec tables).
        if self.product_class == "custom":
            self.field_schema = _validate_field_schema(self.field_schema)
        else:
            self.field_schema = []
        # ADR-015 Slice 2 — pricing strategy. A pack type may bring a declarative
        # spec (pricing_config with a kind); otherwise the id must be known.
        self.pricing_strategy = (self.pricing_strategy or "manual").strip().lower()
        if self.pricing_config and self.pricing_config.get("kind"):
            # Self-contained declarative spec (built-in or pack) — validate the kind.
            kind = self.pricing_config["kind"]
            if kind not in pricing_strategies.KNOWN_KINDS:
                raise ValueError(
                    f"pricing_config.kind must be one of {sorted(pricing_strategies.KNOWN_KINDS)}"
                )
        elif not pricing_strategies.is_builtin(self.pricing_strategy):
            # A non-built-in id (pack strategy) MUST travel with its declarative
            # spec — we don't resolve it from the in-memory pack registry, which
            # is populated lazily and differs per worker process.
            raise ValueError(
                f"pricing_strategy {self.pricing_strategy!r} requires a pricing_config "
                "with a 'kind' (pack strategies carry their own spec)"
            )
        return self


class CatalogItemCreateIn(BaseModel):
    sku: str | None = Field(default=None, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    cost: float = Field(default=0, ge=0)
    unit_price: float | None = Field(default=None, ge=0)
    price: float | None = Field(default=None, ge=0)
    category: str | None = Field(default=None, max_length=120)
    vendor: str | None = Field(default=None, max_length=200)  # #55
    # Engine pricing bucket (doors/openers/parts/labor/other). Optional —
    # when omitted it's derived from category/product_class at write time so
    # estimate lines never fall through to the $0 manual path. See
    # _derive_pricing_category.
    pricing_category: str | None = Field(default=None, max_length=40)
    active: bool = True
    qb_item_id: str | None = Field(default=None, max_length=120)
    # Sprint typed-catalogs — typed install attributes when the parent
    # catalog's product_class warrants them. Keys must match DOOR_SPEC_FIELDS
    # (or its peer for other classes); unknown keys are ignored, not 400'd,
    # so older clients can keep posting basic items unchanged.
    spec: dict[str, object] | None = None
    # ADR-015 — values for a 'custom' catalog's user-defined fields, keyed by
    # field_schema name. Coerced + filtered against the catalog schema on write.
    attributes: dict[str, object] | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("name cannot be blank")
        return trimmed


class CatalogItemPatchIn(BaseModel):
    sku: str | None = Field(default=None, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    cost: float | None = Field(default=None, ge=0)
    price: float | None = Field(default=None, ge=0)
    category: str | None = Field(default=None, max_length=120)
    vendor: str | None = Field(default=None, max_length=200)  # #55
    active: bool | None = None
    qb_item_id: str | None = Field(default=None, max_length=120)
    spec: dict[str, object] | None = None
    attributes: dict[str, object] | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("name cannot be blank")
        return trimmed


class PricingSettingsPatchIn(BaseModel):
    margins: dict[str, dict[str, float]] | None = None
    tiers: list[dict[str, object]] | None = None
    volume_discounts: list[dict[str, object]] | None = None


class CatalogImportIn(BaseModel):
    format: str = Field(default="json", max_length=10)
    items: list[dict[str, object]] | None = Field(default=None, max_length=10000)
    csv_data: str | None = Field(default=None, max_length=5_000_000)

    @field_validator("format")
    @classmethod
    def _validate_format(cls, value: str) -> str:
        fmt = value.strip().lower()
        if fmt not in {"json", "csv"}:
            raise ValueError("format must be 'json' or 'csv'")
        return fmt


class QBSyncPullIn(BaseModel):
    items: list[dict[str, object]] = Field(default_factory=list, max_length=10000)


class QBSyncPushIn(BaseModel):
    create_missing: bool = True


# F-75 / 2026-04-29 — admin pricing-cleanup view. Lists every active
# catalog item where price <= 0 (or cost <= 0 if include_cost=true).
# Powers the Settings → Catalog "Items needing pricing" link.
@router.get("/api/catalogs/items-needing-pricing", response_model=None)
def items_needing_pricing(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    include_cost: bool = False,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, object]:
    _ = (user, request)
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    where = ["deleted_at IS NULL", "active = true", "(price IS NULL OR price <= 0)"]
    if include_cost:
        where.append("(cost IS NULL OR cost <= 0)")
    sql = (
        "SELECT id, catalog_id, sku, name, description, cost, price, category "
        "FROM custom_catalog_items "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY name ASC LIMIT :lim OFFSET :off"
    )
    rows = db.execute(
        text(sql),
        {"lim": page_size, "off": (page - 1) * page_size},
    ).mappings().all()
    total = db.execute(
        text(f"SELECT COUNT(*) FROM custom_catalog_items WHERE {' AND '.join(where)}")
    ).scalar() or 0
    return {
        "items": [dict(r) for r in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


@router.get("/api/catalogs/all-items", response_model=None)
def list_all_catalog_items(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """D-S122-catalog-n-plus-one: single-shot aggregator for the line-item
    catalog picker. Replaces the prior pattern of /api/catalogs + N GETs
    of /api/catalogs/{id}/items. Returns active items across every custom
    catalog the tenant owns. Virtual CHI catalogs are EXCLUDED because the
    picker on /billing/new is for the tenant's own priced items; CHI rows
    are surfaced separately via the door-catalog picker.
    """
    rows = db.execute(
        select(CustomCatalogItem)
        .join(CustomCatalog, CustomCatalogItem.catalog_id == CustomCatalog.id)
        .where(
            CustomCatalog.deleted_at.is_(None),
            CustomCatalog.active.is_(True),  # #50 — inactive catalogs leave the picker
            CustomCatalogItem.deleted_at.is_(None),
            CustomCatalogItem.active.is_(True),
        )
        .order_by(CustomCatalogItem.name.asc(), CustomCatalogItem.id.asc())
    ).scalars().all()
    return {"items": [_serialize_item(item) for item in rows], "total": len(rows)}


@router.get("/api/catalogs/pricing-categories", response_model=None)
def list_pricing_categories(
    _: dict = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[str]:
    """Valid engine pricing buckets — base set plus any admin-seeded tier
    category. Frontend uses this instead of a hardcoded list so adding a
    margin tier for a new type (e.g. 'gates') surfaces it everywhere."""
    return sorted(_valid_pricing_categories(db))


@router.get("/api/catalogs/pricing-strategies", response_model=None)
def list_pricing_strategies(_: dict = Depends(get_current_user)) -> list[dict[str, object]]:
    """ADR-015 Slice 2 — pricing strategies for the New Catalog dialog. Built-in
    plus any registered by a Catalog Pack."""
    return pricing_strategies.list_strategies()


def _register_pack_strategy_safe(ps: dict) -> None:
    """Register a pack's declarative strategy into the core registry, ignoring
    collisions/bad shapes (best-effort; the spec is also copied onto catalogs)."""
    if ps.get("id") and ps.get("kind"):
        try:
            pricing_strategies.register_pack_strategy(
                ps["id"], ps.get("label", ps["id"]), ps["kind"], ps.get("params")
            )
        except ValueError:
            pass


@router.get("/api/catalogs/pack-types", response_model=None)
def list_pack_catalog_types(_: dict = Depends(get_current_user)) -> list[dict[str, object]]:
    """ADR-015 Slice 3 — catalog types contributed by installed Catalog Packs.

    Fetched from the plugin-host (`GET /api/plugins`). A pack's type is DATA
    (field_schema + a declarative pricing strategy); the New Catalog dialog lists
    these and, on create, copies the schema + pricing onto the catalog so it is
    self-contained — no pack code runs in core at pricing time (ADR-013).
    Resilient: if the plugin-host is unreachable, returns [] (packs are optional).
    """
    from gdx_dispatch.routers.plugins_proxy import _plugin_host_url

    out: list[dict[str, object]] = []
    try:
        resp = httpx.get(f"{_plugin_host_url()}/api/plugins", timeout=3.0)
        resp.raise_for_status()
        plugins = resp.json()
    except Exception as exc:  # noqa: BLE001 — plugin-host down/absent is non-fatal
        # Packs are optional; don't break the catalog page. Log so a real
        # plugin-host outage is visible rather than silently showing no packs.
        log.warning("pack-types: plugin-host unreachable (%s): %s", type(exc).__name__, str(exc)[:200])
        return out
    for p in plugins:
        for ps in (p.get("pricing_strategies") or []):
            _register_pack_strategy_safe(ps)
        for ct in (p.get("catalog_types") or []):
            if not ct.get("key") or not ct.get("label"):
                continue
            ps = ct.get("pricing_strategy") or {}
            _register_pack_strategy_safe(ps)
            config = (
                {"kind": ps["kind"], "params": ps.get("params") or {}}
                if ps.get("kind") else {}
            )
            out.append({
                "key": ct["key"],
                "label": ct["label"],
                "plugin": p.get("key"),
                "field_schema": ct.get("field_schema") or [],
                "pricing_strategy": ps.get("id") or "manual",
                "pricing_config": config,
            })
    return out


@router.get("/api/catalogs", response_model=None)
def list_catalogs(_: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, object]]:
    # Sprint typed-catalogs follow-up — surface manufacturer feed tables
    # (chi_door_catalog, chi_parts_catalog) as virtual read-only catalogs so
    # the Catalogs page picker shows everything a tenant can browse, not just
    # rows in custom_catalogs.
    virtual = _list_virtual_catalogs(db)
    rows = db.execute(
        select(CustomCatalog)
        .where(CustomCatalog.deleted_at.is_(None))
        .order_by(CustomCatalog.created_at.desc(), CustomCatalog.id.desc())
    ).scalars().all()
    return [*virtual, *(_serialize_catalog(row) for row in rows)]


@router.post("/api/catalogs", response_model=None, status_code=201)
def create_catalog(
    payload: CatalogCreateIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    row = CustomCatalog(
        name=payload.name,
        source_system=payload.source,
        product_class=payload.product_class,
        field_schema=payload.field_schema or [],
        pricing_strategy=payload.pricing_strategy or "manual",
        pricing_config=payload.pricing_config or {},
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="catalog_created",
        entity_type="catalog",
        entity_id=str(row.id),
        details={
            "name": row.name,
            "source_system": row.source_system,
            "product_class": row.product_class,
        },
        request=request,
    )
    db.commit()
    return _serialize_catalog(row)


class CatalogPatchIn(BaseModel):
    # #50 — currently only the active flag is mutable post-create.
    active: bool


@router.patch("/api/catalogs/{catalog_id}", response_model=None)
def patch_catalog(
    catalog_id: UUID,
    payload: CatalogPatchIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Enable/disable a whole catalog (#50). Inactive catalogs stay on the
    Catalogs page but their items leave the estimate/billing pickers
    (all-items filters CustomCatalog.active). Virtual CHI catalogs can't toggle.
    """
    if str(catalog_id) in VIRTUAL_CATALOG_IDS:
        raise HTTPException(status_code=409, detail="Virtual catalogs cannot be modified")
    row = _get_catalog_or_404(catalog_id, db)
    row.active = payload.active
    db.commit()
    db.refresh(row)

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="catalog_updated",
        entity_type="catalog",
        entity_id=str(row.id),
        details={"name": row.name, "active": row.active},
        request=request,
    )
    db.commit()
    return _serialize_catalog(row)


@router.delete("/api/catalogs/{catalog_id}", response_model=None)
def delete_catalog(
    catalog_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    """Soft-delete a catalog (#49). Sets deleted_at; list/get/all-items already
    filter deleted_at IS NULL, so the catalog and its items leave the pickers.
    Items keep their own rows (filtered via the catalog join). Virtual CHI
    catalogs are feed-backed and cannot be deleted.
    """
    if str(catalog_id) in VIRTUAL_CATALOG_IDS:
        raise HTTPException(status_code=409, detail="Virtual catalogs cannot be deleted")
    row = _get_catalog_or_404(catalog_id, db)
    row.deleted_at = utcnow()
    db.commit()

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="catalog_deleted",
        entity_type="catalog",
        entity_id=str(row.id),
        details={"name": row.name, "soft_delete": True},
        request=request,
    )
    db.commit()
    return {"deleted": True}


@router.get("/api/catalogs/{catalog_id}", response_model=None)
def get_catalog(
    catalog_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if str(catalog_id) in VIRTUAL_CATALOG_IDS:
        items = _virtual_catalog_items(str(catalog_id), search=None, page=1, per_page=10000, db=db)
        payload = _virtual_catalog_payload(str(catalog_id), items["total"])
        payload["items"] = items["items"]
        return payload

    row = _get_catalog_or_404(catalog_id, db, include_items=True)
    payload = _serialize_catalog(row)
    payload["items"] = [_serialize_item(item) for item in row.items if item.deleted_at is None]
    return payload


@router.get("/api/catalogs/{catalog_id}/items", response_model=None)
def list_catalog_items(
    catalog_id: UUID,
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=250),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    # Virtual CHI catalogs route through their own read-only items shim.
    if str(catalog_id) in VIRTUAL_CATALOG_IDS:
        return _virtual_catalog_items(str(catalog_id), search, page, per_page, db)

    _get_catalog_or_404(catalog_id, db)

    filters = [CustomCatalogItem.catalog_id == catalog_id, CustomCatalogItem.deleted_at.is_(None)]
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                CustomCatalogItem.sku.ilike(pattern),
                CustomCatalogItem.name.ilike(pattern),
                CustomCatalogItem.description.ilike(pattern),
                CustomCatalogItem.category.ilike(pattern),
            )
        )

    total = int(db.execute(select(func.count()).where(*filters)).scalar_one())
    rows = db.execute(
        select(CustomCatalogItem)
        .where(*filters)
        .options(selectinload(CustomCatalogItem.door_spec))
        .order_by(CustomCatalogItem.created_at.desc(), CustomCatalogItem.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).scalars().all()

    return {
        "items": [_serialize_item(row) for row in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.post("/api/catalogs/{catalog_id}/items", response_model=None, status_code=201)
def add_catalog_item(
    catalog_id: UUID,
    payload: CatalogItemCreateIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _get_catalog_or_404(catalog_id, db)

    # F-74 / 2026-04-29 — tenant may require non-empty description.
    tenant_id_for_policy, _ = _audit_ids(user, request)
    from gdx_dispatch.modules.catalog_policy import enforce_save_pricing, require_description_or_422
    require_description_or_422(tenant_id_for_policy, payload.description)

    catalog = _get_catalog_or_404(catalog_id, db)
    product_class = (catalog.product_class or "parts").strip().lower()

    # ADR-015 Slice 2 — apply the catalog's pricing strategy when no price was
    # entered. The zero-price policy gate (F-75) sees the FINAL price, so a
    # strategy-computed non-zero retail correctly stays active.
    effective_price = _retail_for(catalog, payload.cost, payload.price)
    active_after_policy = enforce_save_pricing(
        tenant_id_for_policy, price=effective_price
    )

    row = CustomCatalogItem(
        catalog_id=catalog_id,
        sku=payload.sku.strip() if payload.sku else None,
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        cost=_money(payload.cost),
        price=_money(effective_price),
        category=payload.category.strip() if payload.category else None,
        vendor=payload.vendor.strip() if payload.vendor else None,
        pricing_category=_derive_pricing_category(
            payload.pricing_category, payload.category, product_class,
            _valid_pricing_categories(db),
        ),
        product_class=product_class,
        active=payload.active and active_after_policy,
        qb_item_id=payload.qb_item_id.strip() if payload.qb_item_id else None,
    )
    # ADR-015 — coerce custom-field values against the catalog's declared schema.
    if product_class == "custom":
        row.attributes = _coerce_attributes(catalog.field_schema or [], payload.attributes)
    db.add(row)
    db.flush()

    # Sprint typed-catalogs — persist class-specific spec when applicable.
    if product_class == "door" and payload.spec:
        spec_kwargs = {k: payload.spec[k] for k in DOOR_SPEC_FIELDS if k in payload.spec}
        if spec_kwargs:
            db.add(DoorSpec(catalog_item_id=row.id, **spec_kwargs))

    db.commit()
    db.refresh(row)

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="catalog_item_added",
        entity_type="catalog_item",
        entity_id=str(row.id),
        details={"catalog_id": str(catalog_id), "sku": row.sku, "name": row.name},
        request=request,
    )
    db.commit()
    return _serialize_item(row)


# ---------------------------------------------------------------------------
# Save-from-estimate-line — promote a free-text estimate line into the
# catalog with engine-computed retail sell price. Lab demo, 2026-04-30.
# ---------------------------------------------------------------------------

_VALID_PRICING_CATEGORIES = {"doors", "openers", "parts", "labor", "other"}

# product_class (door/opener/spring/…) → engine pricing_category. springs,
# tracks and remotes are all priced as "parts".
_PRODUCT_CLASS_TO_PRICING = {
    "door": "doors", "opener": "openers", "spring": "parts",
    "track": "parts", "remote": "parts", "parts": "parts",
}

# Free-form category words → bucket, for words that aren't just a singular of
# the bucket. Garage-door domain: remotes/keypads/springs/tracks/cables are all
# priced as parts; operators are openers.
_PRICING_SYNONYMS = {
    "operator": "openers", "operators": "openers",
    "remote": "parts", "remotes": "parts", "keypad": "parts", "keypads": "parts",
    "accessory": "parts", "accessories": "parts", "hardware": "parts",
    "spring": "parts", "springs": "parts", "track": "parts", "tracks": "parts",
    "cable": "parts", "cables": "parts", "part": "parts",
}


def _normalize_to_bucket(cand: str | None, valid: set[str]) -> str | None:
    """Map one free-form word to a valid (non-labor) pricing bucket, or None.

    Handles exact matches, singular→plural (opener→openers, door→doors, and
    any admin-seeded type like gate→gates), and domain synonyms."""
    c = (cand or "").strip().lower()
    if not c:
        return None
    if c in valid and c != "labor":
        return c
    if f"{c}s" in valid and f"{c}s" != "labor":
        return f"{c}s"
    return _PRICING_SYNONYMS.get(c)


def _valid_pricing_categories(db: Session) -> set[str]:
    """Base buckets ∪ any pricing_category that has an active tier set, so an
    admin-seeded type (e.g. 'gates') becomes valid everywhere with no code
    change. Base set is the floor — validation still works before any seed."""
    rows = (
        db.execute(select(PricingTierSet.pricing_category).where(PricingTierSet.active).distinct())
        .scalars()
        .all()
    )
    return _VALID_PRICING_CATEGORIES | {(r or "").strip().lower() for r in rows if r}


def _derive_pricing_category(
    explicit: str | None,
    category: str | None,
    product_class: str | None,
    valid: set[str] = _VALID_PRICING_CATEGORIES,
) -> str | None:
    """Best-guess the engine pricing_category for a catalog item.

    Explicit value wins; else fall back to the free-form `category` if it's a
    valid bucket, else map from product_class. Returns a value that always has
    a seeded tier set so estimate lines never hit the $0 manual fallback.
    'labor' is never returned — labor lines price via the LaborPriceItem matrix,
    not the tier engine (which rejects category='labor'); those stay None.
    """
    for cand in (explicit, category):
        bucket = _normalize_to_bucket(cand, valid)
        if bucket:
            return bucket
    pc = (product_class or "").strip().lower()
    if pc == "labor":
        return None
    return _PRODUCT_CLASS_TO_PRICING.get(pc, "other")


class SaveFromEstimateLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=200)
    cost: float = Field(ge=0)
    pricing_category: str
    sku: str | None = None
    category: str | None = None  # free-form display category

    @field_validator("pricing_category")
    @classmethod
    def _validate_pricing_category(cls, value: str) -> str:
        v = (value or "").strip().lower()
        if v not in _VALID_PRICING_CATEGORIES:
            raise ValueError(
                f"pricing_category must be one of {sorted(_VALID_PRICING_CATEGORIES)}"
            )
        return v


def _get_or_create_default_catalog(db: Session) -> CustomCatalog:
    """Return the tenant's manual catalog, creating a 'Default' one if none."""
    row = db.execute(
        select(CustomCatalog)
        .where(
            CustomCatalog.deleted_at.is_(None),
            CustomCatalog.source_system == "manual",
        )
        .order_by(CustomCatalog.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if row:
        return row
    row = CustomCatalog(name="Default", source_system="manual")
    db.add(row)
    db.flush()
    return row


@router.post("/api/catalogs/save-from-estimate-line", response_model=None, status_code=201)
def save_from_estimate_line(
    payload: SaveFromEstimateLineIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Promote a free-text estimate line into the catalog with engine-computed price.

    Cost goes in; pricing engine looks up the tenant's retail margin tier for
    the given pricing_category and returns the sell price. Stored as a new
    CustomCatalogItem in the default manual catalog.
    """
    from gdx_dispatch.services.pricing_engine import (
        CustomerView,
        PricingConfigError,
        hydrate_settings_from_db,
        price_line,
    )

    settings = hydrate_settings_from_db(db)
    customer = CustomerView(pricing_class="retail", margin_override_pct=None)
    try:
        line_price = price_line(
            cost=Decimal(str(payload.cost)),
            pricing_category=payload.pricing_category,
            customer=customer,
            settings=settings,
        )
    except PricingConfigError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Pricing engine cannot price this cost: {exc}",
        ) from exc

    catalog = _get_or_create_default_catalog(db)

    row = CustomCatalogItem(
        catalog_id=catalog.id,
        sku=payload.sku.strip() if payload.sku else None,
        name=payload.description.strip(),
        description=payload.description.strip(),
        cost=_money(payload.cost),
        price=_money(line_price.sell),
        category=payload.category.strip() if payload.category else None,
        pricing_category=payload.pricing_category,
        active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="catalog_item_saved_from_estimate",
        entity_type="catalog_item",
        entity_id=str(row.id),
        details={
            "catalog_id": str(catalog.id),
            "name": row.name,
            "cost": float(line_price.cost),
            "sell": float(line_price.sell),
            "margin_pct": float(line_price.margin_pct),
            "pricing_category": payload.pricing_category,
            "pricing_source": line_price.source,
        },
        request=request,
    )
    db.commit()

    result = _serialize_item(row)
    result["margin_pct"] = float(line_price.margin_pct)
    result["pricing_source"] = line_price.source
    return result


@router.patch("/api/catalogs/{catalog_id}/items/{item_id}", response_model=None)
def patch_catalog_item(
    catalog_id: UUID,
    item_id: UUID,
    payload: CatalogItemPatchIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    row = _get_item_or_404(catalog_id, item_id, db)
    updates = payload.model_dump(exclude_unset=True)

    if "cost" in updates and updates["cost"] is not None:
        row.cost = _money(float(updates["cost"]))
    if "price" in updates and updates["price"] is not None:
        row.price = _money(float(updates["price"]))
    if "sku" in updates:
        row.sku = updates["sku"].strip() if isinstance(updates["sku"], str) and updates["sku"].strip() else None
    if "name" in updates and updates["name"] is not None:
        row.name = updates["name"].strip()
    if "description" in updates:
        row.description = (
            updates["description"].strip()
            if isinstance(updates["description"], str) and updates["description"].strip()
            else None
        )
    if "category" in updates:
        row.category = (
            updates["category"].strip()
            if isinstance(updates["category"], str) and updates["category"].strip()
            else None
        )
    if "vendor" in updates:
        row.vendor = (
            updates["vendor"].strip()
            if isinstance(updates["vendor"], str) and updates["vendor"].strip()
            else None
        )
    if "active" in updates and updates["active"] is not None:
        row.active = bool(updates["active"])
    if "qb_item_id" in updates:
        row.qb_item_id = updates["qb_item_id"].strip() if isinstance(updates["qb_item_id"], str) else None

    # Sprint typed-catalogs — class-specific spec patch.
    spec_payload = updates.get("spec")
    if spec_payload and (row.product_class or "parts").lower() == "door":
        spec = row.door_spec
        if spec is None:
            spec = DoorSpec(catalog_item_id=row.id)
            db.add(spec)
        for field in DOOR_SPEC_FIELDS:
            if field in spec_payload:
                setattr(spec, field, spec_payload[field])

    # ADR-015 — merge custom-field values, coerced against the catalog schema.
    if "attributes" in updates and (row.product_class or "parts").lower() == "custom":
        coerced = _coerce_attributes(row.catalog.field_schema or [], updates.get("attributes"))
        merged = {**(row.attributes or {}), **coerced}
        row.attributes = merged

    db.commit()
    db.refresh(row)

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="catalog_item_updated",
        entity_type="catalog_item",
        entity_id=str(row.id),
        details={"catalog_id": str(catalog_id), "changed": list(updates.keys())},
        request=request,
    )
    db.commit()
    return _serialize_item(row)


@router.delete("/api/catalogs/{catalog_id}/items/{item_id}", response_model=None)
def delete_catalog_item(
    catalog_id: UUID,
    item_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    row = _get_item_or_404(catalog_id, item_id, db)
    row.deleted_at = utcnow()
    row.active = False
    db.commit()

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="catalog_item_deleted",
        entity_type="catalog_item",
        entity_id=str(row.id),
        details={"catalog_id": str(catalog_id), "sku": row.sku, "soft_delete": True},
        request=request,
    )
    db.commit()
    return {"deleted": True}


def _import_attributes(catalog: "CustomCatalog", raw: dict[str, object], product_class: str) -> dict | None:
    """Custom-field values for a bulk/AI-imported row (#53).

    Only custom catalogs have a field_schema; for everything else return None.
    Accepts either a nested ``attributes`` dict or flat columns named after the
    schema fields (so a CSV with a "Vendor" column works the same as JSON with
    ``{"attributes": {"Vendor": …}}``). _coerce_attributes keeps only declared
    fields, so scalar columns (sku/name/cost/…) are dropped automatically.
    """
    if product_class != "custom":
        return None
    src: dict[str, object] = dict(raw) if isinstance(raw, dict) else {}
    nested = raw.get("attributes")
    if isinstance(nested, dict):
        src.update(nested)
    return _coerce_attributes(catalog.field_schema or [], src)


def _normalize_import_item(raw: dict[str, object]) -> CatalogItemCreateIn:
    return CatalogItemCreateIn(
        sku=str(raw.get("sku") or "").strip() or None,
        name=str(raw.get("name") or "").strip(),
        description=str(raw.get("description") or "").strip() or None,
        cost=float(raw.get("cost") or 0),
        price=float(raw.get("price")) if raw.get("price") not in (None, "") else None,
        category=str(raw.get("category") or "").strip() or None,
        vendor=str(raw.get("vendor") or raw.get("supplier") or "").strip() or None,
        pricing_category=str(raw.get("pricing_category") or "").strip() or None,
        active=bool(raw.get("active", True)),
        qb_item_id=str(raw.get("qb_item_id") or "").strip() or None,
    )


@router.post("/api/catalogs/{catalog_id}/import", response_model=None)
def bulk_import_catalog_items(
    catalog_id: UUID,
    payload: CatalogImportIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    catalog = _get_catalog_or_404(catalog_id, db)
    product_class = (catalog.product_class or "parts").strip().lower()

    if payload.format == "json":
        raw_items = payload.items or []
    else:
        if not payload.csv_data:
            raise HTTPException(status_code=422, detail="csv_data is required for csv imports")
        raw_items = [dict(row) for row in csv.DictReader(StringIO(payload.csv_data))]

    valid_categories = _valid_pricing_categories(db)
    imported = 0
    for raw in raw_items:
        item = _normalize_import_item(raw)
        row = CustomCatalogItem(
            catalog_id=catalog_id,
            sku=item.sku,
            name=item.name,
            description=item.description,
            cost=_money(item.cost),
            # ADR-015 Slice 2 — same pricing strategy as single-add, so a CSV/JSON
            # import of cost-only rows prices identically to the Add form.
            price=_money(_retail_for(catalog, item.cost, item.price)),
            category=item.category,
            vendor=item.vendor,
            pricing_category=_derive_pricing_category(
                item.pricing_category, item.category, product_class, valid_categories
            ),
            product_class=product_class,
            active=item.active,
            qb_item_id=item.qb_item_id,
        )
        attrs = _import_attributes(catalog, raw, product_class)
        if attrs is not None:
            row.attributes = attrs
        db.add(row)
        imported += 1

    db.commit()

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="catalog_imported",
        entity_type="catalog",
        entity_id=str(catalog_id),
        details={"format": payload.format, "imported": imported},
        request=request,
    )
    db.commit()
    return {"imported": imported, "failed": 0}


def _upsert_qb_item(catalog: "CustomCatalog", raw: dict[str, object], db: Session) -> str:
    catalog_id = catalog.id
    qb_item_id = str(raw.get("qb_item_id") or "").strip() or None
    sku = str(raw.get("sku") or "").strip() or None

    match = None
    if qb_item_id:
        match = db.execute(
            select(CustomCatalogItem).where(
                CustomCatalogItem.catalog_id == catalog_id,
                CustomCatalogItem.qb_item_id == qb_item_id,
                CustomCatalogItem.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
    if match is None and sku:
        match = db.execute(
            select(CustomCatalogItem).where(
                CustomCatalogItem.catalog_id == catalog_id,
                CustomCatalogItem.sku == sku,
                CustomCatalogItem.deleted_at.is_(None),
            )
        ).scalar_one_or_none()

    if match is None:
        row = CustomCatalogItem(
            catalog_id=catalog_id,
            sku=sku,
            name=str(raw.get("name") or "").strip() or "QB Item",
            description=str(raw.get("description") or "").strip() or None,
            cost=_money(float(raw.get("cost") or 0)),
            price=_money(_retail_for(catalog, raw.get("cost"), raw.get("price"))),
            category=str(raw.get("category") or "").strip() or None,
            vendor=str(raw.get("vendor") or raw.get("supplier") or "").strip() or None,
            active=bool(raw.get("active", True)),
            qb_item_id=qb_item_id,
        )
        db.add(row)
        return "created"

    match.sku = sku
    match.name = str(raw.get("name") or match.name)
    match.description = str(raw.get("description") or "").strip() or match.description
    match.cost = _money(float(raw.get("cost") or match.cost or 0))
    match.price = _money(_retail_for(catalog, raw.get("cost") or match.cost, raw.get("price")) or match.price or 0)
    match.category = str(raw.get("category") or "").strip() or match.category
    match.active = bool(raw.get("active", match.active))
    if qb_item_id:
        match.qb_item_id = qb_item_id
    return "updated"


@router.post("/api/catalogs/{catalog_id}/ai-import", response_model=None)
async def ai_import_catalog(
    catalog_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Use AI (Claude) to extract parts from an uploaded file (PDF text, CSV, or spreadsheet).

    Accepts: text content, CSV, or JSON list. For PDFs/images, client should
    pre-extract text or use OCR. The AI receives raw text and returns a
    structured list of parts which are then bulk-imported.
    """
    from gdx_dispatch.core.ai_router import AITask, get_ai_router

    catalog = _get_catalog_or_404(catalog_id, db)

    content_bytes = await file.read()
    try:
        text_content = content_bytes.decode("utf-8", errors="replace")[:50000]  # Cap at 50KB
    except Exception:
        log.exception("ai_import_catalog_failed extra_context=%s", "unknown")
        raise HTTPException(status_code=422, detail="Could not read file as text. Upload CSV, TXT, or extracted PDF text.") from None

    tenant_id, _ = _audit_ids(user, request)
    prompt = f"""You are parsing a parts catalog for a garage door company. Extract all parts from the text below and return a JSON array.

For each part, extract:
- sku (or generate one from name if missing)
- name (required)
- description
- cost (unit cost as number)
- price (retail price as number, can be same as cost if not given)
- category
- vendor (manufacturer)
- manufacturer_part_number

Return ONLY a valid JSON array, no prose. Example:
[{{"sku":"SPR-001","name":"Torsion Spring 2\" 0.243","description":"Right wind","cost":45.00,"price":89.00,"category":"Springs","vendor":"CHI","manufacturer_part_number":"TS-0243R"}}]

Text to parse:
---
{text_content}
---

JSON array:"""

    try:
        ai_response = await get_ai_router().generate(
            task=AITask.GENERAL,
            prompt=prompt,
            tenant_id=tenant_id,
            max_tokens=4096,
            temperature=0.1,
        )
    except Exception as exc:
        log.exception("ai_import_catalog_failed extra_context=%s", exc)
        raise HTTPException(status_code=500, detail=f"AI extraction failed: {exc}") from None

    # Parse the JSON response
    import json as _json
    import re as _re
    text = ai_response if isinstance(ai_response, str) else ai_response.get("text", "")
    # Strip markdown code fences if present
    text = _re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = _re.sub(r"\s*```$", "", text)
    try:
        extracted_items = _json.loads(text)
    except Exception:
        log.exception("ai_import_catalog_failed extra_context=%s", "unknown")
        raise HTTPException(status_code=500, detail=f"AI returned invalid JSON: {text[:200]}") from None

    if not isinstance(extracted_items, list):
        raise HTTPException(status_code=500, detail="AI did not return a JSON array")

    # Import the extracted items
    product_class = (catalog.product_class or "parts").strip().lower()
    imported = 0
    for raw in extracted_items:
        try:
            item = _normalize_import_item(raw)
            row = CustomCatalogItem(
                catalog_id=catalog_id,
                sku=item.sku,
                name=item.name,
                description=item.description,
                cost=_money(item.cost),
                price=_money(_retail_for(catalog, item.cost, item.price)),
                category=item.category,
                vendor=item.vendor,
                product_class=product_class,
                active=True,
            )
            attrs = _import_attributes(catalog, raw, product_class)
            if attrs is not None:
                row.attributes = attrs
            db.add(row)
            imported += 1
        except Exception:
            log.exception("catalog_ai_import_row_failed")
            continue

    db.commit()

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="catalog_ai_imported",
        entity_type="catalog",
        entity_id=str(catalog_id),
        details={"imported": imported, "total_extracted": len(extracted_items), "filename": file.filename},
        request=request,
    )
    db.commit()
    return {
        "imported": imported,
        "total_extracted": len(extracted_items),
        "sample": extracted_items[:3] if extracted_items else [],
    }


@router.post("/api/catalogs/{catalog_id}/sync/qb/pull", response_model=None)
def qb_pull_sync(
    catalog_id: UUID,
    payload: QBSyncPullIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    catalog = _get_catalog_or_404(catalog_id, db)
    created = 0
    updated = 0
    for raw in payload.items:
        action = _upsert_qb_item(catalog, raw, db)
        if action == "created":
            created += 1
        else:
            updated += 1
    db.commit()

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="catalog_qb_pulled",
        entity_type="catalog",
        entity_id=str(catalog_id),
        details={"created": created, "updated": updated},
        request=request,
    )
    db.commit()
    return {"created": created, "updated": updated}


@router.post("/api/catalogs/{catalog_id}/sync/qb/push", response_model=None)
def qb_push_sync(
    catalog_id: UUID,
    payload: QBSyncPushIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _get_catalog_or_404(catalog_id, db)
    rows = db.execute(
        select(CustomCatalogItem).where(
            CustomCatalogItem.catalog_id == catalog_id,
            CustomCatalogItem.deleted_at.is_(None),
            CustomCatalogItem.active.is_(True),
        )
    ).scalars().all()

    pushed_items: list[dict[str, object]] = []
    for row in rows:
        if row.qb_item_id:
            continue
        if not payload.create_missing:
            continue
        qb_item_id = f"QB-{str(row.id)[:8]}"
        row.qb_item_id = qb_item_id
        pushed_items.append({"item_id": str(row.id), "qb_item_id": qb_item_id})

    db.commit()

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="catalog_qb_pushed",
        entity_type="catalog",
        entity_id=str(catalog_id),
        details={"pushed_count": len(pushed_items)},
        request=request,
    )
    db.commit()
    return {"pushed": len(pushed_items), "items": pushed_items}


# Back-compat wrappers used by existing tests that import pricing helpers from catalog.py.
def get_pricing_settings(_: dict = Depends(get_current_user)) -> dict[str, object]:
    return deepcopy(_PRICING_SETTINGS)


def patch_pricing_settings(payload: PricingSettingsPatchIn, _: dict = Depends(get_current_user)) -> dict[str, object]:
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        _PRICING_SETTINGS[key] = value
    return deepcopy(_PRICING_SETTINGS)


def calculate_sell_price(
    cost: float,
    margin_type: str,
    customer_type: str,
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    margin_key = margin_type.strip().lower()
    customer_key = customer_type.strip().lower()

    margin_map = _PRICING_SETTINGS.get("margins", {})
    if not isinstance(margin_map, dict) or margin_key not in margin_map:
        raise HTTPException(status_code=422, detail="Unknown margin_type")

    customer_margins = margin_map[margin_key]
    if not isinstance(customer_margins, dict) or customer_key not in customer_margins:
        raise HTTPException(status_code=422, detail="Unknown customer_type")

    margin = float(customer_margins[customer_key])
    if margin < 0 or margin >= 1:
        raise HTTPException(status_code=422, detail="Invalid margin value")

    sell_price = _money(cost / (1 - margin))
    return {
        "cost": _money(cost),
        "margin_type": margin_key,
        "customer_type": customer_key,
        "margin": margin,
        "sell_price": sell_price,
    }
