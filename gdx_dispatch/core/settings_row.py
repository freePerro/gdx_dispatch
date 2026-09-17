"""Read the tenant's `tenant_settings` row, seeding it on first read.

Seven routers carried this same function — SELECT the columns they own, INSERT
a default row `ON CONFLICT DO NOTHING` when there is none, COMMIT, SELECT
again — copied from one template in the 2026-06-18 initial release
(`routers/session_policy.py` said outright that it "mirrors dispatch_settings").
Each copy interpolated its column list into the SELECT, so each carried a
`# noqa: S608` on the strength of a comment. This is the one copy, and its
column gate is a refusal, not a comment.

**The commit here is load-bearing for `core/settings_audit.py`.** That helper
stages an audited upsert and a re-read inside ONE transaction and documents
that the guarantee survives only because the reader's commit happens
exclusively on the create branch — before anything is staged — and never
otherwise. Keep it that way: no commit on the found-row path, none after a
write. `test_settings_row.py` counts the commits.

Why raw SQL and not `TenantSettings` (`core/tenant_settings.py`): the model
lacks eight columns the live table has (prod `information_schema`, read
2026-09-16): `estimate_email_subject_template`, `estimate_email_body_template`
and `estimates_default_terms` from the 001 baseline, `estimate_expiry_days`
from migration 046, and the four invoice/receipt email templates from 086. A
column list is what actually matches the table; reconciling the model is
separate work.

**Known sharp edge, carried over unchanged from the seven copies:** the tenant
id is bound as `str(tenant_id)`, the dashed spelling, exactly as every copy and
`core/settings_audit.py` bind it. Postgres (prod) accepts that against a
`uuid` column. On an ORM-built SQLite schema `Uuid` is stored as 32 dashless
hex, so the dashed bind matches nothing, the seed inserts a SECOND row, and the
read returns defaults (CLAUDE.md, "SQLite stores a Uuid column as 32 dashless
hex"). Fixing it means binding the same spelling on the read and the audited
write together, plus the hand-built test tables that store the dashed form —
not a change to smuggle into an extraction. `test_settings_row.py` pins the
defect with a strict xfail so the fix cannot land without retiring the pin.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_SEED = text(
    "INSERT INTO tenant_settings (tenant_id) VALUES (:tid) "
    "ON CONFLICT (tenant_id) DO NOTHING"
)


def settings_column(name: str) -> str:
    """The gate between a caller's column name and the SELECT it is spliced
    into. Callers pass module-constant tuples, never request data — but the
    interpolation below carries `# noqa: S608` on the strength of THIS check,
    so it has to refuse, not trust (same shape as `routers/reports.py::
    _alias_prefix` and `routers/customers.py::_sql_identifier`)."""
    # fullmatch, not match: `$` accepts a trailing newline.
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"unsafe tenant_settings column name: {name!r}")
    return name


def read_settings_row(db: Session, tenant_id: Any, columns: Sequence[str]) -> Row[Any]:
    """SELECT `columns` from the tenant's settings row, creating the row with
    defaults if it does not exist yet. Returns the row positionally, in the
    order of `columns`; the caller owns the mapping to its API shape.

    Commits ONLY when it had to seed the row — see the module docstring.
    """
    cols = ", ".join(settings_column(c) for c in columns)
    stmt = text(f"SELECT {cols} FROM tenant_settings WHERE tenant_id = :tid")  # noqa: S608 — every column name passed settings_column(); the tenant id is bound
    params = {"tid": str(tenant_id)}
    row = db.execute(stmt, params).first()
    if row is None:
        # Create-on-read so every settings page shows a usable default the
        # first time it is opened. The commit is deliberate and confined to
        # this branch (settings_audit.py depends on that).
        db.execute(_SEED, params)
        db.commit()
        row = db.execute(stmt, params).first()
    if row is None:
        # Unreachable — the seed guarantees the row — but a None here would
        # surface as an IndexError in the caller's mapping, which reads as a
        # bug in the caller rather than a failed seed.
        raise RuntimeError("tenant_settings seed failed")
    return row
