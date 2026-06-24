from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from datetime import UTC, datetime
from uuid import UUID as _UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.core.permissions import PLATFORM_LOCKED_ROLES, assert_can_assign_role
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job, RolePermission, User
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-ops"],
    dependencies=[Depends(require_permission("settings.write"))],
)


# Admin-tier roles, matching require_role(...) usage across the other routers.
# `owner` outranks `admin` in RBAC_HIERARCHY, so gating on role == "admin" alone
# wrongly locked the owner (the seeded account) out of every endpoint here.
_ADMIN_ROLES = {"admin", "owner", "superadmin"}


def _require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Insufficient role")
    return user


def _hash_password(password: str) -> str:
    # Fail closed: bcrypt is a hard dependency. No SHA-256 fallback — a fast
    # hash for password storage is worse than erroring loudly. (CodeQL #71)
    import bcrypt

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


class UserCreate(BaseModel):
    # Bounded against DoS + DB column overflow. email is RFC-5321 max 254.
    # password min 8 enforces a minimum strength at the edge before it
    # even reaches the hash step.
    username: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(min_length=1, max_length=50)


class UserPatch(BaseModel):
    role: str | None = Field(default=None, min_length=1, max_length=50)
    active: bool | None = None


class UserInvite(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: str = Field(default="user", min_length=1, max_length=50)
    full_name: str | None = Field(default=None, max_length=200)
    # Invite messages are free-form but bounded to a reasonable email body.
    message: str | None = Field(default=None, max_length=2000)


class RolePermissionsUpdate(BaseModel):
    role: str = Field(min_length=1, max_length=50)
    # permissions list capped to prevent arbitrary-length arrays
    permissions: list[str] = Field(default_factory=list, max_length=200)


def _rows_from_csv_bytes(raw: bytes) -> list[dict]:
    decoded = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(decoded))
    return [dict(row) for row in reader]


