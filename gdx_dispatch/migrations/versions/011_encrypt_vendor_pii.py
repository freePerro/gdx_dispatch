"""Widen vendors.tax_id / account_number to TEXT for EncryptedString-at-rest

``Vendor.tax_id`` (EIN/SSN-equivalent) and ``Vendor.account_number`` (bank
account) moved from plaintext ``VARCHAR(50)`` / ``VARCHAR(100)`` to the
``EncryptedString`` TypeDecorator (impl = TEXT). Fernet ciphertext of a short
value is ~120+ chars, so the old fixed-length VARCHARs would TRUNCATE the
ciphertext and corrupt the data on the first encrypted write. This migration
widens both columns to TEXT before the new code writes any ciphertext.

``vendors`` is built by create_all (like custom_catalog_items / 005-008), not
the squashed baseline, so guard the ALTER with to_regclass: a no-op on a fresh
DB where create_all already made the columns TEXT from the new model, and the
actual widening on existing tenant DBs. Idempotent across the multi-container
boot.

Existing rows stay plaintext after this runs; EncryptedString reads them via
its InvalidToken passthrough and re-encrypts on the next ORM write. To encrypt
the existing rows immediately (SOC2 at-rest), run the one-shot
``gdx_dispatch.tools.encrypt_vendor_pii_rows`` tool — same pattern as the
qb_token_store re-encrypt.

Renumbered 010 -> 011 and rechained onto 010_app_settings_debug_logging so the
two formerly-sibling 010s (this one + the app-settings debug-logging migration
on PR #82) form a LINEAR history (009 -> 010_app_settings -> 011_vendor_pii)
instead of two children of 009 = an alembic multi-head deploy break. This makes
the app-settings migration (PR #82) the predecessor, so #82 must merge first;
this branch should be rebased onto a main that already contains it.

Revision ID: 011_encrypt_vendor_pii
Revises: 010_app_settings_debug_logging
"""
from alembic import op

revision = "011_encrypt_vendor_pii"
down_revision = "010_app_settings_debug_logging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.vendors') IS NOT NULL THEN
            ALTER TABLE vendors
              ALTER COLUMN tax_id TYPE TEXT,
              ALTER COLUMN account_number TYPE TEXT;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Narrowing back to VARCHAR would truncate any ciphertext written while the
    # column was TEXT. Refuse silently-lossy downgrades: only revert on a fresh
    # DB by leaving TEXT in place is not possible, so this is intentionally a
    # no-op (the column stays TEXT, which is a strict superset of VARCHAR).
    pass
