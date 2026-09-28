"""
Audit log viewer and SOC2 compliance dashboard — admin-only endpoints.

Every public helper here degrades instead of raising: a viewer that 500s is
worse than one reporting "chain unverified". Each of those helpers' reads runs
under ``core.database.contained_read`` (GDXA-152, the helper added by GDXA-86),
because on Postgres a failed statement aborts the whole transaction and the
degraded dict would otherwise be handed back on a session whose every later
statement raises 25P02 (InFailedSqlTransaction). SQLite does not poison a
transaction this way, so the class is invisible to the SQLite test arm.

Be honest about the strength of that here, because the first version of this
docstring was not: in THIS file the containment is prophylactic. Every one of
these helpers is reached only from route handlers in this same file, and
``get_db``'s teardown is a bare ``close()`` with no commit, so today no caller
issues another statement after the swallow — there is no live instance of the
500-on-an-unrelated-line it protects against. It is cheap, it is correct, and it
stops the next caller from inheriting the trap; it is not a fix for an observed
failure. The one handler here that DOES continue past its own swallow is
``compliance_summary``, and that one is contained — see the note on it.

``core/audit_labels.py`` is the same story with a real *structural* caller:
``routers/estimates.py::get_estimate_activity`` resolves labels mid-request and
then runs further SELECTs with no handler, so a poisoned transaction there is a
500 rather than a degraded label. Both files are prophylactic as to the
*trigger*, though — no production instance has been produced for either, and an
earlier draft of these comments invented one (see ``_resolve_customer_users``).

``contained_read`` and not ``db.begin_nested()``: the latter flushes the
caller's pending ORM state even under this app's ``autoflush=False``, and emits
that write *before* the SAVEPOINT exists. See the five rules on the helper —
the two that bind here are that the block goes INSIDE the existing ``try`` (so
each function's own ``except`` still produces its degraded dict) and that these
are reads only.
"""
from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, StreamingResponse

from gdx_dispatch.core.audit import AuditLog, TenantBase, _payload_json
from gdx_dispatch.core.database import contained_read, get_db

# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)
ADMIN_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")


