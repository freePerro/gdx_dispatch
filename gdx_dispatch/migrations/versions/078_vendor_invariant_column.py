"""The arithmetic-check verdict becomes a real column (money-audit M26).

The check guards untrusted LLM-extracted money, and its verdict lived in a
free-text `notes` field shared with the LLM marker — which is how a substring
contract (`startswith`) silently reported PASS for failing bills. The one-line
read fix shipped earlier; this is the prescribed second half: store the boolean.

**Backfill is deliberately partial and honest.** Rows whose notes carry the
INVARIANT_MISMATCH marker are backfilled FALSE — the marker is written if and
only if the check failed, so that direction is proven. Rows without the marker
stay NULL: "no marker" and "passed" are only equivalent if every historical row
ran the check, which this migration does not assert. Readers fall back to the
substring for NULL rows, which reproduces the pre-column inference exactly —
so legacy behavior is unchanged while new rows get the authoritative value.

Runs on SQLite and Postgres. Rollback: drop the column; readers fall back to
the substring for every row, which is the pre-migration world.

Revision ID: 078_vendor_invariant_column
Revises: 076_payment_voided_reason
"""
from __future__ import annotations

import contextlib

from alembic import op

revision = "078_vendor_invariant_column"
down_revision = "076_payment_voided_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE IF EXISTS vendor_invoices ADD COLUMN IF NOT EXISTS invariant_ok BOOLEAN;"
        )
    else:
        # SQLite: no IF NOT EXISTS for columns. A fresh DB already has the
        # column from ORM metadata, so "duplicate column" is expected there.
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "ALTER TABLE vendor_invoices ADD COLUMN invariant_ok BOOLEAN;"
            )
    # Proven direction only: the marker is written iff the check failed.
    bind.exec_driver_sql(
        "UPDATE vendor_invoices SET invariant_ok = FALSE "
        "WHERE invariant_ok IS NULL AND notes LIKE '%%INVARIANT_MISMATCH%%';"
    )


def downgrade() -> None:
    # Loses no facts a reader cannot re-infer: the substring fallback IS the
    # pre-column world, and FALSE rows all carry the marker in notes.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE IF EXISTS vendor_invoices DROP COLUMN IF EXISTS invariant_ok;"
        )
    else:
        with contextlib.suppress(Exception):
            bind.exec_driver_sql("ALTER TABLE vendor_invoices DROP COLUMN invariant_ok;")
