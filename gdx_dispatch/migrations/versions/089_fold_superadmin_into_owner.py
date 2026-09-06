"""Fold any platform-superadmin user into owner.

Revision ID: 089_fold_superadmin_into_owner
Revises: 088_drop_supplier_portal_tables
Create Date: 2026-09-06

The "super_admin" role (also spelled "superadmin" / "super-admin") was the
platform operator of a hosted control plane this single-tenant app never
had. The role registry stops recognising it in the same change, so a user
row still carrying the spelling would drop from the admin tier to "unknown
role" at the next login — locked out of every admin surface with no one
able to see why. Owner is the nearest real role (both were owner-tier).
Production holds 0 such rows (read 2026-09-06); this exists for any other
install.

Downgrade cannot tell a folded row from a native owner, so it leaves the
rows alone (a no-op by design, recorded here rather than faked).
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

revision = "089_fold_superadmin_into_owner"
down_revision = "088_drop_supplier_portal_tables"
branch_labels = None
depends_on = None

_SPELLINGS = "('super_admin', 'superadmin', 'super-admin')"


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("users"):
        bind.exec_driver_sql(  # noqa: S608 — the IN list is a module constant, no user input
            f"UPDATE users SET role = 'owner' WHERE LOWER(TRIM(role)) IN {_SPELLINGS};"
        )


def downgrade() -> None:
    """No-op: a folded row is indistinguishable from a native owner."""
