"""Record which address fields of a customer location a human edited in GDX.

The QuickBooks sub-customer pull (`_upsert_subcustomer_location` in
modules/quickbooks/sync.py) writes address/city/state/zip onto a mapped
location on every pull whenever QB has a value, so a correction made in the
office is overwritten on the next sync with no trace. Customers got the same
guard in migration 070; locations never did.

* ``customer_locations.local_edit_at`` — timestamp, nullable. WHEN a human
  last edited the location. Informational.
* ``customer_locations.local_edit_fields`` — JSON list, nullable. WHICH fields
  a human owns (subset of address/city/state/zip). The authority, per field,
  for the reason 070 gives: a single timestamp cannot tell "GDX never had a
  zip" from "a human deleted the wrong zip".

Same split as ``customers`` after 070: on an upgraded Postgres this adds
TIMESTAMPTZ and JSONB; on a fresh install ``create_all`` builds the table from
the model first, so ``local_edit_fields`` is plain ``json`` there and this
migration is a no-op. Read the list in Python; do not compare it in SQL,
because ``json`` has no equality operator. SQLite gets TIMESTAMP and JSON
(TEXT affinity).

No backfill. Existing rows get NULL, which means "no human edit recorded"
and keeps today's behaviour until an edit path stamps the row. The edit-path
stamping and the sync-side skip ship separately (GDXA-240); this migration
only makes the columns exist.

Guarded with ``has_table``/column checks because ``customer_locations`` is an
ORM-created table: where ``create_all`` has not run yet it is absent, and
``create_all`` then builds it from the model with the columns present.

Rollback: ``downgrade()`` drops both columns (``batch_alter_table`` so SQLite
rebuilds the table). The sync falls back to QB-wins, today's behaviour.

Revision ID: 105_location_local_edit
Revises: 104_refuse_debit_cards
Create Date: 2026-10-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "105_location_local_edit"
down_revision = "104_refuse_debit_cards"
branch_labels = None
depends_on = None

TABLE = "customer_locations"
COLUMNS = ("local_edit_at", "local_edit_fields")


def _present(bind) -> set[str] | None:
    """The subset of COLUMNS already on the table, or None when it is absent."""
    insp = inspect(bind)
    if not insp.has_table(TABLE):
        return None
    return {col["name"] for col in insp.get_columns(TABLE)} & set(COLUMNS)


def upgrade() -> None:
    present = _present(op.get_bind())
    if present is None or present == set(COLUMNS):
        return
    with op.batch_alter_table(TABLE) as batch:
        if "local_edit_at" not in present:
            batch.add_column(
                sa.Column("local_edit_at", sa.DateTime(timezone=True), nullable=True)
            )
        if "local_edit_fields" not in present:
            batch.add_column(
                sa.Column(
                    "local_edit_fields",
                    sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                    nullable=True,
                )
            )


def downgrade() -> None:
    present = _present(op.get_bind())
    if not present:
        return
    with op.batch_alter_table(TABLE) as batch:
        for name in COLUMNS:
            if name in present:
                batch.drop_column(name)
