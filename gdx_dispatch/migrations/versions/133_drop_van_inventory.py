"""Drop ``van_inventory`` and ``van_inventory_log`` (GDXA-1826).

Both were created by ``create_all`` at boot, never by a migration, and their
only writer was ``routers/van_inventory.py`` (``POST /api/van-inventory`` and
``POST /api/van-inventory/use``), which no Vue file ever called. Parts a tech
uses are captured and billed through the closeout and mobile paths into
``job_parts_needed``; the Variance Report reads those rows since GDXA-1825.
The router and both models leave in the same change (maintainer ruling,
option B, 2026-10-19, on GDXA-1713).

Same contract as 088, 091 and 092: both tables are counted BEFORE anything is
dropped, and one occupied table refuses the whole migration. A row means the
"nobody could reach it" premise is wrong on this install, so stop, keep the
data, let a person look. ``alembic upgrade head`` runs from the container
entrypoint under ``set -e`` and ``app`` restarts ``unless-stopped``, so a
refusal crash-loops the app until the table is exported and emptied; that is
the intended trade-off. Recount both tables on prod and demo before deploying.
``DROP TABLE IF EXISTS`` is portable across SQLite and Postgres and nothing
else runs. ``job_parts_needed`` rows with ``source='van'`` written before this
are not touched.

Rollback: ``downgrade()`` recreates both empty, with the models' columns in
portable types (VARCHAR/TIMESTAMP rather than Postgres uuid/timestamptz).
Restore the router and models first if the API is wanted back.

Revision ID: 133_drop_van_inventory
Revises: 132_drop_safety_checklists
Create Date: 2026-10-19
"""

from alembic import op
from sqlalchemy import inspect

revision = "133_drop_van_inventory"
down_revision = "132_drop_safety_checklists"
branch_labels = None
depends_on = None

# Log first: it points at van_inventory, so a future FK could not block the drop.
TABLES = (
    "van_inventory_log",
    "van_inventory",
)

_RECREATE = {
    "van_inventory": """
CREATE TABLE IF NOT EXISTS van_inventory (
    id VARCHAR(36) PRIMARY KEY,
    company_id VARCHAR(36) NOT NULL,
    truck_id VARCHAR(36) NOT NULL,
    sku VARCHAR(100),
    name VARCHAR(300) NOT NULL,
    quantity INTEGER NOT NULL,
    min_stock INTEGER NOT NULL,
    category VARCHAR(100),
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    deleted_at TIMESTAMP
)
""",
    "van_inventory_log": """
CREATE TABLE IF NOT EXISTS van_inventory_log (
    id VARCHAR(36) PRIMARY KEY,
    van_inventory_id VARCHAR(36) NOT NULL,
    job_id VARCHAR(36),
    quantity_change INTEGER NOT NULL,
    reason VARCHAR(500),
    created_by VARCHAR(36) NOT NULL,
    created_at TIMESTAMP
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
            f"133: {listing} — refusing to drop anything. Nothing in the app reads "
            "these tables any more. Export the rows if anyone wants them (pg_dump -t "
            "<table> on Postgres; sqlite3 .dump on SQLite), empty the table, then "
            "restart the container so the migration reruns."
        )
    for t in present:
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {t};")


def downgrade() -> None:
    bind = op.get_bind()
    for t in reversed(TABLES):
        bind.exec_driver_sql(_RECREATE[t])
