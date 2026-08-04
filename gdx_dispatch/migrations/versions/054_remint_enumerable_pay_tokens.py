"""Re-mint enumerable invoice pay tokens (public_token LIKE 'qb-%').

The QuickBooks importer minted ``public_token = f"qb-{qb_id}"`` — sequential
and therefore enumerable — while every other minting site uses
``secrets.token_urlsafe(48)``. Anyone could walk ``/pay/qb-1``, ``/pay/qb-2``
… and read the customer name, invoice number and balance owed on each
imported invoice.

After the 2026-08-04 payment-authorization hardening this matters more, not
less: the token IS the authorization for the public payment endpoints
(``create-intent``, ``confirm``, ``ach/setup``, ``ach/charge``). A guessable
token means guessable authorization.

Fix at the source (``modules/quickbooks/sync.py``) plus this backfill for rows
already written.

NOTE ON BLAST RADIUS: any ``/pay/qb-N`` link already sent to a customer stops
working — they get a 404 and need a fresh link (Billing → Send Payment Link,
which mints a new token anyway). That is the correct trade: a dead link costs
one resend, an enumerable one exposes the whole imported AR book. QB-imported
invoices are historical records rather than live payment requests, so few (if
any) such links were ever sent.

Revision ID: 054_remint_enumerable_pay_tokens
Revises: 053_bank_stmt_permissions
"""
import secrets

import sqlalchemy as sa
from alembic import op

revision = "054_remint_enumerable_pay_tokens"
down_revision = "053_bank_stmt_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id FROM invoices WHERE public_token LIKE 'qb-%'")
    ).fetchall()

    # public_token is UNIQUE; generate per row and let the loop retry rather
    # than risk a collision aborting the whole migration.
    for (invoice_id,) in rows:
        for _ in range(5):
            token = secrets.token_urlsafe(48)[:64]
            try:
                conn.execute(
                    sa.text(
                        "UPDATE invoices SET public_token = :tok WHERE id = :iid"
                    ),
                    {"tok": token, "iid": invoice_id},
                )
                break
            except sa.exc.IntegrityError:  # pragma: no cover — 384-bit collision
                continue

    print(f"054: re-minted {len(rows)} enumerable invoice pay tokens")


def downgrade() -> None:
    # Deliberately irreversible: restoring 'qb-{id}' would re-open the
    # enumeration hole, and the original values carry no information worth
    # keeping (they were derived from the QuickBooks id, which is still on
    # the row).
    pass