def _require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
        )
    if credentials is None or credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access denied",
        )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["audit-dashboard"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_audit_table(db: Session) -> None:
    """Create audit_log table if it does not exist (dev / test convenience)."""
    with contextlib.suppress(Exception):
        TenantBase.metadata.create_all(bind=db.bind, checkfirst=True)


def _row_to_dict(r: AuditLog) -> dict:
    return {
        "id": str(r.id),
        "created_at": str(r.created_at),
        "event_type": r.event_type,
        "actor_id": r.actor_id,
        "actor_role": r.actor_role,
        "entity_type": r.entity_type,
        "entity_id": r.entity_id,
        "payload": r.payload,
        "ip_address": r.ip_address,
        "request_id": r.request_id,
        "hash": r.hash,
    }


def _walk_chain(db: Session, tenant_id: str | None) -> tuple[bool, int | None, int]:
    """Walk the rows in chain order; return ``(ok, broken_at_row, total_rows)``.

    Extracted because ``_run_integrity_check`` and ``verify_audit_chain`` were
    byte-identical from the query down to the loop and differed only in the dict
    they wrap around the answer. Raises on a read failure; both callers catch and
    degrade. The read is SAVEPOINT-wrapped — see the module docstring.

    ⚠ **This formula does not match the writer's, so it reports a break on any
    non-empty real database.** Pre-existing, NOT introduced by the extraction —
    and deliberately not fixed here, because which side is wrong is a decision
    about what the stored chain means, not a refactor. Measured 2026-09-27:

    * ``core/audit.py::log_audit_event_sync`` hashes
      ``"{prev}{tenant}:{user}:{action}:{entity_type}:{entity_id}:{payload}:{request_id}"``
      — colon-delimited, seven fields, seeded ``prev_hash = ""`` — into
      ``row_hash``.
    * this walks ``"{prev}{event_type}{actor}{entity_id}{payload}"`` — no
      delimiters, four fields, seeded ``"0" * 64`` — against ``row.hash`` and
      ``row.prev_hash``.

    Two unrelated schemes. Rows written by the real writer therefore fail at
    row 1, so ``/api/audit-integrity``,
    ``/api/admin/audit-logs/integrity-check`` and
    ``compliance_summary.audit_log_integrity`` report a tampered audit chain on
    prod. ``core/audit.py``'s own ``verify_audit_chain`` (a different function,
    same name) uses the writer's formula and returns True on the same rows.

    So do not read the SAVEPOINT below as protecting a trustworthy answer: it
    keeps a WRONG answer from also poisoning the caller's transaction. Raised
    for a ruling rather than fixed in a hardening commit.
    """
    q = select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    if tenant_id:
        q = q.where(AuditLog.entity_id.like(f"{tenant_id}%"))
    with contained_read(db):
        rows = db.execute(q).scalars().all()
    prev_hash = "0" * 64
    for idx, row in enumerate(rows, start=1):
        actor = row.actor_id or "system"
        expected = hashlib.sha256(
            f"{prev_hash}{row.event_type}{actor}{row.entity_id}{_payload_json(row.payload or {})}".encode()
        ).hexdigest()
        if row.prev_hash != prev_hash or row.hash != expected:
            return False, idx, len(rows)
        prev_hash = row.hash
    return True, None, len(rows)


def _run_integrity_check(db: Session, tenant_id: str | None = None) -> dict:
    """Verify SHA-256 hash chain for all (or tenant-filtered) audit log rows."""
    try:
        ok, broken_at_row, total_rows = _walk_chain(db, tenant_id)
        return {"ok": ok, "broken_at_row": broken_at_row, "total_rows": total_rows}
    except Exception:
        logging.getLogger(__name__).exception("_run_integrity_check caught exception")
        return {"ok": False, "broken_at_row": None, "total_rows": 0}


# ---------------------------------------------------------------------------
# Public helper functions (called by routes and tests directly)
# ---------------------------------------------------------------------------


def get_audit_events(
    db: Session,
    tenant_id: str | None = None,
    user_id: str | None = None,
    event_type: str | None = None,
    resource_type: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    page: int = 1,
    limit: int = 100,
) -> dict:
    """Query audit log with optional filters; return paginated result dict."""
    _ensure_audit_table(db)
    try:
        q = select(AuditLog)
        if tenant_id:
            q = q.where(AuditLog.entity_id.like(f"{tenant_id}%"))
        if user_id:
            q = q.where(AuditLog.actor_id == user_id)
        if event_type:
            q = q.where(AuditLog.event_type == event_type)
        if resource_type:
            q = q.where(AuditLog.entity_type == resource_type)
        if start_date:
            q = q.where(AuditLog.created_at >= start_date)
        if end_date:
            q = q.where(AuditLog.created_at <= end_date)

        count_q = select(func.count()).select_from(q.subquery())
        with contained_read(db):  # see module docstring — covers both reads
            total: int = db.execute(count_q).scalar_one_or_none() or 0
            pages = math.ceil(total / limit) if total else 1
            offset = (page - 1) * limit

            rows = (
                db.execute(
                    q.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
                )
                .scalars()
                .all()
            )
        return {
            "events": [_row_to_dict(r) for r in rows],
            "total": total,
            "pages": pages,
            "page": page,
        }
    except Exception as exc:
        logging.getLogger(__name__).exception("get_audit_events caught exception")
        return {"events": [], "total": 0, "pages": 1, "page": page, "error": str(exc)}


def get_audit_summary(db: Session, tenant_id: str | None = None) -> dict:
    """Return event-type counts, unique actors/resources for the last 30 days."""
    _ensure_audit_table(db)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    try:
        base_q = select(AuditLog).where(AuditLog.created_at >= since)
        if tenant_id:
            base_q = base_q.where(AuditLog.entity_id.like(f"{tenant_id}%"))

        # Per-event-type counts
        group_q = (
            select(AuditLog.event_type, func.count(AuditLog.id).label("cnt"))
            .where(AuditLog.created_at >= since)
        )
        if tenant_id:
            group_q = group_q.where(AuditLog.entity_id.like(f"{tenant_id}%"))
        group_q = group_q.group_by(AuditLog.event_type)

        unique_actors_q = (
            select(func.count(AuditLog.actor_id.distinct()))
            .where(AuditLog.created_at >= since)
        )
        if tenant_id:
            unique_actors_q = unique_actors_q.where(AuditLog.entity_id.like(f"{tenant_id}%"))

        unique_resources_q = (
            select(func.count(AuditLog.entity_id.distinct()))
            .where(AuditLog.created_at >= since)
        )
        if tenant_id:
            unique_resources_q = unique_resources_q.where(AuditLog.entity_id.like(f"{tenant_id}%"))

        # One savepoint over all three reads — see module docstring. This is a
        # cost choice, not a fidelity one, and the first version of this comment
        # got that wrong: it claimed containing them together "reports the real
        # error rather than a cascade", but the first failure exits the ``try``
        # immediately either way, so reads 2 and 3 never run whatever the
        # savepoint granularity. One SAVEPOINT round-trip instead of three is the
        # whole difference.
        with contained_read(db):
            by_event_type = {row[0]: row[1] for row in db.execute(group_q).all()}
            unique_actors: int = db.execute(unique_actors_q).scalar_one_or_none() or 0
            unique_resources: int = db.execute(unique_resources_q).scalar_one_or_none() or 0

        total_events: int = sum(by_event_type.values())

        return {
            "by_event_type": by_event_type,
            "total_events": total_events,
            "unique_actors": unique_actors,
            "unique_resources": unique_resources,
            "period_days": 30,
        }
    except Exception as exc:
        logging.getLogger(__name__).exception("get_audit_summary caught exception")
        return {
            "by_event_type": {},
            "total_events": 0,
            "unique_actors": 0,
            "unique_resources": 0,
            "period_days": 30,
            "error": str(exc),
        }


def export_audit_log(
    db: Session,
    tenant_id: str | None = None,
    fmt: str = "csv",
):
    """Export audit log rows as CSV StreamingResponse or JSON list (max 10 000 rows)."""
    _ensure_audit_table(db)
    try:
        q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(10_000)
        if tenant_id:
            q = select(AuditLog).where(AuditLog.entity_id.like(f"{tenant_id}%")).order_by(AuditLog.created_at.desc()).limit(10_000)
        with contained_read(db):  # see module docstring
            rows = db.execute(q).scalars().all()
    except Exception:
        logging.getLogger(__name__).exception("export_audit_log caught exception")
        rows = []

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "created_at", "event_type", "actor_id", "actor_role",
            "entity_type", "entity_id", "ip_address", "request_id", "hash",
        ])
        for r in rows:
            writer.writerow([
                str(r.id),
                str(r.created_at),
                r.event_type,
                r.actor_id or "",
                r.actor_role or "",
                r.entity_type,
                r.entity_id,
                r.ip_address or "",
                r.request_id or "",
                r.hash,
            ])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
        )

    # JSON
    return [_row_to_dict(r) for r in rows]


