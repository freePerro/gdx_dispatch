"""Grant customers.contact_write to existing role snapshots

Adding a key to BUILTIN_ROLES is not enough to deliver it. `_load_user_permissions`
(core/modules.py) unions BUILTIN into a role's stored snapshot **only for
admin/owner** — every other role's snapshot is trusted verbatim (step 4). Every
tenant here already has persisted `tenant_roles` rows, so without this migration
a technician's snapshot keeps the exact list it was seeded with, the new key
never arrives, and the "add a contact" UI 403s forever. It would look shipped
and be dead.

That failure mode is already recorded in the resolver's own docstring as
D-S97-perm-snapshot ("snapshots taken at signup miss any BUILTIN keys added
later, silently locking the admin out of new features") — it was closed for
admin/owner only. This migration closes it by hand for the roles that need this
one key.

The alternative — union BUILTIN for every role — would silently grant every
future permission to everyone the moment it's added, which is a platform policy
change, not a migration.

Idempotent: appends only where the key is absent, so re-running is a no-op and
a tenant that has since edited its roles in the UI keeps its edits. Deliberately
does NOT touch roles a tenant renamed or invented: only the three builtin names
that get this key by default. An operator who wants it elsewhere ticks the box
in Roles & Permissions.

Revision ID: 029_customer_contact_write
Revises: 028_parts_needed_sku_255
"""
from alembic import op

revision = "029_customer_contact_write"
down_revision = "028_parts_needed_sku_255"
branch_labels = None
depends_on = None

_ROLES = ("technician", "dispatcher", "sales")
_KEY = "customers.contact_write"


def upgrade() -> None:
    # tenant_roles.permissions is **text** holding a JSON string, NOT jsonb.
    # That matters more than it looks: on text, `||` is string CONCATENATION, so
    # the obvious `permissions || '["key"]'` would staple a literal onto the end
    # of the JSON and corrupt every role it touched. Cast to jsonb to do the
    # work, cast back to text to store it.
    #
    # The LIKE guard is the cheap filter that keeps a malformed row from
    # reaching ::jsonb, which RAISES and would abort the whole migration.
    # Verified on prod: 14/14 rows well-formed, 0 null/empty.
    #
    # It is written '[%%]' rather than '[%]' because this string goes to the
    # DBAPI, and psycopg2 reads a bare % as a parameter placeholder: it fails
    # with "immutabledict is not a sequence" and, since env.py wraps the upgrade
    # in a single transaction, drags every other migration in the run down with
    # it. (Which is exactly what it did the first time this was run.)
    op.get_bind().exec_driver_sql(
        f"""
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'tenant_roles'
          ) THEN
            UPDATE tenant_roles
               SET permissions = ((permissions::jsonb) || '["{_KEY}"]'::jsonb)::text
             WHERE name IN {_ROLES!r}
               AND permissions LIKE '[%%]'
               AND jsonb_typeof(permissions::jsonb) = 'array'
               -- Not already granted, and not a wildcard role (owner holds "*"
               -- and appending a key to it would be noise).
               AND NOT ((permissions::jsonb) @> '["{_KEY}"]'::jsonb)
               AND NOT ((permissions::jsonb) @> '["*"]'::jsonb);
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        f"""
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'tenant_roles'
          ) THEN
            UPDATE tenant_roles
               SET permissions = ((permissions::jsonb) - '{_KEY}')::text
             WHERE name IN {_ROLES!r}
               AND permissions LIKE '[%%]'
               AND jsonb_typeof(permissions::jsonb) = 'array';
          END IF;
        END $$;
        """
    )
