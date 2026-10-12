"""Number the live jobs that were created without a job_number (GDXA-834).

Several create paths built a ``Job`` with no ``job_number`` (onboarding demo
data, estimate conversion, service calls, service triggers), and every path
that does allocate one leaves the job unnumbered when allocation fails. The
office then sees a UUID prefix where the number belongs. Prod held 36
estimate-converted jobs and 4 open service calls with no number (maintainer's
ledger, 2026-10-03; recount on prod and demo before deploying, see the PR).
The onboarding create path is fixed in the same change; the other create
paths are their owners' sibling fixes, so a job created after this revision
runs and before those land comes in unnumbered again, and alembic will not
rerun 119.

Existing rows, exactly:

- **Changed:** a job with ``job_number IS NULL`` and ``deleted_at IS NULL``.
  It gets the next number from the tenant's own counter, rendered with the
  tenant's own ``job_number_format`` through ``apply_template``, the same
  renderer ``next_job_number`` uses. Jobs are numbered oldest first
  (``created_at``, then ``id``), so the numbers follow history. ``{year}`` is
  the year the migration runs, as it is for a job created today: a template
  with ``{year}`` gets this year's series, and the counter resets on a year
  change exactly as ``next_job_number`` resets it.
- **Counter:** ``tenant_settings.job_number_next_seq`` moves past the last
  number handed out and ``job_number_year_seen`` is stamped, so the next job
  created cannot be given a number this migration used.
- **Never reused:** a rendered number already on any job, live or deleted, is
  skipped and the counter moves on. The live unique index
  (``ux_jobs_job_number_live``, 048) therefore cannot refuse an update here.
- **Left as is:** soft-deleted jobs (numbering them would only burn numbers),
  and every job when no ``tenant_settings`` row can be matched to it: one row
  serves every job (single tenant); with several rows, the job's
  ``company_id`` must match one. Left jobs are logged at WARNING. A template
  that cannot produce a fresh number (no ``{seq}``) stops numbering with a
  WARNING instead of looping.
- ``jobs.updated_at`` is not touched: the technician activity reader treats a
  moved ``updated_at`` as activity, and this is not activity. The record is
  the audit event.

Trail: one hash-chained audit event (``job_number_backfill``) through
``log_audit_event_sync``, as 117 does, flushed into this migration's
transaction, never committed on its own. It lists every job id with the
number it got. Nothing numbered, no event.

Portability: plain SELECTs and per-row UPDATEs keyed on values read back from
the same table, so the SQLite dashless-hex trap does not apply: an id or a
``tenant_id`` is bound back in the form the engine returned it in. The
downgrade re-encodes ids per dialect. No ``%`` in any SQL. Not a row-refusing
migration: it never raises on data. Rerunnable: a second run finds no
unnumbered live job and writes nothing.

Rollback: ``downgrade()`` reads this migration's audit events and sets
``job_number`` back to NULL on each listed job that still holds the number it
was given. The counter is NOT wound back: numbers may have been handed out
since, and a counter moved backwards re-issues them. The gap it leaves is
harmless. By hand: take ``details.jobs`` (id to number) from the
``audit_logs`` row with ``action = 'job_number_backfill'`` and run
``UPDATE jobs SET job_number = NULL WHERE id = :id AND job_number = :number``
for each pair.

Revision ID: 119_backfill_job_numbers
Revises: 118_closeout_billed_invoice
Create Date: 2026-10-12
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from alembic import op
from sqlalchemy import inspect, text

revision = "119_backfill_job_numbers"
down_revision = "118_closeout_billed_invoice"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

ACTION = "job_number_backfill"
ACTOR = "migration:119_backfill_job_numbers"
_DEFAULT_FORMAT = "JOB-{year}-{seq:03d}"
_MAX_SKIPS = 100_000

_UNNUMBERED = text(
    """
SELECT j.id, j.company_id, c.name AS customer_name
FROM jobs j
LEFT JOIN customers c ON c.id = j.customer_id
WHERE j.job_number IS NULL
  AND j.deleted_at IS NULL
