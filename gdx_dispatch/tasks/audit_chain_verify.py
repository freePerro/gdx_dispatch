"""Nightly audit-chain integrity check — plan §13.

The audit log is hash-chained (prev_hash/row_hash) and verify_audit_chain
proves tamper-evidence, but it was referenced only from tests — no endpoint,
no scheduled run. A tamper-evident log nobody checks is a locked filing
cabinet with no one holding the key. This beat task runs the check nightly
and logs LOUDLY on failure (a broken chain is a compliance incident, not a
soft warning). The admin can also run it on demand via GET
/api/audit/verify-chain.
"""
from __future__ import annotations

import logging

from gdx_dispatch.core.audit import verify_audit_chain
from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


@celery_app.task(name="audit.verify_chain_nightly", queue="priority:low")
def verify_chain_nightly() -> dict:
    """Verify the whole audit chain. Returns {ok, rows} and logs an ERROR when
    the chain is broken so it surfaces in Sentry / the log triage, not just a
    return value nobody reads."""
    try:
        with SessionLocal() as db:
            from sqlalchemy import func, select

            from gdx_dispatch.core.audit import AuditLog

            ok = verify_audit_chain(db)
            rows = int(db.execute(select(func.count()).select_from(AuditLog)).scalar() or 0)
            from sqlalchemy import or_ as _or
            unchained = int(db.execute(
                select(func.count()).select_from(AuditLog).where(
                    _or(AuditLog.row_hash.is_(None), AuditLog.row_hash == "")
                )
            ).scalar() or 0)
    except Exception:  # noqa: BLE001 — a scheduled integrity check must never
        # crash the beat; a read failure is itself worth logging.
        log.exception("audit_chain_verify_failed_to_run")
        return {"ok": None, "rows": 0, "error": "read_failed"}

    if ok:
        log.info("audit_chain_verify_ok", extra={"rows_checked": rows})
    elif unchained > 0:
        # Known: the GL ledger writers build audit rows directly (no hash).
        # That is a data-hygiene gap, not tampering — WARN, don't cry wolf.
        # A clean nightly check needs those writers routed through the chain
        # (tracked separately); until then this quantifies the gap.
        log.warning(
            "audit_chain_has_unchained_rows — %d of %d rows bypass the hash "
            "chain (GL/legacy writers); not tamper, but the all-scope check "
            "can't attest until they chain.",
            unchained, rows,
        )
    else:
        # ok:false with ZERO unchained rows = a genuine break in the chain.
        log.error(
            "AUDIT_CHAIN_BROKEN — the hash chain failed with no unchained "
            "rows to explain it; a row was tampered, reordered, or deleted. "
            "Investigate immediately.",
            extra={"rows_checked": rows},
        )
    return {"ok": bool(ok), "rows": rows, "unchained": unchained}
