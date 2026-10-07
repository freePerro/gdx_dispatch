"""Record handled Stripe webhook events by event id (GDXA-357).

* ``stripe_webhook_events``: ``event_id`` (Stripe's ``evt_...``, primary
  key), ``event_type``, ``result_status``, ``processed_at``. The webhook
  route reads it before dispatching and writes it after the handler returns,
  so a late redelivery of an event that was already handled changes nothing.

Why: Stripe can deliver an event it already delivered, after a later event
for the same charge. A dispute withdrawal redelivered after the dispute was
won voided the payment again (``tests/test_stripe_webhook_redelivery.py``).
It stops a repeat of the same event id only; it does not order distinct
events. ``payment_intent.succeeded`` is never recorded (M14 recovers through
its re-send); its own replay hazard is closed in ``core/payments.py``.

No existing row is touched. The table starts empty, so every event that
arrives after the upgrade is handled exactly as before on its first delivery.
Guarded with ``has_table``: the table is ORM-created, and ``create_all`` runs
before alembic on boot, so on a fresh install this is a no-op.

Rollback: ``downgrade()`` drops the table. With the table gone the route
logs ``stripe_webhook_event_lookup_failed`` / ``..._record_failed`` and runs
every delivery's handler, which is the pre-109 behaviour; roll the code back
with it to silence the logs. On the next boot ``create_all`` recreates the
table empty unless the model is removed too.

Revision ID: 109_stripe_webhook_events
Revises: 108_invoice_line_qty_decimal
Create Date: 2026-10-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "109_stripe_webhook_events"
down_revision = "108_invoice_line_qty_decimal"
branch_labels = None
depends_on = None

TABLE = "stripe_webhook_events"


def upgrade() -> None:
    if inspect(op.get_bind()).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("result_status", sa.String(64), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    if inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
