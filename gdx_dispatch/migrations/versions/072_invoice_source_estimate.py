"""Where an office-built invoice's numbers came from, without claiming it IS
that estimate's bill.

`/billing/new` prefills its line editor from an accepted estimate and then lets
the operator edit those lines. It could not record the link, because the only
field available -- `invoices.estimate_id` -- means something stronger.

The first attempt reused that column anyway. It is wrong, and the way it is
wrong is expensive:

* `modules/deposits/service.py` matches deposits on
  ``or_(job_id == X, estimate_id == E)``. On office invoices `estimate_id` was
  effectively always NULL (5 of 340 rows), so the second arm was dead in
  practice. Populating it on the majority path ARMS that arm: a paid deposit
  sitting on a DIFFERENT job that shares the estimate gets netted into this
  invoice. Reproduced in review -- a $2,000 invoice came out at $1,500 with a
  "Less deposit paid" line for another job's money. The double-application
  guard next to it is job-scoped and does not cover the estimate arm.
* `core/closeout_reconciliation.py` skips invoices with an `estimate_id`,
  commenting "estimate-billed = agreed price, not a discrepancy". Reusing the
  column would have silently dropped most office invoices out of the
  revised-closeout discrepancy list -- and the premise would be false anyway,
  since the prefilled path lets the operator edit every line.

So provenance gets its own column. `estimate_id` keeps meaning "the server
copied this estimate's lines"; `source_estimate_id` means "the numbers started
from this estimate, and a human may have changed them since". Both are true
statements, they are not the same statement, and only the first one should move
money.

Nullable, no default, no backfill: an invoice created before this cannot know
where its numbers came from, and guessing would invent the provenance the
column exists to record.

Existing rows: unchanged, all read NULL. No behaviour changes for any existing
query -- nothing reads this column until the code that writes it also ships.
Rollback: drop the column and its index; deposit netting, reconciliation and
the detail-page chip all keep working off `estimate_id` exactly as before.

Revision ID: 072_invoice_source_estimate
Revises: 071_invoice_labor_provenance
"""
import contextlib

from alembic import op

revision = "072_invoice_source_estimate"
down_revision = "071_invoice_labor_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite lane: no IF NOT EXISTS on ADD COLUMN, so suppress the
        # duplicate-column error and let a re-run be a no-op.
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "ALTER TABLE invoices ADD COLUMN source_estimate_id CHAR(32);"
            )
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_invoices_source_estimate_id "
                "ON invoices (source_estimate_id);"
            )
        return

    bind.exec_driver_sql(
        """
        ALTER TABLE invoices
            ADD COLUMN IF NOT EXISTS source_estimate_id uuid;
        """
    )
    # No FOREIGN KEY, deliberately: `invoices.estimate_id` -- the column this
    # one sits beside and mirrors -- has never had one either. Estimates live
    # in another module's table and this plane does not constrain that
    # reference. Adding a constraint here alone would make the pair
    # inconsistent and could fail on tenants with historical orphan rows.
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_invoices_source_estimate_id "
        "ON invoices (source_estimate_id);"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite pre-3.35 cannot DROP COLUMN; an unread nullable column is
        # harmless and beats failing the downgrade.
        return
    bind.exec_driver_sql("DROP INDEX IF EXISTS ix_invoices_source_estimate_id;")
    bind.exec_driver_sql(
        """
        ALTER TABLE invoices
            DROP COLUMN IF EXISTS source_estimate_id;
        """
    )
