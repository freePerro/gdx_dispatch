"""Catalog policy API + AI-suggestion endpoint."""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/catalog-policy", tags=["catalog-policy"])


_COLS = (
    "catalog_require_description",
    "catalog_render_name_when_desc_empty",
    "catalog_ai_suggest_descriptions",
    "catalog_block_zero_price_on_invoice",
    "catalog_warn_zero_price_on_invoice",
    "catalog_block_zero_price_on_save",
    "catalog_auto_inactivate_zero_price",
)


class PolicyPayload(BaseModel):
    catalog_require_description: bool = False
    catalog_render_name_when_desc_empty: bool = True
    catalog_ai_suggest_descriptions: bool = False
    catalog_block_zero_price_on_invoice: bool = False
    catalog_warn_zero_price_on_invoice: bool = True
    catalog_block_zero_price_on_save: bool = False
    catalog_auto_inactivate_zero_price: bool = False


class SuggestPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    sku: str | None = None
    category: str | None = None


def _tenant_uuid(request: Request) -> UUID:
    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        return UUID(tid)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid tenant context") from exc


def _read(db: Session, tid: UUID) -> dict[str, Any]:
    cols = ", ".join(_COLS)
    row = db.execute(
        text(f"SELECT {cols} FROM tenant_settings WHERE tenant_id = :tid"),
        {"tid": str(tid)},
    ).first()
    if row is None:
        db.execute(
            text("INSERT INTO tenant_settings (tenant_id) VALUES (:tid) ON CONFLICT (tenant_id) DO NOTHING"),
            {"tid": str(tid)},
        )
        db.commit()
        row = db.execute(
            text(f"SELECT {cols} FROM tenant_settings WHERE tenant_id = :tid"),
            {"tid": str(tid)},
        ).first()
    return {col: bool(row[i]) for i, col in enumerate(_COLS)}


@router.get("", response_model=None)
def get_policy_endpoint(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _ = user
    return _read(db, _tenant_uuid(request))


@router.patch("", response_model=None)
def update_policy(
    payload: PolicyPayload,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin or owner required")
    tid = _tenant_uuid(request)
    set_clause = ", ".join(f"{c} = :{c}" for c in _COLS)
    db.execute(
        text(
            f"INSERT INTO tenant_settings (tenant_id, {', '.join(_COLS)}) "
            f"VALUES (:tid, {', '.join(':' + c for c in _COLS)}) "
            f"ON CONFLICT (tenant_id) DO UPDATE SET {set_clause}"
        ),
        {"tid": str(tid), **{c: getattr(payload, c) for c in _COLS}},
    )
    db.commit()
    return _read(db, tid)


@router.post("/suggest-description", response_model=None)
def suggest_description(
    payload: SuggestPayload,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Generate a customer-friendly description for a catalog item.

    Calls the existing per-tenant AI assistant. Returns {"description": "..."}
    or 503 if the AI module is not configured for this tenant. Idempotent —
    no DB writes; the caller decides whether to keep the suggestion."""
    _ = user
    try:
        from gdx_dispatch.routers.ai import ask as ai_ask  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="AI module not available") from exc

    prompt = (
        "Write a concise customer-friendly description (1-2 sentences, no marketing fluff) "
        "for this garage door industry catalog item. Output the description only.\n\n"
        f"Name: {payload.name}\n"
        f"SKU: {payload.sku or 'n/a'}\n"
        f"Category: {payload.category or 'n/a'}"
    )
    try:
        # ai_ask signature varies; pass-through. Best-effort.
        result = ai_ask(  # type: ignore[misc]
            request=request,
            payload={"prompt": prompt, "max_tokens": 120},
        )
        text_out = (
            result.get("text") if isinstance(result, dict) else str(result or "")
        ) or ""
        return {"description": text_out.strip()[:1000]}
    except Exception as exc:  # noqa: BLE001
        log.exception("catalog_ai_suggest_failed")
        raise HTTPException(status_code=502, detail=f"AI suggestion failed: {exc}") from exc
