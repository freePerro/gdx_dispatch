"""Backfill tenant_subscriptions from Tenant.stripe_* columns.

This migration populates the new tenant_subscriptions table using data
currently residing in the tenants table. It is gated by the
CC_V2_BACKFILL_ENABLED environment variable to prevent accidental
data movement in environments where it is not intended.
"""

import os
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "058_cc_data_backfill"
down_revision = "057_cc_postgres_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Perform the data backfill if CC_V2_BACKFILL_ENABLED is 'true'."""
    if os.environ.get("CC_V2_BACKFILL_ENABLED", "false") != "true":
        print(
            "CC_V2_BACKFILL_ENABLED!=true; skipping data copy. "
            "Re-run alembic upgrade head with the env var set to perform backfill."
        )
        return

    bind = op.get_bind()

    # 1. Look up the starter plan ID
    plan_query = sa.text("SELECT id FROM subscription_plans WHERE code = :code")
    starter_plan_id = bind.execute(plan_query, {"code": "starter"}).scalar()

    if not starter_plan_id:
        raise RuntimeError(
            "subscription_plans 'starter' missing — cc2-s03 must have run successfully"
        )

    # 2. Fetch tenants to backfill
    tenant_query = sa.text(
        """
        SELECT id AS tenant_id,
               stripe_customer_id,
               stripe_subscription_id,
               subscription_status,
               trial_ends_at
        FROM tenants
        WHERE deleted_at IS NULL
        """
    )
    tenants = bind.execute(tenant_query).fetchall()

    # 3. Iterate and insert
    inserted_count = 0
    insert_query = sa.text(
        """
        INSERT INTO tenant_subscriptions (
            tenant_id,
            plan_id,
            stripe_customer_id,
            stripe_subscription_id,
            billing_cycle,
            status,
            trial_ends_at,
            delinquent,
            days_overdue,
            churn_risk_score,
            created_at,
            updated_at
        )
        VALUES (
            :tenant_id,
            :plan_id,
            :stripe_customer_id,
            :stripe_subscription_id,
            :billing_cycle,
            :status,
            :trial_ends_at,
            :delinquent,
            :days_overdue,
            :churn_risk_score,
            now(),
            now()
        )
        ON CONFLICT (tenant_id) DO NOTHING
        """
    )

    for row in tenants:
        # Map status
        raw_status = row.subscription_status
        if raw_status == "trialing":
            status = "trialing"
        elif raw_status == "active":
            status = "active"
        elif raw_status == "past_due":
            status = "past_due"
        elif raw_status in ("canceled", "cancelled"):
            status = "canceled"
        elif raw_status == "unpaid":
            status = "unpaid"
        else:
            status = "incomplete"

        # Check if insertion actually happened (via rowcount on the individual execute)
        # However, with ON CONFLICT DO NOTHING, we check if the tenant_id already exists
        # in the new table to determine if this is a new row.
        # To keep logic simple and performant, we just use the query.
        # We'll count how many rows the bind actually affected if we weren't using ON CONFLICT.
        # Since we are using ON CONFLICT, we check for existence first or count post-hoc.
        # For the sake of this migration's logging, we'll use a simpler approach:
        # check if the row exists before inserting to accurately report 'inserted' count.

        check_exists = bind.execute(
            sa.text("SELECT 1 FROM tenant_subscriptions WHERE tenant_id = :tid"),
            {"tid": row.tenant_id},
        ).scalar()

        if not check_exists:
            bind.execute(
                insert_query,
                {
                    "tenant_id": row.tenant_id,
                    "plan_id": starter_plan_id,
                    "stripe_customer_id": row.stripe_customer_id,
                    "stripe_subscription_id": row.stripe_subscription_id,
                    "billing_cycle": "monthly",
                    "status": status,
                    "trial_ends_at": row.trial_ends_at,
                    "delinquent": status == "past_due",
                    "days_overdue": 0,
                    "churn_risk_score": 0,
                },
            )
            inserted_count += 1

    print(f"cc2-s10 backfill: inserted {inserted_count} tenant_subscriptions row(s)")


def downgrade() -> None:
    """Downgrade is best-effort and non-destructive."""
    print(
        "cc2-s10 downgrade: tenant_subscriptions backfill is best-effort; "
        "manual cleanup may be needed."
    )
