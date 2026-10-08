"""Portal quote requests: a customer asks for new doors, by job, door by door.

* ``quote_requests`` — one per request: the customer and the portal user who
  sent it, the Lead it opened, a required job name, the site address, notes,
  and ``doors`` (a JSON list validated by ``modules/quote_requests/service.py``;
  ``doors_version`` names its shape), and when the customer last edited it
  (``edited_at``) or withdrew it (``withdrawn_at``). Soft-deleted via
  ``deleted_at``.
* ``quote_request_photos`` — photos of a door (``door_index`` into ``doors``),
  re-encoded on the way in like door-listing photos; ``deleted_at`` is set
  when the customer removes that door.

Existing rows: none — both tables are new and start empty. Nothing else is
touched.

Portability: ``op.create_table`` on both engines, guarded with ``has_table``.
Both tables are ORM-managed and ``create_all`` runs before alembic on boot,
so on a fresh install this is a no-op.

Rollback: ``downgrade()`` drops the photo table, then the request table.
Photo files under ``<UPLOAD_DIR>/<tenant>/quote_requests/`` stay on disk,
unreferenced. The Leads that requests opened stay and read as ordinary leads
whose notes name the job. On the next boot ``create_all`` recreates both
tables empty unless the models are removed too.

Revision ID: 112_quote_requests
Revises: 111_job_cancel_lifecycle
Create Date: 2026-10-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "112_quote_requests"
down_revision = "111_job_cancel_lifecycle"
branch_labels = None
depends_on = None

REQUESTS = "quote_requests"
PHOTOS = "quote_request_photos"


def upgrade() -> None:
    insp = inspect(op.get_bind())
    if not insp.has_table(REQUESTS):
        op.create_table(
            REQUESTS,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("customer_id", sa.Uuid(), nullable=False),
            sa.Column("submitted_by_user_id", sa.Uuid(), nullable=False),
            sa.Column("lead_id", sa.Uuid(), nullable=True),
            sa.Column("job_name", sa.String(200), nullable=False),
            sa.Column("site_address", sa.String(500), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("doors", sa.JSON(), nullable=False),
            sa.Column("doors_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_quote_requests_customer_id", REQUESTS, ["customer_id"])
        op.create_index("ix_quote_requests_lead_id", REQUESTS, ["lead_id"])
    if not insp.has_table(PHOTOS):
        op.create_table(
            PHOTOS,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("quote_request_id", sa.Uuid(), sa.ForeignKey(f"{REQUESTS}.id"), nullable=False),
            sa.Column("door_index", sa.Integer(), nullable=False),
            sa.Column("filename", sa.String(255), nullable=False),
            sa.Column("content_type", sa.String(80), nullable=False, server_default="image/jpeg"),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_quote_request_photos_quote_request_id", PHOTOS, ["quote_request_id"])


def downgrade() -> None:
    insp = inspect(op.get_bind())
    if insp.has_table(PHOTOS):
        op.drop_table(PHOTOS)
    if insp.has_table(REQUESTS):
        op.drop_table(REQUESTS)
