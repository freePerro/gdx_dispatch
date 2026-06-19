"""Pin the InvalidToken plaintext-passthrough on ``qb_oauth._decrypt``.

S122-9 Slice 1: the pre-existing prod state has ``qb_token_store``
rows whose ``access_token_enc`` / ``refresh_token_enc`` are plaintext
Intuit tokens (``RT1-...`` for refresh, ``eyJ...`` for access JWT).
Activating ``MASTER_ENCRYPTION_KEY`` without the passthrough would
cause ``Fernet.decrypt`` to raise on the first read after deploy and
break QB sync for the entire window between deploy and the re-encrypt
tool finishing. The passthrough closes that window.

Pattern lifted from ``gdx_dispatch.core.database._decrypt_db_url:102``.
"""

from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet

from gdx_dispatch.core import pii
from gdx_dispatch.modules.quickbooks import oauth as qb_oauth


def test_decrypt_passes_plaintext_through_when_key_set(monkeypatch):
    """A plaintext Intuit refresh token (`RT1-...`) is not a Fernet
    token. With the key loaded, ``_decrypt`` must return the value
    as-is instead of raising InvalidToken."""
    key = base64.urlsafe_b64encode(os.urandom(32))
    monkeypatch.setattr(pii, "_FERNET", Fernet(key))

    plaintext = "RT1-100-H0-1234567890abcdef"
    assert qb_oauth._decrypt(plaintext) == plaintext


def test_decrypt_roundtrips_real_ciphertext(monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32))
    fernet = Fernet(key)
    monkeypatch.setattr(pii, "_FERNET", fernet)

    cipher = fernet.encrypt(b"my-secret-token").decode("utf-8")
    assert qb_oauth._decrypt(cipher) == "my-secret-token"


def test_decrypt_noops_without_key(monkeypatch):
    """No key loaded → ``_decrypt`` returns the value as-is regardless
    of shape. Matches the dev-mode pre-activation behavior."""
    monkeypatch.setattr(pii, "_FERNET", None)
    assert qb_oauth._decrypt("anything") == "anything"
    assert qb_oauth._decrypt("") == ""