@router.get("/users")
def list_users(
    _: dict = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> list[dict]:
    users = db.execute(select(User).order_by(User.username)).scalars().all()
    return [{"id": str(u.id), "username": u.username, "email": u.email, "role": u.role,
             "active": u.active, "created_at": str(u.created_at) if u.created_at else None,
             "updated_at": str(u.updated_at) if u.updated_at else None} for u in users]


@router.post("/users", status_code=201)
def create_user(
    body: UserCreate,
    _: dict = Depends(_require_admin),
    db: Session = Depends(get_db),
    request: Request = None,
) -> dict:
    email = body.email.strip().lower()
    # D103 (an earlier session): pre-fix this defaulted to "" if request.state.tenant
    # was missing, then queried `User.company_id == ""` — which spuriously
    # passed the uniqueness check for any insert. Three-plane: the connection
    # IS the tenant boundary (get_db), so the company_id filter is
    # redundant; drop it. Also fail loud on missing tenant context rather than
    # papering over with an empty string.
    tenant = getattr(getattr(request, "state", None), "tenant", None) or {}
    tenant_id = str(tenant.get("id", "")).strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    existing = db.execute(
        select(User.id).where(
            User.email == email,
            User.deleted_at.is_(None),
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="User with this email already exists")

    assert_can_assign_role(_, body.role)
    now = datetime.now(UTC)
    user_id = uuid4()
    u = User(id=user_id, username=body.username.strip(), email=email,
             password_hash=_hash_password(body.password), role=body.role,
             company_id=tenant_id,
             active=True, created_at=now, updated_at=now)
    db.add(u)
    db.commit()
    log_audit_event_sync(
        db=db,
        tenant_id=str(getattr(getattr(request, "state", None), "tenant", {}).get("id", "")) if request else None,
        user_id=str(_.get("sub") or _.get("user_id") or "system"),
        action="user_created",
        entity_type="user",
        entity_id=user_id,
        details={"email": body.email.strip().lower(), "role": body.role},
        ip_address=(request.client.host if request and request.client else None),
        request=request,
    )
    db.commit()
    return {
        "id": str(user_id),
        "username": body.username.strip(),
        "email": body.email.strip().lower(),
        "role": body.role,
        "active": True,
    }


@router.post("/users/invite", status_code=201)
def invite_user(
    body: UserInvite,
    _: dict = Depends(_require_admin),
    db: Session = Depends(get_db),
    request: Request = None,
) -> dict:
    """Invite a new user. Creates the record in an 'invited' state with a
    secure random placeholder password they must reset on first login.

    Closes SettingsView.vue → POST /api/admin/users/invite gap.
    """
    import secrets as _secrets
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="valid email required")
    if body.role not in ("admin", "owner", "dispatcher", "technician", "viewer", "user"):
        raise HTTPException(status_code=422, detail="invalid role")

    tenant_id = str((getattr(getattr(request, "state", None), "tenant", {}) or {}).get("id", ""))
    existing = db.execute(
        select(User.id).where(
            User.email == email,
            User.company_id == tenant_id,
            User.deleted_at.is_(None),
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="User with this email already exists")

    user_id = uuid4()
    now = datetime.now(UTC)
    invite_token = _secrets.token_urlsafe(32)
    placeholder_pw = _hash_password(_secrets.token_urlsafe(32)[:72])
    username = (body.full_name or email.split("@")[0]).strip()[:50]

    assert_can_assign_role(_, body.role)
    u = User(id=user_id, username=username, email=email, password_hash=placeholder_pw,
             role=body.role, company_id=tenant_id, active=True, must_change_password=True,
             created_at=now, updated_at=now)
    db.add(u)
    db.commit()
    log_audit_event_sync(
        db=db,
        tenant_id=str(getattr(getattr(request, "state", None), "tenant", {}).get("id", "")) if request else None,
        user_id=str(_.get("sub") or _.get("user_id") or "system"),
        action="user_invited",
        entity_type="user",
        entity_id=user_id,
        details={"email": email, "role": body.role, "invited_by": _.get("email"), "invite_token": invite_token},
        ip_address=(request.client.host if request and request.client else None),
        request=request,
    )
    db.commit()
    # Email delivery is handled by a downstream worker that picks up the
    # invite token from audit_log.details. The token is also returned so an
    # admin can hand-deliver the reset link if needed.
    return {
        "id": user_id,
        "email": email,
        "role": body.role,
        "status": "invited",
        "invite_token": invite_token,
        "message": f"User invited. Reset link: /reset-password?token={invite_token}",
    }


@router.patch("/users/{id}")
def patch_user(
    id: str,
    body: UserPatch,
    _: dict = Depends(_require_admin),
    db: Session = Depends(get_db),
    request: Request = None,
) -> dict:
    try:
        id_uuid = _UUID(str(id))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="User not found")
    current = db.execute(select(User).where(User.id == id_uuid)).scalar_one_or_none()
    if current is None:
        raise HTTPException(status_code=404, detail="User not found")

    next_role = body.role if body.role is not None else (current.role or "user")
    next_active = body.active if body.active is not None else bool(current.active)
    assert_can_assign_role(_, next_role, current.role)
    current.role = next_role
    current.active = next_active
    current.updated_at = datetime.now(UTC)
    db.commit()
    action = "user_deactivated" if next_active is False else "user_updated"
    log_audit_event_sync(
        db=db,
        tenant_id=str(getattr(getattr(request, "state", None), "tenant", {}).get("id", "")) if request else None,
        user_id=str(_.get("sub") or _.get("user_id") or "system"),
        action=action,
        entity_type="user",
        entity_id=id,
        details={"role": next_role, "active": bool(next_active)},
        ip_address=(request.client.host if request and request.client else None),
        request=request,
    )
    db.commit()
    return {
        "id": str(current.id),
        "username": current.username or "",
        "email": current.email or "",
        "role": next_role,
        "active": bool(next_active),
    }


@router.delete("/users/{id}")
def deactivate_user(
    id: str,
    _: dict = Depends(_require_admin),
    db: Session = Depends(get_db),
    request: Request = None,
) -> dict:
    try:
        id_uuid = _UUID(str(id))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="User not found")
    u = db.execute(select(User).where(User.id == id_uuid)).scalar_one_or_none()
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")

    u.active = False
    u.updated_at = datetime.now(UTC)
    db.commit()
    log_audit_event_sync(
        db=db,
        tenant_id=str(getattr(getattr(request, "state", None), "tenant", {}).get("id", "")) if request else None,
        user_id=str(_.get("sub") or _.get("user_id") or "system"),
        action="user_deactivated",
        entity_type="user",
        entity_id=id,
        details={"active": False},
        ip_address=(request.client.host if request and request.client else None),
        request=request,
    )
    db.commit()
    return {"deactivated": True, "id": id}


