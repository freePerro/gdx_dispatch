from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.wholesale.models import CatalogItem, PricingTier


def upsert_catalog_item(
    wholesaler_tenant_id: str,
    sku: str,
    name: str,
    base_price: float,
    description: str | None,
    db: Session,
) -> CatalogItem:
    item = db.execute(
        select(CatalogItem).where(
            CatalogItem.wholesaler_tenant_id == wholesaler_tenant_id,
            CatalogItem.sku == sku,
        )
    ).scalar_one_or_none()
    if item:
        item.name = name
        item.base_price = base_price
        if description is not None:
            item.description = description
    else:
        item = CatalogItem(
            wholesaler_tenant_id=wholesaler_tenant_id,
            sku=sku,
            name=name,
            base_price=base_price,
            description=description,
        )
        db.add(item)
    db.commit()
    db.refresh(item)
    return item


def get_catalog(wholesaler_tenant_id: str, db: Session) -> list[CatalogItem]:
    return list(
        db.execute(
            select(CatalogItem).where(
                CatalogItem.wholesaler_tenant_id == wholesaler_tenant_id,
                CatalogItem.is_active.is_(True),
            ).order_by(CatalogItem.sku)
        ).scalars().all()
    )


def set_pricing_tier(
    wholesaler_tenant_id: str,
    distributor_tenant_id: str,
    tier_name: str,
    discount_pct: float,
    db: Session,
) -> PricingTier:
    tier = db.execute(
        select(PricingTier).where(
            PricingTier.wholesaler_tenant_id == wholesaler_tenant_id,
            PricingTier.distributor_tenant_id == distributor_tenant_id,
        )
    ).scalar_one_or_none()
    if tier:
        tier.tier_name = tier_name
        tier.discount_pct = discount_pct
    else:
        tier = PricingTier(
            wholesaler_tenant_id=wholesaler_tenant_id,
            distributor_tenant_id=distributor_tenant_id,
            tier_name=tier_name,
            discount_pct=discount_pct,
        )
        db.add(tier)
    db.commit()
    db.refresh(tier)
    return tier


def get_discounted_price(
    wholesaler_tenant_id: str,
    distributor_tenant_id: str,
    sku: str,
    db: Session,
) -> float | None:
    item = db.execute(
        select(CatalogItem).where(
            CatalogItem.wholesaler_tenant_id == wholesaler_tenant_id,
            CatalogItem.sku == sku,
            CatalogItem.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if not item:
        return None
    tier = db.execute(
        select(PricingTier).where(
            PricingTier.wholesaler_tenant_id == wholesaler_tenant_id,
            PricingTier.distributor_tenant_id == distributor_tenant_id,
        )
    ).scalar_one_or_none()
    discount = float(tier.discount_pct) / 100 if tier else 0
    return float(item.base_price) * (1 - discount)
