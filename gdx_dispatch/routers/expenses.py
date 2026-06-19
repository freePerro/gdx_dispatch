from __future__ import annotations

import datetime as _dt
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Expense, ExpenseLine
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["expenses"], dependencies=[Depends(require_module("jobs"))])


def _actor_id(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")

_EXPENSE_CATEGORIES = [
    "Fuel",
    "Parts/Supplies",
    "Tools/Equipment",
    "Advertising",
    "Insurance",
    "Vehicle Maintenance",
    "Subcontractor",
    "Other",
]


class ExpenseCreate(BaseModel):
    vendor: str = Field(min_length=1, max_length=200)
    amount: float = Field(ge=0, le=10_000_000)
    date: date
    category: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    job_id: UUID | None = None


class ExpensePatch(BaseModel):
    vendor: str | None = Field(default=None, max_length=200)
    amount: float | None = Field(default=None, ge=0, le=10_000_000)
    date: _dt.date | None = None
    category: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    job_id: UUID | None = None


class ExpenseLineCreate(BaseModel):
    account: str = Field(min_length=1, max_length=100)
    amount: float = Field(ge=0, le=10_000_000)
    description: str | None = Field(default=None, max_length=2000)


def _to_float(value: Decimal | float | int | None) -> float:
    if value is None:
        return 0.0
    return float(value)


def _expense_to_dict(expense: Expense, include_lines: bool = False) -> dict:
    payload = {
        "id": str(expense.id),
        "vendor": expense.vendor,
        "amount": _to_float(expense.amount),
        "date": expense.date.isoformat() if expense.date else None,
        "category": expense.category,
        "description": expense.description,
        "job_id": str(expense.job_id) if expense.job_id else None,
        "created_at": expense.created_at.isoformat() if expense.created_at else None,
        "updated_at": expense.updated_at.isoformat() if expense.updated_at else None,
        "deleted_at": expense.deleted_at.isoformat() if expense.deleted_at else None,
    }
    if include_lines:
        payload["lines"] = [_line_to_dict(line) for line in expense.lines]
    return payload


def _line_to_dict(line: ExpenseLine) -> dict:
    return {
        "id": str(line.id),
        "expense_id": str(line.expense_id),
        "account": line.account,
        "amount": _to_float(line.amount),
        "description": line.description,
        "created_at": line.created_at.isoformat() if line.created_at else None,
    }


@router.get("/expenses", response_model=None)
def list_expenses(
    start_date: _dt.date | None = None,
    end_date: _dt.date | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    q = select(Expense).where(Expense.deleted_at.is_(None))
    if start_date is not None:
        q = q.where(Expense.date >= start_date)
    if end_date is not None:
        q = q.where(Expense.date <= end_date)
    rows = list(db.execute(q.order_by(Expense.date.desc(), Expense.created_at.desc())).scalars().all())
    return [_expense_to_dict(row) for row in rows]


@router.post("/expenses", response_model=None, status_code=201)
def create_expense(
    payload: ExpenseCreate,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _tid = str((getattr(request.state, "tenant", {}) or {}).get("id") or "tenant-test")
    expense = Expense(**payload.model_dump(), company_id=_tid)
    db.add(expense)
    db.commit()
    db.refresh(expense)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="expense_created",
        entity_type="expense",
        entity_id=str(expense.id),
        details={"vendor": expense.vendor, "amount": _to_float(expense.amount)},
    )
    db.commit()
    return _expense_to_dict(expense)


@router.get("/expenses/{expense_id}", response_model=None)
def get_expense(
    expense_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = db.execute(
        select(Expense)
        .options(selectinload(Expense.lines))
        .where(Expense.id == expense_id, Expense.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Expense not found")
    return _expense_to_dict(row, include_lines=True)


@router.patch("/expenses/{expense_id}", response_model=None)
def update_expense(
    expense_id: UUID,
    payload: ExpensePatch,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = db.execute(
        select(Expense).where(Expense.id == expense_id, Expense.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Expense not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    row.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="expense_updated",
        entity_type="expense",
        entity_id=str(row.id),
        details=payload.model_dump(exclude_unset=True),
    )
    db.commit()
    return _expense_to_dict(row)


@router.delete("/expenses/{expense_id}", response_model=None)
def delete_expense(
    expense_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = db.execute(
        select(Expense).where(Expense.id == expense_id, Expense.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Expense not found")
    row.deleted_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    db.commit()
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="expense_deleted",
        entity_type="expense",
        entity_id=str(row.id),
        details={"soft_delete": True},
    )
    db.commit()
    return {"deleted": True}


@router.post("/expenses/{expense_id}/lines", response_model=None, status_code=201)
def create_expense_line(
    expense_id: UUID,
    payload: ExpenseLineCreate,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    expense = db.execute(
        select(Expense).where(Expense.id == expense_id, Expense.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")
    # Tenant isolation on ExpenseLine is enforced transitively via
    # expense_id → Expense.company_id. The ExpenseLine ORM has no
    # company_id column; passing one would TypeError at construction.
    # Tracked down 2026-04-17 as part of the SS-4c company_id backfill
    # over-reach (same sprint as the labor.py request bug).
    line = ExpenseLine(expense_id=expense_id, **payload.model_dump())
    db.add(line)
    db.commit()
    db.refresh(line)
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="create_expense_line",
                entity_type="expense_line",
                entity_id=str(expense_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_expense_line_audit_failed')
    return _line_to_dict(line)


@router.get("/expense-categories", response_model=None)
def list_expense_categories(_: dict = Depends(get_current_user)) -> list[str]:
    return _EXPENSE_CATEGORIES
