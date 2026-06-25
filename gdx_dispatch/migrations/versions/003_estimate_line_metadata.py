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
    #
    # estimate_lines is NOT in baseline_squashed.sql — it's created by
    # SQLAlchemy create_all() at app startup. On a fresh DB the entrypoint runs
    # `alembic upgrade head` BEFORE the app boots, so the table doesn't exist
    # yet; guard the ALTER so the migration is a no-op then (create_all builds
    # estimate_lines WITH line_metadata from the model). On existing DBs the
    # table is present and the ALTER runs. Without this guard a fresh-DB boot
    # (the release smoke test) aborts here.
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.estimate_lines') IS NOT NULL THEN
            ALTER TABLE estimate_lines ADD COLUMN IF NOT EXISTS line_metadata JSON;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE estimate_lines DROP COLUMN IF EXISTS line_metadata"
    )
