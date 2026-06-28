"""app_settings.debug_logging_enabled — operator debug toggle

When ON, handled-but-swallowed errors (e.g. the absent cc_support_tickets
control-plane table) are also recorded to the server-error sink. app_settings is
ORM-managed (built by create_all, not the squashed baseline), so guard the ALTER
with to_regclass — no-op on a fresh DB where create_all already includes the
column, runs on existing DBs. Idempotent (ADD COLUMN IF NOT EXISTS).

NOTE: a separate unmerged branch (vendor-PII) also carries a migration numbered
010 off 009. If both ever land, alembic will report multiple heads and a merge
revision is needed — resolve at merge time.

Revision ID: 010_app_settings_debug_logging
Revises: 009_canon_user_roles
"""
from alembic import op

revision = "010_app_settings_debug_logging"
down_revision = "009_canon_user_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.app_settings') IS NOT NULL THEN
            ALTER TABLE app_settings
              ADD COLUMN IF NOT EXISTS debug_logging_enabled BOOLEAN NOT NULL DEFAULT false;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE app_settings DROP COLUMN IF EXISTS debug_logging_enabled"
    )
