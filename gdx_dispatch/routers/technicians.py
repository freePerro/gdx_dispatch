from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import AppSettings, Technician, TechUnavailability, User

log = logging.getLogger(__name__)

try:
    from gdx_dispatch.routers.auth import get_current_user
except ImportError:  # fallback for missing auth module to allow router initialization
    log.exception("technicians_auth_import_failed_using_fallback")
    async def get_current_user() -> dict[str, Any]:
        return {}


router = APIRouter(prefix="/api/technicians", tags=["technicians"], dependencies=[Depends(require_module("dispatch"))])


class TechnicianCreate(BaseModel):
    user_id: str | None = None
    skills: list[str] | str | None = None
    hourly_rate: float | None = None


class TechnicianPatch(BaseModel):
    user_id: str | None = None
    skills: list[str] | str | None = None
    hourly_rate: float | None = None
    active: bool | None = None


class TechnicianSkillCreate(BaseModel):
    skill: str | None = None


class UnavailabilityCreate(BaseModel):
    start_at: datetime
    end_at: datetime
    reason: str | None = None


def jsonable_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(content))


def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", {}) or {}
    return str(tenant.get("id", ""))


async def _current_user_dependency(request: Request) -> dict[str, Any]:
    # OAuth2PasswordBearer can deadlock in some test runtimes; parse bearer token directly.
    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        return {}
    # get_current_user(request, token) — request is required first arg for
    # app-state denylist lookup. Prior call passed token as request, which
    # hit AttributeError: 'str' object has no attribute 'app' on every
    # /api/technicians call (regression since the denylist seam landed).
    return await get_current_user(request, token)