@router.get("/export")
def full_export(
    _: dict = Depends(_require_admin),
    db: Session = Depends(get_db),
    request: Request = None,
) -> dict:
    customers = list(
        db.execute(select(Customer).where(Customer.deleted_at.is_(None)).order_by(Customer.created_at.asc()))
        .scalars()
        .all()
    )
    jobs = list(db.execute(select(Job).where(Job.deleted_at.is_(None)).order_by(Job.created_at.asc())).scalars().all())
    invoices = list(
        db.execute(select(Invoice).where(Invoice.deleted_at.is_(None)).order_by(Invoice.created_at.asc())).scalars().all()
    )
    payload = {
        "customers": jsonable_encoder(customers),
        "jobs": jsonable_encoder(jobs),
        "invoices": jsonable_encoder(invoices),
    }
    log_audit_event_sync(
        db=db,
        tenant_id=str(getattr(getattr(request, "state", None), "tenant", {}).get("id", "")) if request else None,
        user_id=str(_.get("sub") or _.get("user_id") or "system"),
        action="data_exported",
        entity_type="tenant",
        entity_id=str(getattr(getattr(request, "state", None), "tenant", {}).get("id", "system")) if request else "system",
        details={"customers": len(customers), "jobs": len(jobs), "invoices": len(invoices)},
        ip_address=(request.client.host if request and request.client else None),
        request=request,
    )
    db.commit()
    return payload


