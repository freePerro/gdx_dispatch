"""D97 Phase 0a — swap slug → UUID columns and refresh RLS policies.

Step 3 of the slug→UUID column-shape reconciliation. For every table
listed in 029's ``SHADOW_COLUMNS``:

  * DROP its tenant-isolation RLS policy (re-created at the end after the
    column types align with the GUC type).
  * DROP the old slug column.
  * RENAME the ``*_uuid`` shadow column to its canonical name (typically
    ``tenant_id`` — same name the slug column had — with two exceptions
    documented in ``CANONICAL_NAMES`` below).
  * SET NOT NULL on the new column iff the old slug column was NOT NULL.
  * ADD FOREIGN KEY to ``tenants(id)`` with action chosen per table:
      - ``CASCADE`` for tenant-scoped operational tables (default).
      - ``SET NULL`` for audit/log tables that should outlive tenant
        deletion for compliance.
      - No FK at all for ``platform_consumer_audit`` (per runbook: 6
        orphan rows reference a deleted tenant; append-only audit trail
        must survive tenant deletion).

Plus three table-specific operations:

  * ``tenants.parent_tenant_id`` DROP COLUMN — dead column, zero active
    writers (an earlier session audit + an earlier session fixture cleanup).
  * ``platform_consumer_audit.tenant_id`` ALTER COLUMN TYPE uuid USING
    ``tenant_id::uuid`` — data is already UUID-shape strings; no shadow
    column was needed.
  * ``service_accounts.allowed_tenant_slugs`` DROP COLUMN — readers were
    flipped in an earlier session (cf07b7f2) to prefer ``allowed_tenant_uuids``;
    the slug column has no remaining writers.

Then the RLS layer is fully refreshed:

  * Every policy in ``control_plane_rls_targets.{TEXT_TENANT_TABLES,
    UUID_TENANT_TABLES, OWNER_TENANT_TABLES}`` is dropped and re-created.
  * ``control_plane_rls_targets.py`` is updated in the same commit so all
    the formerly-text tenant columns now live in ``UUID_TENANT_TABLES``;
    ``policy_sql`` re-renders with ``::text`` cast on the UUID column so
    it compares cleanly against ``current_setting('app.tenant_id', true)``
    (which always returns text).

Operational guards:

  * ``SET LOCAL statement_timeout = 0`` — large ALTERs get as long as
    they need; never timeout mid-DDL.
  * ``SET LOCAL lock_timeout = '5s'`` — fail fast if a competing lock
    holds us back; better to retry than queue indefinitely.
  * Tables locked in deterministic alphabetical order — minimizes
    deadlock risk against any concurrent reader.

Post-deploy operational steps (NOT in this migration; see runbook):

  1. ``VACUUM (ANALYZE) platform_consumer_audit;`` (one-shot, after
     the type-cast ALTERs the underlying rows in place).
  2. App rolling restart — pgbouncer cached plans referenced the old
     column types and must be invalidated.
  3. PAT/SCIM token revocation script (7 service-account tokens at
     last count).

Reversible: opposite swap. Re-create slug column, re-populate from JOIN
on ``tenants(id, slug)``, drop UUID column, restore old policies.
Lossy if any slug-only data was written between up/down — same caveat
as any column-type migration.

Revision ID: d97_swap_uuid_columns
Down revision: d97_backfill_uuid_columns
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "d97_swap_uuid_columns"
down_revision = "d97_backfill_uuid_columns"
branch_labels = None
depends_on = None


# ── Swap plan ─────────────────────────────────────────────────────────────
#
# Each entry: (table, old_slug_col, new_uuid_col, canonical_name, fk_action).
#
# canonical_name is the column name AFTER the rename. Almost always
# equal to old_slug_col; the two exceptions are platform_feature_flags
# (``company_id`` → ``tenant_id``) and the cousin columns (whose canonical
# names drop the trailing ``_id`` since the shadow has ``_uuid`` and the
# desired final form is the original cousin name).
#
# fk_action:
#   "CASCADE"  — tenant delete cascades to this table.
#   "SET NULL" — preserve row, blank tenant_id on tenant delete.
#   None       — no FK (platform_consumer_audit only).
#
# Order: alphabetical by table for deterministic lock acquisition.

SWAP_PLAN: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("audit_logs", "tenant_id", "tenant_uuid", "tenant_id", "SET NULL"),
    ("audit_retention_policy", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("billing_overage_event", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("billing_plan", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("cross_tenant_share", "sharer_tenant_id", "sharer_tenant_uuid", "sharer_tenant_id", "CASCADE"),
    ("cross_tenant_share", "sharee_tenant_id", "sharee_tenant_uuid", "sharee_tenant_id", "CASCADE"),
    ("cross_tenant_share_acceptance", "accepted_by_tenant_id", "accepted_by_tenant_uuid", "accepted_by_tenant_id", "CASCADE"),
    ("cutover_schedule", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("deprecated_table_record", "tenant_id", "tenant_uuid", "tenant_id", "SET NULL"),
    ("event_outbox", "tenant_id", "tenant_uuid", "tenant_id", "SET NULL"),
    ("game_definitions", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("game_state", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("installations", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("locations", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("mcp_execution_log", "tenant_id", "tenant_uuid", "tenant_id", "SET NULL"),
    ("mcp_tool_execution_audit", "tenant_id", "tenant_uuid", "tenant_id", "SET NULL"),
    ("memberships", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("metering_checkpoint", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("metering_usage", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    # platform_feature_flags: drop company_id entirely, rename shadow → tenant_id.
    ("platform_feature_flags", "company_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("resource_field_descriptors", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("resource_instance", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("resource_type", "owner_tenant_id", "owner_tenant_uuid", "owner_tenant_id", "CASCADE"),
    ("sandbox_envs", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("shadow_migration_checkpoint", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("shadow_migration_drift", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("shadow_migration_state", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("shared_resources", "owner_tenant_id", "owner_tenant_uuid", "owner_tenant_id", "CASCADE"),
    ("shares", "target_tenant_id", "target_tenant_uuid", "target_tenant_id", "CASCADE"),
    ("ss21_admin_consent_grants", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("ss21_authorization_codes", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("ss21_oauth_tokens", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("ss21_webhook_subscriptions", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("ss31_federation_provider", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("sso_configs", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
    ("tenant_health_logs", "tenant_id", "tenant_uuid", "tenant_id", "CASCADE"),
)


# Tables whose RLS policy must be DROPped before the column drop and
# CREATEd again after the rename. Pulled from
# control_plane_rls_targets at module load to keep the two in sync.
def _rls_target_tables() -> list[str]:
    from gdx_dispatch.migrations._rls_frozen import (
        OWNER_TENANT_TABLES,
        TEXT_TENANT_TABLES,
        UUID_TENANT_TABLES,
    )
    seen: list[str] = []
    for t in (*TEXT_TENANT_TABLES, *UUID_TENANT_TABLES, *OWNER_TENANT_TABLES):
        if t not in seen:
            seen.append(t)
    return sorted(seen)


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


def _column_is_not_null(table: str, column: str) -> bool:
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, column),
    ).fetchone()
    if not row:
        return False
    return row[0] == "NO"


def _column_type(table: str, column: str) -> str | None:
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, column),
    ).fetchone()
    return row[0] if row else None


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute("SET LOCAL statement_timeout = 0")
    op.execute("SET LOCAL lock_timeout = '5s'")

    # ── 1. DROP every existing tenant-isolation RLS policy ──────────────────
    # The column type is about to change. Some predicates compare a varchar
    # column against a text GUC; after the swap, columns will be UUID and the
    # policy must be re-rendered with ``::text`` cast. Cleanest path: drop
    # them all up front, recreate at the end.
    for t in _rls_target_tables():
        if _table_exists(t):
            op.execute(f"DROP POLICY IF EXISTS {t}_tenant_isolation ON {t};")

    # Commerce-plane policies (migration 025) reference the slug columns
    # 031 is about to drop. Drop them up front; recreate at the end.
    if _table_exists("cross_tenant_share"):
        op.execute(
            "DROP POLICY IF EXISTS cross_tenant_share_parties_read "
            "ON cross_tenant_share;"
        )
        op.execute(
            "DROP POLICY IF EXISTS cross_tenant_share_sharer_write "
            "ON cross_tenant_share;"
        )
    if _table_exists("cross_tenant_share_acceptance"):
        op.execute(
            "DROP POLICY IF EXISTS cross_tenant_share_acceptance_accepter_only "
            "ON cross_tenant_share_acceptance;"
        )

    # ── 2. Per-table swap (alphabetical lock order) ─────────────────────────
    for table, old_col, uuid_col, canonical, fk_action in SWAP_PLAN:
        if not _table_exists(table):
            # Lab DBs without full pave skip the table — the next create_all
            # will produce the canonical UUID column directly.
            continue
        if not _column_exists(table, uuid_col):
            # 029 didn't add the shadow (e.g. table absent at 029 time).
            # Nothing to swap; skip.
            continue

        old_exists = _column_exists(table, old_col)
        canonical_exists_pre = _column_exists(table, canonical)
        not_null = old_exists and _column_is_not_null(table, old_col)

        # Special case: 029's SHADOW_COLUMNS named an ``old_col`` that
        # doesn't exist on this DB AND the ``canonical`` column already
        # exists as uuid. (Discovered an earlier session on lab:
        # platform_feature_flags has ``tenant_id`` uuid directly; the
        # 029 entry for ``company_id → tenant_uuid`` was based on a
        # stale assumption.) The shadow is dead weight; drop it and
        # treat the existing canonical as the source of truth.
        if not old_exists and canonical_exists_pre and uuid_col != canonical:
            if _column_type(table, canonical) == "uuid":
                op.execute(f'ALTER TABLE {table} DROP COLUMN "{uuid_col}"')
            else:
                raise RuntimeError(
                    f"D97 031: {table}.{old_col} missing, {canonical} exists "
                    f"but is not uuid (got {_column_type(table, canonical)}). "
                    f"Migration assumes either a swap shape or a pre-existing "
                    f"uuid column. Investigate before retrying."
                )
        else:
            # 2a. Drop the slug column (if still present).
            if old_exists:
                # Drop indexes that referenced the old column — Postgres
                # will drop column-attached indexes automatically with
                # the column, so this is implicit.
                op.execute(f'ALTER TABLE {table} DROP COLUMN "{old_col}"')

            # 2b. Rename shadow → canonical name (only if a rename is needed).
            if uuid_col != canonical:
                # If a same-named column already exists (e.g. partial
                # re-run), bail loudly — re-running on a half-applied
                # DB needs human eyes.
                if _column_exists(table, canonical):
                    raise RuntimeError(
                        f"D97 031: cannot rename {table}.{uuid_col} -> "
                        f"{canonical} because {canonical} already exists. "
                        f"Migration state is inconsistent; investigate "
                        f"before retrying."
                    )
                op.execute(
                    f'ALTER TABLE {table} RENAME COLUMN "{uuid_col}" '
                    f'TO "{canonical}"'
                )

        # 2c. Apply NOT NULL if the original slug column had it.
        if not_null:
            op.execute(
                f'ALTER TABLE {table} ALTER COLUMN "{canonical}" SET NOT NULL'
            )

        # 2d. Foreign key.
        if fk_action is not None:
            fk_name = f"fk_{table}_{canonical}_tenants"
            # Drop any pre-existing FK by the same name (idempotent re-runs).
            op.execute(
                f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{fk_name}"'
            )
            op.execute(
                f'ALTER TABLE {table} ADD CONSTRAINT "{fk_name}" '
                f'FOREIGN KEY ("{canonical}") REFERENCES tenants(id) '
                f'ON DELETE {fk_action}'
            )

    # ── 3. tenants.parent_tenant_id — drop dead column ─────────────────────
    if _column_exists("tenants", "parent_tenant_id"):
        op.execute('ALTER TABLE tenants DROP COLUMN "parent_tenant_id"')

    # ── 4. platform_consumer_audit — direct type cast (no shadow) ──────────
    if _column_exists("platform_consumer_audit", "tenant_id"):
        col_type = _column_type("platform_consumer_audit", "tenant_id")
        if col_type and col_type != "uuid":
            op.execute(
                "ALTER TABLE platform_consumer_audit "
                "ALTER COLUMN tenant_id TYPE uuid USING tenant_id::uuid"
            )
            # Per runbook: NO foreign key. Append-only audit logs must
            # survive tenant deletion for compliance; 6 orphan rows on
            # prod (curl probe 2026-04-22) reference an already-deleted
            # tenant and would block FK enforcement.

    # ── 5. service_accounts.allowed_tenant_slugs — drop legacy column ──────
    if _column_exists("service_accounts", "allowed_tenant_slugs"):
        op.execute(
            'ALTER TABLE service_accounts DROP COLUMN "allowed_tenant_slugs"'
        )

    # ── 6. Re-render every RLS policy ──────────────────────────────────────
    # Late import: the module re-classifies tables as UUID-typed (the
    # in-tree commit alongside this migration moves them).
    from gdx_dispatch.migrations._rls_frozen import (
        OWNER_TENANT_TABLES,
        TEXT_TENANT_TABLES,
        UUID_TENANT_TABLES,
        policy_sql,
    )

    for t in TEXT_TENANT_TABLES:
        if _table_exists(t):
            op.execute(policy_sql(t, "tenant_id"))
    for t in UUID_TENANT_TABLES:
        if _table_exists(t):
            op.execute(policy_sql(t, "tenant_id", "::text"))
    for t in OWNER_TENANT_TABLES:
        if _table_exists(t):
            op.execute(policy_sql(t, "owner_tenant_id", "::text"))

    # Commerce-plane policies — re-render with ::text casts (the SQL
    # generators in commerce_plane_rls_targets are updated in the same
    # commit).
    from gdx_dispatch.migrations._rls_frozen import (
        cross_tenant_share_acceptance_policy_sql,
        cross_tenant_share_policy_sql,
    )

    if _table_exists("cross_tenant_share"):
        op.execute(cross_tenant_share_policy_sql())
    if _table_exists("cross_tenant_share_acceptance"):
        op.execute(cross_tenant_share_acceptance_policy_sql())


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute("SET LOCAL statement_timeout = 0")
    op.execute("SET LOCAL lock_timeout = '5s'")

    # Drop every RLS policy first — column types are about to change back.
    for t in _rls_target_tables():
        if _table_exists(t):
            op.execute(f"DROP POLICY IF EXISTS {t}_tenant_isolation ON {t};")

    # Commerce-plane policies reference the columns we're about to rename
    # back to slug; drop them too.
    if _table_exists("cross_tenant_share"):
        op.execute(
            "DROP POLICY IF EXISTS cross_tenant_share_parties_read "
            "ON cross_tenant_share;"
        )
        op.execute(
            "DROP POLICY IF EXISTS cross_tenant_share_sharer_write "
            "ON cross_tenant_share;"
        )
    if _table_exists("cross_tenant_share_acceptance"):
        op.execute(
            "DROP POLICY IF EXISTS cross_tenant_share_acceptance_accepter_only "
            "ON cross_tenant_share_acceptance;"
        )

    # Reverse the cousin / canonical renames + FK + NOT NULL + drop.
    for table, old_col, uuid_col, canonical, fk_action in SWAP_PLAN:
        if not _table_exists(table):
            continue
        if not _column_exists(table, canonical):
            continue

        # Drop FK first.
        if fk_action is not None:
            fk_name = f"fk_{table}_{canonical}_tenants"
            op.execute(
                f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{fk_name}"'
            )

        # Drop NOT NULL (we don't know whether downgrade target row state
        # supports NOT NULL anymore — be permissive on rollback).
        op.execute(
            f'ALTER TABLE {table} ALTER COLUMN "{canonical}" DROP NOT NULL'
        )

        # Rename canonical back to shadow if they differ.
        if canonical != uuid_col:
            op.execute(
                f'ALTER TABLE {table} RENAME COLUMN "{canonical}" TO "{uuid_col}"'
            )

        # Re-add the slug column (best-effort backfill from tenants).
        # Use varchar(100) — superset of the historical varchar(64)/(100)
        # mix; the underlying column was always varchar.
        if not _column_exists(table, old_col):
            op.execute(
                f'ALTER TABLE {table} ADD COLUMN "{old_col}" varchar(100) NULL'
            )
            op.execute(
                f"UPDATE {table} SET {old_col} = t.slug "
                f"FROM tenants t WHERE t.id = {table}.{uuid_col} "
                f"AND {table}.{uuid_col} IS NOT NULL"
            )

    # tenants.parent_tenant_id — restore the dead column as nullable text.
    if not _column_exists("tenants", "parent_tenant_id"):
        op.execute(
            'ALTER TABLE tenants ADD COLUMN "parent_tenant_id" varchar(100) NULL'
        )

    # platform_consumer_audit — cast back to varchar.
    if _column_exists("platform_consumer_audit", "tenant_id"):
        col_type = _column_type("platform_consumer_audit", "tenant_id")
        if col_type == "uuid":
            op.execute(
                "ALTER TABLE platform_consumer_audit "
                "ALTER COLUMN tenant_id TYPE varchar(64) USING tenant_id::text"
            )

    # service_accounts.allowed_tenant_slugs — re-add as JSON, repopulate.
    if not _column_exists("service_accounts", "allowed_tenant_slugs"):
        op.execute(
            'ALTER TABLE service_accounts ADD COLUMN "allowed_tenant_slugs" json NULL'
        )
        op.execute(
            "UPDATE service_accounts sa "
            "SET allowed_tenant_slugs = ("
            "  SELECT COALESCE(json_agg(t.slug ORDER BY ord), '[]'::json) "
            "  FROM json_array_elements_text(sa.allowed_tenant_uuids::json) "
            "       WITH ORDINALITY AS s(uuid_str, ord) "
            "  JOIN tenants t ON t.id = s.uuid_str::uuid"
            ") "
            "WHERE sa.allowed_tenant_uuids IS NOT NULL"
        )

    # Re-create RLS policies from the (post-rollback) module state. The
    # commit that lands 031 also flips the module's TEXT/UUID lists; on
    # downgrade, that flip is reversed by reverting the commit, so this
    # import resolves to whichever version of the module is on disk at
    # downgrade time.
    from gdx_dispatch.migrations._rls_frozen import (
        OWNER_TENANT_TABLES,
        TEXT_TENANT_TABLES,
        UUID_TENANT_TABLES,
        policy_sql,
    )

    for t in TEXT_TENANT_TABLES:
        if _table_exists(t):
            op.execute(policy_sql(t, "tenant_id"))
    for t in UUID_TENANT_TABLES:
        if _table_exists(t):
            op.execute(policy_sql(t, "tenant_id", "::text"))
    for t in OWNER_TENANT_TABLES:
        if _table_exists(t):
            op.execute(policy_sql(t, "owner_tenant_id", "::text"))

    # Commerce-plane policies — re-render against the (now-rolled-back)
    # slug columns. The generators in commerce_plane_rls_targets reflect
    # whichever module-state is on disk at downgrade time.
    from gdx_dispatch.migrations._rls_frozen import (
        cross_tenant_share_acceptance_policy_sql,
        cross_tenant_share_policy_sql,
    )

    if _table_exists("cross_tenant_share"):
        op.execute(cross_tenant_share_policy_sql())
    if _table_exists("cross_tenant_share_acceptance"):
        op.execute(cross_tenant_share_acceptance_policy_sql())
