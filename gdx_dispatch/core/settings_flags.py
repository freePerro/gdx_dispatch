"""Small resilient readers for company-wide tenant_settings toggles.

Separate from routers/jobs._load_workflow_flags (which reads the job-completion
gate columns) so billing/closeout code can check a flag without importing the
jobs router. Every reader defaults to the SAFE value (feature off) on any read
error — a missing column on a not-yet-migrated DB must never crash the caller.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


def qb_money_pull_paused(tenant_id: str, db=None) -> bool:
    """Is the QB→GDX money back-flow (invoice/payment pulls) paused for the
    QB phase-out?

    FAIL-CLOSED, unlike the feature-flag readers below: on an unexpected
    read error this returns True (paused). This is a data-protection
    switch, not a feature gate — a wrongly-blocked sync is a retry; a pull
    that runs while the flag is unreadable can overwrite GDX-corrected
    payment dates and mint duplicate payment rows, which no retry undoes.

    Two deliberate exceptions stay OPEN:
    - no settings row → False (flag never set);
    - table/column missing (schema predates migration 049) → False: a
      schema that cannot STORE the flag cannot have it ON, and entrypoint
      runs `alembic upgrade head` before boot anyway.

    Pass the caller's session via `db` when one exists (the sync gate
    does) — single-company system, one DB; a second engine connection
    buys nothing.
    """
    stmt = text(
        "SELECT qb_money_pull_paused FROM tenant_settings WHERE tenant_id = :tid"
    )
    try:
        if db is not None:
            row = db.execute(stmt, {"tid": tenant_id}).first()
        else:
            with SessionLocal() as _db:
                row = _db.execute(stmt, {"tid": tenant_id}).first()
        return bool(row[0]) if row else False
    except Exception as exc:
        if _is_missing_schema_error(exc):
            return False  # pre-049 schema — the flag can't be ON in it
        log.exception("qb_money_pull_pause_read_failed", extra={"tenant_id": tenant_id})
        return True


def _is_missing_schema_error(exc: Exception) -> bool:
    """True only for 'the table/column does not exist' — the one error class
    that proves the pause flag CAN'T be set. Exact PG sqlstate match
    (42P01 UndefinedTable / 42703 UndefinedColumn); SQLite has no error
    codes, so its two fixed message forms are matched by string. Anything
    else is a genuine read failure and must NOT read as open."""
    pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
    if pgcode in ("42P01", "42703"):
        return True
    msg = str(exc).lower()
    return "no such table" in msg or "no such column" in msg


def closeout_reconciliation_enabled(tenant_id: str) -> bool:
    """Is the §12 closeout→billing reconciliation feature turned on for this
    tenant? Default False (feature off) on any error."""
    try:
        with SessionLocal() as db:
            row = db.execute(
                text(
                    "SELECT closeout_billing_reconciliation "
                    "FROM tenant_settings WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            ).first()
            return bool(row[0]) if row else False
    except Exception:
        log.exception("closeout_reconciliation_flag_read_failed", extra={"tenant_id": tenant_id})
        return False
