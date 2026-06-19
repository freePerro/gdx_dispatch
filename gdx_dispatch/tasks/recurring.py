from __future__ import annotations

import logging
import os
from typing import Any

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.routers.recurring_jobs import materialize_due_recurring_jobs

log = logging.getLogger(__name__)

_TENANT_ID = os.getenv("GDX_TENANT_ID", "")


@celery_app.task(queue="priority:low")
def generate_recurring_jobs(tenant_id: str = "") -> dict[str, Any]:
    tid = tenant_id or _TENANT_ID
    db = SessionLocal()
    try:
        result = materialize_due_recurring_jobs(db, actor_id="system", tenant_id=tid)
        return {"tenant_id": tid, **result}
    except Exception:
        log.exception("generate_recurring_jobs_failed", extra={"tenant_id": tid})
        db.rollback()
        raise
    finally:
        db.close()
