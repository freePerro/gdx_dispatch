"""app_settings learns the shop's payroll calendar.

Six additive columns, all with safe defaults, no backfill of anyone's data:
there is nothing to convert because no pay-period concept existed before
(`GET /api/payroll/pay-periods` was a stub returning []).

Defaults are deliberately NEUTRAL, not this shop's values. `weekly_mon` is
what both timesheet screens already assumed, so an install that upgrades
and never opens the setting sees exactly the view it saw yesterday.
`payroll_autosend_enabled` is false for the same reason a send that nobody
configured must not start mailing hours to an address nobody set.

Rollback: `downgrade()` drops all six. That is safe — they hold
configuration, not records. Losing them un-configures pay periods (the
presets fall back to plain weeks and the scheduled send stops); it destroys
no hours, no timesheet and no audit trail, all of which live in
timeclock_entries_router and audit_logs and are untouched here.

Revision ID: 081_pay_period_settings
Revises: 080_adjustment_tax_component
"""
import contextlib

from alembic import op

revision = "081_pay_period_settings"
down_revision = "080_adjustment_tax_component"
branch_labels = None
depends_on = None

# (name, type, default-clause). One list, two dialect renderings, so the
# SQLite and Postgres arms cannot drift apart.
_COLUMNS = [
    ("pay_period_cadence", "varchar(20)", "DEFAULT 'weekly_mon' NOT NULL"),
    ("pay_period_anchor_start", "date", ""),
    ("pay_period_pay_lag_days", "smallint", "DEFAULT 0 NOT NULL"),
    ("payroll_recipient_emails", "text", "DEFAULT '' NOT NULL"),
    ("payroll_autosend_enabled", "boolean", "DEFAULT false NOT NULL"),
    ("payroll_autosend_hour", "smallint", "DEFAULT 7 NOT NULL"),
]


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name != "postgresql":
        # SQLite has no ADD COLUMN IF NOT EXISTS. suppress() per column so a
        # partially-applied state (a column already present from a stamped
        # rerun) completes the rest instead of aborting the whole migration.
        for name, coltype, default in _COLUMNS:
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(
                    f"ALTER TABLE app_settings ADD COLUMN {name} {coltype} {default};".strip()
                )
        return

    for name, coltype, default in _COLUMNS:
        bind.exec_driver_sql(
            f"""
            DO $$ BEGIN
                IF to_regclass('app_settings') IS NOT NULL THEN
                    ALTER TABLE app_settings
                        ADD COLUMN IF NOT EXISTS {name} {coltype} {default};
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Modern SQLite supports DROP COLUMN; older builds do not, and an
        # un-droppable settings column is harmless dead weight either way.
        for name, _coltype, _default in reversed(_COLUMNS):
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(f"ALTER TABLE app_settings DROP COLUMN {name};")
        return

    for name, _coltype, _default in reversed(_COLUMNS):
        bind.exec_driver_sql(
            f"ALTER TABLE app_settings DROP COLUMN IF EXISTS {name};"
        )
