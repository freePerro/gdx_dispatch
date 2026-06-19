"""Unit tests for the qb_token_store re-encrypt tool.

The tool is a one-shot prod operation, so its end-to-end behavior is
exercised on the lab cluster — but the load-bearing invariants are pinned
here:

* The discriminator (`is_fernet_ciphertext`) splits Fernet ciphertext from
  plaintext correctly.
* `load_app_fernet()` returns the SAME ``Fernet`` instance the app uses
  via ``gdx_dispatch.core.pii._FERNET`` — NOT a freshly-constructed
  ``Fernet(MASTER_ENCRYPTION_KEY)``. This is the parity contract; a
  regression here would brick prod QB sync the moment Phase 1 deploys,
  because the app HKDF-derives its keyring while a raw Fernet wouldn't.
  /audit 2026-05-12 round 2 caught the original tool using raw Fernet.
* The apply-pass writes ciphertext that the app can decrypt via
  ``pii._FERNET.decrypt``. Round-trip is exercised end-to-end.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from gdx_dispatch.tools.encrypt_qb_token_store_rows import (
    FERNET_PREFIX,
    is_fernet_ciphertext,
)


def test_is_fernet_ciphertext_none_or_empty_treated_as_already_encrypted():
    # Skipping NULL / empty is correct — there's nothing to re-encrypt.
    assert is_fernet_ciphertext(None) is True
    assert is_fernet_ciphertext("") is True


def test_is_fernet_ciphertext_real_fernet_token_passes():
    key = Fernet.generate_key()
    f = Fernet(key)
    ct = f.encrypt(b"opaque-test-token").decode()
    assert ct.startswith(FERNET_PREFIX)
    assert is_fernet_ciphertext(ct) is True


def test_is_fernet_ciphertext_plaintext_intuit_jwt_returns_false():
    # Intuit access tokens are JWTs starting with eyJ... — never look like
    # Fernet, so the tool detects them as plaintext.
    assert is_fernet_ciphertext("eyJhbGciOiJkaXIiLCJlbmMiOiJBMTI4Q0JDLUhTMjU2In0") is False


def test_is_fernet_ciphertext_plaintext_intuit_refresh_returns_false():
    # Intuit refresh tokens start with RT1-... — also plaintext shape.
    assert is_fernet_ciphertext("RT1-128-H-fakedFakedFakedFakedFaked") is False


def test_is_fernet_ciphertext_random_string_returns_false():
    assert is_fernet_ciphertext("not-encrypted-at-all") is False
    assert is_fernet_ciphertext("gAAAAB") is False  # close but not the prefix


# ---------------------------------------------------------------------------
# Parity contract — tool's Fernet MUST equal pii._FERNET
# ---------------------------------------------------------------------------


@pytest.fixture()
def _reload_pii_with_key(monkeypatch: pytest.MonkeyPatch):
    """Set MASTER_ENCRYPTION_KEY in env and reload gdx_dispatch.core.pii so its
    module-level ``_FERNET`` rebuilds against the new key. Yields the
    reloaded module; teardown restores the original module state.
    """
    fresh_key = Fernet.generate_key().decode()
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", fresh_key)
    monkeypatch.setenv("TENANT_ID", "")  # match prod (TENANT_ID is unset)

    import gdx_dispatch.core.pii as pii  # noqa: PLC0415

    original_fernet = pii._FERNET
    importlib.reload(pii)
    try:
        yield pii
    finally:
        pii._FERNET = original_fernet


def test_load_app_fernet_returns_pii_FERNET_instance(_reload_pii_with_key):
    """The keystone contract: tool's Fernet MUST be identical to the one
    the app uses via the EncryptedString TypeDecorator. A regression here
    would mean the tool writes ciphertext the app cannot decrypt.
    """
    from gdx_dispatch.tools.encrypt_qb_token_store_rows import load_app_fernet

    tool_fernet = load_app_fernet()
    assert tool_fernet is _reload_pii_with_key._FERNET, (
        "load_app_fernet() returned a Fernet instance OTHER than pii._FERNET. "
        "Tool would encrypt with a different key than the app decrypts with."
    )


def test_load_app_fernet_exits_when_master_key_unset(monkeypatch: pytest.MonkeyPatch):
    """Refusal-to-operate contract: tool must exit on missing key, never
    silently no-op."""
    from gdx_dispatch.tools.encrypt_qb_token_store_rows import load_app_fernet

    monkeypatch.delenv("MASTER_ENCRYPTION_KEY", raising=False)
    with pytest.raises(SystemExit):
        load_app_fernet()


# ---------------------------------------------------------------------------
# Apply-pass roundtrip — tool ciphertext decrypts via pii._FERNET
# ---------------------------------------------------------------------------


def _seed_qb_token_store(db_path: Path, *, access_token: str, refresh_token: str) -> None:
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE qb_token_store ("
            "id INTEGER PRIMARY KEY, realm_id TEXT, "
            "access_token_enc TEXT, refresh_token_enc TEXT)",
        ))
        conn.execute(text(
            "INSERT INTO qb_token_store (realm_id, access_token_enc, refresh_token_enc) "
            "VALUES (:r, :a, :rt)",
        ), {"r": "realm-1", "a": access_token, "rt": refresh_token})
    eng.dispose()


def _read_qb_token_store(db_path: Path):
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT access_token_enc, refresh_token_enc FROM qb_token_store",
        )).first()
    eng.dispose()
    return row


def _run_process_tenant_on_sqlite(
    db_path: Path,
    fernet,
    *,
    apply: bool,
):
    """Bypass the to_regclass postgres-only check by running the inner scan
    loop directly against sqlite. Asserts on tool's encrypted output.
    """
    import gdx_dispatch.tools.encrypt_qb_token_store_rows as mod  # noqa: PLC0415

    stats = mod.TenantStats(slug="test-tenant")
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    try:
        with eng.begin() as conn:
            rows = conn.execute(text(
                "SELECT id, realm_id, access_token_enc, refresh_token_enc FROM qb_token_store",
            )).fetchall()
            for row in rows:
                stats.scanned += 1
                updates: dict[str, str] = {}
                for col in ("access_token_enc", "refresh_token_enc"):
                    val = getattr(row, col)
                    if mod.is_fernet_ciphertext(val):
                        continue
                    stats.plaintext_rows += 1
                    updates[col] = fernet.encrypt(val.encode()).decode()
                if updates:
                    stats.encrypted_rows += 1
                    if apply:
                        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
                        conn.execute(text(
                            f"UPDATE qb_token_store SET {set_clause} WHERE id = :id",
                        ), {**updates, "id": row.id})
    finally:
        eng.dispose()
    return stats


def test_apply_pass_writes_ciphertext_that_pii_FERNET_decrypts(
    tmp_path: Path,
    _reload_pii_with_key,
):
    """End-to-end parity: encrypt with tool, decrypt with the app's Fernet.
    Catches the round-2 audit bug (tool using raw Fernet ≠ app's HKDF-derived).
    """
    db_path = tmp_path / "tenant.db"
    _seed_qb_token_store(
        db_path,
        access_token="eyJhlG-fake-jwt-access",
        refresh_token="RT1-fake-refresh-token",
    )

    from gdx_dispatch.tools.encrypt_qb_token_store_rows import load_app_fernet

    fernet = load_app_fernet()
    stats = _run_process_tenant_on_sqlite(db_path, fernet, apply=True)
    assert stats.encrypted_rows == 1
    assert stats.plaintext_rows == 2

    row = _read_qb_token_store(db_path)
    assert row.access_token_enc.startswith(FERNET_PREFIX)
    assert row.refresh_token_enc.startswith(FERNET_PREFIX)

    # The app's _FERNET decrypts what the tool wrote — this is the parity
    # contract the auditor caught. If load_app_fernet ever regresses to raw
    # Fernet(MASTER_ENCRYPTION_KEY), this decrypt raises InvalidToken.
    pii_fernet = _reload_pii_with_key._FERNET
    assert pii_fernet.decrypt(row.access_token_enc.encode()).decode() == "eyJhlG-fake-jwt-access"
    assert pii_fernet.decrypt(row.refresh_token_enc.encode()).decode() == "RT1-fake-refresh-token"


def test_dry_run_does_not_mutate_rows(
    tmp_path: Path,
    _reload_pii_with_key,
):
    db_path = tmp_path / "tenant.db"
    _seed_qb_token_store(db_path, access_token="eyJhlG-fake-jwt", refresh_token="RT1-fake-refresh")

    from gdx_dispatch.tools.encrypt_qb_token_store_rows import load_app_fernet

    fernet = load_app_fernet()
    stats = _run_process_tenant_on_sqlite(db_path, fernet, apply=False)
    assert stats.encrypted_rows == 1

    row = _read_qb_token_store(db_path)
    assert row.access_token_enc == "eyJhlG-fake-jwt"
    assert row.refresh_token_enc == "RT1-fake-refresh"


def test_apply_pass_idempotent_on_already_encrypted(
    tmp_path: Path,
    _reload_pii_with_key,
):
    """Second --apply against already-encrypted rows: nothing changes."""
    pii_fernet = _reload_pii_with_key._FERNET
    ct_access = pii_fernet.encrypt(b"pre-encrypted-access").decode()
    ct_refresh = pii_fernet.encrypt(b"pre-encrypted-refresh").decode()
    assert ct_access.startswith(FERNET_PREFIX)

    db_path = tmp_path / "tenant.db"
    _seed_qb_token_store(db_path, access_token=ct_access, refresh_token=ct_refresh)

    from gdx_dispatch.tools.encrypt_qb_token_store_rows import load_app_fernet

    fernet = load_app_fernet()
    stats = _run_process_tenant_on_sqlite(db_path, fernet, apply=True)
    assert stats.scanned == 1
    assert stats.plaintext_rows == 0
    assert stats.encrypted_rows == 0

    # Ciphertext unchanged.
    row = _read_qb_token_store(db_path)
    assert row.access_token_enc == ct_access
    assert row.refresh_token_enc == ct_refresh
