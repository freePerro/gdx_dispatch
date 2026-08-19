"""Invoice line flag: this part's price already includes the installation.

Doug 2026-08-19: "sometimes the install price is in the part price", and
"there is no signal for it we just need to have a check box option for it",
placed "at billing time".

Billing a bundled part alongside an hourly labor line charges the customer
for the install twice, and nothing in the data says which parts bundle it.
The catalog carries both variants of the same opener as separate items --
one at the bare price, one priced with the install -- distinguishable only by
the words in a free-text name. Money code may not guess from prose, so the
office ticks a box and the invoice records the answer.

The flag lives on the LINE, not the catalog item (Doug's call): it is decided
at billing, with the tech's notes in front of the person deciding, and the
catalog is left untouched -- no per-item pass over the tenant's 81 opener
rows before this becomes usable.

DEFAULT FALSE is load-bearing: every existing line keeps exactly today's
behaviour, and the double-bill warning stays silent until a human
deliberately ticks something. No backfill -- inferring the flag from an item
name is the guess this column exists to replace.

Existing rows: unchanged, all read false. Rollback: drop the column; the
warning is advisory only, so nothing downstream breaks without it.

Revision ID: 069_invoice_line_includes_labor
Revises: 068_plugin_email_outbox
"""
import contextlib

from alembic import op

revision = "069_invoice_line_includes_labor"
down_revision = "068_plugin_email_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite lane: no IF NOT EXISTS on ADD COLUMN, so suppress the
        # duplicate-column error and let a re-run be a no-op.
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "ALTER TABLE invoice_lines "
                "ADD COLUMN includes_labor boolean NOT NULL DEFAULT 0;"
            )
        return
    bind.exec_driver_sql(
        """
        ALTER TABLE invoice_lines
            ADD COLUMN IF NOT EXISTS includes_labor boolean NOT NULL DEFAULT false;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite pre-3.35 cannot DROP COLUMN; leaving an unread boolean is
        # harmless and beats failing the downgrade.
        return
    bind.exec_driver_sql(
        "ALTER TABLE invoice_lines DROP COLUMN IF EXISTS includes_labor;"
    )
