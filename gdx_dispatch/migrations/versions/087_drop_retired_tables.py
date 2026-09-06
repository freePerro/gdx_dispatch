"""Drop the four tables and two columns whose code left in the single-tenant purge.

`tenant_module_grants`, `service_accounts` and `platform_feature_flags` lost
their ORM models on 2026-09-03 (#610) and hold 0 rows on prod; `tenants`
kept two columns no code reads (`subscription_status`, `stripe_connect_account_id`).
`bug_reports` lost its model on 2026-09-06 (#613): it was the second, unread
copy of every bug report — prod holds 7 rows, all real reports from the
mobile app (Jul–Aug 2026), none of which any screen ever showed.

So this migration is copy-then-drop, not drop: each `bug_reports` row that is
not already in `support_tickets` becomes a `category='bug'` ticket there,
where the Feedback page (`/feedback`, `GET /api/support/my`) lists it by
subject, category, priority and status. The body carries the description
plus the page URL and browser the reporter was on, the same shape the bug
button writes today — and that page renders no ticket body and has no close
route yet (#622), so the text is stored, not shown. Priority maps `critical` →
`urgent` (the support vocabulary); status `new` → `open`. `opened_by_email`
comes from `users.email` when the reporter's row still exists (joined through a
text cast: `users.id` is uuid on Postgres, `bug_reports.user_id` text), else
`unknown@bug-reports`. Only then is the table dropped.

The two `tenants` columns: `phase-d-saas-residue.md` (2026-09-03) kept them
("costs a money-path review"). That review, 2026-09-06: the ORM stopped
mapping them on 2026-09-03, the one Stripe reader (`core/payments.py::
_stripe_extra`) reads the ambient tenant dict built from env, never the
column, and every prod row holds NULL — so dropping them touches no money
path. The owner's 2026-09-06 instruction covered the whole decision list,
these columns named in item 2.

Both dialects: the copy is one INSERT … SELECT with a NOT EXISTS guard, so it
is rerunnable; the drops are IF EXISTS; the column drops use ALTER TABLE …
DROP COLUMN, which SQLite has supported since 3.35 (the image ships 3.46).

Rollback: `downgrade()` recreates the four tables empty (a minimal shape,
not the baseline's exact DDL — nothing reads them) and re-adds the two
`tenants` columns. The copied tickets stay — they are real reports; they
keep the bug report's id, so the old `bug_report` audit rows still resolve.

Revision ID: 087_drop_retired_tables
Revises: 086_invoice_receipt_templates
"""
from alembic import op
from sqlalchemy import inspect

revision = "087_drop_retired_tables"
down_revision = "086_invoice_receipt_templates"
branch_labels = None
depends_on = None

_TABLES = ("bug_reports", "tenant_module_grants", "service_accounts", "platform_feature_flags")

# Copy every bug report into support_tickets exactly once. `||` and COALESCE
# are the same on both engines; the NOT EXISTS guard keys on the report id
# reused as the ticket id, so a rerun inserts nothing.
_COPY = """
INSERT INTO support_tickets
    (id, tenant_id, opened_by_email, opened_by_user_id, subject, body,
     category, priority, status, created_at, closed_at, resolution_summary)
SELECT
    b.id,
    b.company_id,
    COALESCE(u.email, 'unknown@bug-reports'),
    b.user_id,
    b.subject,
    b.description
        || CASE WHEN b.page_url IS NULL THEN '' ELSE '\n\n---\nPage: ' || b.page_url END
        || CASE WHEN b.browser_info IS NULL THEN '' ELSE '\nBrowser: ' || b.browser_info END
        || '\n(copied from the retired bug_reports table by migration 087; same id)',
    'bug',
    CASE WHEN b.priority = 'critical' THEN 'urgent' ELSE COALESCE(b.priority, 'medium') END,
    CASE WHEN b.status = 'new' OR b.status IS NULL THEN 'open' ELSE b.status END,
    COALESCE(b.created_at, CURRENT_TIMESTAMP),
    b.resolved_at,
    b.resolution_notes
FROM bug_reports b
LEFT JOIN users u ON CAST(u.id AS TEXT) = b.user_id
WHERE NOT EXISTS (SELECT 1 FROM support_tickets s WHERE s.id = b.id)
"""

# Recreated empty on downgrade: enough shape for an older image to boot and
# for its create_all to leave them alone (the squashed baseline's exact
# types and constraints are not reproduced — nothing reads these tables).
_RECREATE = {
    "bug_reports": """
CREATE TABLE IF NOT EXISTS bug_reports (
    id VARCHAR(36) PRIMARY KEY,
    company_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36),
    subject VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    priority VARCHAR(20),
    page_url TEXT,
    browser_info TEXT,
    status VARCHAR(20),
    created_at TIMESTAMP,
    resolved_at TIMESTAMP,
    resolved_by VARCHAR(36),
    resolution_notes TEXT
)
""",
    "tenant_module_grants": """
CREATE TABLE IF NOT EXISTS tenant_module_grants (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    module_key TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    granted_at TIMESTAMP
)
""",
    "service_accounts": """
CREATE TABLE IF NOT EXISTS service_accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    key_prefix TEXT,
    scopes TEXT,
    allowed_tenant_slugs TEXT,
    created_by TEXT,
    created_at TIMESTAMP,
    revoked_at TIMESTAMP
)
""",
    "platform_feature_flags": """
CREATE TABLE IF NOT EXISTS platform_feature_flags (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    tenant_id TEXT,
    created_at TIMESTAMP
)
""",
}


def _has_table(bind, name: str) -> bool:
    return inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspect(bind).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    # 1. Copy the bug reports somewhere a person can see them — only when both
    #    tables exist (a fresh install has neither bug_reports nor rows).
    if _has_table(bind, "bug_reports"):
        if not _has_table(bind, "support_tickets"):
            n = bind.exec_driver_sql("SELECT COUNT(*) FROM bug_reports").scalar() or 0
            if n:
                raise RuntimeError(
                    f"087: {n} bug_reports rows but no support_tickets table to copy them into — "
                    "refusing to drop them"
                )
        else:
            bind.exec_driver_sql(_COPY)
    # 2. Drop the retired tables.
    for table in _TABLES:
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {table};")
    # 3. Drop the two tenants columns no code reads.
    if _has_table(bind, "tenants"):
        for col in ("subscription_status", "stripe_connect_account_id"):
            if _has_column(bind, "tenants", col):
                bind.exec_driver_sql(f"ALTER TABLE tenants DROP COLUMN {col};")


def downgrade() -> None:
    bind = op.get_bind()
    for ddl in _RECREATE.values():
        bind.exec_driver_sql(ddl)
    if _has_table(bind, "tenants"):
        if not _has_column(bind, "tenants", "subscription_status"):
            bind.exec_driver_sql(
                "ALTER TABLE tenants ADD COLUMN subscription_status VARCHAR(32) NOT NULL DEFAULT 'trialing';"
            )
        if not _has_column(bind, "tenants", "stripe_connect_account_id"):
            bind.exec_driver_sql("ALTER TABLE tenants ADD COLUMN stripe_connect_account_id VARCHAR(255);")
