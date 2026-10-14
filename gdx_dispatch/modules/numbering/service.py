"""Numbering service — atomic next-number generation.

Locks the TenantSettings row via SELECT ... FOR UPDATE
so two concurrent jobs creating in parallel can't grab the same seq.
"""
from __future__ import annotations

import logging
import re
import string
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.tenant_settings import TenantSettings

log = logging.getLogger(__name__)

_INITIALS_RE = re.compile(r"[A-Za-z]+")

# How far next_job_number walks past numbers already issued before giving up and
# letting ux_jobs_job_number_live refuse the insert. A counter this far behind
# is a restore or import gone wrong, not drift.
_MAX_SKIP = 10_000


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


def _has_jobs_table(db: Session) -> bool:
    return inspect(db.connection()).has_table("jobs")


def _is_issued(db: Session, number: str) -> bool:
    """True when any job, live or soft-deleted, already carries ``number``.

    Wider than ux_jobs_job_number_live on purpose: a deleted job's invoices and
    payments still quote its number, so it is never handed out again (the rule
    migration 119 follows).
    """
    return (
        db.execute(
            text("SELECT 1 FROM jobs WHERE job_number = :n LIMIT 1"),
            {"n": number},
        ).first()
        is not None
    )


def _template_pattern(template: str, year: int) -> re.Pattern[str] | None:
    """A regex matching the numbers ``template`` renders in ``year``, ``{seq}`` captured.

    None when the template has no ``{seq}`` or uses a token apply_template does not
    know (it then renders the ``JOB-{seq:03d}`` fallback, which this does not chase).
    """
    parts: list[str] = []
    has_seq = False
    try:
        for literal, field, spec, _conv in string.Formatter().parse(template):
            parts.append(re.escape(literal))
            if field is None:
                continue
            if field == "seq":
                parts.append(r"\d+" if has_seq else r"(\d+)")
                has_seq = True
            elif field == "year":
                parts.append(re.escape(format(year, spec or "")))
            elif field == "yy":
                parts.append(re.escape(format(f"{year % 100:02d}", spec or "")))
            elif field == "month":
                parts.append(r"\d{2}")
            elif field == "customer_initials":
                parts.append(r"[A-Z]*")
            else:
                return None
    except (ValueError, TypeError):
        return None
    return re.compile("^" + "".join(parts) + "$") if has_seq else None


def highest_issued_seq(db: Session, template: str, *, year: int | None = None) -> int:
    """The highest ``{seq}`` among job numbers, live or deleted, ``template`` renders this year.

    0 when none match, when the template cannot be matched, or when ``jobs`` is absent.
    Reads every number: it serves the settings save, not job creation.
    """
    y = year if year is not None else datetime.now(timezone.utc).year
    pattern = _template_pattern(template, y)
    if pattern is None or not _has_jobs_table(db):
        return 0
    rows = db.execute(
        text("SELECT job_number FROM jobs WHERE job_number IS NOT NULL")
    ).scalars()
    best = 0
    for number in rows:
        m = pattern.match(number)
        if m:
            best = max(best, int(m.group(1)))
    return best


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

    rendered = apply_template(template, next_seq, year=current_year, customer_name=customer_name)

    # The counter is not the only record of what has been issued: a year reset,
    # a restore, an import or a settings rewind can leave it behind live jobs,
    # and then the Job insert hits ux_jobs_job_number_live after this number is
    # already committed (GDXA-933). Walk past every number already issued while
    # the row lock is held, so the counter heals itself.
    if _has_jobs_table(control_db):
        skipped = 0
        while _is_issued(control_db, rendered) and skipped < _MAX_SKIP:
            candidate = apply_template(template, next_seq + 1, year=current_year, customer_name=customer_name)
            if candidate == rendered:
                break  # the template does not vary with {seq}; the index will refuse it
            next_seq += 1
            skipped += 1
            rendered = candidate
        if skipped:
            log.warning(
                "job_number_counter_behind tenant=%s skipped=%d issued=%s", tid, skipped, rendered
            )

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
