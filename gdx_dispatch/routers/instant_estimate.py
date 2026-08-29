"""Instant Estimator — AI-powered job description to estimate."""
from __future__ import annotations

import contextlib
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.part_pricing import sell_price_for_row
from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/ai",
    tags=["ai"],
    dependencies=[Depends(require_module("estimates"))],
)

LABOR_RATES = {
    "replacement": 350.0,
    "repair": 150.0,
    "installation": 450.0,
    "service": 125.0,
    "maintenance": 100.0,
}


class InstantEstimateIn(BaseModel):
    description: str = Field(min_length=5, max_length=2000)


def _parse_description(description: str) -> dict[str, Any]:
    """Extract structured data from job description using keyword matching.
    Falls back to keyword extraction if AI is unavailable.
    """
    desc_lower = description.lower()

    # Extract dimensions (e.g., "16x7", "9x8", "16 x 7")
    import re
    dims = re.findall(r'(\d{1,2})\s*[xX×]\s*(\d{1,2})', description)
    width = float(dims[0][0]) if dims else None
    height = float(dims[0][1]) if dims else None

    # Extract material
    material = None
    for mat in ["steel", "wood", "aluminum", "fiberglass", "vinyl"]:
        if mat in desc_lower:
            material = mat
            break

    # Extract insulation
    insulation = None
    for ins in ["insulated", "non-insulated", "polystyrene", "polyurethane"]:
        if ins in desc_lower:
            insulation = ins
            break

    # Extract job type
    job_type = "service"
    for jt in LABOR_RATES:
        if jt in desc_lower:
            job_type = jt
            break
    # Also check common synonyms
    if "replace" in desc_lower:
        job_type = "replacement"
    elif "install" in desc_lower or "new door" in desc_lower:
        job_type = "installation"
    elif "fix" in desc_lower or "broken" in desc_lower:
        job_type = "repair"

    # Extract part keywords
    part_keywords = []
    for kw in ["spring", "cable", "track", "roller", "hinge", "seal",
                "weatherstrip", "opener", "remote", "keypad", "sensor",
                "bracket", "drum", "bearing", "panel"]:
        if kw in desc_lower:
            part_keywords.append(kw)

    return {
        "width": width,
        "height": height,
        "material": material,
        "insulation": insulation,
        "job_type": job_type,
        "part_keywords": part_keywords,
        "raw": description,
    }


