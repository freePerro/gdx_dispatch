"""tenant_settings gains tenant-editable invoice + receipt email templates.

Issue #351 / email-overhaul tech-debt row 6: the estimate email subject and
body have been tenant-editable (``estimate_email_subject_template`` /
``estimate_email_body_template``) since the email overhaul, while the invoice
and receipt copy stayed hardcoded consts in ``routers/invoices.py``. These
four columns make all three document types editable the same way.

Four nullable TEXT columns, no default — the same shape as the estimate
pair. NULL or blank means "use the platform default" (the consts in
``routers/invoices.py``), so an upgraded install keeps sending exactly the
copy it sent yesterday until someone types into Settings → Estimates.
Nothing is backfilled: writing the default text into the column would freeze
today's wording per install and hide future default improvements.

``tenant_settings`` is a baseline (001) table, not a boot-created one, so the
Postgres arm is a plain ``ADD COLUMN IF NOT EXISTS`` (046 shape) with no
``to_regclass`` guard — if the table is missing, this should fail loudly, not
skip. SQLite has no ``IF NOT EXISTS`` on ADD COLUMN, so that arm suppresses
"duplicate column" the way 067/081/084 do, which keeps a stamped rerun from
aborting halfway.

Rollback: ``downgrade()`` drops the four columns. They hold configuration
text re-enterable from Settings; no records or audit chains depend on them.

Revision ID: 086_invoice_receipt_templates
Revises: 085_drop_review_requests
"""
import contextlib

from alembic import op

revision = "086_invoice_receipt_templates"
down_revision = "085_drop_review_requests"
branch_labels = None
depends_on = None

_TABLE = "tenant_settings"

# One list, two dialect renderings, so the SQLite and Postgres arms cannot
# drift apart (same shape as 081/084). All nullable TEXT, no default —
# mirrors estimate_email_subject_template / estimate_email_body_template.
_COLUMNS = [
    "invoice_email_subject_template",
    "invoice_email_body_template",
    "receipt_email_subject_template",
    "receipt_email_body_template",
]


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name != "postgresql":
        # SQLite has no ADD COLUMN IF NOT EXISTS. suppress() so a stamped
        # rerun (column already present) completes instead of aborting.
        for name in _COLUMNS:
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(f"ALTER TABLE {_TABLE} ADD COLUMN {name} TEXT;")
        return

    for name in _COLUMNS:
        bind.exec_driver_sql(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} TEXT;"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        for name in reversed(_COLUMNS):
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(f"ALTER TABLE {_TABLE} DROP COLUMN {name};")
        return

    for name in reversed(_COLUMNS):
        bind.exec_driver_sql(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name};")
