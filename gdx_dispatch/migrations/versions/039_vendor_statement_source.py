"""Vendor statements — record which door the statement came through

Statements can now arrive two ways: the office dropping a PDF on the Vendor
Statements page ('upload'), or the Outlook vendor-bill ingest pulling one off
an allowlisted supplier sender ('email'). Without this column the statements
list can't answer "is the automation actually working?", which is the whole
reason the email rung was built.

``vendor_statements`` is ORM-created (not in the squashed baseline), so on a
fresh DB create_orm_tables() already builds the column and this migration is a
guarded no-op. It only does real work on a DB where vendor_statements already
exists without the column — which is every deployed environment, and where a
missing column would 500 the page on the first list query.

Revision ID: 039_vendor_statement_source
Revises: 038_expense_status
"""
from alembic import op

revision = "039_vendor_statement_source"
down_revision = "038_expense_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.vendor_statements') IS NOT NULL THEN
            ALTER TABLE vendor_statements
              ADD COLUMN IF NOT EXISTS source varchar(20) NOT NULL DEFAULT 'upload';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.vendor_statements') IS NOT NULL THEN
            ALTER TABLE vendor_statements DROP COLUMN IF EXISTS source;
          END IF;
        END $$;
        """
    )
