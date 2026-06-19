"""Control-plane RLS — Migration A / Phase A2 (three-plane isolation).

Enables ROW LEVEL SECURITY + FORCE on every control-plane table with a
NOT NULL tenant-scoping column, and installs a single ``FOR ALL`` policy
that gates SELECT/INSERT/UPDATE/DELETE on ``current_setting('app.tenant_id',
true)``. GUC is set per session by ``gdx_dispatch.core.database.get_db``
(commit 06315317). Unset GUC → NULL → predicate false → zero rows
returned and every write rejected. Fail-closed.

FORCE ROW LEVEL SECURITY is required because the runtime user ``gdx`` is
the table owner — without FORCE the owner bypasses policies and the
whole migration is a no-op at runtime.

Canonical target list lives in ``gdx_dispatch/tools/control_plane_rls_targets.py``
(also consumed by the integration tests). Deferred / commerce-plane
tables are documented there.

Revision ID: control_plane_rls
Down revision: ss35_pii_tracking
"""
from __future__ import annotations

from alembic import op

from gdx_dispatch.migrations._rls_frozen import (
    OWNER_TENANT_TABLES,
    TEXT_TENANT_TABLES,
    UUID_TENANT_TABLES,
    policy_sql,
)

revision = "control_plane_rls"
down_revision = "ss35_pii_tracking"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


# tenant_settings is created later in the chain (mig 033_tenant_settings_table)
# and that migration applies the same RLS policy itself (line 58 of 033).
# Including it here would crash on a fresh DB. Lab/prod were seeded
# incrementally so this gap was invisible there; fresh-DB bring-up (and the
# SS-5 PG integration gate) hit it. See ai-queue/operations/inbox/
# D-alembic-024-tenant-settings-ordering.md for the full background.
_DEFER_TO_LATER = frozenset({"tenant_settings"})


def upgrade() -> None:
    if not _is_postgres():
        return
    for t in TEXT_TENANT_TABLES:
        if t in _DEFER_TO_LATER:
            continue
        op.execute(policy_sql(t, "tenant_id"))
    for t in UUID_TENANT_TABLES:
        if t in _DEFER_TO_LATER:
            continue
        op.execute(policy_sql(t, "tenant_id", "::text"))
    for t in OWNER_TENANT_TABLES:
        if t in _DEFER_TO_LATER:
            continue
        op.execute(policy_sql(t, "owner_tenant_id"))


def downgrade() -> None:
    if not _is_postgres():
        return
    for t in TEXT_TENANT_TABLES + UUID_TENANT_TABLES + OWNER_TENANT_TABLES:
        if t in _DEFER_TO_LATER:
            continue
        op.execute(f"DROP POLICY IF EXISTS {t}_tenant_isolation ON {t};")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY;")
