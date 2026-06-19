"""Job assignments — Phase 1.4 multi-tech support.

D1: list/create/delete the (job, tech) edges that say who is assigned.
D5: per-job ``is_lead`` designation, optional and at-most-one per job.

Per-tech state-machine timestamps (en_route_at / arrived_at /
completed_at) are written by ``gdx_dispatch/routers/mobile.py`` when the calling
tech taps the corresponding button — this router only manages the
membership and lead-tech bits.

Backwards-compat: ``Job.assigned_to`` is kept as a denormalization of
"the primary tech." On every assignment write we recompute it as
(lead-tech if any, else first-assigned tech, else NULL) so dashboards
and the legacy /api/jobs list keep returning a useful primary.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.models.tenant_models import Job, JobAssignment
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api",
    tags=["job-assignments"],
    dependencies=[Depends(require_module("jobs"))],
)


_DISPATCH_ROLES = {"dispatcher", "admin", "owner", "super_admin"}


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


def _role(user: dict) -> str:
    return str(user.get("role") or "").lower()


def _require_dispatch_role(user: dict) -> None:
    if _role(user) not in _DISPATCH_ROLES:
        raise HTTPException(
            status_code=403,
            detail="assignment changes require dispatcher, admin, or owner role",
        )


class AssignBody(BaseModel):
    tech_id: str = Field(min_length=1, max_length=36)
    user_id: str | None = Field(default=None, max_length=36)
    is_lead: bool = False


class LeadBody(BaseModel):
    """Set or clear the lead. ``tech_id=None`` clears the lead."""

    tech_id: str | None = Field(default=None, max_length=36)


def _serialize(a: JobAssignment) -> dict[str, Any]:
    return {
        "id": a.id,
        "job_id": a.job_id,
        "tech_id": a.tech_id,
        "user_id": a.user_id,
        "is_lead": bool(a.is_lead),
        "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
        "assigned_by": a.assigned_by,
        "en_route_at": a.en_route_at.isoformat() if a.en_route_at else None,
        "arrived_at": a.arrived_at.isoformat() if a.arrived_at else None,
        "completed_at": a.completed_at.isoformat() if a.completed_at else None,
    }


def _list_active(db: Session, job_id: str) -> list[JobAssignment]:
    return list(
        db.execute(
            select(JobAssignment)
            .where(JobAssignment.job_id == job_id, JobAssignment.deleted_at.is_(None))
            .order_by(JobAssignment.assigned_at.asc())
        ).scalars().all()
    )


def _recompute_primary(db: Session, job_id: str) -> str | None:
    """Set ``Job.assigned_to`` = lead, else first-assigned, else NULL.

    Called after every assignment write so the legacy single-tech reads
    keep returning a useful "primary tech" without inventing a separate
    code path. Three-plane: connection isolates tenant; no FK filter."""
    rows = _list_active(db, job_id)
    primary: str | None = None
    for r in rows:
        if r.is_lead:
            primary = r.tech_id
            break
    if primary is None and rows:
        primary = rows[0].tech_id
    db.execute(
        text("UPDATE jobs SET assigned_to = :p WHERE id = :j AND deleted_at IS NULL"),
        {"p": primary, "j": job_id},
    )
    return primary


@router.get(
    "/jobs/{job_id}/assignments",
    dependencies=[Depends(require_permission("jobs.read_all"))],
)
def list_assignments(
    job_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return [_serialize(a) for a in _list_active(db, job_id)]


@router.post(
    "/jobs/{job_id}/assignments",
    status_code=201,
    dependencies=[Depends(require_permission("jobs.write"))],
)
def add_assignment(
    job_id: str,
    request: Request,
    payload: AssignBody,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_dispatch_role(user)
    tid = _tid(request)
    uid = _uid(user)

    # Reject duplicate active assignments for the same tech on this job.
    existing = db.execute(
        select(JobAssignment).where(
            JobAssignment.job_id == job_id,
            JobAssignment.tech_id == payload.tech_id,
            JobAssignment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"tech {payload.tech_id} is already assigned to job {job_id}",
        )

    # If this row claims lead, demote any sibling lead first (at most one
    # lead per job is the rule for D5).
    if payload.is_lead:
        db.execute(
            text(
                "UPDATE job_assignments SET is_lead = :f "
                "WHERE job_id = :j AND deleted_at IS NULL"
            ),
            {"j": job_id, "f": False},
        )
        # text() bypasses ORM identity map; expire stale is_lead caches.
        db.expire_all()

    row = JobAssignment(
        id=str(uuid4()),
        job_id=job_id,
        tech_id=payload.tech_id,
        user_id=payload.user_id,
        is_lead=bool(payload.is_lead),
        assigned_at=datetime.now(timezone.utc),
        assigned_by=uid,
    )
    db.add(row)
    db.flush()
    primary = _recompute_primary(db, job_id)
    db.commit()
    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="assign",
        entity_type="job_assignment", entity_id=row.id,
        details={
            "job_id": job_id, "tech_id": payload.tech_id,
            "is_lead": payload.is_lead, "primary_after": primary,
        },
        request=request,
    )
    return _serialize(row)


@router.delete(
    "/jobs/{job_id}/assignments/{assignment_id}",
    dependencies=[Depends(require_permission("jobs.write"))],
)
def remove_assignment(
    job_id: str,
    assignment_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_dispatch_role(user)
    tid = _tid(request)
    uid = _uid(user)

    row = db.execute(
        select(JobAssignment).where(
            JobAssignment.id == assignment_id,
            JobAssignment.job_id == job_id,
        )
    ).scalar_one_or_none()
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="assignment not found")

    row.deleted_at = datetime.now(timezone.utc)
    db.flush()
    primary = _recompute_primary(db, job_id)
    db.commit()
    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="unassign",
        entity_type="job_assignment", entity_id=assignment_id,
        details={"job_id": job_id, "tech_id": row.tech_id, "primary_after": primary},
        request=request,
    )
    return {"status": "removed", "primary_after": primary}


@router.put(
    "/jobs/{job_id}/lead",
    dependencies=[Depends(require_permission("jobs.write"))],
)
def set_lead(
    job_id: str,
    request: Request,
    payload: LeadBody,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """D5 — set the lead tech. ``tech_id=None`` clears it."""
    _require_dispatch_role(user)
    tid = _tid(request)
    uid = _uid(user)

    rows = _list_active(db, job_id)
    if not rows:
        raise HTTPException(status_code=404, detail="job has no assignments")

    target_tech = payload.tech_id
    if target_tech is not None:
        if not any(r.tech_id == target_tech for r in rows):
            raise HTTPException(
                status_code=400,
                detail=f"tech {target_tech} is not assigned to this job",
            )

    # Single UPDATE — set TRUE for the chosen tech, FALSE for everyone else.
    if target_tech is None:
        db.execute(
            text(
                "UPDATE job_assignments SET is_lead = :f "
                "WHERE job_id = :j AND deleted_at IS NULL"
            ),
            {"j": job_id, "f": False},
        )
    else:
        db.execute(
            text(
                "UPDATE job_assignments SET is_lead = (tech_id = :t) "
                "WHERE job_id = :j AND deleted_at IS NULL"
            ),
            {"t": target_tech, "j": job_id},
        )
    db.flush()
    # text() UPDATEs bypass the ORM identity map; expire so
    # _recompute_primary sees the new is_lead values, not stale cache.
    db.expire_all()
    primary = _recompute_primary(db, job_id)
    db.commit()
    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="set_lead",
        entity_type="job", entity_id=job_id,
        details={"lead_tech_id": target_tech, "primary_after": primary},
        request=request,
    )
    return {"job_id": job_id, "lead_tech_id": target_tech, "primary_after": primary}


# ---------------------------------------------------------------------------
# Per-tech state stamps — called by mobile.py state-machine handlers.
# ---------------------------------------------------------------------------


def stamp_tech_state(
    db: Session,
    *,
    job_id: str,
    tech_id: str,
    state: str,
    when: datetime,
) -> JobAssignment | None:
    """D2 — stamp the calling tech's per-tech timestamp.

    Returns the JobAssignment row that was updated, or None if the tech
    has no assignment for this job (legacy single-tech path: caller can
    lazily create one on first state stamp, but we don't auto-create
    here — that's the caller's policy).

    ``state`` ∈ ``{"en_route", "arrived", "complete"}``. Stamps the
    corresponding column when it's still NULL; idempotent for repeat
    taps. Caller commits.
    """
    if state not in {"en_route", "arrived", "complete"}:
        raise ValueError(f"unknown state: {state!r}")
    row = db.execute(
        select(JobAssignment).where(
            JobAssignment.job_id == job_id,
            JobAssignment.tech_id == tech_id,
            JobAssignment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if state == "en_route" and row.en_route_at is None:
        row.en_route_at = when
    elif state == "arrived" and row.arrived_at is None:
        row.arrived_at = when
    elif state == "complete" and row.completed_at is None:
        row.completed_at = when
    return row


def ensure_assignment_for_legacy_job(
    db: Session,
    *,
    job_id: str,
    tech_id: str,
    user_id: str | None = None,
) -> JobAssignment:
    """Lazy back-fill: when a single-tech-era job has Job.assigned_to set
    but no JobAssignment row, create one so per-tech stamps land somewhere.

    Called from mobile state-machine handlers as a safety net for
    pre-Phase-1.4 jobs that haven't been touched by the back-fill
    migration yet. Idempotent. Caller commits.
    """
    existing = db.execute(
        select(JobAssignment).where(
            JobAssignment.job_id == job_id,
            JobAssignment.tech_id == tech_id,
            JobAssignment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = JobAssignment(
        id=str(uuid4()),
        job_id=job_id,
        tech_id=tech_id,
        user_id=user_id,
        is_lead=False,
        assigned_at=datetime.now(timezone.utc),
        assigned_by="system_lazy_backfill",
    )
    db.add(row)
    db.flush()
    return row


def is_lead_for_job(db: Session, *, job_id: str, tech_id: str) -> bool:
    """D4 helper — is this tech the lead on this job?

    Returns False if the tech has no assignment OR if no lead is set.
    """
    row = db.execute(
        select(JobAssignment.is_lead).where(
            JobAssignment.job_id == job_id,
            JobAssignment.tech_id == tech_id,
            JobAssignment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    return bool(row)


def has_any_lead(db: Session, *, job_id: str) -> bool:
    """Distinguishes "no lead set" from "this tech is not lead." When
    there is no lead at all, D4's gate falls back to permissive (any
    assigned tech can complete) — otherwise a misconfigured tenant could
    lock every job from completing."""
    row = db.execute(
        text(
            "SELECT 1 FROM job_assignments "
            "WHERE job_id = :j AND deleted_at IS NULL AND is_lead = :t LIMIT 1"
        ),
        {"j": job_id, "t": True},
    ).scalar()
    return bool(row)
