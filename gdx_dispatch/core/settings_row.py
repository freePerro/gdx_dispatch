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

**The tenant id is bound typed, one spelling for every dialect.**
`tenant_settings.tenant_id` is `Uuid`: a native `uuid` on Postgres, 32
dashless hex on an ORM-built SQLite schema (CLAUDE.md, "SQLite stores a Uuid
column as 32 dashless hex"). The seven copies this replaced, and
`core/settings_audit.py`, all bound `str(tenant_id)` — the dashed spelling —
which Postgres casts and SQLite never matches, so on SQLite the read missed,
the seed inserted a SECOND row and every settings page showed defaults
(GDXA-292). `settings_sql()` attaches a `Uuid`-typed `:tid` and
`tenant_id_value()` hands it a `UUID`, so SQLAlchemy renders the column's own
spelling per dialect. The read, the seed and the audited write all go through
them; a raw `str(tenant_id)` bind against this table is the defect, not a
style choice. Not every site is converted yet: the maintainer ruled the fix
narrow (two files), so raw dashed binds remain in `routers/session_policy.py`,
`routers/jobs.py` (`_load_workflow_flags`), `core/settings_flags.py`,
`modules/numbering/service.py`, `modules/payroll/router.py` and five domain
modules — listed in GDXA-292. On SQLite those now address a different row
than this helper; `test_settings_row.py` pins the session-policy write and
the job workflow gates.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Uuid, bindparam, text
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import TextClause

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")



def settings_sql(sql: str) -> TextClause:
    """`text(sql)` with `:tid` typed as the `Uuid` column it is compared to.
    Pair it with `tenant_id_value()` — the type's bind processor wants a
    `UUID`, not a string."""
    return text(sql).bindparams(bindparam("tid", type_=Uuid(as_uuid=True)))


def tenant_id_value(tenant_id: Any) -> UUID:
    """The tenant id as a `UUID`, for a `settings_sql()` `:tid`. Raises
    `ValueError` on a malformed id — the same input Postgres already refuses
    at the cast, instead of a silent miss on SQLite."""
    return tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))


_SEED = settings_sql(
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
    stmt = settings_sql(f"SELECT {cols} FROM tenant_settings WHERE tenant_id = :tid")  # noqa: S608 — every column name passed settings_column(); the tenant id is bound
    params = {"tid": tenant_id_value(tenant_id)}
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
