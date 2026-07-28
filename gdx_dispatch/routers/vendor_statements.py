"""Vendor statement upload + read API.

Slice 1 endpoints:
    POST /api/vendor-statements/upload   (multipart PDF + vendor=midwest)
    GET  /api/vendor-statements
    GET  /api/vendor-statements/{id}
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.modules.vendor_statements.account import build_vendor_accounts
from gdx_dispatch.modules.vendor_statements.classifier import VALID_CLASSIFICATIONS
from gdx_dispatch.modules.vendor_statements.models import VendorStatement, VendorStatementLine
from gdx_dispatch.modules.vendor_statements.parsers.midwest import MidwestParseError
from gdx_dispatch.modules.vendor_statements.service import (
    DuplicateDocumentError,
    upload_midwest_statement,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vendor-statements", tags=["vendor-statements"])


_SUPPORTED_VENDORS = {"midwest"}


class VendorStatementLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_no: int
    vendor_invoice_no: str
    vendor_job_no: Optional[str]
    line_date: Optional[date]
    amount: Decimal
    balance: Decimal
    description: Optional[str]
    po_ref: Optional[str]
    aging_bucket: Optional[str]
    classification: Optional[str]
    matched_job_id: Optional[UUID]
    notes: Optional[str] = None


class VendorStatementSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    vendor_name: str
    vendor_code: Optional[str]
    statement_date: Optional[date]
    document_id: Optional[UUID]
    parser_name: str
    parser_version: int
    raw_total: Decimal
    line_count: int
    status: str
    source: str = "upload"
    uploaded_by: Optional[str]
    created_at: datetime


class VendorStatementDetailOut(VendorStatementSummaryOut):
    lines: list[VendorStatementLineOut] = []


class LinePatch(BaseModel):
    classification: Optional[str] = None
    notes: Optional[str] = None


class DuplicateOut(BaseModel):
    detail: str
    existing_document_id: str
    original_name: Optional[str] = None


# ── vendor account (what is actually owed) ─────────────────────────────


class AccountLineOut(BaseModel):
    invoice_no: str
    line_date: date | None
    amount: Decimal
    paid: Decimal
    balance: Decimal
    aging_bucket: str | None
    vendor_job_no: str | None
    po_ref: str | None
    description: str | None
    classification: str | None
    matched_job_id: UUID | None
    first_seen_on: date | None
    statements_seen: int


class AccountChangeOut(BaseModel):
    previous_statement_date: date | None
    new_invoice_count: int
    new_invoice_total: Decimal
    cleared_count: int
    cleared_total: Decimal
    paid_down_count: int
    paid_down_total: Decimal
    # DERIVED from the statement diff, never a recorded payment. Consumers must
    # present it as inferred: a credit memo reads the same as a payment, and two
    # payments inside one statement gap collapse into one number.
    implied_payment_total: Decimal


class VendorAccountOut(BaseModel):
    vendor_name: str
    vendor_code: str | None
    as_of: date | None
    statement_id: UUID
    statement_count: int
    open_balance: Decimal
    open_line_count: int
    original_total: Decimal
    paid_to_date: Decimal
    oldest_line_date: date | None
    days_oldest_open: int | None
    aging: dict[str, Decimal]
    lines: list[AccountLineOut]
    change: AccountChangeOut | None


@router.post(
    "/upload",
    status_code=201,
    response_model=VendorStatementDetailOut,
    dependencies=[Depends(require_permission("vendor_statements.write"))],
)
async def upload_statement(
    request: Request,
    file: UploadFile = File(...),
    vendor: str = Form(default="midwest"),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VendorStatementDetailOut:
    vendor_key = (vendor or "").strip().lower()
    if vendor_key not in _SUPPORTED_VENDORS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported vendor '{vendor}'. supported: {sorted(_SUPPORTED_VENDORS)}",
        )

    pdf_bytes = await file.read()

    try:
        result = upload_midwest_statement(
            db,
            pdf_bytes=pdf_bytes,
            original_filename=file.filename or "midwest-statement.pdf",
            content_type=file.content_type,
            uploaded_by=str(user.get("user_id") or user.get("sub") or "") or None,
        )
    except DuplicateDocumentError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "duplicate document — already uploaded",
                "existing_document_id": exc.existing_document_id,
                "original_name": exc.original_name,
            },
        )
    except MidwestParseError as exc:
        raise HTTPException(status_code=422, detail=f"could not parse statement: {exc}")

    db.commit()
    db.refresh(result.statement)

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="vendor_statement_uploaded",
        entity_type="vendor_statement",
        entity_id=str(result.statement.id),
        details={
            "vendor": vendor_key,
            "document_id": str(result.document.id),
            "line_count": result.statement.line_count,
            "raw_total": str(result.statement.raw_total),
            "statement_date": (
                result.statement.statement_date.isoformat()
                if result.statement.statement_date
                else None
            ),
        },
    )
    db.commit()

    return _detail_response(db, result.statement)


@router.get(
    "/accounts",
    response_model=list[VendorAccountOut],
    dependencies=[Depends(require_permission("vendor_statements.read"))],
)
async def list_vendor_accounts(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[VendorAccountOut]:
    """Current position per vendor: what is open, how old, and what moved since
    the previous statement.

    Reads the LATEST statement as the current position — a statement is a
    snapshot of open items, so summing statements double-counts every invoice
    that stayed open (by 15x on the first real dataset). Declared BEFORE
    ``/{statement_id}`` so the literal path isn't swallowed by the UUID route.
    """
    today = datetime.now(timezone.utc).date()
    return [
        VendorAccountOut(
            vendor_name=a.vendor_name,
            vendor_code=a.vendor_code,
            as_of=a.as_of,
            statement_id=a.statement_id,
            statement_count=a.statement_count,
            open_balance=a.open_balance,
            open_line_count=a.open_line_count,
            original_total=a.original_total,
            paid_to_date=a.paid_to_date,
            oldest_line_date=a.oldest_line_date,
            days_oldest_open=a.days_oldest_open(today),
            aging=a.aging,
            lines=[AccountLineOut(**vars(ln)) for ln in a.lines],
            change=(
                AccountChangeOut(
                    previous_statement_date=a.change.previous_statement_date,
                    new_invoice_count=a.change.new_invoice_count,
                    new_invoice_total=a.change.new_invoice_total,
                    cleared_count=a.change.cleared_count,
                    cleared_total=a.change.cleared_total,
                    paid_down_count=a.change.paid_down_count,
                    paid_down_total=a.change.paid_down_total,
                    implied_payment_total=a.change.implied_payment_total,
                )
                if a.change is not None else None
            ),
        )
        for a in build_vendor_accounts(db)
    ]


@router.get(
    "",
    response_model=list[VendorStatementSummaryOut],
    dependencies=[Depends(require_permission("vendor_statements.read"))],
)
async def list_statements(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[VendorStatementSummaryOut]:
    stmt = (
        select(VendorStatement)
        .where(VendorStatement.deleted_at.is_(None))
        .order_by(VendorStatement.created_at.desc())
    )
    rows = db.execute(stmt).scalars().all()
    return [VendorStatementSummaryOut.model_validate(r) for r in rows]


@router.get(
    "/{statement_id}",
    response_model=VendorStatementDetailOut,
    dependencies=[Depends(require_permission("vendor_statements.read"))],
)
async def get_statement(
    statement_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VendorStatementDetailOut:
    statement = db.execute(
        select(VendorStatement)
        .where(VendorStatement.id == statement_id)
        .where(VendorStatement.deleted_at.is_(None))
    ).scalar_one_or_none()
    if statement is None:
        raise HTTPException(status_code=404, detail="vendor statement not found")
    return _detail_response(db, statement)


@router.patch(
    "/{statement_id}/lines/{line_id}",
    response_model=VendorStatementLineOut,
    dependencies=[Depends(require_permission("vendor_statements.write"))],
)
async def update_line(
    statement_id: UUID,
    line_id: UUID,
    payload: LinePatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VendorStatementLineOut:
    line = db.execute(
        select(VendorStatementLine)
        .where(VendorStatementLine.id == line_id)
        .where(VendorStatementLine.statement_id == statement_id)
    ).scalar_one_or_none()
    if line is None:
        raise HTTPException(status_code=404, detail="vendor statement line not found")

    changes: dict[str, dict] = {}

    if payload.classification is not None:
        new_value = payload.classification.strip().lower()
        if new_value not in VALID_CLASSIFICATIONS:
            raise HTTPException(
                status_code=400,
                detail=f"classification must be one of {sorted(VALID_CLASSIFICATIONS)}",
            )
        if new_value != line.classification:
            changes["classification"] = {"from": line.classification, "to": new_value}
            line.classification = new_value

    if payload.notes is not None:
        cleaned = payload.notes.strip() or None
        if cleaned != line.notes:
            changes["notes"] = {"from": line.notes, "to": cleaned}
            line.notes = cleaned

    if not changes:
        return VendorStatementLineOut.model_validate(line)

    db.flush()

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="vendor_statement_line_updated",
        entity_type="vendor_statement_line",
        entity_id=str(line.id),
        details={"statement_id": str(statement_id), "changes": changes},
    )
    db.commit()
    db.refresh(line)
    return VendorStatementLineOut.model_validate(line)


def _detail_response(db: Session, statement: VendorStatement) -> VendorStatementDetailOut:
    line_rows = db.execute(
        select(VendorStatementLine)
        .where(VendorStatementLine.statement_id == statement.id)
        .order_by(VendorStatementLine.line_no.asc())
    ).scalars().all()
    return VendorStatementDetailOut(
        **VendorStatementSummaryOut.model_validate(statement).model_dump(),
        lines=[VendorStatementLineOut.model_validate(r) for r in line_rows],
    )