def _to_skill_list(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else [part.strip() for part in value.split(",")]
    seen: set[str] = set()
    result: list[str] = []
    for skill in raw:
        s = (skill or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        result.append(s)
    return result


def _skills_from_db(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return _to_skill_list(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        # Legacy rows store skills as comma-separated strings, newer rows as JSON.
        # Only attempt JSON parse when the payload actually looks like JSON;
        # otherwise fall back to comma-split. This avoids noisy ERROR logs for
        # every legacy tech on every page load.
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return _to_skill_list([str(s) for s in parsed])
            except json.JSONDecodeError:
                log.exception("_skills_from_db_failed")
                log.debug("technician_skills_legacy_fallback", extra={"raw": stripped[:80]})
        return _to_skill_list(raw)
    return []


def _serialize_skills(skills: list[str]) -> str:
    return json.dumps(_to_skill_list(skills))


def _tech_to_dict(
    tech: Technician,
    *,
    user_shift: tuple[Any, Any, int | None] | None = None,
    tenant_shift: tuple[Any, Any, int] = (None, None, 31),
) -> dict[str, Any]:
    """Convert a Technician ORM instance to a serializable dict.

    Sprint dispatch-capacity (2026-05-20): when ``user_shift`` and
    ``tenant_shift`` are supplied, resolves the effective shift the
    dispatch board uses for capacity. Per-field inherit: if the user's
    shift_start is NULL, use the tenant default; same for end + workdays.
    """
    u_start, u_end, u_days = (user_shift or (None, None, None))
    t_start, t_end, t_days = tenant_shift
    eff_start = u_start or t_start
    eff_end = u_end or t_end
    eff_days = u_days if u_days is not None else t_days
    data = {
        "id": tech.id,
        "user_id": tech.user_id,
        "name": tech.name,
        "email": tech.email,
        "phone": tech.phone,
        "skills": _skills_from_db(tech.skills),
        "hourly_rate": tech.hourly_rate,
        "active": bool(tech.active) if tech.active is not None else True,
        "territory": tech.territory,
        "availability_status": tech.availability_status,
        "commission_pct": tech.commission_pct,
        "created_at": tech.created_at,
        "updated_at": tech.updated_at,
        "deleted_at": tech.deleted_at,
        # Per-user override raw values (null = inherit) — UI shows "inherit".
        "shift_start": u_start.isoformat(timespec="minutes") if u_start else None,
        "shift_end": u_end.isoformat(timespec="minutes") if u_end else None,
        "workdays": int(u_days) if u_days is not None else None,
        # Effective resolved values the dispatch board uses for capacity.
        "effective_shift_start": eff_start.isoformat(timespec="minutes") if eff_start else None,
        "effective_shift_end": eff_end.isoformat(timespec="minutes") if eff_end else None,
        "effective_workdays": int(eff_days) if eff_days is not None else None,
    }
    return data


def _unavail_to_dict(u: TechUnavailability) -> dict[str, Any]:
    """Convert a TechUnavailability ORM instance to a serializable dict."""
    return {
        "id": str(u.id),
        "technician_id": u.tech_id,
        "start_at": u.start_at,
        "end_at": u.end_at,
        "reason": u.reason,
        "created_at": u.created_at,
    }


def _get_technician(db: Session, tenant_id: str, technician_id: str) -> Technician | None:
    stmt = (
        select(Technician)
        .where(Technician.id == technician_id)
        .where(Technician.company_id == tenant_id)
        .where(Technician.deleted_at.is_(None))
    )
    return db.execute(stmt).scalars().first()


# 2026-04-29 nav-cleanup: hide deliberate verification accounts from prod
# technician/dispatch lists. They live in the DB to back the JWT mint flow
# (see gdx_dispatch/docs/vps_ops.md) but should not show up to end users.
_VERIFICATION_TECH_IDS = {
    "39cd217e-c849-49ac-aa67-9894d637e6f5",  # "Test Admin" tech_id
    "50971c65-d4c9-40e6-926a-dedf42c0a284",  # "Technician" tech_id
}
_VERIFICATION_TECH_NAMES = {"test admin", "technician"}


def _is_verification_tech(tech: Technician) -> bool:
    if str(getattr(tech, "id", "")) in _VERIFICATION_TECH_IDS:
        return True
    name = (getattr(tech, "name", "") or "").strip().lower()
    return name in _VERIFICATION_TECH_NAMES


@router.get("", response_model=None)
def list_technicians(
    request: Request,
    include_verification: bool = False,
    current_user: Any = Depends(_current_user_dependency),
    db: Session = Depends(get_db),
):
    _ = current_user
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(Technician)
        .where(Technician.deleted_at.is_(None))
        .order_by(Technician.name.asc().nullslast(), Technician.created_at.desc())
    )
    rows = db.execute(stmt).scalars().all()
    if not include_verification:
        rows = [r for r in rows if not _is_verification_tech(r)]

    # Sprint dispatch-capacity — pull tenant default shift once + each
    # tech's user-row shift override (if linked). Computing the effective
    # shift here keeps the dispatch board client-side logic to "show what
    # the server resolved" rather than re-implementing inheritance in Vue.
    settings = db.query(AppSettings).first()
    tenant_shift = (
        settings.default_shift_start if settings else None,
        settings.default_shift_end if settings else None,
        int(settings.default_workdays) if settings else 31,
    )
    from uuid import UUID as _UUID
    _raw_user_ids = [str(t.user_id) for t in rows if t.user_id]
    user_uuids: list[_UUID] = []
    for uid in _raw_user_ids:
        try:
            user_uuids.append(_UUID(uid))
        except (ValueError, AttributeError):
            pass
    user_shifts: dict[str, tuple[Any, Any, int | None]] = {}
    if user_uuids:
        for u in db.execute(
            select(User.id, User.shift_start, User.shift_end, User.workdays)
            .where(User.id.in_(user_uuids))
        ).all():
            user_shifts[str(u.id)] = (u.shift_start, u.shift_end, u.workdays)

    return jsonable_response([
        _tech_to_dict(
            r,
            user_shift=user_shifts.get(str(r.user_id)) if r.user_id else None,
            tenant_shift=tenant_shift,
        )
        for r in rows
    ])


@router.post("", response_model=None)
def create_technician(
    payload: TechnicianCreate,
    request: Request,
    current_user: Any = Depends(_current_user_dependency),
    db: Session = Depends(get_db),
):
    _ = current_user
    user_id = (payload.user_id or "").strip()
    if not user_id:
        return jsonable_response({"detail": "user_id is required"}, 400)

    tenant_id = _tenant_id(request)
    now = datetime.now(UTC)
    skills_json = _serialize_skills(_to_skill_list(payload.skills))

    tech = Technician(
        id=str(uuid.uuid4()),
        company_id=tenant_id,
        user_id=user_id,
        skills=skills_json,
        hourly_rate=payload.hourly_rate,
        active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(tech)
    db.flush()
    db.refresh(tech)
    db.commit()
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str((current_user or {}).get("sub") or (current_user or {}).get("user_id") or "system"),
        action="technician_created",
        entity_type="technician",
        entity_id=str(tech.id),
        details={"user_id": tech.user_id},
        ip_address=request.client.host if request.client else None,
        request=request,
    )
    db.commit()
    log.info("technician_created", extra={"tenant_id": tenant_id, "technician_id": str(tech.id)})
    return jsonable_response(_tech_to_dict(tech), 201)


@router.get("/skills", response_model=None)
def list_all_skills_early(
    request: Request,
    current_user: Any = Depends(_current_user_dependency),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Return all unique skills across all technicians."""
    tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id", ""))
    stmt = (
        select(Technician.skills)
        .where(Technician.company_id == tenant_id)
        .where(Technician.deleted_at.is_(None))
        .where(Technician.skills.isnot(None))
    )
    rows = db.execute(stmt).scalars().all()
    all_skills: set[str] = set()
    for skills_val in rows:
        all_skills.update(_skills_from_db(skills_val))
    return jsonable_response({"skills": sorted(all_skills)})


@router.get("/{technician_id}", response_model=None)
def get_technician(
    technician_id: str,
    request: Request,
    current_user: Any = Depends(_current_user_dependency),
    db: Session = Depends(get_db),
):
    _ = current_user
    import uuid as _uuid
    try:
        _uuid.UUID(technician_id)
    except (ValueError, AttributeError):
        logging.getLogger(__name__).exception("get_technician caught exception")
        return jsonable_response({"detail": "technician not found"}, 404)
    tenant_id = _tenant_id(request)
    tech = _get_technician(db, tenant_id, technician_id)
    if not tech:
        return jsonable_response({"detail": "technician not found"}, 404)
    return jsonable_response(_tech_to_dict(tech))


@router.patch("/{technician_id}", response_model=None)
def patch_technician(
    technician_id: str,
    payload: TechnicianPatch,
    request: Request,
    current_user: Any = Depends(_current_user_dependency),
    db: Session = Depends(get_db),
):
    _ = current_user
    tenant_id = _tenant_id(request)
    tech = _get_technician(db, tenant_id, technician_id)
    if not tech:
        return jsonable_response({"detail": "technician not found"}, 404)

    updates = payload.model_dump(exclude_unset=True)

    if "user_id" in updates:
        user_id = (updates["user_id"] or "").strip()
        if not user_id:
            return jsonable_response({"detail": "user_id is required"}, 400)
        tech.user_id = user_id
    if "skills" in updates:
        tech.skills = _serialize_skills(_to_skill_list(updates["skills"]))
    if "hourly_rate" in updates:
        tech.hourly_rate = updates["hourly_rate"]
    if "active" in updates:
        tech.active = bool(updates["active"])

    tech.updated_at = datetime.now(UTC)
    db.flush()
    db.commit()
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str((current_user or {}).get("sub") or (current_user or {}).get("user_id") or "system"),
        action="technician_updated",
        entity_type="technician",
        entity_id=technician_id,
        details=updates,
        ip_address=request.client.host if request.client else None,
        request=request,
    )
    db.commit()

    # Re-fetch to ensure fresh state
    db.refresh(tech)
    return jsonable_response(_tech_to_dict(tech))


@router.delete("/{technician_id}", response_model=None)
def delete_technician(
    technician_id: str,
    request: Request,
    current_user: Any = Depends(_current_user_dependency),
    db: Session = Depends(get_db),
):
    _ = current_user
    tenant_id = _tenant_id(request)
    tech = _get_technician(db, tenant_id, technician_id)
    if not tech:
        return jsonable_response({"detail": "technician not found"}, 404)

    now = datetime.now(UTC)
    tech.deleted_at = now
    tech.updated_at = now
    db.flush()
    db.commit()
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=str((current_user or {}).get("sub") or (current_user or {}).get("user_id") or "system"),
            action="delete_technician",
            entity_type="technician",
            entity_id=str(technician_id),
            details={},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception('delete_technician_audit_failed')
    return jsonable_response({"deleted": True})


## /skills endpoint moved before /{technician_id} to fix route ordering


@router.get("/{technician_id}/skills", response_model=None)
def list_technician_skills(
    technician_id: str,
    request: Request,
    current_user: Any = Depends(_current_user_dependency),
    db: Session = Depends(get_db),
):
    _ = current_user
    tenant_id = _tenant_id(request)
    tech = _get_technician(db, tenant_id, technician_id)
    if not tech:
        return jsonable_response({"detail": "technician not found"}, 404)
    return jsonable_response({"skills": _skills_from_db(tech.skills)})


@router.post("/{technician_id}/skills", response_model=None)
def add_technician_skill(
    technician_id: str,
    payload: TechnicianSkillCreate,
    request: Request,
    current_user: Any = Depends(_current_user_dependency),
    db: Session = Depends(get_db),
):
    _ = current_user
    tenant_id = _tenant_id(request)
    tech = _get_technician(db, tenant_id, technician_id)
    if not tech:
        return jsonable_response({"detail": "technician not found"}, 404)

    skill = (payload.skill or "").strip()
    if not skill:
        return jsonable_response({"detail": "skill is required"}, 400)

    existing = _skills_from_db(tech.skills)
    if skill not in existing:
        existing.append(skill)
        tech.skills = _serialize_skills(existing)
        tech.updated_at = datetime.now(UTC)
        db.flush()
        db.commit()
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=str((current_user or {}).get("sub") or (current_user or {}).get("user_id") or "system"),
            action="add_technician_skill",
            entity_type="technician_skill",
            entity_id=str(technician_id),
            details={},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception('add_technician_skill_audit_failed')
    return jsonable_response({"skills": existing})


@router.get("/{technician_id}/availability", response_model=None)
def get_technician_availability(
    technician_id: str,
    request: Request,
    current_user: Any = Depends(_current_user_dependency),
    db: Session = Depends(get_db),
):
    _ = current_user
    tenant_id = _tenant_id(request)
    tech = _get_technician(db, tenant_id, technician_id)
    if not tech:
        return jsonable_response({"detail": "technician not found"}, 404)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(TechUnavailability)
        .where(TechUnavailability.tech_id == technician_id)
        .order_by(TechUnavailability.start_at.asc())
    )
    rows = db.execute(stmt).scalars().all()
    return jsonable_response([_unavail_to_dict(r) for r in rows])


@router.post("/{technician_id}/unavailability", response_model=None)
def add_technician_unavailability(
    technician_id: str,
    payload: UnavailabilityCreate,
    request: Request,
    current_user: Any = Depends(_current_user_dependency),
    db: Session = Depends(get_db),
):
    _ = current_user
    tenant_id = _tenant_id(request)
    tech = _get_technician(db, tenant_id, technician_id)
    if not tech:
        return jsonable_response({"detail": "technician not found"}, 404)

    if payload.end_at <= payload.start_at:
        return jsonable_response({"detail": "end_at must be greater than start_at"}, 400)

    now = datetime.now(UTC)
    unavail = TechUnavailability(
        company_id=tenant_id,
        tech_id=technician_id,
        start_at=payload.start_at,
        end_at=payload.end_at,
        reason=(payload.reason or "").strip() or None,
        created_at=now,
    )
    db.add(unavail)
    db.flush()
    db.refresh(unavail)
    db.commit()
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=str((current_user or {}).get("sub") or (current_user or {}).get("user_id") or "system"),
            action="add_technician_unavailability",
            entity_type="technician_unavailability",
            entity_id=str(technician_id),
            details={},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception('add_technician_unavailability_audit_failed')
    return jsonable_response(_unavail_to_dict(unavail), 201)
