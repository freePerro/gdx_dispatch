"""Host-provided catalog upsert for plugins (#51, ADR-013).

A plugin can capture items (e.g. the CHI pricing plugin scrapes a door's spec +
price) but ADR-013 gave it no supported way to PERSIST them into a browsable,
reusable catalog — only onto an estimate line. So a captured door was invisible
to the item picker. This is the missing surface: a plugin calls
``upsert_catalog_items`` with its DB session (from ``get_plugin_db`` — already
the tenant connection) and the target catalog id, and items land through the
SAME pricing-strategy / vendor / custom-attribute path the UI and CSV import use.

Kept out of ``plugin_api/__init__`` (which stays stdlib-only for host-side
discovery tests) — importing this pulls in the ORM + core catalog helpers, the
same as ``plugin_api/context``.

Usage from a plugin route::

    from gdx_dispatch.plugin_api.catalog import upsert_catalog_items
    res = upsert_catalog_items(db, catalog_id, [
        {"sku": "CHI-2216", "name": "CHI Door 16x7", "cost": 1850,
         "vendor": "CHI", "attributes": {...}},
    ], source="chi-pricing")
    # res.created / res.updated
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import CustomCatalog, CustomCatalogItem
from gdx_dispatch.routers.catalog import (
    VIRTUAL_CATALOG_IDS,
    _coerce_attributes,
    _money,
    _retail_for,
)


class CatalogUpsertError(ValueError):
    """Raised when the target catalog is missing, deleted, or virtual."""


@dataclass(frozen=True)
class UpsertResult:
    created: int
    updated: int
    catalog_id: str


def upsert_catalog_items(
    db: Session,
    catalog_id: UUID | str,
    items: list[dict],
    *,
    source: str | None = None,
) -> UpsertResult:
    """Upsert ``items`` into the given catalog, deduping by SKU.

    Each item is a dict with any of: sku, name, description, cost, price,
    category, vendor, attributes. Pricing follows the catalog's strategy
    (``_retail_for``) exactly like every other write path, so a plugin can't
    accidentally store retail=cost. ``vendor`` defaults to ``source`` when the
    item doesn't carry its own, so captured items are traceable to the plugin.

    The ``db`` session IS the tenant connection (three-plane), so catalog/items
    are already tenant-scoped — no company_id filter needed. Caller commits via
    this function; it commits once at the end.

    Raises ``CatalogUpsertError`` for a missing/deleted/virtual catalog.
    """
    cid = catalog_id if isinstance(catalog_id, UUID) else UUID(str(catalog_id))
    if str(cid) in VIRTUAL_CATALOG_IDS:
        raise CatalogUpsertError("cannot upsert into a virtual (feed-backed) catalog")

    catalog = db.execute(
        select(CustomCatalog).where(
            CustomCatalog.id == cid, CustomCatalog.deleted_at.is_(None)
        )
    ).scalar_one_or_none()
    if catalog is None:
        raise CatalogUpsertError(f"catalog {cid} not found")

    product_class = (catalog.product_class or "parts").strip().lower()
    created = 0
    updated = 0

    for raw in items:
        sku = str(raw.get("sku") or "").strip() or None
        cost = raw.get("cost")
        price = raw.get("price")
        vendor = str(raw.get("vendor") or source or "").strip() or None

        match = None
        if sku:
            match = db.execute(
                select(CustomCatalogItem).where(
                    CustomCatalogItem.catalog_id == cid,
                    CustomCatalogItem.sku == sku,
                    CustomCatalogItem.deleted_at.is_(None),
                )
            ).scalar_one_or_none()

        if match is None:
            row = CustomCatalogItem(
                catalog_id=cid,
                sku=sku,
                name=str(raw.get("name") or sku or "Item").strip()[:200],
                description=str(raw.get("description") or "").strip() or None,
                cost=_money(float(cost or 0)),
                price=_money(_retail_for(catalog, cost, price)),
                category=str(raw.get("category") or "").strip() or None,
                vendor=vendor,
                product_class=product_class,
                active=True,
            )
            if product_class == "custom":
                row.attributes = _coerce_attributes(
                    catalog.field_schema or [], raw.get("attributes")
                )
            db.add(row)
            created += 1
        else:
            if raw.get("name"):
                match.name = str(raw["name"]).strip()[:200]
            if raw.get("description") is not None:
                match.description = str(raw.get("description") or "").strip() or None
            if cost is not None:
                match.cost = _money(float(cost))
            # Reprice from the (possibly updated) cost via the catalog strategy.
            match.price = _money(
                _retail_for(catalog, cost if cost is not None else match.cost, price)
                or match.price
                or 0
            )
            if raw.get("category"):
                match.category = str(raw["category"]).strip()
            if vendor:
                match.vendor = vendor
            if product_class == "custom" and raw.get("attributes"):
                coerced = _coerce_attributes(catalog.field_schema or [], raw.get("attributes"))
                match.attributes = {**(match.attributes or {}), **coerced}
            updated += 1

    db.commit()
    return UpsertResult(created=created, updated=updated, catalog_id=str(cid))