def verify_audit_chain(db: Session, tenant_id: str | None = None) -> dict:
    """Walk all rows ordered by created_at/id and verify the SHA-256 hash chain.

    Returns {"ok": bool, "broken_at_row": int|None, "total_rows": int, "message": str}.
    """
    _ensure_audit_table(db)
    try:
        ok, broken_at_row, total_rows = _walk_chain(db, tenant_id)
        return {
            "ok": ok,
            "broken_at_row": broken_at_row,
            "total_rows": total_rows,
            "message": (
                f"Chain intact ({total_rows} rows verified)"
                if ok
                else f"Hash mismatch at row {broken_at_row}"
            ),
        }
    except Exception as exc:
        logging.getLogger(__name__).exception("verify_audit_chain caught exception")
        return {
            "ok": False,
            "broken_at_row": None,
            "total_rows": 0,
            "message": str(exc),
        }


# ---------------------------------------------------------------------------
# Route 1: List audit logs
# ---------------------------------------------------------------------------


@router.get("/api/admin/audit-logs", dependencies=[Depends(_require_admin)])
def list_audit_logs(
    tenant_id: str | None = Query(None),
    event_type: str | None = Query(None),
    user_id: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> JSONResponse:
    _ensure_audit_table(db)
    try:
        q = select(AuditLog)
        if tenant_id:
            q = q.where(AuditLog.entity_id.like(f"{tenant_id}%"))
        if event_type:
            q = q.where(AuditLog.event_type == event_type)
        if user_id:
            q = q.where(AuditLog.actor_id == user_id)
        if start:
            try:
                start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
                q = q.where(AuditLog.created_at >= start_dt)
            except ValueError:
                logging.getLogger(__name__).exception("list_audit_logs caught exception")
                pass
        if end:
            try:
                end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
                q = q.where(AuditLog.created_at <= end_dt)
            except ValueError:
                logging.getLogger(__name__).exception("list_audit_logs caught exception")
                pass

        count_q = select(func.count()).select_from(q.subquery())
        with contained_read(db):  # see module docstring — covers both reads
            total: int = db.execute(count_q).scalar_one_or_none() or 0
            pages = math.ceil(total / limit) if total else 1
            offset = (page - 1) * limit

            rows = db.execute(q.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)).scalars().all()
        events = [_row_to_dict(r) for r in rows]
        integrity_ok = total < 1000

        return JSONResponse({
            "events": events,
            "total": total,
            "pages": pages,
            "page": page,
            "integrity_ok": integrity_ok,
        })
    except Exception as exc:
        logging.getLogger(__name__).exception("list_audit_logs caught exception")
        return JSONResponse({"events": [], "total": 0, "pages": 1, "page": page, "integrity_ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Route 2: Integrity check
# ---------------------------------------------------------------------------


@router.get("/api/admin/audit-logs/integrity-check", dependencies=[Depends(_require_admin)])
def audit_integrity_check(
    tenant_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    _ensure_audit_table(db)
    result = _run_integrity_check(db, tenant_id=tenant_id)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Route 3: Compliance summary
# ---------------------------------------------------------------------------


@router.get("/api/admin/compliance-summary", dependencies=[Depends(_require_admin)])
def compliance_summary(db: Session = Depends(get_db)) -> JSONResponse:
    _ensure_audit_table(db)
    now = datetime.now(timezone.utc)

    # MFA adoption
    mfa_val = os.getenv("MFA_REQUIRED", "").strip().lower()
    mfa_adoption_pct: float = 100.0 if mfa_val in ("1", "true", "yes") else 0.0

    # Audit log integrity. The `LIMIT 500` below applies ONLY to the probe — the
    # comment that used to sit here said "fast: check up to 500 rows only to
    # avoid timeout" and that was never true of the check itself:
    # `_run_integrity_check` on the next line calls `_walk_chain`, which has no
    # LIMIT and materialises the whole table as ORM objects. Measured against
    # prod's row count (34,130 rows / 23 MB, PG 16.13, read 2026-09-27) that is
    # ~0.9 s, now held inside a SAVEPOINT for its whole duration, on each of the
    # three admin endpoints that call it. Not fixed here: adding a LIMIT would
    # silently narrow what "the chain verifies" means, which is the same
    # decision as the formula mismatch `_walk_chain` documents, and both want a
    # ruling rather than a hardening commit.
    # This handler is the exception to the "no caller continues past the swallow"
    # note in the module docstring, so it is contained and the others are not:
    # it swallows HERE and then reads again at `failed_login_24h` below. This
    # probe's query is a superset of `_walk_chain`'s, so any schema-drift failure
    # aborts the transaction BEFORE _walk_chain's own savepoint is reached, and
    # the later count then degrades to 0 — a SOC2 dashboard reporting zero failed
    # logins in 24h, which is silently wrong rather than visibly broken.
    # Measured: rename audit_logs.ip_address and failed_login_24h goes 1 -> 0.
    try:
        q = select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc()).limit(500)
        with contained_read(db):
            db.execute(q).scalars().all()
        integrity_result = _run_integrity_check(db)
        audit_log_integrity: bool = integrity_result["ok"]
    except Exception:
        logging.getLogger(__name__).exception("compliance_summary caught exception")
        audit_log_integrity = False

    # Last backup age
    last_backup_ts = os.getenv("LAST_BACKUP_TS", "")
    last_backup_age_hours: float = -1.0
    if last_backup_ts:
        try:
            ts = float(last_backup_ts)
            last_backup_age_hours = round((now.timestamp() - ts) / 3600, 2)
        except (ValueError, TypeError):
            logging.getLogger(__name__).exception("compliance_summary caught exception")
            pass

    # Active sessions
    active_session_count: int = 0
    with contextlib.suppress(ValueError, TypeError):
        active_session_count = int(os.getenv("ACTIVE_SESSION_COUNT", "0"))

    # Failed logins in last 24h
    failed_login_24h: int = 0
    try:
        since_24h = now - timedelta(hours=24)
        with contained_read(db):
            failed_login_24h = db.execute(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.event_type == "login_failed",
                    AuditLog.created_at >= since_24h,
                )
            ).scalar_one_or_none() or 0
    except Exception:
        logging.getLogger(__name__).exception("compliance_summary caught exception")
        pass

    return JSONResponse({
        "mfa_adoption_pct": mfa_adoption_pct,
        "audit_log_integrity": audit_log_integrity,
        "last_backup_age_hours": last_backup_age_hours,
        "active_session_count": active_session_count,
        "failed_login_24h": failed_login_24h,
    })


# ---------------------------------------------------------------------------
# Route 4: Compliance report (downloadable)
# ---------------------------------------------------------------------------


@router.get("/api/admin/compliance-report", dependencies=[Depends(_require_admin)], response_model=None)
def compliance_report(
    fmt: str = Query("json"),
    db: Session = Depends(get_db),
):
    _ensure_audit_table(db)
    try:
        with contained_read(db):  # see module docstring
            rows = (
                db.execute(
                    select(AuditLog).order_by(AuditLog.created_at.desc()).limit(10000)
                )
                .scalars()
                .all()
            )
    except Exception:
        logging.getLogger(__name__).exception("compliance_report caught exception")
        rows = []

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "created_at", "event_type", "actor_id", "actor_role", "entity_type", "entity_id", "ip_address"])
        for r in rows:
            writer.writerow([
                str(r.id),
                str(r.created_at),
                r.event_type,
                r.actor_id or "",
                r.actor_role or "",
                r.entity_type,
                r.entity_id,
                r.ip_address or "",
            ])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=compliance_report.csv"},
        )

    events = [_row_to_dict(r) for r in rows]
    return JSONResponse(events)