@router.post("/import/customers")
async def import_customers(
    request: Request,
    file: UploadFile | None = File(None),
    _: dict = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict:
    rows: list[dict]
    if file is not None:
        raw = await file.read()
        filename = (file.filename or "").lower()
        if filename.endswith(".csv"):
            rows = _rows_from_csv_bytes(raw)
        elif filename.endswith(".json"):
            payload = json.loads(raw.decode("utf-8"))
            rows = payload if isinstance(payload, list) else list(payload.get("customers", []))
        else:
            raise HTTPException(status_code=400, detail="Unsupported file type")
    else:
        content_type = request.headers.get("content-type", "").lower()
        if "application/json" not in content_type:
            raise HTTPException(status_code=400, detail="Expected JSON body or CSV/JSON file upload")
        payload = await request.json()
        rows = payload if isinstance(payload, list) else list(payload.get("customers", []))

    created = 0
    updated = 0
    skipped = 0

    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        email = str(row.get("email") or "").strip().lower() or None
        phone = str(row.get("phone") or "").strip() or None
        address = str(row.get("address") or "").strip() or None
        notes = str(row.get("notes") or "").strip() or None

        existing = None
        if email:
            existing = db.execute(
                select(Customer).where(Customer.deleted_at.is_(None), Customer.email == email).limit(1)
            ).scalar_one_or_none()
        if existing is None:
            existing = db.execute(
                select(Customer).where(Customer.deleted_at.is_(None), Customer.name == name).limit(1)
            ).scalar_one_or_none()

        if existing is None:
            db.add(Customer(name=name, email=email, phone=phone, address=address, notes=notes, company_id=_["tenant_id"]))
            created += 1
        else:
            existing.name = name
            if email:
                existing.email = email
            if phone:
                existing.phone = phone
            if address:
                existing.address = address
            if notes:
                existing.notes = notes
            updated += 1

    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


@router.get("/audit-log")
def get_audit_log(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _: dict = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict:
    total = int(db.execute(select(func.count()).select_from(AuditLog)).scalar_one() or 0)
    offset = (page - 1) * page_size
    rows = list(
        db.execute(select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(offset).limit(page_size))
        .scalars()
        .all()
    )
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [
            {
                "id": str(row.id),
                "event_type": row.event_type,
                "actor_id": row.actor_id,
                "actor_role": row.actor_role,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "payload": row.payload,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }


@router.get("/permissions")
def list_role_permissions(
    _: dict = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> list[dict]:
    rps = db.execute(select(RolePermission).order_by(RolePermission.role)).scalars().all()
    return [
        {
            "role": rp.role,
            "permissions": list(json.loads(str(rp.permissions))) if rp.permissions else [],
            "updated_at": str(rp.updated_at) if rp.updated_at else None,
        }
        for rp in rps
    ]


@router.post("/permissions")
def update_role_permissions(
    body: RolePermissionsUpdate,
    _: dict = Depends(_require_admin),
    db: Session = Depends(get_db),
    request: Request = None,
) -> dict:
    # Platform-lock: the owner/admin role permission-sets cannot be rewritten
    # (mirrors role_permissions.py). Prevents privilege escalation by editing
    # the admin tier's capabilities.
    if (body.role or "").strip().lower() in PLATFORM_LOCKED_ROLES:
        raise HTTPException(
            status_code=400,
            detail="The owner and admin roles are platform-locked and cannot be edited.",
        )
    serialized = json.dumps(body.permissions)
    now = datetime.now(UTC).isoformat()

    now_dt = datetime.now(UTC)
    existing = db.execute(select(RolePermission).where(RolePermission.role == body.role)).scalar_one_or_none()
    if existing:
        existing.permissions = serialized
        existing.updated_at = now_dt
    else:
        db.add(RolePermission(role=body.role, permissions=serialized, updated_at=now_dt))
    db.commit()
    log_audit_event_sync(
        db=db,
        tenant_id=str(getattr(getattr(request, "state", None), "tenant", {}).get("id", "")) if request else None,
        user_id=str(_.get("sub") or _.get("user_id") or "system"),
        action="permissions_changed",
        entity_type="role_permissions",
        entity_id=body.role,
        details={"permissions": body.permissions},
        ip_address=(request.client.host if request and request.client else None),
        request=request,
    )
    db.commit()
    return {"role": body.role, "permissions": body.permissions, "updated_at": now}


# ---------------------------------------------------------------------------
# Bank reconciliation dashboard API
# ---------------------------------------------------------------------------

@router.get("/reconciliation")
def billing_reconciliation(
    request: Request,
    _: dict = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Run billing reconciliation — compare expected vs actual Stripe amounts."""
    from gdx_dispatch.core.database import SessionLocal
    control_db = SessionLocal()
    try:
        from gdx_dispatch.core.reconciliation import run_billing_reconciliation
        result = run_billing_reconciliation(control_db)
        log_audit_event_sync(
            db=db,
            tenant_id=str(getattr(getattr(request, "state", None), "tenant", {}).get("id", "")) if request else None,
            user_id=str(_.get("sub") or _.get("user_id") or "system"),
            action="billing_reconciliation_run",
            entity_type="reconciliation",
            entity_id="manual",
            details={"checked": result.get("checked", 0), "discrepancies": len(result.get("discrepancies", []))},
            ip_address=(request.client.host if request and request.client else None),
            request=request,
        )
        db.commit()
        return result
    except ImportError:
        log.exception("reconciliation_module_import_failed")
        raise HTTPException(status_code=503, detail="Reconciliation module not available") from None
    finally:
        control_db.close()


_RELEASES_URL = "https://api.github.com/repos/freePerro/gdx_dispatch/releases/latest"


def _ver_tuple(v: str) -> tuple[int, ...]:
    # ponytail: digits-only semver compare; "v1.10.0-rc1" -> (1, 10, 0). Enough
    # to answer "is latest newer". Swap for packaging.version if pre-release
    # ordering ever matters.
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) if nums else ()


@router.get("/update-check")
async def update_check(_: dict = Depends(_require_admin)) -> dict:
    """Compare the running release (APP_VERSION) against the latest GitHub release.

    Self-hosted boxes use this to know an update exists; they apply it with
    docker/update.sh. update_available is only true when both versions parse to
    real numbers and latest > current, so 'dev'/'latest' tags never false-alarm.
    """
    import httpx

    current = os.getenv("APP_VERSION", "dev")
    out: dict = {
        "current": current,
        "latest": None,
        "update_available": False,
        "notes_url": None,
        "error": None,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                _RELEASES_URL, headers={"Accept": "application/vnd.github+json"}
            )
        if r.status_code == 404:
            out["error"] = "no_releases"
            return out
        r.raise_for_status()
        data = r.json()
        tag = data.get("tag_name") or ""
        latest = tag.removeprefix("v").removeprefix("V") or None
        out["latest"] = latest
        out["notes_url"] = data.get("html_url")
        cur_t, lat_t = _ver_tuple(current), _ver_tuple(latest or "")
        out["update_available"] = bool(cur_t and lat_t and lat_t > cur_t)
    except Exception:
        log.warning("update_check_failed", exc_info=True)
        out["error"] = "unreachable"
    return out
