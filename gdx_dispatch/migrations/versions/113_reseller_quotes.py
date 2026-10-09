"""Contractor resale quotes: a reseller's brand, and the quotes they resell.

* ``reseller_profiles`` — one per customer: company name and contact lines,
  a minted logo filename, their own terms, a default markup, and which
  version of the privacy disclaimer they agreed to, when and by whom.
* ``resale_quotes`` — one per branded quote: the estimate it resells, the
  markup, our price and theirs, and a frozen JSON snapshot of the marked-up
  lines the PDF prints. Soft-deleted via ``deleted_at``.

Existing rows: none — both tables are new and start empty. Nothing else is
touched; no invoice, ledger, AR or estimate row is read-modified.

Portability: ``op.create_table`` on both engines, guarded with ``has_table``.
Both tables are ORM-managed and ``create_all`` runs before alembic on boot,
so on a fresh install this is a no-op.

Rollback: ``downgrade()`` drops ``resale_quotes``, then ``reseller_profiles``.
Logo files under ``<UPLOAD_DIR>/reseller/`` stay on disk, unreferenced and
unserved. On the next boot ``create_all`` recreates both tables empty unless
the models are removed too.

Revision ID: 113_reseller_quotes
Revises: 112_quote_requests
Create Date: 2026-10-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "113_reseller_quotes"
down_revision = "112_quote_requests"
branch_labels = None
depends_on = None

PROFILES = "reseller_profiles"
QUOTES = "resale_quotes"


def upgrade() -> None:
    insp = inspect(op.get_bind())
    if not insp.has_table(PROFILES):
        op.create_table(
            PROFILES,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("customer_id", sa.Uuid(), nullable=False, unique=True),
            sa.Column("company_name", sa.String(200), nullable=True),
            sa.Column("phone", sa.String(50), nullable=True),
            sa.Column("email", sa.String(200), nullable=True),
            sa.Column("address", sa.String(500), nullable=True),
            sa.Column("website", sa.String(200), nullable=True),
            sa.Column("license_no", sa.String(100), nullable=True),
            sa.Column("logo_file", sa.String(255), nullable=True),
            sa.Column("terms_text", sa.Text(), nullable=True),
            sa.Column("default_markup_pct", sa.Numeric(6, 2), nullable=False, server_default="0"),
            sa.Column("disclaimer_version", sa.String(32), nullable=True),
            sa.Column("disclaimer_accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("disclaimer_accepted_by", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not insp.has_table(QUOTES):
        op.create_table(
            QUOTES,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("customer_id", sa.Uuid(), nullable=False),
            sa.Column("estimate_id", sa.Uuid(), nullable=False),
            sa.Column("created_by", sa.Uuid(), nullable=False),
            sa.Column("reference", sa.String(60), nullable=False),
            sa.Column("end_customer_name", sa.String(200), nullable=True),
            sa.Column("end_customer_address", sa.String(500), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("markup_pct", sa.Numeric(6, 2), nullable=False),
            sa.Column("base_subtotal", sa.Numeric(12, 2), nullable=True),
            sa.Column("resale_subtotal", sa.Numeric(12, 2), nullable=True),
            sa.Column("lines_snapshot", sa.JSON(), nullable=False),
            sa.Column("hide_line_prices", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_resale_quotes_customer_id", QUOTES, ["customer_id"])
        op.create_index("ix_resale_quotes_estimate_id", QUOTES, ["estimate_id"])


def downgrade() -> None:
    insp = inspect(op.get_bind())
    if insp.has_table(QUOTES):
        op.drop_table(QUOTES)
    if insp.has_table(PROFILES):
        op.drop_table(PROFILES)