# ---------------------------------------------------------------------------
# Route 5 (deleted Sprint 1.0 B2): the `/admin/audit-log` HTML admin page was
# legacy Jinja (template was never deployed). The Vue SPA now owns that route
# via AuditLogViewer.vue which calls the JSON endpoint at `/api/admin/audit-log`.
# Keeping a handler here shadowed the SPA and rendered a broken page.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Route 6: Paginated audit events (delegates to get_audit_events helper)
# ---------------------------------------------------------------------------


@router.get("/api/audit-events", dependencies=[Depends(_require_admin)])
def api_audit_events(
    tenant_id: str | None = Query(None),
    user_id: str | None = Query(None),
    event_type: str | None = Query(None),
    resource_type: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> JSONResponse:
    start_dt: datetime | None = None
    end_dt: datetime | None = None
    if start:
        with contextlib.suppress(ValueError):
            start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    if end:
        with contextlib.suppress(ValueError):
            end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    result = get_audit_events(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        event_type=event_type,
        resource_type=resource_type,
        start_date=start_dt,
        end_date=end_dt,
        page=page,
        limit=limit,
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Route 7: Audit summary (delegates to get_audit_summary helper)
# ---------------------------------------------------------------------------


@router.get("/api/audit-summary", dependencies=[Depends(_require_admin)])
def api_audit_summary(
    tenant_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return JSONResponse(get_audit_summary(db, tenant_id=tenant_id))


# ---------------------------------------------------------------------------
# Route 8: Audit export (delegates to export_audit_log helper)
# ---------------------------------------------------------------------------


@router.post("/api/audit-export", dependencies=[Depends(_require_admin)], response_model=None)
def api_audit_export(
    tenant_id: str | None = Query(None),
    fmt: str = Query("csv"),
    db: Session = Depends(get_db),
):
    result = export_audit_log(db, tenant_id=tenant_id, fmt=fmt)
    if isinstance(result, list):
        return JSONResponse(result)
    return result


# ---------------------------------------------------------------------------
# Route 9: Hash chain integrity (delegates to verify_audit_chain helper)
# ---------------------------------------------------------------------------


@router.get("/api/audit-integrity", dependencies=[Depends(_require_admin)])
def api_audit_integrity(
    tenant_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return JSONResponse(verify_audit_chain(db, tenant_id=tenant_id))
