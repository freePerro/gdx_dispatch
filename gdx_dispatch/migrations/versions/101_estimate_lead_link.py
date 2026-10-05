"""Estimates carry their lead; a lead records which estimate counts as won.

* estimates.lead_id — UUID NULL, indexed (many estimates per lead).
* leads.selected_estimate_id — UUID NULL.
* Backfill estimates.lead_id from each live lead's estimate_id (the draft
  Start estimate made). Column to column, so SQLite's dashless-hex UUID
  storage round-trips; only where lead_id IS NULL, so it is idempotent. On
  the empty schema pave_tenant_db rebuilds on it updates nothing, and the
  reloaded rows bring their own lead_id.

Plan: docs/design/lead-to-paid-tracking-plan.md. No money rows touched.

Revision ID: 101_estimate_lead_link
Revises: 100_payment_confirm_senders
Create Date: 2026-09-29
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

log = logging.getLogger("alembic.runtime.migration")

revision = "101_estimate_lead_link"
down_revision = "100_payment_confirm_senders"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = inspect(bind)
    if not insp.has_table(table):
        return False
    return any(col["name"] == column for col in insp.get_columns(table))


def _has_index(bind, table: str, index_name: str) -> bool:
    insp = inspect(bind)
    if not insp.has_table(table):
        return False
    return any(ix.get("name") == index_name for ix in insp.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if insp.has_table("estimates"):
        if not _has_column(bind, "estimates", "lead_id"):
            with op.batch_alter_table("estimates") as batch:
                batch.add_column(sa.Column("lead_id", sa.Uuid(as_uuid=True), nullable=True))
        if not _has_index(bind, "estimates", "ix_estimates_lead_id"):
            op.create_index("ix_estimates_lead_id", "estimates", ["lead_id"])

    if insp.has_table("leads") and not _has_column(bind, "leads", "selected_estimate_id"):
        with op.batch_alter_table("leads") as batch:
            batch.add_column(sa.Column("selected_estimate_id", sa.Uuid(as_uuid=True), nullable=True))

    if (
        insp.has_table("estimates")
        and insp.has_table("leads")
        and _has_column(bind, "leads", "estimate_id")
    ):
        result = bind.execute(sa.text(
            "UPDATE estimates SET lead_id = ("
            "  SELECT l.id FROM leads l"
            "  WHERE l.estimate_id = estimates.id AND l.deleted_at IS NULL"
            "  ORDER BY l.created_at LIMIT 1"
            ") "
            "WHERE lead_id IS NULL AND EXISTS ("
            "  SELECT 1 FROM leads l"
            "  WHERE l.estimate_id = estimates.id AND l.deleted_at IS NULL"
            ")"
        ))
        log.info(f"101_estimate_lead_link: backfilled lead_id on {result.rowcount} estimate(s)")


def downgrade() -> None:
    """Drop the two columns and the index. The backfilled values are derived
    from leads.estimate_id, which stays, so nothing is lost."""
    bind = op.get_bind()
    insp = inspect(bind)
    if insp.has_table("estimates"):
        if _has_index(bind, "estimates", "ix_estimates_lead_id"):
            op.drop_index("ix_estimates_lead_id", table_name="estimates")
        if _has_column(bind, "estimates", "lead_id"):
            with op.batch_alter_table("estimates") as batch:
                batch.drop_column("lead_id")
    if _has_column(bind, "leads", "selected_estimate_id"):
        with op.batch_alter_table("leads") as batch:
            batch.drop_column("selected_estimate_id")
