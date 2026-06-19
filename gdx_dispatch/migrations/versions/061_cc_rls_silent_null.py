"""Add silent-null flag (, true) to CC tenant_isolation policies.

Mig 053 (usage_records, mrr_ledger) and mig 054 (invoices, payments,
connect_accounts, connect_charges, dunning_state) all created
``tenant_isolation`` policies whose USING clause is::

    tenant_id = current_setting('app.tenant_id')::uuid

without the second ``, true`` argument to ``current_setting``. Without the
flag, Postgres raises ``UndefinedObject: unrecognized configuration
parameter`` whenever the GUC is not set on the session.

This bites every CC v2 platform-scope read: those endpoints don't carry a
tenant context, so ``app.tenant_id`` stays unset, and the
``tenant_isolation`` policy explodes before the OR can fall through to the
``_cc_staff_read`` policy that would have permitted the row.

Mig 052 (``tenant_subscriptions``, ``tenant_usage_meter_links``) and mig
055 (``webhook_events``, ``billing_audit_log``) already pass ``, true`` —
they're not touched here.

This migration uses ``ALTER POLICY ... USING (...)`` rather than
DROP+CREATE so that WITH CHECK clauses, applied roles, and FOR-clause
remain intact. (None of these policies have WITH CHECK, but ALTER POLICY
is the surgical operator regardless.)

Revision matches lab/prod head ``060_oauth_dcr_clients``.
"""
from alembic import op

revision = "061_cc_rls_silent_null"
down_revision = "060_oauth_dcr_clients"
branch_labels = None
depends_on = None


# (table, policy_name) — one row per policy needing the silent-null fix.
_POLICIES_TO_PATCH: list[tuple[str, str]] = [
    # mig 053
    ("usage_records", "tenant_isolation"),
    ("mrr_ledger", "tenant_isolation"),
    # mig 054 — created in a loop with f-string `<table>_tenant_isolation`
    ("invoices", "invoices_tenant_isolation"),
    ("payments", "payments_tenant_isolation"),
    ("connect_accounts", "connect_accounts_tenant_isolation"),
    ("connect_charges", "connect_charges_tenant_isolation"),
    ("dunning_state", "dunning_state_tenant_isolation"),
]


def upgrade() -> None:
    for table, policy in _POLICIES_TO_PATCH:
        op.execute(
            f"ALTER POLICY {policy} ON {table} "
            "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
        )


def downgrade() -> None:
    # Revert to the original (silent-null-less) form. This restores the bug,
    # but symmetric downgrade is the alembic contract.
    for table, policy in _POLICIES_TO_PATCH:
        op.execute(
            f"ALTER POLICY {policy} ON {table} "
            "USING (tenant_id = current_setting('app.tenant_id')::uuid)"
        )
