"""
Role permissions router — fine-grained RBAC on top of admin_ops.

Tenants define named roles with explicit permission sets, then assign
users to those roles. Built-in roles are seeded per-tenant on first
access and cannot be modified or deleted.

Gate: require_module("jobs") — baseline module every tenant has.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["role_permissions"],
    dependencies=[Depends(require_module("jobs"))],
)


# ---------------------------------------------------------------------------
# Permission catalogue + built-in roles
# ---------------------------------------------------------------------------

from gdx_dispatch.core.permissions import (  # noqa: E402
    AVAILABLE_PERMISSIONS,
    BUILTIN_DESCRIPTIONS,
    BUILTIN_ROLES,
    PERMISSIONS,
    PLATFORM_LOCKED_ROLES,
    WILDCARD,
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

from gdx_dispatch.models.tenant_models import TenantRole as Role  # noqa: E402
from gdx_dispatch.models.tenant_models import UserRoleAssignment

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z][a-zA-Z0-9_ -]*$")
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("permissions")
    @classmethod
    def _must_be_known(cls, v):
        unknown = [p for p in v if p not in AVAILABLE_PERMISSIONS]
        if unknown:
            raise ValueError(f"unknown permissions: {unknown}")
        return list(dict.fromkeys(v))


class RolePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[a-zA-Z][a-zA-Z0-9_ -]*$")
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] | None = Field(default=None, max_length=100)

    @field_validator("permissions")
    @classmethod
    def _must_be_known(cls, v):
        if v is None:
            return v
        unknown = [p for p in v if p not in AVAILABLE_PERMISSIONS]
        if unknown:
            raise ValueError(f"unknown permissions: {unknown}")
        return list(dict.fromkeys(v))


class AssignRoleIn(BaseModel):
    role_id: str = Field(min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None) or {}
    return str(tenant.get("id") or "")


def _user_id(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("sub") or user.get("user_id") or "system")
    return "system"


def _serialize_role(r: Role) -> dict[str, Any]:
    try:
        perms = json.loads(r.permissions) if r.permissions else []
    except json.JSONDecodeError:
        log.exception("_serialize_role_failed")
        perms = []
    is_system = bool(r.is_system)
    return {
        "id": str(r.id),
        "name": r.name,
        "description": r.description,
        "permissions": perms,
        "is_system": is_system,
        "is_platform_locked": is_system and r.name in PLATFORM_LOCKED_ROLES,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _serialize_assignment(a: UserRoleAssignment, role: Role | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(a.id),
        "user_id": a.user_id,
        "role_id": str(a.role_id),
        "assigned_by": a.assigned_by,
        "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
    }
    if role is not None:
        out["role_name"] = role.name
        try:
            out["role_permissions"] = json.loads(role.permissions) if role.permissions else []
        except json.JSONDecodeError:
            log.exception("_serialize_assignment_failed")
            out["role_permissions"] = []
        out["is_system"] = bool(role.is_system)
    return out


def _get_role_scoped(db: Session, role_id: UUID, tenant_id: str) -> Role:
    row = db.execute(
        select(Role).where(
            Role.id == role_id,
            Role.company_id == tenant_id,
            Role.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Role not found")
    return row


def _seed_builtin_roles(db: Session, tenant_id: str) -> None:
    """Insert the 5 built-in roles for a tenant if none exist. Idempotent."""
    existing = db.execute(
        select(Role.id).where(Role.company_id == tenant_id).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return
    try:
        for name, perms in BUILTIN_ROLES.items():
            db.add(
                Role(
                    company_id=tenant_id,
                    name=name,
                    description=BUILTIN_DESCRIPTIONS.get(name),
                    permissions=json.dumps(perms),
                    is_system=True,
                )
            )
        db.commit()
    except IntegrityError:
        log.exception("role_permissions_builtin_seed_race")
        try:
            db.rollback()
        except Exception:
            log.exception("role_permissions_builtin_seed_rollback_failed")
    except Exception:
        log.exception("role_permissions_builtin_seed_failed")
        try:
            db.rollback()
        except Exception:
            log.exception("role_permissions_builtin_seed_rollback_failed")


async def _privileged_write_rate_limit(
    request: Request,
    user: dict = Depends(get_current_user),
) -> None:
    """One privileged role-permission write per second per actor.

    Slice 4.2 — abuse-loud rate limit. 429 with retry-after; all denials log
    at WARNING with actor + IP so a programmatic abuser shows up in logs.
    """
    from gdx_dispatch.core.rate_limiter import rate_limiter

    actor = _user_id(user)
    if not actor:
        return  # anonymous never reaches here, defensive
    operation = f"role-perm-write:{actor}"
    tenant_id = _tenant_id(request) or "unknown"
    try:
        allowed = await rate_limiter.check(tenant_id, operation, limit=1, window_seconds=1)
    except Exception:
        # Redis unreachable (CI / unit tests). Fail-open on infra outage —
        # backend gates already 403 the actual permission check, so we'd
        # rather let the request through than 5xx because the rate limiter
        # itself is down. Logged so an outage shows up.
        log.warning("role_permissions_rate_limit_unavailable tenant=%s actor=%s", tenant_id, actor)
        return
    if not allowed:
        ip = (request.client.host if request.client else None) or "?"
        log.warning(
            "role_permissions_rate_limit_hit actor=%s tenant=%s ip=%s path=%s",
            actor, tenant_id, ip, request.url.path,
        )
        raise HTTPException(
            status_code=429,
            detail="Too many privileged writes. Slow down.",
            headers={"Retry-After": "1"},
        )


def _enforce_delegation_cap(
    request: Request,
    requested_perms: list[str],
) -> None:
    """No-delegation-beyond-own-grant-set.

    A caller with ``settings.write`` is barred from conferring permissions
    they don't themselves hold. Prevents the audit-found escalation: an
    admin (whose contract excludes ``billing.write``) PATCHing a low-tier
    seeded role (viewer/technician/sales/accounting/dispatcher) to include
    ``billing.write`` or ``*`` and then routing users through it.

    Wildcard semantics: if the caller has ``*`` they can grant anything
    including ``*``. Otherwise every key in ``requested_perms`` must already
    be in the caller's effective set (populated on ``request.state`` by
    the ``require_permission`` dependency that ran earlier).
    """
    grantor_perms: set[str] = getattr(request.state, "user_permissions", set()) or set()
    if WILDCARD in grantor_perms:
        return
    overreach = sorted(set(requested_perms) - grantor_perms)
    if overreach:
        raise HTTPException(
            status_code=403,
            detail=f"Cannot grant permissions you don't have: {overreach}",
        )


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict | None = None,
    request: Request | None = None,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("role_permissions_audit_failed")
        try:
            db.rollback()
        except Exception:
            log.exception("role_permissions_audit_rollback_failed")


# ---------------------------------------------------------------------------
# Endpoints — Permission catalogue
# ---------------------------------------------------------------------------

@router.get("/api/role-permissions/permissions", response_model=None)
def list_permissions(_: dict = Depends(get_current_user)) -> list[str]:
    return list(AVAILABLE_PERMISSIONS)


@router.get("/api/role-permissions/permissions/catalog", response_model=None)
def list_permissions_catalog(_: dict = Depends(get_current_user)) -> list[dict[str, str]]:
    """Rich catalog: {key, label, category}. Frontend uses category for grid grouping."""
    return [{"key": k, "label": label, "category": category} for (k, label, category) in PERMISSIONS]


# Sprint 1.5 banner — was backed by a per-tenant ``TenantFeatureFlag`` raised by
# a backfill script and cleared via ack. That provisioning/feature-flag table was
# removed in the single-tenant collapse, so there is never a pending migration.
# The endpoints remain as stable no-ops for the existing frontend
# (RolePermissionsView calls both on mount).


@router.get("/api/role-permissions/migration-banner", response_model=None)
def migration_banner(
    _: dict = Depends(get_current_user),
) -> dict[str, bool]:
    return {"pending": False}


@router.post(
    "/api/role-permissions/migration-banner/ack",
    response_model=None,
    dependencies=[Depends(require_permission("settings.write")), Depends(_privileged_write_rate_limit)],
)
def ack_migration_banner(
    request: Request,
    user: Any = Depends(get_current_user),
) -> dict[str, bool]:
    return {"pending": False}


# Slice 4.4 — recent role/permission audit trail (compliance view).
@router.get("/api/admin/permission-audit", response_model=None)
def list_permission_audit(
    request: Request,
    _: dict = Depends(require_permission("settings.write")),
    db: Session = Depends(get_db),
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Recent permission/role changes for compliance review.

    Returns up to `limit` rows (max 500) of audit_log entries whose action
    starts with `role_` — covers role create/update/delete/reset, user
    role assignment/unassignment, and migration acknowledgements.
    """
    from gdx_dispatch.core.audit import AuditLog

    capped = max(1, min(int(limit or 100), 500))
    tenant_id = _tenant_id(request)
    rows = db.execute(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action.like("role_%"),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(capped)
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "action": r.action,
            "actor_id": r.user_id,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "details": r.details,
            "ip_address": r.ip_address,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Endpoints — Roles
# ---------------------------------------------------------------------------

@router.get("/api/role-permissions/roles", response_model=None)
def list_roles(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    _seed_builtin_roles(db, tenant_id)
    rows = db.execute(
        select(Role)
        .where(Role.company_id == tenant_id, Role.deleted_at.is_(None))
        .order_by(Role.is_system.desc(), Role.name.asc())
    ).scalars().all()
    return [_serialize_role(r) for r in rows]


@router.post(
    "/api/role-permissions/roles",
    response_model=None,
    status_code=201,
    dependencies=[Depends(require_permission("settings.write")), Depends(_privileged_write_rate_limit)],
)
def create_role(
    payload: RoleIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    _enforce_delegation_cap(request, payload.permissions)
    row = Role(
        company_id=tenant_id,
        name=payload.name,
        description=payload.description,
        permissions=json.dumps(payload.permissions),
        is_system=False,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        log.exception("role_permissions_create_conflict")
        db.rollback()
        raise HTTPException(status_code=409, detail="Role with this name already exists") from None
    db.refresh(row)
    _audit(
        db, tenant_id=tenant_id, user=user,
        action="role_created",
        entity_type="role",
        entity_id=str(row.id),
        details={
            "name": row.name,
            "before_permissions": None,
            "after_permissions": row.permissions,
        },
        request=request,
    )
    return _serialize_role(row)


@router.get("/api/role-permissions/roles/{role_id}", response_model=None)
def get_role(
    role_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_role_scoped(db, role_id, tenant_id)
    return _serialize_role(row)


@router.patch(
    "/api/role-permissions/roles/{role_id}",
    response_model=None,
    dependencies=[Depends(require_permission("settings.write")), Depends(_privileged_write_rate_limit)],
)
def update_role(
    role_id: UUID,
    payload: RolePatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_role_scoped(db, role_id, tenant_id)
    if row.is_system and row.name in PLATFORM_LOCKED_ROLES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{row.name}' is a platform-contract role and cannot be modified. "
                "Clone to a custom role to customize its permissions."
            ),
        )
    # Seeded roles keep their canonical name. Fail loud rather than silently
    # dropping the field so API consumers see why their rename didn't stick.
    if row.is_system and payload.name is not None and payload.name != row.name:
        raise HTTPException(
            status_code=422,
            detail=f"'{row.name}' is a seeded role; its name cannot be changed.",
        )
    if payload.permissions is not None:
        _enforce_delegation_cap(request, payload.permissions)
    before = {"name": row.name, "description": row.description, "permissions": row.permissions}
    if payload.name is not None and not row.is_system:
        row.name = payload.name
    if payload.description is not None:
        row.description = payload.description
    if payload.permissions is not None:
        row.permissions = json.dumps(payload.permissions)
    try:
        db.commit()
    except IntegrityError:
        log.exception("role_permissions_update_conflict")
        db.rollback()
        raise HTTPException(status_code=409, detail="Role with this name already exists") from None
    db.refresh(row)
    _audit(
        db, tenant_id=tenant_id, user=user,
        action="role_updated",
        entity_type="role",
        entity_id=str(role_id),
        details={
            "fields": list(payload.model_dump(exclude_unset=True).keys()),
            "name": row.name,
            "before_permissions": before["permissions"],
            "after_permissions": row.permissions,
        },
        request=request,
    )
    return _serialize_role(row)


@router.delete(
    "/api/role-permissions/roles/{role_id}",
    response_model=None,
    status_code=204,
    dependencies=[Depends(require_permission("settings.write")), Depends(_privileged_write_rate_limit)],
)
def delete_role(
    role_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    row = _get_role_scoped(db, role_id, tenant_id)
    if row.is_system:
        raise HTTPException(status_code=400, detail="Built-in roles cannot be deleted")
    before_perms = row.permissions
    role_name = row.name
    row.deleted_at = utcnow()
    db.commit()
    _audit(
        db, tenant_id=tenant_id, user=user,
        action="role_deleted",
        entity_type="role",
        entity_id=str(role_id),
        details={
            "name": role_name,
            "before_permissions": before_perms,
            "after_permissions": None,
        },
        request=request,
    )
    return None


@router.post(
    "/api/role-permissions/roles/{role_id}/reset",
    response_model=None,
    dependencies=[Depends(require_permission("settings.write")), Depends(_privileged_write_rate_limit)],
)
def reset_role_to_default(
    role_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Reset a builtin role's permissions + description to canonical seed.

    Only `is_system=True` rows whose `name` matches a key in BUILTIN_ROLES
    can be reset; custom roles raise 400.
    """
    tenant_id = _tenant_id(request)
    row = _get_role_scoped(db, role_id, tenant_id)
    canonical = BUILTIN_ROLES.get(row.name)
    if not row.is_system or canonical is None:
        raise HTTPException(status_code=400, detail="Only built-in roles can be reset")
    before_perms = row.permissions
    row.permissions = json.dumps(canonical)
    row.description = BUILTIN_DESCRIPTIONS.get(row.name)
    row.is_system = True
    db.commit()
    _audit(
        db, tenant_id=tenant_id, user=user,
        action="role_reset_to_default",
        entity_type="role",
        entity_id=str(role_id),
        details={
            "name": row.name,
            "before_permissions": before_perms,
            "after_permissions": row.permissions,
        },
        request=request,
    )
    db.refresh(row)
    return _serialize_role(row)


# ---------------------------------------------------------------------------
# Endpoints — User assignments
# ---------------------------------------------------------------------------

@router.get("/api/role-permissions/users/{user_id}/roles", response_model=None)
def list_user_roles(
    user_id: str,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    assignments = db.execute(
        select(UserRoleAssignment).where(
            UserRoleAssignment.user_id == user_id,
        )
    ).scalars().all()
    if not assignments:
        return []
    role_ids = [a.role_id for a in assignments]
    roles = db.execute(
        select(Role).where(
            Role.id.in_(role_ids),
            Role.deleted_at.is_(None),
        )
    ).scalars().all()
    by_id = {r.id: r for r in roles}
    out: list[dict[str, Any]] = []
    for a in assignments:
        role = by_id.get(a.role_id)
        if role is None:
            continue
        out.append(_serialize_assignment(a, role))
    return out


@router.post(
    "/api/role-permissions/users/{user_id}/roles",
    response_model=None,
    status_code=201,
    dependencies=[Depends(require_permission("settings.write")), Depends(_privileged_write_rate_limit)],
)
def assign_role(
    user_id: str,
    payload: AssignRoleIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    try:
        role_uuid = UUID(payload.role_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid role_id") from None

    role = _get_role_scoped(db, role_uuid, tenant_id)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    existing = db.execute(
        select(UserRoleAssignment).where(
            UserRoleAssignment.user_id == user_id,
            UserRoleAssignment.role_id == role_uuid,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _serialize_assignment(existing, role)

    assignment = UserRoleAssignment(
        company_id=tenant_id,
        user_id=user_id,
        role_id=role_uuid,
        assigned_by=_user_id(user),
    )
    db.add(assignment)
    try:
        db.commit()
    except IntegrityError:
        log.exception("role_permissions_assign_race")
        db.rollback()
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        existing = db.execute(
            select(UserRoleAssignment).where(
                UserRoleAssignment.user_id == user_id,
                UserRoleAssignment.role_id == role_uuid,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _serialize_assignment(existing, role)
        raise HTTPException(status_code=409, detail="Assignment conflict") from None
    db.refresh(assignment)

    # Keep users.role in sync with a builtin-role assignment. The permission
    # resolver prefers the assignment, and the JWT carries users.role; if the
    # two drift apart for admin/owner, the user is silently downgraded to the
    # lower set (an owner stuck on admin perms, 2026-06-26). change_role already
    # syncs both directions — assign_role must too. (Custom roles leave
    # users.role alone; the snapshot drives their perms.)
    if role.name in BUILTIN_ROLES:
        from gdx_dispatch.models.tenant_models import User as _User
        try:
            target = db.execute(select(_User).where(_User.id == UUID(str(user_id)))).scalar_one_or_none()
        except (ValueError, AttributeError):
            target = None
        if target is not None and (target.role or "") != role.name:
            target.role = role.name
            target.updated_at = utcnow()
            db.commit()
            log.info("assign_role synced users.role=%s for user=%s", role.name, user_id)

    _audit(
        db, tenant_id=tenant_id, user=user,
        action="user_role_assigned",
        entity_type="user_role_assignment",
        entity_id=str(assignment.id),
        details={"user_id": user_id, "role_id": str(role_uuid), "role_name": role.name},
        request=request,
    )
    # Privilege change → revoke the user's sessions so an unexpired token can't
    # keep pre-change caps. Kills the refresh family; the bounded access-token TTL
    # window is accepted (claim-based roles; see auth_revoke / #5 residual).
    try:
        from gdx_dispatch.core.auth_revoke import revoke_user_sessions
        revoke_user_sessions(str(user_id), reason="role_assigned")
    except Exception:
        log.exception("assign_role_revoke_failed user_id=%s", user_id)
    return _serialize_assignment(assignment, role)


@router.delete(
    "/api/role-permissions/users/{user_id}/roles/{role_id}",
    response_model=None,
    status_code=204,
    dependencies=[Depends(require_permission("settings.write")), Depends(_privileged_write_rate_limit)],
)
def unassign_role(
    user_id: str,
    role_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    assignment = db.execute(
        select(UserRoleAssignment).where(
            UserRoleAssignment.user_id == user_id,
            UserRoleAssignment.role_id == role_id,
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    db.delete(assignment)
    db.commit()
    _audit(
        db, tenant_id=tenant_id, user=user,
        action="user_role_unassigned",
        entity_type="user_role_assignment",
        entity_id=str(assignment.id),
        details={"user_id": user_id, "role_id": str(role_id)},
        request=request,
    )
    # Privilege change → revoke the user's sessions (see assign_role).
    try:
        from gdx_dispatch.core.auth_revoke import revoke_user_sessions
        revoke_user_sessions(str(user_id), reason="role_unassigned")
    except Exception:
        log.exception("unassign_role_revoke_failed user_id=%s", user_id)
    return None
