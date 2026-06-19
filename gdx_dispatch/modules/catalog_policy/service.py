"""Catalog policy resolver — read tenant flags, enforce on writes."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text

from gdx_dispatch.core.database import SessionLocal, tenant_context

log = logging.getLogger(__name__)


@dataclass
class CatalogPolicy:
    require_description: bool = False
    render_name_when_desc_empty: bool = True
    ai_suggest_descriptions: bool = False
    # F-75 — zero-pricing toggles
    block_zero_price_on_invoice: bool = False
    warn_zero_price_on_invoice: bool = True
    block_zero_price_on_save: bool = False
    auto_inactivate_zero_price: bool = False


def get_policy(tenant_id: str) -> CatalogPolicy:
    """Read the per-tenant flags. Best-effort — defaults on any read error."""
    try:
        with tenant_context(tenant_id), SessionLocal() as cdb:
            row = cdb.execute(
                text(
                    "SELECT catalog_require_description, "
                    "       catalog_render_name_when_desc_empty, "
                    "       catalog_ai_suggest_descriptions, "
                    "       catalog_block_zero_price_on_invoice, "
                    "       catalog_warn_zero_price_on_invoice, "
                    "       catalog_block_zero_price_on_save, "
                    "       catalog_auto_inactivate_zero_price "
                    "FROM tenant_settings WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            ).first()
            if row is None:
                return CatalogPolicy()
            return CatalogPolicy(
                require_description=bool(row[0]),
                render_name_when_desc_empty=bool(row[1]),
                ai_suggest_descriptions=bool(row[2]),
                block_zero_price_on_invoice=bool(row[3]),
                warn_zero_price_on_invoice=bool(row[4]),
                block_zero_price_on_save=bool(row[5]),
                auto_inactivate_zero_price=bool(row[6]),
            )
    except Exception:
        log.exception("catalog_policy_read_failed", extra={"tenant_id": tenant_id})
        return CatalogPolicy()


def enforce_save_pricing(tenant_id: str, *, price: float | int | None) -> bool:
    """For F-75 (c) + (d) — applied during catalog item create/update.

    Returns the effective `active` value the caller should persist.
    Raises 422 if (c) block_zero_price_on_save is on AND price <= 0.
    If (d) auto_inactivate_zero_price is on AND price <= 0, returns False
    (caller writes active=False)."""
    p = float(price or 0)
    pol = get_policy(tenant_id)
    if pol.block_zero_price_on_save and p <= 0:
        raise HTTPException(
            status_code=422,
            detail="price must be > 0 (your tenant has 'Block zero-price catalog saves' enabled).",
        )
    if pol.auto_inactivate_zero_price and p <= 0:
        return False
    return True


def block_or_warn_invoice_line(tenant_id: str, *, price: float | int | None) -> str | None:
    """For F-75 (a) + (b) — applied when adding a catalog item to an
    invoice. Raises 422 on (a). Returns a warning string for (b) so the
    caller can attach it to the response (frontend renders the banner).
    Returns None when neither toggle fires."""
    p = float(price or 0)
    if p > 0:
        return None
    pol = get_policy(tenant_id)
    if pol.block_zero_price_on_invoice:
        raise HTTPException(
            status_code=422,
            detail="this catalog item has no price set — price it before invoicing.",
        )
    if pol.warn_zero_price_on_invoice:
        return "zero-price line item — review before sending invoice"
    return None


def require_description_or_422(tenant_id: str, description: str | None) -> None:
    """Raise 422 if the tenant has require_description on AND the value
    is missing or whitespace-only."""
    if not get_policy(tenant_id).require_description:
        return
    if not description or not description.strip():
        raise HTTPException(
            status_code=422,
            detail="description is required (your tenant has 'Require catalog description' enabled).",
        )
