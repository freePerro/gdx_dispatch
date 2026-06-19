"""
Signatures router — digital signature capture for estimates/invoices/work orders/completion docs.

Two flows:
- In-person: tech captures a canvas signature on the tablet (base64 PNG) via the
  admin endpoint while handing the device to the customer.
- Remote: staff generates a single-use signing token (7-day default expiry) that
  is emailed to the customer. The customer hits the public endpoints (no auth) to
  load the signing page and submit a signature.

Admin endpoints are gated behind the `estimates` module. Public endpoints do not
require auth/tenant context — tenant scope is derived from the signature row
itself (the token is the capability).
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta
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


DOCUMENT_TYPES = ("estimate", "invoice", "work_order", "completion")
STATUS_PENDING = "pending"
STATUS_SIGNED = "signed"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Routers — admin (gated) and public (token-only)
# ---------------------------------------------------------------------------


admin_router = APIRouter(
    tags=["signatures"],
    dependencies=[Depends(require_module("estimates"))],
)

public_router = APIRouter(tags=["signatures_public"])


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import DocumentSignature  # noqa: E402

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class InPersonSignatureIn(BaseModel):
    document_type: str = Field(pattern=r"^(estimate|invoice|work_order|completion)$")
    document_id: str = Field(min_length=1, max_length=64)
    signature_data: str = Field(min_length=1, max_length=1_400_000)
    signed_by: str = Field(min_length=1, max_length=200)
    signed_by_email: str | None = Field(default=None, max_length=254)


class RemoteSignatureRequestIn(BaseModel):
    document_type: str = Field(pattern=r"^(estimate|invoice|work_order|completion)$")
    document_id: str = Field(min_length=1, max_length=64)
    customer_email: str = Field(min_length=3, max_length=254)
    expires_days: int = Field(default=7, ge=1, le=30)


class PublicSignIn(BaseModel):
    signature_data: str = Field(min_length=1, max_length=1_400_000)
    signed_by: str = Field(min_length=1, max_length=200)
    signed_by_email: str | None = Field(default=None, max_length=254)


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


def _user_label(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return user.get("email") or user.get("name") or user.get("sub")


def _client_ip(request: Request) -> str | None:
    try:
        client = getattr(request, "client", None)
        if client is None:
            return None
        return str(getattr(client, "host", None) or "")[:45] or None
    except Exception:  # return None if client information is unavailable or malformed
        log.exception("signatures_client_ip_lookup_failed")
        return None


def _serialize(sig: DocumentSignature, *, include_signature: bool = True) -> dict[str, Any]:
    data = {
        "id": str(sig.id),
        "company_id": sig.company_id,
        "document_type": sig.document_type,
        "document_id": sig.document_id,
        "status": sig.status,
        "signed_by": sig.signed_by,
        "signed_by_email": sig.signed_by_email,
        "signed_at": sig.signed_at.isoformat() if sig.signed_at else None,
        "signed_ip": sig.signed_ip,
        "token": sig.token,
        "token_expires_at": sig.token_expires_at.isoformat() if sig.token_expires_at else None,
        "requested_by": sig.requested_by,
        "requested_at": sig.requested_at.isoformat() if sig.requested_at else None,
        "created_at": sig.created_at.isoformat() if sig.created_at else None,
    }
    if include_signature:
        data["signature_data"] = sig.signature_data
    return data


def _public_view(sig: DocumentSignature) -> dict[str, Any]:
    """Minimal view for the public signing page — hides tenant-sensitive metadata."""
    return {
        "id": str(sig.id),
        "document_type": sig.document_type,
        "document_id": sig.document_id,
        "status": sig.status,
        "token_expires_at": sig.token_expires_at.isoformat() if sig.token_expires_at else None,
    }


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
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
            entity_type="signature",
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("signatures_audit_failed action=%s entity_id=%s", action, entity_id)
        db.rollback()


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


@admin_router.get("/api/signatures/pending", response_model=None)
def list_pending_signatures(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    rows = db.execute(
        select(DocumentSignature)
        .where(
            DocumentSignature.company_id == tenant_id,
            DocumentSignature.status == STATUS_PENDING,
            DocumentSignature.deleted_at.is_(None),
        )
        .order_by(DocumentSignature.requested_at.desc())
    ).scalars().all()
    return [_serialize(r, include_signature=False) for r in rows]


@admin_router.get("/api/signatures/{document_type}/{document_id}", response_model=None)
def get_signatures_for_document(
    document_type: str,
    document_id: str,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    if document_type not in DOCUMENT_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid document_type. Must be one of: {DOCUMENT_TYPES}")
    tenant_id = _tenant_id(request)
    rows = db.execute(
        select(DocumentSignature)
        .where(
            DocumentSignature.company_id == tenant_id,
            DocumentSignature.document_type == document_type,
            DocumentSignature.document_id == document_id,
            DocumentSignature.deleted_at.is_(None),
        )
        .order_by(DocumentSignature.created_at.desc())
    ).scalars().all()
    return [_serialize(r) for r in rows]


@admin_router.post("/api/signatures", response_model=None, status_code=201)
def create_in_person_signature(
    payload: InPersonSignatureIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    now = utcnow()
    sig = DocumentSignature(
        id=uuid4(),
        company_id=tenant_id,
        document_type=payload.document_type,
        document_id=payload.document_id,
        status=STATUS_SIGNED,
        signature_data=payload.signature_data,
        signed_by=payload.signed_by,
        signed_by_email=payload.signed_by_email,
        signed_at=now,
        signed_ip=_client_ip(request),
        requested_by=_user_label(user),
        requested_at=now,
        created_at=now,
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="signature_signed",
        entity_id=str(sig.id),
        details={
            "document_type": sig.document_type,
            "document_id": sig.document_id,
            "flow": "in_person",
        },
        request=request,
    )
    return _serialize(sig)


@admin_router.post("/api/signatures/request-remote", response_model=None, status_code=201)
def request_remote_signature(
    payload: RemoteSignatureRequestIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    now = utcnow()
    token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(days=payload.expires_days)

    sig = DocumentSignature(
        id=uuid4(),
        company_id=tenant_id,
        document_type=payload.document_type,
        document_id=payload.document_id,
        status=STATUS_PENDING,
        signed_by_email=payload.customer_email,
        token=token,
        token_expires_at=expires_at,
        requested_by=_user_label(user),
        requested_at=now,
        created_at=now,
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="signature_requested",
        entity_id=str(sig.id),
        details={
            "document_type": sig.document_type,
            "document_id": sig.document_id,
            "customer_email": payload.customer_email,
            "expires_days": payload.expires_days,
        },
        request=request,
    )
    return {
        "id": str(sig.id),
        "token": token,
        "signing_url": f"/sign/{token}",
        "expires_at": expires_at.isoformat(),
        "status": sig.status,
        "document_type": sig.document_type,
        "document_id": sig.document_id,
    }


@admin_router.post("/api/signatures/{signature_id}/cancel", response_model=None)
def cancel_signature(
    signature_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    sig = db.execute(
        select(DocumentSignature).where(
            DocumentSignature.id == signature_id,
            DocumentSignature.company_id == tenant_id,
            DocumentSignature.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not sig:
        raise HTTPException(status_code=404, detail="Signature not found")
    if sig.status != STATUS_PENDING:
        raise HTTPException(status_code=409, detail=f"Cannot cancel signature in status '{sig.status}'")
    sig.status = STATUS_CANCELLED
    # Invalidate any remote token
    sig.token = None
    db.commit()
    db.refresh(sig)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="signature_cancelled",
        entity_id=str(sig.id),
        details={"document_type": sig.document_type, "document_id": sig.document_id},
        request=request,
    )
    return _serialize(sig, include_signature=False)


# ---------------------------------------------------------------------------
# Public endpoints (token-gated, no auth)
# ---------------------------------------------------------------------------


def _load_token_row(db: Session, token: str) -> DocumentSignature:
    """Load a pending signature row by token, enforcing expiry and single-use."""
    if not token or len(token) > 128:
        raise HTTPException(status_code=404, detail="Signature request not found")
    row = db.execute(
        select(DocumentSignature).where(
            DocumentSignature.token == token,
            DocumentSignature.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Signature request not found")
    if row.status != STATUS_PENDING:
        raise HTTPException(status_code=404, detail="Signature request not found")
    if row.token_expires_at is not None:
        # SQLite can strip tzinfo on round-trip; normalize both sides to UTC-naive for comparison.
        exp = row.token_expires_at
        now = utcnow()
        if exp.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        elif exp.tzinfo is not None and now.tzinfo is None:
            exp = exp.replace(tzinfo=None)
        if exp < now:
            raise HTTPException(status_code=404, detail="Signature request not found")
    return row


@public_router.get("/api/signatures/token/{token}", response_model=None)
def public_get_signature_by_token(
    token: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = _load_token_row(db, token)
    return _public_view(row)


@public_router.post("/api/signatures/token/{token}", response_model=None)
def public_sign_by_token(
    token: str,
    payload: PublicSignIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = _load_token_row(db, token)
    now = utcnow()
    row.status = STATUS_SIGNED
    row.signature_data = payload.signature_data
    row.signed_by = payload.signed_by
    if payload.signed_by_email:
        row.signed_by_email = payload.signed_by_email
    row.signed_at = now
    row.signed_ip = _client_ip(request)
    # Single-use: clear the token so the same URL can't be replayed
    row.token = None
    db.commit()
    db.refresh(row)

    # Audit under the row's own tenant (no authed user on public endpoints)
    _audit(
        db,
        tenant_id=row.company_id,
        user={"sub": "public-signer", "email": row.signed_by_email},
        action="signature_signed",
        entity_id=str(row.id),
        details={
            "document_type": row.document_type,
            "document_id": row.document_id,
            "flow": "remote",
        },
        request=request,
    )
    return _public_view(row)
