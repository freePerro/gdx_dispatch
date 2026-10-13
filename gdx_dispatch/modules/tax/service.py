"""Tax resolution service.

Single entry point: `resolve_rate(db, customer_id) -> Decimal`. A caller
billing a job uses `resolve_rate_for_job(db, customer_id, job_type)`, which
adds the job-lane rule (installations are never taxed) in front of it.

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
    # Honor exempt_from / exempt_until if set — against the shop's calendar
    # day, not the UTC server's (GDXA-505): an exemption ending today lapsed
    # at ~7pm Central. Read only when a bound is set, so an open-ended
    # exemption costs no settings query.
    if not (row.exempt_from or row.exempt_until):
        return True
    from gdx_dispatch.core.pay_periods import shop_today_from_settings  # noqa: PLC0415

    today = shop_today_from_settings(db)
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
    """
    if is_customer_exempt(db, customer_id):
        return _ZERO
    cfg = db.execute(select(TaxConfig).limit(1)).scalar_one_or_none()
    if cfg is None or cfg.default_rate is None:
        return _ZERO
    return Decimal(str(cfg.default_rate))


def is_install_lane(job_type: str | None) -> bool:
    """True when the job is an installation (§8 install lane): a construction
    contract, never taxed (Doug, 2026-10-12, GDXA-793/796)."""
    from gdx_dispatch.core.billing_lanes import lane_for_job  # noqa: PLC0415

    return lane_for_job(job_type) == "install"


def resolve_rate_for_job(
    db: Session,
    customer_id: UUID | str | None,
    job_type: str | None,
    quoted_rate: Decimal | None = None,
) -> Decimal:
    """The tax rate for a job's bill, with the job lane applied first.

    Used by office invoice create and `/api/tax/resolve` (GDXA-796) and the
    change order detail total (GDXA-797). Not yet by the closeout autodraft
    (its own inline lane check) or estimate totals (no lane check): those are
    separate issues.

    - Install lane → 0, without reading the tenant default.
    - Exempt customer → 0, on any lane.
    - `quoted_rate` (an estimate's own rate override, M24) → that rate: the
      customer accepted it, so the tenant default must not replace it
      (GDXA-842). It cannot tax an installation or an exempt customer — both
      are never taxed (Doug, 2026-10-12).
    - Otherwise the tenant default.

    Like `resolve_rate`, this raises on a DB error; callers own containment.
    """
    if is_install_lane(job_type):
        return _ZERO
    if quoted_rate is None:
        return resolve_rate(db, customer_id)
    if is_customer_exempt(db, customer_id):
        return _ZERO
    return Decimal(str(quoted_rate))
