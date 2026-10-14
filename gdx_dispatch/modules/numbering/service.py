"""Numbering service — atomic next-number generation.

Locks the TenantSettings row via SELECT ... FOR UPDATE
so two concurrent jobs creating in parallel can't grab the same seq.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.tenant_settings import TenantSettings

log = logging.getLogger(__name__)

_INITIALS_RE = re.compile(r"[A-Za-z]+")


def _customer_initials(customer_name: str | None) -> str:
    if not customer_name:
        return ""
    parts = _INITIALS_RE.findall(customer_name)
    if not parts:
        return ""
    # First two words, first letter each — caps. "Becky Meinecke" → "BM".
    return ("".join(p[0] for p in parts[:2])).upper()


def apply_template(template: str, seq: int, *, year: int, customer_name: str | None = None) -> str:
    """Render a number from the template + tokens.

    Falls back to ``JOB-{seq:03d}`` if the template references an
    unknown token — better than crashing job creation."""
    yy = year % 100
    month = datetime.now(timezone.utc).month
    initials = _customer_initials(customer_name)
    try:
        return template.format(
            seq=seq,
            year=year,
            yy=f"{yy:02d}",
            month=f"{month:02d}",
            customer_initials=initials,
        )
    except (KeyError, IndexError, ValueError):
        return f"JOB-{seq:03d}"


def preview(template: str, seq: int, *, year: int | None = None, customer_name: str = "Sample Customer") -> str:
    """Render a sample number for the Settings UI preview pane."""
    y = year if year is not None else datetime.now(timezone.utc).year
    return apply_template(template, seq, year=y, customer_name=customer_name)


class NumberingExhausted(RuntimeError):
    """No number within ``_MAX_SKIPS`` of the counter is free of every job."""


# The same reach migration 119's backfill gives itself. A template with no
# ``{seq}`` renders one number for every seq, so the bound is what stops it.
_MAX_SKIPS = 100_000

_TAKEN_NUMBER = text("SELECT 1 FROM jobs WHERE job_number = :number")
_TAKEN_NUMBERS = text("SELECT job_number FROM jobs WHERE job_number IS NOT NULL")


def first_free_seq(
    db: Session,
    template: str,
    seq: int,
    *,
    year: int,
    customer_name: str | None = None,
) -> tuple[int, str]:
    """The first seq at or after ``seq`` whose number is on no job, and that number.

    A counter behind the numbers already issued (rewound in Settings, or a
    restored database) otherwise hands out a number ``ux_jobs_job_number_live``
    refuses, and the job create 500s (GDXA-924). A soft-deleted job keeps its
    number too, though the index would allow reuse: purchase orders, sent
    emails and the audit trail still quote it, and migration 119's backfill
    never reuses one either. The usual case is one indexed lookup; only a
    collision reads the issued numbers to walk past them.
    """
    rendered = apply_template(template, seq, year=year, customer_name=customer_name)
    if db.execute(_TAKEN_NUMBER, {"number": rendered}).first() is None:
        return seq, rendered
    used = set(db.execute(_TAKEN_NUMBERS).scalars())
    for candidate in range(seq + 1, seq + 1 + _MAX_SKIPS):
        rendered = apply_template(template, candidate, year=year, customer_name=customer_name)
        if rendered not in used:
            return candidate, rendered
    raise NumberingExhausted(f"no free job number within {_MAX_SKIPS} of seq {seq} for {template!r}")


def next_job_number(
    control_db: Session,
    tenant_id: UUID | str,
    *,
    customer_name: str | None = None,
) -> str:
    """Atomically increment the tenant's job counter and render the next number.

    Caller passes a ControlSession (the per-request session from
    ``get_db``). We lock the TenantSettings row ``FOR UPDATE``
    so concurrent job creates serialize on the counter.

    Yearly reset: when the template contains ``{year}`` or ``{yy}`` AND
    the year has changed since job_number_year_seen, the seq resets to 1.

    A number already on a job, live or deleted, is skipped and the counter persisted past
    it (``first_free_seq``). ``jobs`` is read through this same session: the
    app has one engine, so the "control" session is the tenant database.
    """
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))

    # Lock the row. If absent, create with defaults.
    row = control_db.execute(
        text(
            """
            SELECT tenant_id, job_number_format, job_number_next_seq, job_number_year_seen
            FROM tenant_settings
            WHERE tenant_id = :tid
            FOR UPDATE
            """
        ),
        {"tid": str(tid)},
    ).first()

    if row is None:
        ts = TenantSettings(tenant_id=tid)
        control_db.add(ts)
        control_db.flush()
        # Re-read with lock to get the defaults the column server_default applied.
        row = control_db.execute(
            text(
                "SELECT tenant_id, job_number_format, job_number_next_seq, job_number_year_seen "
                "FROM tenant_settings WHERE tenant_id = :tid FOR UPDATE"
            ),
            {"tid": str(tid)},
        ).first()

    template = row[1] or "JOB-{year}-{seq:03d}"
    next_seq = int(row[2] or 1)
    year_seen = row[3]
    current_year = datetime.now(timezone.utc).year

    # Reset on year boundary if format references year
    if ("{year}" in template or "{yy}" in template) and year_seen and year_seen != current_year:
        next_seq = 1

    # Skip past any number already on a job. Raises NumberingExhausted
    # before the UPDATE, so the counter is left where it was.
    free_seq, rendered = first_free_seq(
        control_db, template, next_seq, year=current_year, customer_name=customer_name
    )
    if free_seq != next_seq:
        log.warning(
            "job_number_counter_behind tenant=%s seq=%s advanced_to=%s", tid, next_seq, free_seq
        )
        next_seq = free_seq

    # Persist incremented counter + last year seen
    control_db.execute(
        text(
            """
            UPDATE tenant_settings
               SET job_number_next_seq = :new_seq,
                   job_number_year_seen = :year
             WHERE tenant_id = :tid
            """
        ),
        {"new_seq": next_seq + 1, "year": current_year, "tid": str(tid)},
    )

    return rendered


def allocate_job_number(tenant_id: UUID | str, *, customer_name: str | None = None) -> str | None:
    """The next job number in a session of its own, committed before the job.

    The FOR UPDATE on tenant_settings is held for the allocation only, and a
    numbering failure leaves the job unnumbered rather than unmade: it logs
    and returns None (GDXA-834).
    """
    from gdx_dispatch.core.database import SessionLocal  # noqa: PLC0415

    try:
        with SessionLocal() as cdb:
            number = next_job_number(cdb, tenant_id, customer_name=customer_name)
            cdb.commit()
            return number
    except Exception:
        log.exception("job_number_allocation_failed tenant=%s", tenant_id)
        return None
