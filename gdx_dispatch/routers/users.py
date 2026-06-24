"""
User management router — full CRUD for multi-tenant user administration.
"""
from __future__ import annotations

import logging
import secrets
from datetime import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typing import Literal
from werkzeug.security import generate_password_hash

from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.log_redact import redact_email
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.core.name_normalize import humanize_name
from gdx_dispatch.core.permissions import assert_can_assign_role
from gdx_dispatch.core.tenant_ctx import bind_tenant_context
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/users",
    tags=["users"],
    dependencies=[Depends(bind_tenant_context), Depends(require_module("jobs"))],
)

VALID_ROLES = ("admin", "dispatch", "tech", "sales", "owner")


class UserCreateIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default="dispatch", pattern=r"^(admin|dispatch|tech|sales|owner)$")


class UserPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, min_length=3, max_length=254)
    route_start_address: str | None = Field(default=None, max_length=500)
    # Sprint dispatch-capacity (2026-05-20) — per-user shift override.
    # Any field can be set independently; the dispatch board falls back to
    # AppSettings.default_shift_* per-field. Send explicit null to clear
    # an override back to inherit. workdays bitmask Mon=1..Sun=64.
    shift_start: time | None = None
    shift_end: time | None = None
    workdays: int | None = Field(default=None, ge=1, le=127)


class SelfPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    route_start_address: str | None = Field(default=None, max_length=500)


class SelfPasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class RoleChangeIn(BaseModel):
    role: str = Field(pattern=r"^(admin|dispatch|tech|sales|owner)$")


class PasswordResetIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="dispatch", pattern=r"^(admin|dispatch|tech|sales|owner)$")


# Categorical reasons for an admin/owner-initiated lockout. Captured in the
# audit row's details payload alongside any free-text notes. Used by the
# Lock Out dialog in UsersView; required so SOC2 / forensics can answer
# "why was this account disabled" without parsing prose.
LOCKOUT_REASONS = ("terminated", "security_incident", "policy_violation", "suspicious_activity", "other")
LockoutReason = Literal["terminated", "security_incident", "policy_violation", "suspicious_activity", "other"]


class LockoutIn(BaseModel):
    reason: LockoutReason
    notes: str | None = Field(default=None, max_length=2000)


def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("sub") or user.get("user_id") or "system")
    return "system"


def _audit(db: Session, *, request: Request, user: Any, action: str, entity_id: str, details: dict[str, Any] | None = None) -> None:
    try:
        log_audit_event_sync(db, tenant_id=_tenant_id(request), user_id=_user_id(user),
                             action=action, entity_type="user", entity_id=entity_id,
                             details=details or {}, request=request)
        db.commit()
    except Exception:
        log.exception("user_audit_failed action=%s entity_id=%s", action, entity_id)
        try:
            db.rollback()
        except Exception:
            log.exception("user_audit_rollback_failed")


def _serialize(u: User) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "email": u.email or "",
        "name": u.full_name or u.name or "",
        "role": u.role or "user",
        "active": bool(u.active) if u.active is not None else True,
        "schedulable": bool(u.schedulable) if u.schedulable is not None else False,
        "phone": u.phone or "",
        "route_start_address": u.route_start_address or "",
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        # Sprint dispatch-capacity — per-user shift override. NULL = inherit
        # tenant default (returned as null so the UI shows "inherit").
        "shift_start": u.shift_start.isoformat(timespec="minutes") if u.shift_start else None,
        "shift_end": u.shift_end.isoformat(timespec="minutes") if u.shift_end else None,
        "workdays": int(u.workdays) if u.workdays is not None else None,
    }


