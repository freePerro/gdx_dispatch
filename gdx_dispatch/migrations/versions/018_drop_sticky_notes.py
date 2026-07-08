"""drop sticky_notes — dead table (backend CRUD existed, no live SPA UI)

The sticky-notes canvas had a model + /api/sticky-notes routes but was never
wired into the frontend. Removed 2026-07-07 (model + router + tests). sticky_notes
was ORM-managed (built by create_all, not the squashed baseline), so:
  - fresh install: the model is gone, create_orm_tables() never builds it, and
    this DROP IF EXISTS is a no-op.
  - existing DB: this drops the now-orphaned table.
Guarded/idempotent either way. See docs/design/call-capture-followup-plan.md.

Revision ID: 018_drop_sticky_notes
Revises: 017_planner_call_capture
"""
from alembic import op

revision = "018_drop_sticky_notes"
down_revision = "017_planner_call_capture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS sticky_notes")


def downgrade() -> None:
    # Recreate a minimal shell so a downgrade doesn't hard-fail. The feature is
    # dead; this is only to keep the migration reversible. Columns mirror the
    # removed ORM model.
    op.get_bind().exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS sticky_notes (
            id UUID PRIMARY KEY,
            company_id VARCHAR(64) NOT NULL,
            title VARCHAR(200),
            body TEXT NOT NULL,
            color VARCHAR(20) NOT NULL DEFAULT '#fef3c7',
            pos_x INTEGER NOT NULL DEFAULT 0,
            pos_y INTEGER NOT NULL DEFAULT 0,
            width INTEGER NOT NULL DEFAULT 240,
            height INTEGER NOT NULL DEFAULT 180,
            created_by VARCHAR(200),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
