from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.models.tenant_models import Resource

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/resources",
    tags=["resources"],
    dependencies=[Depends(require_role("admin", "owner", "user", "tech", "dispatcher", "superadmin"))],
)

# Upload directory for resource files — must be writable inside the container
UPLOAD_DIR = os.environ.get("GDX_RESOURCE_DIR", "/tmp/gdx-resources")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ResourceCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    category: str = Field(..., pattern=r"^(extension|training|branding|app)$")
    version: str | None = None


class ResourceOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    category: str
    file_size: int | None = None
    mime_type: str | None = None
    version: str | None = None
    download_count: int = 0
    created_at: str | None = None


class ResourceListOut(BaseModel):
    items: list[ResourceOut]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_id(user: dict[str, Any]) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


def _user_role(user: dict[str, Any]) -> str:
    return str(user.get("role") or user.get("job_title") or "").lower()


def _tenant_id(request: Request | None) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    return str(tenant.get("id") or "")


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for") if hasattr(request, "headers") else None
    if xff:
        return str(xff).split(",", 1)[0].strip()
    return request.client.host if request.client else None


def _require_admin(user: dict[str, Any]) -> None:
    role = _user_role(user)
    if role not in ("admin", "owner", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin privileges required")


def _normalize_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _resource_dict(row: Resource) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "category": row.category,
        "file_size": row.file_size,
        "mime_type": row.mime_type,
        "version": row.version,
        "download_count": int(row.download_count or 0),
        "created_at": _normalize_datetime(row.created_at),
    }


