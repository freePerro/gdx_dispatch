"""Door & Parts Catalog — CHI doors, openers, and parts with pricing.

Migrated from GDX dispatch. Provides searchable catalog for estimates
and proposals with cost/sell pricing and margin calculations.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/catalog",
    tags=["door-catalog"],
    dependencies=[Depends(require_module("estimates"))],
)


def _float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):  # returns None if input cannot be converted to float
        logging.getLogger(__name__).exception("_float caught exception")
        return None


# ── Door Catalog ─────────────────────────────────────────────────────────────

@router.get("/doors")
def list_doors(
    request: Request,
    search: str = Query(default="", max_length=200),
    door_type: str = Query(default=""),
    width: float | None = Query(default=None),
    height: float | None = Query(default=None),
    color: str = Query(default=""),
    insulation_type: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Install/estimate door picker source.

    Sprint typed-catalogs Slice 3 — UNIONs CHI feed (chi_door_catalog) with
    tenant-custom doors (custom_catalog_items + door_specs where
    product_class='door'). Same shape on both sides so the picker UI sees
    one stream.
    """
    where_chi = ["is_active = true"]
    where_custom = ["cci.product_class = 'door'", "cci.active = true", "cci.deleted_at IS NULL"]
    params: dict[str, Any] = {}

    if search:
        where_chi.append("(description ILIKE :q OR model_number ILIKE :q OR sku ILIKE :q OR brand ILIKE :q)")
        where_custom.append(
            "(cci.description ILIKE :q OR ds.model_number ILIKE :q OR cci.sku ILIKE :q "
            "OR ds.manufacturer ILIKE :q OR cci.name ILIKE :q)"
        )
        params["q"] = f"%{search}%"
    if door_type:
        where_chi.append("door_type = :dt")
        where_custom.append("ds.door_type = :dt")
        params["dt"] = door_type
    if width is not None:
        where_chi.append("width = :w")
        where_custom.append("ds.width = :w")
        params["w"] = width
    if height is not None:
        where_chi.append("height = :h")
        where_custom.append("ds.height = :h")
        params["h"] = height
    if color:
        where_chi.append("color ILIKE :color")
        where_custom.append("ds.color ILIKE :color")
        params["color"] = f"%{color}%"
    if insulation_type:
        where_chi.append("insulation_type ILIKE :ins")
        where_custom.append("ds.insulation_type ILIKE :ins")
        params["ins"] = f"%{insulation_type}%"

    chi_where_sql = " AND ".join(where_chi)
    custom_where_sql = " AND ".join(where_custom)

    total_chi = db.execute(text(f"SELECT count(*) FROM chi_door_catalog WHERE {chi_where_sql}"), params).scalar() or 0
    total_custom = db.execute(
        text(
            f"SELECT count(*) FROM custom_catalog_items cci "
            f"LEFT JOIN door_specs ds ON ds.catalog_item_id = cci.id "
            f"WHERE {custom_where_sql}"
        ),
        params,
    ).scalar() or 0
    total = int(total_chi) + int(total_custom)

    offset = (page - 1) * page_size
    union_sql = f"""
        SELECT id, sku, brand, manufacturer, model_number, door_type, description,
               sales_talking_point, width, height, color, cost, sell_price,
               insulation_type, r_value, panel_style, section_construction,
               window_option, window_type, finish_type, high_lift, is_custom,
               source
        FROM (
            SELECT id, sku, brand, manufacturer, model_number, door_type, description,
                   sales_talking_point, width, height, color, cost, sell_price,
                   insulation_type, r_value, panel_style, section_construction,
                   window_option, window_type, finish_type, high_lift, is_custom,
                   'chi' AS source, brand AS sort_brand, model_number AS sort_model
            FROM chi_door_catalog WHERE {chi_where_sql}

            UNION ALL

            SELECT cci.id AS id, cci.sku AS sku,
                   ds.manufacturer AS brand, ds.manufacturer AS manufacturer,
                   ds.model_number AS model_number, ds.door_type AS door_type,
                   COALESCE(cci.description, cci.name) AS description,
                   ds.sales_talking_point AS sales_talking_point,
                   ds.width AS width, ds.height AS height, ds.color AS color,
                   cci.cost AS cost, cci.price AS sell_price,
                   ds.insulation_type AS insulation_type, ds.r_value AS r_value,
                   ds.panel_style AS panel_style, ds.section_construction AS section_construction,
                   ds.window_option AS window_option, ds.window_type AS window_type,
                   ds.finish_type AS finish_type, ds.high_lift AS high_lift,
                   true AS is_custom,
                   'custom' AS source,
                   ds.manufacturer AS sort_brand, ds.model_number AS sort_model
            FROM custom_catalog_items cci
            LEFT JOIN door_specs ds ON ds.catalog_item_id = cci.id
            WHERE {custom_where_sql}
        ) AS doors
        ORDER BY sort_brand NULLS LAST, sort_model NULLS LAST
        LIMIT :lim OFFSET :off
    """
    rows = db.execute(text(union_sql), {**params, "lim": page_size, "off": offset}).mappings().all()

    items = [
        {
            "id": r["id"], "sku": r["sku"], "brand": r["brand"],
            "manufacturer": r["manufacturer"], "model_number": r["model_number"],
            "door_type": r["door_type"], "description": r["description"],
            "sales_talking_point": r["sales_talking_point"],
            "width": _float(r["width"]), "height": _float(r["height"]),
            "color": r["color"], "cost": _float(r["cost"]), "sell_price": _float(r["sell_price"]),
            "insulation_type": r["insulation_type"], "r_value": _float(r["r_value"]),
            "panel_style": r["panel_style"], "section_construction": r["section_construction"],
            "window_option": r["window_option"], "window_type": r["window_type"],
            "finish_type": r["finish_type"], "high_lift": r["high_lift"],
            "is_custom": r["is_custom"],
            "source": r["source"],
        }
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/doors/filters")
def door_filters(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Available filter values for the door catalog."""
    types = [r[0] for r in db.execute(text("SELECT DISTINCT door_type FROM chi_door_catalog WHERE door_type IS NOT NULL ORDER BY door_type")).fetchall()]
    colors = [r[0] for r in db.execute(text("SELECT DISTINCT color FROM chi_door_catalog WHERE color IS NOT NULL ORDER BY color")).fetchall()]
    brands = [r[0] for r in db.execute(text("SELECT DISTINCT brand FROM chi_door_catalog WHERE brand IS NOT NULL ORDER BY brand")).fetchall()]
    insulations = [r[0] for r in db.execute(text("SELECT DISTINCT insulation_type FROM chi_door_catalog WHERE insulation_type IS NOT NULL ORDER BY insulation_type")).fetchall()]
    widths = [_float(r[0]) for r in db.execute(text("SELECT DISTINCT width FROM chi_door_catalog WHERE width IS NOT NULL ORDER BY width")).fetchall()]
    heights = [_float(r[0]) for r in db.execute(text("SELECT DISTINCT height FROM chi_door_catalog WHERE height IS NOT NULL ORDER BY height")).fetchall()]
    return {"door_types": types, "colors": colors, "brands": brands, "insulation_types": insulations, "widths": widths, "heights": heights}


# ── Parts & Openers Catalog ──────────────────────────────────────────────────

@router.get("/parts")
def list_parts(
    request: Request,
    search: str = Query(default="", max_length=200),
    part_type: str = Query(default=""),
    brand: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    where = ["is_active = true"]
    params: dict[str, Any] = {}

    if search:
        where.append("(name ILIKE :q OR sku ILIKE :q OR description ILIKE :q OR model ILIKE :q)")
        params["q"] = f"%{search}%"
    if part_type:
        where.append("part_type = :pt")
        params["pt"] = part_type
    if brand:
        where.append("brand ILIKE :brand")
        params["brand"] = f"%{brand}%"

    where_sql = " AND ".join(where)
    total = db.execute(text(f"SELECT count(*) FROM chi_parts_catalog WHERE {where_sql}"), params).scalar() or 0

    offset = (page - 1) * page_size
    rows = db.execute(text(f"""
        SELECT id, sku, name, part_type, brand, manufacturer, model,
               cost, sell_price, description, rail_length_ft, mount_type
        FROM chi_parts_catalog WHERE {where_sql}
        ORDER BY part_type, name
        LIMIT :lim OFFSET :off
    """), {**params, "lim": page_size, "off": offset}).mappings().all()

    items = [
        {
            "id": r["id"], "sku": r["sku"], "name": r["name"],
            "part_type": r["part_type"], "brand": r["brand"],
            "manufacturer": r["manufacturer"], "model": r["model"],
            "cost": _float(r["cost"]), "sell_price": _float(r["sell_price"]),
            "description": r["description"],
            "rail_length_ft": r["rail_length_ft"], "mount_type": r["mount_type"],
        }
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ── Pricing Calculator ───────────────────────────────────────────────────────

# Sprint 1.0.5 — singular cost→sell math via the canonical pricing engine.
# Replaces the prior hardcoded margin tiers (Doors/Openers fixed at 30/25/22%
# regardless of cost; Parts at hardcoded cost cutoffs) which conflicted with
# the editable per-tenant tier sets seeded into the DB at signup/pave.
#
# Endpoint contract preserved: same path, same query params, same response
# keys. Map: category (door|opener|part) → pricing_category (doors|openers|
# parts); customer_type (Retail|Contractor|Wholesale) → pricing_class
# (retail|contractor|wholesale).

_CATEGORY_MAP = {"door": "doors", "opener": "openers", "part": "parts"}


@router.get("/price")
def calculate_price(
    cost: float = Query(..., ge=0),
    category: str = Query(default="door", pattern="^(door|opener|part)$"),
    customer_type: str = Query(default="Retail", pattern="^(Retail|Contractor|Wholesale)$"),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Calculate sell price from cost using this tenant's editable margin tier sets."""
    from decimal import Decimal as _D

    from fastapi import HTTPException

    from gdx_dispatch.services.pricing_engine import (
        CustomerView,
        PricingConfigError,
        hydrate_settings_from_db,
        price_line,
    )

    pricing_category = _CATEGORY_MAP[category]
    pricing_class = customer_type.lower()  # type: ignore[assignment]

    try:
        settings = hydrate_settings_from_db(db)
        result = price_line(
            cost=_D(str(cost)),
            pricing_category=pricing_category,
            customer=CustomerView(pricing_class=pricing_class, margin_override_pct=None),  # type: ignore[arg-type]
            settings=settings,
        )
    except PricingConfigError as e:
        # Fail loud — never silently fall back. A misconfigured tenant must see
        # the error so admin can fix it.
        log.error("price_calc_config_error tenant=%s cost=%s cat=%s err=%s",
                  getattr(getattr(db, "info", None), "get", lambda *_: None)("tenant_id"),
                  cost, category, e)
        raise HTTPException(status_code=409, detail=f"Pricing config error: {e}") from e

    return {
        "cost": round(float(result.cost), 2),
        "sell_price": round(float(result.sell), 2),
        "profit": round(float(result.profit), 2),
        # margin_pct historically returned as percent integer; preserve that shape
        "margin_pct": round(float(result.margin_pct) * 100, 2),
        "category": category,
        "customer_type": customer_type,
        # New: surfaces which input won (tier|customer_override|wholesale_tier|line_override)
        "pricing_source": result.source,
    }
