"""Link a feed account to the statement account it really is.

Books-convergence Track 2 item 4 needs to say, per feed transaction, whether
the bank's own statement corroborates it. That requires knowing which
``bank_accounts`` row a ``bank_feed_accounts`` row is the same real-world
account as — and the two sides share no natural key. ``bank_accounts`` is
keyed on institution + last4; ``bank_feed_accounts`` on connection +
external id.

Inferring it was considered and rejected. On the live tenant every SimpleFIN
account carries an EMPTY ``account_number_masked``; the only surviving last-4
is inside the operator-typed account *name*, in parentheses. A money surface
whose account pairing silently re-derives itself from a display name breaks
the first time somebody renames an account, and breaks quietly. So the link
is stored, set by an operator once, and NULL until then — the status column
reports ``unlinked`` rather than inventing a verdict.

Nullable on purpose: 2 of the live tenant's 7 feed accounts have no statement
account at all (a dead Banno connection), and that is a legitimate end state,
not an incomplete migration.

The bank-feed tables are ORM-created (``create_orm_tables()``), not shipped by
a migration — see 064. So this ALTERs a table that may legitimately not exist
yet on a fresh database, and the ORM's ``create_all`` produces the column with
its proper type and FK there. Guarded accordingly on both engines.

Revision ID: 074_bank_feed_statement_link
Revises: 073_drop_dead_money_columns
"""
from __future__ import annotations

import contextlib

from alembic import op

revision = "074_bank_feed_statement_link"
down_revision = "073_drop_dead_money_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite has no ADD COLUMN IF NOT EXISTS and no ALTER TABLE IF EXISTS.
        # A fresh database gets the column from create_all with the real FK;
        # here the column may already be present, so a failure is expected and
        # not a reason to fail the migration.
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "ALTER TABLE bank_feed_accounts ADD COLUMN bank_account_id CHAR(32);"
            )
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_bank_feed_accounts_bank_account_id "
                "ON bank_feed_accounts (bank_account_id);"
            )
        return

    bind.exec_driver_sql(
        "ALTER TABLE IF EXISTS bank_feed_accounts "
        "ADD COLUMN IF NOT EXISTS bank_account_id uuid;"
    )
    # Index rather than a DB-level FK: the column is added here only for
    # databases that predate it, while create_all builds the constrained
    # version. Adding the constraint on one lane only would leave the two
    # shapes disagreeing about what happens when a bank account is removed.
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_bank_feed_accounts_bank_account_id "
        "ON bank_feed_accounts (bank_account_id);"
    )


def downgrade() -> None:
    # Dropping this loses operator-entered links, and nothing can rebuild them
    # — there is no derivable key to fall back on, which is the reason the
    # column exists. Re-linking is a click per account.
    bind = op.get_bind()
    with contextlib.suppress(Exception):
        bind.exec_driver_sql("DROP INDEX IF EXISTS ix_bank_feed_accounts_bank_account_id;")
    if bind.dialect.name != "postgresql":
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "ALTER TABLE bank_feed_accounts DROP COLUMN bank_account_id;"
            )
        return
    bind.exec_driver_sql(
        "ALTER TABLE IF EXISTS bank_feed_accounts DROP COLUMN IF EXISTS bank_account_id;"
    )
