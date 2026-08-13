"""Shared object-level authorization for job-scoped endpoints.

Dispatch/admin roles may access any job in the tenant; a plain technician may
only access jobs assigned to them — either directly (jobs.assigned_to == their
user id) or via an appointment tying their technician record to the job. Raises
404 (not 403) so one technician cannot probe another technician's job ids.

Mirrors routers/mobile.py's _job_belongs_to_user so the web and mobile surfaces
enforce the same rule from one place.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.permissions import is_dispatch_manager


def _user_id(user: Any) -> str:
    u = user or {}
    if isinstance(u, dict):
        return str(u.get("user_id") or u.get("sub") or "")
    return str(getattr(u, "user_id", "") or getattr(u, "sub", "") or "")


def job_belongs_to_user(db: Session, tenant_id: str, job_id: str, user_id: str | None) -> bool:
    """True if job_id is assigned to the user.

    CRITICAL: jobs.assigned_to stores a *technician.id* (varchar), not a
    users.id — so we must map the caller's user id to their technician record.
    Ownership holds if ANY of:
      (a) jobs.assigned_to == the caller's technician id (the common case), or
      (b) jobs.assigned_to == the caller's user id (legacy/direct), or
      (c) an appointment ties the caller's technician record to the job, or
      (d) a Phase 1.4 job_assignments row ties their technician record to the
          job — the /api/mobile/jobs list matches these, so the ownership gate
          must too or a listed job 404s on open (2026-07-16 audit finding).
    All columns are varchar (CASTs cover jobs.id/job_assignments.job_id being
    uuid vs varchar across planes), so the SQL is portable (PG + SQLite).
    """
    if not job_id or not tenant_id or not user_id:
        return False
    params = {"j": str(job_id), "t": tenant_id, "u": str(user_id)}
    assigned = db.execute(
        text(
            "SELECT 1 FROM jobs j "
            "LEFT JOIN technicians t ON t.id = j.assigned_to "
            "WHERE j.id = :j AND j.company_id = :t AND j.deleted_at IS NULL "
            "AND (j.assigned_to = :u OR t.user_id = :u) LIMIT 1"
        ),
        params,
    ).scalar()
    if assigned:
        return True
    via_appt = db.execute(
        text(
            "SELECT 1 FROM appointments a JOIN technicians te ON te.id = a.tech_id "
            "WHERE a.job_id = :j AND a.company_id = :t AND a.deleted_at IS NULL "
            "AND te.user_id = :u LIMIT 1"
        ),
        params,
    ).scalar()
    if via_appt:
        return True
    via_assignment = db.execute(
        text(
            "SELECT 1 FROM job_assignments ja "
            "JOIN technicians te ON te.id = ja.tech_id "
            "JOIN jobs j ON CAST(j.id AS TEXT) = CAST(ja.job_id AS TEXT) "
            "WHERE CAST(ja.job_id AS TEXT) = :j AND ja.deleted_at IS NULL "
            "AND j.company_id = :t AND j.deleted_at IS NULL "
            "AND te.user_id = :u LIMIT 1"
        ),
        params,
    ).scalar()
    return bool(via_assignment)


def user_job_ids(db: Session, tenant_id: str, user_id: str | None) -> list[str]:
    """Every job id the user owns — the SET form of job_belongs_to_user.

    Same four ownership paths (a-d above), asked once instead of per-job, for
    callers that need to scope a LIST rather than gate a single row. It lives
    here, beside the single-row gate, precisely so the two can never disagree:
    the A1 audit finding (2026-07-29) was a forked ownership query that
    silently matched nothing, and field billing 404'd on ~90% of jobs for a
    year because of it. If you change the paths, change them in both.

    Returns ids as TEXT — jobs.id is uuid on Postgres and varchar on SQLite,
    and callers compare against invoices.job_id across the same split.
    """
    if not tenant_id or not user_id:
        return []
    rows = db.execute(
        text(
            "SELECT CAST(j.id AS TEXT) AS jid FROM jobs j "
            "LEFT JOIN technicians t ON t.id = j.assigned_to "
            "WHERE j.company_id = :t AND j.deleted_at IS NULL "
            "AND (j.assigned_to = :u OR t.user_id = :u) "
            "UNION "
            "SELECT CAST(a.job_id AS TEXT) FROM appointments a "
            "JOIN technicians te ON te.id = a.tech_id "
            "WHERE a.company_id = :t AND a.deleted_at IS NULL AND te.user_id = :u "
            "UNION "
            "SELECT CAST(ja.job_id AS TEXT) FROM job_assignments ja "
            "JOIN technicians te ON te.id = ja.tech_id "
            "JOIN jobs j2 ON CAST(j2.id AS TEXT) = CAST(ja.job_id AS TEXT) "
            "WHERE ja.deleted_at IS NULL AND j2.company_id = :t "
            "AND j2.deleted_at IS NULL AND te.user_id = :u"
        ),
        {"t": tenant_id, "u": str(user_id)},
    ).scalars().all()
    return [r for r in rows if r]


def assert_job_access(db: Session, tenant_id: str, current_user: Any, job_id: str) -> None:
    """Raise 404 unless the caller may access this job (dispatch/admin = any;
    technician = own jobs only)."""
    if is_dispatch_manager(current_user):
        return
    if not job_belongs_to_user(db, tenant_id, job_id, _user_id(current_user)):
        raise HTTPException(status_code=404, detail="Job not found")
