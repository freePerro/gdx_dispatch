"""Numbering API — read/write the per-tenant job number format + counter.

``TenantSettings`` owns the columns; endpoints
are admin-gated because changing the format mid-stream can collide with
existing numbers if the tenant rewinds the counter."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.settings_audit import audited_settings_upsert
from gdx_dispatch.core.settings_row import read_settings_row
from gdx_dispatch.modules.numbering.service import NumberingExhausted, apply_template, first_free_seq
from gdx_dispatch.modules.numbering.service import preview as render_preview
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/numbering", tags=["numbering"])


class NumberingConfigOut(BaseModel):
    job_number_format: str
    job_number_next_seq: int
    job_number_year_seen: int | None = None
    preview: str


class NumberingConfigIn(BaseModel):
    job_number_format: str = Field(..., min_length=1, max_length=200)
    job_number_next_seq: int = Field(..., ge=1)


def _tenant_uuid(request: Request) -> UUID:
    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        return UUID(tid)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid tenant context") from exc


_COLS = ("job_number_format", "job_number_next_seq", "job_number_year_seen")


def _read_row(db: Session, tid: UUID) -> dict[str, Any]:
    row = read_settings_row(db, tid, _COLS)
    fmt = row[0] or "JOB-{year}-{seq:03d}"
    seq = int(row[1] or 1)
    yr = row[2]
    return {
        "job_number_format": fmt,
        "job_number_next_seq": seq,
        "job_number_year_seen": yr,
        "preview": render_preview(fmt, seq, customer_name="Becky Meinecke"),
    }


@router.get("/config", response_model=NumberingConfigOut)
def get_config(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _ = user
    tid = _tenant_uuid(request)
    return _read_row(db, tid)


@router.patch("/config", response_model=NumberingConfigOut)
def update_config(
    payload: NumberingConfigIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin or owner required")
    tid = _tenant_uuid(request)
    # Validate the template renders without raising.
    try:
        render_preview(payload.job_number_format, payload.job_number_next_seq)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid format template: {exc}") from exc

    # Refuse a counter that would issue a number already on a job
    # (GDXA-924): the allocator would skip it anyway, but a setting that says
    # one thing and does another is how the counter got behind unnoticed.
    # Rendered with no customer, so a {customer_initials} template is checked
    # for the initial-less number only; the allocator's skip covers the rest.
    fmt, seq = payload.job_number_format, payload.job_number_next_seq
    year = datetime.now(UTC).year
    # A format that renders the same number for every seq (no {seq}) numbers
    # one job; the allocator skips the taken number, finds no other, and every
    # later job is created unnumbered with only a log line to show for it.
    if apply_template(fmt, seq, year=year) == apply_template(fmt, seq + 1, year=year):
        raise HTTPException(
            status_code=400,
            detail="the format must include {seq}, or every job after the first gets the same number",
        )
    try:
        free_seq, free_number = first_free_seq(db, fmt, seq, year=year)
    except NumberingExhausted as exc:
        raise HTTPException(
            status_code=400,
            detail=f"format {fmt!r} cannot produce a job number that is not already in use",
        ) from exc
    if free_seq != seq:
        taken = apply_template(fmt, seq, year=year)
        raise HTTPException(
            status_code=400,
            detail=(
                f"{taken} is already on a job. "
                f"The next free number is {free_number} (next number {free_seq})."
            ),
        )

    # Invariant #1. This is the change most likely to need forensics: the
    # router's own docstring warns that rewinding the counter collides with
    # job numbers already issued.
    return audited_settings_upsert(
        db,
        request,
        user,
        tenant_id=tid,
        values={
            "job_number_format": payload.job_number_format,
            "job_number_next_seq": payload.job_number_next_seq,
        },
        action="numbering_config_updated",
        read=_read_row,
    )
