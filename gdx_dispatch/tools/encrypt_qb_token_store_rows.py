"""One-shot re-encrypt of ``qb_token_store`` rows silently stored as plaintext.

Background
----------
The S122-1 boot gate caught that ``pii._FERNET`` quietly went ``None`` whenever
``MASTER_ENCRYPTION_KEY`` was unset, which caused ``EncryptedString`` columns
(notably ``qb_token_store.access_token_enc`` / ``refresh_token_enc``) to
round-trip plaintext. The boot gate refuses-to-boot going forward, but pre-
existing plaintext rows still need to be re-encrypted with the new key before
prod containers start (otherwise Fernet would raise ``InvalidToken`` on the
first read after deploy and break QB sync).

Crucially the tool uses ``gdx_dispatch.core.pii._FERNET`` for encryption — NOT a
freshly-constructed ``Fernet(MASTER_ENCRYPTION_KEY)``. ``pii.py`` HKDF-
derives the actual Fernet keyring from ``(MASTER_ENCRYPTION_KEY, TENANT_ID,
info=b"gdx-pii-v1")``. Encrypting with a raw key here would produce
ciphertext the app could never decrypt — bricking QB sync the moment Phase
1 deploys. Caught pre-commit by /audit 2026-05-12 round 2.

Usage
-----
Pre-/deploy path — run while the OLD code is still serving traffic, so the
existing token rows are encrypted before the new container reads them::

    # On the VPS, with the new key already in /opt/gdx_dispatch/gdx_dispatch/docker/.env:
    ssh your-server "set -a; . /opt/gdx_dispatch/gdx_dispatch/docker/.env; set +a; \\
        docker exec -e MASTER_ENCRYPTION_KEY=\\\"\\$MASTER_ENCRYPTION_KEY\\\" \\
        docker-app-1 python -m gdx_dispatch.tools.encrypt_qb_token_store_rows --dry-run"

    # Same command with --apply when the dry-run report looks right.

Behavior
--------
* **Idempotent.** Rows whose ciphertext already starts with ``gAAAAA`` are
  skipped (Fernet v0x80 token prefix). Re-runs are safe.
* **Per-tenant.** Enumerates tenants from the control DB; decrypts each
  ``db_url_enc`` with ``GDX_FERNET_KEY`` (existing path) and connects to
  the tenant DB. Tenants without a ``qb_token_store`` table are skipped.
* **Audit-logged.** Each touched row writes an ``audit_logs`` entry with
  ``action='encrypt_plaintext_pii'`` so the repair is traceable.
* **No tokens in transcript.** Only counts and column names are printed —
  never the plaintext or ciphertext values.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


# Fernet ciphertext is URL-safe base64 of (version byte 0x80 + 8-byte timestamp
# + IV + ciphertext + HMAC). The first 9 bytes (0x80 + 8-byte timestamp)
# encode to "gAAAAA" in URL-safe base64 (assuming the high-order timestamp
# bytes are zero today and for the next ~17,000 years).
FERNET_PREFIX = "gAAAAA"


@dataclass
class TenantStats:
    slug: str
    scanned: int = 0
    plaintext_rows: int = 0
    encrypted_rows: int = 0
    errors: list[str] = field(default_factory=list)


def is_fernet_ciphertext(value: str | None) -> bool:
    """Return True if the value is already encrypted (or NULL — nothing to do)."""
    if value is None or value == "":
        return True
    return value.startswith(FERNET_PREFIX)


def load_app_fernet():
    """Return the Fernet keyring the app itself uses for ``EncryptedString``.

    ``gdx_dispatch.core.pii`` HKDF-derives a Fernet keyring from ``MASTER_ENCRYPTION_
    KEY`` + ``TENANT_ID`` at import time. The tool MUST use this same
    keyring or the app cannot decrypt the rows the tool writes. Constructing
    a raw ``Fernet(MASTER_ENCRYPTION_KEY)`` here would brick prod QB sync.
    """
    if not os.environ.get("MASTER_ENCRYPTION_KEY"):
        sys.exit(
            "MASTER_ENCRYPTION_KEY is not set in the process environment. "
            "Refusing to operate without a key — would silently no-op.",
        )
    # Import AFTER the env-var check so the early-exit message is clean. The
    # pii module reads MASTER_ENCRYPTION_KEY and TENANT_ID at import time to
    # build _FERNET; if env was set before this import, _FERNET is valid.
    from gdx_dispatch.core import pii  # noqa: PLC0415

    if pii._FERNET is None:
        sys.exit(
            "gdx_dispatch.core.pii._FERNET is None after import. Either MASTER_ENCRYPTION_KEY "
            "was empty at import time, or pii.py's HKDF construction failed. "
            "Cannot encrypt with a None keyring.",
        )
    return pii._FERNET


def iter_tenants() -> Iterator[tuple[str, str, str]]:
    """Yield (tenant_id, slug, decrypted_db_url) for every live tenant."""
    from gdx_dispatch.core.database import _decrypt_db_url  # noqa: PLC0415

    control_url = os.environ.get("CONTROL_DATABASE_URL")
    if not control_url:
        sys.exit("CONTROL_DATABASE_URL not set in environment")

    eng = create_engine(control_url, future=True)
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, slug, db_url_enc FROM tenants "
                    "WHERE deleted_at IS NULL ORDER BY slug"
                ),
            ).fetchall()
    finally:
        eng.dispose()

    for row in rows:
        tenant_id = str(row.id)
        slug = row.slug
        try:
            db_url = _decrypt_db_url(row.db_url_enc)
        except Exception as exc:  # noqa: BLE001 — keep going on bad rows
            print(f"  ⚠ tenant {slug}: failed to decrypt db_url_enc ({exc}); skipping")
            continue
        yield tenant_id, slug, db_url


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
            # privilege on it" — disambiguate by also probing
            # information_schema (which most roles can read).
            exists = db.execute(
                text("SELECT to_regclass('public.qb_token_store') IS NOT NULL"),
            ).scalar()
            if not exists:
                visible_in_schema = db.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'qb_token_store'",
                    ),
                ).scalar()
                if visible_in_schema:
                    stats.errors.append(
                        "qb_token_store exists in information_schema but to_regclass "
                        "returned NULL — likely a SELECT permission issue on the "
                        "connecting role. Skipping this tenant.",
                    )
                return stats

            rows = db.execute(
                text(
                    "SELECT id, realm_id, access_token_enc, refresh_token_enc "
                    "FROM qb_token_store",
                ),
            ).fetchall()

            for row in rows:
                stats.scanned += 1
                updates: dict[str, str] = {}
                touched_cols: list[str] = []
                for col in ("access_token_enc", "refresh_token_enc"):
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
                    text(f"UPDATE qb_token_store SET {set_clause} WHERE id = :id"),
                    {**updates, "id": row.id},
                )
                # Use the canonical audit helper so id/row_hash/prev_hash
                # auto-fill via the ORM and the immutability chain stays
                # intact. The raw INSERT shape attempted in v1 broke on
                # audit_logs.id NOT NULL with no default + bypassed the
                # before_insert hash listener — both auditor catches.
                _log_audit_event_sync = _resolve_audit_helper()
                _log_audit_event_sync(
                    db,
                    action="encrypt_plaintext_pii",
                    entity_type="qb_token_store",
                    entity_id=str(row.id),
                    details={
                        "realm_id": row.realm_id,
                        "columns": touched_cols,
                        "source": "gdx_dispatch.tools.encrypt_qb_token_store_rows",
                    },
                    tenant_id=tenant_id,
                    user_id="system",
                )
            db.commit()
    finally:
        eng.dispose()
    return stats


def _resolve_audit_helper():
    """Lazy import so module load doesn't drag the whole audit chain in
    when running --help or under tests that don't reach process_tenant."""
    from gdx_dispatch.core.audit import log_audit_event_sync  # noqa: PLC0415

    return log_audit_event_sync


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-encrypt qb_token_store rows silently stored as plaintext.",
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

    print(f"=== qb_token_store re-encrypt: {mode} ===")
    fernet = load_app_fernet()
    print(
        "gdx_dispatch.core.pii._FERNET loaded (HKDF-derived from MASTER_ENCRYPTION_KEY "
        "+ TENANT_ID — parity with app)",
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
    # Non-zero exit if any tenant had an error — auditor catch: silent-skip
    # on permission issue would otherwise look like a clean run.
    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
