"""editable service-labor description template

Doug 2026-08-07: "the labor description is editable there but what it
automatically fills in is not." The auto-filled text — 'Service labor —
4.00 man-hours (… first hour $100.00, then $100.00/hr)' — was hardcoded
in core/billing_lanes. It becomes a tenant setting beside the service
rates it describes: NULL = the built-in default (no behavior change until
someone edits it). Placeholders: {man_hours} {hours} {techs}
{first_hour_price} {hourly_rate}; a broken template falls back to the
default at render — a settings typo must never 500 a closeout or an
invoice.

Revision ID: 060_service_labor_description
Revises: 059_invoice_attached_photos
"""
from alembic import op

revision = "060_service_labor_description"
down_revision = "059_invoice_attached_photos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS — same create_all-vs-alembic split as 040-044.
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE pricing_settings ADD COLUMN IF NOT EXISTS "
        "service_labor_description_template TEXT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE pricing_settings DROP COLUMN IF EXISTS "
        "service_labor_description_template"
    )
