"""One-shot re-encrypt of plaintext ``vendors.tax_id`` / ``account_number`` rows.

Background
----------
``Vendor.tax_id`` (EIN/SSN-equivalent) and ``Vendor.account_number`` (bank
account) moved from plaintext ``VARCHAR`` to the ``EncryptedString``
TypeDecorator. New ORM writes encrypt; reads of legacy plaintext rows succeed
via EncryptedString's InvalidToken passthrough. This tool encrypts the existing
plaintext rows so the data is encrypted at rest immediately (SOC2), rather than
lazily on next write.

It mirrors ``encrypt_qb_token_store_rows`` exactly and REUSES its proven
tenant-enumeration / Fernet-loader / audit helpers — the only differences are
the table (``vendors``) and the columns (``tax_id``, ``account_number``). Like
that tool it encrypts with ``gdx_dispatch.core.pii._FERNET`` (HKDF-derived), NOT
a raw ``Fernet(MASTER_ENCRYPTION_KEY)``, so the app can decrypt what it writes.

Usage
-----
Run AFTER migration 011_encrypt_vendor_pii has widened the columns to TEXT (else
ciphertext truncates). Run with the new key already in the container env::

    docker exec -e MASTER_ENCRYPTION_KEY="$MASTER_ENCRYPTION_KEY" \\
        docker-app-1 python -m gdx_dispatch.tools.encrypt_vendor_pii_rows --dry-run
    # then --apply when the dry-run report looks right.

Behavior
--------
* **Idempotent.** Rows whose value already starts with ``gAAAAA`` (Fernet token
  prefix) are skipped. Re-runs are safe.
* **Per-tenant.** Enumerates tenants from the control DB and connects to each
  tenant DB. Tenants without a ``vendors`` table are skipped.
* **Audit-logged.** Each touched row writes an ``audit_logs`` entry with
  ``action='encrypt_plaintext_pii'`` via the canonical ORM helper.
* **No PII in transcript.** Only counts and column names are printed.
* **Non-zero exit on any tenant error** — a silent permission skip must not
  look like a clean run.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# Reuse the battle-tested helpers from the qb_token_store re-encrypt tool rather
# than re-implement tenant enumeration / Fernet loading / audit logging.
from gdx_dispatch.tools.encrypt_qb_token_store_rows import (
    TenantStats,
    _resolve_audit_helper,
    is_fernet_ciphertext,
    iter_tenants,
    load_app_fernet,
)

_COLUMNS = ("tax_id", "account_number")


def process_tenant(
    tenant_id: str,
    slug: str,
    db_url: str,
    fernet,
    *,
    apply: bool,
) -> TenantStats:
    stats = TenantStats(slug=slug)
    try:
        eng = create_engine(db_url, future=True)
    except Exception as exc:  # noqa: BLE001
        stats.errors.append(f"create_engine failed: {exc}")
        return stats

    try:
        with Session(eng) as db:
            # to_regclass returns NULL for both "missing table" AND "no SELECT
            # privilege" — disambiguate via information_schema (most roles can
            # read it), same as the qb tool.
            exists = db.execute(
                text("SELECT to_regclass('public.vendors') IS NOT NULL"),
            ).scalar()
            if not exists:
                visible = db.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'vendors'",
                    ),
                ).scalar()
                if visible:
                    stats.errors.append(
                        "vendors exists in information_schema but to_regclass "
                        "returned NULL — likely a SELECT permission issue on the "
                        "connecting role. Skipping this tenant.",
                    )
                return stats

            rows = db.execute(
                text("SELECT id, tax_id, account_number FROM vendors"),
            ).fetchall()

            for row in rows:
                stats.scanned += 1
                updates: dict[str, str] = {}
                touched_cols: list[str] = []
                for col in _COLUMNS:
                    val = getattr(row, col)
                    if is_fernet_ciphertext(val):
                        continue
                    stats.plaintext_rows += 1
                    updates[col] = fernet.encrypt(val.encode("utf-8")).decode("utf-8")
                    touched_cols.append(col)

                if not updates:
                    continue

                stats.encrypted_rows += 1
                if not apply:
                    continue

                set_clause = ", ".join(f"{k} = :{k}" for k in updates)
                db.execute(
                    text(f"UPDATE vendors SET {set_clause} WHERE id = :id"),  # noqa: S608
                    {**updates, "id": row.id},
                )
                log_audit_event_sync = _resolve_audit_helper()
                log_audit_event_sync(
                    db,
                    action="encrypt_plaintext_pii",
                    entity_type="vendors",
                    entity_id=str(row.id),
                    details={
                        "columns": touched_cols,
                        "source": "gdx_dispatch.tools.encrypt_vendor_pii_rows",
                    },
                    tenant_id=tenant_id,
                    user_id="system",
                )
            db.commit()
    finally:
        eng.dispose()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-encrypt vendors.tax_id / account_number rows stored as plaintext.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually UPDATE rows (default: dry-run report only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit dry-run (the default; provided for symmetry with --apply)",
    )
    args = parser.parse_args()
    if args.apply and args.dry_run:
        sys.exit("--apply and --dry-run are mutually exclusive")
    apply = bool(args.apply)
    mode = "APPLY" if apply else "DRY-RUN"

    print(f"=== vendors PII re-encrypt: {mode} ===")
    fernet = load_app_fernet()
    print(
        "gdx_dispatch.core.pii._FERNET loaded (HKDF-derived from "
        "MASTER_ENCRYPTION_KEY + TENANT_ID — parity with app)",
    )

    totals = TenantStats(slug="__totals__")
    total_errors = 0
    for tenant_id, slug, db_url in iter_tenants():
        print(f"\n--- tenant {slug} ---")
        stats = process_tenant(tenant_id, slug, db_url, fernet, apply=apply)
        print(
            f"  scanned={stats.scanned} "
            f"plaintext_found={stats.plaintext_rows} "
            f"encrypted={stats.encrypted_rows}",
        )
        for err in stats.errors:
            print(f"  ⚠ {err}")
        total_errors += len(stats.errors)
        totals.scanned += stats.scanned
        totals.plaintext_rows += stats.plaintext_rows
        totals.encrypted_rows += stats.encrypted_rows

    print(
        f"\n=== Totals: scanned={totals.scanned} "
        f"plaintext_found={totals.plaintext_rows} "
        f"encrypted={totals.encrypted_rows} "
        f"errors={total_errors} ({mode}) ===",
    )
    if not apply and totals.plaintext_rows > 0:
        print("Re-run with --apply to commit the re-encrypts.")
    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
