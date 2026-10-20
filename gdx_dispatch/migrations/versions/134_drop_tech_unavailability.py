"""Drop ``tech_unavailability`` (GDXA-1830).

Created by ``create_all`` at boot, never by a migration. Time off is one
record: ``time_off_requests`` (``routers/time_off.py``), which techs file from
the mobile Time off path and the office approves. The duplicate writers left
in GDXA-1828 (``routers/technicians.py``) and GDXA-1829
(``/api/tech-unavailability``); the schedule board and available-slots read
approved time off since GDXA-1829. Nothing reads or writes this table, and the
model leaves in the same change (maintainer ruling on GDXA-1775, 2026-10-19).

Same contract as 088, 091, 092 and 133: the table is counted BEFORE it is
dropped, and any row refuses the migration. Rows are not copied into
``time_off_requests``: an approved request carries the closed timeclock
entries its approval wrote, and ``tech_id`` here is a technician id where a
request names a user, so a copy would invent approvals nobody made. A row
means a person must look, so stop and keep the data. ``alembic upgrade head``
runs from the container entrypoint under ``set -e`` and ``app`` restarts
``unless-stopped``, so a refusal crash-loops the app until the table is
exported and emptied; that is the intended trade-off. Recount on prod and demo
before deploying. ``DROP TABLE IF EXISTS`` is portable across SQLite and
Postgres and drops its two indexes with it.

Rollback: ``downgrade()`` recreates the table empty, with the model's columns
in portable types (VARCHAR/TIMESTAMP rather than Postgres uuid/timestamptz).
Restore the model and a writer first if the data path is wanted back.

Revision ID: 134_drop_tech_unavailability
Revises: 133_drop_van_inventory
Create Date: 2026-10-19
"""

from alembic import op
from sqlalchemy import inspect

revision = "134_drop_tech_unavailability"
down_revision = "133_drop_van_inventory"
branch_labels = None
depends_on = None

TABLES = ("tech_unavailability",)

_RECREATE = {
    "tech_unavailability": """
CREATE TABLE IF NOT EXISTS tech_unavailability (
    id VARCHAR(36) PRIMARY KEY,
    company_id VARCHAR(64) NOT NULL,
    tech_id VARCHAR(64) NOT NULL,
    start_at TIMESTAMP NOT NULL,
    end_at TIMESTAMP NOT NULL,
    reason VARCHAR(200),
    created_by VARCHAR(200),
    created_at TIMESTAMP NOT NULL,
    deleted_at TIMESTAMP
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
            f"134: {listing} — refusing to drop it. Nothing in the app reads this "
            "table any more; approved time off lives in time_off_requests. Export "
            "the rows (pg_dump -t <table> on Postgres; sqlite3 .dump on SQLite), "
            "re-enter any still-relevant days as time off requests, empty the "
            "table, then restart the container so the migration reruns."
        )
    for t in present:
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {t};")


def downgrade() -> None:
    bind = op.get_bind()
    for t in reversed(TABLES):
        bind.exec_driver_sql(_RECREATE[t])
