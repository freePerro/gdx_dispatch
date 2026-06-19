"""
Exports router — bulk CSV export for every major tenant entity.

Read-only, admin-gated. Returns CSV with proper Content-Disposition so browsers
download as a file. Dispatcher fallback: `/api/exports/all` JSON endpoint for
offline processing.

Pattern: raw SQL via `text()` with OperationalError/ProgrammingError catch
(some tables may be absent in test/minimal fixtures). All queries tenant-scoped
via bind params. No Postgres-specific functions — portable to SQLite.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

# Gate: "customers" is a starter-default module present for every tenant.
# Role gate (admin/owner) enforced per-handler via Depends(require_role(...)).
router = APIRouter(
    tags=["exports"],
    dependencies=[Depends(require_module("customers")), Depends(require_role("admin", "owner", "superadmin"))],
)

ALLOWED_ENTITIES = {
    "customers",
    "jobs",
    "invoices",
    "estimates",
    "payments",
    "technicians",
    "leads",
}
MAX_ENTITIES = 10


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


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid date: {value!r} (expect YYYY-MM-DD)",
        ) from None


def _filename(entity: str, tenant_id: str) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    safe_tid = "".join(c for c in tenant_id if c.isalnum() or c in ("-", "_"))[:64] or "tenant"
    return f"{entity}_{safe_tid}_{today}.csv"


def _csv_response(entity: str, tenant_id: str, header: list[str], rows: list[list[Any]]) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{_filename(entity, tenant_id)}"'
        },
    )


def _safe_query(
    db: Session, sql: str, params: dict[str, Any], entity: str
) -> list[tuple]:
    """Run SQL, return rows. On missing table / schema issues, return []."""
    try:
        return list(db.execute(text(sql), params).all())
    except (OperationalError, ProgrammingError):  # returns empty list on missing table or schema issues per contract
        log.exception("exports_%s_table_missing", entity)
        return []
    except SQLAlchemyError:  # returns empty list on missing table or schema issues per contract
        log.exception("exports_%s_query_failed", entity)
        return []


def _audit_export(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    entity: str,
    row_count: int,
    request: Request,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action="export_downloaded",
            entity_type="export",
            entity_id=entity,
            details={"entity": entity, "row_count": row_count},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("exports_audit_failed entity=%s", entity)
        try:
            db.rollback()
        except Exception:
            log.exception("exports_audit_rollback_failed")


def _require_admin(user: Any) -> None:
    role = str((user or {}).get("role") or "").lower()
    if role not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Insufficient role")


# ---------------------------------------------------------------------------
# Fetch functions — each returns (header, rows) tuple. Graceful degrade.
# ---------------------------------------------------------------------------
def _fetch_customers(
    db: Session, *, tenant_id: str
) -> tuple[list[str], list[list[Any]]]:
    header = [
        "id", "name", "email", "phone", "address", "city", "state", "zip",
        "created_at", "deleted_at",
    ]
    sql = """
        SELECT id, name, email, phone, address, city, state, zip,
               created_at, deleted_at
          FROM customers
         WHERE company_id = :tenant_id
         ORDER BY created_at DESC
    """
    rows = _safe_query(db, sql, {"tenant_id": tenant_id}, "customers")
    return header, [list(r) for r in rows]


def _fetch_jobs(
    db: Session,
    *,
    tenant_id: str,
    status: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> tuple[list[str], list[list[Any]]]:
    header = [
        "id", "customer_id", "customer_name", "title", "status",
        "lifecycle_stage", "total", "scheduled_at", "completed_at", "created_at",
    ]
    sql = """
        SELECT j.id, j.customer_id,
               COALESCE(c.name, '') AS customer_name,
               j.title, j.status, j.lifecycle_stage, j.total,
               j.scheduled_at, j.completed_at, j.created_at
          FROM jobs j
          LEFT JOIN customers c
                 ON c.id = j.customer_id
                AND c.company_id = :tenant_id
         WHERE j.company_id = :tenant_id
    """
    params: dict[str, Any] = {"tenant_id": tenant_id}
    if status:
        sql += " AND j.status = :status"
        params["status"] = status
    if start is not None:
        sql += " AND DATE(j.created_at) >= :start"
        params["start"] = start.isoformat()
    if end is not None:
        sql += " AND DATE(j.created_at) <= :end"
        params["end"] = end.isoformat()
    sql += " ORDER BY j.created_at DESC"
    rows = _safe_query(db, sql, params, "jobs")
    return header, [list(r) for r in rows]


def _fetch_invoices(
    db: Session,
    *,
    tenant_id: str,
    status: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> tuple[list[str], list[list[Any]]]:
    header = [
        "id", "invoice_number", "customer_id", "customer_name", "total",
        "status", "due_date", "created_at",
    ]
    sql = """
        SELECT i.id, i.invoice_number, i.customer_id,
               COALESCE(c.name, '') AS customer_name,
               i.total, i.status, i.due_date, i.created_at
          FROM invoices i
          LEFT JOIN customers c
                 ON c.id = i.customer_id
                AND c.company_id = :tenant_id
         WHERE i.company_id = :tenant_id
    """
    params: dict[str, Any] = {"tenant_id": tenant_id}
    if status:
        sql += " AND i.status = :status"
        params["status"] = status
    if start is not None:
        sql += " AND DATE(i.created_at) >= :start"
        params["start"] = start.isoformat()
    if end is not None:
        sql += " AND DATE(i.created_at) <= :end"
        params["end"] = end.isoformat()
    sql += " ORDER BY i.created_at DESC"
    rows = _safe_query(db, sql, params, "invoices")
    return header, [list(r) for r in rows]


def _fetch_estimates(
    db: Session, *, tenant_id: str, status: str | None = None
) -> tuple[list[str], list[list[Any]]]:
    header = [
        "id", "estimate_number", "customer_name", "total", "status",
        "sent_at", "accepted_at", "created_at",
    ]
    sql = """
        SELECT e.id, e.estimate_number,
               COALESCE(c.name, '') AS customer_name,
               e.total, e.status, e.sent_at, e.accepted_at, e.created_at
          FROM estimates e
          LEFT JOIN customers c
                 ON c.id = e.customer_id
                AND c.company_id = :tenant_id
         WHERE e.company_id = :tenant_id
    """
    params: dict[str, Any] = {"tenant_id": tenant_id}
    if status:
        sql += " AND e.status = :status"
        params["status"] = status
    sql += " ORDER BY e.created_at DESC"
    rows = _safe_query(db, sql, params, "estimates")
    return header, [list(r) for r in rows]


def _fetch_payments(
    db: Session,
    *,
    tenant_id: str,
    start: date | None = None,
    end: date | None = None,
) -> tuple[list[str], list[list[Any]]]:
    header = ["id", "invoice_id", "amount", "method", "status", "created_at"]
    sql = """
        SELECT id, invoice_id, amount, method, status, created_at
          FROM payments
         WHERE company_id = :tenant_id
    """
    params: dict[str, Any] = {"tenant_id": tenant_id}
    if start is not None:
        sql += " AND DATE(created_at) >= :start"
        params["start"] = start.isoformat()
    if end is not None:
        sql += " AND DATE(created_at) <= :end"
        params["end"] = end.isoformat()
    sql += " ORDER BY created_at DESC"
    rows = _safe_query(db, sql, params, "payments")
    return header, [list(r) for r in rows]


def _fetch_technicians(
    db: Session, *, tenant_id: str
) -> tuple[list[str], list[list[Any]]]:
    header = ["id", "name", "email", "phone", "active", "created_at"]
    sql = """
        SELECT id, name, email, phone, active, created_at
          FROM technicians
         WHERE company_id = :tenant_id
         ORDER BY created_at DESC
    """
    rows = _safe_query(db, sql, {"tenant_id": tenant_id}, "technicians")
    return header, [list(r) for r in rows]


def _fetch_leads(
    db: Session, *, tenant_id: str, stage: str | None = None
) -> tuple[list[str], list[list[Any]]]:
    header = [
        "id", "name", "email", "phone", "stage", "estimated_value",
        "assigned_to", "created_at",
    ]
    sql = """
        SELECT id, name, email, phone, stage, estimated_value,
               assigned_to, created_at
          FROM leads
         WHERE company_id = :tenant_id
    """
    params: dict[str, Any] = {"tenant_id": tenant_id}
    if stage:
        sql += " AND stage = :stage"
        params["stage"] = stage
    sql += " ORDER BY created_at DESC"
    rows = _safe_query(db, sql, params, "leads")
    return header, [list(r) for r in rows]


def _rows_to_dicts(header: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        rec: dict[str, Any] = {}
        for i, col in enumerate(header):
            val = r[i] if i < len(r) else None
            if isinstance(val, (datetime, date)):
                val = val.isoformat()
            rec[col] = val
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/api/exports/customers", response_model=None)
def export_customers(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    _require_admin(user)
    tenant_id = _tenant_id(request)
    header, rows = _fetch_customers(db, tenant_id=tenant_id)
    _audit_export(
        db, tenant_id=tenant_id, user=user,
        entity="customers", row_count=len(rows), request=request,
    )
    return _csv_response("customers", tenant_id, header, rows)


@router.get("/api/exports/jobs", response_model=None)
def export_jobs(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None, max_length=64),
    start: str | None = Query(default=None, max_length=20),
    end: str | None = Query(default=None, max_length=20),
) -> Response:
    _require_admin(user)
    tenant_id = _tenant_id(request)
    s = _parse_iso_date(start)
    e = _parse_iso_date(end)
    header, rows = _fetch_jobs(
        db, tenant_id=tenant_id, status=status, start=s, end=e
    )
    _audit_export(
        db, tenant_id=tenant_id, user=user,
        entity="jobs", row_count=len(rows), request=request,
    )
    return _csv_response("jobs", tenant_id, header, rows)


@router.get("/api/exports/invoices", response_model=None)
def export_invoices(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None, max_length=64),
    start: str | None = Query(default=None, max_length=20),
    end: str | None = Query(default=None, max_length=20),
) -> Response:
    _require_admin(user)
    tenant_id = _tenant_id(request)
    s = _parse_iso_date(start)
    e = _parse_iso_date(end)
    header, rows = _fetch_invoices(
        db, tenant_id=tenant_id, status=status, start=s, end=e
    )
    _audit_export(
        db, tenant_id=tenant_id, user=user,
        entity="invoices", row_count=len(rows), request=request,
    )
    return _csv_response("invoices", tenant_id, header, rows)


@router.get("/api/exports/estimates", response_model=None)
def export_estimates(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None, max_length=64),
) -> Response:
    _require_admin(user)
    tenant_id = _tenant_id(request)
    header, rows = _fetch_estimates(db, tenant_id=tenant_id, status=status)
    _audit_export(
        db, tenant_id=tenant_id, user=user,
        entity="estimates", row_count=len(rows), request=request,
    )
    return _csv_response("estimates", tenant_id, header, rows)


@router.get("/api/exports/payments", response_model=None)
def export_payments(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start: str | None = Query(default=None, max_length=20),
    end: str | None = Query(default=None, max_length=20),
) -> Response:
    _require_admin(user)
    tenant_id = _tenant_id(request)
    s = _parse_iso_date(start)
    e = _parse_iso_date(end)
    header, rows = _fetch_payments(db, tenant_id=tenant_id, start=s, end=e)
    _audit_export(
        db, tenant_id=tenant_id, user=user,
        entity="payments", row_count=len(rows), request=request,
    )
    return _csv_response("payments", tenant_id, header, rows)


@router.get("/api/exports/technicians", response_model=None)
def export_technicians(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    _require_admin(user)
    tenant_id = _tenant_id(request)
    header, rows = _fetch_technicians(db, tenant_id=tenant_id)
    _audit_export(
        db, tenant_id=tenant_id, user=user,
        entity="technicians", row_count=len(rows), request=request,
    )
    return _csv_response("technicians", tenant_id, header, rows)


@router.get("/api/exports/leads", response_model=None)
def export_leads(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    stage: str | None = Query(default=None, max_length=64),
) -> Response:
    _require_admin(user)
    tenant_id = _tenant_id(request)
    header, rows = _fetch_leads(db, tenant_id=tenant_id, stage=stage)
    _audit_export(
        db, tenant_id=tenant_id, user=user,
        entity="leads", row_count=len(rows), request=request,
    )
    return _csv_response("leads", tenant_id, header, rows)


@router.get("/api/exports/all", response_model=None)
def export_all(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    entities: str = Query(
        ...,
        max_length=256,
        description="Comma-separated list of entities to export",
    ),
) -> JSONResponse:
    _require_admin(user)
    tenant_id = _tenant_id(request)

    requested = [e.strip() for e in (entities or "").split(",") if e.strip()]
    if not requested:
        raise HTTPException(status_code=422, detail="entities is required")
    if len(requested) > MAX_ENTITIES:
        raise HTTPException(
            status_code=422,
            detail=f"too many entities (max {MAX_ENTITIES})",
        )
    for ent in requested:
        if ent not in ALLOWED_ENTITIES:
            raise HTTPException(
                status_code=422, detail=f"unknown entity: {ent!r}"
            )

    # Deduplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for ent in requested:
        if ent not in seen:
            seen.add(ent)
            ordered.append(ent)

    result: dict[str, list[dict[str, Any]]] = {}
    total_rows = 0
    for ent in ordered:
        if ent == "customers":
            h, r = _fetch_customers(db, tenant_id=tenant_id)
        elif ent == "jobs":
            h, r = _fetch_jobs(db, tenant_id=tenant_id)
        elif ent == "invoices":
            h, r = _fetch_invoices(db, tenant_id=tenant_id)
        elif ent == "estimates":
            h, r = _fetch_estimates(db, tenant_id=tenant_id)
        elif ent == "payments":
            h, r = _fetch_payments(db, tenant_id=tenant_id)
        elif ent == "technicians":
            h, r = _fetch_technicians(db, tenant_id=tenant_id)
        elif ent == "leads":
            h, r = _fetch_leads(db, tenant_id=tenant_id)
        else:
            continue
        records = _rows_to_dicts(h, r)
        result[ent] = records
        total_rows += len(records)

    _audit_export(
        db, tenant_id=tenant_id, user=user,
        entity="all:" + ",".join(ordered),
        row_count=total_rows, request=request,
    )
    return JSONResponse(content=result)
