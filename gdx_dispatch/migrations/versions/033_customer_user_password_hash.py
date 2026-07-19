"""Customer portal password login — add customer_users.password_hash

Doug 2026-07-18. The customer portal is passwordless (magic-link only); this
adds the credential column so a customer can set a password and sign in with
email+password (magic-link stays as onboarding + forgot-password).

The column already exists in the CustomerUser model, but ``customer_users`` is a
create_all (tenant-plane) table that predates every migration — and
``create_all(checkfirst=True)`` is ADD-ONLY (it never ALTERs an existing table).
So a database whose ``customer_users`` was created before ``password_hash``
entered the model never received the column. Guarantee it here.

``customer_users`` is create_all (not in baseline_squashed.sql) — guard with
to_regclass (003/013/019 pattern) so a fresh-DB boot under GDX_SKIP_BOOTSTRAP is
a no-op, and ``ADD COLUMN IF NOT EXISTS`` makes it idempotent when create_all
already built the column.

Revision ID: 033_customer_user_password_hash
Revises: 032_po_door_snapshot
"""
from alembic import op

revision = "033_customer_user_password_hash"
down_revision = "032_po_door_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.customer_users') IS NOT NULL THEN
            ALTER TABLE customer_users
              ADD COLUMN IF NOT EXISTS password_hash VARCHAR(200);
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.customer_users') IS NOT NULL THEN
            ALTER TABLE customer_users DROP COLUMN IF EXISTS password_hash;
          END IF;
        END $$;
        """
    )
