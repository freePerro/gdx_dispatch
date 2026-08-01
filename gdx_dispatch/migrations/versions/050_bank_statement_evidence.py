"""Bank statement evidence layer — the GL Phase 2 reserved tables.

Four ORM-managed, brand-new tables (bank_accounts, bank_statement_imports,
bank_statement_lines, bank_statement_line_sources) — so, per the 040
pattern, `create_orm_tables()` has already built them by the time this
runs, and plain op.create_table() would crash-loop the container. Raw
CREATE TABLE IF NOT EXISTS throughout; re-running is a no-op.

Unlike the Banno bank_feeds tables (deliberately migration-less), this
module gets a migration on purpose: future column adds on evidence tables
must not depend on create_all, which never alters an existing table.

The bank_accounts.gl_account_id FK is added CONDITIONALLY: gl_accounts is
ORM-managed and absent from baseline_squashed.sql, so an alembic-only
database (the CI baseline check) doesn't have it. In every deployed
environment create_orm_tables builds gl_accounts (ledger models are
registered in models/__init__.py) before alembic runs, so the constraint
lands there via the DO-block; the ORM metadata always carries it.

Revision ID: 050_bank_statement_evidence
Revises: 049_qb_money_pull_pause
"""
from alembic import op

revision = "050_bank_statement_evidence"
down_revision = "049_qb_money_pull_pause"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS bank_accounts (
            id                        uuid PRIMARY KEY,
            name                      varchar(120) NOT NULL,
            kind                      varchar(20)  NOT NULL DEFAULT 'checking',
            institution               varchar(120) NOT NULL,
            last4                     varchar(4)   NOT NULL,
            statement_import_format   varchar(30)  NOT NULL DEFAULT 'pdf_community_bank',
            gl_account_id             uuid,
            created_at                timestamptz  NOT NULL DEFAULT now(),
            updated_at                timestamptz  NOT NULL DEFAULT now(),
            CONSTRAINT uq_bank_accounts_inst_last4 UNIQUE (institution, last4)
        );
        """
    )
    bind.exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.gl_accounts') IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM pg_constraint
                             WHERE conname = 'fk_bank_accounts_gl_account') THEN
            ALTER TABLE bank_accounts
              ADD CONSTRAINT fk_bank_accounts_gl_account
              FOREIGN KEY (gl_account_id) REFERENCES gl_accounts(id);
          END IF;
        END $$;
        """
    )
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS bank_statement_imports (
            id                        uuid PRIMARY KEY,
            bank_account_id           uuid NOT NULL REFERENCES bank_accounts(id),
            file_sha256               varchar(64) NOT NULL,
            original_filename         varchar(255),
            storage_path              text,
            period_start              date NOT NULL,
            period_end                date NOT NULL,
            beginning_balance_cents   bigint NOT NULL,
            ending_balance_cents      bigint NOT NULL,
            deposits_count            integer,
            deposits_total_cents      bigint NOT NULL DEFAULT 0,
            debits_count              integer,
            debits_total_cents        bigint NOT NULL DEFAULT 0,
            service_charge_cents      bigint NOT NULL DEFAULT 0,
            interest_cents            bigint NOT NULL DEFAULT 0,
            tie_out_status            varchar(16) NOT NULL,
            tie_out_report            json,
            lines_added               integer NOT NULL DEFAULT 0,
            lines_deduped             integer NOT NULL DEFAULT 0,
            voided_at                 timestamptz,
            imported_by               varchar(64),
            created_at                timestamptz NOT NULL DEFAULT now(),
            updated_at                timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS bank_statement_lines (
            id                uuid PRIMARY KEY,
            bank_account_id   uuid NOT NULL REFERENCES bank_accounts(id),
            import_id         uuid NOT NULL REFERENCES bank_statement_imports(id),
            txn_date          date NOT NULL,
            amount_cents      bigint NOT NULL,
            description       text NOT NULL,
            section           varchar(20) NOT NULL,
            check_number      varchar(20),
            occurrence_n      integer NOT NULL DEFAULT 1,
            line_hash         varchar(64) NOT NULL,
            created_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_bank_stmt_line_hash UNIQUE (bank_account_id, line_hash)
        );
        """
    )
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS bank_statement_line_sources (
            id          uuid PRIMARY KEY,
            line_id     uuid NOT NULL REFERENCES bank_statement_lines(id),
            import_id   uuid NOT NULL REFERENCES bank_statement_imports(id),
            created_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_bank_stmt_line_source UNIQUE (line_id, import_id)
        );
        """
    )

    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_bank_statement_imports_bank_account_id "
        "ON bank_statement_imports (bank_account_id);"
    )
    # Partial: a VOIDED import's file may be imported again (void exists for
    # "the parser got this file wrong"; after a fix the same bytes re-import).
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_stmt_import_sha_active "
        "ON bank_statement_imports (file_sha256) WHERE voided_at IS NULL;"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_bank_account_id "
        "ON bank_statement_lines (bank_account_id);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_import_id "
        "ON bank_statement_lines (import_id);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_bank_stmt_lines_acct_date "
        "ON bank_statement_lines (bank_account_id, txn_date);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_bank_statement_line_sources_line_id "
        "ON bank_statement_line_sources (line_id);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_bank_statement_line_sources_import_id "
        "ON bank_statement_line_sources (import_id);"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS bank_statement_line_sources;")
    bind.exec_driver_sql("DROP TABLE IF EXISTS bank_statement_lines;")
    bind.exec_driver_sql("DROP TABLE IF EXISTS bank_statement_imports;")
    bind.exec_driver_sql("DROP TABLE IF EXISTS bank_accounts;")
