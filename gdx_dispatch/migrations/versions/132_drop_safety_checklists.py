"""Drop ``safety_checklists``, the table behind the retired safety checklist.

Doug ruled on GDXA-1714 (2026-10-19) to retire the safety checklist outright.
No Vue file ever called ``/api/safety/*``; the router went in GDXA-1833 and the
always-zero performance stat in GDXA-1832. This revision drops the table, and
the ``SafetyChecklist`` model leaves in the same change. Like the 092 tables
it was built by ``create_all`` at boot, never by a migration.

Same contract as 088, 091 and 092: the table is counted BEFORE anything is
dropped, inside this migration's transaction, and a non-empty table refuses
the migration. A row means a person wrote to the checklist on this install, so
stop, keep the data and let a person look. ``alembic upgrade head`` runs from
the container entrypoint, so a refusal means THE APP WILL NOT START (it
crash-loops under ``unless-stopped``) until the table is exported and emptied.
That is the intended trade-off. Recount on prod and on demo immediately before
deploying. ``DROP TABLE IF EXISTS`` is portable across SQLite and Postgres, and
the migration runs nothing else.

Rollback: ``downgrade()`` recreates the table empty, with the model's columns
in portable types (VARCHAR/TEXT/BOOLEAN/TIMESTAMP). Rows exported before the
upgrade can be loaded back into it.

Revision ID: 132_drop_safety_checklists
Revises: 131_vacation_year_policy
Create Date: 2026-10-19
"""

from alembic import op
from sqlalchemy import inspect

revision = "132_drop_safety_checklists"
down_revision = "131_vacation_year_policy"
branch_labels = None
depends_on = None

TABLE = "safety_checklists"

_RECREATE = """
CREATE TABLE IF NOT EXISTS safety_checklists (
    id VARCHAR(36) PRIMARY KEY,
    company_id VARCHAR(36) NOT NULL,
    job_id VARCHAR(36) NOT NULL,
    technician_id VARCHAR(36) NOT NULL,
    items TEXT NOT NULL,
    completed BOOLEAN,
    photo_url TEXT,
    signed_at TIMESTAMP,
    created_at TIMESTAMP,
    deleted_at TIMESTAMP
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table(TABLE):
        return
    rows = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()  # noqa: S608 — module constant
    if rows:
        raise RuntimeError(
            f"132: {TABLE} holds {int(rows)} row(s) — refusing to drop it. Nothing in "
            "the app reads this table any more. Export the rows if anyone wants them "
            f"(pg_dump -t {TABLE} on Postgres; sqlite3 .dump {TABLE} on SQLite), empty "
            "the table, then restart the container so the migration reruns."
        )
    bind.exec_driver_sql(f"DROP TABLE IF EXISTS {TABLE};")


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_RECREATE)