def _ensure_resource_exists(db: Session, resource_id: str, company_id: str) -> Resource:
    import uuid as _uuid
    try:
        rid = _uuid.UUID(resource_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Resource not found") from None

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    row = (
        db.query(Resource)
        .filter(Resource.id == rid, Resource.deleted_at.is_(None))
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Resource not found")
    return row


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", response_model=ResourceListOut)
async def list_resources(
    category: str | None = Query(default=None),
    request: Request = None,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ResourceListOut:
    """List available resources for the tenant."""
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    query = db.query(Resource).filter(Resource.deleted_at.is_(None))
    if category:
        query = query.filter(Resource.category == category)

    total = query.count()
    rows = query.order_by(Resource.category, Resource.name).all()

    return ResourceListOut(
        items=[ResourceOut(**_resource_dict(r)) for r in rows],
        total=total,
    )


@router.get("/{resource_id}", response_model=ResourceOut)
async def get_resource(
    resource_id: str,
    request: Request = None,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ResourceOut:
    """Get a single resource's metadata."""
    row = _ensure_resource_exists(db, resource_id, _tenant_id(request))
    return ResourceOut(**_resource_dict(row))


@router.get("/{resource_id}/download")
async def download_resource(
    resource_id: str,
    request: Request = None,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Download a resource file. Increments download count."""
    company_id = _tenant_id(request)
    row = _ensure_resource_exists(db, resource_id, company_id)

    file_path = row.file_path
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found on disk")

    # Increment download count
    row.download_count = (row.download_count or 0) + 1
    row.updated_at = datetime.now(timezone.utc)
    db.commit()

    await log_audit_event(
        db=db,
        tenant_id=company_id,
        user_id=_user_id(user),
        action="resource_downloaded",
        entity_type="resource",
        entity_id=resource_id,
        details={"name": row.name, "category": row.category},
        ip_address=_client_ip(request),
        request=request,
    )
    db.commit()

    return FileResponse(
        path=file_path,
        filename=row.name,
        media_type=row.mime_type or "application/octet-stream",
    )


@router.post("", status_code=201, response_model=ResourceOut)
async def create_resource(
    name: str = Query(..., min_length=1),
    description: str = Query(default=None),
    category: str = Query(..., pattern=r"^(extension|training|branding|app)$"),
    version: str = Query(default=None),
    file: UploadFile = File(...),
    request: Request = None,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ResourceOut:
    """Upload a new resource. Admin/owner only."""
    _require_admin(user)

    company_id = _tenant_id(request)
    resource_id = uuid4()
    now = datetime.now(timezone.utc)

    # Sanitize filename + constrain destination to UPLOAD_DIR. category is an
    # allowlisted enum and company_id is tenant-scoped, but resolve+contain is
    # the durable guard against path traversal. (CodeQL path-injection)
    safe_name = os.path.basename(file.filename or "upload")
    base = os.path.realpath(UPLOAD_DIR)
    file_dest = os.path.realpath(os.path.join(base, company_id, category, f"{resource_id}_{safe_name}"))
    if os.path.commonpath([base, file_dest]) != base:
        raise HTTPException(status_code=400, detail="Invalid upload path")
    os.makedirs(os.path.dirname(file_dest), exist_ok=True)

    # Save file
    try:
        content = await file.read()
        with open(file_dest, "wb") as f:
            f.write(content)
        file_size = len(content)
    except Exception:
        log.exception("resource_upload_failed")
        raise HTTPException(status_code=500, detail="Failed to save file") from None

    mime_type = file.content_type or "application/octet-stream"

    try:
        resource = Resource(
            id=resource_id,
            company_id=company_id,
            name=name,
            description=description,
            category=category,
            file_path=file_dest,
            file_size=file_size,
            mime_type=mime_type,
            version=version,
            download_count=0,
            created_by=_user_id(user),
            created_at=now,
            updated_at=now,
        )
        db.add(resource)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        # Clean up uploaded file on DB failure
        if os.path.exists(file_dest):
            os.remove(file_dest)
        log.exception("create_resource_failed")
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from None

    await log_audit_event(
        db=db,
        tenant_id=company_id,
        user_id=_user_id(user),
        action="resource_created",
        entity_type="resource",
        entity_id=str(resource_id),
        details={"name": name, "category": category, "file_size": file_size},
        ip_address=_client_ip(request),
        request=request,
    )
    db.commit()

    log.info("resource_created", extra={"resource_id": str(resource_id), "category": category})
    refreshed = _ensure_resource_exists(db, str(resource_id), company_id)
    return ResourceOut(**_resource_dict(refreshed))


@router.delete("/{resource_id}", status_code=204)
async def delete_resource(
    resource_id: str,
    request: Request = None,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Soft-delete a resource. Admin/owner only."""
    _require_admin(user)
    import uuid as _uuid
    try:
        rid = _uuid.UUID(resource_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Resource not found") from None

    company_id = _tenant_id(request)
    now = datetime.now(timezone.utc)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    row = (
        db.query(Resource)
        .filter(Resource.id == rid, Resource.deleted_at.is_(None))
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Resource not found")

    row.deleted_at = now
    row.updated_at = now
    db.commit()

    await log_audit_event(
        db=db,
        tenant_id=company_id,
        user_id=_user_id(user),
        action="resource_deleted",
        entity_type="resource",
        entity_id=resource_id,
        details={"soft_delete": True},
        ip_address=_client_ip(request),
        request=request,
    )
    db.commit()

    log.info("resource_deleted", extra={"resource_id": resource_id})
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Seed default resources (Chrome extension, etc.)
# ---------------------------------------------------------------------------

async def seed_default_resources(db: Session, company_id: str) -> None:
    """Seed default resources for a new tenant. Called during onboarding."""

    defaults = [
        {
            "name": "GDX Supplier Portal Extension",
            "description": "Chrome extension for CHI, Clopay, Amarr, and Wayne Dalton supplier portals. Auto-fills orders and extracts pricing.",
            "category": "extension",
            "version": "1.0.0",
            "mime_type": "application/zip",
        },
        {
            "name": "Technician Quick Start Guide",
            "description": "PDF guide for new technicians — mobile app, timeclock, photo uploads, signature capture.",
            "category": "training",
            "mime_type": "application/pdf",
        },
        {
            "name": "Dispatcher Operations Manual",
            "description": "PDF guide for dispatchers — scheduling, route optimization, job assignment.",
            "category": "training",
            "mime_type": "application/pdf",
        },
        {
            "name": "GDX Mobile Shortcut",
            "description": "Add-to-homescreen shortcut for the GDX mobile web app.",
            "category": "app",
            "mime_type": "text/html",
        },
    ]

    now = datetime.now(timezone.utc)
    for res in defaults:
        existing = (
            db.query(Resource)
            .filter(Resource.name == res["name"], Resource.deleted_at.is_(None))
            .first()
        )

        if not existing:
            resource = Resource(
                id=uuid4(),
                company_id=company_id,
                name=res["name"],
                description=res.get("description"),
                category=res["category"],
                file_path=f"/tmp/gdx-resources/default/{res['category']}/{res['name'].lower().replace(' ', '_')}",
                file_size=0,
                mime_type=res.get("mime_type"),
                version=res.get("version"),
                download_count=0,
                created_by="system",
                created_at=now,
                updated_at=now,
            )
            db.add(resource)
    db.commit()
