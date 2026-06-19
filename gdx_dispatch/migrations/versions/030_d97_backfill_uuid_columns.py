"""D97 Phase 0a — backfill the shadow UUID columns added by 029 / 029b.

Step 2 of the slug→UUID column-shape reconciliation. Pure UPDATE; no
schema mutation. Reads from ``tenants(slug, id)`` and writes the
matching UUID into the ``*_uuid`` shadow columns.

Scope (an earlier session + an earlier session prod survey, re-verified an earlier session):
* ``memberships``: 9 rows, 4 distinct slugs, all resolve.
* ``installations``: 7 rows, 7 distinct slugs, all resolve.
* ``service_accounts.allowed_tenant_uuids``: per-row JSON array of UUIDs
  derived from the per-row ``allowed_tenant_slugs`` JSON array. Prod
  currently has 1 row with ``allowed_tenant_slugs IS NULL`` — no-op there.
* Cousin columns (sharer/sharee/accepted_by/owner/target) live on tables
  that are empty on prod (cross_tenant_share, cross_tenant_share_acceptance,
  resource_type, shared_resources, shares); UPDATEs match 0 rows. Still
  emit them so re-running on a populated lab hits them too.
* ``platform_feature_flags.tenant_uuid``: empty on prod; same shape.

Out of scope (handled by 031 directly, no shadow column to fill):
* ``platform_consumer_audit.tenant_id`` — already UUID-shape strings;
  cast in 031 via ``ALTER COLUMN TYPE uuid USING tenant_id::uuid``.
* ``tenants.parent_tenant_id`` — dead column (0 active writers, all NULL);
  dropped in 031.

Hard assertions (raise on any unresolvable slug — fail loud, never silent):
* memberships: 0 rows with ``tenant_uuid IS NULL`` AND ``tenant_id IS NOT NULL``.
* installations: same.
* service_accounts: for each row where ``allowed_tenant_slugs`` is a
  non-empty JSON array, the resulting ``allowed_tenant_uuids`` array length
  must match the source array length. Otherwise we silently dropped
  unresolvable slugs.

Reversible: ``UPDATE … SET *_uuid = NULL``.

Revision ID: d97_backfill_uuid_columns
Down revision: d97_add_allowed_tenant_uuids
"""
from __future__ import annotations

from alembic import op


revision = "d97_backfill_uuid_columns"
down_revision = "d97_add_allowed_tenant_uuids"
branch_labels = None
depends_on = None


# (table, slug_column, uuid_column) — only tables/columns added by 029.
# Order is alphabetical by table for deterministic lock acquisition.
BACKFILL_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("audit_logs", "tenant_id", "tenant_uuid"),
    ("audit_retention_policy", "tenant_id", "tenant_uuid"),
    ("billing_overage_event", "tenant_id", "tenant_uuid"),
    ("billing_plan", "tenant_id", "tenant_uuid"),
    ("cross_tenant_share", "sharer_tenant_id", "sharer_tenant_uuid"),
    ("cross_tenant_share", "sharee_tenant_id", "sharee_tenant_uuid"),
    ("cross_tenant_share_acceptance", "accepted_by_tenant_id", "accepted_by_tenant_uuid"),
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
    ("platform_feature_flags", "company_id", "tenant_uuid"),
    ("resource_field_descriptors", "tenant_id", "tenant_uuid"),
    ("resource_instance", "tenant_id", "tenant_uuid"),
    ("resource_type", "owner_tenant_id", "owner_tenant_uuid"),
    ("sandbox_envs", "tenant_id", "tenant_uuid"),
    ("shadow_migration_checkpoint", "tenant_id", "tenant_uuid"),
    ("shadow_migration_drift", "tenant_id", "tenant_uuid"),
    ("shadow_migration_state", "tenant_id", "tenant_uuid"),
    ("shared_resources", "owner_tenant_id", "owner_tenant_uuid"),
    ("shares", "target_tenant_id", "target_tenant_uuid"),
    ("ss21_admin_consent_grants", "tenant_id", "tenant_uuid"),
    ("ss21_authorization_codes", "tenant_id", "tenant_uuid"),
    ("ss21_oauth_tokens", "tenant_id", "tenant_uuid"),
    ("ss21_webhook_subscriptions", "tenant_id", "tenant_uuid"),
    ("ss31_federation_provider", "tenant_id", "tenant_uuid"),
    ("sso_configs", "tenant_id", "tenant_uuid"),
    ("tenant_health_logs", "tenant_id", "tenant_uuid"),
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _table_has_columns(table: str, *cols: str) -> bool:
    bind = op.get_bind()
    rows = bind.exec_driver_sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    ).fetchall()
    have = {r[0] for r in rows}
    return all(c in have for c in cols)


def upgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()

    # ── 1. Backfill scalar UUID columns from slug JOINs ──────────────────
    for table, slug_col, uuid_col in BACKFILL_PAIRS:
        if not _table_has_columns(table, slug_col, uuid_col):
            # Lab/partial DBs may be missing the table; 029's guard
            # already skipped those. Same here.
            continue
        bind.exec_driver_sql(
            f"UPDATE {table} SET {uuid_col} = t.id "
            f"FROM tenants t WHERE t.slug = {table}.{slug_col} "
            f"AND {table}.{slug_col} IS NOT NULL "
            f"AND {table}.{uuid_col} IS NULL"
        )

    # ── 2. Backfill service_accounts.allowed_tenant_uuids JSON array ─────
    if _table_has_columns("service_accounts", "allowed_tenant_slugs", "allowed_tenant_uuids"):
        bind.exec_driver_sql(
            "UPDATE service_accounts sa "
            "SET allowed_tenant_uuids = ("
            "  SELECT COALESCE(json_agg(t.id ORDER BY ord), '[]'::json) "
            "  FROM json_array_elements_text(sa.allowed_tenant_slugs) "
            "       WITH ORDINALITY AS s(slug, ord) "
            "  JOIN tenants t ON t.slug = s.slug"
            ") "
            "WHERE sa.allowed_tenant_slugs IS NOT NULL "
            "  AND json_typeof(sa.allowed_tenant_slugs) = 'array' "
            "  AND sa.allowed_tenant_uuids IS NULL"
        )

    # ── 3. Hard assertions — fail loud on unresolvable slugs ─────────────
    for table, slug_col, uuid_col in BACKFILL_PAIRS:
        if not _table_has_columns(table, slug_col, uuid_col):
            continue
        row = bind.exec_driver_sql(
            f"SELECT count(*) FROM {table} "
            f"WHERE {slug_col} IS NOT NULL AND {uuid_col} IS NULL"
        ).fetchone()
        unresolved = row[0] if row else 0
        if unresolved:
            raise RuntimeError(
                f"D97 030 backfill: {table}.{uuid_col} has {unresolved} "
                f"unresolved row(s) where {slug_col} did not match any "
                f"tenants.slug. Investigate before retrying."
            )

    if _table_has_columns("service_accounts", "allowed_tenant_slugs", "allowed_tenant_uuids"):
        # Per-row length must match — silent slug drops would be invisible
        # without this check.
        row = bind.exec_driver_sql(
            "SELECT count(*) FROM service_accounts sa "
            "WHERE sa.allowed_tenant_slugs IS NOT NULL "
            "  AND json_typeof(sa.allowed_tenant_slugs) = 'array' "
            "  AND ("
            "    sa.allowed_tenant_uuids IS NULL "
            "    OR json_array_length(sa.allowed_tenant_uuids) "
            "       <> json_array_length(sa.allowed_tenant_slugs)"
            "  )"
        ).fetchone()
        mismatched = row[0] if row else 0
        if mismatched:
            raise RuntimeError(
                f"D97 030 backfill: service_accounts has {mismatched} row(s) "
                f"where allowed_tenant_uuids length does not match "
                f"allowed_tenant_slugs length. Some slugs did not resolve."
            )


def downgrade() -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    for table, _slug_col, uuid_col in BACKFILL_PAIRS:
        if not _table_has_columns(table, uuid_col):
            continue
        bind.exec_driver_sql(f"UPDATE {table} SET {uuid_col} = NULL")
    if _table_has_columns("service_accounts", "allowed_tenant_uuids"):
        bind.exec_driver_sql(
            "UPDATE service_accounts SET allowed_tenant_uuids = NULL"
        )
