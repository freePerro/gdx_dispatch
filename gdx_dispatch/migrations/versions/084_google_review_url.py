"""app_settings gains the company's public "leave us a review" link.

One additive column, default '' (blank = no link, nothing rendered). The
shell in core/email_layout.render_email prints it as a footer line on every
customer email — invoices, receipts, estimates, reminders, portal invites,
automation and plugin mail. Staff mail (payroll timesheets, password resets)
opts out. Nothing is backfilled: no install had a review link before, and a
default of THIS shop's Google place would ship our review page to every
other install as a side effect of a schema change.

Rollback: `downgrade()` drops the column. It holds one URL of configuration,
re-enterable from Settings → Branding in ten seconds; no records, no audit
trail depend on it.

Revision ID: 084_google_review_url
Revises: 083_invoice_line_pricing_source
"""
import contextlib

from alembic import op

revision = "084_google_review_url"
down_revision = "083_invoice_line_pricing_source"
branch_labels = None
depends_on = None

# (name, type, default-clause). One list, two dialect renderings, so the
# SQLite and Postgres arms cannot drift apart (same shape as 081).
_COLUMNS = [
    ("google_review_url", "varchar(500)", "DEFAULT '' NOT NULL"),
]


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name != "postgresql":
        # SQLite has no ADD COLUMN IF NOT EXISTS. suppress() so a stamped
        # rerun (column already present) completes instead of aborting.
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
        for name, _coltype, _default in reversed(_COLUMNS):
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(f"ALTER TABLE app_settings DROP COLUMN {name};")
        return

    for name, _coltype, _default in reversed(_COLUMNS):
        bind.exec_driver_sql(
            f"ALTER TABLE app_settings DROP COLUMN IF EXISTS {name};"
        )
