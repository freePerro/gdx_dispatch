"""Portal tokens are signed with the key routers/auth/core.py validated at boot (GDXA-1309).

portal.py used to build its own key: ``os.getenv("JWT_SECRET", "dev-secret")``
with HS256. In an RS256 deployment (RS_PRIVATE_KEY + RS_PUBLIC_KEY set, which
auth/core.py allows without JWT_SECRET) that went one of two ways. With
JWT_SECRET absent from the environment it fell back to the public string, so
anyone could mint a customer-portal session. With JWT_SECRET set but empty
(the compose files pass ``${JWT_SECRET:-}``) PyJWT refused the empty HMAC key,
so portal login and every guarded portal request failed with a 500. The probe
covers both, in a subprocess because both modules read the environment once,
at import.

Sharing the key with staff JWTs is safe because a portal token carries the
portal audience: every staff decode passes no audience, and PyJWT refuses an
aud-bearing token then. The staff-gate tests run on a path where the users-row
check applies and on a TenantMiddleware bypass path where it is skipped, with a
staff-token positive control on the latter.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.middleware.principal_stamp import _decode_to_principal
from gdx_dispatch.core.modules import require_permission, require_role
from gdx_dispatch.core.permissions import BUILTIN_ROLES
from gdx_dispatch.models.tenant_models import TenantRole, User, UserRoleAssignment
from gdx_dispatch.routers import portal
from gdx_dispatch.routers.auth import core as auth_core

REPO_ROOT = Path(__file__).resolve().parents[2]

_RS256_PROBE = textwrap.dedent(
    """
    import os
    from types import SimpleNamespace
    from uuid import uuid4

    import jwt
    from fastapi import HTTPException

    assert not os.environ.get("JWT_SECRET")
    from gdx_dispatch.routers import portal
    from gdx_dispatch.routers.auth import core

    assert portal.ALG == "RS256", portal.ALG
    assert portal.SIGN_KEY is core.SIGN_KEY
    assert portal.VERIFY_KEY is core.VERIFY_KEY

    cu = SimpleNamespace(id=uuid4(), customer_id=uuid4())
    token = portal.issue_customer_access_token(cu)
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(
        token, os.environ["RS_PUBLIC_KEY"], algorithms=["RS256"], audience=portal.PORTAL_AUDIENCE,
    )
    assert claims["role"] == "customer" and claims["sub"] == str(cu.id)

    forged = jwt.encode(
        {"sub": str(cu.id), "role": "customer", "customer_id": str(cu.customer_id),
         "typ": "access", "aud": portal.PORTAL_AUDIENCE, "exp": int(claims["exp"])},
        "dev-secret", algorithm="HS256",
    )
    try:
        portal.get_current_portal_customer(token=forged, db=None)
    except HTTPException as exc:
        assert exc.status_code == 401, exc.status_code
    else:
        raise AssertionError("a dev-secret HS256 token was accepted")
    print("RS256-PROBE-OK")
    """
)


def _rsa_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return priv, pub


@pytest.mark.parametrize("jwt_secret", [None, ""], ids=["absent", "empty-as-compose-sets-it"])
def test_rs256_deployment_without_jwt_secret_does_not_sign_with_dev_secret(jwt_secret):
    priv, pub = _rsa_pair()
    env = {k: v for k, v in os.environ.items() if k != "JWT_SECRET"}
    if jwt_secret is not None:
        env["JWT_SECRET"] = jwt_secret
    env.update(RS_PRIVATE_KEY=priv, RS_PUBLIC_KEY=pub, PYTHONPATH=str(REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", _RS256_PROBE],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-4000:]
    assert "RS256-PROBE-OK" in proc.stdout


def test_portal_uses_the_auth_core_key_in_process():
    assert portal.SIGN_KEY is auth_core.SIGN_KEY
    assert portal.VERIFY_KEY is auth_core.VERIFY_KEY
    assert portal.ALG == auth_core.ALG
    assert portal.SIGN_KEY != "dev-secret"


def _portal_token(**over) -> str:
    """A token shaped as the portal mints it; ``over`` replaces a claim, None drops it."""
    claims = {
        "sub": str(uuid4()),
        "role": "customer",
        "customer_id": str(uuid4()),
        "typ": "access",
        "aud": portal.PORTAL_AUDIENCE,
        "exp": int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, auth_core.SIGN_KEY, algorithm=auth_core.ALG)


def test_issued_portal_token_carries_the_portal_audience():
    token = portal.issue_customer_access_token(SimpleNamespace(id=uuid4(), customer_id=uuid4()))
    claims = jwt.decode(
        token, auth_core.VERIFY_KEY, algorithms=[auth_core.ALG], audience=portal.PORTAL_AUDIENCE,
    )
    assert claims["aud"] == portal.PORTAL_AUDIENCE


# ── a portal token is refused by every staff decode path ─────────────────────


def _staff_app(*, tenant: bool) -> FastAPI:
    """``tenant=False`` is a path TenantMiddleware bypasses (/api/push/, /pwa/,
    /admin/): no request.state.tenant, so get_current_user skips the users-row
    lookup and the token's own claims are all that can refuse it."""
    # An empty users/roles schema from the ORM: the permission resolver
    # really queries it for a UUID subject and finds no row.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (User, TenantRole, UserRoleAssignment):
        model.__table__.create(engine)
    session = sessionmaker(bind=engine)

    def _db():
        with session() as db:
            yield db

    app = FastAPI()
    app.state.denylist = Denylist()
    app.dependency_overrides[get_db] = _db

    if tenant:
        @app.middleware("http")
        async def _tenant(request: Request, call_next):
            # db_url present, so get_current_user's DB verify looks the
            # subject up in users instead of skipping as a bare test tenant.
            request.state.tenant = {"id": "t", "db_url": "sqlite://"}
            return await call_next(request)

    @app.get("/any-role", dependencies=[Depends(require_role(*BUILTIN_ROLES))])
    def _role() -> dict:
        return {"ok": True}

    @app.get("/perm", dependencies=[Depends(require_permission("customers.read_all"))])
    def _perm() -> dict:
        return {"ok": True}

    @app.get("/me")
    def _me(user: Annotated[dict, Depends(auth_core.get_current_user)]) -> dict:
        return user

    return app


