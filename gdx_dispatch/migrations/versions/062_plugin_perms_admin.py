"""Grant the blanket plugin keys to existing admin/owner role rows.

ADR-013 per-plugin authorization adds `plugins.read` / `plugins.write` to the
static catalog, and `BUILTIN_ROLES` picks them up in the code bundle. That is
NOT enough for rows already in the tenant DB.

`_load_user_permissions` (core/modules.py) only unions the BUILTIN set over a
stored snapshot when the assigned role NAME equals the role on the user row.
When those drift — a condition the resolver already logs as real — an admin
falls through to the snapshot, which cannot contain a key minted today. The
result would be an admin locked out of their own tenant's plugins, which is the
one outcome core/permissions.py says must never happen. Same S97 perm-snapshot
trap that migration 053 was written for.

Deliberately admin/owner ONLY. Dispatcher/sales/accounting/technician/viewer are
meant to lose plugin access until granted per plugin — that is the point of the
feature and Doug confirmed it (2026-08-11). Custom roles are untouched for the
same reason: an operator decides who gets a plugin, not this migration.

Revision ID: 062_plugin_perms_admin
Revises: 061_retire_standalone_proposals
"""
from alembic import op

revision = "062_plugin_perms_admin"
down_revision = "061_retire_standalone_proposals"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    # Literal statements rather than a loop over the key names: the keys are
    # fixed, and interpolating them would read as string-built SQL.
    bind.exec_driver_sql(
        """
        UPDATE tenant_roles
        SET permissions = (permissions::jsonb || '["plugins.read"]'::jsonb)::text,
            updated_at = now()
        WHERE name IN ('admin', 'owner')
          AND deleted_at IS NULL
          AND NOT (permissions::jsonb ? 'plugins.read');
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE tenant_roles
        SET permissions = (permissions::jsonb || '["plugins.write"]'::jsonb)::text,
            updated_at = now()
        WHERE name IN ('admin', 'owner')
          AND deleted_at IS NULL
          AND NOT (permissions::jsonb ? 'plugins.write');
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        UPDATE tenant_roles
        SET permissions = (permissions::jsonb - 'plugins.read')::text,
            updated_at = now()
        WHERE name IN ('admin', 'owner') AND deleted_at IS NULL;
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE tenant_roles
        SET permissions = (permissions::jsonb - 'plugins.write')::text,
            updated_at = now()
        WHERE name IN ('admin', 'owner') AND deleted_at IS NULL;
        """
    )
