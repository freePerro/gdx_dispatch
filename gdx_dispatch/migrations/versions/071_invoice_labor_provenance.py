"""Invoice lines record WHERE their labor price came from.

Doug 2026-08-19, asked whether Add Labor should bill the matrix flat price or
the tech's attested hours: "it could be either." Both lanes ship, the operator
picks, and the line has to say which one they picked — otherwise "is this
labor quoted or attested?" is unanswerable after the fact, which is invariant
#1 on a money row.

`estimate_lines` has carried `labor_price_item_id` + `estimated_man_hours`
since S97; `invoice_lines` never did, so the estimate -> invoice copy already
drops that provenance today. This closes the asymmetry as well as serving the
new picker.

`labor_source` is the field that makes the two-lane design auditable:

    matrix    flat price from a LaborPriceItem row (a QUOTED contract price)
    attested  hours the tech signed off, x the loaded labor rate
    manual    someone typed it

That distinction is not cosmetic. Billed labor comes from attested hours only;
a matrix flat price is a contract price and is NOT a claim about hours, which
is why the matrix lane never writes an hours count into the description. The
column is what lets a later reader tell those apart instead of guessing from
prose.

All three columns are NULLABLE with no default, so every existing invoice line
is valid unchanged and reads as "no labor provenance recorded" -- which is the
truth for all of them. No backfill: inferring a matrix row from a historical
description is exactly the guess this column exists to replace.

ON DELETE SET NULL on the FK so archiving a matrix row never breaks a
historical invoice -- the snapshot values on the line stay intact, matching how
`estimate_lines` already behaves.

Existing rows: unchanged, all three read NULL.

Rollback: on Postgres the downgrade drops the constraint, index and all three
columns. On SQLite it is a deliberate NO-OP -- pre-3.35 cannot DROP COLUMN, and
three unread nullable columns are harmless where failing the downgrade is not.
Either way nothing downstream computes money from these: they are provenance
only, and `unit_price` remains the billed number.

Revision ID: 071_invoice_labor_provenance
Revises: 070_customer_local_edit_at
"""
import contextlib

from alembic import op

revision = "071_invoice_labor_provenance"
down_revision = "070_customer_local_edit_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite lane: no IF NOT EXISTS on ADD COLUMN, so suppress the
        # duplicate-column error and let a re-run be a no-op. The FK is
        # declared on the model; SQLite does not enforce it by default and
        # cannot ADD CONSTRAINT after the fact, so the column goes in bare.
        for ddl in (
            "ALTER TABLE invoice_lines ADD COLUMN labor_price_item_id CHAR(32);",
            "ALTER TABLE invoice_lines ADD COLUMN estimated_man_hours NUMERIC(6, 2);",
            "ALTER TABLE invoice_lines ADD COLUMN labor_source VARCHAR(16);",
        ):
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(ddl)
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_invoice_lines_labor_price_item_id "
                "ON invoice_lines (labor_price_item_id);"
            )
        return

    bind.exec_driver_sql(
        """
        ALTER TABLE invoice_lines
            ADD COLUMN IF NOT EXISTS labor_price_item_id uuid,
            ADD COLUMN IF NOT EXISTS estimated_man_hours numeric(6, 2),
            ADD COLUMN IF NOT EXISTS labor_source varchar(16);
        """
    )
    # Postgres has no ADD CONSTRAINT IF NOT EXISTS, and wrapping it in
    # contextlib.suppress does NOT make it idempotent: a failed statement
    # aborts the whole transaction, so the CREATE INDEX below would then fail
    # and take the upgrade with it. Check the catalog instead.
    bind.exec_driver_sql(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_invoice_lines_labor_price_item'
            ) THEN
                ALTER TABLE invoice_lines
                    ADD CONSTRAINT fk_invoice_lines_labor_price_item
                    FOREIGN KEY (labor_price_item_id)
                    REFERENCES labor_price_items (id)
                    ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_invoice_lines_labor_price_item_id "
        "ON invoice_lines (labor_price_item_id);"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite pre-3.35 cannot DROP COLUMN; leaving three unread nullable
        # columns is harmless and beats failing the downgrade.
        return
    bind.exec_driver_sql(
        "DROP INDEX IF EXISTS ix_invoice_lines_labor_price_item_id;"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE invoice_lines
            DROP CONSTRAINT IF EXISTS fk_invoice_lines_labor_price_item,
            DROP COLUMN IF EXISTS labor_price_item_id,
            DROP COLUMN IF EXISTS estimated_man_hours,
            DROP COLUMN IF EXISTS labor_source;
        """
    )