def _get_user_or_404(db: Session, tenant_id: str, user_id: str) -> User:
    u = db.execute(
        select(User).where(User.id == user_id, User.company_id == tenant_id, User.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return u


@router.get("/me", response_model=None)
def get_me(request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _serialize(_get_user_or_404(db, _tenant_id(request), _user_id(user)))


@router.get("/me/permissions", response_model=None)
def get_my_permissions(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Resolved permission set for the logged-in user.

    Frontend calls this on login to populate the auth store; the sidebar,
    route guards, and v-if gates read from that set. Backend remains the
    only source of truth — these are advisory hints for UX only.

    Sprint Auth & Identity Hardening — Slice 7. Pre-fix: this endpoint
    short-circuited to ``[WILDCARD]`` when the JWT-claim role was admin
    or owner. With Slice 1+2 in place, ``user["role"]`` is now DB-derived
    on every request — but the fast path still introduces a divergence
    surface (a future code change to the resolver wouldn't be reflected
    here). Always route through ``_load_user_permissions`` so the
    snapshot/UNION/demotion logic in `gdx_dispatch/core/modules.py` is the single
    source of truth. The resolver returns ``{WILDCARD}`` for verified
    admin/owner anyway.
    """
    from gdx_dispatch.core.modules import _load_user_permissions

    legacy_role = str((user or {}).get("role") or "").lower()
    perms = _load_user_permissions(db, request, user)
    return {"permissions": sorted(perms), "role": legacy_role}


@router.patch("/me", response_model=None)
def update_me(payload: SelfPatchIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    tid = _tenant_id(request)
    uid = _user_id(user)
    u = _get_user_or_404(db, tid, uid)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=422, detail="No fields to update")
    if "name" in data:
        u.full_name = humanize_name(data["name"])
    if "phone" in data:
        u.phone = data["phone"]
    if "route_start_address" in data:
        u.route_start_address = data["route_start_address"]
    u.updated_at = utcnow()
    db.commit()
    _audit(db, request=request, user=user, action="user_self_updated", entity_id=uid, details={"changed_fields": list(data.keys())})
    db.refresh(u)
    return _serialize(u)


@router.post("/me/change-password", response_model=None)
def change_my_password(payload: SelfPasswordChangeIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, str]:
    tid = _tenant_id(request)
    uid = _user_id(user)
    u = _get_user_or_404(db, tid, uid)
    pw_hash = (u.password_hash or "").strip()
    if not pw_hash:
        raise HTTPException(status_code=400, detail="Account has no password set")
    ok = False
    try:
        import bcrypt as _bcrypt
        try:
            ok = _bcrypt.checkpw(payload.current_password.encode(), pw_hash.encode())
        except ValueError:
            from werkzeug.security import check_password_hash
            ok = check_password_hash(pw_hash, payload.current_password)
    except Exception:
        from werkzeug.security import check_password_hash
        ok = check_password_hash(pw_hash, payload.current_password)
    if not ok:
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=400, detail="New password must be different from current password")
    u.password_hash = generate_password_hash(payload.new_password)
    u.updated_at = utcnow()
    db.commit()
    _audit(db, request=request, user=user, action="user_self_password_changed", entity_id=uid, details={})
    return {"ok": "password_changed"}


@router.get("", response_model=None)
def list_users(request: Request, _: dict = Depends(get_current_user), db: Session = Depends(get_db),
               limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    users = db.execute(
        select(User).where(User.deleted_at.is_(None))
        .order_by(User.created_at.desc()).limit(limit).offset(offset)
    ).scalars().all()
    return [_serialize(u) for u in users]


@router.post("", response_model=None, status_code=201, dependencies=[Depends(require_permission("users.write"))])
def create_user(payload: UserCreateIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    # Sprint 1.0 Phase B4 (H1 + H5): user creation used to fail silently on
    # `username NOT NULL` because UserCreateIn doesn't carry a username and
    # the constructor never populated it. Fix:
    #   1. Auto-populate `username = email` (column is varchar(80); if email
    #      exceeds 80 chars the IntegrityError path below converts to 400).
    #   2. Wrap commit in IntegrityError → 400 with the failing column in
    #      `detail` so the frontend can surface a useful message.
    #   3. Post-INSERT read-your-write verification — if the row doesn't read
    #      back, raise 500. an earlier session H5 was exactly this class: direct-DB
    #      INSERT "succeeded" but no row persisted; the silent path is now
    #      gated.
    tid = _tenant_id(request)
    email = payload.email.lower().strip()
    existing = db.execute(select(User.id).where(User.email == email, User.company_id == tid, User.deleted_at.is_(None))).first()
    if existing:
        raise HTTPException(status_code=409, detail="A user with this email already exists")
    now = utcnow()
    # varchar(80) ceiling on username; truncate with suffix to stay unique-ish.
    # Full email is still stored in `email`. This only affects the `username`
    # fallback we synthesize when the caller doesn't provide one.
    username = email[:80]
    assert_can_assign_role(user, payload.role)
    u = User(id=uuid4(), company_id=tid, email=email, username=username, full_name=humanize_name(payload.name.strip()),
             password_hash=generate_password_hash(payload.password), role=payload.role,
             active=True, schedulable=True, created_at=now, updated_at=now)
    db.add(u)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Extract the violating column from the orig diag when available.
        col = ""
        orig = getattr(exc, "orig", None)
        diag = getattr(orig, "diag", None)
        if diag is not None:
            col = getattr(diag, "column_name", "") or ""
        detail = f"User could not be created: constraint on column {col!r}" if col else "User could not be created due to a database constraint"
        log.warning("user_create_integrity_error email=%s column=%s", redact_email(email), col or "?")
        raise HTTPException(status_code=400, detail=detail) from exc
    # Read-your-write verification — fail LOUD if the row is not durable.
    readback = db.execute(select(User.id).where(User.id == u.id)).scalar_one_or_none()
    if readback is None:
        log.error("user_create_readback_failed email=%s user_id=%s", redact_email(email), str(u.id))
        raise HTTPException(status_code=500, detail="User creation did not persist — please retry or contact support")
    _sync_user_role_assignment(db, tid, str(u.id), payload.role)
    db.commit()
    _audit(db, request=request, user=user, action="user_created", entity_id=str(u.id), details={"email": email, "role": payload.role})
    db.refresh(u)
    return _serialize(u)


@router.get("/{user_id}", response_model=None)
def get_user(user_id: str, request: Request, _: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _serialize(_get_user_or_404(db, _tenant_id(request), user_id))


@router.patch("/{user_id}", response_model=None, dependencies=[Depends(require_permission("users.write"))])
def update_user(user_id: str, payload: UserPatchIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    tid = _tenant_id(request)
    u = _get_user_or_404(db, tid, user_id)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=422, detail="No fields to update")
    if "name" in data:
        u.full_name = humanize_name(data["name"])
    if "phone" in data:
        u.phone = data["phone"]
    if "email" in data and data["email"]:
        u.email = data["email"].lower().strip()
    if "route_start_address" in data:
        u.route_start_address = data["route_start_address"]
    if "shift_start" in data:
        u.shift_start = data["shift_start"]
    if "shift_end" in data:
        u.shift_end = data["shift_end"]
    if "workdays" in data:
        u.workdays = data["workdays"]
    u.updated_at = utcnow()
    db.commit()
    _audit(db, request=request, user=user, action="user_updated", entity_id=user_id, details={"changed_fields": list(data.keys())})
    db.refresh(u)
    return _serialize(u)


# Legacy role-name → new TenantRole-name mapping. The legacy enum used
# `dispatch`/`tech`; the RBAC catalog uses `dispatcher`/`technician`.
_LEGACY_ROLE_MAP = {
    "dispatch": "dispatcher",
    "tech": "technician",
}


def _sync_user_role_assignment(db: Session, tenant_id: str, user_id: str, legacy_role: str) -> None:
    """Mirror a legacy role change into user_role_assignments.

    Without this, the new RBAC layer (which reads from user_role_assignments
    first) ignores legacy-role updates: an admin demoted via /users/{id}/role
    still resolves to their pre-demotion permissions until their assignment
    row is rewritten. This wipes the user's current assignments and inserts
    a single assignment pointing at the matching builtin TenantRole.
    """
    from gdx_dispatch.models.tenant_models import TenantRole, UserRoleAssignment
    from sqlalchemy import select as _select

    target_name = _LEGACY_ROLE_MAP.get(legacy_role, legacy_role)
    role_row = db.execute(
        _select(TenantRole).where(
            TenantRole.company_id == tenant_id,
            TenantRole.name == target_name,
            TenantRole.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    # Drop the user's existing assignments regardless. If the role doesn't
    # exist (e.g. legacy tenant pre-backfill), the user gets no assignment
    # and the legacy User.role fallback in _load_user_permissions takes over.
    db.query(UserRoleAssignment).filter(
        UserRoleAssignment.company_id == tenant_id,
        UserRoleAssignment.user_id == user_id,
    ).delete(synchronize_session=False)
    if role_row is not None:
        db.add(
            UserRoleAssignment(
                company_id=tenant_id,
                user_id=user_id,
                role_id=role_row.id,
            )
        )


@router.post("/{user_id}/role", response_model=None, dependencies=[Depends(require_permission("users.write"))])
def change_role(user_id: str, payload: RoleChangeIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    tid = _tenant_id(request)
    u = _get_user_or_404(db, tid, user_id)
    if _user_id(user) == user_id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    old_role = u.role or "user"
    assert_can_assign_role(user, payload.role, old_role)
    u.role = payload.role
    u.updated_at = utcnow()
    _sync_user_role_assignment(db, tid, user_id, payload.role)
    db.commit()
    # Sprint Auth & Identity Hardening — Slice 3.
    # Role changed → revoke open refresh tokens. The currently-active
    # access token still works until its short TTL expires; the refresh
    # handler (Slice 1) re-derives role from `users.role`, so even if
    # the access token replays during the brief window the new role is
    # the one that gets minted on the next refresh attempt.
    revoked = 0
    if old_role != payload.role:
        try:
            from gdx_dispatch.core.auth_revoke import revoke_user_sessions

            revoked = revoke_user_sessions(str(user_id), reason="role_changed")
        except Exception:
            log.exception("change_role_revoke_failed user_id=%s", user_id)
    _audit(
        db, request=request, user=user, action="user_role_changed",
        entity_id=user_id,
        details={"old_role": old_role, "new_role": payload.role, "sessions_revoked": revoked},
    )
    db.refresh(u)
    return _serialize(u)


# Only admin + owner roles can lock or unlock users. require_permission
# alone is insufficient because tenant-customizable roles (dispatcher /
# sales / etc.) can be granted users.write, which would otherwise let a
# dispatcher with that permission disable an admin. Doug 2026-05-20.
LOCKOUT_ACTOR_ROLES = ("admin", "owner")


def _require_lockout_actor(user: Any) -> None:
    role = ""
    if isinstance(user, dict):
        role = str(user.get("role") or "").lower()
    if role not in LOCKOUT_ACTOR_ROLES:
        raise HTTPException(status_code=403, detail="Only admins or owners can lock or unlock users")


@router.post("/{user_id}/lockout", response_model=None, dependencies=[Depends(require_permission("users.write"))])
def lockout_user(user_id: str, payload: LockoutIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Admin-initiated lockout. Sets active=false and revokes every refresh
    token in the user's family. The locked user's *user-bearer* access
    tokens are rejected on the next request by `_db_verify_user`
    (auth/core.py), which reads `users.active` on every authenticated
    call. Distinct from `delete_user`: lockout is reversible via `/unlock`
    and retains the row for legal hold / forensics.

    Authorization: `require_permission("users.write")` + actor must be
    admin or owner. The permission gate alone is insufficient because
    tenants can grant `users.write` to non-platform-locked roles.

    Owners cannot be locked out from this surface — the UI hides the button
    and this endpoint 400s defense-in-depth. Avoids the "last owner" edge
    case entirely.

    Known gap (tracked in D-pat-lockout-bypass): PAT-bearer access tokens
    skip the `users.active` check in `_db_verify_user` (service_account
    actor_kind shortcut), so a user-owned PAT issued before lockout keeps
    working. PAT revocation needs to land in `gdx_dispatch/core/pat_validation.py`
    next. Until then, lockout is fully effective for browser sessions
    only; ops should also revoke any user-owned PATs out-of-band.
    """
    _require_lockout_actor(user)
    tid = _tenant_id(request)
    u = _get_user_or_404(db, tid, user_id)
    if _user_id(user) == user_id:
        raise HTTPException(status_code=400, detail="Cannot lock yourself out")
    if (u.role or "").lower() == "owner":
        raise HTTPException(status_code=400, detail="Owners cannot be locked out")
    if u.active is False:
        raise HTTPException(status_code=409, detail="User is already locked out")
    u.active = False
    u.updated_at = utcnow()
    db.commit()
    # Revoke the refresh family. If Redis is down the row flip still
    # stands (active=false stops the next request via _db_verify_user) but
    # we surface the partial failure in the audit details so ops can
    # distinguish "fully revoked" from "DB-only revoke pending Redis recovery."
    revoked: int | str = 0
    try:
        from gdx_dispatch.core.auth_revoke import revoke_user_sessions

        revoked = revoke_user_sessions(str(user_id), reason="user_locked")
    except Exception:
        log.exception("lockout_revoke_failed user_id=%s", user_id)
        revoked = "error"
    _audit(
        db, request=request, user=user, action="user_locked",
        entity_id=user_id,
        details={
            "reason": payload.reason,
            "notes": (payload.notes or "").strip() or None,
            "sessions_revoked": revoked,
            "locked_by": _user_id(user),
        },
    )
    return _serialize(u)


@router.post("/{user_id}/unlock", response_model=None, dependencies=[Depends(require_permission("users.write"))])
def unlock_user(user_id: str, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Reverse a prior lockout. Sets active=true and writes a `user_unlocked`
    audit row. The currently-revoked refresh family stays revoked (the
    user will sign in fresh on next login) — that's intentional.
    Authorization: admin or owner only (same gate as `/lockout`).
    """
    _require_lockout_actor(user)
    tid = _tenant_id(request)
    u = _get_user_or_404(db, tid, user_id)
    if u.active is True:
        raise HTTPException(status_code=409, detail="User is already active")
    u.active = True
    u.updated_at = utcnow()
    db.commit()
    _audit(
        db, request=request, user=user, action="user_unlocked",
        entity_id=user_id,
        details={"unlocked_by": _user_id(user)},
    )
    return _serialize(u)


@router.get("/{user_id}/lockout-info", response_model=None, dependencies=[Depends(require_permission("users.write"))])
def get_lockout_info(user_id: str, request: Request, _: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return the most recent `user_locked` audit row for a user, so the
    Locked badge can reveal who locked them and why on click. 404 if the
    user has no lockout history.

    Tenant isolation is the connection itself — `db` is already scoped to
    the current tenant by `get_db`, so we filter by entity_id only
    and let the connection enforce tenant boundaries (mirrors the
    `tenant_plane_redundant_filter_scan` rule).
    """
    tid = _tenant_id(request)
    _get_user_or_404(db, tid, user_id)  # 404s if outside tenant
    row = db.execute(
        select(AuditLog)
        .where(
            AuditLog.action == "user_locked",
            AuditLog.entity_type == "user",
            AuditLog.entity_id == user_id,
        )
        .order_by(desc(AuditLog.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No lockout history for this user")
    details = row.details or {}
    return {
        "reason": details.get("reason"),
        "notes": details.get("notes"),
        "locked_by": details.get("locked_by"),
        "locked_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.post("/{user_id}/toggle-schedulable", response_model=None, dependencies=[Depends(require_permission("users.write"))])
def toggle_schedulable(user_id: str, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    tid = _tenant_id(request)
    u = _get_user_or_404(db, tid, user_id)
    u.schedulable = not (u.schedulable if u.schedulable is not None else False)
    u.updated_at = utcnow()
    db.commit()
    _audit(db, request=request, user=user, action="user_schedulable_toggled", entity_id=user_id, details={"schedulable": u.schedulable})
    db.refresh(u)
    return _serialize(u)


@router.post("/{user_id}/reset-password", response_model=None, dependencies=[Depends(require_permission("users.write"))])
def reset_password(user_id: str, payload: PasswordResetIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, str]:
    tid = _tenant_id(request)
    u = _get_user_or_404(db, tid, user_id)
    u.password_hash = generate_password_hash(payload.new_password)
    u.updated_at = utcnow()
    db.commit()
    _audit(db, request=request, user=user, action="user_password_reset", entity_id=user_id, details={"reset_by": _user_id(user)})
    return {"ok": "password_reset", "user_id": user_id}


@router.post("/{user_id}/send-reset-link", response_model=None, dependencies=[Depends(require_permission("users.write"))])
def send_reset_link(user_id: str, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, str]:
    tid = _tenant_id(request)
    u = _get_user_or_404(db, tid, user_id)
    if not u.email:
        raise HTTPException(status_code=400, detail="User has no email address on file")
    reset_token = secrets.token_urlsafe(32)
    log.info("password_reset_link_generated", extra={"tenant_id": tid, "user_id": user_id, "email": redact_email(u.email), "token_prefix": reset_token[:8]})
    _audit(db, request=request, user=user, action="password_reset_link_sent", entity_id=user_id, details={"email": u.email, "initiated_by": _user_id(user)})
    return {"ok": "reset_link_queued", "email": u.email}


@router.delete("/{user_id}", response_model=None, dependencies=[Depends(require_permission("users.write"))])
def delete_user(user_id: str, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    tid = _tenant_id(request)
    u = _get_user_or_404(db, tid, user_id)
    if _user_id(user) == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    now = utcnow()
    u.deleted_at = now
    u.updated_at = now
    db.commit()
    # Sprint Auth & Identity Hardening — Slice 3.
    # Pre-fix `delete_user` only soft-deleted the row; the deleted user's
    # refresh + access tokens kept working until natural expiration.
    # Combined with the refresh handler trusting JWT `role` claims, that
    # made "delete an admin" a no-op against an in-flight admin session.
    # Now we add every JTI in the user's refresh family to the global
    # used set — the next refresh attempt 401s; the access token dies on
    # its ~15-minute TTL.
    try:
        from gdx_dispatch.core.auth_revoke import revoke_user_sessions

        revoked = revoke_user_sessions(str(user_id), reason="user_deleted")
    except Exception:
        log.exception("delete_user_revoke_failed user_id=%s", user_id)
        revoked = 0
    _audit(
        db, request=request, user=user, action="user_deleted",
        entity_id=user_id,
        details={"deleted_by": _user_id(user), "sessions_revoked": revoked},
    )
    return {"deleted": True, "id": user_id, "sessions_revoked": revoked}


@router.post("/invite", response_model=None, status_code=201, dependencies=[Depends(require_permission("users.write"))])
def invite_user(payload: InviteIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    tid = _tenant_id(request)
    email = payload.email.lower().strip()
    existing = db.execute(select(User.id).where(User.email == email, User.company_id == tid, User.deleted_at.is_(None))).first()
    if existing:
        raise HTTPException(status_code=409, detail="A user with this email already exists")
    assert_can_assign_role(user, payload.role)
    temp_password = secrets.token_urlsafe(16)
    now = utcnow()
    u = User(id=uuid4(), company_id=tid, email=email, full_name=payload.name.strip(),
             password_hash=generate_password_hash(temp_password), role=payload.role,
             active=True, schedulable=False, created_at=now, updated_at=now)
    db.add(u)
    db.commit()
    _sync_user_role_assignment(db, tid, str(u.id), payload.role)
    db.commit()
    log.info("user_invite_email_queued", extra={"tenant_id": tid, "invited_user_id": str(u.id), "email": email, "invited_by": _user_id(user)})
    _audit(db, request=request, user=user, action="user_invited", entity_id=str(u.id), details={"email": email, "role": payload.role, "invited_by": _user_id(user)})
    db.refresh(u)
    return _serialize(u)
