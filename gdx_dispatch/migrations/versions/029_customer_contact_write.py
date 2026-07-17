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
    # pg_input_is_valid, not a LIKE. The first draft guarded with
    # `permissions LIKE '[%%]'` and a comment claiming that kept malformed rows
    # away from ::jsonb. It does not, and one line of SQL disproves it:
    #
    #   '[oops]' LIKE '[%]'  -> true, then ::jsonb RAISES
    #     invalid input syntax for type json
    #
    # LIKE filters things that don't LOOK like an array; anything shaped [...]
    # sails through and raises, and because env.py wraps the upgrade in one
    # transaction it takes the whole run down. Nor does WHERE-clause order save
    # it: the planner puts `~~` first because it is cheap, not because SQL
    # promises to evaluate the guard before the cast. pg_input_is_valid answers
    # the actual question — "will this cast" — and both prod and dev are PG
    # 16.13, so it's available. (Caught in review, 2026-07-17.)
    #
    # '%%' escaping is still needed on any literal % here: exec_driver_sql
    # passes params, so psycopg2 reads a bare % as a placeholder and fails with
    # "immutabledict is not a sequence" — verified, and it is exactly what this
    # migration did on its first run.
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
               AND permissions IS NOT NULL
               AND pg_input_is_valid(permissions, 'jsonb')
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
    """Deliberately a no-op. Stripping the key back out is not the inverse.

    The first draft stripped it unconditionally, which is not what upgrade did
    in reverse — upgrade grants only where the key is ABSENT, so it has no idea
    which rows it actually touched. Two ways that loses real data:

      * A tenant seeded AFTER this shipped gets the key from BUILTIN_ROLES
        without this migration ever running. An unconditional strip removes a
        grant 029 never made.
      * An operator who ticked "Add or correct customer contact details" onto a
        role in the Roles & Permissions UI has their deliberate choice deleted
        by a schema rollback.

    Leaving the key granted on a rollback is harmless in the other direction:
    the older code simply never checks a permission it doesn't define, and
    _load_user_permissions ignores unknown keys. Harmless-and-stale beats
    correct-looking-and-destructive, so this does nothing. (Review, 2026-07-17.)
    """
    pass
