"""PDF Template Editor — customizable PDF layouts per tenant."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import PdfTemplate
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/pdf-templates",
    tags=["pdf-templates"],
    dependencies=[Depends(require_module("jobs"))],
)

TEMPLATE_TYPES = [
    "estimate", "invoice", "work_order",
    "install_sheet", "safety_checklist", "purchase_order",
]

DEFAULT_BLOCKS = [
    {"id": "logo", "type": "logo", "order": 1, "visible": True, "styles": {}, "settings": {}},
    {"id": "company_info", "type": "company_info", "order": 2, "visible": True, "styles": {}, "settings": {}},
    {"id": "customer_info", "type": "customer_info", "order": 3, "visible": True, "styles": {}, "settings": {}},
    {"id": "line_items", "type": "line_items", "order": 4, "visible": True, "styles": {}, "settings": {"show_unit_price": True, "show_tax": True}},
    {"id": "totals", "type": "totals", "order": 5, "visible": True, "styles": {}, "settings": {}},
    {"id": "notes", "type": "notes", "order": 6, "visible": True, "styles": {}, "settings": {}},
    {"id": "terms", "type": "terms", "order": 7, "visible": True, "styles": {}, "settings": {}},
    {"id": "signature", "type": "signature", "order": 8, "visible": False, "styles": {}, "settings": {}},
]


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


class BlockConfig(BaseModel):
    id: str
    type: str
    order: int
    visible: bool = True
    styles: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class TemplateConfigIn(BaseModel):
    brand_color: str = Field(default="#0057a8", max_length=20)
    font_family: str = Field(default="Helvetica", max_length=50)
    header_content: str | None = Field(default=None, max_length=2000)
    footer_content: str | None = Field(default=None, max_length=2000)
    blocks: list[BlockConfig] = Field(default_factory=list, max_length=20)
    logo_url: str | None = Field(default=None, max_length=2000)


def _serialize(tmpl: PdfTemplate) -> dict[str, Any]:
    blocks_raw = tmpl.blocks
    if isinstance(blocks_raw, str):
        try:
            blocks = json.loads(blocks_raw)
        except (json.JSONDecodeError, TypeError):
            logging.getLogger(__name__).exception("_serialize caught exception")
            blocks = DEFAULT_BLOCKS
    else:
        blocks = blocks_raw or DEFAULT_BLOCKS

    return {
        "id": str(tmpl.id),
        "template_type": tmpl.template_type,
        "brand_color": tmpl.brand_color or "#0057a8",
        "font_family": tmpl.font_family or "Helvetica",
        "header_content": tmpl.header_content,
        "footer_content": tmpl.footer_content,
        "blocks": blocks,
        "logo_url": tmpl.logo_url,
        "updated_at": str(tmpl.updated_at) if tmpl.updated_at else None,
    }


@router.get("")
def list_templates(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List all PDF template configs for the tenant."""
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    templates = db.execute(
        select(PdfTemplate)
        .order_by(PdfTemplate.template_type)
    ).scalars().all()

    existing = {t.template_type: _serialize(t) for t in templates}

    # Return all types, using defaults for unconfigured ones
    result = []
    for tt in TEMPLATE_TYPES:
        if tt in existing:
            result.append(existing[tt])
        else:
            result.append({
                "id": None,
                "template_type": tt,
                "brand_color": "#0057a8",
                "font_family": "Helvetica",
                "header_content": None,
                "footer_content": None,
                "blocks": DEFAULT_BLOCKS,
                "logo_url": None,
                "updated_at": None,
            })
    return result


@router.get("/{template_type}")
def get_template(
    template_type: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Get template config for a specific type."""
    if template_type not in TEMPLATE_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type. Must be one of: {TEMPLATE_TYPES}")
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    tmpl = db.execute(
        select(PdfTemplate).where(
            PdfTemplate.template_type == template_type,
        )
    ).scalar_one_or_none()

    if tmpl:
        return _serialize(tmpl)

    return {
        "id": None,
        "template_type": template_type,
        "brand_color": "#0057a8",
        "font_family": "Helvetica",
        "header_content": None,
        "footer_content": None,
        "blocks": DEFAULT_BLOCKS,
        "logo_url": None,
        "updated_at": None,
    }


@router.put("/{template_type}")
def save_template(
    template_type: str,
    request: Request,
    payload: TemplateConfigIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Save/update template config."""
    if template_type not in TEMPLATE_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type. Must be one of: {TEMPLATE_TYPES}")
    tid = _tid(request)
    uid = _uid(user)
    now = datetime.now(timezone.utc)
    blocks_json = json.dumps([b.model_dump() for b in payload.blocks])

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    existing = db.execute(
        select(PdfTemplate).where(
            PdfTemplate.template_type == template_type,
        )
    ).scalar_one_or_none()

    if existing:
        existing.brand_color = payload.brand_color
        existing.font_family = payload.font_family
        existing.header_content = payload.header_content
        existing.footer_content = payload.footer_content
        existing.blocks = blocks_json
        existing.logo_url = payload.logo_url
        existing.updated_at = now
        template_id = str(existing.id)
    else:
        template_id = str(uuid4())
        new_tmpl = PdfTemplate(
            id=template_id,
            company_id=tid,
            template_type=template_type,
            brand_color=payload.brand_color,
            font_family=payload.font_family,
            header_content=payload.header_content,
            footer_content=payload.footer_content,
            blocks=blocks_json,
            logo_url=payload.logo_url,
            created_at=now,
            updated_at=now,
        )
        db.add(new_tmpl)
    db.commit()

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="update",
        entity_type="pdf_template", entity_id=template_id,
        details={"template_type": template_type, "blocks": len(payload.blocks)},
        request=request,
    )

    return {"status": "saved", "id": template_id, "template_type": template_type}


@router.get("/types/available")
def available_types(
    user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, str]]:
    """Return list of available template types."""
    labels = {
        "estimate": "Estimate",
        "invoice": "Invoice",
        "work_order": "Work Order",
        "install_sheet": "Install Sheet",
        "safety_checklist": "Safety Checklist",
        "purchase_order": "Purchase Order",
    }
    return [{"key": k, "label": labels.get(k, k)} for k in TEMPLATE_TYPES]
