"""A planner task can carry a reminder (the GDX Planner app, 2026-10-14).

* ``planner_tasks.remind_at`` — ``DateTime(timezone=True) NULL``, the instant
  the reminder is due. ``due_date`` stays a calendar date; this is a time.
* ``planner_tasks.remind_user_id`` — ``String(36) NULL``, the user who set the
  reminder and the one it notifies.
* ``planner_tasks.reminder_sent_at`` — ``DateTime(timezone=True) NULL``,
  stamped by ``tasks/planner_reminders.py`` in the same transaction as the
  bell row it writes, so a reminder is sent once.

Existing rows: every ``planner_tasks`` row reads NULL in all three, which
means "no reminder". Nothing is backfilled and no other table is read or
written.

Portability: plain ``op.add_column`` of nullable columns with no default,
which both engines take without a table rebuild. Each column is skipped when
the table is absent or already has it (``create_all`` builds them from the
model on a fresh database but never adds a column to an existing table).

Rollback: ``downgrade()`` drops the three columns (``batch_alter_table`` so
SQLite rebuilds the table). Every reminder time is lost; bell rows already
written stay. Code that reads the columns must be rolled back first.

Revision ID: 125_planner_task_reminders
Revises: 124_change_order_tax_rate
Create Date: 2026-10-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "125_planner_task_reminders"
down_revision = "124_change_order_tax_rate"
branch_labels = None
depends_on = None

_TABLE = "planner_tasks"
_COLUMNS = (
    ("remind_at", lambda: sa.DateTime(timezone=True)),
    ("remind_user_id", lambda: sa.String(36)),
    ("reminder_sent_at", lambda: sa.DateTime(timezone=True)),
)


def _columns(bind) -> set[str] | None:
    """The table's column names, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(_TABLE):
        return None
    return {col["name"] for col in insp.get_columns(_TABLE)}


def upgrade() -> None:
    cols = _columns(op.get_bind())
    if cols is None:
        return
    for name, type_ in _COLUMNS:
        if name not in cols:
            op.add_column(_TABLE, sa.Column(name, type_(), nullable=True))


def downgrade() -> None:
    cols = _columns(op.get_bind())
    if cols is None:
        return
    present = [name for name, _ in _COLUMNS if name in cols]
    if not present:
        return
    with op.batch_alter_table(_TABLE) as batch:
        for name in present:
            batch.drop_column(name)
