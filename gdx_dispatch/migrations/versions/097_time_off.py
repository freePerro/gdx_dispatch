"""Time off and holiday pay (the 2026-09-23 plan of that name).

* ``time_off_requests`` — a tech's request for paid days off and the office's
  ruling on it. The timeclock table holds only APPROVED paid time (as closed
  ``vacation`` / ``holiday`` entries); the workflow lives here so no hours
  reader ever has to know about a pending day.
* ``app_settings.holiday_calendar`` — JSON list of ``{date, name, minutes}``
  the office posts holiday pay from. Ships ``[]``.
* ``app_settings.time_off_default_minutes`` — what a day off is worth when
  nobody says otherwise. Ships 480 (eight hours).
* ``app_settings.time_off_counts_toward_overtime`` — per-company option the
  pay-period export states on every time-off row. Ships ``false`` (Doug,
  2026-09-23: for this shop it does not count).

No timeclock row is touched and no money column changes. Guarded with
``has_table`` / column checks because every table here is ORM-created: on a
database where ``create_all`` has not run yet they are absent, and
``create_all`` then builds them from the model.

Rollback: ``downgrade()`` drops the three columns and the table. Timeclock
entries an approval created stay in place — they are ordinary closed entries
— so a downgrade loses only the request history and the calendar.

Revision ID: 097_time_off
Revises: 096_card_surcharge_columns
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "097_time_off"
down_revision = "096_card_surcharge_columns"
branch_labels = None
depends_on = None

TABLE = "time_off_requests"

_SETTINGS_COLUMNS = (
    ("holiday_calendar", sa.JSON(), sa.text("'[]'")),
    ("time_off_default_minutes", sa.SmallInteger(), sa.text("480")),
    ("time_off_counts_toward_overtime", sa.Boolean(), sa.text("false")),
)


def _has_column(bind, table: str, column: str) -> bool | None:
    """True/False, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(table):
        return None
    return any(col["name"] == column for col in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if not insp.has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("company_id", sa.String(36), nullable=False),
            sa.Column("technician_id", sa.String(36), nullable=False, index=True),
            sa.Column("entry_type", sa.String(20), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("minutes_per_day", sa.Integer(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, index=True),
            sa.Column("requested_by", sa.String(36), nullable=False),
            sa.Column("reviewed_by", sa.String(36), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("review_note", sa.Text(), nullable=True),
            sa.Column("entry_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
    for column, type_, default in _SETTINGS_COLUMNS:
        present = _has_column(bind, "app_settings", column)
        if present is None or present:
            continue
        op.add_column(
            "app_settings",
            sa.Column(column, type_, nullable=False, server_default=default),
        )


def downgrade() -> None:
    bind = op.get_bind()
    for column, _type, _default in _SETTINGS_COLUMNS:
        if not _has_column(bind, "app_settings", column):
            continue
        with op.batch_alter_table("app_settings") as batch:
            batch.drop_column(column)
    if inspect(bind).has_table(TABLE):
        op.drop_table(TABLE)
