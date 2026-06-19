"""D97 sibling — add ``service_accounts.allowed_tenant_uuids JSON NULL``.

Sibling shape-migration to 029. ``service_accounts.allowed_tenant_slugs``
stores a JSON array of tenant slugs; D97's UUID-as-identity rule requires
a parallel UUID-shaped column. Additive only — readers/writers keep using
``allowed_tenant_slugs``. 030 backfills via JOIN; 031 swaps readers
(``service_accounts.py``, ``service_account_mint.py``,
``migrate_service_accounts.py``) to read ``allowed_tenant_uuids`` and
drops the slug column.

Independent of 029 — order doesn't matter; both are additive nullable.

Revision ID: d97_add_allowed_tenant_uuids
Down revision: d97_add_uuid_columns
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "d97_add_allowed_tenant_uuids"
down_revision = "d97_add_uuid_columns"
branch_labels = None
depends_on = None


TABLE = "service_accounts"
NEW_COL = "allowed_tenant_uuids"


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
    if not _table_exists(TABLE):
        return
    if _column_exists(TABLE, NEW_COL):
        return
    op.add_column(TABLE, sa.Column(NEW_COL, sa.JSON, nullable=True))


def downgrade() -> None:
    if not _is_postgres():
        return
    if not _table_exists(TABLE):
        return
    if not _column_exists(TABLE, NEW_COL):
        return
    op.drop_column(TABLE, NEW_COL)
