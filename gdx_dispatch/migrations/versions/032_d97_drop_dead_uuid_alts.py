"""D97 Phase 0a — defensive cleanup of any surviving ``*_uuid`` shadow columns.

Step 4 of the slug→UUID column-shape reconciliation. Catches any
``tenant_uuid`` / ``sharer_tenant_uuid`` / ``sharee_tenant_uuid`` /
``accepted_by_tenant_uuid`` / ``owner_tenant_uuid`` / ``target_tenant_uuid``
column on a control-plane table that survived 031.

031 already drops every shadow column it can reach — either by RENAME
(``*_uuid`` → canonical) or explicit DROP (the platform_feature_flags
edge case). The only way a shadow survives 031 is the gap between 029
and 031 in table presence: 029 added the shadow when the table existed,
the table was then dropped or the column was orphaned by some other
out-of-tree migration, and 031 saw a missing or partial state and skipped.
This migration is the defensive sweep.

In practice, on lab + prod (an earlier session), 031 left zero leftovers. This
migration runs as a no-op there. Keeping it as a fence-post against
future drift.

Reversible: the downgrade is itself a no-op (we don't know what the
shadow column was, so we can't re-add it; if a downgrade caller needs
the shadow back, downgrade through 031 first which has the SHADOW_COLUMNS
catalog).

Revision ID: d97_drop_dead_uuid_alts
Down revision: d97_swap_uuid_columns
"""
from __future__ import annotations

from alembic import op


revision = "d97_drop_dead_uuid_alts"
down_revision = "d97_swap_uuid_columns"
branch_labels = None
depends_on = None


# Suffix patterns that 029 used for shadow columns. Anything matching
# any of these on a public-schema table at this point is dead and gets
# dropped. The non-shadow canonical names (``tenant_id``, ``sharer_tenant_id``
# etc.) are not in this list — those are the live columns we want to
# keep.
SHADOW_SUFFIXES: tuple[str, ...] = (
    "tenant_uuid",
    "sharer_tenant_uuid",
    "sharee_tenant_uuid",
    "accepted_by_tenant_uuid",
    "owner_tenant_uuid",
    "target_tenant_uuid",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()
    rows = bind.exec_driver_sql(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND column_name = ANY(%s)",
        (list(SHADOW_SUFFIXES),),
    ).fetchall()

    for table, column in rows:
        op.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')


def downgrade() -> None:
    # No-op by design. We don't know which (table, column) pairs were
    # dropped (any combination of 6 suffixes × any subset of public tables),
    # and re-adding a nullable uuid column without a backfill plan would
    # leave the DB in a state alembic upgrade can't reason about.
    #
    # If you need a shadow column back at this point, downgrade through
    # 031 (``alembic downgrade d97_backfill_uuid_columns``) which uses
    # the SHADOW_COLUMNS catalog to re-create the per-table shape.
    return
