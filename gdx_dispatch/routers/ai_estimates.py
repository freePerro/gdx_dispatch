"""AI Smart Estimates — suggest line items based on job description.

Uses local AI when configured, falls back to keyword-matched parts catalog.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.ai_provider import generate_sync
from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/ai/estimates",
    tags=["ai-estimates"],
    dependencies=[Depends(require_module("estimates"))],
)

# Common garage door parts/labor with typical pricing
_PARTS_CATALOG = [
    {"keywords": ["torsion", "spring"], "description": "Torsion Spring Replacement", "quantity": 1, "unit_price": 185.00, "category": "Springs"},
    {"keywords": ["extension", "spring"], "description": "Extension Spring Replacement (pair)", "quantity": 1, "unit_price": 150.00, "category": "Springs"},
    {"keywords": ["opener", "motor", "liftmaster", "chamberlain"], "description": "Belt Drive Opener Installation", "quantity": 1, "unit_price": 450.00, "category": "Openers"},
    {"keywords": ["chain", "opener"], "description": "Chain Drive Opener Installation", "quantity": 1, "unit_price": 350.00, "category": "Openers"},
    {"keywords": ["cable", "drum"], "description": "Lift Cable & Drum Replacement", "quantity": 2, "unit_price": 45.00, "category": "Parts"},
    {"keywords": ["roller", "nylon"], "description": "Nylon Roller Replacement (set of 12)", "quantity": 1, "unit_price": 120.00, "category": "Parts"},
    {"keywords": ["panel", "section", "dent", "damage"], "description": "Door Panel/Section Replacement", "quantity": 1, "unit_price": 350.00, "category": "Doors"},
    {"keywords": ["weatherstrip", "seal", "bottom"], "description": "Bottom Seal / Weatherstrip", "quantity": 1, "unit_price": 65.00, "category": "Parts"},
    {"keywords": ["sensor", "eye", "safety"], "description": "Safety Sensor Alignment/Replacement", "quantity": 1, "unit_price": 75.00, "category": "Parts"},
    {"keywords": ["track", "bent", "alignment"], "description": "Track Repair / Realignment", "quantity": 1, "unit_price": 125.00, "category": "Parts"},
    {"keywords": ["install", "new door", "full install"], "description": "New Garage Door Installation", "quantity": 1, "unit_price": 1200.00, "category": "Doors"},
    {"keywords": ["tune", "maintenance", "lube", "adjustment"], "description": "Tune-Up & Maintenance Service", "quantity": 1, "unit_price": 95.00, "category": "Labor"},
    {"keywords": ["emergency", "after hours"], "description": "Emergency / After-Hours Service Fee", "quantity": 1, "unit_price": 150.00, "category": "Labor"},
    {"keywords": ["labor", "service", "diagnostic"], "description": "Service Call / Diagnostic Fee", "quantity": 1, "unit_price": 85.00, "category": "Labor"},
    {"keywords": ["keypad", "remote", "wireless"], "description": "Wireless Keypad / Remote Programming", "quantity": 1, "unit_price": 55.00, "category": "Parts"},
    {"keywords": ["insulation", "insulated"], "description": "Door Insulation Kit", "quantity": 1, "unit_price": 200.00, "category": "Doors"},
    {"keywords": ["commercial", "high lift", "vertical"], "description": "Commercial Door Service", "quantity": 1, "unit_price": 500.00, "category": "Labor"},
]


class SuggestRequest(BaseModel):
    job_description: str = Field(min_length=3, max_length=2000)
    job_type: str = Field(default="Service", max_length=50)


def _keyword_suggest(description: str) -> list[dict]:
    desc_lower = description.lower()
    matches = []
    for item in _PARTS_CATALOG:
        if any(kw in desc_lower for kw in item["keywords"]):
            matches.append({
                "description": item["description"],
                "quantity": item["quantity"],
                "unit_price": item["unit_price"],
                "category": item["category"],
            })
    # Always include service call if nothing else matched
    if not matches:
        matches.append({
            "description": "Service Call / Diagnostic Fee",
            "quantity": 1,
            "unit_price": 85.00,
            "category": "Labor",
        })
    return matches


def _ai_suggest(description: str, job_type: str) -> list[dict] | None:
    prompt = (
        f"A customer needs garage door service. Job type: {job_type}. "
        f"Description: {description}\n\n"
        f"Suggest line items for an estimate. Return a JSON array where each item has: "
        f"description (string), quantity (number), unit_price (number), category (string: Doors/Openers/Springs/Parts/Labor/Other).\n"
        f"Use realistic garage door industry pricing. Return ONLY the JSON array, no other text."
    )
    result = generate_sync(
        prompt=prompt,
        system="You are a garage door service estimator. Return only valid JSON arrays.",
        max_tokens=1000,
    )
    if not result:
        return None
    try:
        # Strip markdown fences if present
        clean = result.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        items = json.loads(clean)
        if isinstance(items, list) and items:
            return [
                {
                    "description": str(i.get("description", ""))[:200],
                    "quantity": max(1, int(i.get("quantity", 1))),
                    "unit_price": max(0, float(i.get("unit_price", 0))),
                    "category": str(i.get("category", "Other"))[:50],
                }
                for i in items[:10]
            ]
    except (json.JSONDecodeError, ValueError, TypeError):
        log.warning("ai_estimate_parse_failed response=%s", result[:200])
    return None


@router.post("/suggest")
def suggest_estimate(
    body: SuggestRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", ""))

    # Try AI first, fall back to keyword matching
    lines = _ai_suggest(body.job_description, body.job_type)
    source = "ai"
    if not lines:
        lines = _keyword_suggest(body.job_description)
        source = "catalog"

    total = sum(l["quantity"] * l["unit_price"] for l in lines)

    try:
        log_audit_event_sync(
            db, tenant_id=tenant_id,
            user_id=str(user.get("sub") or "system"),
            action="ai_estimate_suggested",
            entity_type="estimate",
            entity_id="",
            details={"job_type": body.job_type, "source": source, "line_count": len(lines)},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("ai_estimate_audit_failed")

    return {"suggested_lines": lines, "estimated_total": total, "source": source}
