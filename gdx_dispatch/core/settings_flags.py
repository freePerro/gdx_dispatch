"""Small resilient readers for company-wide tenant_settings toggles.

Separate from routers/jobs._load_workflow_flags (which reads the job-completion
gate columns) so billing/closeout code can check a flag without importing the
jobs router. Every reader defaults to the SAFE value (feature off) on any read
error — a missing column on a not-yet-migrated DB must never crash the caller.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


def closeout_reconciliation_enabled(tenant_id: str) -> bool:
    """Is the §12 closeout→billing reconciliation feature turned on for this
    tenant? Default False (feature off) on any error."""
    try:
        with SessionLocal() as db:
            row = db.execute(
                text(
                    "SELECT closeout_billing_reconciliation "
                    "FROM tenant_settings WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            ).first()
            return bool(row[0]) if row else False
    except Exception:
        log.exception("closeout_reconciliation_flag_read_failed", extra={"tenant_id": tenant_id})
        return False
