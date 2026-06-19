"""Celery task: auto-create invoices from active service agreements.

Runs daily. Finds service agreements with next_billing_date <= today,
creates an invoice for each, and advances next_billing_date.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

from dateutil.relativedelta import relativedelta
from sqlalchemy import text

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


def _process_tenant_billing(tenant_id: str) -> int:
    """Process recurring billing."""
    db = SessionLocal()
    created = 0

    try:
        now = datetime.now(timezone.utc)
        agreements = db.execute(
            text(
                """
                SELECT id, customer_id, amount, billing_interval_months, next_billing_date
                FROM service_agreements
                WHERE company_id = :tid
                  AND active = true
                  AND deleted_at IS NULL
                  AND next_billing_date <= :now
                """
            ),
            {"tid": tenant_id, "now": now},
        ).mappings().all()

        for agreement in agreements:
            invoice_id = str(uuid4())
            db.execute(
                text(
                    """
                    INSERT INTO invoices (id, customer_id, company_id, invoice_number,
                        status, total, amount_paid, public_token, created_at)
                    VALUES (:id, :cid, :tid, :num, 'draft', :total, 0, :token, :now)
                    """
                ),
                {
                    "id": invoice_id,
                    "cid": str(agreement["customer_id"]),
                    "tid": tenant_id,
                    "num": f"SA-{uuid4().hex[:8].upper()}",
                    "total": float(agreement["amount"]),
                    "token": uuid4().hex[:32],
                    "now": now,
                },
            )

            # Advance next_billing_date
            interval = int(agreement["billing_interval_months"] or 1)
            next_date = agreement["next_billing_date"] + relativedelta(months=interval)
            db.execute(
                text("UPDATE service_agreements SET next_billing_date = :nd WHERE id = :aid"),
                {"nd": next_date, "aid": str(agreement["id"])},
            )
            created += 1

        if created:
            db.commit()
            log.info("recurring_billing_processed", extra={"tenant_id": tenant_id, "invoices_created": created})

    except Exception:
        log.exception("recurring_billing_failed", extra={"tenant_id": tenant_id})
        db.rollback()
    finally:
        db.close()

    return created


@celery_app.task(name="process_recurring_billing")
def process_recurring_billing() -> dict:
    """Run recurring billing."""
    tenant_id = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    total = 0
    try:
        total += _process_tenant_billing(tenant_id)
    except Exception:
        log.exception("process_recurring_billing_failed")
    return {"tenants_checked": 1, "invoices_created": total}
