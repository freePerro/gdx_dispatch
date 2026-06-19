"""Commerce-plane RLS — Migration A / Phase A3 (three-plane isolation).

Enables + FORCES RLS on control-DB tables whose rows are legitimately
visible to more than one tenant:

  * cross_tenant_share — two parties (sharer + sharee). Both can SELECT;
    only sharer can INSERT/UPDATE.
  * cross_tenant_share_acceptance — single party (accepter). Only the
    accepter can SELECT/INSERT/UPDATE.

See ``gdx_dispatch/tools/commerce_plane_rls_targets.py`` for rationale and the
policy SQL.

Out of scope for A3 (documented in the targets module):
  * tenant_relationships / cross_tier_module_grants — orphan ORM models,
    no tables on lab.
  * dealer_orders / wholesale.* / distributor.* — tenant plane, not
    commerce plane.

Revision ID: commerce_plane_rls
Down revision: control_plane_rls
"""
from __future__ import annotations

from alembic import op

from gdx_dispatch.migrations._rls_frozen import (
    cross_tenant_share_acceptance_policy_sql,
    cross_tenant_share_policy_sql,
)


revision = "commerce_plane_rls"
down_revision = "control_plane_rls"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    op.execute(cross_tenant_share_policy_sql())
    op.execute(cross_tenant_share_acceptance_policy_sql())


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(
        "DROP POLICY IF EXISTS cross_tenant_share_parties_read ON cross_tenant_share;"
    )
    op.execute(
        "DROP POLICY IF EXISTS cross_tenant_share_sharer_write ON cross_tenant_share;"
    )
    op.execute("ALTER TABLE cross_tenant_share NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE cross_tenant_share DISABLE ROW LEVEL SECURITY;")
    op.execute(
        "DROP POLICY IF EXISTS cross_tenant_share_acceptance_accepter_only "
        "ON cross_tenant_share_acceptance;"
    )
    op.execute(
        "ALTER TABLE cross_tenant_share_acceptance NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE cross_tenant_share_acceptance DISABLE ROW LEVEL SECURITY;"
    )
