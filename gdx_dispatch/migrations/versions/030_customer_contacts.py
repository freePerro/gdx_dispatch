"""customer_contacts — more than one person per account

`customers` holds exactly one name/phone/email. That is right for a house and
wrong for everything else: a property manager, a front desk and an on-site super
all belong to the same account and are three different people to call. Techs are
the ones who find out who to actually ring, so the mobile job screen writes here
and the contact follows the customer to every future job.

Plaintext name/phone/email, matching `customers` (which keeps those three in the
clear pending D-S122-9-customer-search-encryption — LIKE against ciphertext
matches nothing). No address: the job's address is the customer's, and a second
one here would only ever disagree with it.

ORM-created like its siblings, so create_orm_tables() builds this on a fresh DB
and the migration is what brings an existing one up. Guarded + idempotent.

Revision ID: 030_customer_contacts
Revises: 029_customer_contact_write
"""
from alembic import op

revision = "030_customer_contacts"
down_revision = "029_customer_contact_write"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS customer_contacts (
            id            varchar(36) PRIMARY KEY,
            company_id    varchar(36) NOT NULL,
            customer_id   uuid NOT NULL REFERENCES customers(id),
            name          text NOT NULL,
            phone         text,
            email         text,
            label         varchar(120),
            created_by    varchar(36),
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz,
            deleted_at    timestamptz
        );
        """
    )
    # The only query this table serves: "the live contacts for this customer".
    op.get_bind().exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_customer_contacts_customer_id "
        "ON customer_contacts (customer_id);"
    )
    op.get_bind().exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_customer_contacts_company_id "
        "ON customer_contacts (company_id);"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS customer_contacts;")
