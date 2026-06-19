"""SS-16 slice D — ENABLE RLS + CREATE policies on critical tables.

Tables covered (see ``gdx_dispatch.tools.rls_render.CRITICAL_TABLES``):
    * jobs        — tech sees only assigned rows; admin/owner sees all tenant rows
    * customers   — admin/owner only
    * invoices    — admin/owner only
    * leads       — tech sees only assigned rows; admin/owner sees all tenant rows

Uses ``render_policies()`` from :mod:`gdx_dispatch.tools.rls_render` to produce the
SQL — keeps the source-of-truth in one template and this migration thin.

The policies enforce ``company_id = current_setting('app.tenant_id',
true)::uuid`` plus the role-specific predicate. GUCs are set per request
by :class:`gdx_dispatch.core.middleware.tenant_role_middleware.TenantRoleMiddleware`
(SS-16 slice C).

No-op on non-Postgres dialects (sqlite in tests) — RLS is a PG feature.

Revision ID: ss16_rls_critical
Down revision: INTEGRATION_TODO — supervisor chains this onto the main
    migration graph at end-of-sprint integration. Do NOT set to a real
    revision here; the orchestrator rewrites this placeholder when it
    lifts the file into the active chain.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from gdx_dispatch.tools.rls_render import CRITICAL_TABLES, render_policies

# NOTE: revision id uses the sprint slug so the supervisor can grep-find
# and retarget down_revision without guessing. Keep stable.
revision = "ss16_rls_critical"
down_revision = "ss15_admin_pats"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # RLS is a PostgreSQL feature. On sqlite (test env) this migration
        # is a no-op; SS-16 integration tests are gated on PG and only run
        # where RLS is real.
        return

    # CRITICAL_TABLES live on the TENANT-scoped DB (jobs/customers/invoices/
    # leads). When this migration runs against the CONTROL plane DB those
    # tables do not exist — skip silently so the control-plane alembic
    # chain stays green. Tenant DBs get the RLS apply via a separate
    # tenant-scoped invocation of this same SQL (see SS-16 plan).
    inspector = inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    for table, select_extra_predicate, soft_delete_column in CRITICAL_TABLES:
        if table not in existing_tables:
            continue
        # Also require company_id column — the rendered policy references
        # it directly; a listed table that lacks the column (drift between
        # CRITICAL_TABLES and the schema) would fail CREATE POLICY.
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "company_id" not in columns:
            continue
        sql = render_policies(table, select_extra_predicate, soft_delete_column)
        # Render produces multiple statements (ALTER TABLE … ENABLE RLS;
        # CREATE POLICY … select; CREATE POLICY … write). Execute the
        # whole block via raw SQL so Postgres parses them in order.
        op.execute(sql)


def downgrade() -> None:
    if not _is_postgres():
        return

    inspector = inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    for table, _, _ in CRITICAL_TABLES:
        if table not in existing_tables:
            continue
        # Order matters: drop policies before disabling RLS so the
        # policy objects are gone cleanly (DISABLE alone doesn't drop).
        op.execute(f"DROP POLICY IF EXISTS {table}_select_policy ON {table};")
        op.execute(f"DROP POLICY IF EXISTS {table}_write_insert_policy ON {table};")
        op.execute(f"DROP POLICY IF EXISTS {table}_write_policy ON {table};")
        op.execute(f"DROP POLICY IF EXISTS {table}_write_delete_policy ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
