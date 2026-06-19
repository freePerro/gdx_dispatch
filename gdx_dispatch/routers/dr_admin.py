"""SS-34 slice F — super-admin endpoints for DR drills.

Endpoints::

    POST /api/admin/dr/drills
         Schedule + run a drill. Body:
           {"scope": "full"|"tenant"|"schema",
            "staging_db_url": "postgresql://…",
            "source_db_url": "postgresql://…",
            "snapshot_target": "/var/backups/…",
            "scope_selector": "tenant_abc",   # optional
            "dry_run": false,
            "scheduled_for": "2026-04-20T…"}   # optional, defaults now

    GET  /api/admin/dr/drills
         List recent drills with pass/fail summary.

    GET  /api/admin/dr/drills/{id}
         Full report: snapshot, restore, verification.

    POST /api/admin/dr/drills/{id}/rerun-verification
         Re-run verification against the SAME staging DB — used when
         adding new checks without re-dumping.

Security:

* Every endpoint requires the ``super-admin`` cross-tenant role.
  Capability string: ``"platform:dr:admin"``.
* A verification-failed drill returns HTTP 500 (5xx) per SS-34 spec
  — "verification failures DO NOT raise [in the orchestrator] —
  the router decides on failure action."

Persistence (Sprint 0.9-l, 2026-04-20):

The router now persists drill state to the canonical platform tables
``dr_drill_run`` + ``dr_verification_report`` + ``dr_snapshot_manifest``
via ``request.state.db``. The previous process-local ``DRILL_STORE``
dict + threading.Lock are gone — Postgres row-level locking plus
session-level transactions handle concurrency.

``dr_verification_report`` is append-only: every rerun writes a NEW row
rather than mutating the prior one (per SS-34 spec — "the sequence of
reports for a drill is itself evidence").
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.core.dr.drill_orchestrator import DrillReport, run_drill
from gdx_dispatch.core.dr.restore_to_staging import ProductionTargetRefused
from gdx_dispatch.core.dr.verification_harness import (
    VerificationConfig,
    run_verification,
)
from gdx_dispatch.core.unified_principal import Principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/dr", tags=["dr"])

CAPABILITY = "platform:dr:admin"

# Slice F is now DB-backed (Sprint 0.9-l). The mount point is still
# gated on SS-34 supervisor wiring (router inclusion in gdx_dispatch/main.py),
# but the storage layer itself is production-ready.
_PRODUCTION_READY = True


def _require_super_admin(principal: Principal) -> str:
    """Return an identity handle if the caller is a super-admin; else raise.

    DR drills are platform-level operations — tenant admins are NOT allowed.
    Accepts both hyphenated and snake_case role spellings plus the
    capability-derived ``is_super_admin`` flag (principal carries ("*","*")).
    """
    if principal.is_super_admin or principal.principal_role in (
        "super-admin",
        "super_admin",
        "platform-admin",
    ):
        return str(principal.identity_id) if principal.identity_id else "super-admin"
    raise HTTPException(
        status_code=403,
        detail=f"super-admin role required (capability: {CAPABILITY})",
    )


def _get_db(request: Request) -> Session:
    db = getattr(request.state, "db", None)
    if db is None:
        raise HTTPException(
            status_code=500, detail="no db session on request.state.db"
        )
    return db


# ───────────────────── DB ↔ dict serialization ─────────────────────


def _drill_row_to_payload(row: Any, verification: Any = None) -> dict[str, Any]:
    """Render a DrDrillRun ORM row as the public report dict.

    Prefers ``report_json`` (the full serialized DrillReport written at
    schedule time) when available — it already carries snapshot +
    restore + verification subtrees. Overlays the latest verification
    row if supplied, so rerun-verification results show through.
    """
    base: dict[str, Any] = dict(row.report_json) if row.report_json else {}
    base.setdefault("drill_run_id", row.drill_run_id)
    base.setdefault("scheduled_for",
                    row.scheduled_for.isoformat() if row.scheduled_for else None)
    base.setdefault("started_at",
                    row.started_at.isoformat() if row.started_at else None)
    base.setdefault("finished_at",
                    row.finished_at.isoformat() if row.finished_at else None)
    base.setdefault("scope", row.scope)
    base.setdefault("dry_run", row.dry_run)
    base["passed"] = row.passed
    base["failure_reason"] = row.failure_reason
    if row.scheduled_by_identity_id:
        base["scheduled_by_identity_id"] = row.scheduled_by_identity_id
    if verification is not None:
        base["verification"] = verification.checks_json
    return base


def _upsert_drill_run(
    db: Session,
    *,
    drill_run_id: str,
    payload: dict[str, Any],
    scheduled_for: datetime,
    scope: str,
    dry_run: bool,
    passed: bool,
    failure_reason: str | None,
    scheduled_by_identity_id: str | None,
) -> None:
    """Insert-or-update a dr_drill_run row from a serialized report dict.

    Uses ORM session.merge() which maps naturally to SQL UPSERT semantics
    for the primary key (drill_run_id). Postgres row-level locking +
    session transaction boundaries handle concurrent writers — the
    idempotency contract lives on the PK.
    """
    from gdx_dispatch.models.platform import DrDrillRun

    def _parse_iso(v: Any) -> datetime | None:
        if not v:
            return None
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(v)
        except (ValueError, TypeError):
            return None

    row = DrDrillRun(
        drill_run_id=drill_run_id,
        scheduled_for=scheduled_for,
        started_at=_parse_iso(payload.get("started_at")),
        finished_at=_parse_iso(payload.get("finished_at")),
        scope=scope,
        scope_selector=payload.get("scope_selector"),
        dry_run=dry_run,
        passed=passed,
        failure_reason=failure_reason,
        snapshot_id=(payload.get("snapshot") or {}).get("id") if payload.get("snapshot") else None,
        scheduled_by_identity_id=scheduled_by_identity_id,
        report_json=payload,
    )
    db.merge(row)
    db.flush()


def _insert_verification_report(
    db: Session,
    *,
    drill_run_id: str,
    verification_dict: dict[str, Any],
    passed: bool,
    failed_count: int,
) -> None:
    """Append a dr_verification_report row. NEVER mutates prior rows."""
    from gdx_dispatch.models.platform import DrVerificationReport

    vr = DrVerificationReport(
        id=uuid4(),
        drill_run_id=drill_run_id,
        created_at=datetime.now(timezone.utc),
        passed=passed,
        failed_count=failed_count,
        checks_json=verification_dict,
    )
    db.add(vr)
    db.flush()


def _fetch_drill(db: Session, drill_run_id: str) -> Any | None:
    from gdx_dispatch.models.platform import DrDrillRun
    return db.get(DrDrillRun, drill_run_id)


def _fetch_latest_verification(db: Session, drill_run_id: str) -> Any | None:
    from gdx_dispatch.models.platform import DrVerificationReport
    return (
        db.query(DrVerificationReport)
        .filter(DrVerificationReport.drill_run_id == drill_run_id)
        .order_by(desc(DrVerificationReport.created_at))
        .first()
    )


# ───────────────────────── request bodies ─────────────────────────


class ScheduleDrillBody(BaseModel):
    scope: str = Field(default="full")
    staging_db_url: str
    source_db_url: str
    snapshot_target: str
    scope_selector: str | None = None
    dry_run: bool = False
    scheduled_for: datetime | None = None
    # Optional caller-supplied drill id for idempotent retries.
    drill_run_id: str | None = None


class RerunVerificationBody(BaseModel):
    staging_db_url: str
    known_identity_id: str | None = None
    tenant_ids_to_verify: list[str] = Field(default_factory=list)


# ───────────────────────── endpoints ─────────────────────────


@router.post("/drills")
def schedule_drill(
    request: Request,
    body: ScheduleDrillBody = Body(...),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    caller = _require_super_admin(principal)
    db = _get_db(request)

    if body.scope not in ("full", "tenant", "schema"):
        raise HTTPException(status_code=400, detail=f"bad scope: {body.scope}")
    if body.scope in ("tenant", "schema") and not body.scope_selector:
        raise HTTPException(
            status_code=400,
            detail=f"scope_selector required when scope={body.scope}",
        )

    drill_id = body.drill_run_id or str(uuid4())
    scheduled = body.scheduled_for or datetime.now(timezone.utc)

    try:
        report: DrillReport = run_drill(
            drill_run_id=drill_id,
            scheduled_for=scheduled,
            scope=body.scope,
            staging_db_url=body.staging_db_url,
            source_db_url=body.source_db_url,
            snapshot_target=body.snapshot_target,
            scope_selector=body.scope_selector,
            dry_run=body.dry_run,
            # write_audit intentionally not wired through here: the
            # DB persistence happens after run_drill returns, in a
            # single transaction against the request-scoped session.
            # The orchestrator's own async persistence callback would
            # need its own session — which is fine, but unused in the
            # admin-triggered path.
        )
    except ProductionTargetRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("drill infra failure: %s", exc)
        # Persist a failure stub so the list endpoint surfaces it.
        stub_payload = {
            "drill_run_id": drill_id,
            "scheduled_for": scheduled.isoformat(),
            "scope": body.scope,
            "dry_run": body.dry_run,
            "passed": False,
            "failure_reason": f"infra: {exc}",
            "scheduled_by_identity_id": caller,
        }
        _upsert_drill_run(
            db,
            drill_run_id=drill_id,
            payload=stub_payload,
            scheduled_for=scheduled,
            scope=body.scope,
            dry_run=body.dry_run,
            passed=False,
            failure_reason=f"infra: {exc}",
            scheduled_by_identity_id=caller,
        )
        db.commit()
        raise HTTPException(
            status_code=500, detail=f"drill infra failure: {exc}"
        ) from exc

    payload = report.to_dict()
    payload["scheduled_by_identity_id"] = caller

    _upsert_drill_run(
        db,
        drill_run_id=drill_id,
        payload=payload,
        scheduled_for=scheduled,
        scope=body.scope,
        dry_run=body.dry_run,
        passed=report.passed,
        failure_reason=report.failure_reason,
        scheduled_by_identity_id=caller,
    )
    # Record the verification run (if any) as its own row so reruns
    # produce a chronological chain of evidence rather than overwriting.
    if report.verification is not None:
        v_dict = report.verification.to_dict()
        _insert_verification_report(
            db,
            drill_run_id=drill_id,
            verification_dict=v_dict,
            passed=report.verification.passed,
            failed_count=len(report.verification.failed_checks),
        )
    db.commit()

    if not report.passed:
        # Verification failure — per spec, router returns 5xx.
        raise HTTPException(
            status_code=500,
            detail={
                "drill_run_id": drill_id,
                "message": "drill verification failed",
                "failure_reason": report.failure_reason,
            },
        )
    return payload


@router.get("/drills")
def list_drills(
    request: Request,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    from gdx_dispatch.models.platform import DrDrillRun

    _require_super_admin(principal)
    db = _get_db(request)

    rows = (
        db.query(DrDrillRun)
        .order_by(desc(DrDrillRun.scheduled_for))
        .limit(50)
        .all()
    )
    return {
        "count": len(rows),
        "drills": [
            {
                "drill_run_id": r.drill_run_id,
                "scheduled_for": r.scheduled_for.isoformat() if r.scheduled_for else None,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "scope": r.scope,
                "dry_run": r.dry_run,
                "passed": r.passed,
                "failure_reason": r.failure_reason,
            }
            for r in rows
        ],
    }


@router.get("/drills/{drill_run_id}")
def get_drill(
    drill_run_id: str,
    request: Request,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    _require_super_admin(principal)
    db = _get_db(request)

    row = _fetch_drill(db, drill_run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="drill not found")
    verification = _fetch_latest_verification(db, drill_run_id)
    return _drill_row_to_payload(row, verification=verification)


@router.post("/drills/{drill_run_id}/rerun-verification")
def rerun_verification(
    drill_run_id: str,
    request: Request,
    body: RerunVerificationBody = Body(...),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """Re-run verification against the same staging DB.

    Useful when new checks are added without re-dumping. The caller
    supplies the staging URL explicitly (we don't trust a stale URL
    persisted from the original drill). Each rerun APPENDS a new
    ``dr_verification_report`` row; the parent ``dr_drill_run.passed``
    is updated (via UPDATE, acquiring row lock) to reflect the latest.
    """
    _require_super_admin(principal)
    db = _get_db(request)

    row = _fetch_drill(db, drill_run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="drill not found")

    db_exec = getattr(request.state, "dr_db_exec", None)
    if db_exec is None:
        raise HTTPException(
            status_code=500,
            detail="no dr_db_exec on request.state (supervisor wires at integration)",
        )

    cfg = VerificationConfig(
        known_identity_id=body.known_identity_id,
        tenant_ids_to_verify=tuple(body.tenant_ids_to_verify),
    )
    vr = run_verification(
        db_exec=db_exec,
        db_for_hashchain=getattr(request.state, "dr_db_session", None),
        config=cfg,
    )
    v_dict = vr.to_dict()

    # Append a new verification report row — never mutate prior ones.
    _insert_verification_report(
        db,
        drill_run_id=drill_run_id,
        verification_dict=v_dict,
        passed=vr.passed,
        failed_count=len(vr.failed_checks),
    )

    # Update parent drill outcome. SQLAlchemy emits UPDATE which
    # acquires a row-level lock under Postgres — safe against
    # concurrent reruns.
    row.passed = vr.passed
    if not vr.passed:
        row.failure_reason = (
            f"verification: {len(vr.failed_checks)} failed checks"
        )
    # Update the stored report_json to reflect the newest verification
    # so list/get callers see current state without a second query.
    new_report: dict[str, Any] = dict(row.report_json) if row.report_json else {}
    new_report["verification"] = v_dict
    new_report["passed"] = vr.passed
    if not vr.passed:
        new_report["failure_reason"] = row.failure_reason
    row.report_json = new_report
    db.flush()
    db.commit()

    payload = _drill_row_to_payload(row)

    if not vr.passed:
        raise HTTPException(
            status_code=500,
            detail={
                "drill_run_id": drill_run_id,
                "message": "rerun verification failed",
                "failed_count": len(vr.failed_checks),
            },
        )
    return payload
