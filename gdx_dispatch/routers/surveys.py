"""
Surveys router — NPS + CSAT surveys for post-job customer feedback.

Flow:
- Admins define reusable survey templates (NPS/CSAT/custom).
- Staff triggers a "send": a SurveySend row is created with a single-use
  `token` (secrets.token_urlsafe) and an expiry (default 30 days). The caller
  is responsible for the actual email/SMS dispatch — this router only mints
  the token + public URL.
- Customers click the emailed/SMSed link and hit the public endpoints (no
  auth). Tenant scope is derived from the SurveySend row itself.
- A metrics endpoint computes NPS and CSAT averages over a rolling window.

Admin endpoints gated by the `customers` module. Public endpoints have no
auth — the token is the capability.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)


SURVEY_KINDS = ("nps", "csat", "custom")


# ---------------------------------------------------------------------------
# Routers — admin (gated) + public (token-only)
# ---------------------------------------------------------------------------


admin_router = APIRouter(
    tags=["surveys"],
    dependencies=[Depends(require_module("customers"))],
)

public_router = APIRouter(tags=["surveys_public"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import SurveyResponse, SurveySend, SurveyTemplate  # noqa: E402

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class SurveyTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="nps", pattern=r"^(nps|csat|custom)$")
    question: str = Field(min_length=1, max_length=500)
    follow_up_question: str | None = Field(default=None, max_length=500)
    active: bool = True


class SurveyTemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: str | None = Field(default=None, pattern=r"^(nps|csat|custom)$")
    question: str | None = Field(default=None, min_length=1, max_length=500)
    follow_up_question: str | None = Field(default=None, max_length=500)
    active: bool | None = None


class SurveySendIn(BaseModel):
    template_id: str = Field(min_length=1, max_length=64)
    customer_id: str | None = Field(default=None, max_length=64)
    job_id: str | None = Field(default=None, max_length=64)
    recipient_email: str | None = Field(default=None, max_length=254)
    recipient_phone: str | None = Field(default=None, max_length=30)
    expires_days: int = Field(default=30, ge=1, le=365)


class PublicSurveyResponseIn(BaseModel):
    score: int = Field(ge=0, le=10)
    comment: str | None = Field(default=None, max_length=5000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_id(request: Request) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: Any) -> str:
    if not isinstance(user, dict):
        return "system"
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "system")


def _client_ip(request: Request) -> str | None:
    try:
        client = getattr(request, "client", None)
        if client is None:
            return None
        return str(getattr(client, "host", None) or "")[:45] or None
    except Exception:  # return None if client information cannot be retrieved
        log.exception("surveys_client_ip_lookup_failed")
        return None


def _parse_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail=f"Invalid UUID: {value}") from None


def _serialize_template(t: SurveyTemplate) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "company_id": t.company_id,
        "name": t.name,
        "kind": t.kind,
        "question": t.question,
        "follow_up_question": t.follow_up_question,
        "active": bool(t.active),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _serialize_send(s: SurveySend) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "company_id": s.company_id,
        "template_id": str(s.template_id),
        "customer_id": str(s.customer_id) if s.customer_id else None,
        "job_id": str(s.job_id) if s.job_id else None,
        "recipient_email": s.recipient_email,
        "recipient_phone": s.recipient_phone,
        "token": s.token,
        "expires_at": s.expires_at.isoformat() if s.expires_at else None,
        "responded_at": s.responded_at.isoformat() if s.responded_at else None,
        "sent_at": s.sent_at.isoformat() if s.sent_at else None,
    }


def _serialize_response(r: SurveyResponse) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "company_id": r.company_id,
        "send_id": str(r.send_id),
        "template_id": str(r.template_id),
        "score": int(r.score),
        "comment": r.comment,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
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
        log.exception("surveys_audit_failed action=%s entity_id=%s", action, entity_id)
        db.rollback()


def _normalize_expiry(exp: datetime | None) -> tuple[datetime | None, datetime]:
    """Normalize stored expiry vs current time so SQLite/Postgres both work."""
    now = utcnow()
    if exp is None:
        return None, now
    if exp.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif exp.tzinfo is not None and now.tzinfo is None:
        exp = exp.replace(tzinfo=None)
    return exp, now


# ---------------------------------------------------------------------------
# Admin — templates
# ---------------------------------------------------------------------------


@admin_router.get("/api/surveys/templates", response_model=None)
def list_templates(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    rows = db.execute(
        select(SurveyTemplate)
        .where(
            SurveyTemplate.company_id == tenant_id,
            SurveyTemplate.deleted_at.is_(None),
        )
        .order_by(SurveyTemplate.created_at.desc())
    ).scalars().all()
    return [_serialize_template(r) for r in rows]


@admin_router.post("/api/surveys/templates", response_model=None, status_code=201)
def create_template(
    payload: SurveyTemplateIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    now = utcnow()
    tpl = SurveyTemplate(
        id=uuid4(),
        company_id=tenant_id,
        name=payload.name,
        kind=payload.kind,
        question=payload.question,
        follow_up_question=payload.follow_up_question,
        active=payload.active,
        created_at=now,
        updated_at=now,
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="survey_template_created",
        entity_type="survey_template",
        entity_id=str(tpl.id),
        details={"name": tpl.name, "kind": tpl.kind},
        request=request,
    )
    return _serialize_template(tpl)


@admin_router.patch("/api/surveys/templates/{template_id}", response_model=None)
def update_template(
    template_id: UUID,
    payload: SurveyTemplatePatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    tpl = db.execute(
        select(SurveyTemplate).where(
            SurveyTemplate.id == template_id,
            SurveyTemplate.company_id == tenant_id,
            SurveyTemplate.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Survey template not found")
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(tpl, key, value)
    tpl.updated_at = utcnow()
    db.commit()
    db.refresh(tpl)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="survey_template_updated",
        entity_type="survey_template",
        entity_id=str(tpl.id),
        details={"fields": list(data.keys())},
        request=request,
    )
    return _serialize_template(tpl)


@admin_router.delete("/api/surveys/templates/{template_id}", response_model=None, status_code=204)
def delete_template(
    template_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    tpl = db.execute(
        select(SurveyTemplate).where(
            SurveyTemplate.id == template_id,
            SurveyTemplate.company_id == tenant_id,
            SurveyTemplate.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Survey template not found")
    tpl.deleted_at = utcnow()
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="survey_template_deleted",
        entity_type="survey_template",
        entity_id=str(tpl.id),
        details={"name": tpl.name},
        request=request,
    )
    return None


# ---------------------------------------------------------------------------
# Admin — sends
# ---------------------------------------------------------------------------


@admin_router.post("/api/surveys/send", response_model=None, status_code=201)
def send_survey(
    payload: SurveySendIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    template_uuid = _parse_uuid(payload.template_id)
    tpl = db.execute(
        select(SurveyTemplate).where(
            SurveyTemplate.id == template_uuid,
            SurveyTemplate.company_id == tenant_id,
            SurveyTemplate.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Survey template not found")
    if not tpl.active:
        raise HTTPException(status_code=409, detail="Survey template is inactive")

    now = utcnow()
    token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(days=payload.expires_days)

    send = SurveySend(
        id=uuid4(),
        company_id=tenant_id,
        template_id=tpl.id,
        customer_id=_parse_uuid(payload.customer_id),
        job_id=_parse_uuid(payload.job_id),
        recipient_email=payload.recipient_email,
        recipient_phone=payload.recipient_phone,
        token=token,
        expires_at=expires_at,
        sent_at=now,
        created_at=now,
    )
    db.add(send)
    db.commit()
    db.refresh(send)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="survey_sent",
        entity_type="survey_send",
        entity_id=str(send.id),
        details={
            "template_id": str(tpl.id),
            "kind": tpl.kind,
            "has_email": bool(payload.recipient_email),
            "has_phone": bool(payload.recipient_phone),
            "expires_days": payload.expires_days,
        },
        request=request,
    )
    return {
        "send_id": str(send.id),
        "token": token,
        "public_url": f"/survey/{token}",
        "expires_at": expires_at.isoformat(),
        "template_id": str(tpl.id),
        "kind": tpl.kind,
    }


# ---------------------------------------------------------------------------
# Admin — responses + metrics
# ---------------------------------------------------------------------------


@admin_router.get("/api/surveys/responses", response_model=None)
def list_responses(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    template_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    stmt = select(SurveyResponse).where(SurveyResponse.company_id == tenant_id)
    if template_id:
        tpl_uuid = _parse_uuid(template_id)
        stmt = stmt.where(SurveyResponse.template_id == tpl_uuid)
    rows = db.execute(
        stmt.order_by(SurveyResponse.created_at.desc()).limit(limit).offset(offset)
    ).scalars().all()
    # Enrich with template kind when available for easier UI rendering.
    tpl_cache: dict[UUID, SurveyTemplate] = {}
    out: list[dict[str, Any]] = []
    for r in rows:
        data = _serialize_response(r)
        tpl = tpl_cache.get(r.template_id)
        if tpl is None:
            tpl = db.execute(
                select(SurveyTemplate).where(
                    SurveyTemplate.id == r.template_id,
                    SurveyTemplate.company_id == tenant_id,
                )
            ).scalar_one_or_none()
            if tpl is not None:
                tpl_cache[r.template_id] = tpl
        if tpl is not None:
            data["template_kind"] = tpl.kind
            data["template_name"] = tpl.name
        out.append(data)
    return out


@admin_router.get("/api/surveys/metrics", response_model=None)
def survey_metrics(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    days: int = 30,
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    days = max(1, min(int(days or 30), 3650))
    cutoff = utcnow() - timedelta(days=days)

    sends = db.execute(
        select(SurveySend).where(
            SurveySend.company_id == tenant_id,
            SurveySend.sent_at >= cutoff,
        )
    ).scalars().all()
    responses = db.execute(
        select(SurveyResponse).where(
            SurveyResponse.company_id == tenant_id,
            SurveyResponse.created_at >= cutoff,
        )
    ).scalars().all()

    total_sent = len(sends)
    total_responded = len(responses)
    response_rate = (total_responded / total_sent) if total_sent else 0.0

    # Build a template->kind map once to classify each response.
    tpl_ids = {r.template_id for r in responses}
    kind_by_tpl: dict[UUID, str] = {}
    if tpl_ids:
        tpl_rows = db.execute(
            select(SurveyTemplate).where(
                SurveyTemplate.company_id == tenant_id,
                SurveyTemplate.id.in_(tpl_ids),
            )
        ).scalars().all()
        kind_by_tpl = {t.id: t.kind for t in tpl_rows}

    nps_scores = [r.score for r in responses if kind_by_tpl.get(r.template_id) == "nps"]
    csat_scores = [r.score for r in responses if kind_by_tpl.get(r.template_id) == "csat"]

    nps_score: float | None = None
    if nps_scores:
        promoters = sum(1 for s in nps_scores if s >= 9)
        detractors = sum(1 for s in nps_scores if s <= 6)
        nps_score = round(((promoters - detractors) / len(nps_scores)) * 100, 2)

    csat_avg: float | None = None
    if csat_scores:
        csat_avg = round(sum(csat_scores) / len(csat_scores), 2)

    return {
        "window_days": days,
        "total_sent": total_sent,
        "total_responded": total_responded,
        "response_rate": round(response_rate, 4),
        "nps_score": nps_score,
        "nps_sample_size": len(nps_scores),
        "csat_avg": csat_avg,
        "csat_sample_size": len(csat_scores),
    }


# ---------------------------------------------------------------------------
# Public — token-gated (no auth)
# ---------------------------------------------------------------------------


def _load_send_by_token(db: Session, token: str) -> SurveySend:
    if not token or len(token) > 128:
        raise HTTPException(status_code=404, detail="Survey not found")
    row = db.execute(
        select(SurveySend).where(SurveySend.token == token)
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Survey not found")
    if row.responded_at is not None:
        raise HTTPException(status_code=404, detail="Survey not found")
    exp, now = _normalize_expiry(row.expires_at)
    if exp is not None and exp < now:
        raise HTTPException(status_code=404, detail="Survey not found")
    return row


@public_router.get("/api/surveys/public/{token}", response_model=None)
def public_get_survey(
    token: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    send = _load_send_by_token(db, token)
    tpl = db.execute(
        select(SurveyTemplate).where(
            SurveyTemplate.id == send.template_id,
            SurveyTemplate.company_id == send.company_id,
            SurveyTemplate.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Survey not found")
    return {
        "send_id": str(send.id),
        "kind": tpl.kind,
        "question": tpl.question,
        "follow_up_question": tpl.follow_up_question,
        "expires_at": send.expires_at.isoformat() if send.expires_at else None,
    }


@public_router.post("/api/surveys/public/{token}", response_model=None, status_code=201)
def public_submit_survey(
    token: str,
    payload: PublicSurveyResponseIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    send = _load_send_by_token(db, token)
    tpl = db.execute(
        select(SurveyTemplate).where(
            SurveyTemplate.id == send.template_id,
            SurveyTemplate.company_id == send.company_id,
            SurveyTemplate.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Survey not found")

    # Kind-specific score validation: NPS 0-10, CSAT 1-5.
    if tpl.kind == "nps":
        if payload.score < 0 or payload.score > 10:
            raise HTTPException(status_code=422, detail="NPS score must be 0-10")
    elif tpl.kind == "csat" and (payload.score < 1 or payload.score > 5):
        raise HTTPException(status_code=422, detail="CSAT score must be 1-5")
    # custom: accept full 0-10 range (already enforced by Pydantic bounds)

    now = utcnow()
    resp = SurveyResponse(
        id=uuid4(),
        company_id=send.company_id,
        send_id=send.id,
        template_id=send.template_id,
        score=payload.score,
        comment=payload.comment,
        submitted_ip=_client_ip(request),
        created_at=now,
    )
    db.add(resp)
    send.responded_at = now
    db.commit()
    db.refresh(resp)

    _audit(
        db,
        tenant_id=send.company_id,
        user={"sub": "public-survey", "email": send.recipient_email},
        action="survey_response_received",
        entity_type="survey_response",
        entity_id=str(resp.id),
        details={
            "send_id": str(send.id),
            "template_id": str(send.template_id),
            "kind": tpl.kind,
            "score": payload.score,
        },
        request=request,
    )
    return {
        "id": str(resp.id),
        "send_id": str(send.id),
        "score": resp.score,
        "created_at": resp.created_at.isoformat() if resp.created_at else None,
    }