@router.post("/instant-estimate")
def instant_estimate(
    request: Request,
    payload: InstantEstimateIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Parse a job description and return auto-filled estimate line items."""
    tenant_id = str(company_id())
    parsed = _parse_description(payload.description)

    line_items: list[dict[str, Any]] = []
    suggested_door = None
    total = 0.0

    # 1. Search for matching door in CHI catalog
    try:
        conditions = []
        params: dict[str, Any] = {}

        if parsed["width"]:
            conditions.append("width = :width")
            params["width"] = parsed["width"]
        if parsed["height"]:
            conditions.append("height = :height")
            params["height"] = parsed["height"]
        if parsed["material"]:
            conditions.append("LOWER(section_material) LIKE :material")
            params["material"] = f"%{parsed['material']}%"

        if conditions:
            where = " AND ".join(conditions)
            # Sprint typed-catalogs follow-up — UNION CHI feed with tenant-
            # custom doors (custom_catalog_items + door_specs). Same WHERE
            # applied to both sides; spec column names mirror chi for the
            # custom branch so the result shape stays unified. Build the
            # ``ds.``-qualified clause explicitly — string-replacing on the
            # chi conditions would also rewrite the ``:width``/``:height`` bind
            # placeholders into ``:ds.width`` (parsed as a phantom ``:ds``).
            custom_conditions = []
            if parsed["width"]:
                custom_conditions.append("ds.width = :width")
            if parsed["height"]:
                custom_conditions.append("ds.height = :height")
            if parsed["material"]:
                custom_conditions.append("LOWER(ds.section_material) LIKE :material")
            custom_where = " AND ".join(custom_conditions)
            rows = db.execute(
                text(f"""
                    SELECT id, model_number, description, width, height,
                           insulation_type, section_material, sell_price, cost
                    FROM (
                        SELECT id, model_number, description, width, height,
                               insulation_type, section_material,
                               sell_price, cost
                        FROM chi_door_catalog
                        WHERE {where} AND is_active = true

                        UNION ALL

                        SELECT cci.id AS id, ds.model_number AS model_number,
                               COALESCE(cci.description, cci.name) AS description,
                               ds.width AS width, ds.height AS height,
                               ds.insulation_type AS insulation_type,
                               ds.section_material AS section_material,
                               cci.price AS sell_price, cci.cost AS cost
                        FROM custom_catalog_items cci
                        LEFT JOIN door_specs ds ON ds.catalog_item_id = cci.id
                        WHERE cci.product_class = 'door' AND cci.active = true
                              AND cci.deleted_at IS NULL AND {custom_where}
                    ) AS doors
                    LIMIT 5
                """),
                params,
            ).mappings().all()

            # Price through the margin engine, never off the cost column. None
            # means "we cannot price this" — omit the line rather than emit a
            # number the office would have to catch.
            door = dict(rows[0]) if rows else None
            sell = sell_price_for_row(
                db, price=door.get("sell_price"), cost=door.get("cost"),
                pricing_category="doors",
            ) if door else None
            if door is not None and sell is None:
                log.info(
                    "instant_estimate: no sell price for door %r — line omitted",
                    door.get("model_number"),
                )
            if door is not None and sell is not None:
                price = float(sell)
                suggested_door = {
                    "id": str(door["id"]),
                    "model": door.get("model_number", ""),
                    "description": door.get("description", ""),
                    "width": door.get("width"),
                    "height": door.get("height"),
                    "price": price,
                }
                line_items.append({
                    "name": f"{door.get('model_number', 'Door')} ({door.get('width')}x{door.get('height')})",
                    "qty": 1,
                    "unit_price": price,
                    "source": "chi_catalog",
                })
                total += price
    except Exception:
        log.exception("instant_estimate: door search failed")
        with contextlib.suppress(Exception):
            db.rollback()

    # 2. Search for matching parts
    for kw in parsed["part_keywords"]:
        try:
            parts = db.execute(
                text("SELECT id, name, part_type, sell_price, cost FROM chi_parts_catalog "
                     "WHERE (LOWER(name) LIKE :kw OR LOWER(part_type) LIKE :kw) "
                     "AND is_active = true LIMIT 3"),
                {"kw": f"%{kw}%"},
            ).mappings().all()

            for part in parts:
                # `sell_price or cost` quoted our DEALER COST to the customer at
                # zero margin: sell_price is NULL on every row of the CHI parts
                # feed, so the fallback fired every time. sell_price_for_row
                # applies the same two rules as the capture path — believe a
                # price only above cost, otherwise mark the cost up through the
                # tenant's margin tiers — and returns None when it cannot price.
                sell = sell_price_for_row(
                    db, price=part.get("sell_price"), cost=part.get("cost"),
                    pricing_category="parts",
                )
                if sell is None:
                    # Unpriceable is not $0 and not cost. Leave it out; the
                    # office prices it, which is what an absent line prompts.
                    log.info(
                        "instant_estimate: no sell price for %r — line omitted",
                        part.get("name"),
                    )
                    continue
                price = float(sell)
                qty = 2 if kw in ("spring", "cable", "roller") else 1
                line_items.append({
                    "name": part.get("name", kw),
                    "qty": qty,
                    "unit_price": price,
                    "source": "chi_parts",
                })
                total += price * qty
        except Exception:
            log.exception("instant_estimate: parts search failed for %s", kw)
            with contextlib.suppress(Exception):
                db.rollback()

    # 3. Add labor
    job_type = parsed["job_type"]
    labor_cost = LABOR_RATES.get(job_type, 125.0)
    line_items.append({
        "name": f"Labor — {job_type.capitalize()}",
        "qty": 1,
        "unit_price": labor_cost,
        "source": "labor",
    })
    total += labor_cost

    # Audit
    log_audit_event_sync(
        db, tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="create", entity_type="instant_estimate",
        entity_id=str(uuid4()),
        details={"description": payload.description[:200], "items": len(line_items), "total": total},
        request=request,
    )
    # log_audit_event_sync only add()s + flush()es, and get_db never commits —
    # so without this the row died with the request and this endpoint had no
    # trail at all. Zero audit rows read as "never used" when it actually meant
    # "never recorded". Same pattern as ai_estimates.py.
    db.commit()

    return {
        "line_items": line_items,
        "suggested_door": suggested_door,
        "total": round(total, 2),
        "parsed": {
            "job_type": parsed["job_type"],
            "dimensions": f"{parsed['width']}x{parsed['height']}" if parsed["width"] else None,
            "material": parsed["material"],
            "parts_found": len(parsed["part_keywords"]),
        },
    }
