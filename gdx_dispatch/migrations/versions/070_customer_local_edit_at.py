"""Record when a human last edited a customer's identity fields in GDX.

Doug 2026-08-19, asked directly: when a QuickBooks pull disagrees with GDX on
name/email/phone, GDX wins on rows a human has edited.

Why this column has to exist at all: `pull_customers` assigned
`customer.name/.email/.phone` unconditionally for every mapped row, so a QB
row with no `PrimaryEmailAddr` BLANKED a good GDX email, and any correction
made in the office was overwritten on the next sync with no trace — the only
record was one run-level count row. QuickBooks is being phased out; it is no
longer the authority on who a customer is.

Two columns, because one is not enough. `local_edit_at` records WHEN;
`local_edit_fields` records WHICH — a JSON subset of name/email/phone. A
single timestamp cannot distinguish "GDX never had an email, let QB fill it"
from "a human DELETED the wrong email, QB must not put it back": both read as
empty, and the field goes back to QB. That is the single edit most worth
protecting, so ownership is per field.

Nothing is compared against a sync clock. An earlier draft tested
`local_edit_at > qb_entity_maps.synced_at`, which is broken — `_upsert_map`
re-stamps `synced_at` on EVERY pull including no-ops, so after one sync the
comparison is permanently False and "GDX wins" silently stops winning, for
every row, forever.

Backfill: audit-PROVEN only. `customer_updated` audit rows name the fields a
human changed, so the customers with a recorded office edit are marked from
their own audit trail — that is evidence, not inference. Customers with no
such row stay NULL and keep exactly today's behaviour. Skipped entirely if
audit_logs is absent or shaped differently, because a migration that cannot
read the evidence must not guess.

Rollback: drop the column. The sync falls back to QB-wins, which is the
behaviour that shipped for the last four months, so nothing downstream breaks.

Revision ID: 070_customer_local_edit_at
Revises: 069_invoice_line_includes_labor
"""
import contextlib

from alembic import op

revision = "070_customer_local_edit_at"
down_revision = "069_invoice_line_includes_labor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite lane: no IF NOT EXISTS on ADD COLUMN, so suppress the
        # duplicate-column error and let a re-run be a no-op.
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "ALTER TABLE customers ADD COLUMN local_edit_at TIMESTAMP NULL;"
            )
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "ALTER TABLE customers ADD COLUMN local_edit_fields TEXT NULL;"
            )
        _backfill_from_audit(bind)
        return
    bind.exec_driver_sql(
        """
        ALTER TABLE customers
            ADD COLUMN IF NOT EXISTS local_edit_at TIMESTAMPTZ NULL;
        """
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE customers
            ADD COLUMN IF NOT EXISTS local_edit_fields JSONB NULL;
        """
    )
    _backfill_from_audit(bind)


def _backfill_from_audit(bind) -> None:
    """Mark customers whose office edits the audit trail actually records.

    Evidence, not inference: `customer_updated` rows carry the operator-facing
    field set in `details`. A customer with no such row is left NULL and keeps
    today's behaviour. Any failure here is swallowed — a tenant whose
    audit_logs predates that action, or is shaped differently, must not fail
    the migration over a best-effort marking.
    """
    import json as _json

    try:
        rows = bind.exec_driver_sql(
            """
            SELECT entity_id, details, created_at
            FROM audit_logs
            WHERE action = 'customer_updated' AND entity_id IS NOT NULL
            """
        ).fetchall()
    except Exception:
        return

    owned: dict[str, set] = {}
    seen_at: dict[str, object] = {}
    for entity_id, details, created_at in rows:
        try:
            payload = details if isinstance(details, dict) else _json.loads(details or "{}")
        except Exception:
            continue
        fields = {k for k in ("name", "email", "phone") if k in (payload or {})}
        if not fields:
            continue
        key = str(entity_id)
        owned.setdefault(key, set()).update(fields)
        seen_at[key] = created_at

    for key, fields in owned.items():
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "UPDATE customers SET local_edit_at = %s, local_edit_fields = %s "
                "WHERE CAST(id AS TEXT) = %s"
                if bind.dialect.name == "postgresql"
                else "UPDATE customers SET local_edit_at = ?, local_edit_fields = ? "
                     "WHERE CAST(id AS TEXT) = ?",
                (seen_at.get(key), _json.dumps(sorted(fields)), key),
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite pre-3.35 cannot DROP COLUMN; leaving an unread timestamp is
        # harmless and beats failing the downgrade.
        return
    bind.exec_driver_sql(
        "ALTER TABLE customers DROP COLUMN IF EXISTS local_edit_at;"
    )
    bind.exec_driver_sql(
        "ALTER TABLE customers DROP COLUMN IF EXISTS local_edit_fields;"
    )
