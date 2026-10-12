"""Delete the module grants whose keys were retired (GDXA-663 / GDXA-706).

Four retirements removed a module key from ``core/modules.py`` and left its
``company_module_grants`` rows behind: ``stripe_connect`` (#578),
``equipment_tracking`` and its ``fleet`` alias (#735), ``campaigns`` (#739),
and the ``contractors`` alias (the control-plane removal). Only 088 cleaned up
after itself (``chrome_extension``). Such a row gates nothing and Settings →
Modules never shows it. Its key 404s the disable route, so no admin can remove
it. And ``enabled_module_keys()`` still forwards it to plugin-host as
"enabled", so a future plugin that reused one of these keys would come up
pre-enabled on this install. The maintainer ruled to delete them (2026-10-11).
Prod held 2 ``equipment_tracking``, 1 ``fleet``, 2 ``campaigns`` and 2
``stripe_connect`` rows (read-only query, 2026-10-11); demo held none of them.

Scope: only keys that were once registered in ``core/modules.py``. Prod also
holds grants for keys that never were (the first release's grant API took any
name). Some of them are live plugin keys (``midland``, ``chipricing``), so a
"delete whatever is not registered" rule would be wrong. Those rows are a
separate decision and this migration leaves them alone.

Existing rows: every ``company_module_grants`` row whose ``module_key`` is in
``_RETIRED`` is deleted, and the count is logged per key. No other row or table
is touched. ``chrome_extension`` is listed again so this one migration matches
``core.modules.RETIRED_MODULE_KEYS`` in full; 088 already deleted that row, so
here it deletes nothing.

Not a row-refusing migration: it never raises on data, so it cannot crash-loop
the ``app`` entrypoint. Guarded by ``has_table``, so it is a no-op on a database
that never created the grants table, and it can be rerun. Plain ``DELETE ... IN``
with bound parameters runs the same on SQLite and Postgres. ``module_key`` is a
``VARCHAR``, not a ``Uuid``, so the SQLite hex-storage trap does not apply.

Rollback: ``downgrade()`` is a no-op, as 088's is for its grant. On an older
image the retired keys mean nothing either. To restore a row, re-enable the
module from Settings on an image that still registers it.

Revision ID: 116_retired_module_grants
Revises: 115_vacation_allowance
Create Date: 2026-10-11
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "116_retired_module_grants"
down_revision = "115_vacation_allowance"
branch_labels = None
depends_on = None

# A copy, not an import of core.modules: a migration must keep meaning what it
# meant when it shipped. The test pins this tuple to RETIRED_MODULE_KEYS.
_RETIRED = (
    "campaigns",
    "chrome_extension",
    "contractors",
    "equipment_tracking",
    "fleet",
    "stripe_connect",
)

_log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("company_module_grants"):
        return
    stmt = sa.text(
        "SELECT module_key, COUNT(*) FROM company_module_grants "
        "WHERE module_key IN :keys GROUP BY module_key"
    ).bindparams(sa.bindparam("keys", expanding=True))
    counts = dict(bind.execute(stmt, {"keys": list(_RETIRED)}).fetchall())
    if not counts:
        return
    bind.execute(
        sa.text("DELETE FROM company_module_grants WHERE module_key IN :keys").bindparams(
            sa.bindparam("keys", expanding=True)
        ),
        {"keys": list(_RETIRED)},
    )
    _log.info(
        "116: deleted retired module grants: %s",
        ", ".join(f"{k}={n}" for k, n in sorted(counts.items())),
    )


def downgrade() -> None:
    pass
