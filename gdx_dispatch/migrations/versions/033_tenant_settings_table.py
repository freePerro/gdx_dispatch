"""Sprint 1.x-S3 — control-plane ``tenant_settings`` table.

Per-tenant settings, starting with the LLM/AI provider API key (Fernet-
encrypted in S4). Schema only here. RLS uses the canonical ``policy_sql``
pattern; ``tenant_id`` is uuid so the column is cast to ``::text`` to compare
against the ``app.tenant_id`` GUC.

Revision ID: tenant_settings_table
Down revision: d97_drop_dead_uuid_alts
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.types import Uuid

from gdx_dispatch.migrations._rls_frozen import policy_sql

revision = "tenant_settings_table"
down_revision = "d97_drop_dead_uuid_alts"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "tenant_settings",
        sa.Column(
            "tenant_id",
            Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("llm_provider_key_enc", sa.Text(), nullable=True),
        sa.Column("llm_provider_key_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("llm_provider_key_last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("llm_provider_key_last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    if _is_postgres():
        op.execute(policy_sql("tenant_settings", "tenant_id", "::text"))
        # GRANT only if gdx_app exists — same skip-cleanly pattern as
        # mig 027_grant_gdx_app_rls_tables. Fresh dev/test DBs (incl. the
        # SS-5 PG integration gate) have no gdx_app provisioned.
        bind = op.get_bind()
        role_exists = bind.exec_driver_sql(
            "SELECT 1 FROM pg_roles WHERE rolname = 'gdx_app'"
        ).fetchone()
        if role_exists:
            op.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_settings TO gdx_app"
            )


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP POLICY IF EXISTS tenant_settings_tenant_isolation ON tenant_settings")
        op.execute("ALTER TABLE tenant_settings NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE tenant_settings DISABLE ROW LEVEL SECURITY")
    op.drop_table("tenant_settings")
