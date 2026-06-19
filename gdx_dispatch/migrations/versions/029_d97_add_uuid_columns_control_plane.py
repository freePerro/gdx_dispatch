"""D97 Phase 0a — additive nullable UUID columns on every slug-shaped control-plane table.

Step 1 of the slug→UUID column-shape reconciliation. Adds a nullable
``*_uuid`` shadow column next to every ``character varying`` ``*_tenant_id``
(or ``company_id`` / ``owner_tenant_id`` / ``sharer_tenant_id`` / ``sharee_tenant_id``
/ ``accepted_by_tenant_id`` / ``target_tenant_id``) column on the prod
control plane. No drops, no renames, no policy changes. Pure DDL.

App keeps reading the slug column. Subsequent migrations:

* 030 backfills the new UUID columns via JOIN on ``tenants.slug``.
* 031 atomically swaps writers + policies, drops the slug columns,
  renames ``*_uuid → *_tenant_id`` (or canonical ``tenant_id``), and
  re-renders the RLS policies. Code coupling lives in 031.
* 032 cleans up any leftover empties.

Excluded by design:
- ``tenant_module_grants.tenant_id``, ``granted_by_tenant_id`` — already UUID.
- ``tenants.parent_tenant_id`` — dead column (zero writers, all NULL); dropped in 031.
- ``platform_consumer_audit.tenant_id`` — varchar but stored values already
  look like UUIDs; handled by direct ``ALTER COLUMN TYPE uuid USING …::uuid``
  in 031, no shadow column.

Survey source: Phase 0 an earlier session + Phase 0c an earlier session prod re-pull.
See ``gdx_dispatch/docs/d97_rls_runbook.md`` for the full plan.

Revision ID: d97_add_uuid_columns
Down revision: grant_gdx_app_all_tables
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "d97_add_uuid_columns"
down_revision = "grant_gdx_app_all_tables"
branch_labels = None
depends_on = None


# (table, old_slug_column, new_uuid_column)
#
# Convention: shadow column name mirrors the old name with the ``_id`` /
# ``_id``-suffix replaced by ``_uuid`` (or a fresh ``tenant_uuid`` for
# ``company_id``, since 031 renames it to canonical ``tenant_id``). 031
# performs the swap rename to the final canonical name.
SHADOW_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # tenant_id → tenant_uuid (29 tables)
    ("audit_logs", "tenant_id", "tenant_uuid"),
    ("audit_retention_policy", "tenant_id", "tenant_uuid"),
    ("billing_overage_event", "tenant_id", "tenant_uuid"),
    ("billing_plan", "tenant_id", "tenant_uuid"),
    ("cutover_schedule", "tenant_id", "tenant_uuid"),
    ("deprecated_table_record", "tenant_id", "tenant_uuid"),
    ("event_outbox", "tenant_id", "tenant_uuid"),
    ("game_definitions", "tenant_id", "tenant_uuid"),
    ("game_state", "tenant_id", "tenant_uuid"),
    ("installations", "tenant_id", "tenant_uuid"),
    ("locations", "tenant_id", "tenant_uuid"),
    ("mcp_execution_log", "tenant_id", "tenant_uuid"),
    ("mcp_tool_execution_audit", "tenant_id", "tenant_uuid"),
    ("memberships", "tenant_id", "tenant_uuid"),
    ("metering_checkpoint", "tenant_id", "tenant_uuid"),
    ("metering_usage", "tenant_id", "tenant_uuid"),
    ("resource_field_descriptors", "tenant_id", "tenant_uuid"),
    ("resource_instance", "tenant_id", "tenant_uuid"),
    ("sandbox_envs", "tenant_id", "tenant_uuid"),
    ("shadow_migration_checkpoint", "tenant_id", "tenant_uuid"),
    ("shadow_migration_drift", "tenant_id", "tenant_uuid"),
    ("shadow_migration_state", "tenant_id", "tenant_uuid"),
    ("ss21_admin_consent_grants", "tenant_id", "tenant_uuid"),
    ("ss21_authorization_codes", "tenant_id", "tenant_uuid"),
    ("ss21_oauth_tokens", "tenant_id", "tenant_uuid"),
    ("ss21_webhook_subscriptions", "tenant_id", "tenant_uuid"),
    ("ss31_federation_provider", "tenant_id", "tenant_uuid"),
    ("sso_configs", "tenant_id", "tenant_uuid"),
    ("tenant_health_logs", "tenant_id", "tenant_uuid"),
    # cousin columns (5)
    ("cross_tenant_share", "sharer_tenant_id", "sharer_tenant_uuid"),
    ("cross_tenant_share", "sharee_tenant_id", "sharee_tenant_uuid"),
    ("cross_tenant_share_acceptance", "accepted_by_tenant_id", "accepted_by_tenant_uuid"),
    ("resource_type", "owner_tenant_id", "owner_tenant_uuid"),
    ("shared_resources", "owner_tenant_id", "owner_tenant_uuid"),
    ("shares", "target_tenant_id", "target_tenant_uuid"),
    # company_id (1) — 031 will drop company_id and rename tenant_uuid → tenant_id
    ("platform_feature_flags", "company_id", "tenant_uuid"),
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    ).fetchone()
    return bool(row)


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, column),
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    if not _is_postgres():
        return
    for table, _old_col, new_col in SHADOW_COLUMNS:
        if not _table_exists(table):
            # Tables declared in ORM but not yet created on this DB
            # (e.g. lab without full pave) are skipped — they get the
            # canonical UUID column at next create_all().
            continue
        if _column_exists(table, new_col):
            continue
        op.add_column(
            table,
            sa.Column(new_col, postgresql.UUID(as_uuid=True), nullable=True),
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    for table, _old_col, new_col in SHADOW_COLUMNS:
        if not _table_exists(table):
            continue
        if not _column_exists(table, new_col):
            continue
        op.drop_column(table, new_col)
