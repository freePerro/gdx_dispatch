"""Per-tenant billing terms (UX audit F-36)

Default payment-terms days, plus per-pricing-class defaults
(contractor / retail / wholesale), plus early-pay discount, late fee,
and interest configuration. The daily late-fee + interest application
is a follow-up sprint — the column shape lands today so customers
created from now on get a sane non-NULL due_date and the dashboard
"Overdue Invoices" tile stops silently undercounting.

Revision ID: 042_billing_terms
Revises: 041_server_errors
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "042_billing_terms"
down_revision = "041_server_errors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column("default_payment_terms_days", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column("tenant_settings", sa.Column("contractor_payment_terms_days", sa.Integer(), nullable=True))
    op.add_column("tenant_settings", sa.Column("retail_payment_terms_days", sa.Integer(), nullable=True))
    op.add_column("tenant_settings", sa.Column("wholesale_payment_terms_days", sa.Integer(), nullable=True))
    op.add_column("tenant_settings", sa.Column("early_pay_discount_percent", sa.Numeric(5, 4), nullable=True))
    op.add_column("tenant_settings", sa.Column("early_pay_discount_days", sa.Integer(), nullable=True))
    op.add_column("tenant_settings", sa.Column("late_fee_flat_amount", sa.Numeric(10, 2), nullable=True))
    op.add_column("tenant_settings", sa.Column("late_fee_percent", sa.Numeric(5, 4), nullable=True))
    op.add_column(
        "tenant_settings",
        sa.Column("late_fee_grace_days", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("tenant_settings", sa.Column("interest_rate_monthly_percent", sa.Numeric(5, 4), nullable=True))
    op.add_column(
        "tenant_settings",
        sa.Column("interest_grace_days", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    for col in (
        "interest_grace_days",
        "interest_rate_monthly_percent",
        "late_fee_grace_days",
        "late_fee_percent",
        "late_fee_flat_amount",
        "early_pay_discount_days",
        "early_pay_discount_percent",
        "wholesale_payment_terms_days",
        "retail_payment_terms_days",
        "contractor_payment_terms_days",
        "default_payment_terms_days",
    ):
        op.drop_column("tenant_settings", col)
