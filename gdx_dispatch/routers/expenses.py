from __future__ import annotations

import datetime as _dt
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Expense, ExpenseLine, JobReceipt
from gdx_dispatch.modules.ledger.engine import PeriodLockedError
from gdx_dispatch.modules.ledger.models import ExpenseReceipt
from gdx_dispatch.modules.ledger.rules import (
    ExpenseCompositionError,
    post_expense_recorded,
    repost_expense,
)
from gdx_dispatch.core.expense_categories import (
    EXPENSE_CATEGORIES,
    canonicalize_expense_category,
)
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["expenses"], dependencies=[Depends(require_module("jobs"))])


def _actor_id(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")

# Canonical vocabulary lives in core/expense_categories.py (one source of
# truth for the API, the posting rules, and the frontend endpoint). Alias
# kept for existing importers.
_EXPENSE_CATEGORIES = EXPENSE_CATEGORIES


class ExpenseCreate(BaseModel):
    vendor: str = Field(min_length=1, max_length=200)
    # GL S8 (spec §5.5): gt=0 — a $0 expense is unrecordable noise.
    amount: float = Field(gt=0, le=10_000_000)
    date: date
    category: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    job_id: UUID | None = None


class ExpensePatch(BaseModel):
    vendor: str | None = Field(default=None, max_length=200)
    amount: float | None = Field(default=None, gt=0, le=10_000_000)
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


def _post_or_409(fn) -> None:
    """Ledger refusals surface as 409s with the reason, never bare 500s."""
    try:
        fn()
    except ExpenseCompositionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PeriodLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"expense date falls in a locked accounting period — {exc}",
        ) from exc


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
    payloads = [_expense_to_dict(row) for row in rows]
    _annotate_gl_accounts(db, rows, payloads)
    _annotate_receipt_counts(db, rows, payloads)
    return payloads


def _annotate_receipt_counts(db: Session, rows: list[Expense], payloads: list[dict]) -> None:
    """One grouped query — the list's Receipts column and the
    only-with-receipt filter read this."""
    if not rows:
        return
    from sqlalchemy import func

    counts = dict(
        db.execute(
            select(ExpenseReceipt.expense_id, func.count())
            .where(
                ExpenseReceipt.expense_id.in_([r.id for r in rows]),
                ExpenseReceipt.deleted_at.is_(None),
            )
            .group_by(ExpenseReceipt.expense_id)
        ).all()
    )
    for row, payload in zip(rows, payloads):
        payload["receipt_count"] = int(counts.get(row.id, 0))


def _annotate_gl_accounts(db: Session, rows: list[Expense], payloads: list[dict]) -> None:
    """GL S11 (spec §9): expense detail shows the CoA account it posts to —
    resolved through the same category map P5 uses, one settings read for
    the whole page. Annotation only; failures never break the list."""
    if not rows:
        return
    try:
        from gdx_dispatch.modules.ledger import service as ledger_service
        from gdx_dispatch.modules.ledger.models import GlAccount, ROLE_EXPENSE_FALLBACK
        from gdx_dispatch.modules.ledger.rules import _expense_account_id

        company_id = rows[0].company_id
        settings = ledger_service.get_gl_settings(db, company_id)
        if settings is None:
            return
        fallback = db.execute(
            select(GlAccount).where(
                GlAccount.company_id == company_id,
                GlAccount.role == ROLE_EXPENSE_FALLBACK,
                GlAccount.active.is_(True),
            )
        ).scalar_one_or_none()
        cache: dict = {}
        for row, payload in zip(rows, payloads):
            key = row.category or ""
            if key not in cache:
                account_id = _expense_account_id(db, settings, company_id, row.category)
                account = db.get(GlAccount, account_id) if account_id else fallback
                cache[key] = account
            account = cache[key]
            if account is not None:
                payload["gl_account"] = {"code": account.code, "name": account.name}
    except Exception:
        log.exception("expense_gl_account_annotation_failed")


