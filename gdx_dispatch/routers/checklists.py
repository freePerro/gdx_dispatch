from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Checklist, ChecklistItem, ChecklistTemplate
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["checklists"], dependencies=[Depends(require_module("jobs"))])


class ChecklistTemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    items: list[str] = Field(default_factory=list, max_length=500)


class ChecklistTemplateResponse(BaseModel):
    id: str
    name: str
    items: list[str]
    created_at: str


class JobChecklistCreateRequest(BaseModel):
    template_id: str = Field(min_length=1, max_length=64)


class ChecklistItemUpdateRequest(BaseModel):
    completed: bool


class ChecklistItemResponse(BaseModel):
    id: str
    label: str
    completed: bool


class ChecklistResponse(BaseModel):
    id: str
    job_id: str
    template_id: str
    created_at: str
    items: list[ChecklistItemResponse]


def _tenant_id(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id") or "")


def _user_id(current_user: Any) -> str:
    user = current_user or {}
    return str(user.get("user_id") or user.get("sub") or "system")


def _list_items(db: Session, tenant_id: str, checklist_id: str) -> list[ChecklistItemResponse]:
    rows = db.execute(
        select(ChecklistItem).where(
            ChecklistItem.tenant_id == tenant_id, ChecklistItem.checklist_id == checklist_id
        ).order_by(ChecklistItem.id)
    ).scalars().all()
    return [ChecklistItemResponse(id=r.id, label=r.item_label, completed=bool(r.completed)) for r in rows]


@router.get("/api/checklist-templates", response_model=list[ChecklistTemplateResponse])
def list_checklist_templates(
    request: Request, current_user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
) -> list[ChecklistTemplateResponse]:
    tenant_id = _tenant_id(request)
    try:
        rows = db.execute(
            select(ChecklistTemplate).where(ChecklistTemplate.tenant_id == tenant_id)
            .order_by(ChecklistTemplate.created_at.desc())
        ).scalars().all()
        return [
            ChecklistTemplateResponse(
                id=r.id, name=r.name, items=list(json.loads(r.items_json)) if r.items_json else [],
                created_at=r.created_at or "",
            )
            for r in rows
        ]
    except SQLAlchemyError:
        log.exception("checklist_templates_list_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to list checklist templates") from None


@router.post("/api/checklist-templates", response_model=ChecklistTemplateResponse, status_code=201)
def create_checklist_template(
    payload: ChecklistTemplateCreateRequest, request: Request,
    current_user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
) -> ChecklistTemplateResponse:
    tenant_id = _tenant_id(request)
    row_id = str(uuid4())
    created_at = datetime.now(UTC).isoformat()
    try:
        db.add(ChecklistTemplate(id=row_id, tenant_id=tenant_id, name=payload.name,
                                  items_json=json.dumps(payload.items), created_at=created_at))
        db.commit()
        asyncio.run(log_audit_event(db=db, tenant_id=tenant_id, user_id=_user_id(current_user),
                                     action="checklist_template_created", entity_type="checklist_template",
                                     entity_id=row_id, details=payload.model_dump(mode="json"), request=request))
        db.commit()
        return ChecklistTemplateResponse(id=row_id, name=payload.name, items=payload.items, created_at=created_at)
    except SQLAlchemyError:
        db.rollback()
        log.exception("checklist_template_create_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to create checklist template") from None


@router.get("/api/jobs/{job_id}/checklist", response_model=ChecklistResponse)
def get_job_checklist(
    job_id: str, request: Request, current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChecklistResponse:
    tenant_id = _tenant_id(request)
    try:
        cl = db.execute(
            select(Checklist).where(Checklist.tenant_id == tenant_id, Checklist.job_id == job_id)
            .order_by(Checklist.created_at.desc())
        ).scalars().first()
        if not cl:
            raise HTTPException(status_code=404, detail="Checklist not found")
        return ChecklistResponse(id=cl.id, job_id=job_id, template_id=cl.template_id,
                                  created_at=cl.created_at or "", items=_list_items(db, tenant_id, cl.id))
    except SQLAlchemyError:
        log.exception("job_checklist_get_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        raise HTTPException(status_code=500, detail="Failed to fetch checklist") from None


@router.post("/api/jobs/{job_id}/checklist", response_model=ChecklistResponse, status_code=201)
def create_job_checklist(
    job_id: str, payload: JobChecklistCreateRequest, request: Request,
    current_user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
) -> ChecklistResponse:
    tenant_id = _tenant_id(request)
    checklist_id = str(uuid4())
    created_at = datetime.now(UTC).isoformat()
    try:
        tmpl = db.execute(
            select(ChecklistTemplate).where(ChecklistTemplate.tenant_id == tenant_id, ChecklistTemplate.id == payload.template_id)
        ).scalar_one_or_none()
        if not tmpl:
            raise HTTPException(status_code=404, detail="Checklist template not found")

        db.add(Checklist(id=checklist_id, tenant_id=tenant_id, job_id=job_id,
                          template_id=payload.template_id, created_at=created_at))
        for label in list(json.loads(tmpl.items_json)) if tmpl.items_json else []:
            db.add(ChecklistItem(id=str(uuid4()), tenant_id=tenant_id, checklist_id=checklist_id,
                                  item_label=str(label), completed=0))
        db.commit()
        asyncio.run(log_audit_event(db=db, tenant_id=tenant_id, user_id=_user_id(current_user),
                                     action="job_checklist_created", entity_type="checklist",
                                     entity_id=checklist_id, details={"job_id": job_id, "template_id": payload.template_id},
                                     request=request))
        db.commit()
        return ChecklistResponse(id=checklist_id, job_id=job_id, template_id=payload.template_id,
                                  created_at=created_at, items=_list_items(db, tenant_id, checklist_id))
    except SQLAlchemyError:
        db.rollback()
        log.exception("job_checklist_create_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        raise HTTPException(status_code=500, detail="Failed to create checklist") from None


@router.patch("/api/checklists/{checklist_id}/items/{item_id}", response_model=ChecklistItemResponse)
def update_checklist_item(
    checklist_id: str, item_id: str, payload: ChecklistItemUpdateRequest, request: Request,
    current_user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
) -> ChecklistItemResponse:
    tenant_id = _tenant_id(request)
    try:
        item = db.execute(
            select(ChecklistItem).where(
                ChecklistItem.tenant_id == tenant_id, ChecklistItem.checklist_id == checklist_id,
                ChecklistItem.id == item_id,
            )
        ).scalar_one_or_none()
        if not item:
            raise HTTPException(status_code=404, detail="Checklist item not found")
        item.completed = int(payload.completed)
        db.commit()
        asyncio.run(log_audit_event(db=db, tenant_id=tenant_id, user_id=_user_id(current_user),
                                     action="checklist_item_updated", entity_type="checklist_item",
                                     entity_id=item_id, details={"completed": payload.completed}, request=request))
        db.commit()
        return ChecklistItemResponse(id=item_id, label=item.item_label, completed=payload.completed)
    except SQLAlchemyError:
        db.rollback()
        log.exception("checklist_item_update_failed", extra={"tenant_id": tenant_id, "item_id": item_id})
        raise HTTPException(status_code=500, detail="Failed to update checklist item") from None
