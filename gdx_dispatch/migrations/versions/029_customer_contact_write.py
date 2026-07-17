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
import logging

from alembic import op

from gdx_dispatch.migrations.grant_helpers import grant_permission_to_seeded_roles

log = logging.getLogger("alembic.runtime.migration")

revision = "029_customer_contact_write"
down_revision = "028_parts_needed_sku_255"
branch_labels = None
depends_on = None

_KEY = "customers.contact_write"
# The seeded builtin roles that get this key by default. Kept in lockstep with
# BUILTIN_ROLES by test_customer_contact_write_migration — if permissions.py and
# this list disagree, a role is silently over- or under-granted, so that test
# fails loudly at PR time rather than in prod.
_ROLES = ("technician", "dispatcher", "sales")


def upgrade() -> None:
    # All the sharp edges of amending this text-JSON column — string-concat vs
    # jsonb, malformed rows that pass a LIKE guard and then raise on ::jsonb, the
    # is_system gate that keeps a tenant's custom same-named role untouched, and
    # the %-escaping footguns — live in one tested helper now, so this migration
    # (and the next one) can't re-derive them wrong. See grant_helpers.py.
    granted = grant_permission_to_seeded_roles(op.get_bind(), permission=_KEY, roles=_ROLES)
    # Log the count so a skipped tenant is visible, not silent. The gate is
    # is_system=true (verified: every builtin row on prod is), but if a tenant's
    # builtin rows were ever seeded is_system=false this grants 0 and the feature
    # would 403 for them — an operator needs to see that in the deploy log rather
    # than discover it as a support ticket. (The leads.* tool logs the same way.)
    log.info("029_customer_contact_write: granted %s to %s seeded role row(s)", _KEY, granted)


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
