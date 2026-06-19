"""Tax resolution service.

Single entry point: `resolve_rate(db, customer_id) -> Decimal`.

Today: returns 0 if customer is exempt, else returns TaxConfig.default_rate.
Tomorrow: zip lookup against TaxJurisdiction, line-item category overrides,
provider plugins (Avalara/TaxJar). The contract stays the same so callers
never have to know.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption


_ZERO = Decimal("0")


def get_or_create_config(db: Session) -> TaxConfig:
    """Return the single TaxConfig row, creating it (rate=0) if absent."""
    cfg = db.execute(select(TaxConfig).limit(1)).scalar_one_or_none()
    if cfg is None:
        cfg = TaxConfig(name="Default", default_rate=_ZERO)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def is_customer_exempt(db: Session, customer_id: UUID | str | None) -> bool:
    if not customer_id:
        return False
    try:
        cid = customer_id if isinstance(customer_id, UUID) else UUID(str(customer_id))
    except (ValueError, AttributeError):
        return False
    row = db.execute(
        select(TaxExemption).where(TaxExemption.customer_id == cid, TaxExemption.exempt.is_(True))
    ).scalar_one_or_none()
    if row is None:
        return False
    # Honor exempt_from / exempt_until if set
    from datetime import date as _date
    today = _date.today()
    if row.exempt_from and today < row.exempt_from:
        return False
    if row.exempt_until and today > row.exempt_until:
        return False
    return True


def resolve_rate(db: Session, customer_id: UUID | str | None = None) -> Decimal:
    """Return the tax rate (decimal fraction) to apply for a transaction.

    Today's rules:
    - Customer exempt → 0
    - Else → TaxConfig.default_rate

    Future hooks (currently no-ops):
    - jurisdiction lookup by service-address zip
    - per-line-item category overrides
    - per-tenant TaxProvider plugin (Avalara, TaxJar)
    """
    if is_customer_exempt(db, customer_id):
        return _ZERO
    cfg = db.execute(select(TaxConfig).limit(1)).scalar_one_or_none()
    if cfg is None or cfg.default_rate is None:
        return _ZERO
    return Decimal(str(cfg.default_rate))
