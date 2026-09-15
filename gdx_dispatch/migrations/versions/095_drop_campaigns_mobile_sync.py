"""Drop the campaigns module's two tables and the orphaned mobile_sync_actions.

All three were created by ``create_all`` at boot, never by a migration, and
held 0 rows on production and on demo (counted 2026-09-13 and again
2026-09-14). Their models leave in the same change, which matters: the
entrypoint runs ``create_all`` before ``alembic upgrade head``, so a leftover
model would recreate a table on the next boot.

* ``campaigns`` and ``campaign_sends`` belonged to ``modules/campaigns/``,
  headless since its router left (#638). The module is retired with the
  Campaigns tab and ``routers/campaigns.py``, whose send route reported a send
  it never made.
* ``mobile_sync_actions`` lost its only reader and writer when
  ``POST /api/mobile/sync`` was removed (#633, #636).

``marketing_campaigns`` is NOT dropped: the 2026-09-13 ruling keeps it (prod
holds one soft-deleted QA seed row).

Same contract as 088, 091 and 092: every table is counted BEFORE anything is
dropped, and one occupied table refuses the whole migration. A row means the
"nothing could reach it" premise is wrong on this install, so stop, keep the
data, and let a person look. ``alembic upgrade head`` runs from the container
entrypoint, so a refusal means THE APP WILL NOT START until the table is
exported and emptied; that is the intended trade-off. ``DROP TABLE IF EXISTS``
is portable across SQLite and Postgres.

On Postgres, ``create_all`` also made three enum types for these tables
(``campaign_trigger``, ``campaign_channel``, ``campaign_send_status``). Nothing
else used them on prod or demo (2026-09-14), and a fresh install no longer
creates them, so they are dropped too, so a migrated install matches a fresh
one. Each DROP TYPE runs inside a savepoint. If Postgres refuses because
something still depends on the type (a column, an array column, a domain, a
function argument), that savepoint is rolled back and the type is left in
place, rather than failing the migration, because a failed migration
crash-loops the app. Postgres decides what counts as a dependency; an earlier
draft asked information_schema.columns and missed arrays, domains and
functions. Any other error still raises.

Rollback: ``downgrade()`` recreates all three tables empty, with the models'
columns in portable types (VARCHAR/TIMESTAMP rather than Postgres
uuid/timestamptz/enum). The enum types are not recreated; the recreated enum
columns are VARCHAR.

Revision ID: 095_drop_campaigns_mobile_sync
Revises: 094_not_null_to_match_orm
Create Date: 2026-09-14
"""

from alembic import op
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError

revision = "095_drop_campaigns_mobile_sync"
down_revision = "094_not_null_to_match_orm"
branch_labels = None
depends_on = None

# Child first: campaign_sends has a foreign key to campaigns.
TABLES = (
    "campaign_sends",
    "campaigns",
    "mobile_sync_actions",
)

# SQLSTATE 2BP01, dependent_objects_still_exist.
_PG_DEPENDENT_OBJECTS_STILL_EXIST = "2BP01"

PG_ENUM_TYPES = (
    "campaign_send_status",
    "campaign_trigger",
    "campaign_channel",
)

_RECREATE = {
    "campaigns": """
CREATE TABLE IF NOT EXISTS campaigns (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    "trigger" VARCHAR(30) NOT NULL,
    delay_days INTEGER NOT NULL,
    message_template TEXT NOT NULL,
    channel VARCHAR(10) NOT NULL,
    is_active BOOLEAN NOT NULL,
    send_count INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL
)
""",
    "campaign_sends": """
CREATE TABLE IF NOT EXISTS campaign_sends (
    id VARCHAR(36) PRIMARY KEY,
    campaign_id VARCHAR(36) NOT NULL,
    customer_id VARCHAR(36) NOT NULL,
    entity_type VARCHAR(50) NOT NULL,
    entity_id VARCHAR(50) NOT NULL,
    scheduled_at TIMESTAMP NOT NULL,
    sent_at TIMESTAMP,
    status VARCHAR(20) NOT NULL,
    idempotency_key VARCHAR(100) NOT NULL UNIQUE
)
""",
    "mobile_sync_actions": """
CREATE TABLE IF NOT EXISTS mobile_sync_actions (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    fingerprint TEXT,
    action_type TEXT,
    entity_id TEXT,
    queued_at TEXT,
    created_at TEXT
)
""",
}


def _has_table(bind, name: str) -> bool:
    return inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    present = [t for t in TABLES if _has_table(bind, t)]
    occupied: dict[str, int] = {}
    for t in present:
        rows = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar()  # noqa: S608 — name from the module tuple
        if rows:
            occupied[t] = int(rows)
    if occupied:
        listing = ", ".join(f"{t} holds {n} row(s)" for t, n in occupied.items())
        raise RuntimeError(
            f"095: {listing} — refusing to drop anything. Nothing in the app reads "
            "these tables any more. Export the rows if anyone wants them (pg_dump -t "
            "<table> on Postgres; sqlite3 .dump on SQLite), empty the table, then "
            "restart the container so the migration reruns."
        )
    for t in present:
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {t};")

    if bind.dialect.name == "postgresql":
        for typ in PG_ENUM_TYPES:
            try:
                with bind.begin_nested():
                    bind.exec_driver_sql(f"DROP TYPE IF EXISTS {typ};")
            except DBAPIError as exc:
                # psycopg2 calls it pgcode, psycopg 3 sqlstate.
                code = getattr(exc.orig, "pgcode", None) or getattr(exc.orig, "sqlstate", None)
                if code != _PG_DEPENDENT_OBJECTS_STILL_EXIST:
                    raise
                # Something else still uses the type; leave it.


def downgrade() -> None:
    bind = op.get_bind()
    for t in reversed(TABLES):
        bind.exec_driver_sql(_RECREATE[t])
