from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from uuid import UUID

import stripe
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import Tenant, TenantModuleGrant
from gdx_dispatch.core.database import SessionLocal

logger = logging.getLogger(__name__)
_BASE_PRICE = float(os.getenv("BILLING_BASE_PRICE", "0"))
_PER_MODULE_PRICE = float(os.getenv("BILLING_PER_MODULE_PRICE", "0"))
_DELTA_THRESHOLD = float(os.getenv("BILLING_RECONCILIATION_DELTA", "0.10"))


def _stripe_monthly_amount(subscription_id: str | None) -> float:
    if not subscription_id:
        return 0.0
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        return 0.0
    try:
        sub = stripe.Subscription.retrieve(subscription_id)
    except Exception:
        logger.exception("stripe_subscription_retrieve_failed", extra={"subscription_id": subscription_id})
        return 0.0
    items = sub.get("items", {}).get("data", [])
    cents = sum(int((i.get("price") or {}).get("unit_amount") or 0) * int(i.get("quantity") or 1) for i in items)
    return round(cents / 100.0, 2)


def run_billing_reconciliation(db: Session) -> dict:
    now = datetime.now(timezone.utc)
    discrepancies: list[dict[str, float | str]] = []
    # Single-tenant: no Stripe subscription to reconcile; return empty result.
    return {"checked": 0, "discrepancies": discrepancies}


def detect_schema_drift(tenant_id: str, expected_tables: list[str], db: Session) -> bool:
    try:
        tenant = db.get(Tenant, UUID(tenant_id))
    except ValueError:
        logging.getLogger(__name__).exception("detect_schema_drift caught exception")
        return True
    if not tenant or tenant.deleted_at is not None:
        return True
    with db.get_bind().connect() as conn:
        try:
            rows = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))
        except Exception:
            logging.getLogger(__name__).exception("detect_schema_drift caught exception")
            rows = conn.execute(text("SELECT name AS table_name FROM sqlite_master WHERE type='table'"))
        actual = {r[0] for r in rows}
    missing, extra = sorted(set(expected_tables) - actual), sorted(actual - set(expected_tables))
    drift = bool(missing or extra)
    if drift:
        logger.warning("control_audit_event", extra={"event_type": "schema_drift_detected", "entity_type": "tenant", "entity_id": tenant_id, "payload": {"missing": missing, "extra": extra}})
    return drift
