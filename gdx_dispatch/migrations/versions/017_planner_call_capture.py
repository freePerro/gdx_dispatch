"""planner_tasks call-capture columns — contact_phone, phone_com_call_id, source

Quick-capture a phone-call note into a PlannerTask without stopping to find or
create a customer. planner_tasks is ORM-managed (built by create_all, not the
squashed baseline), so guard the ALTER with to_regclass: a no-op on a fresh DB
where create_all already includes the columns, runs on existing DBs. Each ADD is
IF NOT EXISTS so the whole thing is idempotent.

Revision ID: 017_planner_call_capture
Revises: 016_dunning_and_recurring
"""
from alembic import op

revision = "017_planner_call_capture"
down_revision = "016_dunning_and_recurring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.planner_tasks') IS NOT NULL THEN
            ALTER TABLE planner_tasks
              ADD COLUMN IF NOT EXISTS contact_phone VARCHAR(40),
              ADD COLUMN IF NOT EXISTS phone_com_call_id VARCHAR(80),
              ADD COLUMN IF NOT EXISTS source VARCHAR(20);
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        ALTER TABLE planner_tasks
          DROP COLUMN IF EXISTS contact_phone,
          DROP COLUMN IF EXISTS phone_com_call_id,
          DROP COLUMN IF EXISTS source
        """
    )