@router.post("/expenses", response_model=None, status_code=201)
def create_expense(
    payload: ExpenseCreate,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _tid = str((getattr(request.state, "tenant", {}) or {}).get("id") or "tenant-test")
    canonical = canonicalize_expense_category(payload.category)
    if canonical is None:
        # GL S8 (P5): categories drive the posting account map — free-text
        # would route everything to 6900 forever. Legacy vocabulary
        # (materials/supplies/…) is accepted and canonicalized.
        raise HTTPException(
            status_code=422,
            detail=f"category must be one of {_EXPENSE_CATEGORIES}",
        )
    data = payload.model_dump()
    data["category"] = canonical
    expense = Expense(**data, company_id=_tid)
    db.add(expense)
    db.flush()
    _post_or_409(lambda: post_expense_recorded(db, expense, actor=_actor_id(_)))
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
    updates = payload.model_dump(exclude_unset=True)
    if "category" in updates:
        canonical = canonicalize_expense_category(updates["category"])
        if canonical is None:
            raise HTTPException(
                status_code=422,
                detail=f"category must be one of {_EXPENSE_CATEGORIES}",
            )
        updates["category"] = canonical
    for field, value in updates.items():
        setattr(row, field, value)
    row.updated_at = datetime.now(UTC)
    _post_or_409(lambda: repost_expense(db, row, actor=_actor_id(_)))  # GL S8: P6
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
    _post_or_409(lambda: repost_expense(db, row, actor=_actor_id(_)))  # GL S8: reverses the live entry
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
    db.flush()
    db.refresh(expense)
    # GL S8 (P6): lines are built incrementally, so per-add we only reject
    # what can never reconcile (overshoot); an under-complete set defers the
    # repost and the header-level entry stays live. Equality reposts.
    lines_sum = sum(_to_float(l.amount) for l in expense.lines)
    header = _to_float(expense.amount)
    if lines_sum > header + 0.005:
        raise HTTPException(
            status_code=409,
            detail=f"expense lines sum to {lines_sum:.2f}, over the header amount {header:.2f}",
        )
    if abs(lines_sum - header) <= 0.005:
        _post_or_409(lambda: repost_expense(db, expense, actor=_actor_id(_)))
    db.commit()
    db.refresh(line)
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


# ---------------------------------------------------------------------------
# Expense receipts (GL S8, spec §3.7) — source documents. Rev. Proc. 97-22:
# sha256 = integrity; original-resolution download = reproduction; soft-
# delete-only + files never unlinked = ≥7-year retention (Pub 583).
# ---------------------------------------------------------------------------

_RECEIPT_MAX_BYTES = 25 * 1024 * 1024
_RECEIPT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"}


def _receipt_dir(company_id: str, expense_id: UUID):
    # Returns a pathlib.Path. No return annotation: os/Path are imported
    # function-locally (deliberate — keeps `os` out of module scope for the
    # realpath path-injection barrier), so a "os.PathLike" forward-ref
    # annotation is an undefined name (F821, the CI hard gate).
    import os as _os
    from pathlib import Path as _Path

    base = _os.path.realpath(_os.getenv("UPLOAD_DIR", "/app/uploads"))
    candidate = _os.path.realpath(
        _os.path.join(base, company_id, "expense_receipt", str(expense_id))
    )
    if not candidate.startswith(base + _os.sep):  # CodeQL path-injection barrier
        raise HTTPException(status_code=400, detail="Invalid upload path")
    _Path(candidate).mkdir(parents=True, exist_ok=True)
    return _Path(candidate)


def _receipt_to_dict(r: ExpenseReceipt) -> dict:
    return {
        "id": str(r.id),
        "expense_id": str(r.expense_id),
        "filename": r.filename,
        "content_type": r.content_type,
        "size_bytes": r.size_bytes,
        "sha256": r.sha256,
        "uploaded_by": r.uploaded_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
    }


def _get_live_expense(db: Session, expense_id: UUID) -> Expense:
    expense = db.execute(
        select(Expense).where(Expense.id == expense_id, Expense.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")
    return expense


@router.post("/expenses/{expense_id}/receipts", response_model=None, status_code=201)
def upload_expense_receipt(
    expense_id: UUID,
    file: UploadFile = File(...),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    import hashlib

    expense = _get_live_expense(db, expense_id)
    if (file.content_type or "") not in _RECEIPT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"receipt must be one of {sorted(_RECEIPT_TYPES)}",
        )
    blob = file.file.read(_RECEIPT_MAX_BYTES + 1)
    if len(blob) > _RECEIPT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="receipt exceeds 25MB")
    if not blob:
        raise HTTPException(status_code=422, detail="empty file")

    digest = hashlib.sha256(blob).hexdigest()
    existing = db.execute(
        select(ExpenseReceipt).where(
            ExpenseReceipt.expense_id == expense.id,
            ExpenseReceipt.sha256 == digest,
            ExpenseReceipt.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing:
        return _receipt_to_dict(existing)  # same content — idempotent

    suffix = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/heic": ".heic", "application/pdf": ".pdf",
    }[file.content_type]
    directory = _receipt_dir(expense.company_id, expense.id)
    path = directory / f"{digest}{suffix}"
    path.write_bytes(blob)

    receipt = ExpenseReceipt(
        expense_id=expense.id,
        filename=(file.filename or f"receipt{suffix}")[:200],
        content_type=file.content_type,
        size_bytes=len(blob),
        sha256=digest,
        storage_path=str(path),
        uploaded_by=_actor_id(_),
        company_id=expense.company_id,
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="expense_receipt_uploaded", entity_type="expense",
        entity_id=str(expense.id), details={"sha256": digest, "size": len(blob)},
    )
    db.commit()
    return _receipt_to_dict(receipt)


@router.get("/expenses/{expense_id}/receipts", response_model=None)
def list_expense_receipts(
    expense_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    _get_live_expense(db, expense_id)
    rows = db.execute(
        select(ExpenseReceipt).where(
            ExpenseReceipt.expense_id == expense_id,
            ExpenseReceipt.deleted_at.is_(None),
        ).order_by(ExpenseReceipt.created_at)
    ).scalars().all()
    return [_receipt_to_dict(r) for r in rows]


@router.get("/expenses/{expense_id}/receipts/{receipt_id}/download", response_model=None)
def download_expense_receipt(
    expense_id: UUID,
    receipt_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    import os as _os

    receipt = db.get(ExpenseReceipt, receipt_id)
    if receipt is None or receipt.expense_id != expense_id or receipt.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if not _os.path.exists(receipt.storage_path):
        # UPLOAD_DIR drift between environments — a clear 404 beats a
        # send-time 500 (audit round 4).
        raise HTTPException(status_code=404, detail="Receipt file missing from storage")
    return FileResponse(
        receipt.storage_path,
        media_type=receipt.content_type,
        filename=receipt.filename,
    )


@router.delete("/expenses/{expense_id}/receipts/{receipt_id}", response_model=None)
def soft_delete_expense_receipt(
    expense_id: UUID,
    receipt_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Soft delete ONLY — the row keeps its hash and the file is never
    unlinked (≥7-year retention, Pub 583). There is no hard-delete path."""
    receipt = db.get(ExpenseReceipt, receipt_id)
    if receipt is None or receipt.expense_id != expense_id or receipt.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    receipt.deleted_at = datetime.now(UTC)
    db.commit()
    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="expense_receipt_soft_deleted", entity_type="expense",
        entity_id=str(expense_id), details={"receipt_id": str(receipt_id)},
    )
    db.commit()
    return {"deleted": True, "retained": True}


# ---------------------------------------------------------------------------
# Promote-from-field (GL S8, spec §3.7): JobReceipt → prefilled Expense
# ---------------------------------------------------------------------------

class PromoteReceiptIn(BaseModel):
    job_receipt_id: UUID
    category: str = Field(default="Parts/Supplies")


@router.post("/expenses/promote-from-receipt", response_model=None, status_code=201)
def promote_job_receipt(
    payload: PromoteReceiptIn,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """A road receipt captured in the field becomes a real Expense —
    prefilled from the JobReceipt, linked via promoted_expense_id so the
    photo evidence stays attached and re-promotion is idempotent."""
    receipt = db.get(JobReceipt, payload.job_receipt_id)
    if receipt is None or receipt.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Job receipt not found")
    if receipt.promoted_expense_id:
        existing = db.get(Expense, receipt.promoted_expense_id)
        if existing and existing.deleted_at is None:
            return _expense_to_dict(existing)  # already promoted — idempotent
    if not receipt.amount or float(receipt.amount) <= 0:
        raise HTTPException(
            status_code=422,
            detail="job receipt has no amount — add one before promoting",
        )
    promote_category = canonicalize_expense_category(payload.category)
    if promote_category is None:
        raise HTTPException(status_code=422, detail=f"category must be one of {_EXPENSE_CATEGORIES}")

    _tid = str((getattr(request.state, "tenant", {}) or {}).get("id") or "tenant-test")
    expense = Expense(
        vendor=(receipt.vendor or "Field purchase")[:200],
        amount=float(receipt.amount),
        date=(receipt.purchased_at.date() if receipt.purchased_at else date.today()),
        category=promote_category,
        description=(receipt.notes or "")[:2000] or None,
        job_id=receipt.job_id,
        company_id=_tid,
    )
    db.add(expense)
    db.flush()
    receipt.promoted_expense_id = expense.id
    _post_or_409(lambda: post_expense_recorded(db, expense, actor=_actor_id(_)))
    db.commit()
    db.refresh(expense)
    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="job_receipt_promoted", entity_type="expense",
        entity_id=str(expense.id), details={"job_receipt_id": str(receipt.id)},
    )
    db.commit()
    return _expense_to_dict(expense)
