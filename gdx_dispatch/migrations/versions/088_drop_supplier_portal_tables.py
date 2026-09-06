"""Drop the six supplier-portal tables.

Revision ID: 088_drop_supplier_portal_tables
Revises: 087_drop_retired_tables
Create Date: 2026-09-06

The supplier portal (dealer ↔ distributor catalog, ordering, invitations,
supplier logins, a delivery load sheet) was a SaaS-era surface: its own
account table outside `users`, a public join page, and a tenant-link table
for a many-tenant world this single-tenant install never had. No screen in
the SPA could sign a supplier in (the load-sheet view read a `supplier_id`
that nothing ever wrote), and every one of the six tables holds 0 rows on
production (read 2026-09-06). The routers, models, templates and the view
leave in the same change; this migration retires the tables.

Upgrade refuses to run if any of the six holds a row: this is a purge of a
surface nobody could reach, not a data migration, and a row would mean the
assumption is wrong on that install. It also deletes the `chrome_extension`
module-grant row ("Supplier Portal Bridge"), whose key leaves MODULES in the
same change; without that the row would sit in `company_module_grants`
forever, invisible to every screen and un-deletable from any UI.

Downgrade recreates the six tables empty with the shapes the ORM declared:
uuid ids and timestamptz on Postgres (what prod's pg_dump showed), CHAR(36)
and TIMESTAMP on SQLite — so an older image boots AND its queries bind. It
does not re-insert the deleted grant row: on an older image the bridge
module simply shows as disabled, and an admin can switch it on.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

revision = "088_drop_supplier_portal_tables"
down_revision = "087_drop_retired_tables"
branch_labels = None
depends_on = None

_TABLES = (
    "supplier_order_lines",
    "supplier_orders",
    "supplier_catalog",
    "supplier_tenant_links",
    "supplier_invitations",
    "supplier_accounts",
)

# The shapes the retired ORM models declared (String(36) tenant/company ids,
# nullable created_at). {UUID} and {TS} are filled per engine by _ddl():
# Postgres gets uuid / timestamp with time zone (prod's actual types), SQLite
# CHAR(36) / TIMESTAMP.
_RECREATE = {
    "supplier_catalog": """CREATE TABLE IF NOT EXISTS supplier_catalog (
        id {UUID} PRIMARY KEY, company_id VARCHAR(36) NOT NULL,
        supplier_name VARCHAR(200) NOT NULL, sku VARCHAR(100), name VARCHAR(300) NOT NULL,
        description TEXT, unit_price NUMERIC(12, 2) NOT NULL, stock_level INTEGER,
        category VARCHAR(100), created_at {TS});""",
    "supplier_orders": """CREATE TABLE IF NOT EXISTS supplier_orders (
        id {UUID} PRIMARY KEY, company_id VARCHAR(36) NOT NULL,
        supplier_name VARCHAR(200) NOT NULL, status VARCHAR(50) NOT NULL,
        total_amount NUMERIC(12, 2) NOT NULL, notes TEXT,
        created_at {TS}, updated_at {TS});""",
    "supplier_order_lines": """CREATE TABLE IF NOT EXISTS supplier_order_lines (
        id {UUID} PRIMARY KEY, order_id {UUID} NOT NULL, sku VARCHAR(100),
        name VARCHAR(300), quantity INTEGER NOT NULL, unit_price NUMERIC(12, 2) NOT NULL,
        line_total NUMERIC(12, 2) NOT NULL);""",
    "supplier_invitations": """CREATE TABLE IF NOT EXISTS supplier_invitations (
        id {UUID} PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL,
        supplier_email VARCHAR(254) NOT NULL, supplier_name VARCHAR(200) NOT NULL,
        token VARCHAR(100) NOT NULL UNIQUE, status VARCHAR(20) NOT NULL,
        created_at {TS}, accepted_at {TS});""",
    "supplier_accounts": """CREATE TABLE IF NOT EXISTS supplier_accounts (
        id {UUID} PRIMARY KEY, email VARCHAR(254) NOT NULL UNIQUE,
        password_hash VARCHAR(256) NOT NULL, company_name VARCHAR(200) NOT NULL,
        phone VARCHAR(50), created_at {TS});""",
    "supplier_tenant_links": """CREATE TABLE IF NOT EXISTS supplier_tenant_links (
        id {UUID} PRIMARY KEY, supplier_id {UUID} NOT NULL, tenant_id VARCHAR(36) NOT NULL,
        status VARCHAR(20) NOT NULL, created_at {TS},
        CONSTRAINT uq_supplier_tenant UNIQUE (supplier_id, tenant_id));""",
}


def _has_table(bind, name: str) -> bool:
    return inspect(bind).has_table(name)


def _ddl(bind, text: str) -> str:
    pg = bind.dialect.name == "postgresql"
    return text.replace("{UUID}", "uuid" if pg else "CHAR(36)").replace(
        "{TS}", "TIMESTAMP WITH TIME ZONE" if pg else "TIMESTAMP"
    )


def upgrade() -> None:
    bind = op.get_bind()
    occupied = {}
    for t in _TABLES:
        if _has_table(bind, t):
            occupied[t] = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar()  # noqa: S608 — names from a module constant
    occupied = {t: n for t, n in occupied.items() if n}
    if occupied:
        raise RuntimeError(
            "088: supplier-portal tables are not empty — refusing to drop them: "
            + ", ".join(f"{t}={n}" for t, n in sorted(occupied.items()))
            + ". Nothing in the app reads these tables. Export the rows if anyone wants "
            "them (pg_dump -t <table> on Postgres; sqlite3 .dump on SQLite), empty "
            "the listed tables, then restart the container so the migration reruns."
        )
    for table in _TABLES:
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {table};")
    if _has_table(bind, "company_module_grants"):
        bind.exec_driver_sql("DELETE FROM company_module_grants WHERE module_key = 'chrome_extension';")


def downgrade() -> None:
    bind = op.get_bind()
    for ddl in _RECREATE.values():
        bind.exec_driver_sql(_ddl(bind, ddl))
