"""Lead intake, call-back dates, estimate linking, and technician permission.

* leads.follow_up_date — DATE NULL, indexed.
* leads.estimate_id — UUID NULL, indexed.
* leads.origin_ref — VARCHAR(120) NULL.
* Grant leads.intake to technician seeded role snapshots.
* Seed default lead intake custom fields if definitions table exists.

Revision ID: 099_lead_intake
Revises: 098_planner_today
Create Date: 2026-09-28
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

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

_DEFAULT_LEAD_FIELDS = [
    {
        "field_key": "job_kind",
        "label": "Job kind",
        "field_type": "select",
        "options": json.dumps(["Repair", "New door", "New opener", "Door + opener"]),
        "required": False,
        "sort_order": 1,
    },
    {
        "field_key": "door_count",
        "label": "Door count",
        "field_type": "number",
        "options": None,
        "required": False,
        "sort_order": 2,
    },
    {
        "field_key": "door_size",
        "label": "Door size",
        "field_type": "text",
        "options": None,
        "required": False,
        "sort_order": 3,
    },
    {
        "field_key": "door_options",
        "label": "Door options",
        "field_type": "text",
        "options": None,
        "required": False,
        "sort_order": 4,
    },
    {
        "field_key": "opener",
        "label": "Opener",
        "field_type": "text",
        "options": None,
        "required": False,
        "sort_order": 5,
    },
]


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

    # Seed default lead custom field definitions if table exists
    if insp.has_table("custom_field_definitions"):
        # Discover tenant IDs
        tenant_ids: set[str] = set()
        if insp.has_table("leads"):
            rows = bind.execute(sa.text("SELECT DISTINCT company_id FROM leads WHERE company_id IS NOT NULL")).fetchall()
            for r in rows:
                if r[0]:
                    tenant_ids.add(str(r[0]))
        if not tenant_ids and insp.has_table("app_settings"):
            rows = bind.execute(sa.text("SELECT DISTINCT company_id FROM app_settings WHERE company_id IS NOT NULL")).fetchall()
            for r in rows:
                if r[0]:
                    tenant_ids.add(str(r[0]))
        if not tenant_ids and insp.has_table("tenant_roles"):
            rows = bind.execute(sa.text("SELECT DISTINCT company_id FROM tenant_roles WHERE company_id IS NOT NULL")).fetchall()
            for r in rows:
                if r[0]:
                    tenant_ids.add(str(r[0]))

        now = datetime.now(timezone.utc)
        for tid in sorted(tenant_ids):
            existing = bind.execute(
                sa.text(
                    "SELECT count(*) FROM custom_field_definitions "
                    "WHERE company_id = :tid AND entity_type = 'lead' AND deleted_at IS NULL"
                ),
                {"tid": tid},
            ).scalar()
            if existing == 0:
                for f in _DEFAULT_LEAD_FIELDS:
                    bind.execute(
                        sa.text(
                            "INSERT INTO custom_field_definitions "
                            "(id, company_id, entity_type, field_key, label, field_type, options, required, sort_order, created_at, updated_at) "
                            "VALUES (:id, :company_id, 'lead', :field_key, :label, :field_type, :options, :required, :sort_order, :created_at, :updated_at)"
                        ),
                        {
                            "id": str(uuid4()),
                            "company_id": tid,
                            "field_key": f["field_key"],
                            "label": f["label"],
                            "field_type": f["field_type"],
                            "options": f["options"],
                            "required": f["required"],
                            "sort_order": f["sort_order"],
                            "created_at": now,
                            "updated_at": now,
                        },
                    )


def downgrade() -> None:
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
