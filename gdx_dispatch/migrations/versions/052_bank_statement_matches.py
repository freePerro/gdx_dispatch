"""Bank statement match tables — the GL Phase 2 §3.2 three-table shape.

bank_matches (parent) + bank_match_lines (statement side, 1:N) +
bank_match_externals (books side, 1:N) — a batched deposit slip is one
statement line matched to MANY per-invoice payments, which a single
line·matched_id row cannot hold (the plan-level audit's foundational
finding, docs/design/bank-statement-import-plan.md §11.1).

Exclusivity via partial unique indexes on the children's denormalized
match_status (kept in sync by the service, single-writer): a line or a
books record sits in at most one non-rejected match.

ORM-managed and brand new — per the 040/050/051 pattern,
create_orm_tables() has already built these when this runs; raw CREATE
TABLE IF NOT EXISTS, idempotent.

Revision ID: 052_bank_statement_matches
Revises: 051_bank_statement_images
"""
from alembic import op

revision = "052_bank_statement_matches"
down_revision = "051_bank_statement_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS bank_matches (
            id                uuid PRIMARY KEY,
            bank_account_id   uuid NOT NULL REFERENCES bank_accounts(id),
            rule              varchar(10) NOT NULL,
            status            varchar(12) NOT NULL DEFAULT 'suggested',
            classification    varchar(20),
            confidence        numeric(3,2),
            note              text,
            created_by        varchar(64),
            confirmed_by      varchar(64),
            confirmed_at      timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS bank_match_lines (
            id            uuid PRIMARY KEY,
            match_id      uuid NOT NULL REFERENCES bank_matches(id),
            line_id       uuid NOT NULL REFERENCES bank_statement_lines(id),
            match_status  varchar(12) NOT NULL DEFAULT 'suggested',
            created_at    timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS bank_match_externals (
            id            uuid PRIMARY KEY,
            match_id      uuid NOT NULL REFERENCES bank_matches(id),
            source_table  varchar(30) NOT NULL,
            source_id     uuid NOT NULL,
            match_status  varchar(12) NOT NULL DEFAULT 'suggested',
            created_at    timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_bank_matches_bank_account_id ON bank_matches (bank_account_id);",
        "CREATE INDEX IF NOT EXISTS ix_bank_matches_status ON bank_matches (status);",
        "CREATE INDEX IF NOT EXISTS ix_bank_match_lines_match_id ON bank_match_lines (match_id);",
        "CREATE INDEX IF NOT EXISTS ix_bank_match_lines_line_id ON bank_match_lines (line_id);",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_match_line_active "
        "ON bank_match_lines (line_id) WHERE match_status <> 'rejected';",
        "CREATE INDEX IF NOT EXISTS ix_bank_match_externals_match_id ON bank_match_externals (match_id);",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_match_external_active "
        "ON bank_match_externals (source_table, source_id) WHERE match_status <> 'rejected';",
    ):
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS bank_match_externals;")
    bind.exec_driver_sql("DROP TABLE IF EXISTS bank_match_lines;")
    bind.exec_driver_sql("DROP TABLE IF EXISTS bank_matches;")
