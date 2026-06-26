"""#45 — backfill users.role to the canonical (long) vocabulary

users.role was written in two vocabularies depending on the creation path
(routers/users.py wrote short 'tech'/'dispatch'; core/tenant_ui.py wrote long
'technician'/'dispatcher'). Both write paths now normalize to the long form;
this one-shot backfill collapses any remaining short rows so the column holds a
single vocabulary.

`users` is in the squashed baseline, so no to_regclass guard is needed (the
table always exists when migrations run).

Revision ID: 009_canon_user_roles
Revises: 008_catalog_item_vendor
"""
from alembic import op

revision = "009_canon_user_roles"
down_revision = "008_catalog_item_vendor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "UPDATE users SET role = 'technician' WHERE role = 'tech'"
    )
    op.get_bind().exec_driver_sql(
        "UPDATE users SET role = 'dispatcher' WHERE role = 'dispatch'"
    )


def downgrade() -> None:
    # Best-effort reverse to the legacy short forms.
    op.get_bind().exec_driver_sql(
        "UPDATE users SET role = 'tech' WHERE role = 'technician'"
    )
    op.get_bind().exec_driver_sql(
        "UPDATE users SET role = 'dispatch' WHERE role = 'dispatcher'"
    )
