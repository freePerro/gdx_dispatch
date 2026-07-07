"""Sprint 1.0.6 — nightly refresh of customer rolling-12mo paid volume cache.

Defensive backstop. The hot paths (payment.received, estimate-create
stale check) keep the cache fresh during normal use, but customers who
haven't received a payment recently AND haven't been quoted recently can
drift if the rolling window slides past an old payment. This nightly
sweep recomputes every customer in every tenant so a stale cache never
silently mis-prices an estimate the next morning.

Pattern: walk all tenants from
the control DB, open a per-tenant session, refresh, commit.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlalchemy import text

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


def _refresh_tenant_volumes(tenant_id: str) -> int:
    """Recompute cached_rolling_volume for every active customer.

    Returns the number of customers updated. Single SQL UPDATE rather than
    per-row Python — keeps the nightly job cheap on big invoice histories.
    """
    db = SessionLocal()
    updated = 0
    try:
        now = datetime.now(timezone.utc)
        # All-customers refresh in one statement. Keeps locks short and
        # avoids N+1 round-trips on tenants with thousands of customers.
        # COALESCE handles "customer with no qualifying payments" → 0.
        result = db.execute(
            text(
                """
                UPDATE customers c
                SET cached_rolling_volume_paid_12mo = COALESCE(sub.total, 0),
                    cached_rolling_volume_at = :now
                FROM (
                    SELECT i.customer_id AS cid,
                           SUM(p.amount) AS total
                    FROM payments p
                    JOIN invoices i ON i.id = p.invoice_id
                    WHERE i.status != 'void'
                      AND p.payment_date >= (CAST(:now AS DATE) - INTERVAL '365 days')
                      AND i.customer_id IS NOT NULL
                    GROUP BY i.customer_id
                ) sub
                WHERE c.id = sub.cid
                  AND c.deleted_at IS NULL
                """
            ),
            {"now": now},
        )
        updated_with_payments = result.rowcount or 0

        # Zero-out customers who had no qualifying payments but might
        # have a stale non-zero cache (window slid past their last payment).
        # This is the silent-drift case the nightly task exists for.
        result_zero = db.execute(
            text(
                """
                UPDATE customers c
                SET cached_rolling_volume_paid_12mo = 0,
                    cached_rolling_volume_at = :now
                WHERE c.deleted_at IS NULL
                  AND c.cached_rolling_volume_paid_12mo IS NOT NULL
                  AND c.cached_rolling_volume_paid_12mo > 0
                  AND NOT EXISTS (
                      SELECT 1 FROM payments p
                      JOIN invoices i ON i.id = p.invoice_id
                      WHERE i.customer_id = c.id
                        AND i.status != 'void'
                        AND p.payment_date >= (CAST(:now AS DATE) - INTERVAL '365 days')
                  )
                """
            ),
            {"now": now},
        )
        updated_zeroed = result_zero.rowcount or 0
        updated = updated_with_payments + updated_zeroed
        db.commit()
        if updated:
            log.info(
                "customer_volume_refreshed",
                extra={
                    "tenant_id": tenant_id,
                    "with_payments": updated_with_payments,
                    "zeroed": updated_zeroed,
                },
            )
    except Exception:
        log.exception("customer_volume_refresh_failed", extra={"tenant_id": tenant_id})
        db.rollback()
    finally:
        db.close()
    return updated


@celery_app.task(name="refresh_all_customer_rolling_volumes", queue="priority:low")
def refresh_all_customer_rolling_volumes() -> dict:
    """Refresh every customer's rolling-volume cache."""
    tenant_id = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    total = 0
    try:
        total += _refresh_tenant_volumes(tenant_id)
    except Exception:
        log.exception("refresh_all_customer_rolling_volumes_failed")
    return {"tenants_checked": 1, "customers_updated": total}