ORDER BY j.created_at, j.id
"""
)
_USED = text("SELECT job_number FROM jobs WHERE job_number IS NOT NULL")
_SETTINGS = text(
    "SELECT tenant_id, job_number_format, job_number_next_seq, job_number_year_seen "
    "FROM tenant_settings"
)
_NUMBER = text("UPDATE jobs SET job_number = :number WHERE id = :id AND job_number IS NULL")
_COUNTER = text(
    "UPDATE tenant_settings SET job_number_next_seq = :seq, job_number_year_seen = :year "
    "WHERE tenant_id = :tid"
)
_EVENTS = text("SELECT details FROM audit_logs WHERE action = :action")
_UNNUMBER = text("UPDATE jobs SET job_number = NULL WHERE id = :id AND job_number = :number")


def _dashed(raw) -> str:
    return str(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))


def _stored(dialect: str, dashed: str) -> str:
    """The id as this engine stores a Uuid column: SQLite keeps 32 hex."""
    return uuid.UUID(dashed).hex if dialect == "sqlite" else dashed


def _uuid_or_none(raw) -> uuid.UUID | None:
    try:
        return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None


class _Counter:
    """One tenant_settings row's counter, advanced the way next_job_number does."""

    def __init__(self, row, year: int) -> None:
        self.tenant_id = row.tenant_id
        self.template = row.job_number_format or _DEFAULT_FORMAT
        self.seq = int(row.job_number_next_seq or 1)
        self.year = year
        if ("{year}" in self.template or "{yy}" in self.template) and row.job_number_year_seen and row.job_number_year_seen != year:
            self.seq = 1
        self.used_any = False
        self.exhausted = False

    def next(self, used: set[str], customer_name: str | None) -> str | None:
        from gdx_dispatch.modules.numbering.service import apply_template

        if self.exhausted:
            return None
        start = self.seq
        for _ in range(_MAX_SKIPS):
            number = apply_template(self.template, self.seq, year=self.year, customer_name=customer_name)
            self.seq += 1
            if number not in used:
                self.used_any = True
                return number
        # No fresh number in reach: the counter keeps where it was.
        self.seq = start
        self.exhausted = True
        return None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if not {"jobs", "customers", "tenant_settings", "audit_logs"} <= tables:
        return
    jobs = bind.execute(_UNNUMBERED).fetchall()
    if not jobs:
        log.info("119: no unnumbered live job")
        return

    year = datetime.now(UTC).year
    counters = [_Counter(r, year) for r in bind.execute(_SETTINGS).fetchall()]
    by_tenant = {_uuid_or_none(c.tenant_id): c for c in counters}
    used = {r[0] for r in bind.execute(_USED).fetchall()}

    numbered: dict[str, str] = {}
    left: list[str] = []
    for job in jobs:
        counter = counters[0] if len(counters) == 1 else by_tenant.get(_uuid_or_none(job.company_id))
        number = counter.next(used, job.customer_name) if counter is not None else None
        if number is None:
            left.append(_dashed(job.id))
            continue
        bind.execute(_NUMBER, {"number": number, "id": job.id})
        used.add(number)
        numbered[_dashed(job.id)] = number

    for counter in counters:
        if counter.used_any:
            tid = counter.tenant_id
            bind.execute(
                _COUNTER,
                {"seq": counter.seq, "year": year, "tid": str(tid) if isinstance(tid, uuid.UUID) else tid},
            )
    if left:
        log.warning("119: %d unnumbered job(s) left as is (no counter or no fresh number): %s", len(left), left)
    log.info("119: %d job(s) numbered, %d left", len(numbered), len(left))
    if not numbered:
        return

    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import log_audit_event_sync

    session = Session(bind=bind)
    tenants = sorted({r.company_id for r in jobs if r.company_id})
    log_audit_event_sync(
        session,
        tenant_id=(tenants[0] if len(tenants) == 1 else None),
        user_id=ACTOR,
        action=ACTION,
        entity_type="jobs",
        entity_id="batch",
        details={
            "reason": "jobs created without a job_number, numbered oldest first (GDXA-834)",
            "jobs": numbered,
            "left_as_is": left,
        },
    )
    session.flush()


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if not {"jobs", "audit_logs"} <= tables:
        return
    pairs: dict[str, str] = {}
    for (details,) in bind.execute(_EVENTS, {"action": ACTION}).fetchall():
        if isinstance(details, str):
            details = json.loads(details)
        pairs.update((details or {}).get("jobs") or {})
    if not pairs:
        return
    dialect = bind.dialect.name
    restored = 0
    for job_id, number in pairs.items():
        restored += bind.execute(_UNNUMBER, {"id": _stored(dialect, job_id), "number": number}).rowcount
    log.info("119 downgrade: %d job(s) set back to job_number NULL", restored)

    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import log_audit_event_sync

    session = Session(bind=bind)
    log_audit_event_sync(
        session,
        user_id=ACTOR,
        action=ACTION + "_reverted",
        entity_type="jobs",
        entity_id="batch",
        details={"job_ids": sorted(pairs), "restored": restored},
    )
    session.flush()
