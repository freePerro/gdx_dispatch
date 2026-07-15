"""GL Phase 1 S5 — revenue_category_account_map column on gl_settings

Fresh DBs get the column from ``create_all`` (it's on the model). This
migration covers the deploy ordering where S2 (which created gl_settings)
reached production before S5: plugin-table-schema-drift's twin — create_all
builds NEW tables but never ALTERs existing ones (#41 scope).

Idempotent: ADD COLUMN IF NOT EXISTS, guarded on the table existing at all
(if it doesn't, create_all is about to build it WITH the column, so there is
nothing to do).

Revision ID: 021_gl_revenue_map
Revises: 020_gl_coa_role_index
"""
from alembic import op

revision = "021_gl_revenue_map"
down_revision = "020_gl_coa_role_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $mig$
        BEGIN
          IF to_regclass('public.gl_settings') IS NOT NULL THEN
            ALTER TABLE gl_settings
              ADD COLUMN IF NOT EXISTS revenue_category_account_map JSON;
          END IF;
        END
        $mig$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $mig$
        BEGIN
          IF to_regclass('public.gl_settings') IS NOT NULL THEN
            ALTER TABLE gl_settings DROP COLUMN IF EXISTS revenue_category_account_map;
          END IF;
        END
        $mig$;
        """
    )
