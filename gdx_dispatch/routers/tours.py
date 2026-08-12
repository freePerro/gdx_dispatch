"""Tours router — per-user in-app tour progress.

GET  /api/me/tours                       — current progress + catalog meta
POST /api/me/tours/{tour_id}/start       — record start
POST /api/me/tours/{tour_id}/complete    — record completion
POST /api/me/tours/{tour_id}/skip        — record skip
POST /api/me/tours/{tour_id}/step        — update last_step

Tenant-plane: isolation is the DB connection itself. No tenant_id column on
UserTourProgress per CLAUDE.md three-plane invariant.

Graceful degradation: if the `user_tour_progress` table doesn't exist on
the tenant DB (it is created by `TenantBase.metadata.create_all()`, so this
means a DB that predates the model and never got the hand-DDL), reads
return empty progress and writes are no-ops with a warning log. Tours
fire for everyone in that case — degraded mode is "tour shows every
time," which is annoying but not broken.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import UserTourProgress
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/me/tours", tags=["tours"])

_VALID_STATUSES = ("started", "completed", "skipped")
_TOUR_ID_RE = r"^[a-z0-9][a-z0-9_\-]{0,63}$"


class StepIn(BaseModel):
    step_index: int = Field(ge=0, le=200)


class ProgressIn(BaseModel):
    """Optional payload on start/complete/skip — lets the client tell the
    server the catalog version of the tour being recorded. Bumping a
    tour's version in tours/catalog.js + sending the new int here is
    what triggers the row's `version` to advance + `status` to reset
    on the next /start call, so a rewritten tour re-fires for users
    who saw the old one."""
    version: int | None = Field(default=None, ge=1, le=1000)


class StatusOut(BaseModel):
    tour_id: str
    status: str
    version: int
    last_step: int | None
    started_at: datetime | None
    completed_at: datetime | None


class ToursOut(BaseModel):
    progress: dict[str, StatusOut]
    available: bool = True


def _user_uuid(user: dict[str, Any]) -> UUID:
    raw = user.get("id") or user.get("user_id") or user.get("sub")
    if raw is None:
        raise HTTPException(status_code=401, detail="missing user id")
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid user id shape") from e


def _validate_tour_id(tour_id: str) -> str:
    import re
    if not re.match(_TOUR_ID_RE, tour_id):
        raise HTTPException(status_code=400, detail="invalid tour_id")
    return tour_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_status_out(row: UserTourProgress) -> StatusOut:
    return StatusOut(
        tour_id=row.tour_id,
        status=row.status,
        version=row.version,
        last_step=row.last_step,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


@router.get("", response_model=ToursOut)
def list_tours(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> ToursOut:
    user_id = _user_uuid(current_user)
    try:
        rows = db.execute(
            select(UserTourProgress).where(UserTourProgress.user_id == user_id)
        ).scalars().all()
    except (ProgrammingError, OperationalError) as e:
        log.warning("tours_table_missing tenant_user=%s err=%s", user_id, type(e).__name__)
        db.rollback()
        return ToursOut(progress={}, available=False)
    return ToursOut(
        progress={r.tour_id: _to_status_out(r) for r in rows},
        available=True,
    )


def _upsert_progress(
    db: Session,
    user_id: UUID,
    tour_id: str,
    *,
    status: str | None = None,
    last_step: int | None = None,
    version: int | None = None,
) -> StatusOut | None:
    """Upsert a row for (user_id, tour_id) with these semantics:

    - **New row:** insert with the given status/version/last_step.
    - **Existing row, version is newer:** treat as a re-run after a catalog
      rewrite — set version, reset status to whatever the caller asked
      for (start/complete/skip), clear completed_at + last_step.
    - **Existing row, version is same or older:** apply status/last_step
      ONLY if it's a "forward" move. Specifically, do NOT downgrade a
      `completed`/`skipped` row back to `started` on a stale /start call,
      since a stale browser tab firing /start should not clobber a fresh
      completion on another device.

    Returns the resulting row as a StatusOut, or None if the tenant DB
    can't service the call (missing table / connection issue) — the
    frontend treats None as "fell back to localStorage."
    """
    try:
        row = db.execute(
            select(UserTourProgress).where(
                UserTourProgress.user_id == user_id,
                UserTourProgress.tour_id == tour_id,
            )
        ).scalar_one_or_none()
        if row is None:
            new_row = UserTourProgress(
                user_id=user_id,
                tour_id=tour_id,
                status=status or "started",
                version=version or 1,
                last_step=last_step,
                completed_at=_utcnow() if status == "completed" else None,
            )
            db.add(new_row)
            try:
                db.commit()
                db.refresh(new_row)
                return _to_status_out(new_row)
            except IntegrityError:
                # Lost the insert race: a concurrent request created the
                # (user_id, tour_id) row between our SELECT and INSERT and the
                # unique constraint fired. The global handler would turn this
                # into a 409. Roll back our failed insert, re-load the winner's
                # row, and FALL THROUGH to the merge path below — so this
                # caller's mutation (complete / skip / step) still lands.
                # Returning the winner's row unchanged would silently drop it:
                # a raced /complete would report "started" and the tour would
                # re-fire forever. _upsert_progress serves all four verbs, not
                # just /start.
                db.rollback()
                row = db.execute(
                    select(UserTourProgress).where(
                        UserTourProgress.user_id == user_id,
                        UserTourProgress.tour_id == tour_id,
                    )
                ).scalar_one_or_none()
                if row is None:
                    # On Postgres (READ COMMITTED — what GDX runs) the row the
                    # winner committed is visible to this fresh SELECT, so this
                    # is effectively unreachable. Under snapshot isolation
                    # (e.g. MySQL REPEATABLE READ) the re-SELECT could still see
                    # nothing; rather than crash _to_status_out(None) we return
                    # None, which the frontend treats as "fell back to
                    # localStorage" — a graceful degrade, not an error.
                    return None

        # Existing row (or the row the race winner just created) — decide how
        # to merge this caller's requested change.
        is_upgrade = version is not None and version > row.version
        if is_upgrade:
            row.version = version
            row.status = status or "started"
            row.completed_at = _utcnow() if status == "completed" else None
            row.last_step = None if status in ("started", None) else last_step
        else:
            # Same or older version: forward-only writes.
            terminal_states = ("completed", "skipped")
            if status is not None and not (
                status == "started" and row.status in terminal_states
            ):
                row.status = status
                if status == "completed":
                    row.completed_at = _utcnow()
            if last_step is not None:
                row.last_step = last_step
        db.commit()
        db.refresh(row)
        return _to_status_out(row)
    except (ProgrammingError, OperationalError) as e:
        log.warning(
            "tours_write_skipped tenant_user=%s tour=%s err=%s",
            user_id, tour_id, type(e).__name__,
        )
        db.rollback()
        return None


@router.post("/{tour_id}/start", response_model=StatusOut | None)
def start_tour(
    tour_id: str,
    payload: ProgressIn | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> StatusOut | None:
    tour_id = _validate_tour_id(tour_id)
    user_id = _user_uuid(current_user)
    version = payload.version if payload else None
    return _upsert_progress(db, user_id, tour_id, status="started", version=version)


@router.post("/{tour_id}/complete", response_model=StatusOut | None)
def complete_tour(
    tour_id: str,
    payload: ProgressIn | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> StatusOut | None:
    tour_id = _validate_tour_id(tour_id)
    user_id = _user_uuid(current_user)
    version = payload.version if payload else None
    return _upsert_progress(db, user_id, tour_id, status="completed", version=version)


@router.post("/{tour_id}/skip", response_model=StatusOut | None)
def skip_tour(
    tour_id: str,
    payload: ProgressIn | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> StatusOut | None:
    tour_id = _validate_tour_id(tour_id)
    user_id = _user_uuid(current_user)
    version = payload.version if payload else None
    return _upsert_progress(db, user_id, tour_id, status="skipped", version=version)


@router.post("/{tour_id}/step", response_model=StatusOut | None)
def update_step(
    tour_id: str,
    payload: StepIn,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> StatusOut | None:
    tour_id = _validate_tour_id(tour_id)
    user_id = _user_uuid(current_user)
    return _upsert_progress(db, user_id, tour_id, last_step=payload.step_index)
