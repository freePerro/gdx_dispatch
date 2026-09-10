"""Shared object-level authorization for job-scoped endpoints.

Dispatch/admin roles may access any job in the tenant; a plain technician may
only access jobs assigned to them — either directly (jobs.assigned_to == their
user id) or via an appointment tying their technician record to the job. Raises
404 (not 403) so one technician cannot probe another technician's job ids.

Mirrors routers/mobile.py's _job_belongs_to_user so the web and mobile surfaces
enforce the same rule from one place.
"""
from __future__ import annotations

import uuid as _uuid
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


def _job_match(column: str) -> str:
    """SQL fragment matching `column` against a job id, engine-agnostically.

    Two engines disagree about how a job id is spelled, and a third case is not
    a uuid at all:

    * Postgres stores ``jobs.id`` as ``uuid`` and renders it lowercase-dashed;
    * SQLite (the test harness, and ``create_all`` boxes) stores the same value
      as 32 hex characters with the dashes stripped;
    * plenty of job ids in this codebase are plain strings — ``"job-1"`` — and
      must keep working.

    So the fragment always compares as TEXT, against a value canonicalised in
    :func:`_job_id_params`. The native ``= :j_native`` branch is added ONLY when
    the caller's id parsed as a uuid, because on Postgres handing raw client
    text to a uuid column raises InvalidTextRepresentation — a 500 with the
    transaction aborted, not a 404. A phone posting ``job_id=undefined`` is an
    ordinary event on these routes, and the SQLite harness cannot see it.
    """
    return f"({column} = :j_native OR CAST({column} AS TEXT) IN (:j, :jh))"


def _job_match_text_only(column: str) -> str:
    """The same, minus the native branch, for an id that is not a uuid."""
    return f"(CAST({column} AS TEXT) IN (:j, :jh))"


