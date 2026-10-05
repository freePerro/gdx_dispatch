"""Planner "Today" tab (the 2026-09-28 planner Today plan).

* ``planner_tasks.today_date`` — the business-local calendar day a task was
  pinned to Today, stored per the planner's D@00:00 UTC convention. A task is
  on Today only while that date *is* today, so the list empties itself each
  morning with nothing scheduled to run.
* ``planner_day_notes`` — one free-text note per user per business day, for
  thoughts that are not tasks yet.

Existing rows: every ``planner_tasks`` row gets ``today_date`` NULL, which
means "not on Today". Nothing is backfilled and no money column changes.

Both tables are ORM-managed (``create_all``), so each step is guarded with
``has_table`` / column checks: on a database where ``create_all`` has not run
yet, ``planner_tasks`` is absent and ``create_all`` later builds it from the
model, column included.

Rollback: ``downgrade()`` drops the column and the table. It loses only the
Today pins and the day notes; the tasks themselves are untouched.

Revision ID: 098_planner_today
Revises: 097_time_off
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "098_planner_today"
down_revision = "097_time_off"
branch_labels = None
depends_on = None

TASKS = "planner_tasks"
COLUMN = "today_date"
NOTES = "planner_day_notes"


def _has_column(bind, table: str, column: str) -> bool | None:
    """True/False, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(table):
        return None
    return any(col["name"] == column for col in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, TASKS, COLUMN) is False:
        op.add_column(TASKS, sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))
    if not inspect(bind).has_table(NOTES):
        op.create_table(
            NOTES,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("company_id", sa.String(36), nullable=False),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("note_date", sa.Date(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("updated_by", sa.String(36), nullable=True),
            sa.Column("updated_via", sa.String(20), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "note_date", name="uq_planner_day_notes_user_date"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, TASKS, COLUMN):
        with op.batch_alter_table(TASKS) as batch:
            batch.drop_column(COLUMN)
    if inspect(bind).has_table(NOTES):
        op.drop_table(NOTES)
