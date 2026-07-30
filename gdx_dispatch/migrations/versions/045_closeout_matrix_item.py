"""Closeout install-matrix pick — plan §8 install lane.

job_closeouts.labor_matrix_item_id: the labor_price_items row a tech picked
at closeout for a flat-priced install. NULL for service-lane jobs (hourly)
and for installs with no pick (fall to office-priced). String(36), not an FK
— labor_price_items rows are occasionally reseeded, and billing_lanes reads
flat_price live from the row anyway.

IF NOT EXISTS — same create_all-vs-alembic split as 040–044.

Revision ID: 045_closeout_matrix_item
Revises: 044_service_pricing_and_adjustments
"""
from alembic import op

revision = "045_closeout_matrix_item"
down_revision = "044_service_pricing_and_adjustments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE job_closeouts "
        "ADD COLUMN IF NOT EXISTS labor_matrix_item_id varchar(36)"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE job_closeouts DROP COLUMN IF EXISTS labor_matrix_item_id"
    )
