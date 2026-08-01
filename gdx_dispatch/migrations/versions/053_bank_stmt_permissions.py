"""Grant bank_feeds.statements to existing accounting role rows.

Doug 2026-08-01: statement import/void is the accounting role's job.
The new key lives in the code bundle (core/permissions.py), but the
permission resolver honors the TENANT DB ROW for the accounting role
(tenant_roles.permissions, a JSON-array text column) — snapshots taken
at seed time never learn new BUILTIN keys on their own (the S97
perm-snapshot trap, admin/owner-only union). So existing rows get the
key appended here; rows that already have it, custom roles, and deleted
rows are untouched.

Scan viewing needs no backfill: it is gated on accounting.write, which
the accounting role rows already carry.

Revision ID: 053_bank_stmt_permissions
Revises: 052_bank_statement_matches
"""
from alembic import op

revision = "053_bank_stmt_permissions"
down_revision = "052_bank_statement_matches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        UPDATE tenant_roles
        SET permissions = (permissions::jsonb || '["bank_feeds.statements"]'::jsonb)::text,
            updated_at = now()
        WHERE name = 'accounting'
          AND deleted_at IS NULL
          AND NOT (permissions::jsonb ? 'bank_feeds.statements');
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        UPDATE tenant_roles
        SET permissions = (permissions::jsonb - 'bank_feeds.statements')::text,
            updated_at = now()
        WHERE name = 'accounting' AND deleted_at IS NULL;
        """
    )
