"""Sprint 0.9 slice 0.9-d — composite ``get_current_principal`` dispatcher tests.

Mocks each underlying validator so this test module can verify the
dispatcher's DECISION LOGIC without standing up the full SS-32 SPIFFE
trust bundle or SS-21 OAuth token store.

Covered flows:

* missing credentials → 401 missing_credentials
* unknown bearer shape → 401 unknown_bearer_shape
* JWT with ``spiffe://`` sub → SPIFFE JWT dispatch (mocked validate_jwt_svid)
* JWT with user sub → OAuth dispatch (mocked token store)
* session cookie → session dispatch
* mTLS peer_spiffe_id → SPIFFE mTLS dispatch (mocked resolve_capabilities)
* scope-to-capability translation convention
* colon-flattened caps translation
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from gdx_dispatch.core import auth_dispatcher
from gdx_dispatch.core.auth_dispatcher import (
    _colon_cap_to_tuple,
    _scope_to_cap,
    get_current_principal,
)


# ── Fake Request helper ─────────────────────────────────────────────────


class _FakeState:
    """Mimics ``request.state`` — attribute-access mutable bag."""


class _FakeAppState:
    """Mimics ``request.app.state`` — attribute-access mutable bag."""


class _FakeApp:
    def __init__(self) -> None:
        self.state = _FakeAppState()


class _FakeRequest:
    """Minimal FastAPI-Request surface for dispatcher unit tests."""

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        state: dict[str, Any] | None = None,
        app_state: dict[str, Any] | None = None,
    ) -> None:
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.state = _FakeState()
        if state:
            for k, v in state.items():
                setattr(self.state, k, v)
        self.app = _FakeApp()
        if app_state:
            for k, v in app_state.items():
                setattr(self.app.state, k, v)


# ── 1. Missing credentials ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_auth_raises_401_missing_credentials() -> None:
    req = _FakeRequest()
    with pytest.raises(HTTPException) as exc:
        await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 401
    assert exc.value.detail["error_type"] == "missing_credentials"


# ── 2. Unknown bearer shape ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_bearer_shape_raises_401() -> None:
    req = _FakeRequest(headers={"authorization": "Bearer totally-random-garbage"})
    # OAuth fallback will be attempted — must fail → unknown_bearer_shape.
    with patch("gdx_dispatch.routers.auth.oauth2.get_token_store") as mock_store:
        mock_store.return_value.get_by_access = MagicMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 401
    assert exc.value.detail["error_type"] == "unknown_bearer_shape"


# ── SPIFFE JWT ─────────────────────────────────────────────────────────


def _make_jwt(payload: dict[str, Any]) -> str:
    """Build an unsigned-shape JWT for routing tests.

    The dispatcher's routing layer only shape-checks + peeks at ``sub``;
    full signature verification happens inside the (mocked) SVID validator.
    """
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"fake-sig").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


@pytest.mark.asyncio
async def test_bearer_jwt_with_spiffe_sub_dispatches_spiffe_jwt() -> None:
    spiffe_id = "spiffe://gdx.local/workload/x"
    token = _make_jwt({"sub": spiffe_id, "aud": "gdx-api", "iat": 1, "exp": 9999999999})

    @dataclass
    class _FakeSid:
        uri: str
        trust_domain: str

    @dataclass
    class _FakeValidated:
        spiffe_id: Any
        kind: str = "jwt"
        claims: dict = None  # type: ignore[assignment]

    fake_validated = _FakeValidated(
        spiffe_id=_FakeSid(uri=spiffe_id, trust_domain="gdx_dispatch.local"),
        claims={},
    )

    @dataclass
    class _FakeResolved:
        capabilities: tuple
        tenant_scope: str

    req = _FakeRequest(
        headers={"authorization": f"Bearer {token}"},
        app_state={
            "spiffe_trust_bundle": {"gdx_dispatch.local": {}},  # pass hasattr('get') == False branch
            "spiffe_audiences": ["gdx-api"],
        },
    )

    with patch(
        "gdx_dispatch.core.spiffe.svid_validator.validate_jwt_svid",
        return_value=fake_validated,
    ), patch(
        "gdx_dispatch.core.spiffe.workload_capability_map.resolve_capabilities",
        return_value=_FakeResolved(capabilities=("invoke:mcp.tool",), tenant_scope="global"),
    ):
        principal = await get_current_principal(req)  # type: ignore[arg-type]

    assert principal.auth_kind == "spiffe"
    assert principal.spiffe_id == spiffe_id
    assert ("invoke", "mcp.tool") in principal.capabilities


# ── 7. OAuth JWT ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bearer_jwt_without_spiffe_sub_dispatches_oauth() -> None:
    token = _make_jwt({"sub": "user-uuid", "iat": 1, "exp": 9999999999})

    @dataclass
    class _FakeRec:
        access_token: str
        refresh_token: str = "r"
        client_id: str = "app1"
        scope: str = "customers.read invoices.write"
        tenant_id: str | None = None
        subject_id: str | None = None
        issued_at: float = 0.0
        expires_at: float = time.time() + 3600
        revoked: bool = False

    fake_rec = _FakeRec(access_token=token)
    store = MagicMock()
    store.get_by_access = MagicMock(return_value=fake_rec)

    req = _FakeRequest(headers={"authorization": f"Bearer {token}"})
    with patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store):
        principal = await get_current_principal(req)  # type: ignore[arg-type]

    assert principal.auth_kind == "oauth"
    assert ("read", "customers") in principal.capabilities
    assert ("write", "invoices") in principal.capabilities


# ── 7.5 Bearer login JWT (D-S118-dispatcher-jwt-gap) ───────────────────


@pytest.mark.asyncio
async def test_bearer_login_jwt_falls_through_and_runs_gates() -> None:
    """D-S118-dispatcher-jwt-gap (Doug 2026-05-10), round-2 fix:

    A JWT-shape Bearer token not in the OAuth store falls through to
    _dispatch_login_jwt, decodes via the legacy path (validate_principal
    fails, jwt.decode succeeds), then runs ALL THREE post-decode gates
    via finalize_login_jwt — Slice 2 (DB verify), Slice 6 (tenant match),
    Slice H (denylist already applied at validate_principal call).

    Round-1 fix skipped finalize_login_jwt entirely; auditor flagged
    that as 3 P0 auth-bypass holes. This test asserts the gates fire.
    """
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    token = _make_jwt({
        "sub": sub_uuid,
        "gdx_tid": tenant_uuid,
        "role": "admin",
        "typ": "access",
        "iat": 1,
        "exp": 9999999999,
    })

    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)

    from gdx_dispatch.core.auth_jwt import JWTValidationError as _JWTErr

    req = _FakeRequest(
        headers={"authorization": f"Bearer {token}"},
        state={"tenant": {"id": tenant_uuid}},
    )

    fake_payload = {
        "sub": sub_uuid,
        "gdx_tid": tenant_uuid,
        "role": "admin",
        "typ": "access",
        "jti": "test-jti-123",
    }

    # finalize_login_jwt is the gate-stack we MUST call. Mock it to
    # return a happy user_dict and assert it WAS called with the right
    # claims — that's what proves the dispatcher invokes the gates.
    finalize_mock = MagicMock(return_value={
        "user_id": sub_uuid,
        "tenant_id": tenant_uuid,
        "role": "admin",
        "imp_actor_id": None,
        "imp_purpose": None,
    })

    with (
        patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store),
        patch("gdx_dispatch.core.auth.validate_principal", side_effect=_JWTErr("primary failed")),
        patch("jwt.decode", return_value=fake_payload),
        patch("gdx_dispatch.routers.auth.core.finalize_login_jwt", finalize_mock),
    ):
        principal = await get_current_principal(req)  # type: ignore[arg-type]

    # The Principal carries the verified role from finalize_login_jwt's
    # return — not the JWT's raw role claim. (In this test they match,
    # but the next test exercises the divergence.)
    assert principal.auth_kind == "session"
    assert principal.tenant_id == tenant_uuid
    assert principal.principal_role == "admin"
    assert str(principal.identity_id) == sub_uuid
    assert len(principal.capabilities) > 0

    # The gate stack was invoked with the decoded claims. If a future
    # refactor bypasses finalize_login_jwt, this assertion fails.
    finalize_mock.assert_called_once()
    call_kwargs = finalize_mock.call_args.kwargs
    assert call_kwargs["sub"] == sub_uuid
    assert call_kwargs["tenant_claim"] == tenant_uuid
    assert call_kwargs["role"] == "admin"


@pytest.mark.asyncio
async def test_dispatch_uses_db_role_not_jwt_role_after_demote() -> None:
    """Slice 2 contract: a user demoted in the DB (admin → tech) keeps
    their old admin JWT until refresh. finalize_login_jwt overlays the
    DB role over the JWT-claim role. The Principal returned by the
    dispatcher must carry the DB role, not the JWT role.

    Pre-fix (round-1) the dispatcher trusted the JWT role and called
    caps_for_role("admin") for a demoted user. This test pins the
    closure of that escalation path.
    """
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    token = _make_jwt({
        "sub": sub_uuid,
        "gdx_tid": tenant_uuid,
        "role": "admin",  # JWT still says admin
        "typ": "access",
        "iat": 1,
        "exp": 9999999999,
    })

    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)

    from gdx_dispatch.core.auth_jwt import JWTValidationError as _JWTErr

    req = _FakeRequest(
        headers={"authorization": f"Bearer {token}"},
        state={"tenant": {"id": tenant_uuid}},
    )

    # finalize_login_jwt overlays the DB role (tech) over the JWT role (admin).
    finalize_mock = MagicMock(return_value={
        "user_id": sub_uuid,
        "tenant_id": tenant_uuid,
        "role": "tech",  # DB demoted
        "imp_actor_id": None,
        "imp_purpose": None,
    })

    with (
        patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store),
        patch("gdx_dispatch.core.auth.validate_principal", side_effect=_JWTErr("primary failed")),
        patch("jwt.decode", return_value={
            "sub": sub_uuid,
            "gdx_tid": tenant_uuid,
            "role": "admin",
            "typ": "access",
            "jti": "j1",
        }),
        patch("gdx_dispatch.routers.auth.core.finalize_login_jwt", finalize_mock),
    ):
        principal = await get_current_principal(req)  # type: ignore[arg-type]

    # Principal carries the DEMOTED role, not the JWT-claim role.
    assert principal.principal_role == "tech"


@pytest.mark.asyncio
async def test_dispatch_typ_guard_rejects_refresh_token() -> None:
    """A token that validates but carries `typ != access` (a refresh
    token replayed as a Bearer) is rejected. The legacy branch checks
    this; the primary branch must too. Without the guard, any valid
    token type passes — including a long-lived refresh token used as
    if it were an access token."""
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    token = _make_jwt({
        "sub": sub_uuid,
        "gdx_tid": tenant_uuid,
        "role": "admin",
        "typ": "refresh",  # NOT access
        "iat": 1,
        "exp": 9999999999,
    })

    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)

    # Mock validate_principal SUCCESS but with typ=refresh. This is the
    # exact case the auditor flagged on the primary branch.
    fake_validated = MagicMock()
    fake_validated.subject = sub_uuid
    fake_validated.tenant_id = tenant_uuid
    fake_validated.raw_claims = {"role": "admin", "typ": "refresh"}
    fake_validated.actor_kind = "human"
    fake_validated.jti = "j1"

    req = _FakeRequest(headers={"authorization": f"Bearer {token}"})

    with (
        patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store),
        patch("gdx_dispatch.core.auth.validate_principal", return_value=fake_validated),
    ):
        with pytest.raises(HTTPException) as ei:
            await get_current_principal(req)  # type: ignore[arg-type]

    assert ei.value.status_code == 401
    assert ei.value.detail.get("error_type") == "invalid_login_jwt"
    assert "access token" in ei.value.detail.get("detail", "").lower()


@pytest.mark.asyncio
async def test_bearer_jwt_invalid_signature_returns_401() -> None:
    """A JWT-shape token that's neither in the OAuth store nor a valid
    login JWT (bad signature) returns 401 cleanly."""
    token = _make_jwt({"sub": "garbage", "iat": 1, "exp": 9999999999})

    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)

    from gdx_dispatch.core.auth_jwt import JWTValidationError as _JWTErr

    req = _FakeRequest(headers={"authorization": f"Bearer {token}"})

    with (
        patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store),
        patch("gdx_dispatch.core.auth.validate_principal", side_effect=_JWTErr("primary failed")),
        patch("jwt.decode", side_effect=Exception("InvalidSignatureError")),
    ):
        with pytest.raises(HTTPException) as ei:
            await get_current_principal(req)  # type: ignore[arg-type]

    assert ei.value.status_code == 401
    assert ei.value.detail.get("error_type") == "invalid_login_jwt"


# ── 8. Session cookie ─────────────────────────────────────────────────
#
# D-S119-opaque-cookie-deprecate (2026-05-10): the opaque-cookie branch in
# `_dispatch_session` accepted `session` / `sid` cookies — both of which
# nothing in gdx_dispatch/app.py ever sets (we don't register Starlette's
# SessionMiddleware). Branch deleted; only the JWT-shape `access_token`
# cookie is recognized now.


@pytest.mark.asyncio
async def test_opaque_session_cookie_returns_401_missing_credentials() -> None:
    """An opaque `session=foo` cookie no longer authenticates. The branch
    that synthesized a Principal from arbitrary cookie values has been
    deleted; this test pins the new contract.

    Pre-fix: any caller setting a `session=foo` cookie via XSS or a stray
    browser script got a usable (zero-cap) Principal. Post-fix: 401.
    """
    req = _FakeRequest(cookies={"session": "opaque-session-id-abc123"})
    with pytest.raises(HTTPException) as exc:
        await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 401
    assert exc.value.detail["error_type"] == "missing_credentials"


@pytest.mark.asyncio
async def test_sid_cookie_is_ignored() -> None:
    """A `sid=foo` cookie (the Starlette SessionMiddleware default name)
    is also ignored — we don't run SessionMiddleware, so nothing in our
    system sets this cookie either."""
    req = _FakeRequest(cookies={"sid": "some-session-id"})
    with pytest.raises(HTTPException) as exc:
        await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 401
    assert exc.value.detail["error_type"] == "missing_credentials"


@pytest.mark.asyncio
async def test_only_access_token_cookie_is_recognized() -> None:
    """Positive contract pin: ONLY the JWT-shape `access_token` cookie
    (set by /auth/login) triggers session dispatch. Any other cookie name
    (`session`, `sid`, or future invented names) is ignored.
    """
    import os as _os
    import jwt as _real_jwt
    tenant = "acme-corp"
    identity = uuid4()
    jwt_cookie = _real_jwt.encode(
        {
            "sub": str(identity), "gdx_tid": tenant, "role": "admin",
            "typ": "access", "iat": 1, "exp": 9999999999, "jti": str(uuid4()),
        },
        _os.environ["JWT_SECRET"], algorithm="HS256",
    )
    req = _FakeRequest(
        cookies={"access_token": jwt_cookie},
        state={"tenant": {"id": tenant}},
    )
    principal = await get_current_principal(req)  # type: ignore[arg-type]
    assert principal.auth_kind == "session"
    assert principal.tenant_id == tenant


@pytest.mark.asyncio
async def test_session_jwt_cookie_extracts_sub_and_tenant() -> None:
    """JWT-shape session cookies now route through _dispatch_login_jwt — so
    the cookie must carry a signature the legacy decode (HS256 with
    JWT_SECRET) accepts. Pre-fix this used the unsigned _make_jwt helper
    and asserted raw-claim extraction with no verification; that contract
    was a P0 forgery hole. Post-fix the cookie path is identical to the
    bearer path."""
    import os as _os
    import jwt as _real_jwt
    tenant = "acme-corp"
    identity = uuid4()
    secret = _os.environ["JWT_SECRET"]
    jwt_cookie = _real_jwt.encode(
        {
            "sub": str(identity),
            "gdx_tid": tenant,
            "role": "admin",
            "typ": "access",
            "iat": 1,
            "exp": 9999999999,
            "jti": str(uuid4()),
        },
        secret,
        algorithm="HS256",
    )
    req = _FakeRequest(
        cookies={"access_token": jwt_cookie},
        state={"tenant": {"id": tenant}},
    )
    principal = await get_current_principal(req)  # type: ignore[arg-type]
    assert principal.auth_kind == "session"
    assert principal.identity_id == identity
    assert principal.tenant_id == tenant
    assert principal.principal_role == "admin"


# ── 9. mTLS peer SPIFFE ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mtls_peer_spiffe_id_dispatches_spiffe_mtls() -> None:
    spiffe_id = "spiffe://gdx.local/workload/backend"

    @dataclass
    class _FakeResolved:
        capabilities: tuple
        tenant_scope: str

    req = _FakeRequest(state={"peer_spiffe_id": spiffe_id})
    with patch(
        "gdx_dispatch.core.spiffe.workload_capability_map.resolve_capabilities",
        return_value=_FakeResolved(capabilities=("read:widget",), tenant_scope="global"),
    ):
        principal = await get_current_principal(req)  # type: ignore[arg-type]

    assert principal.auth_kind == "spiffe"
    assert principal.spiffe_id == spiffe_id
    assert ("read", "widget") in principal.capabilities


# ── 10. Scope → capability tuple translation ──────────────────────────


def test_scope_string_to_capability_tuple_translation() -> None:
    # Documented convention: "<resource>.<action>" → ("<action>", "<resource>").
    assert _scope_to_cap("customers.read") == ("read", "customers")
    assert _scope_to_cap("invoices.write") == ("write", "invoices")
    # Malformed: no dot, empty part, wrong type, etc. → None (caller drops).
    assert _scope_to_cap("nodot") is None
    assert _scope_to_cap("too.many.dots") is None
    assert _scope_to_cap(".read") is None
    assert _scope_to_cap("customers.") is None
    assert _scope_to_cap("") is None


# ── 11. SCIM colon-flattened caps translation ────────────────────────


def test_scim_colon_caps_translation() -> None:
    assert _colon_cap_to_tuple("write:identity") == ("write", "identity")
    assert _colon_cap_to_tuple("read:user") == ("read", "user")
    # Malformed
    assert _colon_cap_to_tuple("nocolon") is None
    assert _colon_cap_to_tuple("a:b:c") is None
    assert _colon_cap_to_tuple(":identity") is None
    assert _colon_cap_to_tuple("write:") is None


# ── Empty bearer token ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_bearer_token_raises_401() -> None:
    req = _FakeRequest(headers={"authorization": "Bearer   "})
    with pytest.raises(HTTPException) as exc:
        await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 401
    assert exc.value.detail["error_type"] == "empty_bearer"


# ── 14. Shared role-gate helpers (0.9-e) ─────────────────────────────


def test_default_caps_for_role_known_and_unknown() -> None:
    from gdx_dispatch.core.auth_dispatcher import default_caps_for_role

    assert default_caps_for_role("super_admin") == (("*", "*"),)
    assert ("read", "*") in default_caps_for_role("admin")
    assert default_caps_for_role("viewer") == (("read", "*"),)
    # Unknown / SPIFFE agent → empty tuple.
    assert default_caps_for_role("not_a_real_role") == ()
    assert default_caps_for_role("agent") == ()


@pytest.mark.asyncio
async def test_require_role_allows_matching_role() -> None:
    from gdx_dispatch.core.unified_principal import Principal
    from gdx_dispatch.core.auth_dispatcher import require_role

    dep = require_role("owner", "admin")
    principal = Principal.from_session(
        identity_id=uuid4(),
        tenant_id="acme-corp",
        role="admin",
        capabilities=(("read", "*"),),
        session_id="test-sess",
    )
    # Call the inner check directly — bypass FastAPI dep resolution.
    result = await dep(principal=principal)  # type: ignore[call-arg]
    assert result is principal


@pytest.mark.asyncio
async def test_require_role_rejects_wrong_role() -> None:
    from gdx_dispatch.core.unified_principal import Principal
    from gdx_dispatch.core.auth_dispatcher import require_role

    dep = require_role("owner", "admin")
    principal = Principal.from_session(
        identity_id=uuid4(),
        tenant_id="acme-corp",
        role="tech",
        capabilities=(("read", "jobs"),),
        session_id="test-sess",
    )
    with pytest.raises(HTTPException) as exc:
        await dep(principal=principal)  # type: ignore[call-arg]
    assert exc.value.status_code == 403
    assert exc.value.detail["error_type"] == "insufficient_role"
    assert exc.value.detail["required_roles"] == ["admin", "owner"]


@pytest.mark.asyncio
async def test_session_dispatch_uses_default_role_caps() -> None:
    # A JWT session cookie with role=admin should carry non-empty caps via
    # caps_for_role("admin"). Post D-S119-session-cookie-gates the cookie
    # path runs the same gates as the bearer path, so we mint a real HS256
    # signature here (legacy decode) instead of the unsigned _make_jwt shape.
    import os as _os
    import jwt as _real_jwt
    tenant = "acme-corp"
    identity = uuid4()
    secret = _os.environ["JWT_SECRET"]
    jwt_cookie = _real_jwt.encode(
        {
            "sub": str(identity),
            "gdx_tid": tenant,
            "role": "admin",
            "typ": "access",
            "iat": 1,
            "exp": 9999999999,
            "jti": str(uuid4()),
        },
        secret,
        algorithm="HS256",
    )
    req = _FakeRequest(
        cookies={"access_token": jwt_cookie},
        state={"tenant": {"id": tenant}},
    )
    principal = await get_current_principal(req)  # type: ignore[arg-type]
    assert principal.auth_kind == "session"
    assert principal.principal_role == "admin"
    # S10 (1.x): CAPABILITY_SETS_BY_ROLE["admin"] is the wildcard. The pre-S10
    # _DEFAULT_ROLE_CAPS["admin"] had ("read","*") in a 5-tuple list; the new
    # canonical shape is the single super-wildcard. Either grants read-access,
    # but the new shape is also a super-admin (is_super_admin=True).
    assert ("*", "*") in principal.capabilities
    assert principal.is_super_admin


# ═══════════════════════════════════════════════════════════════════════════
# Real-signed-JWT integration tests — D-S119-realsigned-jwt-test
# ═══════════════════════════════════════════════════════════════════════════
#
# The tests above mock jwt.decode + finalize_login_jwt. That proves the
# dispatcher invokes the gate stack, but does NOT prove:
#   - signature integrity (a tampered token is rejected),
#   - typ guard fires after a real decode,
#   - Slice 2 DB-verify runs against a real decoded payload,
#   - Slice 6 tenant-match runs against a real decoded payload,
#   - Slice H denylist gate fires (auditor's specific D-item, 2026-05-10).
#
# These tests mint REAL RS256-signed JWTs (matching the Authentik-shape
# the SS-7 primary validator expects) so the decode path is exercised
# cryptographically end-to-end.

from datetime import datetime, timedelta, timezone

import jwt as _real_jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gdx_dispatch.core.denylist import Denylist


_AUTHENTIK_ISSUER_SPA = "https://auth.example.com/application/o/gdx-spa/"
_AUTHENTIK_AUDIENCE = "gdx-api"


@pytest.fixture(scope="module")
def _rs256_keys() -> dict[str, str]:
    """Module-scoped RSA-2048 keypair — keygen is ~50ms so amortize it."""
    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_pem = pk.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return {"private_pem": priv_pem, "public_pem": pub_pem}


@pytest.fixture
def _rs256_mode(_rs256_keys, monkeypatch):
    """Switch gdx_dispatch.routers.auth.core into RS256 mode for one test.

    Monkeypatches module-level ALG/SIGN_KEY/VERIFY_KEY so the dispatcher's
    primary path (validate_principal with the real RS256 key) takes over
    instead of falling through to the legacy HS256 jwt.decode branch. This
    is the only path that consults the SS-7 Slice H denylist — without it,
    the auditor's revoke-jti recipe cannot be exercised.
    """
    from gdx_dispatch.routers.auth import core as _auth_core
    monkeypatch.setattr(_auth_core, "ALG", "RS256")
    monkeypatch.setattr(_auth_core, "SIGN_KEY", _rs256_keys["private_pem"])
    monkeypatch.setattr(_auth_core, "VERIFY_KEY", _rs256_keys["public_pem"])
    return _rs256_keys


def _mint_real_login_jwt(
    *,
    keys: dict[str, str],
    sub: str,
    tenant_id: str,
    role: str = "admin",
    jti: str | None = None,
    typ: str = "access",
    ttl_seconds: int = 900,
    iss: str = _AUTHENTIK_ISSUER_SPA,
    aud: str = _AUTHENTIK_AUDIENCE,
) -> tuple[str, str]:
    """Mint an Authentik-shape RS256 JWT against the test keypair.

    Returns (token, jti). The shape matches what gdx_dispatch.core.auth_jwt.validate_access_token
    expects: full Authentik iss/aud, required claims, plus gdx_tid for the
    tenant binding.
    """
    now = datetime.now(timezone.utc)
    _jti = jti or str(uuid4())
    payload = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": _jti,
        "gdx_tid": tenant_id,
        "role": role,
        "typ": typ,
    }
    token = _real_jwt.encode(payload, keys["private_pem"], algorithm="RS256")
    return token, _jti


@pytest.mark.asyncio
async def test_realsigned_bearer_denylist_revoke_returns_401(_rs256_mode) -> None:
    """Auditor's D-item, exact recipe:

    Mint a real signed JWT, hit the dispatcher → success. Revoke its jti
    on the denylist, hit the dispatcher again → 401. Pins that the Slice H
    denylist gate fires on the dispatcher's bearer-JWT path with no mocks
    on the decode or the gate stack.
    """
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    token, jti = _mint_real_login_jwt(
        keys=_rs256_mode, sub=sub_uuid, tenant_id=tenant_uuid
    )

    denylist = Denylist()
    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)

    def _fresh_req() -> _FakeRequest:
        return _FakeRequest(
            headers={"authorization": f"Bearer {token}"},
            state={"tenant": {"id": tenant_uuid}},
            app_state={"denylist": denylist},
        )

    with patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store):
        principal = await get_current_principal(_fresh_req())  # type: ignore[arg-type]

    assert principal.auth_kind == "session"
    assert principal.tenant_id == tenant_uuid
    assert principal.principal_role == "admin"

    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    denylist.add(jti, expires_at)

    with patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store):
        with pytest.raises(HTTPException) as exc:
            await get_current_principal(_fresh_req())  # type: ignore[arg-type]
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_realsigned_bearer_signature_tamper_returns_401(_rs256_mode) -> None:
    """A real signed JWT with a flipped signature byte must NOT authenticate.

    Pins that the dispatcher's decode actually verifies the signature —
    not just shape-parses claims. Without this, a forged token with valid
    claim shape but invalid signature could slip through if a future
    refactor swapped jwt.decode for an unsigned payload reader.
    """
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    token, _ = _mint_real_login_jwt(
        keys=_rs256_mode, sub=sub_uuid, tenant_id=tenant_uuid
    )

    # Flip one byte in the signature segment.
    header_b64, payload_b64, sig_b64 = token.split(".")
    sig_bytes = bytearray(sig_b64, "ascii")
    sig_bytes[0] = ord("A") if sig_bytes[0] != ord("A") else ord("B")
    tampered = f"{header_b64}.{payload_b64}.{sig_bytes.decode('ascii')}"

    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)
    req = _FakeRequest(
        headers={"authorization": f"Bearer {tampered}"},
        state={"tenant": {"id": tenant_uuid}},
    )
    with patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store):
        with pytest.raises(HTTPException) as exc:
            await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_realsigned_bearer_slice2_db_verify_rejects_deleted_user(_rs256_mode) -> None:
    """Slice 2 contract under a real decode: a deleted/inactive user gets 401.

    finalize_login_jwt calls _db_verify_user which returns None for
    missing/deleted/inactive rows. The dispatcher must propagate that as
    401 (not silently authenticate from JWT claims). Patches the
    _db_verify_user seam to return None — proves the gate is wired,
    without needing a real tenant DB.
    """
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    token, _ = _mint_real_login_jwt(
        keys=_rs256_mode, sub=sub_uuid, tenant_id=tenant_uuid
    )

    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)
    req = _FakeRequest(
        headers={"authorization": f"Bearer {token}"},
        state={"tenant": {"id": tenant_uuid}},
    )

    with (
        patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store),
        patch("gdx_dispatch.routers.auth.core._db_verify_user", return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_realsigned_bearer_slice6_tenant_mismatch_returns_403(_rs256_mode) -> None:
    """Slice 6 contract under a real decode: tenant_id in JWT != host tenant = 403.

    Token minted for tenant A, presented on host that resolved tenant B.
    finalize_login_jwt -> _enforce_tenant_match raises 403; the dispatcher
    must propagate. Pre-fix the cookie path could authenticate on
    mismatched tenants because it never reached this gate.
    """
    sub_uuid = str(uuid4())
    jwt_tenant = str(uuid4())
    host_tenant = str(uuid4())
    assert jwt_tenant != host_tenant
    token, _ = _mint_real_login_jwt(
        keys=_rs256_mode, sub=sub_uuid, tenant_id=jwt_tenant
    )

    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)
    req = _FakeRequest(
        headers={"authorization": f"Bearer {token}"},
        state={"tenant": {"id": host_tenant}},
    )

    with patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store):
        with pytest.raises(HTTPException) as exc:
            await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# Cookie-path gate tests — D-S119-session-cookie-gates
# ═══════════════════════════════════════════════════════════════════════════
#
# Pre-fix, _dispatch_session did an UNVERIFIED base64 decode of JWT-shape
# cookies and built a Principal from raw claims. The fix routes JWT-shape
# cookies through _dispatch_login_jwt so they get the same gate stack
# (signature, typ, Slice 2, Slice 6, Slice H) as the bearer path.


@pytest.mark.asyncio
async def test_realsigned_cookie_denylist_revoke_returns_401(_rs256_mode) -> None:
    """Cookie-path parity for the auditor's revoke-jti recipe.

    Same shape as the bearer denylist test but JWT arrives via the
    access_token cookie. Without the cookie-path fix, revoking the jti
    has no effect — the cookie decode bypasses the denylist entirely.
    """
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    token, jti = _mint_real_login_jwt(
        keys=_rs256_mode, sub=sub_uuid, tenant_id=tenant_uuid
    )

    denylist = Denylist()

    def _fresh_req() -> _FakeRequest:
        return _FakeRequest(
            cookies={"access_token": token},
            state={"tenant": {"id": tenant_uuid}},
            app_state={"denylist": denylist},
        )

    principal = await get_current_principal(_fresh_req())  # type: ignore[arg-type]
    assert principal.auth_kind == "session"
    assert principal.tenant_id == tenant_uuid

    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    denylist.add(jti, expires_at)

    with pytest.raises(HTTPException) as exc:
        await get_current_principal(_fresh_req())  # type: ignore[arg-type]
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_realsigned_cookie_slice6_tenant_mismatch_returns_403(_rs256_mode) -> None:
    """Cookie-path Slice 6 parity: JWT cookie tenant != host tenant = 403.

    Pre-fix this was a silent cross-tenant authentication: a JWT cookie
    for tenant A presented on tenant B's host would build a Principal
    with tenant A's id (because the unverified decode read gdx_tid
    directly).
    """
    sub_uuid = str(uuid4())
    jwt_tenant = str(uuid4())
    host_tenant = str(uuid4())
    assert jwt_tenant != host_tenant
    token, _ = _mint_real_login_jwt(
        keys=_rs256_mode, sub=sub_uuid, tenant_id=jwt_tenant
    )

    req = _FakeRequest(
        cookies={"access_token": token},
        state={"tenant": {"id": host_tenant}},
    )
    with pytest.raises(HTTPException) as exc:
        await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# D-S119-legacy-denylist-gap regression tests
# ═══════════════════════════════════════════════════════════════════════════
#
# Surfaced 2026-05-10 by prod-walk: /auth/admin/revoke wrote the jti to
# Redis correctly, but the post-revoke probe returned 200 (token still
# worked). Root cause: locally-signed login JWTs (which is 100% of prod
# tokens — `_issue()` in /auth/login mints without Authentik iss/aud)
# bypass `validate_principal` (UnsupportedProvider) and land on the legacy
# `jwt.decode` branch, which never consulted the denylist.
#
# Fix: moved the Slice H denylist check into `finalize_login_jwt`, the
# shared post-decode helper both primary and legacy paths already call
# (the S118 round-1 lesson: gates live in the shared helper, not parallel
# decoders). These tests pin the gate at the bearer, cookie, and
# OAuth-fallthrough entry points.
#
# Industry references (research 2026-05-10):
# - FAPI 2.0 §5.3.1: "Resource servers SHALL verify the validity,
#   integrity, expiration and revocation status of access tokens."
# - FusionAuth + flask-jwt-extended: denylist runs AFTER decode succeeds,
#   BEFORE the principal is built / handler runs.


def _mint_legacy_login_jwt(
    *,
    sub: str,
    tenant_id: str,
    jti: str | None = None,
    role: str = "admin",
    typ: str = "access",
    ttl_seconds: int = 900,
) -> tuple[str, str]:
    """Mint a locally-signed HS256 JWT shaped like prod's /auth/login `_issue()`.

    No Authentik iss/aud — that's the prod token shape. Forces the
    dispatcher / get_current_user onto the legacy decode branch, which is
    where the denylist gap lived.

    Returns (token, jti).
    """
    import os as _os
    import jwt as _real_jwt
    _jti = jti or str(uuid4())
    payload = {
        "sub": sub,
        "gdx_tid": tenant_id,
        "tenant_id": tenant_id,
        "role": role,
        "typ": typ,
        "iat": 1,
        "exp": 9999999999,
        "jti": _jti,
    }
    secret = _os.environ["JWT_SECRET"]
    return _real_jwt.encode(payload, secret, algorithm="HS256"), _jti


@pytest.mark.asyncio
async def test_legacy_bearer_jwt_with_revoked_jti_returns_401() -> None:
    """Bearer login JWT whose jti is on the denylist → 401.

    Pre-fix: the locally-signed JWT lands on the legacy decode branch,
    `finalize_login_jwt` runs Slice 2 + Slice 6 but NOT Slice H, so the
    revoke is invisible. Token authenticates anyway.

    Post-fix: `finalize_login_jwt` checks the denylist first and raises
    401 "Token revoked" before any DB lookup.

    This is the test that — had it existed — would have caught the gap
    that shipped through every auth-identity-hardening release.
    """
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    token, jti = _mint_legacy_login_jwt(sub=sub_uuid, tenant_id=tenant_uuid)

    denylist = Denylist()
    denylist.add(jti, datetime.now(timezone.utc) + timedelta(hours=1))

    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)
    req = _FakeRequest(
        headers={"authorization": f"Bearer {token}"},
        state={"tenant": {"id": tenant_uuid}},
        app_state={"denylist": denylist},
    )
    with patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store):
        with pytest.raises(HTTPException) as exc:
            await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_legacy_bearer_jwt_not_revoked_still_works() -> None:
    """Negative control: an UN-revoked legacy login JWT still authenticates.

    Pins that the new denylist gate only rejects when the jti is actually
    listed — doesn't accidentally reject every legacy token (which would
    be a worse bug than what we're fixing).
    """
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    token, _ = _mint_legacy_login_jwt(sub=sub_uuid, tenant_id=tenant_uuid)

    denylist = Denylist()  # empty — no jtis revoked

    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)
    req = _FakeRequest(
        headers={"authorization": f"Bearer {token}"},
        state={"tenant": {"id": tenant_uuid}},
        app_state={"denylist": denylist},
    )
    with patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store):
        principal = await get_current_principal(req)  # type: ignore[arg-type]
    assert principal.auth_kind == "session"
    assert principal.principal_role == "admin"


@pytest.mark.asyncio
async def test_legacy_cookie_jwt_with_revoked_jti_returns_401() -> None:
    """Cookie-path parity: a JWT cookie whose jti is denylisted → 401.

    The cookie path was wired into _dispatch_login_jwt in D-S119-session-
    cookie-gates earlier this sprint. With the finalize_login_jwt fix,
    cookie auth now also honors revoke — pins that parity.
    """
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    token, jti = _mint_legacy_login_jwt(sub=sub_uuid, tenant_id=tenant_uuid)

    denylist = Denylist()
    denylist.add(jti, datetime.now(timezone.utc) + timedelta(hours=1))

    req = _FakeRequest(
        cookies={"access_token": token},
        state={"tenant": {"id": tenant_uuid}},
        app_state={"denylist": denylist},
    )
    with pytest.raises(HTTPException) as exc:
        await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_denylist_gate_fires_before_db_verify() -> None:
    """Order pin: denylist fires BEFORE Slice 2 DB-verify.

    A revoked token should NOT trigger a tenant DB lookup. Matches the
    FusionAuth-documented order. Asserts that when both the jti is
    denylisted AND _db_verify_user WOULD return None, we get the
    "Token revoked" path, not the "User no longer eligible" path —
    which can only happen if denylist runs first.
    """
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    token, jti = _mint_legacy_login_jwt(sub=sub_uuid, tenant_id=tenant_uuid)

    denylist = Denylist()
    denylist.add(jti, datetime.now(timezone.utc) + timedelta(hours=1))

    db_verify_called = MagicMock()
    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)
    req = _FakeRequest(
        headers={"authorization": f"Bearer {token}"},
        state={"tenant": {"id": tenant_uuid}},
        app_state={"denylist": denylist},
    )
    with (
        patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store),
        patch("gdx_dispatch.routers.auth.core._db_verify_user", db_verify_called),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 401
    # _db_verify_user MUST NOT have been called — denylist short-circuits first.
    db_verify_called.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_bearer_jwt_without_jti_does_not_crash() -> None:
    """Defensive: a legacy JWT that lacks a `jti` claim (malformed but
    decodable) must not crash the denylist gate. Pre-fix `_issue()` always
    mints a jti, but a future minter or a hand-rolled token might omit it.
    The fix: `if jti and ...contains(...)` short-circuits cleanly.
    """
    sub_uuid = str(uuid4())
    tenant_uuid = str(uuid4())
    # Mint manually without a jti claim.
    import os as _os
    import jwt as _real_jwt
    payload = {
        "sub": sub_uuid,
        "gdx_tid": tenant_uuid,
        "role": "admin",
        "typ": "access",
        "iat": 1,
        "exp": 9999999999,
        # NO jti claim
    }
    token = _real_jwt.encode(payload, _os.environ["JWT_SECRET"], algorithm="HS256")

    denylist = Denylist()  # empty
    store = MagicMock()
    store.get_by_access = MagicMock(return_value=None)
    req = _FakeRequest(
        headers={"authorization": f"Bearer {token}"},
        state={"tenant": {"id": tenant_uuid}},
        app_state={"denylist": denylist},
    )
    with patch("gdx_dispatch.routers.auth.oauth2.get_token_store", return_value=store):
        principal = await get_current_principal(req)  # type: ignore[arg-type]
    assert principal.auth_kind == "session"


# Note: test_opaque_session_cookie_cannot_reach_role_gated_routes was
# deleted as part of D-S119-opaque-cookie-deprecate — it pinned a code
# path that has been removed. Replacement contract tests live above:
# test_opaque_session_cookie_returns_401_missing_credentials,
# test_sid_cookie_is_ignored, test_only_access_token_cookie_is_recognized.
