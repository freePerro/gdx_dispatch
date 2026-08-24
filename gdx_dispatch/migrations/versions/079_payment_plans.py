"""Payment plans become real tables + an off-by-default option (M39).

Doug 2026-08-24: "we don't do payment plans. but the option for it should be
there for it to be turned on and functional." The endpoint used to return a
plan_id that never existed.

Two ORM-managed new tables (050 pattern: create_orm_tables has already built
them on any booted deployment — raw CREATE TABLE IF NOT EXISTS so re-running
is a no-op) plus one app_settings column (067 pattern), default FALSE and
that default is load-bearing for this deployment.

Revision ID: 079_payment_plans
Revises: 078_vendor_invariant_column
"""
import contextlib

from alembic import op

revision = "079_payment_plans"
down_revision = "078_vendor_invariant_column"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS payment_plans (
            id               uuid PRIMARY KEY,
            invoice_id       uuid NOT NULL REFERENCES invoices(id),
            status           varchar(20) NOT NULL DEFAULT 'active',
            num_installments integer NOT NULL,
            total_amount     numeric(12,2) NOT NULL,
            start_date       date NOT NULL,
            created_by       varchar(64),
            created_at       timestamptz NOT NULL,
            cancelled_at     timestamptz,
            cancelled_by     varchar(64)
        );
        """
        if bind.dialect.name == "postgresql"
        else
        """
        CREATE TABLE IF NOT EXISTS payment_plans (
            id               char(32) PRIMARY KEY,
            invoice_id       char(32) NOT NULL REFERENCES invoices(id),
            status           varchar(20) NOT NULL DEFAULT 'active',
            num_installments integer NOT NULL,
            total_amount     numeric(12,2) NOT NULL,
            start_date       date NOT NULL,
            created_by       varchar(64),
            created_at       timestamp NOT NULL,
            cancelled_at     timestamp,
            cancelled_by     varchar(64)
        );
        """
    )
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS payment_plan_installments (
            id        uuid PRIMARY KEY,
            plan_id   uuid NOT NULL REFERENCES payment_plans(id),
            seq       integer NOT NULL,
            due_date  date NOT NULL,
            amount    numeric(12,2) NOT NULL,
            status    varchar(20) NOT NULL DEFAULT 'pending'
        );
        """
        if bind.dialect.name == "postgresql"
        else
        """
        CREATE TABLE IF NOT EXISTS payment_plan_installments (
            id        char(32) PRIMARY KEY,
            plan_id   char(32) NOT NULL REFERENCES payment_plans(id),
            seq       integer NOT NULL,
            due_date  date NOT NULL,
            amount    numeric(12,2) NOT NULL,
            status    varchar(20) NOT NULL DEFAULT 'pending'
        );
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_payment_plans_invoice_id ON payment_plans (invoice_id);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_payment_plan_installments_plan_id ON payment_plan_installments (plan_id);"
    )

    if bind.dialect.name != "postgresql":
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "ALTER TABLE app_settings ADD COLUMN payment_plans_enabled boolean NOT NULL DEFAULT false;"
            )
        return
    bind.exec_driver_sql(
        """
        DO $$ BEGIN
            IF to_regclass('app_settings') IS NOT NULL THEN
                ALTER TABLE app_settings
                    ADD COLUMN IF NOT EXISTS payment_plans_enabled boolean NOT NULL DEFAULT false;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Additive and false-safe for older code — keep on downgrade.
    pass
