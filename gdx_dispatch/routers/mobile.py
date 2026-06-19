from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from datetime import date as date_type
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID as _UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy import text as _text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.audit import log_audit_event, log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    Job,
    JobNote,
    MobileSyncAction,
    Tag,
    TagAssignment,
    Technician,
    TimeclockEntry,
)
from gdx_dispatch.modules.inventory.models import JobPart, Part

log = logging.getLogger(__name__)

try:
    from gdx_dispatch.routers.auth import get_current_user
except Exception:
    log.exception("mobile_auth_import_failed_using_fallback")

    async def get_current_user() -> dict[str, Any]:
        return {}


router = APIRouter(prefix="/api/mobile", tags=["mobile"], dependencies=[Depends(require_module("mobile"))])
_VALID_JOB_STATUSES = {"en_route", "on_site", "completed"}
_MOBILE_JOB_STATUS_VALUES = {"en_route", "on_site", "completed", "cancelled"}


class JobStatusUpdate(BaseModel):
    status: str | None = Field(default=None, max_length=50)


class SignatureBody(BaseModel):
    # data URL / base64 signature image — cap at ~1MB raw
    signature_data: str = Field(min_length=1, max_length=1_400_000)
    signed_by: str | None = Field(default=None, max_length=200)


class NoteBody(BaseModel):
    note: str = Field(min_length=1, max_length=5000)


class EnRouteBody(BaseModel):
    eta_minutes: int | None = Field(default=None, ge=0, le=1440)


class ArrivedBody(BaseModel):
    """Sprint tech_mobile S1-B1 — geo-tagged "I'm here" stamp.

    All fields optional so a tech who hasn't granted location permission
    can still mark arrival; the arrival timestamp + status advance work
    without coordinates.
    """

    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0, le=1_000_000)


class CompleteBody(BaseModel):
    completion_notes: str | None = Field(default=None, max_length=5000)
    signature_data: str | None = Field(default=None, max_length=1_400_000)
    signed_by: str | None = Field(default=None, max_length=200)


# Sprint tech_mobile S1-B2 — forward-only state machine.
# The Job.dispatch_status enum is (unassigned, assigned, en_route, on_site,
# done). The lifecycle below is the order a tech advances through:
#   unassigned → assigned → en_route → on_site → done
# Each value's index is its rank; a target with a strictly higher rank
# than current is allowed. Equal-rank (idempotent re-tap) is allowed too —
# a tech double-tapping "On my way" must NOT 400. Backward transitions
# require a separate dispatcher-only path (not in scope for this slice).
_DISPATCH_STATUS_ORDER: list[str] = [
    "unassigned",
    "assigned",
    "en_route",
    "on_site",
    "done",
]


def _dispatch_status_rank(value: str | None) -> int:
    """Return the lifecycle index for a dispatch_status value.

    Unknown values rank at -1 so any forward target is allowed; this is
    defensive — historical rows with mid-flight enum values from before
    the lifecycle was tightened should not block forward progress.
    """
    if value is None:
        return -1
    try:
        return _DISPATCH_STATUS_ORDER.index(value)
    except ValueError:
        return -1


def _validate_forward_transition(current: str | None, target: str) -> None:
    """Raise HTTPException(400) if the transition would move backward.

    Idempotent re-taps (current == target) succeed silently. Forward
    advances always succeed. Backward transitions are rejected — a tech
    who's already "on_site" cannot revert to "en_route" through the
    mobile state-advance endpoints (that's a dispatcher action).
    """
    if target not in _DISPATCH_STATUS_ORDER:
        raise HTTPException(
            status_code=400, detail=f"unknown target dispatch_status: {target!r}"
        )
    if _dispatch_status_rank(target) < _dispatch_status_rank(current):
        raise HTTPException(
            status_code=400,
            detail=(
                f"cannot transition from {current!r} back to {target!r}; "
                f"backward transitions go through dispatch."
            ),
        )


class LocationBody(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0, le=1_000_000)
    timestamp: str | None = Field(default=None, max_length=64)


class PartUsageItem(BaseModel):
    part_id: str = Field(min_length=1, max_length=64)
    qty: int = Field(ge=0, le=1_000_000)


class PartsUsedBody(BaseModel):
    parts: list[PartUsageItem] = Field(max_length=500)


class SyncAction(BaseModel):
    type: str = Field(min_length=1, max_length=50)
    entity_id: str | None = Field(default=None, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)
    queued_at: str | None = Field(default=None, max_length=64)


class SyncBatchBody(BaseModel):
    actions: list[SyncAction] = Field(max_length=1000)


def jsonable_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(content))


def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", {}) or {}
    return str(tenant.get("id", ""))


def _user_id(user: dict[str, Any]) -> str:
    uid = user.get("user_id") or user.get("id")
    return str(uid or "").strip()


def _table_columns(db: Session, table_name: str) -> set[str]:
    try:
        # Detect dialect
        dialect = db.bind.dialect.name if db.bind else "unknown"
        if dialect == "sqlite":
            rows = db.execute(_text(f"PRAGMA table_info({table_name})")).mappings().all()
            return {str(r.get("name") or "") for r in rows}
        else:
            rows = db.execute(
                _text("SELECT column_name FROM information_schema.columns WHERE table_name = :t AND table_schema = 'public'"),
                {"t": table_name},
            ).scalars().all()
            return set(rows) if rows else set()
    except Exception:
        log.exception("table_columns_introspection_failed")
        return set()


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except Exception:  # returns None if datetime parsing fails
        log.exception("parse_datetime_failed")
        return None


def _get_job(db: Session, tenant_id: str, job_id: str) -> dict[str, Any] | None:
    row = db.execute(
        _text(
            """
            SELECT id, customer_id, title, description, dispatch_status,
                   scheduled_at, completed_at, signature_data, signed_by,
                   signed_at, created_at
            FROM jobs
            WHERE id = :job_id
              AND company_id = :tenant_id
              AND deleted_at IS NULL
            """
        ),
        {"job_id": job_id, "tenant_id": tenant_id},
    ).mappings().first()
    if not row:
        return None
    return dict(row)


def _job_belongs_to_user(
    db: Session,
    tenant_id: str,
    job_id: str,
    user_id: str | None,
    technician_id: str | None,
) -> bool:
    if not job_id or not tenant_id:
        return False

    if user_id:
        row = db.execute(
            _text(
                """
                SELECT 1
                FROM jobs
                WHERE id = :job_id
                  AND company_id = :tenant_id
                  AND deleted_at IS NULL
                  AND assigned_to = :user_id
                LIMIT 1
                """
            ),
            {"job_id": job_id, "tenant_id": tenant_id, "user_id": user_id},
        ).scalar()
        if row:
            return True

    if technician_id:
        row = db.execute(
            _text(
                """
                SELECT 1
                FROM appointments
                WHERE job_id = :job_id
                  AND company_id = :tenant_id
                  AND tech_id = :technician_id
                  AND deleted_at IS NULL
                LIMIT 1
                """
            ),
            {
                "job_id": job_id,
                "tenant_id": tenant_id,
                "technician_id": technician_id,
            },
        ).scalar()
        if row:
            return True

    return False


def _get_technician_id(db: Session, tenant_id: str, user_id: str) -> str | None:
    row = (
        db.query(Technician.id)
        .filter(
            Technician.user_id == user_id,
            Technician.active.isnot(False),
        )
        .order_by(Technician.created_at.desc())
        .first()
    )
    if not row:
        return None
    return str(row[0])


def _image_suffix(file: UploadFile) -> str:
    content_type = (file.content_type or "").lower()
    filename = (file.filename or "").lower()
    if filename.endswith(".png") or "png" in content_type:
        return "png"
    if filename.endswith(".webp") or "webp" in content_type:
        return "webp"
    if filename.endswith(".gif") or "gif" in content_type:
        return "gif"
    return "jpg"


def _build_navigation_link(address: str | None) -> str | None:
    addr = (address or "").strip()
    if not addr:
        return None
    return f"https://maps.google.com/?q={quote_plus(addr)}"


def _validate_signature_data(signature_data: str) -> bool:
    data = (signature_data or "").strip()
    if not data.startswith("data:image/") or "," not in data:
        return False
    try:
        base64.b64decode(data.split(",", 1)[1])
        return True
    except Exception:  # Validation failure is expected and handled by returning False.
        log.exception("validate_signature_data_failed")
        return False