def _job_id_params(job_id: str) -> tuple[str, dict]:
    """(sql-fragment-builder-key, params) for one job id.

    Returns the params to bind and whether the native comparison is safe.
    """
    raw = str(job_id or "")
    try:
        canonical = str(_uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        # Not a uuid: never touch the native uuid comparison, but still match
        # a plain string id like "job-1" as text.
        return "text", {"j": raw, "jh": raw}
    return "native", {"j": canonical, "jh": canonical.replace("-", ""),
                      "j_native": canonical}


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
    mode, jp = _job_id_params(job_id)
    match = _job_match if mode == "native" else _job_match_text_only
    params = {**jp, "t": tenant_id, "u": str(user_id)}
    assigned = db.execute(
        text(
            "SELECT 1 FROM jobs j "  # noqa: S608 — the only interpolation is a hardcoded
            # column name chosen by _job_match/_job_match_text_only above;
            # every caller-supplied value is a bound parameter.
            "LEFT JOIN technicians t ON t.id = j.assigned_to "
            "WHERE " + match("j.id") + " "
            "AND j.company_id = :t AND j.deleted_at IS NULL "
            "AND (j.assigned_to = :u OR t.user_id = :u) LIMIT 1"
        ),
        params,
    ).scalar()
    if assigned:
        return True
    via_appt = db.execute(
        text(
            "SELECT 1 FROM appointments a JOIN technicians te ON te.id = a.tech_id "  # noqa: S608 — the only interpolation is a hardcoded
            # column name chosen by _job_match/_job_match_text_only above;
            # every caller-supplied value is a bound parameter.
            "WHERE " + match("a.job_id") + " AND a.company_id = :t "
            "AND a.deleted_at IS NULL "
            "AND te.user_id = :u LIMIT 1"
        ),
        params,
    ).scalar()
    if via_appt:
        return True
    via_assignment = db.execute(
        text(
            "SELECT 1 FROM job_assignments ja "  # noqa: S608 — the only interpolation is a hardcoded
            # column name chosen by _job_match/_job_match_text_only above;
            # every caller-supplied value is a bound parameter.
            "JOIN technicians te ON te.id = ja.tech_id "
            "JOIN jobs j ON CAST(j.id AS TEXT) = CAST(ja.job_id AS TEXT) "
            "WHERE " + match("ja.job_id") + " AND ja.deleted_at IS NULL "
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

def creator_of_unassigned_job(
    db: Session, tenant_id: str, job_id: str, user_id: str | None
) -> bool:
    """True while a job the caller created is still unassigned.

    Deliberately NOT folded into ``job_belongs_to_user``: that helper is the
    ownership gate for start/complete/clock/status, and a creator must never
    keep a pass-through onto a job dispatch has since given to someone else
    (clock data is payroll evidence). The moment ``assigned_to`` or a live
    ``job_assignments`` row exists this returns False and normal assignment
    rules are the only way in. The 2026-07-22 audit of the mobile job-create
    fix demanded that split.

    It lives here, beside the ownership gate, so the two can never disagree —
    ``routers/mobile.py._creator_can_read`` delegates to it, and so does every
    attachment route that has to accept "a tech photographing the job they just
    created, before dispatch has assigned it".
    """
    if not job_id or not user_id or not tenant_id:
        return False
    mode, jp = _job_id_params(job_id)
    match = _job_match if mode == "native" else _job_match_text_only
    return bool(
        db.execute(
            text(
                "SELECT 1 FROM jobs j "  # noqa: S608 — the only interpolation is a hardcoded
            # column name chosen by _job_match/_job_match_text_only above;
            # every caller-supplied value is a bound parameter.
                "WHERE " + match("j.id") + " "
                "AND j.company_id = :t AND j.deleted_at IS NULL "
                "AND CAST(j.created_by AS TEXT) = :u "
                "AND j.assigned_to IS NULL "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM job_assignments ja "
                "  WHERE CAST(ja.job_id AS TEXT) = CAST(j.id AS TEXT) "
                "  AND ja.deleted_at IS NULL"
                ") LIMIT 1"
            ),
            {**jp, "t": tenant_id, "u": str(user_id)},
        ).scalar()
    )


def assert_can_attach_to_job(
    db: Session, tenant_id: str, current_user: Any, job_id: str
) -> None:
    """Gate for a route that attaches something to a CALLER-SUPPLIED job id.

    ``POST /api/documents`` took `job_id` straight from the multipart form and
    checked nothing (#518): a technician not assigned to a job could create a
    `job_photos` row on it — proven by execution, 201 with `uploaded_by` set to
    them, while the read gate 404'd the same user on the same job. Photos are
    evidence attached to someone else's work.

    Three ways in, matching the surfaces that legitimately post here:
      * dispatch/admin — any job in the tenant;
      * the assigned technician (``job_belongs_to_user``, all four paths);
      * the creator of a still-unassigned job — the mobile flow where a tech
        creates a job in the dialog and photographs it before assignment.

    Raises the same opaque 404 the mobile read gates use, so a technician
    cannot probe another technician's job ids by watching the status code.

    WHO THIS REFUSES, deliberately recorded because it is a role-model question
    and not an oversight. ``is_dispatch_manager`` covers owner/admin/dispatcher.
    The other builtin roles are refused:

      * ``viewer``  — read-only auditor by definition; a write is not its job.
      * ``accounting`` — holds no ``jobs.*`` permission at all.
      * ``sales`` — holds ``jobs.read_all`` but NOT ``jobs.write``, so it can
        see every job and cannot write to one. Refusing a write is consistent
        with that, but sales can reach the /documents "Link to Job" dialog, so
        the button is there and will 404. Whether sales should be able to
        attach a document to a job is a product call, not something to decide
        inside an authz helper.

    No live trigger today: prod's users are owner, technician and dispatcher
    only (checked 2026-09-10). ``test_job_attachment_authz`` pins the current
    answer for every builtin role so a change here is deliberate.
    """
    if is_dispatch_manager(current_user):
        return
    uid = _user_id(current_user)
    if job_belongs_to_user(db, tenant_id, job_id, uid):
        return
    if creator_of_unassigned_job(db, tenant_id, job_id, uid):
        return
    raise HTTPException(status_code=404, detail="Job not found")
