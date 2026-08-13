"""per-photo customer visibility, default OFF

Doug 2026-08-12: "per photo default off".

Job photos became customer-facing the same day (portal + the /pay page), and
the first cut showed a customer every photo on their job. That is wrong by
default: a tech shoots internal evidence too — damage found on arrival, a
hazard, another contractor's mess — and none of that should reach the customer
because someone pressed the shutter. The office decides, per photo.

Default FALSE is the whole point, and it is retroactive by construction: the
31 photos already in production become internal the moment this runs, and stay
internal until somebody shares them deliberately. NOT NULL so there is no third
"unknown" state for a reader to guess at.

Revision ID: 063_job_photo_customer_visible
Revises: 062_plugin_perms_admin
"""
from alembic import op

revision = "063_job_photo_customer_visible"
down_revision = "062_plugin_perms_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # job_photos is in the squashed baseline, so it always exists here; IF NOT
    # EXISTS keeps the ALTER idempotent across multi-container boots.
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE job_photos "
        "ADD COLUMN IF NOT EXISTS customer_visible boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    # Dropping loses which photos were shared. That is acceptable only because
    # the safe state is the default one: after a downgrade + re-upgrade every
    # photo is internal again, never accidentally public.
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE job_photos DROP COLUMN IF EXISTS customer_visible"
    )
