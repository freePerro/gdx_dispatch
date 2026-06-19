"""gdx_dispatch/core/ai_quote.py — AI-powered quote/estimate generation and pricing intelligence.

Generates quote suggestions for garage door jobs using:
1. Keyword-based parts catalog matching (generate_quote, get_pricing_suggestions,
   analyze_pricing_health) — fast rule-based logic, no external AI API.
2. Historical job-template learning from completed QuoteTemplate records
   (generate_quote_suggestion, learn_from_job, price_benchmarks).

Routes: POST /api/ai/quote, POST /api/ai/pricing/suggest,
        GET /api/ai/pricing/health, POST /api/ai/quote-suggestion,
        POST /api/ai/learn-from-job, GET /api/ai/price-benchmarks.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import JSON, Column, DateTime, Integer, Numeric, String, select, text
from sqlalchemy.orm import Session
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parts catalog
# ---------------------------------------------------------------------------

PARTS_CATALOG: dict[str, dict[str, Any]] = {
    "torsion_spring": {
        "name": "Torsion Spring",
        "unit_price": 185.00,
        "unit_cost": 74.00,
        "unit": "each",
        "description": "Standard torsion spring for residential garage door",
    },
    "labor_spring": {
        "name": "Spring Replacement Labor",
        "unit_price": 95.00,
        "unit_cost": 38.00,
        "unit": "hour",
        "description": "Labor for torsion spring replacement",
    },
    "extension_spring": {
        "name": "Extension Spring",
        "unit_price": 75.00,
        "unit_cost": 30.00,
        "unit": "each",
        "description": "Extension spring for single-car garage door",
    },
    "labor_spring_ext": {
        "name": "Extension Spring Labor",
        "unit_price": 85.00,
        "unit_cost": 34.00,
        "unit": "hour",
        "description": "Labor for extension spring replacement",
    },
    "garage_door_opener": {
        "name": "Garage Door Opener",
        "unit_price": 320.00,
        "unit_cost": 128.00,
        "unit": "each",
        "description": "Belt or chain-drive opener unit",
    },
    "opener_install_labor": {
        "name": "Opener Installation Labor",
        "unit_price": 120.00,
        "unit_cost": 48.00,
        "unit": "hour",
        "description": "Labor for opener installation or replacement",
    },
    "cable_replacement": {
        "name": "Cable Replacement",
        "unit_price": 65.00,
        "unit_cost": 26.00,
        "unit": "each",
        "description": "Lift cable for garage door (pair)",
    },
    "cable_labor": {
        "name": "Cable Replacement Labor",
        "unit_price": 75.00,
        "unit_cost": 30.00,
        "unit": "hour",
        "description": "Labor for cable replacement",
    },
    "panel_replacement": {
        "name": "Door Panel Section",
        "unit_price": 245.00,
        "unit_cost": 98.00,
        "unit": "each",
        "description": "Replacement panel section for sectional door",
    },
    "panel_labor": {
        "name": "Panel Replacement Labor",
        "unit_price": 95.00,
        "unit_cost": 38.00,
        "unit": "hour",
        "description": "Labor for panel section replacement",
    },
    "weather_seal": {
        "name": "Weather Seal Strip",
        "unit_price": 45.00,
        "unit_cost": 18.00,
        "unit": "each",
        "description": "Side weather seal for garage door opening",
    },
    "bottom_seal": {
        "name": "Bottom Door Seal",
        "unit_price": 35.00,
        "unit_cost": 14.00,
        "unit": "each",
        "description": "Rubber bottom seal / astragal",
    },
    "roller_set": {
        "name": "Roller Set (12-pack)",
        "unit_price": 55.00,
        "unit_cost": 22.00,
        "unit": "each",
        "description": "Standard nylon rollers for one door",
    },
    "hinge_set": {
        "name": "Hinge Set",
        "unit_price": 48.00,
        "unit_cost": 19.00,
        "unit": "each",
        "description": "Replacement hinges for sectional door",
    },
    "keypad": {
        "name": "Wireless Keypad",
        "unit_price": 89.00,
        "unit_cost": 36.00,
        "unit": "each",
        "description": "Exterior wireless keypad entry",
    },
    "remote": {
        "name": "Remote Control",
        "unit_price": 45.00,
        "unit_cost": 18.00,
        "unit": "each",
        "description": "Garage door remote transmitter",
    },
    "service_call": {
        "name": "General Service Call",
        "unit_price": 95.00,
        "unit_cost": 38.00,
        "unit": "each",
        "description": "Diagnostic / general service visit",
    },
}

# ---------------------------------------------------------------------------
# Keyword rules
# ---------------------------------------------------------------------------

KEYWORD_RULES: list[dict[str, Any]] = [
    {
        "keywords": ["torsion spring", "torsion", "spring broke", "broken spring", "spring"],
        "parts": ["torsion_spring", "labor_spring"],
        "confidence": 0.85,
        "notes": "Torsion spring replacement detected",
    },
    {
        "keywords": ["extension spring", "side spring"],
        "parts": ["extension_spring", "labor_spring_ext"],
        "confidence": 0.82,
        "notes": "Extension spring replacement detected",
    },
    {
        "keywords": ["opener", "motor", "drive", "liftmaster", "chamberlain", "genie"],
        "parts": ["garage_door_opener", "opener_install_labor"],
        "confidence": 0.80,
        "notes": "Opener replacement/installation detected",
    },
    {
        "keywords": ["cable", "wire", "lift cable", "broken cable"],
        "parts": ["cable_replacement", "cable_labor"],
        "confidence": 0.88,
        "notes": "Cable replacement detected",
    },
    {
        "keywords": ["panel", "section", "dent", "damaged panel"],
        "parts": ["panel_replacement", "panel_labor"],
        "confidence": 0.75,
        "notes": "Panel/section replacement detected",
    },
    {
        "keywords": ["weather", "seal", "draft", "gap", "wind noise"],
        "parts": ["weather_seal", "bottom_seal"],
        "confidence": 0.70,
        "notes": "Weather sealing detected",
    },
    {
        "keywords": ["roller", "noisy", "grinding", "squeaking", "rattling"],
        "parts": ["roller_set"],
        "confidence": 0.65,
        "notes": "Roller replacement detected",
    },
    {
        "keywords": ["hinge"],
        "parts": ["hinge_set"],
        "confidence": 0.70,
        "notes": "Hinge replacement detected",
    },
    {
        "keywords": ["keypad", "entry pad", "code entry", "wireless entry"],
        "parts": ["keypad"],
        "confidence": 0.72,
        "notes": "Keypad installation detected",
    },
    {
        "keywords": ["remote", "clicker", "transmitter", "fob"],
        "parts": ["remote"],
        "confidence": 0.68,
        "notes": "Remote/transmitter needed",
    },
]


# ---------------------------------------------------------------------------
# Keyword-based quote generation
# ---------------------------------------------------------------------------


def generate_quote(
    job_description: str,
    equipment_type: str = "",
    issue_description: str = "",
) -> dict[str, Any]:
    """Generate a suggested quote from free-text job and issue descriptions.

    Matches keywords against KEYWORD_RULES, deduplicates parts (highest confidence
    wins), and returns line items with totals.
    """
    combined = " ".join([
        job_description.lower(),
        equipment_type.lower(),
        issue_description.lower(),
    ])

    # part_key → best confidence from any matching rule
    matched_parts: dict[str, float] = {}
    matched_notes: list[str] = []
    matched_count = 0

    for rule in KEYWORD_RULES:
        if any(kw in combined for kw in rule["keywords"]):
            matched_count += 1
            matched_notes.append(rule["notes"])
            for part_key in rule["parts"]:
                if rule["confidence"] > matched_parts.get(part_key, 0.0):
                    matched_parts[part_key] = rule["confidence"]

    # Fallback: generic service call when nothing matched
    if not matched_parts:
        matched_parts["service_call"] = 0.50
        matched_notes.append("No specific issue detected — defaulting to service call")

    line_items: list[dict[str, Any]] = []
    for part_key, _conf in matched_parts.items():
        catalog = PARTS_CATALOG.get(part_key)
        if not catalog:
            continue
        qty = 1
        unit_price = float(catalog["unit_price"])
        line_items.append({
            "part_key": part_key,
            "name": catalog["name"],
            "qty": qty,
            "unit": catalog["unit"],
            "unit_price": unit_price,
            "subtotal": round(qty * unit_price, 2),
        })

    total = round(sum(item["subtotal"] for item in line_items), 2)
    confidence = round(
        sum(matched_parts.values()) / len(matched_parts) if matched_parts else 0.50,
        3,
    )

    return {
        "line_items": line_items,
        "total": total,
        "confidence": confidence,
        "notes": matched_notes,
        "matched_rules": matched_count,
    }


def get_pricing_suggestions(part_name: str, db: Session) -> dict[str, Any]:
    """Return pricing suggestions for a named part or service.

    Queries historical invoice totals first; falls back to catalog lookup, then
    sensible defaults.
    """
    prices: list[float] = []
    source = "default"

    try:
        from gdx_dispatch.models.tenant_models import Invoice
        rows = db.execute(
            select(Invoice.total).where(
                Invoice.deleted_at.is_(None),
                Invoice.status.in_(["paid", "sent"]),
            )
        ).scalars().all()
        prices = [float(r) for r in rows if r and float(r) > 0]
        if prices:
            source = "historical"
    except Exception as exc:
        logger.warning("Could not query invoices for pricing suggestion: %s", exc)

    # Catalog match by name substring
    catalog_price: float | None = None
    for _key, entry in PARTS_CATALOG.items():
        name_lower = entry["name"].lower()
        if part_name.lower() in name_lower or name_lower in part_name.lower():
            catalog_price = float(entry["unit_price"])
            if not prices:
                source = "catalog"
            break

    if prices:
        suggested = round(sum(prices) / len(prices), 2)
        min_price = round(min(prices), 2)
        max_price = round(max(prices), 2)
    elif catalog_price is not None:
        suggested = catalog_price
        min_price = round(catalog_price * 0.80, 2)
        max_price = round(catalog_price * 1.30, 2)
    else:
        suggested = 85.00
        min_price = 65.00
        max_price = 120.00

    return {
        "part_name": part_name,
        "suggested_price": suggested,
        "min_price": min_price,
        "max_price": max_price,
        "market_avg": round(suggested * 0.95, 2),
        "sample_count": len(prices),
        "source": source,
    }


def analyze_pricing_health(tenant_id: str, db: Session) -> dict[str, Any]:
    """Analyse pricing health for a tenant over the last 90 days.

    Returns avg_margin, below/above market item names, a health_score, and
    actionable recommendations.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    total_invoices = 0
    margins: list[float] = []
    below_market: list[str] = []
    above_market: list[str] = []

    try:
        from gdx_dispatch.models.tenant_models import Invoice
        invoices = db.execute(
            select(Invoice).where(
                Invoice.deleted_at.is_(None),
                Invoice.created_at >= cutoff,
            )
        ).scalars().all()
        total_invoices = len(invoices)
        for inv in invoices:
            total_val = float(inv.total or 0)
            subtotal_val = float(inv.subtotal or 0)
            if total_val > 0 and subtotal_val > 0:
                est_cost = subtotal_val * 0.40
                margins.append((total_val - est_cost) / total_val)
    except Exception as exc:
        logger.warning("Could not query invoices for health analysis: %s", exc)

    avg_margin = round(sum(margins) / len(margins), 4) if margins else 0.35

    for _key, entry in PARTS_CATALOG.items():
        price = float(entry["unit_price"])
        cost = float(entry["unit_cost"])
        if price < cost * 1.5:
            below_market.append(entry["name"])
        if price > cost * 4.0:
            above_market.append(entry["name"])

    recommendations: list[str] = []
    if avg_margin >= 0.30:
        health_score = "good"
        recommendations.append("Pricing is healthy. Consider gradual increases on high-demand items.")
    elif avg_margin >= 0.15:
        health_score = "fair"
        recommendations.append("Margins are moderate. Review parts pricing on spring and opener jobs.")
        recommendations.append("Consider raising spring labor rate to $110/hr to cover overhead.")
    else:
        health_score = "poor"
        recommendations.append("Margins are low. Audit parts costs and raise prices across all categories.")
        recommendations.append("Review supplier pricing and consider bulk purchasing agreements.")
        recommendations.append("Raise minimum service call fee to at least $95.")

    return {
        "tenant_id": tenant_id,
        "avg_margin": avg_margin,
        "below_market_items": below_market,
        "above_market_items": above_market,
        "total_invoices_analyzed": total_invoices,
        "health_score": health_score,
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------

class QuoteTemplate(TenantBase):
    __tablename__ = "quote_templates"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(String(100), index=True, nullable=False)
    job_type = Column(String(100), nullable=False)
    typical_parts = Column(JSON, nullable=True)
    typical_labor_hours = Column(Numeric(6, 2), nullable=True)
    typical_price_low = Column(Numeric(12, 2), nullable=True)
    typical_price_high = Column(Numeric(12, 2), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    use_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class PartEstimate(BaseModel):
    name: str
    estimated_cost: float
    typical_qty: int


class QuoteSuggestion(BaseModel):
    job_type: str
    parts: list[PartEstimate]
    labor_hours: float
    labor_rate: float
    subtotal: float
    markup_pct: float
    total_price: float
    price_range: dict
    confidence: Literal["high", "medium", "low"]
    similar_jobs_count: int


class QuoteRequest(BaseModel):
    job_type: str
    zip_code: str | None = None


class LearnFromJobRequest(BaseModel):
    job_type: str
    parts_cost: float
    labor_hours: float
    final_price: float


# ---------------------------------------------------------------------------
# Industry fallback defaults
# ---------------------------------------------------------------------------

_FALLBACKS: dict[str, dict] = {
    "spring": {
        "labor_hours": 1.5,
        "labor_rate": 120.0,
        "parts": [{"name": "Torsion Spring", "estimated_cost": 85.0, "typical_qty": 1}],
        "markup_pct": 0.35,
    },
    "opener": {
        "labor_hours": 2.0,
        "labor_rate": 120.0,
        "parts": [{"name": "LiftMaster Opener", "estimated_cost": 220.0, "typical_qty": 1}],
        "markup_pct": 0.30,
    },
    "panel": {
        "labor_hours": 3.0,
        "labor_rate": 120.0,
        "parts": [{"name": "Door Panel", "estimated_cost": 180.0, "typical_qty": 1}],
        "markup_pct": 0.25,
    },
}

_DEFAULT_FALLBACK = {
    "labor_hours": 1.0,
    "labor_rate": 120.0,
    "parts": [],
    "markup_pct": 0.30,
}

_URBAN_ZIPS = ("9", "1")  # CA/WA/OR (9xx) and NY/NJ (1xx) — 15% premium


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _get_fallback(job_type: str) -> dict:
    jt = job_type.lower()
    for keyword, defaults in _FALLBACKS.items():
        if keyword in jt:
            return defaults
    return _DEFAULT_FALLBACK


def generate_quote_suggestion(
    job_type: str,
    zip_code: str | None,
    tenant_id: str,
    db: Session,
) -> QuoteSuggestion:
    """Generate a quote suggestion using job history or industry fallbacks."""
    records = (
        db.query(QuoteTemplate)
        .filter(
            QuoteTemplate.tenant_id == tenant_id,
            QuoteTemplate.job_type == job_type,
            QuoteTemplate.deleted_at.is_(None),
        )
        .order_by(QuoteTemplate.last_used_at.desc().nullslast())
        .limit(20)
        .all()
    )

    count = len(records)

    if count >= 10:
        confidence: Literal["high", "medium", "low"] = "high"
    elif count >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    if count >= 3:
        # Compute averages from history
        avg_labor = float(
            sum(float(r.typical_labor_hours or 0) for r in records) / count
        )
        avg_price_low = float(
            sum(float(r.typical_price_low or 0) for r in records) / count
        )
        avg_price_high = float(
            sum(float(r.typical_price_high or 0) for r in records) / count
        )
        # Aggregate parts: collect from all records, deduplicate by name
        parts_map: dict[str, dict] = {}
        for rec in records:
            for p in (rec.typical_parts or []):
                name = p.get("name", "")
                if name not in parts_map:
                    parts_map[name] = {
                        "name": name,
                        "costs": [],
                        "qtys": [],
                    }
                parts_map[name]["costs"].append(float(p.get("estimated_cost", 0)))
                parts_map[name]["qtys"].append(int(p.get("typical_qty", 1)))

        parts: list[PartEstimate] = [
            PartEstimate(
                name=n,
                estimated_cost=sum(v["costs"]) / len(v["costs"]),
                typical_qty=max(set(v["qtys"]), key=v["qtys"].count),
            )
            for n, v in parts_map.items()
        ]

        labor_rate = 120.0
        parts_total = sum(p.estimated_cost * p.typical_qty for p in parts)
        subtotal = parts_total + (avg_labor * labor_rate)
        markup_pct = 0.30

        # Derive markup from avg prices if available
        if avg_price_low > 0 and subtotal > 0:
            avg_final = (avg_price_low + avg_price_high) / 2
            markup_pct = max(0.0, (avg_final - subtotal) / subtotal) if subtotal > 0 else 0.30

        total_price = subtotal * (1.0 + markup_pct)
    else:
        # Use fallback defaults
        fb = _get_fallback(job_type)
        avg_labor = fb["labor_hours"]
        labor_rate = fb["labor_rate"]
        markup_pct = fb["markup_pct"]
        parts = [
            PartEstimate(
                name=p["name"],
                estimated_cost=p["estimated_cost"],
                typical_qty=p["typical_qty"],
            )
            for p in fb["parts"]
        ]
        parts_total = sum(p.estimated_cost * p.typical_qty for p in parts)
        subtotal = parts_total + (avg_labor * labor_rate)
        total_price = subtotal * (1.0 + markup_pct)

    # Urban ZIP premium
    if zip_code and str(zip_code).startswith(_URBAN_ZIPS):
        total_price *= 1.15
        subtotal *= 1.15

    total_price = round(total_price, 2)
    subtotal = round(subtotal, 2)

    return QuoteSuggestion(
        job_type=job_type,
        parts=parts,
        labor_hours=round(avg_labor, 2) if count >= 3 else float(avg_labor),
        labor_rate=120.0,
        subtotal=subtotal,
        markup_pct=round(markup_pct, 4),
        total_price=total_price,
        price_range={"low": round(total_price * 0.85, 2), "high": round(total_price * 1.15, 2)},
        confidence=confidence,
        similar_jobs_count=count,
    )


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/ai", tags=["ai-quotes"])

_auth_dep = Depends(require_role("admin", "owner", "tech"))


@router.post("/quote-suggestion", response_model=QuoteSuggestion)
def quote_suggestion(
    body: QuoteRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> QuoteSuggestion:
    """Generate an AI quote suggestion for a job type."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])
    return generate_quote_suggestion(body.job_type, body.zip_code, tenant_id, db)


@router.post("/learn-from-job")
def learn_from_job(
    body: LearnFromJobRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict:
    """Update QuoteTemplate from a completed job (rolling average)."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])
    now = datetime.now(timezone.utc)

    template = (
        db.query(QuoteTemplate)
        .filter(
            QuoteTemplate.tenant_id == tenant_id,
            QuoteTemplate.job_type == body.job_type,
            QuoteTemplate.deleted_at.is_(None),
        )
        .first()
    )

    if template is None:
        template = QuoteTemplate(
            tenant_id=tenant_id,
            job_type=body.job_type,
            typical_parts=[],
            typical_labor_hours=body.labor_hours,
            typical_price_low=body.final_price * 0.9,
            typical_price_high=body.final_price * 1.1,
            last_used_at=now,
            use_count=1,
            created_at=now,
        )
        db.add(template)
    else:
        n = template.use_count or 0
        # Rolling average
        cur_labor = float(template.typical_labor_hours or 0)
        cur_low = float(template.typical_price_low or 0)
        cur_high = float(template.typical_price_high or 0)

        template.typical_labor_hours = round((cur_labor * n + body.labor_hours) / (n + 1), 2)
        template.typical_price_low = round(
            (cur_low * n + body.final_price * 0.9) / (n + 1), 2
        )
        template.typical_price_high = round(
            (cur_high * n + body.final_price * 1.1) / (n + 1), 2
        )
        template.use_count = n + 1
        template.last_used_at = now

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save template: {exc}") from exc

    return {"status": "ok", "job_type": body.job_type, "use_count": template.use_count}


@router.get("/price-benchmarks")
def price_benchmarks(
    request: Request,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> list:
    """Return average prices per job type for this tenant."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])

    templates = (
        db.query(QuoteTemplate)
        .filter(
            QuoteTemplate.tenant_id == tenant_id,
            QuoteTemplate.deleted_at.is_(None),
        )
        .order_by(QuoteTemplate.job_type)
        .all()
    )

    return [
        {
            "job_type": t.job_type,
            "typical_price_low": float(t.typical_price_low) if t.typical_price_low is not None else None,
            "typical_price_high": float(t.typical_price_high) if t.typical_price_high is not None else None,
            "typical_labor_hours": float(t.typical_labor_hours) if t.typical_labor_hours is not None else None,
            "use_count": t.use_count,
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
        }
        for t in templates
    ]


# ---------------------------------------------------------------------------
# Keyword-based quote + pricing intelligence routes
# ---------------------------------------------------------------------------

class KeywordQuoteRequest(BaseModel):
    job_description: str
    equipment_type: str = ""
    issue_description: str = ""


class PricingSuggestRequest(BaseModel):
    part_name: str


@router.post("/quote")
def api_generate_quote(payload: KeywordQuoteRequest) -> dict[str, Any]:
    """Generate a keyword-based parts quote from job/issue description."""
    return generate_quote(
        job_description=payload.job_description,
        equipment_type=payload.equipment_type,
        issue_description=payload.issue_description,
    )


TenantDB = Annotated[Session, Depends(get_db)]


@router.post("/pricing/suggest")
def api_pricing_suggest(
    payload: PricingSuggestRequest,
    db: TenantDB,
) -> dict[str, Any]:
    """Suggest a price range for a named part or service."""
    return get_pricing_suggestions(part_name=payload.part_name, db=db)


@router.get("/pricing/health")
def api_pricing_health(
    tenant_id: str,
    db: TenantDB,
) -> dict[str, Any]:
    """Return pricing health analysis for the given tenant."""
    return analyze_pricing_health(tenant_id=tenant_id, db=db)


# ---------------------------------------------------------------------------
# AI Quote Generate / History / Feedback (items 61-63)
# ---------------------------------------------------------------------------

class QuoteGenerateRequest(BaseModel):
    job_type: str
    customer_id: str = ""
    notes: str = ""
    zip_code: str = ""


class QuoteGenerateResponse(BaseModel):
    id: str
    job_type: str
    suggested_parts: list[dict[str, Any]]
    estimated_total_low: float
    estimated_total_high: float
    labor_hours: float
    created_at: str


class QuoteFeedbackRequest(BaseModel):
    quote_id: str
    accepted: bool
    final_price: float | None = None
    notes: str = ""


def _ensure_ai_quote_log(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS ai_quote_log (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                job_type TEXT NOT NULL,
                customer_id TEXT,
                input_notes TEXT,
                generated_quote JSONB,
                accepted BOOLEAN,
                final_price NUMERIC(12, 2),
                feedback_notes TEXT,
                feedback_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    db.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_aql_tenant ON ai_quote_log (tenant_id, created_at DESC)"
        )
    )
    db.commit()


@router.post("/quote-generate", response_model=QuoteGenerateResponse)
def api_quote_generate(
    body: QuoteGenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> QuoteGenerateResponse:
    """Generate a full AI-powered quote combining template data and parts catalog."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])

    _ensure_ai_quote_log(db)

    # Get suggestion from historical templates
    suggestion = generate_quote_suggestion(body.job_type, body.zip_code, tenant_id, db)

    # Get parts from keyword matching
    parts_result = generate_quote(
        job_description=body.notes or body.job_type,
        equipment_type="",
        issue_description=body.notes,
    )

    # Combine
    suggested_parts = parts_result.get("line_items", [])
    parts_total = sum(p.get("total", 0) for p in suggested_parts)
    price_range = suggestion.price_range or {}
    low = price_range.get("low") or suggestion.total_price * 0.9 if suggestion.total_price else parts_total * 0.9
    high = price_range.get("high") or suggestion.total_price * 1.1 if suggestion.total_price else parts_total * 1.1
    labor = suggestion.labor_hours if suggestion.labor_hours else 1.5

    quote_id = str(uuid4())
    now = datetime.now(timezone.utc)
    quote_data = {
        "suggested_parts": suggested_parts,
        "estimated_total_low": float(low),
        "estimated_total_high": float(high),
        "labor_hours": float(labor),
    }

    import json
    db.execute(
        text(
            """
            INSERT INTO ai_quote_log
                (id, tenant_id, job_type, customer_id, input_notes, generated_quote, created_at)
            VALUES
                (:id, :tid, :jt, :cid, :notes, :gq, :ca)
            """
        ),
        {
            "id": quote_id,
            "tid": tenant_id,
            "jt": body.job_type,
            "cid": body.customer_id or None,
            "notes": body.notes or None,
            "gq": json.dumps(quote_data),
            "ca": now,
        },
    )
    db.commit()

    logger.info(
        "ai_quote_generated",
        extra={"tenant_id": tenant_id, "quote_id": quote_id, "job_type": body.job_type},
    )

    return QuoteGenerateResponse(
        id=quote_id,
        job_type=body.job_type,
        suggested_parts=suggested_parts,
        estimated_total_low=float(low),
        estimated_total_high=float(high),
        labor_hours=float(labor),
        created_at=now.isoformat(),
    )


@router.get("/quote-history")
def api_quote_history(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    job_type: str | None = None,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict[str, Any]:
    """Fetch paginated AI quote history for this tenant."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])

    _ensure_ai_quote_log(db)

    params: dict[str, Any] = {"tid": tenant_id, "limit": per_page, "offset": (page - 1) * per_page}
    where = "WHERE tenant_id = :tid"
    if job_type:
        where += " AND job_type = :jt"
        params["jt"] = job_type

    count_row = db.execute(
        text(f"SELECT COUNT(*) FROM ai_quote_log {where}"), params
    ).scalar()

    rows = db.execute(
        text(
            f"""
            SELECT id, job_type, customer_id, input_notes, generated_quote,
                   accepted, final_price, feedback_notes, created_at
            FROM ai_quote_log {where}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    return {
        "items": [dict(r) for r in rows],
        "page": page,
        "per_page": per_page,
        "total": count_row or 0,
    }


@router.post("/quote-feedback")
def api_quote_feedback(
    body: QuoteFeedbackRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict[str, str]:
    """Record feedback on a generated AI quote."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])

    _ensure_ai_quote_log(db)

    result = db.execute(
        text(
            """
            UPDATE ai_quote_log
            SET accepted = :accepted,
                final_price = :price,
                feedback_notes = :notes,
                feedback_at = :now
            WHERE id = :qid AND tenant_id = :tid
            RETURNING job_type
            """
        ),
        {
            "accepted": body.accepted,
            "price": body.final_price,
            "notes": body.notes or None,
            "now": datetime.now(timezone.utc),
            "qid": body.quote_id,
            "tid": tenant_id,
        },
    ).mappings().first()

    if not result:
        raise HTTPException(status_code=404, detail="Quote not found")

    db.commit()

    # If accepted, feed back into the learning system
    if body.accepted and body.final_price:
        try:
            job_type = result["job_type"]
            template = (
                db.query(QuoteTemplate)
                .filter(
                    QuoteTemplate.tenant_id == tenant_id,
                    QuoteTemplate.job_type == job_type,
                    QuoteTemplate.deleted_at.is_(None),
                )
                .first()
            )
            if template:
                n = template.use_count or 0
                cur_low = float(template.typical_price_low or 0)
                cur_high = float(template.typical_price_high or 0)
                template.typical_price_low = round(
                    (cur_low * n + body.final_price * 0.9) / (n + 1), 2
                )
                template.typical_price_high = round(
                    (cur_high * n + body.final_price * 1.1) / (n + 1), 2
                )
                template.use_count = n + 1
                template.last_used_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            logger.exception("quote_feedback_learn_failed", extra={"quote_id": body.quote_id})

    logger.info(
        "ai_quote_feedback",
        extra={"tenant_id": tenant_id, "quote_id": body.quote_id, "accepted": body.accepted},
    )

    return {"status": "ok", "quote_id": body.quote_id}
