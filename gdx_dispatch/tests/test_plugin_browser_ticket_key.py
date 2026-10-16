"""The browser-stream ticket is signed with the key auth/core validated at boot
(GDXA-1310), never a module-local env read with a "dev-secret" fallback.

The key is read at import time, so each deployment shape runs in a fresh
interpreter with exactly that environment. Sharing the key with staff JWTs
leaves `typ` as the separation; the in-process tests prove every staff decode
path refuses a real ticket.

Needs FastAPI → runs in the docker image.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import sys

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.middleware.principal_stamp import _decode_to_principal
from gdx_dispatch.core.modules import decode_live_access_token
from gdx_dispatch.routers import browser_proxy
from gdx_dispatch.routers.auth import get_current_user

_REPO = pathlib.Path(__file__).resolve().parents[2]

# Imports browser_proxy and auth/core in the given env and reports the keys.
_PROBE = """
import json, jwt
from gdx_dispatch.routers import browser_proxy as bp
from gdx_dispatch.routers.auth import core
forged = jwt.encode({"scope": "browserstream", "exp": 4102444800}, "dev-secret", algorithm="HS256")
print(json.dumps({
    "alg": bp.ALG,
    "same_sign": bp.SIGN_KEY == core.SIGN_KEY,
    "same_verify": bp.VERIFY_KEY == core.VERIFY_KEY,
    "sign_is_dev_secret": bp.SIGN_KEY == "dev-secret",
    "sign_is_priv": bp.SIGN_KEY == core.PRIV,
    "forged_accepted": bp._decode_ticket(forged) is not None,
}))
"""


def _probe(env_overrides: dict[str, str], drop: tuple[str, ...]) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(env_overrides)
    env["PYTHONPATH"] = str(_REPO)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], env=env, cwd=_REPO,
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _rsa_pem_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pub


def test_rs256_deployment_without_jwt_secret_signs_with_the_rs_key():
    priv, pub = _rsa_pem_pair()
    r = _probe({"RS_PRIVATE_KEY": priv, "RS_PUBLIC_KEY": pub}, drop=("JWT_SECRET",))
    assert r["alg"] == "RS256"
    assert r["sign_is_priv"] and not r["sign_is_dev_secret"]
    assert r["same_sign"] and r["same_verify"]
    assert r["forged_accepted"] is False


def test_hs256_key_is_the_one_core_validated_not_a_second_read():
    # core strips JWT_SECRET; the old local read did not, so a padded secret
    # gave the stream a different key from the one validated at boot.
    secret = "x" * 40
    r = _probe({"JWT_SECRET": f"  {secret}\n"},
               drop=("RS_PRIVATE_KEY", "RS_PUBLIC_KEY"))
    assert r["alg"] == "HS256"
    assert r["same_sign"] and r["same_verify"]
    assert not r["sign_is_dev_secret"]
    assert r["forged_accepted"] is False


def test_no_dev_secret_literal_left_in_the_module():
    src = pathlib.Path(browser_proxy.__file__).read_text()
    assert "dev-secret" not in src
    assert 'getenv("JWT_SECRET"' not in src


# ── a real ticket cannot be replayed as a staff token ────────────────────────

@pytest.fixture
def real_ticket(monkeypatch) -> str:
    monkeypatch.setattr(browser_proxy, "fetch_permissions", lambda key: {"browser"})
    monkeypatch.setattr(browser_proxy, "has_permission_consent", lambda db, key, perm: True)
    app = FastAPI()
    app.include_router(browser_proxy.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u-1", "role": "owner"}
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    app.dependency_overrides[get_db] = lambda: session
    r = TestClient(app).post("/api/plugins/_browser/ticket",
                             json={"key": "chipricing", "url": "https://orderentry.chiohd.com/"})
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


def test_real_ticket_verifies_under_the_core_key(real_ticket):
    from gdx_dispatch.routers.auth import core
    claims = jwt.decode(real_ticket, core.VERIFY_KEY, algorithms=[core.ALG])
    assert claims["typ"] == "browserstream"
    assert browser_proxy._decode_ticket(real_ticket) is not None


def test_staff_decode_paths_refuse_the_ticket(real_ticket):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_current_user(request=None, token=real_ticket))
    assert exc.value.status_code == 401
    assert decode_live_access_token(real_ticket, None) == {}
    assert _decode_to_principal(real_ticket) is None
