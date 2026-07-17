"""Additive permission grants to persisted role snapshots — the safe primitive.

Every new key added to ``BUILTIN_ROLES`` needs a paired migration, because
``_load_user_permissions`` (core/modules.py) unions BUILTIN into a role's stored
``tenant_roles`` snapshot **only for admin/owner** — every other role's snapshot
is trusted verbatim. So a technician only gains a new permission when its
snapshot gains it (D-S97-perm-snapshot). The leads.* sweep established the
discipline; this is that discipline as one reusable, tested call so the next
migration doesn't hand-write the fragile SQL again and reintroduce the same bugs.

What hand-written raw SQL kept getting wrong on this exact table (029, before
review):

  * ``tenant_roles.permissions`` is **text** holding a JSON string, not jsonb.
    ``permissions || '["k"]'`` on text is string CONCATENATION — it staples a
    literal onto the JSON and corrupts the row. Cast to jsonb, cast back.
  * ``permissions LIKE '[%]'`` does NOT keep a malformed row away from the cast:
    ``'[oops]' LIKE '[%]'`` is true, and ``'[oops]'::jsonb`` then RAISES and, in
    alembic's single-transaction upgrade, takes the whole run down.
    ``pg_input_is_valid`` answers the real question — "will this cast".
  * ``WHERE name IN (...)`` with no ``is_system`` gate would also amend a
    tenant's CUSTOM role that happens to share a builtin name. Only the seeded
    builtin (``is_system = true``) is meant to move; a repurposed custom role is
    the tenant's, and they add the key via the Roles UI if they want it.

This helper closes all three, and uses **bound parameters** for the key and the
role list — so there is no string interpolation, no injection surface, and none
of the psycopg2 ``%``-escaping footguns that a DO-block / exec_driver_sql form
carries.

Properties (the leads sweep's safety contract, enforced here for every caller):
  - ADD-ONLY: never removes a key, never touches a non-target key.
  - is_system + exact-name gated: custom roles of the same name are untouched.
  - malformed-row safe: a row whose ``permissions`` won't parse as jsonb is
    skipped, never cast.
  - idempotent + wildcard-safe: a row already holding the key, or holding ``*``
    (owner), is left alone. Safe to re-run.

Postgres-only by construction (jsonb, pg_input_is_valid). A migration that runs
this on SQLite is a mistake the migration itself would hit; the guard below
turns it into a clean no-op return rather than an obscure operator error.
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

# Bound params (:key, :roles) — no f-string, no %-escaping, no injection. The
# only literal is the wildcard sentinel, which contains no % and is not data.
_GRANT_SQL = text(
    """
    UPDATE tenant_roles
       SET permissions = ((permissions::jsonb) || jsonb_build_array(:key))::text
     WHERE name = ANY(:roles)
       AND is_system = true
       AND permissions IS NOT NULL
       AND pg_input_is_valid(permissions, 'jsonb')
       AND jsonb_typeof(permissions::jsonb) = 'array'
       AND NOT ((permissions::jsonb) @> jsonb_build_array(:key))
       AND NOT ((permissions::jsonb) @> '["*"]'::jsonb)
    """
)


def grant_permission_to_seeded_roles(
    bind: Connection,
    *,
    permission: str,
    roles: Sequence[str],
) -> int:
    """Additively add ``permission`` to the seeded builtin snapshot of each role.

    Returns the number of rows changed (0 if the table doesn't exist yet, e.g. a
    fresh DB where ORM create hasn't run, or a non-Postgres bind).

    Only rows with ``is_system = true`` and a name in ``roles`` are touched, and
    only when they don't already hold the key and aren't the wildcard owner. See
    the module docstring for why each guard is load-bearing.
    """
    if not roles:
        return 0
    # Table-existence check in Python rather than a SQL DO-block: a DO-block
    # can't take bound parameters, and bound parameters are the whole point.
    if not inspect(bind).has_table("tenant_roles"):
        return 0
    # jsonb / pg_input_is_valid are Postgres-only. On any other backend this is
    # a clean no-op instead of a confusing dialect error mid-migration.
    if bind.dialect.name != "postgresql":
        return 0
    result = bind.execute(_GRANT_SQL, {"key": permission, "roles": list(roles)})
    return result.rowcount or 0
