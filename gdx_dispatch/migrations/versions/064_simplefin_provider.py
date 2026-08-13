"""SimpleFIN provider — provider column, schedule fetch controls, base URL

The bank declined to provision a Banno external application, so the feed's
second provider is the SimpleFIN Bridge (rides MX's official Jack Henry
aggregator connection). The feed tables themselves are ORM-created
(no migration ships them — models/__init__.py #41 ordering), so this
migration only ALTERs the three existing tables; the new
``bank_feed_balance_snapshots`` table arrives via ``create_orm_tables()``
exactly like its siblings.

- ``banno_institutions.provider`` — the dispatch key (`banno`/`simplefin`).
  Until now the only provider column lived on ``bank_feed_accounts`` and
  was read by nobody.
- ``banno_connections.provider_base_url`` — the credential-STRIPPED
  SimpleFIN base URL. The basic-auth pair lives Fernet-encrypted in
  ``access_token_enc``; the URL column must never contain userinfo.
- ``bank_feed_sync_schedule`` fetch controls — Doug 2026-08-13: fetch-hours
  window interpreted in TENANT-LOCAL time, and a daily fetch cap hard-limited
  to 20 (headroom under the bridge's 24/day quota). The count ledger tracks
  scheduled AND manual fetches per local calendar day.

Revision ID: 064_simplefin_provider
Revises: 063_job_photo_customer_visible
"""
from alembic import op

revision = "064_simplefin_provider"
down_revision = "063_job_photo_customer_visible"
branch_labels = None
depends_on = None

_ALTERS = (
    "ALTER TABLE IF EXISTS banno_institutions "
    "ADD COLUMN IF NOT EXISTS provider varchar(20) NOT NULL DEFAULT 'banno'",
    "ALTER TABLE IF EXISTS banno_connections "
    "ADD COLUMN IF NOT EXISTS provider_base_url text",
    "ALTER TABLE IF EXISTS bank_feed_sync_schedule "
    "ADD COLUMN IF NOT EXISTS fetch_window_start varchar(5)",
    "ALTER TABLE IF EXISTS bank_feed_sync_schedule "
    "ADD COLUMN IF NOT EXISTS fetch_window_end varchar(5)",
    "ALTER TABLE IF EXISTS bank_feed_sync_schedule "
    "ADD COLUMN IF NOT EXISTS daily_fetch_cap integer NOT NULL DEFAULT 20",
    "ALTER TABLE IF EXISTS bank_feed_sync_schedule "
    "ADD COLUMN IF NOT EXISTS fetch_count_today integer NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS bank_feed_sync_schedule "
    "ADD COLUMN IF NOT EXISTS fetch_count_date date",
)

_DROPS = (
    "ALTER TABLE IF EXISTS bank_feed_sync_schedule DROP COLUMN IF EXISTS fetch_count_date",
    "ALTER TABLE IF EXISTS bank_feed_sync_schedule DROP COLUMN IF EXISTS fetch_count_today",
    "ALTER TABLE IF EXISTS bank_feed_sync_schedule DROP COLUMN IF EXISTS daily_fetch_cap",
    "ALTER TABLE IF EXISTS bank_feed_sync_schedule DROP COLUMN IF EXISTS fetch_window_end",
    "ALTER TABLE IF EXISTS bank_feed_sync_schedule DROP COLUMN IF EXISTS fetch_window_start",
    "ALTER TABLE IF EXISTS banno_connections DROP COLUMN IF EXISTS provider_base_url",
    "ALTER TABLE IF EXISTS banno_institutions DROP COLUMN IF EXISTS provider",
)


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in _ALTERS:
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    # Dropping the ledger/window columns loses today's fetch count — safe:
    # the ledger resets every local day anyway. Dropping ``provider``
    # would make any SimpleFIN institution row indistinguishable from a
    # Banno one, so a downgrade requires those rows to be removed first
    # (there is at most one — the singleton bridge row).
    bind = op.get_bind()
    for stmt in _DROPS:
        bind.exec_driver_sql(stmt)
