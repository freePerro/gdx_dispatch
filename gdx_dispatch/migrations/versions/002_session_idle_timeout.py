"""add session_idle_timeout_minutes to tenant_settings

Tenant-wide inactivity auto-logout: minutes of no activity before the frontend
signs the user out. 0 = disabled (the default). Set by admin/owner via
/api/session-policy; read by any signed-in user to enforce.

Revision ID: 002_session_idle_timeout
Revises: 001_squashed_baseline
"""
from alembic import op

revision = "002_session_idle_timeout"
down_revision = "001_squashed_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS keeps this idempotent — the entrypoint runs upgrade head on
    # every boot across multiple containers.
    op.get_bind().exec_driver_sql(
        "ALTER TABLE tenant_settings "
        "ADD COLUMN IF NOT EXISTS session_idle_timeout_minutes "
        "INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE tenant_settings DROP COLUMN IF EXISTS session_idle_timeout_minutes"
    )
