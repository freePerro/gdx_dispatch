"""Numbering service — atomic next-number generation.

Locks the TenantSettings row (control plane) via SELECT ... FOR UPDATE
so two concurrent jobs creating in parallel can't grab the same seq.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import TenantSettings


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
