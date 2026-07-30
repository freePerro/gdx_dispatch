"""The one way to read a job's closeout — plan §12.

`job_closeouts` is append-only history under the supersede model: a
re-closeout stamps `superseded_at` on the prior row and links the new row
back via `supersedes_id`. Exactly one live row per job is enforced by the
partial unique index `uq_job_closeouts_live`.

Readers MUST come through here (or replicate the exact filter):

* a bare ``job_id`` filter with ``scalar_one_or_none()`` raises
  ``MultipleResultsFound`` the first time any job is re-closed out — an
  office-facing 500 on precisely the jobs someone corrected;
* an aggregate over a bare ``job_id`` join double-counts hours — the
  tech-efficiency report had exactly this bug until 2026-07-29.

The raw-SQL equivalent of :func:`get_current_closeout`'s filter is::

    jc.superseded_at IS NULL AND jc.deleted_at IS NULL
"""
from __future__ import annotations

import uuid as _uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import JobCloseout


def get_current_closeout(db: Session, job_id: _uuid.UUID | str) -> JobCloseout | None:
    """The job's CURRENT closeout snapshot, or None if never closed out."""
    jid = job_id if isinstance(job_id, _uuid.UUID) else _uuid.UUID(str(job_id))
    return db.execute(
        select(JobCloseout).where(
            JobCloseout.job_id == jid,
            JobCloseout.superseded_at.is_(None),
            JobCloseout.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


def get_closeout_history(db: Session, job_id: _uuid.UUID | str) -> list[JobCloseout]:
    """Every attestation for the job, newest first — the current row plus each
    superseded restatement, INCLUDING soft-deleted rows. The history is the
    audit story, so nothing is filtered out of it: an administratively-deleted
    attestation still happened, and hiding it here would contradict the
    evidence-preservation rule the supersede model exists for (adversarial
    audit, round 2 — nothing sets JobCloseout.deleted_at today, but the day a
    delete path appears this reader must not quietly swallow rows). Callers
    that need to BADGE deleted rows can read ``deleted_at`` off each row.

    Ordered by (closed_at, created_at) desc — created_at breaks the tie when
    two restatements share a closed_at second, so the display order matches
    insertion order rather than flapping."""
    jid = job_id if isinstance(job_id, _uuid.UUID) else _uuid.UUID(str(job_id))
    return list(
        db.execute(
            select(JobCloseout)
            .where(JobCloseout.job_id == jid)
            .order_by(JobCloseout.closed_at.desc(), JobCloseout.created_at.desc())
        ).scalars()
    )
