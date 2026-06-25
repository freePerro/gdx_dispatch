"""add line_metadata JSON to estimate_lines

Plugin integration point (ADR-013): a plugin (e.g. the CHI pricing plugin) writes
the full captured source spec onto an estimate line here — door specs, install
detail, receiving/load + weight, source ids. Stored in CORE so it survives the
estimate→Job conversion and is readable downstream (techs / receiving / order
tracking) even if the plugin that wrote it is later removed. Generic + nullable:
ordinary lines simply leave it NULL.

Revision ID: 003_estimate_line_metadata
Revises: 002_session_idle_timeout
"""
from alembic import op

revision = "003_estimate_line_metadata"
down_revision = "002_session_idle_timeout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS keeps this idempotent — the entrypoint runs upgrade head on
    # every boot across multiple containers. JSON (not JSONB) to match the other
    # JSON columns in this schema (e.g. customers.metadata_).
    op.get_bind().exec_driver_sql(
        "ALTER TABLE estimate_lines ADD COLUMN IF NOT EXISTS line_metadata JSON"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE estimate_lines DROP COLUMN IF EXISTS line_metadata"
    )
