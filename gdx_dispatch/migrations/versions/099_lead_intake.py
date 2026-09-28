"""Lead intake, call-back dates, estimate linking, and technician permission.

* leads.follow_up_date — DATE NULL, indexed.
* leads.estimate_id — UUID NULL, indexed.
* leads.origin_ref — VARCHAR(120) NULL.
* Grant leads.intake to technician seeded role snapshots.
* The default intake fields are seeded by bootstrap_app, not here.

Revision ID: 099_lead_intake
Revises: 098_planner_today
Create Date: 2026-09-28
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from gdx_dispatch.migrations.grant_helpers import grant_permission_to_seeded_roles

log = logging.getLogger("alembic.runtime.migration")

revision = "099_lead_intake"
down_revision = "098_planner_today"
branch_labels = None
depends_on = None

_KEY = "leads.intake"
_ROLES = ("technician",)

def _has_column(bind, table: str, column: str) -> bool | None:
    insp = inspect(bind)
    if not insp.has_table(table):
        return None
    return any(col["name"] == column for col in insp.get_columns(table))


def _has_index(bind, table: str, index_name: str) -> bool:
    insp = inspect(bind)
    if not insp.has_table(table):
        return False
    return any(ix.get("name") == index_name for ix in insp.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if insp.has_table("leads"):
        with op.batch_alter_table("leads") as batch:
            if not _has_column(bind, "leads", "follow_up_date"):
                batch.add_column(sa.Column("follow_up_date", sa.Date(), nullable=True))
            if not _has_column(bind, "leads", "estimate_id"):
                batch.add_column(sa.Column("estimate_id", sa.Uuid(as_uuid=True), nullable=True))
            if not _has_column(bind, "leads", "origin_ref"):
                batch.add_column(sa.Column("origin_ref", sa.String(120), nullable=True))

        if not _has_index(bind, "leads", "ix_leads_follow_up_date"):
            op.create_index("ix_leads_follow_up_date", "leads", ["follow_up_date"])
        if not _has_index(bind, "leads", "ix_leads_estimate_id"):
            op.create_index("ix_leads_estimate_id", "leads", ["estimate_id"])

    # Deliver leads.intake permission to technician role
    granted = grant_permission_to_seeded_roles(bind, permission=_KEY, roles=_ROLES)
    log.info(f"099_lead_intake: granted {_KEY} to {granted} seeded role row(s)")

    # The default intake fields are NOT seeded here. pave_tenant_db rebuilds
    # a database by re-running `alembic upgrade head` on an empty schema and
    # then reloading the dumped rows, so a data seed in a migration collides
    # with its own reloaded copy (uq_custom_field_key). They are seeded by
    # bootstrap_app, which runs after migrations on every boot.


def downgrade() -> None:
    """Drop the three lead columns. The grant stays.

    The leads.intake grant is deliberately NOT revoked — upgrade grants only
    where the key is absent, so it cannot tell its own grants from a later
    tenant's BUILTIN seed or an operator's tick in Roles & Permissions (the
    reasoning in 029_customer_contact_write.downgrade).
    """
    bind = op.get_bind()
    insp = inspect(bind)

    if insp.has_table("leads"):
        if _has_index(bind, "leads", "ix_leads_estimate_id"):
            op.drop_index("ix_leads_estimate_id", table_name="leads")
        if _has_index(bind, "leads", "ix_leads_follow_up_date"):
            op.drop_index("ix_leads_follow_up_date", table_name="leads")
        with op.batch_alter_table("leads") as batch:
            if _has_column(bind, "leads", "origin_ref"):
                batch.drop_column("origin_ref")
            if _has_column(bind, "leads", "estimate_id"):
                batch.drop_column("estimate_id")
            if _has_column(bind, "leads", "follow_up_date"):
                batch.drop_column("follow_up_date")
