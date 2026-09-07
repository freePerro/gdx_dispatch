"""Drop five tables behind routes that never served.

All five were created by ``create_all`` at boot, never by a migration, and
every one held 0 rows on production and on demo (counted 2026-09-06):

* ``po_requests`` and ``po_request_lines`` — ``routers/po_workflow.py``, the
  third purchase-order system. All four of its routes were shadowed by
  ``routers/purchase_orders.py`` (included first), so no request ever
  reached it (#568).
* ``booking_requests_router`` and ``booking_jobs_router`` —
  ``routers/booking.py``, an office approve/decline flow no Vue file ever
  called (#458).
* ``portal_booking_requests`` — the customer-portal ``POST /booking`` write,
  which the portal SPA never called either (#458).

The routers and models leave in the same change; the record is
``docs/design/dead-duplicates-removal-2026-09-06.md`` (issues #458, #568).

Same contract as 088 and 091: every table is counted BEFORE anything is
dropped, and one occupied table refuses the whole migration — a row means
the "nobody could reach it" premise is wrong on this install, so stop, keep
the data, let a person look. ``alembic upgrade head`` runs from the
container entrypoint, so a refusal means THE APP WILL NOT START until the
table is exported and emptied; that is the intended trade-off.
``DROP TABLE IF EXISTS`` is portable across SQLite and Postgres and nothing
else runs.

Rollback: ``downgrade()`` recreates all five empty, with the models' columns
in portable types (VARCHAR/TIMESTAMP/NUMERIC rather than Postgres
uuid/timestamptz).

Revision ID: 092_drop_dead_duplicate_tables
Revises: 091_drop_inbound_sms
Create Date: 2026-09-06
"""

from alembic import op
from sqlalchemy import inspect

revision = "092_drop_dead_duplicate_tables"
down_revision = "091_drop_inbound_sms"
branch_labels = None
depends_on = None

# Child tables first, so a future FK could not block the drop.
TABLES = (
    "po_request_lines",
    "po_requests",
    "booking_jobs_router",
    "booking_requests_router",
    "portal_booking_requests",
)

_RECREATE = {
    "po_requests": """
CREATE TABLE IF NOT EXISTS po_requests (
    id VARCHAR(36) PRIMARY KEY,
    company_id VARCHAR(36) NOT NULL,
    requested_by VARCHAR(36) NOT NULL,
    job_id VARCHAR(36),
    customer_id VARCHAR(36),
    supplier_name VARCHAR(300),
    status VARCHAR(30) NOT NULL,
    notes TEXT,
    created_at TIMESTAMP,
    approved_at TIMESTAMP,
    received_at TIMESTAMP,
    deleted_at TIMESTAMP
)
""",
    "po_request_lines": """
CREATE TABLE IF NOT EXISTS po_request_lines (
    id VARCHAR(36) PRIMARY KEY,
    po_id VARCHAR(36) NOT NULL,
    sku VARCHAR(100),
    name VARCHAR(300) NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price NUMERIC(12, 2) NOT NULL
)
""",
    "booking_requests_router": """
CREATE TABLE IF NOT EXISTS booking_requests_router (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    service TEXT NOT NULL,
    preferred_date TEXT NOT NULL,
    preferred_slot TEXT,
    status TEXT NOT NULL,
    decline_reason TEXT,
    approved_job_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
""",
    "booking_jobs_router": """
CREATE TABLE IF NOT EXISTS booking_jobs_router (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    booking_request_id TEXT NOT NULL,
    created_at TEXT NOT NULL
)
""",
    "portal_booking_requests": """
CREATE TABLE IF NOT EXISTS portal_booking_requests (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    requested_date TEXT NOT NULL,
    service_type TEXT NOT NULL,
    notes TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
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
            f"092: {listing} — refusing to drop anything. Nothing in the app reads "
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
