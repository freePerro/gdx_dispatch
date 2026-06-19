"""D97 (an earlier session, 2026-04-25) — Grant gdx_app on RLS-protected control-plane tables.

Migration 024 enabled+forced RLS on 23 control-plane tables and migration 025
on 2 commerce-plane tables. The F4 audit found that the runtime role `gdx`
has `rolsuper=t rolbypassrls=t`, so all 26 policies (2 on cross_tenant_share)
were no-ops at runtime — superuser/BYPASSRLS overrides FORCE.

The fix is to flip the FastAPI app's CONTROL_DATABASE_URL to connect as
`gdx_app` (already exists, super=f, bypassrls=f). Before that switch can
land safely, `gdx_app` needs full DML grants on every RLS-protected table
plus USAGE on the public schema and any sequences those tables depend on.

This migration grants exactly those privileges. Idempotent (GRANT is
re-runnable). Does NOT change the connection URL — that is a separate
deploy step (env change + container restart) intentionally decoupled
because a missing grant would 500 every request before the policies
have a chance to enforce.

Verification after the env switch is in
``gdx_dispatch/tools/verify_rls_failclosed.py``.

Revision ID: grant_gdx_app_rls_tables
Down revision: games_schema_drift
"""
from __future__ import annotations

from alembic import op

from gdx_dispatch.migrations._rls_frozen import TARGET_TABLES as COMMERCE_TABLES
from gdx_dispatch.migrations._rls_frozen import (
    OWNER_TENANT_TABLES,
    TEXT_TENANT_TABLES,
    UUID_TENANT_TABLES,
)


revision = "grant_gdx_app_rls_tables"
down_revision = "games_schema_drift"
branch_labels = None
depends_on = None


RLS_TABLES: tuple[str, ...] = (
    TEXT_TENANT_TABLES + UUID_TENANT_TABLES + OWNER_TENANT_TABLES + COMMERCE_TABLES
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _role_exists(role: str) -> bool:
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    if not _is_postgres():
        return
    if not _role_exists("gdx_app"):
        # Role must exist before the connection-user switch; in dev/test
        # environments without gdx_app, skip cleanly so the chain still
        # advances. Prod creation is a separate ops step.
        return

    op.execute("GRANT USAGE ON SCHEMA public TO gdx_app;")
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO gdx_app;")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO gdx_app;"
    )
    for table in RLS_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO gdx_app;"
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    if not _role_exists("gdx_app"):
        return
    for table in RLS_TABLES:
        op.execute(
            f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} FROM gdx_app;"
        )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM gdx_app;"
    )
    op.execute("REVOKE USAGE ON ALL SEQUENCES IN SCHEMA public FROM gdx_app;")
    op.execute("REVOKE USAGE ON SCHEMA public FROM gdx_app;")