def _status(app: FastAPI, path: str, token: str) -> int:
    with TestClient(app) as tc:
        return tc.get(path, headers={"Authorization": f"Bearer {token}"}).status_code


def test_no_builtin_role_is_customer():
    assert "customer" not in BUILTIN_ROLES


@pytest.mark.parametrize("path", ["/any-role", "/perm", "/me"])
def test_bypass_path_admits_a_staff_token(path):
    """Positive control for the next test: on a tenant-bypass path a staff
    access token with no users row gets through every gate, so a refusal
    of the portal token there comes from the token, not the app."""
    token, _ = auth_core._issue(str(uuid4()), "t", "admin", 600, "access")
    assert _status(_staff_app(tenant=False), path, token) == 200


@pytest.mark.parametrize("tenant", [True, False], ids=["users-row-checked", "tenant-bypass"])
@pytest.mark.parametrize("path", ["/any-role", "/perm", "/me"])
def test_portal_token_is_refused_by_staff_gates(path, tenant):
    token = portal.issue_customer_access_token(SimpleNamespace(id=uuid4(), customer_id=uuid4()))
    assert _status(_staff_app(tenant=tenant), path, token) in (401, 403)


def test_portal_token_stamps_no_principal():
    token = portal.issue_customer_access_token(SimpleNamespace(id=uuid4(), customer_id=uuid4()))
    assert _decode_to_principal(token) is None


# ── a staff-key token that is not a portal access token is refused by the portal ─


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: _portal_token(typ="refresh"), id="customer-role-refresh-typ"),
        pytest.param(lambda: _portal_token(typ="browserstream", scope="browserstream"), id="scope-ticket-typ"),
        pytest.param(lambda: _portal_token(aud=None), id="pre-1309-token-without-audience"),
        pytest.param(lambda: auth_core._issue(str(uuid4()), "t", "admin", 600, "access")[0], id="staff-access"),
        pytest.param(lambda: auth_core._issue(str(uuid4()), "t", "customer", 600, "access")[0], id="staff-shape-customer-role"),
    ],
)
def test_portal_refuses_non_portal_tokens(token):
    with pytest.raises(HTTPException) as exc:
        portal.get_current_portal_customer(token=token(), db=None)
    assert exc.value.status_code == 401
