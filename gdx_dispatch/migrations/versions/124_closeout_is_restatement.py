"""A closeout records whether it restates a still-completed job (GDXA-890).

* ``job_closeouts.is_restatement`` — ``Boolean``, nullable. ``closeout_job``
  sets it True when the job was still completed at the re-closeout, False
  otherwise. ``core/closeout_billing.closeout_hours_billed_before`` reads it to
  offer a restatement's hours net of what the job's earlier closeouts already
  billed: 2 h billed, re-closed at 3 h, the next invoice offers 1 h, not 3 h
  (Doug, 2026-10-13). A closeout after an uncomplete/reopen is new work and
  keeps its full hours.
* ``job_closeouts.man_hours_billed`` — ``Numeric(8, 2)``, nullable. The
  man-hours the claim that stamped ``billed_invoice_id`` actually billed: the
  full final day for a first closeout, only the delta for a restatement. The
  run's billed total is the SUM of these over the rows a live invoice holds,
  so voiding one invoice in the run gives back exactly what it billed.

Existing rows: both columns NULL on every closeout; no backfill, no amount,
line, stamp or status moves. ``is_restatement`` NULL is answered at read time
from the audit trail (``closeout_billing._restates``): a re-closeout restates
the row it supersedes unless a ``job_uncompleted`` audit row falls between the
two. So an existing re-closeout of a still-completed job that is not invoiced
yet now offers only its delta — the GDXA-890 double bill, fixed on rows that
are already in that state; a first closeout, or one after an uncomplete,
bills as before. ``man_hours_billed`` NULL on a stamped row reads as its full
man-hours, which is what every claim before 124 billed. No amount, line, stamp
or status moves; there is no backfill. Not a row-refusing migration: it never
raises on data.

Portability: ``batch_alter_table`` as in 118 (SQLite rebuilds the table and
re-creates ``uq_job_closeouts_live`` from reflection; the migration test checks
it keeps its ``WHERE``). A no-op when ``job_closeouts`` is absent or already
has the column; ``create_all`` runs before alembic on boot.

Rollback: ``downgrade()`` drops both columns, and the older code bills every
closeout's full hours again, as before 124.

Revision ID: 124_closeout_is_restatement
Revises: 123_audit_chain_serialized
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "124_closeout_is_restatement"
down_revision = "123_audit_chain_serialized"
branch_labels = None
depends_on = None

_TABLE = "job_closeouts"
_COLUMNS = (
    ("is_restatement", sa.Boolean()),
    ("man_hours_billed", sa.Numeric(8, 2)),
)


def _columns(bind, table: str) -> set[str] | None:
    """The table's column names, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(table):
        return None
    return {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    cols = _columns(op.get_bind(), _TABLE)
    if cols is None:
        return
    missing = [(name, type_) for name, type_ in _COLUMNS if name not in cols]
    if not missing:
        return
    with op.batch_alter_table(_TABLE) as batch:
        for name, type_ in missing:
            batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    cols = _columns(op.get_bind(), _TABLE)
    if cols is None:
        return
    present = [name for name, _ in _COLUMNS if name in cols]
    if not present:
        return
    with op.batch_alter_table(_TABLE) as batch:
        for name in present:
            batch.drop_column(name)
