"""Check/deposit-ticket scans paired to bank statement evidence lines.

One ORM-managed, brand-new table — per the 040/050 pattern,
`create_orm_tables()` builds it before alembic runs, so raw CREATE TABLE
IF NOT EXISTS; re-running is a no-op.

The statements' trailing images page carries one scanned document per
caption (deposit tickets and written checks). Pairing to evidence lines
happens at import time via the caption's amount + full date + check
number; an unpairable scan keeps line_id NULL and lives in the import's
gallery. Rows (and files) are deleted when their import is voided.

Revision ID: 051_bank_statement_images
Revises: 050_bank_statement_evidence
"""
from alembic import op

revision = "051_bank_statement_images"
down_revision = "050_bank_statement_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS bank_statement_line_images (
            id                    uuid PRIMARY KEY,
            import_id             uuid NOT NULL REFERENCES bank_statement_imports(id),
            line_id               uuid REFERENCES bank_statement_lines(id),
            storage_path          text NOT NULL,
            caption_check_no      varchar(20),
            caption_amount_cents  bigint,
            caption_date          date,
            sort_order            integer NOT NULL DEFAULT 0,
            created_at            timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_bank_statement_line_images_import_id "
        "ON bank_statement_line_images (import_id);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_bank_statement_line_images_line_id "
        "ON bank_statement_line_images (line_id);"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS bank_statement_line_images;")
