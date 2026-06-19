from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Job, JobTemplate
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/job-templates", tags=["job-templates"], dependencies=[Depends(require_module("jobs"))])


class JobTemplateCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    job_type: str = Field(min_length=1, max_length=60)
    default_priority: str = Field(default="normal", min_length=1, max_length=30)
    checklist: list[str] = Field(default_factory=list, max_length=500)
    estimated_duration: int = Field(default=60, ge=1, le=10080)
    default_parts: list[dict[str, Any]] = Field(default_factory=list, max_length=500)


class JobTemplatePatchIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=200)
    job_type: str | None = Field(default=None, min_length=1, max_length=60)
    default_priority: str | None = Field(default=None, min_length=1, max_length=30)
    checklist: list[str] | None = None
    estimated_duration: int | None = Field(default=None, ge=1)
    default_parts: list[dict[str, Any]] | None = None


class TemplateApplyIn(BaseModel):
    customer_id: UUID
    scheduled_at: datetime | None = None


def _tenant_id(request: Request | None) -> str | None:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    return tid or None


def _actor_id(user: dict[str, Any] | None) -> str:
    user = user or {}
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


def _dt(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else None


def _serialize_template(tmpl: JobTemplate) -> dict[str, Any]:
    checklist = tmpl.checklist
    if isinstance(checklist, str):
        try:
            checklist = json.loads(checklist)
        except Exception:
            log.exception("job_template_checklist_parse_failed")
            checklist = []

    default_parts = tmpl.default_parts
    if isinstance(default_parts, str):
        try:
            default_parts = json.loads(default_parts)
        except Exception:
            log.exception("job_template_default_parts_parse_failed")
            default_parts = []

    return {
        "id": str(tmpl.id),
        "title": tmpl.title,
        "job_type": tmpl.job_type,
        "default_priority": tmpl.default_priority,
        "checklist": checklist or [],
        "estimated_duration": int(tmpl.estimated_duration or 0),
        "default_parts": default_parts or [],
        "is_active": bool(tmpl.is_active),
        "created_at": _dt(tmpl.created_at),
        "updated_at": _dt(tmpl.updated_at),
    }


def _get_template_or_404(template_id: str, db: Session) -> JobTemplate:
    tmpl = db.execute(
        select(JobTemplate).where(
            JobTemplate.id == template_id,
            JobTemplate.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Job template not found")
    return tmpl


def create_job_from_template(
    db: Session,
    *,
    template: JobTemplate,
    customer_id: UUID,
    scheduled_at: datetime | None,
    company_id: str = "",
) -> Job:
    # Same derive rule as create_job: a template instantiation without a
    # date is a service call awaiting dispatcher review (2026-05-13 rename).
    derived_lifecycle = "scheduled" if scheduled_at else "service_call"
    job = Job(
        id=uuid4(),
        customer_id=customer_id,
        title=str(template.title or "Recurring job"),
        description=f"Template type: {template.job_type}",
        lifecycle_stage=derived_lifecycle,
        dispatch_status="unassigned",
        billing_status="unbilled",
        scheduled_at=scheduled_at,
        source="template",
        company_id=company_id,
    )
    db.add(job)
    db.flush()
    return job


@router.get("", response_model=None)
def list_job_templates(
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        templates = db.execute(
            select(JobTemplate)
            .where(JobTemplate.deleted_at.is_(None))
            .order_by(JobTemplate.created_at.desc())
        ).scalars().all()
        return {"items": [_serialize_template(t) for t in templates]}
    except HTTPException:
        raise
    except Exception:
        log.exception("list_job_templates_failed")
        raise HTTPException(status_code=500, detail="Failed to list job templates") from None


@router.post("", response_model=None, status_code=201)
def create_job_template(
    payload: JobTemplateCreateIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        now = datetime.now(UTC).isoformat()
        template_id = str(uuid4())
        tmpl = JobTemplate(
            id=template_id,
            title=payload.title,
            job_type=payload.job_type,
            default_priority=payload.default_priority,
            checklist=json.dumps(payload.checklist),
            estimated_duration=payload.estimated_duration,
            default_parts=json.dumps(payload.default_parts),
            is_active=1,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        db.add(tmpl)
        db.flush()
        asyncio.run(log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            user_id=_actor_id(user),
            action="job_template_created",
            entity_type="job_template",
            entity_id=template_id,
            details={"title": payload.title, "job_type": payload.job_type},
            request=request,
        ))
        db.commit()
        db.refresh(tmpl)
        return _serialize_template(tmpl)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("create_job_template_failed")
        raise HTTPException(status_code=500, detail="Failed to create job template") from None


@router.get("/{template_id}", response_model=None)
def get_job_template(
    template_id: str,
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        tmpl = _get_template_or_404(template_id, db)
        return _serialize_template(tmpl)
    except HTTPException:
        raise
    except Exception:
        log.exception("get_job_template_failed", extra={"template_id": template_id})
        raise HTTPException(status_code=500, detail="Failed to load job template") from None


@router.patch("/{template_id}", response_model=None)
def patch_job_template(
    template_id: str,
    payload: JobTemplatePatchIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        tmpl = _get_template_or_404(template_id, db)
        updates = payload.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No updatable fields provided")

        now = datetime.now(UTC).isoformat()
        if "title" in updates:
            tmpl.title = updates["title"]
        if "job_type" in updates:
            tmpl.job_type = updates["job_type"]
        if "default_priority" in updates:
            tmpl.default_priority = updates["default_priority"]
        if "checklist" in updates:
            tmpl.checklist = json.dumps(updates["checklist"] or [])
        if "estimated_duration" in updates:
            tmpl.estimated_duration = updates["estimated_duration"]
        if "default_parts" in updates:
            tmpl.default_parts = json.dumps(updates["default_parts"] or [])
        tmpl.updated_at = now

        asyncio.run(log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            user_id=_actor_id(user),
            action="job_template_updated",
            entity_type="job_template",
            entity_id=template_id,
            details={"fields": sorted(list(updates.keys()))},
            request=request,
        ))
        db.commit()
        db.refresh(tmpl)
        return _serialize_template(tmpl)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("patch_job_template_failed", extra={"template_id": template_id})
        raise HTTPException(status_code=500, detail="Failed to update job template") from None


@router.delete("/{template_id}", response_model=None)
def delete_job_template(
    template_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        tmpl = _get_template_or_404(template_id, db)
        now = datetime.now(UTC).isoformat()
        tmpl.deleted_at = now
        tmpl.is_active = 0
        asyncio.run(log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            user_id=_actor_id(user),
            action="job_template_deleted",
            entity_type="job_template",
            entity_id=template_id,
            details={},
            request=request,
        ))
        db.commit()
        return {"ok": True}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("delete_job_template_failed", extra={"template_id": template_id})
        raise HTTPException(status_code=500, detail="Failed to delete job template") from None


@router.post("/{template_id}/apply", response_model=None, status_code=201)
def apply_job_template(
    template_id: str,
    payload: TemplateApplyIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        tmpl = _get_template_or_404(template_id, db)
        job = create_job_from_template(
            db,
            template=tmpl,
            customer_id=payload.customer_id,
            scheduled_at=payload.scheduled_at,
        )
        asyncio.run(log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            user_id=_actor_id(user),
            action="job_template_applied",
            entity_type="job",
            entity_id=str(job.id),
            details={"job_template_id": template_id, "customer_id": str(payload.customer_id)},
            request=request,
        ))
        db.commit()
        return {
            "id": str(job.id),
            "customer_id": str(job.customer_id) if job.customer_id else None,
            "title": job.title,
            "lifecycle_stage": job.lifecycle_stage,
            "scheduled_at": _dt(job.scheduled_at),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("apply_job_template_failed", extra={"template_id": template_id})
        raise HTTPException(status_code=500, detail="Failed to apply job template") from None
