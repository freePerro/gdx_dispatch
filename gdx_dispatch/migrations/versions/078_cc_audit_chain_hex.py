"""Fix cc_staff_audit_log row_hash shape: hex-ASCII (64 bytes), not raw (32).

Background — cc_staff_audit_log.row_hash is BYTEA. Canonical writes (audit.py
write_audit_event) call compute_row_hash() which returns a 64-char hex string;
that hex string is stored verbatim, so the BYTEA column carries 64 ASCII bytes.
The reader (_read_prev_hash) does bytes(raw).decode('ascii') to round-trip.

Migration 068's cc_bootstrap_super_admin SECURITY DEFINER function violated
the contract — it inserted gen_random_bytes(32) (raw 32 binary bytes). The
first such row produced byte 0xa1 in position 0; every subsequent staff
audit-write read that row, hit a UnicodeDecodeError in decode('ascii'), and
500'd. Effect: every CC staff action (allow + deny audits) failed, including
DELETE /api/cc/tenants/<id>.

This migration:
1. CREATE OR REPLACE the bootstrap function with the corrected hex shape so
   any future bootstrap matches the canonical writer's output.
2. Backfill any existing 32-byte row_hash rows in cc_staff_audit_log to hex
   ASCII (encode(row_hash, 'hex') -> 64 bytes), preserving the hash value.
"""
from __future__ import annotations

from alembic import op

revision = "078_cc_audit_chain_hex"
down_revision = "077_estimate_deposit_pct"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Replace the bootstrap function so future bootstraps write hex-ASCII.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cc_bootstrap_super_admin(
            email_in text,
            display_name_in text,
            password_hash_in text
        )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            existing_count int;
            new_id uuid;
            audit_id uuid;
            req_id text;
        BEGIN
            SELECT count(*) INTO existing_count
            FROM cc_staff_users
            WHERE base_role = 'super_admin' AND status = 'active';

            IF existing_count > 0 THEN
                RAISE EXCEPTION 'bootstrap refused: % active super_admin(s) already exist', existing_count
                    USING ERRCODE = 'P0001';
            END IF;

            new_id := gen_random_uuid();
            audit_id := gen_random_uuid();
            req_id := 'bootstrap-' || substr(replace(new_id::text, '-', ''), 1, 8);

            INSERT INTO cc_staff_users (
                id, email, display_name, base_role, status, password_hash
            ) VALUES (
                new_id, email_in, display_name_in, 'super_admin', 'active', password_hash_in
            );

            INSERT INTO cc_staff_audit_log (
                id, actor_id, action, target_type, target_id,
                result, request_id, prev_hash, row_hash
            ) VALUES (
                audit_id, new_id, 'bootstrap.super_admin.create',
                'cc_staff_users', new_id::text, 'allow',
                req_id, NULL, encode(gen_random_bytes(32), 'hex')::bytea
            );

            RETURN new_id;
        END;
        $$;
        """
    )

    # 2. Backfill any existing 32-byte rows to 64-byte hex-ASCII.
    # encode(row_hash, 'hex') turns 32 raw bytes into a 64-char hex string;
    # casting back to bytea stores those 64 ASCII bytes, which is exactly
    # what the canonical writer produces and the reader expects.
    op.execute(
        """
        UPDATE cc_staff_audit_log
        SET row_hash = encode(row_hash, 'hex')::bytea
        WHERE octet_length(row_hash) = 32;
        """
    )


def downgrade() -> None:
    # Restore the original (broken) function body verbatim from mig 068.
    # We do NOT un-backfill row_hash — converting 64 hex bytes back to 32
    # raw bytes is lossless but the chain values themselves don't change,
    # and any new canonical writes since this migration ran would already
    # be in 64-byte form. Leaving them is safe; new writes append cleanly.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cc_bootstrap_super_admin(
            email_in text,
            display_name_in text,
            password_hash_in text
        )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            existing_count int;
            new_id uuid;
            audit_id uuid;
            req_id text;
        BEGIN
            SELECT count(*) INTO existing_count
            FROM cc_staff_users
            WHERE base_role = 'super_admin' AND status = 'active';

            IF existing_count > 0 THEN
                RAISE EXCEPTION 'bootstrap refused: % active super_admin(s) already exist', existing_count
                    USING ERRCODE = 'P0001';
            END IF;

            new_id := gen_random_uuid();
            audit_id := gen_random_uuid();
            req_id := 'bootstrap-' || substr(replace(new_id::text, '-', ''), 1, 8);

            INSERT INTO cc_staff_users (
                id, email, display_name, base_role, status, password_hash
            ) VALUES (
                new_id, email_in, display_name_in, 'super_admin', 'active', password_hash_in
            );

            INSERT INTO cc_staff_audit_log (
                id, actor_id, action, target_type, target_id,
                result, request_id, prev_hash, row_hash
            ) VALUES (
                audit_id, new_id, 'bootstrap.super_admin.create',
                'cc_staff_users', new_id::text, 'allow',
                req_id, NULL, gen_random_bytes(32)
            );

            RETURN new_id;
        END;
        $$;
        """
    )