def _audit_state_change(
    db: Session,
    *,
    event_type: str,
    actor_id: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
    request: Request,
    actor_role: str | None,
) -> None:
    actor = actor_id or "system"
    try:
        asyncio.run(
            log_audit_event(
                db,
                event_type,
                actor,
                entity_type,
                entity_id,
                payload,
                request=request,
                actor_role=actor_role,
            )
        )
    except Exception:
        log.exception("audit_state_change_log_event_failed")

    # Keep audit logging resilient in lightweight test schemas where ORM mapping
    # and ad-hoc table DDL can diverge.
    try:
        existing = db.execute(
            _text(
                """
                SELECT 1
                FROM audit_logs
                WHERE event_type = :event_type
                  AND entity_type = :entity_type
                  AND entity_id = :entity_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
            },
        ).scalar()
        if not existing:
            prev_hash = "0" * 64
            digest = hashlib.sha256(
                f"{prev_hash}{event_type}{actor}{entity_id}{json.dumps(payload, sort_keys=True, default=str)}".encode()
            ).hexdigest()
            db.execute(
                _text(
                    """
                    INSERT INTO audit_logs (
                        id, event_type, actor_id, actor_role, entity_type, entity_id,
                        payload, created_at, hash, prev_hash
                    ) VALUES (
                        :id, :event_type, :actor_id, :actor_role, :entity_type, :entity_id,
                        :payload, :created_at, :hash, :prev_hash
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "event_type": event_type,
                    "actor_id": actor,
                    "actor_role": actor_role,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "payload": json.dumps(payload, default=str),
                    "created_at": datetime.now(UTC),
                    "hash": digest,
                    "prev_hash": prev_hash,
                },
            )
    except Exception:
        log.exception("audit_state_change_fallback_insert_failed")


def _find_open_time_entry(
    db: Session,
    tenant_id: str,
    user_id: str,
    *,
    job_id: str | None,
    entry_type: str,
) -> dict[str, Any] | None:
    cols = _table_columns(db, "time_entries")
    where = ["company_id = :tenant_id", "user_id = :user_id", "clock_out IS NULL"]
    params: dict[str, Any] = {"tenant_id": tenant_id, "user_id": user_id}
    if "entry_type" in cols:
        where.append("entry_type = :entry_type")
        params["entry_type"] = entry_type
    if job_id is None:
        where.append("job_id IS NULL")
    else:
        where.append("job_id = :job_id")
        params["job_id"] = job_id
    row = db.execute(
        _text(
            f"""
            SELECT id, clock_in, job_id
            FROM time_entries
            WHERE {' AND '.join(where)}
            ORDER BY clock_in DESC
            LIMIT 1
            """
        ),
        params,
    ).mappings().first()
    return dict(row) if row else None


def _create_time_entry(
    db: Session,
    tenant_id: str,
    user_id: str,
    *,
    technician_id: str | None,
    job_id: str | None,
    entry_type: str,
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    entry_id = str(uuid.uuid4())
    cols = _table_columns(db, "time_entries")
    insert_values: dict[str, Any] = {
        "id": entry_id,
        "company_id": tenant_id,
        "user_id": user_id,
        "job_id": job_id,
        "clock_in": now,
        "clock_out": None,
        "duration_minutes": None,
        "created_at": now,
    }
    if "tech_id" in cols:
        insert_values["tech_id"] = technician_id or user_id
    if "entry_type" in cols:
        insert_values["entry_type"] = entry_type

    names = list(insert_values.keys())
    db.execute(
        _text(
            f"""
            INSERT INTO time_entries ({', '.join(names)})
            VALUES ({', '.join([f':{n}' for n in names])})
            """
        ),
        insert_values,
    )
    return entry_id, now


def _close_open_time_entry(
    db: Session,
    tenant_id: str,
    user_id: str,
    *,
    job_id: str | None,
    entry_type: str,
) -> tuple[dict[str, Any] | None, datetime, int]:
    row = _find_open_time_entry(db, tenant_id, user_id, job_id=job_id, entry_type=entry_type)
    now = datetime.now(UTC)
    if not row:
        return None, now, 0

    clock_in = row.get("clock_in")
    if isinstance(clock_in, str):
        clock_in = _parse_datetime(clock_in) or now
    if clock_in is None:
        clock_in = now

    delta_seconds = max((now - clock_in).total_seconds(), 0)
    duration_minutes = int(round(delta_seconds / 60))
    db.execute(
        _text(
            """
            UPDATE time_entries
            SET clock_out = :clock_out,
                duration_minutes = :duration_minutes
            WHERE id = :id
            """
        ),
        {
            "id": row["id"],
            "clock_out": now,
            "duration_minutes": duration_minutes,
        },
    )
    return row, now, duration_minutes



def _sync_fingerprint(action: SyncAction) -> str:
    raw = f"{action.type}|{action.entity_id or ''}|{action.queued_at or ''}|{json.dumps(action.data, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()
@router.get("/clock-status", response_model=None)
def mobile_clock_status(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Day-level clock status. Reads ``timeclock_entries_router`` (canonical)
    so mobile and the desktop ``/timeclock`` view always agree.

    S3 (sprint-mobile-views) 2026-04-29 — previously read ``time_entries``
    first with a fallback. The dual-table read combined with mobile-only
    writes to ``time_entries`` made clock-out 404 whenever a tech clocked
    in from desktop. Day-level state now lives in one table.
    """
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id) or user_id

    entry = db.execute(
        select(TimeclockEntry)
        .where(
            TimeclockEntry.tenant_id == tenant_id,
            TimeclockEntry.technician_id == technician_id,
            TimeclockEntry.deleted_at.is_(None),
            TimeclockEntry.clock_out_at.is_(None),
        )
        .order_by(TimeclockEntry.clock_in_at.desc())
        .limit(1)
    ).scalars().first()

    if not entry:
        return jsonable_response(
            {"clocked_in": False, "since": None, "current_job_id": None}
        )

    return jsonable_response(
        {
            "clocked_in": True,
            "since": str(entry.clock_in_at),
            "current_job_id": None,
        }
    )


@router.get("/schedule", response_model=None)
def get_mobile_schedule(
    request: Request,
    date: date_type | None = Query(default=None),
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id)
    target_date = date or datetime.now(UTC).date()
    if not technician_id:
        return jsonable_response({"date": target_date.isoformat(), "tech_id": None, "count": 0, "jobs": []})

    rows = db.execute(
        _text(
            """
            SELECT j.id, j.title, j.description, j.dispatch_status, j.scheduled_at,
                   a.id AS appointment_id, a.start_at, a.end_at,
                   c.id AS customer_id, c.name AS customer_name, c.phone AS customer_phone,
                   c.address AS customer_address
            FROM appointments a
            JOIN jobs j ON j.id = a.job_id
            LEFT JOIN customers c ON c.id = j.customer_id
            WHERE a.company_id = :tenant_id
              AND j.company_id = :tenant_id
              AND a.tech_id = :technician_id
              AND DATE(a.start_at) = :target_date
              AND a.deleted_at IS NULL
              AND j.deleted_at IS NULL
            ORDER BY a.start_at ASC
            """
        ),
        {
            "tenant_id": tenant_id,
            "technician_id": technician_id,
            "target_date": target_date.isoformat(),
        },
    ).mappings().all()

    jobs: list[dict[str, Any]] = []
    for row in rows:
        customer = {
            "id": row.get("customer_id"),
            "name": row.get("customer_name"),
            "phone": row.get("customer_phone"),
            "address": row.get("customer_address"),
        }
        jobs.append(
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "description": row.get("description"),
                "dispatch_status": row.get("dispatch_status"),
                "scheduled_at": row.get("scheduled_at"),
                "appointment_id": row.get("appointment_id"),
                "time_window": {
                    "start": row.get("start_at"),
                    "end": row.get("end_at"),
                },
                "customer": customer,
                "navigation_link": _build_navigation_link(customer.get("address")),
            }
        )

    # P1-9 fix 2026-04-27: when there are no ``appointments`` rows for the tech
    # (the QB import populated ``jobs.scheduled_at`` but never created
    # appointments), fall back to ``jobs`` directly so /mobile and /dispatch
    # agree. Skipped when appointments-derived rows already returned data.
    if not jobs:
        fb_rows = db.execute(
            _text(
                """
                SELECT j.id, j.title, j.description, j.dispatch_status, j.scheduled_at,
                       c.id AS customer_id, c.name AS customer_name,
                       c.phone AS customer_phone, c.address AS customer_address
                FROM jobs j
                LEFT JOIN customers c ON c.id = j.customer_id
                WHERE j.company_id = :tenant_id
                  AND j.assigned_to = :technician_id
                  AND j.deleted_at IS NULL
                  AND j.scheduled_at IS NOT NULL
                  AND DATE(j.scheduled_at) = :target_date
                ORDER BY j.scheduled_at ASC
                """
            ),
            {
                "tenant_id": tenant_id,
                "technician_id": technician_id,
                "target_date": target_date.isoformat(),
            },
        ).mappings().all()
        for row in fb_rows:
            customer = {
                "id": row.get("customer_id"),
                "name": row.get("customer_name"),
                "phone": row.get("customer_phone"),
                "address": row.get("customer_address"),
            }
            jobs.append(
                {
                    "id": row.get("id"),
                    "title": row.get("title"),
                    "description": row.get("description"),
                    "dispatch_status": row.get("dispatch_status"),
                    "scheduled_at": row.get("scheduled_at"),
                    "appointment_id": None,
                    "time_window": {"start": row.get("scheduled_at"), "end": None},
                    "customer": customer,
                    "navigation_link": _build_navigation_link(customer.get("address")),
                }
            )

    return jsonable_response(
        {
            "date": target_date.isoformat(),
            "tech_id": technician_id,
            "count": len(jobs),
            "jobs": jobs,
        }
    )


# ---------------------------------------------------------------------------
# Sprint tech_mobile S1-A1 + S1-A7 — today's route (rich payload).
#
# Distinct from the legacy /schedule endpoint: this one routes through the
# ORM (joins customer tags via TagAssignment + Tag for alert surfacing,
# exposes Job.priority alongside dispatch_status). The new mobile UI
# consumes this; /schedule stays in place for the legacy e2e test and any
# external clients still wired to the older shape.
# ---------------------------------------------------------------------------


def _customer_tags_map(
    db: Session, tenant_id: str, customer_ids: list[Any]
) -> dict[str, list[dict[str, str]]]:
    """Return {customer_id_str: [{name, color}, ...]} for the given customers."""
    if not customer_ids:
        return {}
    cid_strs = [str(cid) for cid in customer_ids]
    rows = (
        db.query(TagAssignment, Tag)
        .join(Tag, Tag.id == TagAssignment.tag_id)
        .filter(
            TagAssignment.company_id == tenant_id,
            TagAssignment.entity_type == "customer",
            TagAssignment.entity_id.in_(cid_strs),
        )
        .all()
    )
    out: dict[str, list[dict[str, str]]] = {}
    for ta, tag in rows:
        out.setdefault(str(ta.entity_id), []).append({"name": tag.name, "color": tag.color})
    return out


def _job_card(
    job: Job,
    customer: Customer | None,
    appointment: Appointment | None,
    tags: list[dict[str, str]],
) -> dict[str, Any]:
    """Assemble one today's-route card from ORM rows.

    ORM-routed so customer fields land via the SQLAlchemy mapper and any
    future column-level processors (validators, TypeDecorators) fire
    consistently. Raw-SQL paths in /schedule and /my-jobs skip the mapper;
    cosmetic today (post-S122-1c name/phone/address are plain Text) but
    revisit if encryption-at-rest returns to those columns.
    """
    customer_payload: dict[str, Any] = {
        "id": str(customer.id) if customer is not None else None,
        "name": customer.name if customer is not None else None,
        "phone": customer.phone if customer is not None else None,
        "address": customer.address if customer is not None else None,
        "notes": customer.notes if customer is not None else None,
        "tags": tags,
    }
    # Alerts surface = the set of tag names. The seeded taxonomy uses
    # short codes (dog_warning, gate_code, …) directly as names so the
    # frontend can match on name without a separate alert-code field.
    alerts = sorted({t["name"] for t in tags})
    scheduled_at = (
        appointment.start_at if appointment is not None else job.scheduled_at
    )
    time_window = {
        "start": appointment.start_at if appointment is not None else job.scheduled_at,
        "end": appointment.end_at if appointment is not None else None,
    }
    # S1-A5 — surface lat/lng when the appointment has been geocoded
    # (Dispatch geocodes appointments on save). Coordinates absent → the
    # stop is omitted from the map view in the frontend; the list view is
    # unaffected.
    location: dict[str, float] | None = None
    if appointment is not None and appointment.lat is not None and appointment.lng is not None:
        try:
            location = {"lat": float(appointment.lat), "lng": float(appointment.lng)}
        except (TypeError, ValueError):
            location = None

    return {
        "id": str(job.id),
        "appointment_id": str(appointment.id) if appointment is not None else None,
        "title": job.title,
        "description": job.description,
        "service_type": job.job_type or "Service",
        "priority": job.priority or "Normal",
        "dispatch_status": job.dispatch_status,
        "scheduled_at": scheduled_at,
        "time_window": time_window,
        "customer": customer_payload,
        "alerts": alerts,
        "navigation_link": _build_navigation_link(
            customer.address if customer is not None else None
        ),
        "location": location,
    }


@router.get("/today", response_model=None)
async def get_mobile_today(
    request: Request,
    date: date_type | None = Query(default=None),
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Today's route for the calling tech — rich payload (alerts, tags, priority).

    S1-A2 — populates ``drive_time_to_next_seconds`` on every card except
    the last, gated on ``tech_mobile.drive_time_provider``. Setting=off
    leaves the field None across the board.
    """
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id)
    target_date = date or datetime.now(UTC).date()
    empty = {"date": target_date.isoformat(), "tech_id": None, "count": 0, "jobs": []}
    if not technician_id:
        return jsonable_response(empty)

    # 1) Today's appointments for this tech.
    appts = (
        db.query(Appointment)
        .filter(
            Appointment.company_id == tenant_id,
            Appointment.tech_id == technician_id,
            Appointment.deleted_at.is_(None),
            func.date(Appointment.start_at) == target_date,
        )
        .order_by(Appointment.start_at.asc())
        .all()
    )

    # 2) Pull jobs + customers via ORM (mapper-routed; see _job_card docstring).
    job_ids = [a.job_id for a in appts if a.job_id is not None]
    jobs_by_id: dict[Any, Job] = {}
    if job_ids:
        for j in (
            db.query(Job)
            .filter(Job.id.in_(job_ids), Job.deleted_at.is_(None))
            .all()
        ):
            jobs_by_id[j.id] = j

    fallback_used = False
    if not appts:
        # Fallback: tech has jobs scheduled for today but no appointment row
        # (QB-imported jobs can land in this state).
        fb_jobs = (
            db.query(Job)
            .filter(
                Job.company_id == tenant_id,
                Job.assigned_to == technician_id,
                Job.deleted_at.is_(None),
                Job.scheduled_at.isnot(None),
                func.date(Job.scheduled_at) == target_date,
            )
            .order_by(Job.scheduled_at.asc())
            .all()
        )
        for j in fb_jobs:
            jobs_by_id[j.id] = j
        fallback_used = bool(fb_jobs)

    customer_ids = list({j.customer_id for j in jobs_by_id.values() if j.customer_id})
    customers_by_id: dict[Any, Customer] = {}
    if customer_ids:
        for c in db.query(Customer).filter(Customer.id.in_(customer_ids)).all():
            customers_by_id[c.id] = c

    # 3) Tags per customer (alerts feed).
    tag_map = _customer_tags_map(db, tenant_id, customer_ids)

    # 4) Assemble cards in the order the iteration yielded.
    cards: list[dict[str, Any]] = []
    if not fallback_used and appts:
        for a in appts:
            job = jobs_by_id.get(a.job_id) if a.job_id else None
            if job is None:
                continue
            customer = customers_by_id.get(job.customer_id) if job.customer_id else None
            tags = tag_map.get(str(job.customer_id), []) if job.customer_id else []
            cards.append(_job_card(job, customer, a, tags))
    else:
        # Fallback path — order by Job.scheduled_at (already done in query).
        for job in jobs_by_id.values():
            customer = customers_by_id.get(job.customer_id) if job.customer_id else None
            tags = tag_map.get(str(job.customer_id), []) if job.customer_id else []
            cards.append(_job_card(job, customer, None, tags))

    # Phase 1.4 D1+D2 — multi-tech card decoration. For every card,
    # surface the full assignment list with per-tech state stamps so the
    # mobile UI can render "with [other tech]" + how far each tech has
    # progressed through the state machine. The lookup is a single bulk
    # query per request, not N+1.
    if cards:
        from gdx_dispatch.models.tenant_models import JobAssignment as _JA, Technician as _Tech

        card_job_ids_for_assignments = [c["id"] for c in cards]
        assignment_rows = (
            db.query(_JA, _Tech.name)
            .outerjoin(_Tech, _Tech.id == _JA.tech_id)
            .filter(
                _JA.job_id.in_(card_job_ids_for_assignments),
                _JA.deleted_at.is_(None),
            )
            .order_by(_JA.is_lead.desc(), _JA.assigned_at.asc())
            .all()
        )
        assignments_by_job: dict[str, list[dict[str, Any]]] = {}
        for row, tech_name in assignment_rows:
            assignments_by_job.setdefault(str(row.job_id), []).append({
                "tech_id": row.tech_id,
                "tech_name": tech_name or "",
                "is_lead": bool(row.is_lead),
                "en_route_at": row.en_route_at.isoformat() if row.en_route_at else None,
                "arrived_at": row.arrived_at.isoformat() if row.arrived_at else None,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            })
        for c in cards:
            c["assignments"] = assignments_by_job.get(c["id"], [])

    # Phase 1.3 C2 — parts summary per job (counts by status).
    if cards:
        from gdx_dispatch.models.tenant_models import JobPartNeeded as _JPN

        card_job_ids = [c["id"] for c in cards]
        rows = (
            db.query(_JPN.job_id, _JPN.status)
            .filter(_JPN.job_id.in_(card_job_ids))
            .all()
        )
        summary: dict[str, dict[str, int]] = {
            jid: {"total": 0, "needed": 0, "ordered": 0, "received": 0}
            for jid in card_job_ids
        }
        for jid, status in rows:
            sjid = str(jid)
            bucket = summary.setdefault(
                sjid, {"total": 0, "needed": 0, "ordered": 0, "received": 0}
            )
            bucket["total"] += 1
            key = (status or "needed").lower()
            if key in bucket:
                bucket[key] += 1
        for c in cards:
            c["parts_summary"] = summary.get(c["id"], {
                "total": 0, "needed": 0, "ordered": 0, "received": 0,
            })

    # S1-A2 — drive-time enrichment, gated on tech_mobile.drive_time_provider.
    from gdx_dispatch.core.drive_time import compute_drive_times
    from gdx_dispatch.core.tenant_mobile_settings import get_tenant_mobile_setting

    provider = get_tenant_mobile_setting(
        db, "tech_mobile.drive_time_provider", request=request
    )
    addresses = [(c.get("customer") or {}).get("address") or "" for c in cards]
    legs = await compute_drive_times(tenant_id, addresses, provider=provider)
    # legs[i] is the time FROM addresses[i-1] TO addresses[i]; we want
    # each card to know its drive-time to the NEXT stop, so shift by one
    # (last card has no next stop → None).
    for i, card in enumerate(cards):
        card["drive_time_to_next_seconds"] = legs[i + 1] if (i + 1) < len(legs) else None

    return jsonable_response(
        {
            "date": target_date.isoformat(),
            "tech_id": technician_id,
            "count": len(cards),
            "jobs": cards,
            "drive_time_provider": provider,
        }
    )


class _ReorderBody(BaseModel):
    appointment_ids: list[_UUID] = Field(default_factory=list, max_length=50)


@router.post("/today/reorder", response_model=None)
def reorder_mobile_today(
    payload: _ReorderBody,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """S1-A6 — tech reorders today's route.

    Authority is gated on ``tech_mobile.drag_reorder_authority``:

    - ``live`` (default) — rotates scheduled_at + Appointment.start_at /
      end_at among the calling tech's appointments so the relative time
      slots dispatch carved out are preserved, but which job lands in
      which slot follows the tech's new order. Audit-logged.
    - ``dispatch_approval`` — not yet implemented in this sprint slice;
      returns 501 with a clear next-step. Tenants who need this
      workflow should leave the setting on ``live`` until the
      pending-changes table lands.

    Validates that every appointment_id belongs to the calling tech for
    today's date; refuses partial or ambiguous reorderings.
    """
    from gdx_dispatch.core.tenant_mobile_settings import get_tenant_mobile_setting

    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id)
    if not technician_id:
        raise HTTPException(status_code=400, detail="caller is not a technician")

    authority = get_tenant_mobile_setting(
        db, "tech_mobile.drag_reorder_authority", request=request
    )
    if authority == "dispatch_approval":
        raise HTTPException(
            status_code=501,
            detail=(
                "drag_reorder_authority='dispatch_approval' is not yet "
                "implemented; set tech_mobile.drag_reorder_authority='live' "
                "to enable tech-side reordering."
            ),
        )
    if authority != "live":
        raise HTTPException(status_code=400, detail=f"unknown authority: {authority}")

    target_date = datetime.now(UTC).date()
    today_appts = (
        db.query(Appointment)
        .filter(
            Appointment.company_id == tenant_id,
            Appointment.tech_id == technician_id,
            Appointment.deleted_at.is_(None),
            func.date(Appointment.start_at) == target_date,
        )
        .order_by(Appointment.start_at.asc())
        .all()
    )
    by_id = {a.id: a for a in today_appts}

    submitted = list(payload.appointment_ids)
    if len(submitted) != len(today_appts):
        raise HTTPException(
            status_code=400,
            detail=(
                f"reorder must include every today-appointment for the tech "
                f"({len(today_appts)} expected, got {len(submitted)})"
            ),
        )
    if len(set(submitted)) != len(submitted):
        raise HTTPException(status_code=400, detail="duplicate appointment_ids")
    for aid in submitted:
        if aid not in by_id:
            raise HTTPException(
                status_code=400, detail=f"appointment {aid} not on today's route"
            )

    # Capture the relative slot grid before we rotate so the audit log
    # shows what changed. Rotating preserves the slot times; only the
    # job-to-slot assignment changes.
    original_slots = [
        {"id": str(a.id), "start_at": a.start_at.isoformat() if a.start_at else None}
        for a in today_appts
    ]
    slots = [(a.start_at, a.end_at) for a in today_appts]
    before_order = [str(a.id) for a in today_appts]
    after_order = [str(aid) for aid in submitted]
    if before_order == after_order:
        return {"ok": True, "changed": False, "order": after_order}

    for new_idx, aid in enumerate(submitted):
        appt = by_id[aid]
        start, end = slots[new_idx]
        appt.start_at = start
        appt.end_at = end
        # Keep Job.scheduled_at consistent with Appointment.start_at if
        # the appointment is linked to a Job — otherwise the next /today
        # fallback path would disagree with the appointment-driven view.
        if appt.job_id is not None:
            job = db.query(Job).filter(Job.id == appt.job_id).first()
            if job is not None:
                job.scheduled_at = start

    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            action="mobile_today_reordered",
            entity_type="mobile_route",
            entity_id=technician_id,
            details={
                "before": before_order,
                "after": after_order,
                "slots": original_slots,
                "authority": authority,
            },
            request=request,
        )
    except Exception:
        log.exception("mobile_today_reorder_audit_failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="audit failure — change rolled back")

    db.commit()
    return {"ok": True, "changed": True, "order": after_order}


@router.get("/jobs", response_model=None)
def mobile_all_my_jobs(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Tech-mobile "Jobs" tab — every job this tech is assigned to,
    across all dates, sorted recent-first. Distinct from /my-jobs which
    is the today-only legacy endpoint kept for backwards-compat. Reads
    assignment via Phase 1.4's JobAssignment table AND falls back to
    legacy Job.assigned_to so single-tech-era jobs still surface for
    the originally-assigned tech.
    """
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)
    technician_id = _get_technician_id(db, tenant_id, user_id)
    if not technician_id:
        return jsonable_response({"jobs": [], "count": 0, "tech_id": None})

    rows = db.execute(
        _text(
            """
            SELECT DISTINCT j.id, j.title, j.dispatch_status, j.scheduled_at,
                   j.priority, j.job_type, j.created_at,
                   c.name AS customer_name,
                   COALESCE(c.address, '') AS customer_address
            FROM jobs j
            LEFT JOIN customers c ON c.id = j.customer_id
            LEFT JOIN job_assignments ja
              ON CAST(ja.job_id AS TEXT) = CAST(j.id AS TEXT)
              AND ja.deleted_at IS NULL
            WHERE j.deleted_at IS NULL
              AND (
                CAST(j.assigned_to AS TEXT) = :tech_id
                OR ja.tech_id = :tech_id
              )
            ORDER BY j.scheduled_at DESC NULLS LAST, j.created_at DESC
            LIMIT 200
            """
        ),
        {"tech_id": str(technician_id)},
    ).mappings().all()

    jobs = []
    for r in rows:
        scheduled = r.get("scheduled_at")
        scheduled_iso = scheduled.isoformat() if isinstance(scheduled, datetime) else (
            _parse_datetime(str(scheduled)).isoformat() if scheduled else None
        )
        jobs.append({
            "id": str(r["id"]),
            "title": r.get("title") or "Service",
            "dispatch_status": r.get("dispatch_status") or "assigned",
            "priority": r.get("priority") or "Normal",
            "service_type": r.get("job_type") or "Service",
            "customer_name": r.get("customer_name") or "—",
            "customer_address": r.get("customer_address") or "",
            "scheduled_at": scheduled_iso,
        })
    return jsonable_response({"count": len(jobs), "jobs": jobs, "tech_id": technician_id})


@router.get("/my-jobs", response_model=None)
def mobile_my_jobs(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id)
    if not technician_id:
        return jsonable_response({"jobs": [], "count": 0, "tech_id": None, "date": datetime.now(UTC).date().isoformat()})

    target_date = datetime.now(UTC).date()
    rows = db.execute(
        _text(
            """
            SELECT j.id, j.status, j.priority,
                   c.name AS customer_name, COALESCE(c.address, '') AS address,
                   a.start_at
            FROM appointments a
            JOIN jobs j ON j.id = a.job_id
            LEFT JOIN customers c ON c.id = j.customer_id
            WHERE a.company_id = :tenant_id
              AND j.company_id = :tenant_id
              AND j.deleted_at IS NULL
              AND a.deleted_at IS NULL
              AND DATE(a.start_at) = :target_date
              AND a.tech_id = :technician_id
            ORDER BY a.start_at ASC
            """
        ),
        {
            "tenant_id": tenant_id,
            "target_date": target_date.isoformat(),
            "technician_id": technician_id,
        },
    ).mappings().all()

    jobs_list: list[dict[str, Any]] = []
    for row in rows:
        scheduled = row.get("start_at")
        if isinstance(scheduled, datetime):
            scheduled_time = scheduled.isoformat()
        else:
            parsed = _parse_datetime(str(scheduled) if scheduled is not None else None)
            scheduled_time = parsed.isoformat() if parsed else None
        jobs_list.append(
            {
                "id": row.get("id"),
                "customer_name": row.get("customer_name"),
                "address": row.get("address"),
                "scheduled_time": scheduled_time,
                "status": row.get("status"),
                "priority": row.get("priority"),
            }
        )

    return jsonable_response(
        {
            "date": target_date.isoformat(),
            "tech_id": technician_id,
            "count": len(jobs_list),
            "jobs": jobs_list,
        }
    )


@router.get("/my-jobs/{job_id}", response_model=None)
def mobile_my_job_detail(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id)
    if not _job_belongs_to_user(db, tenant_id, job_id, user_id, technician_id):
        return jsonable_response({"detail": "job not found"}, 404)

    job = _get_job(db, tenant_id, job_id)
    if not job:
        return jsonable_response({"detail": "job not found"}, 404)

    customer = None
    if job.get("customer_id"):
        try:
            _cid = _UUID(str(job["customer_id"]))
        except (ValueError, AttributeError):
            logging.getLogger(__name__).exception("mobile_my_job_detail caught exception")
            _cid = None
        if _cid is not None:
            # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
            c_obj = db.execute(
                select(Customer).where(
                    Customer.id == _cid,
                )
            ).scalar_one_or_none()
            if c_obj is not None:
                customer = {
                    "id": str(c_obj.id),
                    "name": c_obj.name,
                    "phone": c_obj.phone,
                    "email": c_obj.email,
                    "address": c_obj.address,
                }

    checklists_data: list[dict[str, Any]] = []
    checklist_rows = db.execute(
        _text(
            """
            SELECT id, name, checklist_type, completed_at
            FROM job_checklists
            WHERE company_id = :tenant_id
              AND job_id = :job_id
            ORDER BY created_at ASC
            """
        ),
        {"tenant_id": tenant_id, "job_id": job_id},
    ).mappings().all()

    for checklist in checklist_rows:
        checklist_id = checklist.get("id")
        items_query = db.execute(
            _text(
                """
                SELECT id, item_text, is_checked, checked_at, notes
                FROM job_checklist_items
                WHERE company_id = :tenant_id
                  AND checklist_id = :checklist_id
                ORDER BY sort_order ASC
                """
            ),
            {
                "tenant_id": tenant_id,
                "checklist_id": checklist_id,
            },
        ).mappings().all()
        items = []
        for item in items_query:
            checked_at_raw = item.get("checked_at")
            if isinstance(checked_at_raw, datetime):
                checked_at = checked_at_raw.isoformat()
            else:
                parsed = _parse_datetime(str(checked_at_raw) if checked_at_raw is not None else None)
                checked_at = parsed.isoformat() if parsed else None
            items.append(
                {
                    "id": item.get("id"),
                    "text": item.get("item_text"),
                    "required": True,
                    "completed": bool(item.get("is_checked")),
                    "completed_at": checked_at,
                    "notes": item.get("notes"),
                }
            )
        completed_at_raw = checklist.get("completed_at")
        completed_at = (
            completed_at_raw.isoformat()
            if isinstance(completed_at_raw, datetime)
            else _parse_datetime(str(completed_at_raw) if completed_at_raw is not None else None)
        )
        checklists_data.append(
            {
                "id": checklist_id,
                "name": checklist.get("name"),
                "checklist_type": checklist.get("checklist_type"),
                "completed_at": completed_at.isoformat() if isinstance(completed_at, datetime) else completed_at,
                "items": items,
            }
        )

    photos = db.execute(
        _text(
            """
            SELECT id, filename, photo_type, caption, created_at
            FROM job_photos
            WHERE company_id = :tenant_id
              AND job_id = :job_id
              AND deleted_at IS NULL
            ORDER BY created_at ASC
            """
        ),
        {"tenant_id": tenant_id, "job_id": job_id},
    ).mappings().all()

    return jsonable_response(
        {
            "job": job,
            "customer": customer,
            "checklists": checklists_data,
            "photos": [dict(r) for r in photos],
        }
    )


@router.get("/jobs/{job_id}/checklist", response_model=None)
def mobile_job_checklist_items(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id)
    if not _job_belongs_to_user(db, tenant_id, job_id, user_id, technician_id):
        return jsonable_response({"detail": "job not found"}, 404)

    rows = db.execute(
        _text(
            """
            SELECT jci.id, jci.item_text, jci.is_checked, jci.checked_at
            FROM job_checklist_items jci
            JOIN job_checklists jc ON jc.id = jci.checklist_id
            WHERE jci.company_id = :tenant_id
              AND jc.company_id = :tenant_id
              AND jc.job_id = :job_id
            ORDER BY jci.sort_order ASC
            """
        ),
        {"tenant_id": tenant_id, "job_id": job_id},
    ).mappings().all()

    items: list[dict[str, Any]] = []
    for row in rows:
        checked_at_raw = row.get("checked_at")
        if isinstance(checked_at_raw, datetime):
            checked_at = checked_at_raw.isoformat()
        else:
            parsed = _parse_datetime(str(checked_at_raw) if checked_at_raw is not None else None)
            checked_at = parsed.isoformat() if parsed else None
        items.append(
            {
                "id": row.get("id"),
                "text": row.get("item_text"),
                "required": True,
                "completed": bool(row.get("is_checked")),
                "completed_at": checked_at,
            }
        )

    return jsonable_response({"items": items})


@router.post("/jobs/{job_id}/start", response_model=None)
def mobile_job_start(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    user = current_user or {}
    user_id = _user_id(user)
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id)
    if not _job_belongs_to_user(db, tenant_id, job_id, user_id, technician_id):
        return jsonable_response({"detail": "job not found"}, 404)

    job = _get_job(db, tenant_id, job_id)
    if not job:
        return jsonable_response({"detail": "job not found"}, 404)

    prev_status = job.get("status")
    now = datetime.now(UTC)
    try:
        _jid = _UUID(job_id)
    except (ValueError, AttributeError):
        logging.getLogger(__name__).exception("mobile_job_start caught exception")
        _jid = None
    if _jid is not None:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        _job_obj = db.execute(
            select(Job).where(
                Job.id == _jid,
                Job.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if _job_obj is not None:
            _job_obj.status = "in_progress"
            _job_obj.updated_at = now
    # started_at is not on the Job ORM model — set via raw SQL if the column exists
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    cols = _table_columns(db, "jobs")
    if "started_at" in cols:
        db.execute(
            _text(
                "UPDATE jobs SET started_at = :now WHERE id = :job_id AND deleted_at IS NULL"
            ),
            {"now": now, "job_id": job_id},
        )

    auto_clock_in = False
    entry_id = None
    open_entry = _find_open_time_entry(db, tenant_id, user_id, job_id=job_id, entry_type="job")
    if not open_entry:
        entry_id, _ = _create_time_entry(
            db,
            tenant_id,
            user_id,
            technician_id=technician_id,
            job_id=job_id,
            entry_type="job",
        )
        auto_clock_in = True
        _audit_state_change(
            db,
            event_type="clock_in",
            actor_id=user_id,
            entity_type="job",
            entity_id=job_id,
            payload={"entry_id": entry_id, "entry_type": "job"},
            request=request,
            actor_role=user.get("role"),
        )

    _audit_state_change(
        db,
        event_type="mobile_job_started",
        actor_id=user_id,
        entity_type="job",
        entity_id=job_id,
        payload={"from": prev_status, "to": "in_progress", "auto_clock_in": auto_clock_in, "entry_id": entry_id},
        request=request,
        actor_role=user.get("role"),
    )
    db.commit()

    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            action="mobile_job_started",
            entity_type="job",
            entity_id=job_id,
            details={"from": prev_status, "to": "in_progress", "auto_clock_in": auto_clock_in},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("mobile_job_start_audit_failed")

    return jsonable_response(
        {
            "ok": True,
            "job_id": job_id,
            "status": "in_progress",
            "auto_clock_in": auto_clock_in,
            "entry_id": entry_id,
        }
    )


@router.post("/jobs/{job_id}/status", response_model=None)
def mobile_job_status_update(
    job_id: str,
    payload: JobStatusUpdate,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    new_status = (payload.status or "").strip().lower()
    if new_status not in _MOBILE_JOB_STATUS_VALUES:
        return jsonable_response(
            {"detail": f"Invalid status. Must be one of: {', '.join(sorted(_MOBILE_JOB_STATUS_VALUES))}"},
            400,
        )

    tenant_id = _tenant_id(request)
    user = current_user or {}
    user_id = _user_id(user)
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id)
    if not _job_belongs_to_user(db, tenant_id, job_id, user_id, technician_id):
        return jsonable_response({"detail": "job not found"}, 404)

    job = _get_job(db, tenant_id, job_id)
    if not job:
        return jsonable_response({"detail": "job not found"}, 404)

    old_status = job.get("status")
    now = datetime.now(UTC)
    try:
        _jid = _UUID(job_id)
    except (ValueError, AttributeError):
        logging.getLogger(__name__).exception("mobile_job_status_update caught exception")
        _jid = None
    if _jid is not None:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        _job_obj = db.execute(
            select(Job).where(
                Job.id == _jid,
                Job.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if _job_obj is not None:
            _job_obj.status = new_status
            _job_obj.updated_at = now
    _audit_state_change(
        db,
        event_type="mobile_job_status_changed",
        actor_id=user_id,
        entity_type="job",
        entity_id=job_id,
        payload={"from": old_status, "to": new_status},
        request=request,
        actor_role=user.get("role"),
    )
    db.commit()

    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            action="mobile_job_status_changed",
            entity_type="job",
            entity_id=job_id,
            details={"from": old_status, "to": new_status},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("mobile_job_status_change_audit_failed")

    return jsonable_response(
        {
            "ok": True,
            "job_id": job_id,
            "status": new_status,
        }
    )


@router.get("/job/{job_id}", response_model=None)
def get_mobile_job_detail(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ = current_user
    tenant_id = _tenant_id(request)
    job = _get_job(db, tenant_id, job_id)
    if not job:
        return jsonable_response({"detail": "job not found"}, 404)

    customer = None
    if job.get("customer_id"):
        try:
            _cid = _UUID(str(job["customer_id"]))
        except (ValueError, AttributeError):
            logging.getLogger(__name__).exception("get_mobile_job_detail caught exception")
            _cid = None
        if _cid is not None:
            c_obj = db.execute(
                select(Customer).where(Customer.id == _cid)
            ).scalar_one_or_none()
            if c_obj is not None:
                customer = {
                    "id": str(c_obj.id),
                    "name": c_obj.name,
                    "phone": c_obj.phone,
                    "email": c_obj.email,
                    "address": c_obj.address,
                }

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    notes = db.execute(
        _text(
            """
            SELECT id, body AS note, author_id AS created_by, created_at
            FROM job_notes
            WHERE job_id = :job_id
            ORDER BY created_at ASC
            """
        ),
        {"job_id": job_id},
    ).mappings().all()

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    photos = db.execute(
        _text(
            """
            SELECT id, filename, content_type, file_size, created_at
            FROM job_photos
            WHERE job_id = :job_id
              AND deleted_at IS NULL
            ORDER BY created_at ASC
            """
        ),
        {"job_id": job_id},
    ).mappings().all()

    return jsonable_response(
        {
            "job": job,
            "customer": customer,
            "notes": [dict(r) for r in notes],
            "photos": [dict(r) for r in photos],
        }
    )


@router.post("/jobs/{job_id}/en-route", response_model=None)
def mobile_job_en_route(
    job_id: str,
    payload: EnRouteBody,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    user = current_user or {}
    user_id = _user_id(user)
    job = _get_job(db, tenant_id, job_id)
    if not job:
        return jsonable_response({"detail": "job not found"}, 404)

    try:
        _jid = _UUID(job_id)
    except (ValueError, AttributeError):
        logging.getLogger(__name__).exception("mobile_job_en_route caught exception")
        _jid = None
    if _jid is not None:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        _job_obj = db.execute(
            select(Job).where(
                Job.id == _jid,
                Job.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if _job_obj is not None:
            # S1-B2 — forward-only validation; idempotent re-tap allowed.
            _validate_forward_transition(_job_obj.dispatch_status, "en_route")
            _job_obj.dispatch_status = "en_route"
    # Phase 1.4 D2 — stamp per-tech en_route_at on JobAssignment so multi-
    # tech jobs preserve who hit "On my way" and when. Lazy back-fill an
    # assignment row if a single-tech-era job has none yet.
    _technician_id = _get_technician_id(db, tenant_id, user_id) or user_id
    if _technician_id:
        from gdx_dispatch.routers.job_assignments import (
            ensure_assignment_for_legacy_job,
            stamp_tech_state,
        )
        ensure_assignment_for_legacy_job(
            db, job_id=job_id, tech_id=_technician_id, user_id=user_id,
        )
        stamp_tech_state(
            db, job_id=job_id, tech_id=_technician_id,
            state="en_route", when=datetime.now(UTC),
        )
    _audit_state_change(
        db,
        event_type="en_route",
        actor_id=user_id,
        entity_type="job",
        entity_id=job_id,
        payload={"eta_minutes": payload.eta_minutes},
        request=request,
        actor_role=user.get("role"),
    )
    db.commit()

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="mobile_job_en_route",
                entity_type="mobile_job_en_route",
                entity_id=str(job_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('mobile_job_en_route_audit_failed')
    return jsonable_response(
        {
            "ok": True,
            "job_id": job_id,
            "dispatch_status": "en_route",
            "eta_minutes": payload.eta_minutes,
            "customer_notified": True,
        }
    )


@router.post("/jobs/{job_id}/arrived", response_model=None)
def mobile_job_arrived(
    job_id: str,
    request: Request,
    payload: ArrivedBody | None = None,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """S1-B1 — geo-tagged arrival. Optional lat/lng/accuracy stored on the
    appointment + Job.arrived_at, plus the auto-clock-in for the per-job
    labor timer (existing behavior). State machine validated forward-only
    via _validate_forward_transition.
    """
    tenant_id = _tenant_id(request)
    user = current_user or {}
    user_id = _user_id(user)
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    job = _get_job(db, tenant_id, job_id)
    if not job:
        return jsonable_response({"detail": "job not found"}, 404)

    arrived_payload = payload or ArrivedBody()
    arrival_time = datetime.now(UTC)

    try:
        _jid = _UUID(job_id)
    except (ValueError, AttributeError):
        logging.getLogger(__name__).exception("mobile_job_arrived caught exception")
        _jid = None
    if _jid is not None:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        _job_obj = db.execute(
            select(Job).where(
                Job.id == _jid,
                Job.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if _job_obj is not None:
            # S1-B2 — forward-only validation; idempotent re-tap allowed.
            _validate_forward_transition(_job_obj.dispatch_status, "on_site")
            _job_obj.dispatch_status = "on_site"
            # S1-B1 — stamp Job.arrived_at the first time we hear "I'm here"
            # so audit + payroll have a single source of truth, and skip
            # subsequent re-taps so the original arrival timestamp wins.
            if _job_obj.arrived_at is None:
                _job_obj.arrived_at = arrival_time

        # S1-B1 — stamp the appointment too if one exists. lat/lng come
        # from the tech's device; accuracy lives on the audit row only
        # (no accuracy column on Appointment) so we don't lose it.
        appt = db.execute(
            select(Appointment).where(
                Appointment.job_id == _jid,
                Appointment.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if appt is not None and appt.arrived_at is None:
            appt.arrived_at = arrival_time
            if arrived_payload.lat is not None and arrived_payload.lng is not None:
                # Don't clobber a geocoded appointment lat/lng with the
                # tech's device location — those are different signals.
                # The tech's coordinates land in the audit details below.
                pass

    # Phase 1.4 D2 — per-tech arrived_at on JobAssignment.
    _technician_id_arr = _get_technician_id(db, tenant_id, user_id) or user_id
    if _technician_id_arr:
        from gdx_dispatch.routers.job_assignments import (
            ensure_assignment_for_legacy_job,
            stamp_tech_state,
        )
        ensure_assignment_for_legacy_job(
            db, job_id=job_id, tech_id=_technician_id_arr, user_id=user_id,
        )
        stamp_tech_state(
            db, job_id=job_id, tech_id=_technician_id_arr,
            state="arrived", when=arrival_time,
        )

    technician_id = _get_technician_id(db, tenant_id, user_id)
    open_entry = _find_open_time_entry(db, tenant_id, user_id, job_id=job_id, entry_type="job")
    auto_clock_in = False
    entry_id = None
    if not open_entry:
        entry_id, _ = _create_time_entry(
            db,
            tenant_id,
            user_id,
            technician_id=technician_id,
            job_id=job_id,
            entry_type="job",
        )
        auto_clock_in = True
        _audit_state_change(
            db,
            event_type="clock_in",
            actor_id=user_id,
            entity_type="job",
            entity_id=job_id,
            payload={"entry_type": "job", "entry_id": entry_id},
            request=request,
            actor_role=user.get("role"),
        )

    _audit_state_change(
        db,
        event_type="arrived",
        actor_id=user_id,
        entity_type="job",
        entity_id=job_id,
        payload={
            "auto_clock_in": auto_clock_in,
            "arrived_at": arrival_time.isoformat(),
            "lat": arrived_payload.lat,
            "lng": arrived_payload.lng,
            "accuracy": arrived_payload.accuracy,
        },
        request=request,
        actor_role=user.get("role"),
    )
    db.commit()

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="mobile_job_arrived",
                entity_type="mobile_job_arrived",
                entity_id=str(job_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('mobile_job_arrived_audit_failed')
    return jsonable_response(
        {
            "ok": True,
            "job_id": job_id,
            "dispatch_status": "on_site",
            "auto_clock_in": auto_clock_in,
            "entry_id": entry_id,
        }
    )


@router.post("/jobs/{job_id}/complete", response_model=None, deprecated=True)
def mobile_job_complete(
    job_id: str,
    payload: CompleteBody,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """DEPRECATED — Phase 2 / C3.5 (2026-05-10) replaced this with the
    unified /api/jobs/{id}/closeout endpoint. MobileTodayView no longer
    calls this; only an offline-queued or pre-update PWA client should
    still hit it. Scheduled for removal in C6 (~2026-06-10) once audit
    logs show zero traffic.

    Adds a deprecation log line on every call so we can grep for active
    clients before deletion. Sets RFC 8594 Sunset + Deprecation headers
    so well-behaved clients know to migrate.
    """
    # The user_agent + x-request-id let us identify WHICH client (PWA
    # build hash, mobile app version, curl/cron) is still hitting this
    # route at deletion time. Without them, tenant_id alone won't tell
    # us whether to chase a stale browser cache, an offline-queued
    # replay, or a third-party integration.
    _hdrs = request.headers if request else {}
    log.warning(
        "deprecated_route_called",
        extra={
            "route": "POST /api/mobile/jobs/{job_id}/complete",
            "replacement": "POST /api/jobs/{job_id}/closeout",
            "sunset_planned": "2026-06-10",
            "tenant_id": _tenant_id(request),
            "user_id": _user_id(current_user or {}),
            "job_id": str(job_id),
            "user_agent": _hdrs.get("user-agent", ""),
            "request_id": _hdrs.get("x-request-id", ""),
            "referer": _hdrs.get("referer", ""),
        },
    )
    tenant_id = _tenant_id(request)
    user = current_user or {}
    user_id = _user_id(user)
    job = _get_job(db, tenant_id, job_id)
    if not job:
        return jsonable_response({"detail": "job not found"}, 404)

    # S1-B5 — signature gating + surface check.
    from gdx_dispatch.core.tenant_mobile_settings import get_tenant_mobile_setting

    # Phase 1.4 D4 — lead-tech-only completion gate. Falls back to
    # permissive when no lead is set on the job (otherwise a tenant
    # that flips the flag without designating leads would lock every
    # job from completing). Single-tech jobs without a JobAssignment
    # row also fall back to permissive — Phase 1.4 doesn't retroactively
    # block legacy single-tech jobs.
    if get_tenant_mobile_setting(
        db, "tech_mobile.completion_lead_tech_only", default=False, request=request
    ):
        from gdx_dispatch.routers.job_assignments import has_any_lead, is_lead_for_job

        _technician_id_complete = _get_technician_id(db, tenant_id, user_id) or user_id
        if _technician_id_complete and has_any_lead(db, job_id=job_id):
            if not is_lead_for_job(db, job_id=job_id, tech_id=_technician_id_complete):
                return jsonable_response(
                    {
                        "detail": (
                            "Only the lead tech can complete this job. "
                            "Ask the lead to mark it done, or have dispatch "
                            "reassign the lead role."
                        )
                    },
                    403,
                )

    sig_required_setting = get_tenant_mobile_setting(
        db, "tech_mobile.signature_required_completion", request=request
    )
    sig_surface = get_tenant_mobile_setting(
        db, "tech_mobile.signature_surface", request=request
    )

    signature_data = (payload.signature_data or "").strip()
    existing_signature = (job.get("signature_data") or "").strip()

    # phone_handoff (default) — customer signs on the tech's device,
    # signature_data must be present in the body or already on the job.
    # customer_link — send the customer a tokenized URL to sign at; the
    # /complete request must NOT carry signature_data (it's collected
    # asynchronously). v1 ships phone_handoff only; customer_link is
    # 501 until the tokenized-link flow lands in a future slice.
    if sig_surface == "customer_link":
        return jsonable_response(
            {
                "detail": (
                    "tech_mobile.signature_surface='customer_link' is not yet "
                    "implemented; set it to 'phone_handoff' to complete jobs "
                    "with on-device signatures."
                )
            },
            501,
        )
    if sig_surface not in (None, "phone_handoff"):
        return jsonable_response(
            {"detail": f"unknown signature_surface: {sig_surface!r}"}, 400
        )

    if sig_required_setting == "required":
        if not signature_data and not existing_signature:
            return jsonable_response(
                {"detail": "Signature is required to complete job"}, 400
            )
    elif sig_required_setting == "off":
        # Tenant has fully opted out of signatures — discard any sent.
        signature_data = ""
    # "optional" — accept whatever's sent (or not), no gating.

    now = datetime.now(UTC)
    if signature_data:
        if not _validate_signature_data(signature_data):
            return jsonable_response({"detail": "Invalid signature_data base64 payload"}, 400)
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        db.execute(
            _text(
                """
                UPDATE jobs
                SET signature_data = :signature_data,
                    signed_by = :signed_by,
                    signed_at = :signed_at
                WHERE id = :job_id
                  AND deleted_at IS NULL
                """
            ),
            {
                "signature_data": signature_data,
                "signed_by": (payload.signed_by or "").strip() or None,
                "signed_at": now,
                "job_id": job_id,
            },
        )

    try:
        _jid = _UUID(job_id)
    except (ValueError, AttributeError):
        logging.getLogger(__name__).exception("mobile_job_complete caught exception")
        _jid = None
    if _jid is not None:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        _job_obj = db.execute(
            select(Job).where(
                Job.id == _jid,
                Job.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if _job_obj is not None:
            # S1-B2 — Job.dispatch_status enum is (unassigned, assigned,
            # en_route, on_site, done). Earlier code wrote "completed"
            # here, which is NOT a valid enum value and would raise on
            # PG (loose on SQLite). Use "done"; capture completion in
            # Job.completed_at for the timestamp axis.
            _validate_forward_transition(_job_obj.dispatch_status, "done")
            _job_obj.dispatch_status = "done"
            _job_obj.completed_at = now

    # Phase 1.4 D2 — per-tech completed_at on JobAssignment so multi-tech
    # jobs preserve who finished (vs. who started or visited).
    _technician_id_done = _get_technician_id(db, tenant_id, user_id) or user_id
    if _technician_id_done:
        from gdx_dispatch.routers.job_assignments import (
            ensure_assignment_for_legacy_job,
            stamp_tech_state,
        )
        ensure_assignment_for_legacy_job(
            db, job_id=job_id, tech_id=_technician_id_done, user_id=user_id,
        )
        stamp_tech_state(
            db, job_id=job_id, tech_id=_technician_id_done,
            state="complete", when=now,
        )

    _audit_state_change(
        db,
        event_type="complete",
        actor_id=user_id,
        entity_type="job",
        entity_id=job_id,
        payload={"completion_notes": payload.completion_notes},
        request=request,
        actor_role=user.get("role"),
    )
    db.commit()

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="mobile_job_complete",
                entity_type="mobile_job_complete",
                entity_id=str(job_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('mobile_job_complete_audit_failed')
    return jsonable_response(
        {
            "ok": True,
            "job_id": job_id,
            "dispatch_status": "done",
            "completed_at": now.isoformat(),
        }
    )


@router.post("/job/{job_id}/status", response_model=None)
def update_mobile_job_status(
    job_id: str,
    payload: JobStatusUpdate,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    new_status = (payload.status or "").strip().lower()
    if new_status not in _VALID_JOB_STATUSES:
        return jsonable_response(
            {"detail": "Invalid status. Must be one of: completed, en_route, on_site"},
            400,
        )
    if new_status == "en_route":
        return mobile_job_en_route(
            job_id=job_id,
            payload=EnRouteBody(eta_minutes=None),
            request=request,
            current_user=current_user,
            db=db,
        )
    if new_status == "on_site":
        return mobile_job_arrived(job_id=job_id, request=request, current_user=current_user, db=db)
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="update_mobile_job_status",
                entity_type="mobile_job_status",
                entity_id=str(job_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('update_mobile_job_status_audit_failed')
    return mobile_job_complete(
        job_id=job_id,
        payload=CompleteBody(completion_notes=None),
        request=request,
        current_user=current_user,
        db=db,
    )


@router.post("/clock-in", response_model=None)
def mobile_day_clock_in(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Day-level clock-in. Writes to ``timeclock_entries_router`` (canonical)
    so mobile and desktop ``/timeclock`` write the same row. See
    ``mobile_clock_status`` docstring for the S3 reconciliation context.
    """
    tenant_id = _tenant_id(request)
    user = current_user or {}
    user_id = _user_id(user)
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id) or user_id

    existing = db.execute(
        select(TimeclockEntry)
        .where(
            TimeclockEntry.tenant_id == tenant_id,
            TimeclockEntry.technician_id == technician_id,
            TimeclockEntry.deleted_at.is_(None),
            TimeclockEntry.clock_out_at.is_(None),
        )
        .order_by(TimeclockEntry.clock_in_at.desc())
        .limit(1)
    ).scalars().first()
    if existing:
        return jsonable_response(
            {
                "detail": "Already clocked in for day",
                "entry_id": str(existing.id),
                "clock_in": str(existing.clock_in_at),
            },
            409,
        )

    now_iso = datetime.now(UTC).isoformat()
    entry_id = str(uuid.uuid4())
    entry = TimeclockEntry(
        id=entry_id,
        tenant_id=tenant_id,
        technician_id=technician_id,
        clock_in_at=now_iso,
        clock_out_at=None,
        minutes=None,
        notes=None,
        entry_type="clock",
        created_at=now_iso,
        updated_at=now_iso,
    )
    db.add(entry)
    _audit_state_change(
        db,
        event_type="clock_in",
        actor_id=user_id,
        entity_type="time_entry",
        entity_id=entry_id,
        payload={"entry_type": "day", "table": "timeclock_entries_router"},
        request=request,
        actor_role=user.get("role"),
    )
    db.commit()

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="mobile_day_clock_in",
                entity_type="mobile_day_clock_in",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('mobile_day_clock_in_audit_failed')
    return jsonable_response(
        {"ok": True, "entry_id": entry_id, "clock_in": now_iso, "entry_type": "day"},
        201,
    )


@router.post("/clock-out", response_model=None)
def mobile_day_clock_out(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Day-level clock-out. Closes the open ``timeclock_entries_router`` row
    so the desktop ``/timeclock`` view reflects the same state. See
    ``mobile_clock_status`` docstring for the S3 reconciliation context.
    """
    tenant_id = _tenant_id(request)
    user = current_user or {}
    user_id = _user_id(user)
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id) or user_id

    entry = db.execute(
        select(TimeclockEntry)
        .where(
            TimeclockEntry.tenant_id == tenant_id,
            TimeclockEntry.technician_id == technician_id,
            TimeclockEntry.deleted_at.is_(None),
            TimeclockEntry.clock_out_at.is_(None),
        )
        .order_by(TimeclockEntry.clock_in_at.desc())
        .limit(1)
    ).scalars().first()
    if not entry:
        return jsonable_response({"detail": "No open day time entry found"}, 404)

    now_iso = datetime.now(UTC).isoformat()
    duration_minutes: int | None = None
    try:
        clock_in_dt = _parse_datetime(str(entry.clock_in_at))
        if clock_in_dt is not None:
            delta = datetime.now(UTC) - clock_in_dt
            duration_minutes = int(delta.total_seconds() // 60)
    except (TypeError, ValueError):
        duration_minutes = None

    entry.clock_out_at = now_iso
    entry.minutes = duration_minutes
    entry.updated_at = now_iso

    _audit_state_change(
        db,
        event_type="clock_out",
        actor_id=user_id,
        entity_type="time_entry",
        entity_id=str(entry.id),
        payload={"entry_type": "day", "duration_minutes": duration_minutes, "table": "timeclock_entries_router"},
        request=request,
        actor_role=user.get("role"),
    )
    db.commit()

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="mobile_day_clock_out",
                entity_type="mobile_day_clock_out",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('mobile_day_clock_out_audit_failed')
    return jsonable_response(
        {
            "ok": True,
            "entry_id": str(entry.id),
            "clock_out": now_iso,
            "duration_minutes": duration_minutes,
            "entry_type": "day",
        }
    )


@router.post("/jobs/{job_id}/clock-in", response_model=None)
@router.post("/job/{job_id}/clock-in", response_model=None)
def mobile_clock_in(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    user = current_user or {}
    user_id = _user_id(user)
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)
    if not _get_job(db, tenant_id, job_id):
        return jsonable_response({"detail": "job not found"}, 404)

    existing = _find_open_time_entry(db, tenant_id, user_id, job_id=job_id, entry_type="job")
    if existing:
        return jsonable_response(
            {
                "detail": "Already clocked in on this job",
                "entry_id": str(existing["id"]),
                "clock_in": existing["clock_in"],
            },
            409,
        )

    technician_id = _get_technician_id(db, tenant_id, user_id)
    entry_id, now = _create_time_entry(
        db,
        tenant_id,
        user_id,
        technician_id=technician_id,
        job_id=job_id,
        entry_type="job",
    )
    _audit_state_change(
        db,
        event_type="clock_in",
        actor_id=user_id,
        entity_type="job",
        entity_id=job_id,
        payload={"entry_id": entry_id, "entry_type": "job"},
        request=request,
        actor_role=user.get("role"),
    )
    db.commit()

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="mobile_clock_in",
                entity_type="mobile_clock_in",
                entity_id=str(job_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('mobile_clock_in_audit_failed')
    return jsonable_response(
        {"ok": True, "entry_id": entry_id, "job_id": job_id, "clock_in": now.isoformat()},
        201,
    )


@router.post("/jobs/{job_id}/clock-out", response_model=None)
@router.post("/job/{job_id}/clock-out", response_model=None)
def mobile_clock_out(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    user = current_user or {}
    user_id = _user_id(user)
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)
    if not _get_job(db, tenant_id, job_id):
        return jsonable_response({"detail": "job not found"}, 404)

    row, now, duration_minutes = _close_open_time_entry(db, tenant_id, user_id, job_id=job_id, entry_type="job")
    if not row:
        return jsonable_response({"detail": "No open time entry found for this job"}, 404)

    _audit_state_change(
        db,
        event_type="clock_out",
        actor_id=user_id,
        entity_type="job",
        entity_id=job_id,
        payload={"entry_id": str(row["id"]), "entry_type": "job", "duration_minutes": duration_minutes},
        request=request,
        actor_role=user.get("role"),
    )
    db.commit()

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="mobile_clock_out",
                entity_type="mobile_clock_out",
                entity_id=str(job_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('mobile_clock_out_audit_failed')
    return jsonable_response(
        {
            "ok": True,
            "entry_id": str(row["id"]),
            "job_id": job_id,
            "clock_out": now.isoformat(),
            "duration_minutes": duration_minutes,
        }
    )


@router.get("/timecard", response_model=None)
def mobile_timecard(
    request: Request,
    date: date_type | None = Query(default=None),
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Today's timecard for the calling user.

    The day-level shift clock and the per-job labor clocks live in two
    different tables since the S3 mobile-clock reconciliation
    (commit 9cd67f7d, 2026-04-29):

    - ``timeclock_entries_router`` (TimeclockEntry) — day-level shift
      clocks. Mobile day clock-in / clock-out and the desktop
      ``/timeclock`` page both write here. Each row tagged ``kind='shift'``.
    - ``time_entries`` (TimeEntry) — per-job labor clocks tied to a
      specific job; payroll reads this. Each row tagged ``kind='job'``.

    Both are merged into one chronological list; ``count`` is the total
    across both kinds so the existing mobile UI's "X clocks today"
    badge keeps working.
    """
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id) or user_id
    target_date = date or datetime.now(UTC).date()
    target_iso_prefix = target_date.isoformat()  # 'YYYY-MM-DD'

    entries: list[dict[str, Any]] = []

    # Day-level shift clocks (post-S3 canonical).
    shift_rows = db.execute(
        select(TimeclockEntry)
        .where(
            TimeclockEntry.tenant_id == tenant_id,
            TimeclockEntry.technician_id == technician_id,
            TimeclockEntry.deleted_at.is_(None),
            # clock_in_at is stored as ISO text; LIKE prefix matches the
            # YYYY-MM-DD date portion regardless of timezone suffix.
            TimeclockEntry.clock_in_at.like(f"{target_iso_prefix}%"),
        )
        .order_by(TimeclockEntry.clock_in_at.asc())
    ).scalars().all()
    for row in shift_rows:
        entries.append(
            {
                "kind": "shift",
                "id": row.id,
                "job_id": None,
                "clock_in": row.clock_in_at,
                "clock_out": row.clock_out_at,
                "duration_minutes": row.minutes,
                "entry_type": row.entry_type,
            }
        )

    # Per-job labor clocks (legacy table, intentionally retained for
    # payroll integration — see commit 9cd67f7d's manifest).
    job_rows = db.execute(
        _text(
            """
            SELECT id, job_id, clock_in, clock_out, duration_minutes, entry_type
            FROM time_entries
            WHERE company_id = :tenant_id
              AND user_id = :user_id
              AND DATE(clock_in) = :target_date
              AND deleted_at IS NULL
            ORDER BY clock_in ASC
            """
        ),
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "target_date": target_iso_prefix,
        },
    ).mappings().all()
    for row in job_rows:
        entries.append(
            {
                "kind": "job",
                "id": row.get("id"),
                "job_id": row.get("job_id"),
                "clock_in": row.get("clock_in"),
                "clock_out": row.get("clock_out"),
                "duration_minutes": row.get("duration_minutes"),
                "entry_type": row.get("entry_type"),
            }
        )

    # Merge both sources into one chronological list. Robust to mixed
    # types (datetime vs ISO string) by coercing to a common string for
    # comparison — none of the values can be None for the sort key
    # because clock_in is NOT NULL in both tables.
    entries.sort(key=lambda e: str(e.get("clock_in") or ""))

    return jsonable_response(
        {"date": target_iso_prefix, "count": len(entries), "entries": entries}
    )


_VALID_PHOTO_KINDS = {"before", "during", "after"}


def _photo_exif_metadata(raw: bytes) -> dict[str, Any]:
    """Extract a minimal evidence-trail subset of EXIF from an image.

    Returns at most:
      - capture_time (DateTimeOriginal as ISO string)
      - gps_lat / gps_lng (decoded from GPSInfo block)

    Failure is silent — most camera photos won't trip this and the
    photo upload itself must not fail because EXIF parsing did. Sprint
    tech_mobile S1-B3.
    """
    try:
        from io import BytesIO

        from PIL import ExifTags, Image as _PILImage
    except Exception:
        return {}

    try:
        img = _PILImage.open(BytesIO(raw))
        raw_exif = img.getexif() if hasattr(img, "getexif") else None
    except Exception:
        return {}
    if not raw_exif:
        return {}

    tag_name = {v: k for k, v in ExifTags.TAGS.items()}
    out: dict[str, Any] = {}

    dto_id = tag_name.get("DateTimeOriginal")
    if dto_id is not None and dto_id in raw_exif:
        out["capture_time"] = str(raw_exif[dto_id])

    gps_id = tag_name.get("GPSInfo")
    if gps_id is None or gps_id not in raw_exif:
        return out
    gps_block = raw_exif.get_ifd(gps_id) if hasattr(raw_exif, "get_ifd") else None
    if not gps_block:
        return out
    gps_tag_name = {v: k for k, v in ExifTags.GPSTAGS.items()}
    lat_id = gps_tag_name.get("GPSLatitude")
    lat_ref_id = gps_tag_name.get("GPSLatitudeRef")
    lng_id = gps_tag_name.get("GPSLongitude")
    lng_ref_id = gps_tag_name.get("GPSLongitudeRef")

    def _to_decimal(coord) -> float | None:
        try:
            d, m, s = coord
            return float(d) + float(m) / 60.0 + float(s) / 3600.0
        except Exception:
            return None

    if lat_id in gps_block and lat_ref_id in gps_block:
        lat = _to_decimal(gps_block[lat_id])
        if lat is not None:
            if str(gps_block[lat_ref_id]).upper().startswith("S"):
                lat = -lat
            out["gps_lat"] = lat
    if lng_id in gps_block and lng_ref_id in gps_block:
        lng = _to_decimal(gps_block[lng_id])
        if lng is not None:
            if str(gps_block[lng_ref_id]).upper().startswith("W"):
                lng = -lng
            out["gps_lng"] = lng

    return out


@router.post("/jobs/{job_id}/photos", response_model=None)
@router.post("/job/{job_id}/photo", response_model=None)
async def upload_mobile_job_photo(
    job_id: str,
    request: Request,
    file: UploadFile = File(...),
    kind: str | None = Form(default=None),
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Phase 4.3 (S4-C3) — global slowapi limiter (registered in app.py
    # via SlowAPIMiddleware) already throttles abusive clients. A 20-
    # photo install completes well under that ceiling. Per-user 30/min
    # specifically would require a per-route decorator; deferred to a
    # follow-up so the global default isn't silently overridden.
    _ = request  # keep param referenced (FastAPI route signature)
    """S1-B3 — slot-tagged photo upload with per-tech attribution + EXIF.

    ``kind`` form param is one of {before, during, after}; when the tenant
    setting tech_mobile.photo_slot_tagging is "required" the param is
    mandatory, otherwise it falls back to the historical default
    ("during"). The uploader's user_id stamps JobPhoto.uploaded_by.
    EXIF GPS + capture_time are pulled from the image bytes and recorded
    on the audit row's details JSON; the photo row itself only carries
    kind / uploaded_by / uploaded_at.
    """
    from gdx_dispatch.core.tenant_mobile_settings import get_tenant_mobile_setting

    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if not _get_job(db, tenant_id, job_id):
        return jsonable_response({"detail": "job not found"}, 404)

    # Slot kind validation (S1-B3). When this handler is called directly
    # (legacy test paths that don't go through FastAPI's Form binding),
    # `kind` arrives as the Form() sentinel rather than None — coerce
    # to None defensively so we don't try to call `.strip()` on the
    # sentinel.
    if not isinstance(kind, str):
        kind = None
    slot_required = (
        get_tenant_mobile_setting(
            db, "tech_mobile.photo_slot_tagging", request=request
        )
        == "required"
    )
    if kind is None:
        if slot_required:
            return jsonable_response(
                {"detail": "kind is required (before / during / after)"}, 400
            )
        kind = "during"
    kind = kind.strip().lower()
    if kind not in _VALID_PHOTO_KINDS:
        return jsonable_response(
            {"detail": f"kind must be one of {sorted(_VALID_PHOTO_KINDS)}"}, 400
        )

    content_type = (file.content_type or "").lower()
    if not content_type.startswith("image/"):
        return jsonable_response({"detail": "file must be an image"}, 400)

    raw = await file.read()
    if not raw:
        return jsonable_response({"detail": "file is required"}, 400)

    exif = _photo_exif_metadata(raw)

    suffix = _image_suffix(file)
    filename = f"{uuid.uuid4()}.{suffix}"
    upload_dir = Path(os.getenv("MOBILE_UPLOAD_DIR", "/tmp/gdx_mobile_uploads")) / "job_photos"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / filename).write_bytes(raw)

    now = datetime.now(UTC)
    photo_id = str(uuid.uuid4())
    photo_url = f"/mobile/uploads/job_photos/{filename}"
    db.execute(
        _text(
            """
            INSERT INTO job_photos (
                id, company_id, job_id, kind, url, uploaded_at, uploaded_by,
                filename, content_type, file_size, created_at, deleted_at
            ) VALUES (
                :id, :company_id, :job_id, :kind, :url, :uploaded_at, :uploaded_by,
                :filename, :content_type, :file_size, :created_at, NULL
            )
            """
        ),
        {
            "id": photo_id,
            "company_id": tenant_id,
            "job_id": job_id,
            "kind": kind,
            "url": photo_url,
            "uploaded_at": now,
            "uploaded_by": user_id or None,
            "filename": filename,
            "content_type": content_type,
            "file_size": len(raw),
            "created_at": now,
        },
    )
    db.commit()

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="upload_mobile_job_photo",
                entity_type="mobile_job_photo",
                entity_id=str(job_id),
                details={
                    "photo_id": photo_id,
                    "kind": kind,
                    "uploaded_by": user_id or None,
                    "filename": filename,
                    "exif": exif or None,
                },
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('upload_mobile_job_photo_audit_failed')
    return jsonable_response(
        {
            "ok": True,
            "photo_id": photo_id,
            "job_id": job_id,
            "kind": kind,
            "uploaded_by": user_id or None,
            "filename": filename,
            "file_size": len(raw),
            "content_type": content_type,
            "exif": exif or None,
        },
        201,
    )


@router.post("/jobs/{job_id}/signature", response_model=None)
@router.post("/job/{job_id}/signature", response_model=None)
def capture_mobile_signature(
    job_id: str,
    payload: SignatureBody,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ = current_user
    tenant_id = _tenant_id(request)
    if not _get_job(db, tenant_id, job_id):
        return jsonable_response({"detail": "job not found"}, 404)

    data = (payload.signature_data or "").strip()
    if not _validate_signature_data(data):
        return jsonable_response({"detail": "signature_data must be a base64 image data URL"}, 400)

    now = datetime.now(UTC)
    signed_by = (payload.signed_by or "").strip() or None
    # signature_data/signed_by/signed_at are not on the Job ORM model — use raw SQL
    db.execute(
        _text(
            """
            UPDATE jobs
            SET signature_data = :signature_data,
                signed_by = :signed_by,
                signed_at = :signed_at
            WHERE id = :job_id
              AND company_id = :tenant_id
              AND deleted_at IS NULL
            """
        ),
        {
            "signature_data": data,
            "signed_by": signed_by,
            "signed_at": now,
            "job_id": job_id,
            "tenant_id": tenant_id,
        },
    )
    db.commit()

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="capture_mobile_signature",
                entity_type="capture_mobile_signature",
                entity_id=str(job_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('capture_mobile_signature_audit_failed')
    return jsonable_response({"ok": True, "job_id": job_id, "signed_by": signed_by, "signed_at": now.isoformat()})


@router.post("/jobs/{job_id}/notes", response_model=None)
@router.post("/job/{job_id}/notes", response_model=None)
def add_mobile_job_note(
    job_id: str,
    payload: NoteBody,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not _get_job(db, tenant_id, job_id):
        return jsonable_response({"detail": "job not found"}, 404)

    note = (payload.note or "").strip()
    if not note:
        return jsonable_response({"detail": "note is required"}, 400)

    now = datetime.now(UTC)
    try:
        _jid = _UUID(job_id)
    except (ValueError, AttributeError):
        logging.getLogger(__name__).exception("add_mobile_job_note caught exception")
        _jid = None
    # S1-B4 — per-tech attribution: author_id + a best-effort
    # display name resolved from the user dict (name / full_name / email
    # in that order, falling back to None so the UI can show the user_id
    # if no human-readable label is available).
    user_dict = current_user or {}
    author_name = None
    if isinstance(user_dict, dict):
        author_name = (
            user_dict.get("name")
            or user_dict.get("full_name")
            or user_dict.get("email")
            or None
        )
    note_obj = JobNote(
        id=str(uuid.uuid4()),
        company_id=tenant_id,
        job_id=str(_jid) if _jid is not None else str(uuid.uuid4()),
        body=note,
        author_id=user_id or "",
        author_name=author_name,
        created_at=now,
    )
    db.add(note_obj)
    db.flush()
    note_id = str(note_obj.id)
    db.commit()

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="add_mobile_job_note",
                entity_type="mobile_job_note",
                entity_id=str(job_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('add_mobile_job_note_audit_failed')
    return jsonable_response(
        {
            "ok": True,
            "id": note_id,
            "job_id": job_id,
            "note": note,
            "created_by": user_id or "",
            "created_at": now.isoformat(),
        },
        201,
    )


@router.post("/jobs/{job_id}/parts-used", response_model=None)
def mobile_job_parts_used(
    job_id: str,
    payload: PartsUsedBody,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ = current_user
    tenant_id = _tenant_id(request)
    if not _get_job(db, tenant_id, job_id):
        return jsonable_response({"detail": "job not found"}, 404)

    if not payload.parts:
        return jsonable_response({"detail": "parts are required"}, 400)

    recorded = []
    for part in payload.parts:
        if part.qty <= 0:
            return jsonable_response({"detail": "qty must be > 0"}, 400)

        try:
            _part_uuid = _UUID(part.part_id)
        except (ValueError, AttributeError):
            logging.getLogger(__name__).exception("mobile_job_parts_used caught exception")
            return jsonable_response({"detail": f"invalid part_id: {part.part_id}"}, 400)
        part_obj = db.execute(
            select(Part).where(Part.id == _part_uuid, Part.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not part_obj:
            return jsonable_response({"detail": f"part not found: {part.part_id}"}, 404)
        qty_on_hand = int(part_obj.qty_on_hand or 0)
        if qty_on_hand < part.qty:
            return jsonable_response({"detail": f"insufficient stock for part {part.part_id}"}, 400)

        part_obj.qty_on_hand = qty_on_hand - part.qty
        try:
            _job_uuid_parts = _UUID(job_id)
        except (ValueError, AttributeError):
            logging.getLogger(__name__).exception("mobile_job_parts_used caught exception")
            _job_uuid_parts = uuid.uuid4()
        job_part = JobPart(
            id=uuid.uuid4(),
            job_id=_job_uuid_parts,
            part_id=_part_uuid,
            qty_used=part.qty,
            unit_cost_at_time=float(part_obj.unit_cost or 0),
            created_at=datetime.now(UTC),
        )
        db.add(job_part)
        recorded.append({"part_id": part.part_id, "qty": part.qty})

    db.commit()
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="mobile_job_parts_used",
                entity_type="mobile_job_parts_used",
                entity_id=str(job_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('mobile_job_parts_used_audit_failed')
    return jsonable_response({"ok": True, "job_id": job_id, "recorded": len(recorded), "parts": recorded})


@router.post("/location", response_model=None)
def report_mobile_location(
    payload: LocationBody,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})
    if not user_id:
        return jsonable_response({"detail": "unauthorized"}, 401)

    technician_id = _get_technician_id(db, tenant_id, user_id)
    if not technician_id:
        return jsonable_response({"detail": "technician not found for user"}, 404)

    lat = float(payload.lat)
    lng = float(payload.lng)
    if not (-90 <= lat <= 90):
        return jsonable_response({"detail": "lat must be between -90 and 90"}, 400)
    if not (-180 <= lng <= 180):
        return jsonable_response({"detail": "lng must be between -180 and 180"}, 400)

    recorded_at = _parse_datetime(payload.timestamp) or datetime.now(UTC)
    now = datetime.now(UTC)
    cols = _table_columns(db, "technician_locations")
    query_filters: list[str] = []
    query_params: dict[str, Any] = {}
    if "company_id" in cols:
        query_filters.append("company_id = :company_id")
        query_params["company_id"] = tenant_id
    if "tech_id" in cols:
        query_filters.append("tech_id = :tech_id")
        query_params["tech_id"] = technician_id
    elif "user_id" in cols:
        query_filters.append("user_id = :user_id")
        query_params["user_id"] = user_id

    existing = None
    if "id" in cols and query_filters:
        existing = db.execute(
            _text(
                f"""
                SELECT id
                FROM technician_locations
                WHERE {' AND '.join(query_filters)}
                LIMIT 1
                """
            ),
            query_params,
        ).mappings().first()

    location_id = str(existing["id"]) if existing else str(uuid.uuid4())
    values: dict[str, Any] = {}
    if "id" in cols:
        values["id"] = location_id
    if "company_id" in cols:
        values["company_id"] = tenant_id
    if "tech_id" in cols:
        values["tech_id"] = technician_id
    if "user_id" in cols:
        values["user_id"] = user_id
    if "lat" in cols:
        values["lat"] = lat
    if "lng" in cols:
        values["lng"] = lng
    if "accuracy" in cols:
        values["accuracy"] = payload.accuracy
    if "accuracy_meters" in cols:
        values["accuracy_meters"] = payload.accuracy
    if "recorded_at" in cols:
        values["recorded_at"] = recorded_at
    if "created_at" in cols:
        values["created_at"] = now

    if existing and "id" in cols:
        update_cols = [k for k in values if k not in {"id", "company_id", "tech_id", "user_id", "created_at"}]
        db.execute(
            _text(
                f"""
                UPDATE technician_locations
                SET {', '.join([f'{k} = :{k}' for k in update_cols])}
                WHERE id = :id
                """
            ),
            values,
        )
    else:
        names = list(values.keys())
        db.execute(
            _text(
                f"""
                INSERT INTO technician_locations ({', '.join(names)})
                VALUES ({', '.join([f':{n}' for n in names])})
                """
            ),
            values,
        )
    db.commit()

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="report_mobile_location",
                entity_type="report_mobile_location",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('report_mobile_location_audit_failed')
    return jsonable_response(
        {
            "ok": True,
            "id": location_id,
            "lat": lat,
            "lng": lng,
            "accuracy": payload.accuracy,
            "timestamp": recorded_at.isoformat(),
        }
    )


@router.post("/sync", response_model=None)
def mobile_sync(
    payload: SyncBatchBody,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    processed = 0
    skipped = 0
    failed = 0
    results: list[dict[str, Any]] = []

    for action in payload.actions:
        fp = _sync_fingerprint(action)
        exists = (
            db.query(MobileSyncAction.id)
            .filter(
                MobileSyncAction.fingerprint == fp,
            )
            .first()
        )
        if exists:
            skipped += 1
            results.append({"type": action.type, "entity_id": action.entity_id, "status": "duplicate_skipped"})
            continue

        if action.type in {"job_note", "note"}:
            resp = add_mobile_job_note(
                job_id=action.entity_id or "",
                payload=NoteBody(note=str(action.data.get("note") or "")),
                request=request,
                current_user=current_user,
                db=db,
            )
        elif action.type == "location":
            resp = report_mobile_location(
                payload=LocationBody(
                    lat=float(action.data.get("lat", 0)),
                    lng=float(action.data.get("lng", 0)),
                    accuracy=action.data.get("accuracy"),
                    timestamp=action.data.get("timestamp"),
                ),
                request=request,
                current_user=current_user,
                db=db,
            )
        elif action.type == "en_route":
            resp = mobile_job_en_route(
                job_id=action.entity_id or "",
                payload=EnRouteBody(eta_minutes=action.data.get("eta_minutes")),
                request=request,
                current_user=current_user,
                db=db,
            )
        elif action.type == "arrived":
            resp = mobile_job_arrived(
                job_id=action.entity_id or "",
                request=request,
                current_user=current_user,
                db=db,
            )
        elif action.type == "complete":
            resp = mobile_job_complete(
                job_id=action.entity_id or "",
                payload=CompleteBody(
                    completion_notes=action.data.get("completion_notes"),
                    signature_data=action.data.get("signature_data"),
                    signed_by=action.data.get("signed_by"),
                ),
                request=request,
                current_user=current_user,
                db=db,
            )
        elif action.type == "clock_in":
            resp = mobile_day_clock_in(request=request, current_user=current_user, db=db)
        elif action.type == "clock_out":
            resp = mobile_day_clock_out(request=request, current_user=current_user, db=db)
        elif action.type == "job_clock_in":
            resp = mobile_clock_in(job_id=action.entity_id or "", request=request, current_user=current_user, db=db)
        elif action.type == "job_clock_out":
            resp = mobile_clock_out(job_id=action.entity_id or "", request=request, current_user=current_user, db=db)
        else:
            failed += 1
            results.append(
                {
                    "type": action.type,
                    "entity_id": action.entity_id,
                    "status": "failed",
                    "detail": "unsupported action type",
                }
            )
            continue

        if resp.status_code >= 300:
            failed += 1
            results.append(
                {
                    "type": action.type,
                    "entity_id": action.entity_id,
                    "status": "failed",
                    "detail": jsonable_encoder(json.loads(resp.body) if resp.body else {}),
                }
            )
            continue

        sync_record = MobileSyncAction(
            id=str(uuid.uuid4()),
            company_id=tenant_id,
            fingerprint=fp,
            action_type=action.type,
            entity_id=action.entity_id,
            queued_at=action.queued_at,
            created_at=str(datetime.now(UTC)),
        )
        db.add(sync_record)
        db.commit()

        processed += 1
        results.append({"type": action.type, "entity_id": action.entity_id, "status": "processed"})

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="mobile_sync",
                entity_type="mobile_sync",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('mobile_sync_audit_failed')
    return jsonable_response(
        {
            "ok": True,
            "total": len(payload.actions),
            "processed": processed,
            "skipped_duplicates": skipped,
            "failed": failed,
            "results": results,
        }
    )


# ---------------------------------------------------------------------------
# Mobile job status transitions — tech-mobile lifecycle endpoints
# ---------------------------------------------------------------------------
# POST /api/mobile/jobs/{job_id}/{status} where status ∈ {en_route, on_site,
# paused, complete}. Enforces valid lifecycle transitions. Tenant scoped.

_VALID_MOBILE_STATUSES = {"en_route", "on_site", "paused", "complete"}
# State machine keyed on the *combined* (lifecycle_stage, dispatch_status) tuple.
# Technicians advance a job through: scheduled → en_route → on_site → complete,
# with optional paused/resume along the way. The set values represent the
# allowed PREVIOUS dispatch_status for each target.
_TRANSITION_FROM_DISPATCH: dict[str, set[str]] = {
    "en_route": {"unassigned", "assigned"},   # pre-dispatch states
    "on_site":  {"en_route", "unassigned", "assigned"},
    "paused":   {"en_route", "on_site"},
    "complete": {"en_route", "on_site"},
}


@router.post("/jobs/{job_id}/transition/{status}", response_model=None)
def mobile_update_job_status(
    job_id: str,
    status: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Technician-facing status transition from the mobile app.

    URL: POST /api/mobile/jobs/{job_id}/transition/{status}
    (Path includes /transition/ to avoid collision with /jobs/{id}/complete,
    /photos, /signature, /notes, /parts-used, which already exist.)

    Valid statuses: en_route, on_site, paused, complete.
    Enforces a lifecycle state machine so techs can't jump phases.
    """
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user or {})

    # Validate status
    if status not in _VALID_MOBILE_STATUSES:
        return jsonable_response(
            {"detail": f"status must be one of {sorted(_VALID_MOBILE_STATUSES)}"}, 422,
        )
    # Validate UUID
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("mobile_update_job_status_failed")
        return jsonable_response({"detail": "invalid job_id"}, 422)

    try:
        _jid = _UUID(job_id)
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        _job_obj = db.execute(
            select(Job).where(
                Job.id == _jid,
                Job.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
    except SQLAlchemyError:
        log.exception("mobile_status_lookup_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "Database error"}, 500)

    if not _job_obj:
        return jsonable_response({"detail": "job not found"}, 404)

    current_dispatch = str(_job_obj.dispatch_status or "").lower()
    current_stage = str(_job_obj.lifecycle_stage or "").lower()
    allowed_prev = _TRANSITION_FROM_DISPATCH[status]
    if current_dispatch not in allowed_prev:
        return jsonable_response(
            {
                "detail": f"cannot transition to '{status}' from dispatch_status '{current_dispatch}'",
                "allowed_from": sorted(allowed_prev),
                "current_dispatch_status": current_dispatch,
                "current_lifecycle_stage": current_stage,
            },
            409,
        )

    now = datetime.now(UTC)
    try:
        if status == "en_route":
            # Tech has left the shop. Advance lifecycle to in_progress.
            _job_obj.lifecycle_stage = "in_progress"
            _job_obj.dispatch_status = "en_route"
            _job_obj.status = "In Progress"
            _job_obj.updated_at = now
        elif status == "on_site":
            # Tech arrived. Set arrived_at + advance lifecycle if not already.
            # arrived_at is not on the Job ORM model — keep raw SQL for this branch
            db.execute(
                _text(
                    "UPDATE jobs SET "
                    "lifecycle_stage = :ls, dispatch_status = :d, "
                    "status = :s, arrived_at = :now, updated_at = :now "
                    "WHERE id = :job_id AND company_id = :tenant_id"
                ),
                {
                    "job_id": job_id, "tenant_id": tenant_id,
                    "ls": "in_progress", "d": "on_site", "s": "In Progress", "now": now,
                },
            )
        elif status == "paused":
            # Keep dispatch_status but flag via status/updated_at.
            _job_obj.status = "Paused"
            _job_obj.updated_at = now
        else:  # complete
            _job_obj.lifecycle_stage = "completed"
            _job_obj.dispatch_status = "done"
            _job_obj.status = "Complete"
            _job_obj.completed_at = now
            _job_obj.updated_at = now
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("mobile_status_update_failed", extra={"tenant_id": tenant_id, "job_id": job_id, "status": status})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)

    # Refetch for the response
    db.refresh(_job_obj)
    updated = {
        "id": str(_job_obj.id),
        "title": _job_obj.title,
        "lifecycle_stage": _job_obj.lifecycle_stage,
        "dispatch_status": _job_obj.dispatch_status,
        "status": _job_obj.status,
        "updated_at": _job_obj.updated_at.isoformat() if _job_obj.updated_at else None,
        "completed_at": _job_obj.completed_at.isoformat() if _job_obj.completed_at else None,
    }

    # Audit log — fire-and-forget, don't block on failure
    try:
        import asyncio as _asyncio
        _asyncio.get_event_loop_policy().get_event_loop().create_task(
            log_audit_event(
                db,
                tenant_id=tenant_id,
                user_id=user_id or "mobile-tech",
                action="job_status_mobile_update",
                entity_type="job",
                entity_id=job_id,
                details={"from": current_stage, "to": status, "tech": user_id},
            )
        )
    except Exception:
        log.exception("mobile_status_audit_failed")

    log.info(
        "mobile_job_status_updated",
        extra={"tenant_id": tenant_id, "job_id": job_id, "from": current_stage, "to": status, "tech": user_id},
    )
    return jsonable_response(updated if updated else {"id": job_id, "status": status})
