"""Billing terms resolver — feeds invoice creation."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import SessionLocal, tenant_context

log = logging.getLogger(__name__)


@dataclass
class EffectiveTerms:
    payment_terms_days: int
    early_pay_discount_percent: Decimal | None
    early_pay_discount_days: int | None
    late_fee_flat_amount: Decimal | None
    late_fee_percent: Decimal | None
    late_fee_grace_days: int
    interest_rate_monthly_percent: Decimal | None
    interest_grace_days: int

    def due_date(self, invoice_date: date | datetime | None) -> date:
        if invoice_date is None:
            invoice_date = datetime.utcnow().date()
        elif isinstance(invoice_date, datetime):
            invoice_date = invoice_date.date()
        return invoice_date + timedelta(days=int(self.payment_terms_days))

    def early_pay_deadline(self, invoice_date: date | datetime | None) -> date | None:
        if not self.early_pay_discount_days or not self.early_pay_discount_percent:
            return None
        if invoice_date is None:
            invoice_date = datetime.utcnow().date()
        elif isinstance(invoice_date, datetime):
            invoice_date = invoice_date.date()
        return invoice_date + timedelta(days=int(self.early_pay_discount_days))


def _row_to_terms(row: Any, days: int) -> EffectiveTerms:
    return EffectiveTerms(
        payment_terms_days=int(days),
        early_pay_discount_percent=row[1] if row else None,
        early_pay_discount_days=row[2] if row else None,
        late_fee_flat_amount=row[3] if row else None,
        late_fee_percent=row[4] if row else None,
        late_fee_grace_days=int(row[5] or 0) if row else 0,
        interest_rate_monthly_percent=row[6] if row else None,
        interest_grace_days=int(row[7] or 0) if row else 0,
    )


def resolve_effective_terms(
    *,
    tenant_id: str | UUID,
    pricing_class: str | None,
    customer_payment_terms_days: int | None,
    tenant_db: Session | None = None,  # unused today; reserved for future caching
) -> EffectiveTerms:
    """Decide what payment-terms apply for one (tenant, customer) pair.

    The control DB row is the source of truth for tenant defaults +
    fee config. Customer-level override beats everything except when
    it's NULL (then we fall back to the per-class default, then the
    tenant default).

    Best-effort: if the control DB is unreachable, return a 30-day
    default with no fees — invoice creation never blocks on this.
    """
    _ = tenant_db
    tid = str(tenant_id)
    try:
        with tenant_context(), SessionLocal() as cdb:
            row = cdb.execute(
                text(
                    "SELECT default_payment_terms_days, "
                    "       early_pay_discount_percent, early_pay_discount_days, "
                    "       late_fee_flat_amount, late_fee_percent, late_fee_grace_days, "
                    "       interest_rate_monthly_percent, interest_grace_days, "
                    "       contractor_payment_terms_days, retail_payment_terms_days, "
                    "       wholesale_payment_terms_days "
                    "FROM tenant_settings WHERE tenant_id = :tid"
                ),
                {"tid": tid},
            ).first()
    except Exception:
        log.exception("billing_terms_read_failed", extra={"tenant_id": tid})
        row = None

    if row is None:
        return EffectiveTerms(
            payment_terms_days=30,
            early_pay_discount_percent=None,
            early_pay_discount_days=None,
            late_fee_flat_amount=None,
            late_fee_percent=None,
            late_fee_grace_days=0,
            interest_rate_monthly_percent=None,
            interest_grace_days=0,
        )

    default_days = int(row[0] or 30)
    contractor_days = row[8]
    retail_days = row[9]
    wholesale_days = row[10]

    # 1) Customer-level override wins.
    if customer_payment_terms_days and int(customer_payment_terms_days) > 0:
        days = int(customer_payment_terms_days)
    else:
        # 2) Per-pricing-class default.
        pc = (pricing_class or "").lower()
        if pc == "contractor" and contractor_days:
            days = int(contractor_days)
        elif pc == "retail" and retail_days:
            days = int(retail_days)
        elif pc == "wholesale" and wholesale_days:
            days = int(wholesale_days)
        else:
            days = default_days

    return _row_to_terms(row, days)
