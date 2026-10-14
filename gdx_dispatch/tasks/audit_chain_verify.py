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

from gdx_dispatch.core.audit import audit_chain_broken, audit_chain_report, verify_audit_chain
from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


@celery_app.task(name="audit.verify_chain_nightly", queue="priority:low")
def verify_chain_nightly() -> dict:
    """Verify the whole audit chain. Returns {ok, rows} and, when the chain is
    broken, logs an ERROR marked ``ops_alert`` (fingerprint
    ``audit-chain-verify-nightly``, the beat entry's name) so the ops-alert
    handler records it on Server Errors and emails OPS_ALERT_EMAIL — not just
    a return value nobody reads."""
    try:
        with SessionLocal() as db:
            from sqlalchemy import func, select

            from gdx_dispatch.core.audit import AuditLog

            ok = verify_audit_chain(db)
            rows = int(db.execute(select(func.count()).select_from(AuditLog)).scalar() or 0)
            report = audit_chain_report(db)
    except Exception:  # noqa: BLE001 — a scheduled integrity check must never
        # crash the beat; a read failure is itself worth logging.
        log.exception("audit_chain_verify_failed_to_run")
        return {"ok": None, "rows": 0, "error": "read_failed"}

    unchained = report["unchained"]
    known_breaks = report["known_breaks"]
    # The structural check names a fork, a bad hash or a missing predecessor
    # whatever the unchained count (GDXA-979: the old WARN branch swallowed
    # every failure once any row was unchained).
    broken = audit_chain_broken(ok, report)
    unexplained = broken and not report["broken"]
    if broken:
        log.error(
            "AUDIT_CHAIN_BROKEN — %d row(s) fail their own hash, %d name a "
            "missing predecessor, %d new fork(s), %d new restart(s)%s; a row was "
            "tampered, inserted out of chain, or deleted. Investigate immediately.",
            report["bad_hash"], report["dangling"], report["new_forks"], report["new_restarts"],
            " (and the ordered walk fails with nothing to explain it)" if unexplained else "",
            extra={
                "rows_checked": rows,
                "ops_alert": True,
                "ops_fingerprint": "audit-chain-verify-nightly",
            },
        )
    elif ok:
        log.info("audit_chain_verify_ok", extra={"rows_checked": rows})
    if unchained > 0:
        # Known: the GL ledger writers build audit rows directly (no hash).
        # That is a data-hygiene gap, not tampering. This WARN is about those
        # rows only; it no longer stands in for the check above.
        log.warning(
            "audit_chain_has_unchained_rows — %d of %d rows bypass the hash "
            "chain (GL/legacy writers); not tamper, but the all-scope check "
            "can't attest until they chain.",
            unchained, rows,
        )
    if known_breaks:
        log.info(
            "audit_chain_known_breaks — %d fork(s)/restart(s) written before %s "
            "by the unlocked tip read (GDXA-979); history, not re-alerted.",
            known_breaks, report["known_breaks_before"],
        )
    return {
        "ok": bool(ok),
        "rows": rows,
        "unchained": unchained,
        "forks": len(report["forks"]),
        "new_forks": report["new_forks"],
        "broken": broken,
    }
