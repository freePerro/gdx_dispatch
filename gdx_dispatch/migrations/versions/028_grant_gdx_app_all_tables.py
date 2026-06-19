"""D97 follow-up (an earlier session, 2026-04-25 evening) — broaden gdx_app grants.

Migration 027 granted gdx_app on the 25 RLS-protected tables only. First
prod env-switch attempt 500'd because the auth flow reads `tenants`
(platform-scoped, no RLS, not in 027's list) to resolve subdomain →
tenant_id. Broadening grants to ALL tables in public schema:

  * RLS-tagged tables — already had grants from 027, still gated by
    their policies. Re-granting is idempotent.
  * Platform-scoped tables (tenants, audit_logs, event_outbox,
    game_definitions, game_state, platform_feature_flags, …) — these
    have nullable tenant_id by design (per the deferred-tables comment
    in control_plane_rls_targets.py) and are visible to every connection.
    Granting gdx_app SELECT/INSERT/UPDATE/DELETE matches what the `gdx`
    superuser was doing pre-switch.

Default privileges added so any FUTURE table created in `public` is
automatically grant-visible to gdx_app — without this every new alembic
table would require a parallel GRANT.

Revision ID: grant_gdx_app_all_tables
Down revision: grant_gdx_app_rls_tables
"""
from __future__ import annotations

from alembic import op


revision = "grant_gdx_app_all_tables"
down_revision = "grant_gdx_app_rls_tables"
branch_labels = None
depends_on = None


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
        return
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO gdx_app;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO gdx_app;"
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    if not _role_exists("gdx_app"):
        return
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM gdx_app;"
    )
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM gdx_app;"
    )
