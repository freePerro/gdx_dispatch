"""SS-7 Slice H — unit tests for ``gdx_dispatch.routers.auth.get_current_user``.

Slice H moves the denylist from a module-level singleton (Slice G) onto an
app-scoped seam at ``request.app.state.denylist``. The revoke endpoint
(writer) and :func:`get_current_user` (reader) now resolve the denylist
through the same :func:`_get_app_denylist` helper, so they share an
*instance* per FastAPI app while remaining naturally isolated between
independent apps (one per test, one per worker at runtime).

The contract bullets these tests pin:

1. A successful core-validator call returns the expected
   ``dict[str, str]`` shape derived from the returned :class:`Principal`.
2. A :class:`JWTValidationError` from the core validator maps to 401 via
   :func:`gdx_dispatch.routers.auth._unauth` (no 500, no internal leak).
3. :class:`TokenRevoked` is treated as 401 specifically — not a 500 — and
   its diagnostic message is NOT leaked through the response body.
4. The raw token string passes through verbatim into the core validator
   AND the legacy ``jwt.decode`` fallback so HS256 tokens minted by
   :func:`_issue` continue to authenticate during the migration window.
5. (Slice H) The denylist resolved for ``get_current_user`` is the *same
   instance* (``is``) as the one the admin revoke endpoint writes to,
   proving app-scoped lifecycle: revoke-write → auth-read sees the
   revocation without any cross-worker synchronization.
6. (Slice H) If ``request.app.state`` has no ``denylist`` attribute yet,
   the first call lazily creates one and stores it there; subsequent
   calls return the same stored instance (identity preserved).

The tests are hermetic: ``monkeypatch`` replaces
``gdx_dispatch.core.auth.validate_principal`` per-case, no FastAPI test client is
spun up, no DB or network is touched, and fake ``Request`` objects are
built from :class:`types.SimpleNamespace` so ``request.app.state`` is the
only app surface that matters for the seam.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from gdx_dispatch.core.auth_jwt import (
    InvalidSignature,
    JWTValidationError,
    MalformedToken,
    TokenRevoked,
)
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.principal import ActorKind, Principal
from gdx_dispatch.routers.auth import core as auth_router  # patch target post Slice 8 Phase A —
# the package shim at gdx_dispatch.routers.auth re-exports these names but functions
# in core.py resolve them via core's globals; monkeypatch must target core.

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _principal(
    *,
    subject: str = "user-42",
    tenant_id: str = "tenant-gdx",
    role: str = "owner",
    jti: str | None = None,
) -> Principal:
    """Construct a ``Principal`` matching what SS-6 SPA tokens yield."""
    issued_at = int(datetime.now(UTC).timestamp())
    return Principal(
        tenant_id=tenant_id,
        subject=subject,
        provider="gdx-spa",
        actor_kind=ActorKind.HUMAN,
        identity_type="human",
        issued_at=issued_at,
        expires_at=issued_at + 900,
        issuer="https://auth.example.com/application/o/gdx-spa/",
        audience="gdx-api",
        jti=jti,
        raw_claims={"sub": subject, "gdx_tid": tenant_id, "role": role},
    )


def _legacy_token(
    *,
    sub: str = "legacy-user",
    tenant_id: str = "legacy-tenant",
    role: str = "user",
    typ: str = "access",
    exp_offset_seconds: int = 900,
) -> str:
    """Mint a legacy HS256/RS256 token using the router's module-level keys.

    Mirrors :func:`gdx_dispatch.routers.auth._issue` — same claim set, same signing
    key and algorithm — so the fallback :func:`jwt.decode` path accepts it
    without any monkeypatching of the router-level globals.
    """
    claims = {
        "sub": sub,
        "tenant_id": tenant_id,
        "role": role,
        "jti": str(uuid4()),
        "typ": typ,
        "exp": int((datetime.now(UTC) + timedelta(seconds=exp_offset_seconds)).timestamp()),
    }
    return pyjwt.encode(claims, auth_router.SIGN_KEY, algorithm=auth_router.ALG)


def _fake_request(
    *,
    denylist: Denylist | None = None,
    tenant_id: str | None = "tenant-gdx",
    cookies: dict[str, str] | None = None,
    request_id: str = "test-req-1",
) -> SimpleNamespace:
    """Build a minimal ``request``-shaped object for direct dependency calls.

    Starlette's real ``Request.app.state`` is a ``starlette.datastructures.State``
    namespace that accepts attribute get/set. A ``SimpleNamespace`` is the
    smallest thing that satisfies that contract for the seam under test.

    When ``denylist`` is provided the app pre-seeds ``app.state.denylist``
    with it (so tests can assert identity against a known instance).
    When omitted, ``app.state`` is empty — exercising the lazy-create
    path inside :func:`gdx_dispatch.routers.auth._get_app_denylist`.

    ``tenant_id`` seeds ``request.state.tenant`` so endpoints that read the
    tenant middleware contract (Slice I audit logging) can resolve an id
    without touching real middleware. Pass ``None`` to simulate an absent
    tenant — the endpoint must fall back to ``""`` in that case.

    ``cookies`` seeds the cookie dict so refresh-token / access-token
    cookie flows can be exercised.
    """
    state = SimpleNamespace()
    if denylist is not None:
        state.denylist = denylist
    app = SimpleNamespace(state=state)
    request_state = SimpleNamespace()
    request_state.request_id = request_id
    if tenant_id is not None:
        request_state.tenant = {"id": tenant_id}
    return SimpleNamespace(
        app=app,
        state=request_state,
        cookies=cookies or {},
        headers={},
        url=SimpleNamespace(path="/auth/refresh"),
        client=SimpleNamespace(host="127.0.0.1"),
    )


# ---------------------------------------------------------------------------
# 1. Happy path — core validator returns a Principal → router returns dict
# ---------------------------------------------------------------------------


def test_valid_principal_is_mapped_to_dict(monkeypatch):
    captured: dict[str, Any] = {}
    principal = _principal(subject="user-42", tenant_id="tenant-gdx", role="owner")

    def fake_validate_principal(token: str, **kwargs: Any) -> Principal:
        captured["token"] = token
        captured["kwargs"] = kwargs
        return principal

    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        fake_validate_principal,
    )

    request = _fake_request(denylist=Denylist())
    result = asyncio.run(
        auth_router.get_current_user(request=request, token="opaque-token-string"),
    )

    # Return-shape contract: user_id/tenant_id/role plus impersonation
    # markers (imp_actor_id/imp_purpose) added in cc2-s46 for the CC
    # impersonate flow. None on normal tokens; populated when the issuer
    # mints an impersonation token. Asserting the strict 3-key dict is
    # stale — assert each load-bearing field independently.
    assert result["user_id"] == "user-42"
    assert result["tenant_id"] == "tenant-gdx"
    assert result["role"] == "owner"
    assert result["imp_actor_id"] is None
    assert result["imp_purpose"] is None
    # The core validator received the token verbatim (no re-encoding or
    # mangling by the router).
    assert captured["token"] == "opaque-token-string"
    # The router must supply a keyword-only ``public_keys_by_provider`` map
    # so the validator has keys to try, even when the legacy HS256 path is
    # the active deployment mode.
    assert "public_keys_by_provider" in captured["kwargs"]


def test_role_defaults_to_user_when_claim_absent(monkeypatch):
    # Authentik-minted access tokens may omit ``role`` entirely (the claim
    # is not in the SS-6 property mapping). The router must default rather
    # than raise KeyError.
    principal = Principal(
        tenant_id="tenant-gdx",
        subject="user-42",
        provider="gdx-spa",
        actor_kind=ActorKind.HUMAN,
        identity_type="human",
        issued_at=0,
        expires_at=1,
        issuer="https://auth.example.com/application/o/gdx-spa/",
        audience="gdx-api",
        jti=None,
        raw_claims={"sub": "user-42", "gdx_tid": "tenant-gdx"},
    )
    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        lambda token, **_: principal,
    )

    request = _fake_request(denylist=Denylist())
    result = asyncio.run(auth_router.get_current_user(request=request, token="any"))

    assert result["role"] == "user"


# ---------------------------------------------------------------------------
# 2. JWTValidationError → 401 (legacy fallback also fails on this token)
# ---------------------------------------------------------------------------


def test_jwt_validation_error_becomes_401_when_legacy_also_fails(monkeypatch):
    def raise_jwt_error(token: str, **_: Any) -> Principal:
        raise InvalidSignature("signature did not verify")

    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        raise_jwt_error,
    )

    request = _fake_request(denylist=Denylist())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            auth_router.get_current_user(request=request, token="not.a.real.jwt"),
        )

    # 401, not 500 — the typed error must not leak as an unhandled
    # InvalidSignature exception (which would surface as an opaque 500).
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired access token"


def test_generic_jwt_validation_error_subclass_maps_to_401(monkeypatch):
    # Any JWTValidationError subclass (not just InvalidSignature) must
    # collapse to 401 — the router exception handler must pattern-match on
    # the base class, not a specific subtype.
    class _Custom(JWTValidationError):
        pass

    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        lambda token, **_: (_ for _ in ()).throw(_Custom("custom failure")),
    )

    request = _fake_request(denylist=Denylist())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            auth_router.get_current_user(request=request, token="not.a.real.jwt"),
        )

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# 3. TokenRevoked → 401, not 500
# ---------------------------------------------------------------------------


def test_token_revoked_is_401_never_500(monkeypatch):
    def raise_revoked(token: str, **_: Any) -> Principal:
        raise TokenRevoked("token jti 'abc123' is on the revocation denylist")

    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        raise_revoked,
    )

    request = _fake_request(denylist=Denylist())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            auth_router.get_current_user(request=request, token="not.a.real.jwt"),
        )

    assert exc_info.value.status_code == 401
    # Do not leak internals — the 401 body must remain the generic
    # router-level message, not the denylist diagnostic string.
    assert exc_info.value.detail == "Invalid or expired access token"
    assert "denylist" not in exc_info.value.detail
    assert "abc123" not in exc_info.value.detail


def test_malformed_token_from_core_also_maps_to_401(monkeypatch):
    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        lambda token, **_: (_ for _ in ()).throw(MalformedToken("parse failed")),
    )

    request = _fake_request(denylist=Denylist())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(auth_router.get_current_user(request=request, token="garbage"))

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# 4. Legacy token pass-through — HS256 token accepted via fallback
# ---------------------------------------------------------------------------


def test_legacy_token_passes_through_to_validator_then_legacy_fallback(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_validate_principal(token: str, **kwargs: Any) -> Principal:
        # Record what the core validator received so we can assert the
        # token string was passed through verbatim (no re-encoding).
        captured["token"] = token
        raise InvalidSignature(
            "not an Authentik-shaped token — fall through to legacy decode"
        )

    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        fake_validate_principal,
    )

    token = _legacy_token(sub="legacy-user", tenant_id="legacy-tenant", role="admin")

    # Slice 6 cross-check enforces token tenant == host tenant; this test
    # exercises the HS256 fallback contract, not the cross-check, so use
    # a host with the matching tenant id.
    request = _fake_request(denylist=Denylist(), tenant_id="legacy-tenant")
    result = asyncio.run(auth_router.get_current_user(request=request, token=token))

    # Core validator saw the exact token string (pass-through contract).
    assert captured["token"] == token
    # Legacy fallback accepted the HS256 token and returned the dict the
    # rest of the app expects — HS256 compatibility preserved. Plus the
    # impersonation markers (None on legacy non-impersonation tokens).
    assert result["user_id"] == "legacy-user"
    assert result["tenant_id"] == "legacy-tenant"
    assert result["role"] == "admin"
    assert result["imp_actor_id"] is None
    assert result["imp_purpose"] is None


# ---------------------------------------------------------------------------
# 4.5 D-S119-legacy-denylist-gap regression
# ---------------------------------------------------------------------------
#
# Surfaced 2026-05-10 on prod: /auth/admin/revoke wrote the jti correctly
# but the post-revoke probe still returned 200. Root cause: prod's
# /auth/login mints locally-signed JWTs (no Authentik iss/aud), so they
# always land on the legacy decode branch — which never called the
# denylist gate. Fix: denylist check moved into finalize_login_jwt (the
# shared post-decode helper) so it runs for BOTH primary and legacy
# branches. These tests pin that contract on get_current_user. Without
# the fix the first test below fails (token still authenticates).


def test_legacy_token_with_revoked_jti_returns_401(monkeypatch):
    """Pin: revoking a jti via /auth/admin/revoke must take effect on the
    legacy decode path (which is 100% of prod traffic since /auth/login
    mints locally-signed tokens without Authentik iss/aud).

    Pre-fix: validate_principal raises (non-Authentik shape) → legacy
    jwt.decode succeeds → finalize_login_jwt runs Slice 2 + Slice 6 →
    request authenticates. Denylist never consulted.

    Post-fix: finalize_login_jwt runs Slice H FIRST → 401 "Token revoked"
    before Slice 2's DB hit.
    """
    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        lambda token, **_: (_ for _ in ()).throw(
            InvalidSignature("force legacy path")
        ),
    )

    # Mint a legacy-shape token with a known jti so we can denylist it.
    known_jti = "regression-test-jti-d-s119"
    claims = {
        "sub": "legacy-user",
        "tenant_id": "legacy-tenant",
        "role": "admin",
        "jti": known_jti,
        "typ": "access",
        "exp": int((datetime.now(UTC) + timedelta(seconds=900)).timestamp()),
    }
    token = pyjwt.encode(claims, auth_router.SIGN_KEY, algorithm=auth_router.ALG)

    denylist = Denylist()
    denylist.add(known_jti, datetime.now(UTC) + timedelta(hours=1))

    request = _fake_request(denylist=denylist, tenant_id="legacy-tenant")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(auth_router.get_current_user(request=request, token=token))

    assert exc_info.value.status_code == 401


def test_legacy_token_denylist_gate_fires_before_db_verify(monkeypatch):
    """Order pin: Slice H runs BEFORE Slice 2 DB-verify in finalize_login_jwt.

    A revoked token must NOT trigger a tenant DB lookup. Matches the
    FusionAuth-documented order — cheap gates first, expensive gates
    after. Detected via: patch _db_verify_user; on a revoked token it
    must NOT be called.
    """
    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        lambda token, **_: (_ for _ in ()).throw(
            InvalidSignature("force legacy path")
        ),
    )
    db_verify_mock = MagicMock()
    monkeypatch.setattr(
        "gdx_dispatch.routers.auth.core._db_verify_user", db_verify_mock,
    )

    known_jti = "order-test-jti"
    claims = {
        "sub": "u", "tenant_id": "t", "role": "admin", "jti": known_jti,
        "typ": "access",
        "exp": int((datetime.now(UTC) + timedelta(seconds=900)).timestamp()),
    }
    token = pyjwt.encode(claims, auth_router.SIGN_KEY, algorithm=auth_router.ALG)

    denylist = Denylist()
    denylist.add(known_jti, datetime.now(UTC) + timedelta(hours=1))

    request = _fake_request(denylist=denylist, tenant_id="t")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(auth_router.get_current_user(request=request, token=token))
    assert exc_info.value.status_code == 401
    # The DB-verify seam must NOT have been called — Slice H short-circuits first.
    db_verify_mock.assert_not_called()


def test_legacy_token_without_jti_does_not_crash_denylist_gate(monkeypatch):
    """Defensive: a legacy token without a jti claim must not crash the
    new Slice H gate. The `if jti and ...` guard short-circuits cleanly."""
    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        lambda token, **_: (_ for _ in ()).throw(
            InvalidSignature("force legacy path")
        ),
    )
    # Manually craft a token with no jti claim.
    claims = {
        "sub": "u", "tenant_id": "t", "role": "user", "typ": "access",
        "exp": int((datetime.now(UTC) + timedelta(seconds=900)).timestamp()),
        # NO jti
    }
    token = pyjwt.encode(claims, auth_router.SIGN_KEY, algorithm=auth_router.ALG)

    request = _fake_request(denylist=Denylist(), tenant_id="t")
    result = asyncio.run(auth_router.get_current_user(request=request, token=token))
    assert result["user_id"] == "u"


def test_legacy_token_with_non_access_typ_is_rejected(monkeypatch):
    # The fallback path still enforces the ``typ ∈ {None, "access"}``
    # invariant — a refresh token mistakenly presented as a bearer must
    # not authenticate the caller as a user.
    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        lambda token, **_: (_ for _ in ()).throw(
            InvalidSignature("fall through to legacy")
        ),
    )

    refresh_shaped_token = _legacy_token(typ="refresh")

    request = _fake_request(denylist=Denylist())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            auth_router.get_current_user(request=request, token=refresh_shaped_token),
        )

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# 5. SS-7 Slice H — app-scoped denylist lifecycle seam
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_denylist():
    """Fresh :class:`Denylist` placed on a fresh fake ``app.state`` per test.

    Slice G relied on a module-level singleton that had to be monkeypatched
    away between tests. Slice H makes the denylist live on ``app.state``
    instead — one app per test is naturally isolated, so this fixture just
    hands back ``(denylist, request)`` pre-wired together.
    """
    denylist = Denylist()
    request = _fake_request(denylist=denylist)
    return denylist, request


def test_get_app_denylist_returns_existing_state_instance(isolated_denylist):
    # Identity contract: if ``app.state.denylist`` already holds a Denylist,
    # ``_get_app_denylist`` must return that exact object — never a copy,
    # never a fresh instance. This is the property Slice H is paid for.
    denylist, request = isolated_denylist

    resolved = auth_router._get_app_denylist(request)

    assert resolved is denylist


def test_get_app_denylist_lazily_creates_when_missing():
    # First request into a fresh app: ``app.state`` has no denylist attribute.
    # ``_get_app_denylist`` must create one, stash it on ``app.state``, AND
    # return that same object. Subsequent calls must return the stored
    # instance (no further creations, no drift).
    request = _fake_request()  # no denylist pre-seeded
    assert not hasattr(request.app.state, "denylist")

    first = auth_router._get_app_denylist(request)

    assert isinstance(first, Denylist)
    # Stored on app.state for the next caller.
    assert request.app.state.denylist is first

    # Second call on the same app returns the stored instance (identity,
    # not a fresh Denylist).
    second = auth_router._get_app_denylist(request)
    assert second is first


def test_revoke_endpoint_records_jti_on_app_scoped_denylist(
    isolated_denylist, monkeypatch,
):
    # Write-side contract: calling the endpoint function directly with a
    # request bound to a known denylist records the jti on *that* denylist,
    # not a hidden module-global.
    #
    # Slice I added ``current_user`` and ``db`` parameters plus an audit
    # emission; we stub both to keep this test focused on the denylist
    # write (audit payload shape is pinned by a separate test below).
    denylist, request = isolated_denylist
    expires_at = datetime.now(UTC) + timedelta(seconds=900)
    body = auth_router.RevokeTokenBody(jti="rev-jti-42", expires_at=expires_at)
    monkeypatch.setattr(auth_router, "log_audit_event_sync", lambda *_a, **_k: None)
    fake_db = SimpleNamespace(commit=lambda: None)
    current_user = {"user_id": "admin-1", "tenant_id": "tenant-gdx", "role": "admin"}

    result = auth_router.admin_revoke_token(body, request, current_user, fake_db)

    assert result == {"status": "ok"}
    # ``contains`` is the public query surface; a concrete ``True`` proves
    # the jti was registered AND is not yet expired (the time-aware branch
    # in ``Denylist.contains`` ran to completion).
    assert denylist.contains("rev-jti-42") is True


# ---------------------------------------------------------------------------
# 6. SS-7 Slice I — audit log emission on successful revoke
# ---------------------------------------------------------------------------


def test_revoke_emits_audit_event_with_expected_payload(
    isolated_denylist, monkeypatch,
):
    # Audit contract: on a successful revoke the endpoint calls
    # ``log_audit_event_sync`` exactly once, with the fields pinned by the
    # Slice I task — ``tenant_id`` from ``request.state.tenant``, ``user_id``
    # from the ``Depends(get_current_user)`` dict, ``action="token_revoked"``,
    # ``entity_type="auth"``, ``entity_id=body.jti``, and ``details`` carrying
    # the ISO-8601 expires_at. The request object is passed through so the
    # audit helper can extract IP/request-id on its own.
    denylist, request = isolated_denylist
    expires_at = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
    body = auth_router.RevokeTokenBody(jti="rev-jti-audit", expires_at=expires_at)
    current_user = {"user_id": "admin-42", "tenant_id": "tenant-gdx", "role": "owner"}

    audit_calls: list[dict[str, Any]] = []

    def fake_audit(db: Any, *args: Any, **kwargs: Any) -> None:
        audit_calls.append({"db": db, "args": args, "kwargs": kwargs})

    monkeypatch.setattr(auth_router, "log_audit_event_sync", fake_audit)

    commits: list[bool] = []
    fake_db = SimpleNamespace(commit=lambda: commits.append(True))

    result = auth_router.admin_revoke_token(body, request, current_user, fake_db)

    assert result == {"status": "ok"}
    assert len(audit_calls) == 1, f"expected exactly one audit call: {audit_calls}"
    call = audit_calls[0]
    assert call["db"] is fake_db
    kw = call["kwargs"]
    assert kw["tenant_id"] == "tenant-gdx"
    assert kw["user_id"] == "admin-42"
    assert kw["action"] == "token_revoked"
    assert kw["entity_type"] == "auth"
    assert kw["entity_id"] == "rev-jti-audit"
    assert kw["details"] == {"expires_at": expires_at.isoformat()}
    # The raw token must never land in audit details — only the jti and
    # expires_at metadata per the Slice I privacy rule.
    assert "token" not in kw["details"]
    assert kw["request"] is request
    # db.commit() must fire after the audit insert, matching the pattern
    # used by the login/refresh/logout endpoints.
    assert commits == [True]
    # Denylist write is unchanged — the audit insertion MUST NOT skip or
    # defer the write.
    assert denylist.contains("rev-jti-audit") is True


def test_revoke_audit_failure_still_returns_ok(isolated_denylist, monkeypatch):
    # Fail-open contract: if the audit helper raises, the endpoint must
    # still return the Slice G success payload and the denylist write must
    # still be durable. Rationale: an audit-chain outage cannot be allowed
    # to convert a successful security control (revoke) into a 500 — the
    # revocation already happened in memory; surfacing it as failure would
    # tell the admin the revoke did not land, which is actively wrong.
    denylist, request = isolated_denylist
    expires_at = datetime.now(UTC) + timedelta(seconds=900)
    body = auth_router.RevokeTokenBody(jti="rev-jti-failaudit", expires_at=expires_at)
    current_user = {"user_id": "admin-7", "tenant_id": "tenant-gdx", "role": "admin"}

    def boom(db: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(auth_router, "log_audit_event_sync", boom)
    fake_db = SimpleNamespace(commit=lambda: None)

    result = auth_router.admin_revoke_token(body, request, current_user, fake_db)

    assert result == {"status": "ok"}
    # Denylist write is not rolled back by an audit failure — the jti is
    # still revoked from the reader's point of view.
    assert denylist.contains("rev-jti-failaudit") is True


def test_revoke_tenant_fallback_when_request_state_missing_tenant(monkeypatch):
    # When the tenant middleware hasn't populated ``request.state.tenant``
    # (edge case: platform-level callers, test paths that bypass the
    # middleware), the audit ``tenant_id`` must fall back to ``""`` rather
    # than raising AttributeError or KeyError. The rest of the payload
    # must remain well-formed.
    denylist = Denylist()
    # tenant_id=None ⇒ no ``state.tenant`` attribute at all, exercising the
    # ``getattr(..., "tenant", {})`` fallback branch in the endpoint.
    request = _fake_request(denylist=denylist, tenant_id=None)
    expires_at = datetime.now(UTC) + timedelta(seconds=900)
    body = auth_router.RevokeTokenBody(jti="rev-jti-notenant", expires_at=expires_at)
    current_user = {"user_id": "admin-9", "tenant_id": "", "role": "owner"}

    captured: dict[str, Any] = {}

    def fake_audit(db: Any, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(auth_router, "log_audit_event_sync", fake_audit)
    fake_db = SimpleNamespace(commit=lambda: None)

    result = auth_router.admin_revoke_token(body, request, current_user, fake_db)

    assert result == {"status": "ok"}
    assert captured["tenant_id"] == ""
    assert captured["user_id"] == "admin-9"
    assert captured["entity_id"] == "rev-jti-notenant"


def test_revoke_endpoint_has_admin_owner_role_guard():
    # Inspect the route's dependency chain rather than spinning up a client —
    # the role guard must be wired at the router level so unauthorized
    # callers are rejected before the endpoint body ever runs. Mirrors the
    # assertion pattern in ``gdx_dispatch/tests/test_gdpr.py::test_export_customer_requires_admin``.
    route = next(
        r for r in auth_router.router.routes
        if getattr(r, "path", "") == "/auth/admin/revoke"
    )
    dep_calls = [d.call for d in route.dependant.dependencies]
    role_guard_present = any(
        getattr(c, "__qualname__", "").endswith("require_role.<locals>._dependency")
        for c in dep_calls
    )
    assert role_guard_present, f"no require_role guard on revoke route: {dep_calls}"


def test_require_role_rejects_non_admin_caller():
    # Exercise the guard directly against a fake Request whose ``state.user``
    # carries a non-privileged role. The guard must raise ``HTTPException(403)``
    # — this is the same surface FastAPI would hit before dispatching to the
    # endpoint body, so asserting it here proves non-admins cannot revoke.
    from gdx_dispatch.core.modules import require_role as _require_role

    dependency = _require_role("owner", "admin")
    fake_request = SimpleNamespace(
        state=SimpleNamespace(
            current_user={"role": "user"},
            user={"role": "user"},
        ),
        app=SimpleNamespace(dependency_overrides={}),
        headers={},
    )

    with pytest.raises(HTTPException) as exc_info:
        dependency(fake_request)

    assert exc_info.value.status_code == 403


def test_get_current_user_passes_app_scoped_denylist_to_core_validator(
    monkeypatch, isolated_denylist,
):
    # Pin the wiring contract: ``get_current_user`` MUST hand the denylist
    # from ``request.app.state`` to ``validate_principal`` so revocations
    # written by the admin endpoint are observed by the very next request.
    # An identity check (``is``) is load-bearing here — passing a *copy*
    # would silently lose writes.
    denylist, request = isolated_denylist
    captured: dict[str, Any] = {}

    def fake_validate_principal(token: str, **kwargs: Any) -> Principal:
        captured["kwargs"] = kwargs
        raise InvalidSignature("force fallthrough for assertion")

    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        fake_validate_principal,
    )

    with pytest.raises(HTTPException):
        # Legacy fallback also fails on a garbage token → the HTTPException is
        # expected. The capture happens inside the core-validator path before
        # the fallback, which is what we assert against.
        asyncio.run(
            auth_router.get_current_user(request=request, token="garbage.not.jwt"),
        )

    assert captured["kwargs"].get("denylist") is denylist


def test_revoke_and_read_share_same_app_scoped_denylist(monkeypatch):
    # End-to-end identity contract for Slice H: a single request-like object
    # (same app) is used first to revoke a jti, then to read. The core
    # validator sees the *same* denylist instance on the read — no module
    # globals, no copies. This is the property app-scoped lifecycle buys.
    #
    # Slice I added audit wiring to the endpoint. The Slice H identity
    # contract under test here is orthogonal to audit emission, so we stub
    # the audit helper out to keep the failure surface tight.
    request = _fake_request()  # lazy-create path
    expires_at = datetime.now(UTC) + timedelta(seconds=900)
    monkeypatch.setattr(auth_router, "log_audit_event_sync", lambda *_a, **_k: None)
    fake_db = SimpleNamespace(commit=lambda: None)
    current_user = {"user_id": "admin-live", "tenant_id": "tenant-gdx", "role": "owner"}

    auth_router.admin_revoke_token(
        auth_router.RevokeTokenBody(jti="rev-jti-live", expires_at=expires_at),
        request,
        current_user,
        fake_db,
    )

    written_denylist = request.app.state.denylist
    assert written_denylist.contains("rev-jti-live") is True

    captured: dict[str, Any] = {}

    def denylist_aware_validate(token: str, **kwargs: Any) -> Principal:
        captured["denylist"] = kwargs.get("denylist")
        dl = kwargs.get("denylist")
        if dl is not None and dl.contains("rev-jti-live"):
            raise TokenRevoked(
                "token jti 'rev-jti-live' is on the revocation denylist"
            )
        raise InvalidSignature("not authentik-shaped")

    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        denylist_aware_validate,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            auth_router.get_current_user(request=request, token="garbage.not.jwt"),
        )

    # The denylist passed to the validator IS the one the revoke endpoint
    # wrote to — same FastAPI app, same ``app.state.denylist`` instance.
    assert captured["denylist"] is written_denylist
    # 401 body MUST stay the generic Slice F text — Slice H does not
    # introduce a distinct revoked-token response contract.
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired access token"
    assert "denylist" not in exc_info.value.detail
    assert "rev-jti-live" not in exc_info.value.detail


def test_separate_apps_have_separate_denylists():
    # Regression guard for the isolation property: two independent FastAPI
    # apps (two fake requests with distinct ``app.state``) must NOT share a
    # denylist. A module-level singleton regression would make ``dl_a is
    # dl_b`` true — Slice H requires it be false.
    request_a = _fake_request()
    request_b = _fake_request()

    dl_a = auth_router._get_app_denylist(request_a)
    dl_b = auth_router._get_app_denylist(request_b)

    assert dl_a is not dl_b
    # Revoking on A must not be visible on B.
    dl_a.add("only-on-a", datetime.now(UTC) + timedelta(seconds=900))
    assert dl_a.contains("only-on-a") is True
    assert dl_b.contains("only-on-a") is False


# ---------------------------------------------------------------------------
# 7. SS-7 Slice J — Redis-backed cross-worker denylist seam
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Tiny stand-in for the ``redis.Redis`` surface the Slice J adapter uses.

    Only ``setex`` and ``get`` with ``decode_responses=True`` semantics.
    Shared between two fake FastAPI apps to simulate two workers pointing
    at the same Redis. Hermetic — no network, no real Redis server, no
    dependency on the ``redis`` package's exception hierarchy.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def setex(self, key: str, seconds: int, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)


def test_get_app_denylist_attaches_redis_client_from_helper(monkeypatch):
    # Wiring contract for Slice J: the lazy-create branch in
    # ``_get_app_denylist`` MUST pull the Redis client from
    # ``_denylist_redis_client`` and attach it to the new Denylist so the
    # fan-out writes land somewhere. Without this wiring the adapter is
    # dead code at runtime.
    fake = _FakeRedis()
    monkeypatch.setattr(auth_router, "_denylist_redis_client", lambda: fake)

    request = _fake_request()  # no pre-seeded denylist → exercise lazy-create
    assert not hasattr(request.app.state, "denylist")

    dl = auth_router._get_app_denylist(request)

    assert dl.redis_client is fake
    # Identity preserved by subsequent calls (Slice H contract intact).
    assert auth_router._get_app_denylist(request) is dl


def test_get_app_denylist_uses_none_redis_when_helper_returns_none(monkeypatch):
    # When ``REDIS_URL`` is unset the helper returns ``None`` and the
    # attached Denylist falls back to Slice H local-only behavior. This
    # is the test-environment default and must keep working forever.
    monkeypatch.setattr(auth_router, "_denylist_redis_client", lambda: None)

    request = _fake_request()
    dl = auth_router._get_app_denylist(request)

    assert dl.redis_client is None


def test_revoke_on_one_app_visible_on_second_app_via_shared_redis(monkeypatch):
    # End-to-end Slice J property: two FastAPI apps share a Redis. A
    # revoke landing on app A's ``admin_revoke_token`` must be observed
    # by app B's lazy-created denylist the very next time B's auth path
    # resolves the denylist. No module globals, no explicit sync — just
    # the Redis seam doing its job.
    shared = _FakeRedis()
    monkeypatch.setattr(auth_router, "_denylist_redis_client", lambda: shared)
    monkeypatch.setattr(auth_router, "log_audit_event_sync", lambda *_a, **_k: None)

    request_a = _fake_request()
    request_b = _fake_request()
    expires_at = datetime.now(UTC) + timedelta(seconds=900)
    current_user = {"user_id": "admin-x", "tenant_id": "tenant-gdx", "role": "admin"}
    fake_db = SimpleNamespace(commit=lambda: None)

    auth_router.admin_revoke_token(
        auth_router.RevokeTokenBody(jti="rev-jti-cross", expires_at=expires_at),
        request_a,
        current_user,
        fake_db,
    )

    dl_a = request_a.app.state.denylist
    dl_b = auth_router._get_app_denylist(request_b)

    # The two denylists are separate instances (Slice H isolation) but
    # share a Redis (Slice J fan-out). App A saw the revoke locally; app
    # B sees it only because the Redis seam propagated the write.
    assert dl_a is not dl_b
    assert dl_b.contains("rev-jti-cross") is True


def test_slice_j_fails_open_when_redis_read_fails(monkeypatch):
    # Fail-open read contract at the router boundary: when the attached
    # Redis client raises on GET the second app's denylist reports miss
    # rather than propagating the exception up through ``contains`` and
    # turning a valid bearer token into a 500.
    class BrokenRedis:
        def setex(self, *_a, **_kw) -> None:
            return None

        def get(self, *_a, **_kw) -> str | None:
            raise RuntimeError("redis outage")

    monkeypatch.setattr(auth_router, "_denylist_redis_client", lambda: BrokenRedis())

    request = _fake_request()
    dl = auth_router._get_app_denylist(request)

    # Must not raise; must return False (fail-open miss).
    assert dl.contains("jti-any") is False


def test_slice_j_fails_open_when_redis_write_fails(monkeypatch):
    # Fail-open write contract at the router boundary: the revoke endpoint
    # must still return {"status": "ok"} and the local denylist must
    # still show the jti even when Redis SETEX blows up. Matches the
    # Slice I audit-failure fail-open pattern — a fan-out outage cannot
    # convert a successful security control into a 500.
    class WriteBrokenRedis:
        def setex(self, *_a, **_kw) -> None:
            raise RuntimeError("redis write outage")

        def get(self, *_a, **_kw) -> str | None:
            return None

    monkeypatch.setattr(
        auth_router, "_denylist_redis_client", lambda: WriteBrokenRedis()
    )
    monkeypatch.setattr(auth_router, "log_audit_event_sync", lambda *_a, **_k: None)

    request = _fake_request()
    expires_at = datetime.now(UTC) + timedelta(seconds=900)
    body = auth_router.RevokeTokenBody(jti="rev-jti-failwrite", expires_at=expires_at)
    current_user = {"user_id": "admin-y", "tenant_id": "tenant-gdx", "role": "admin"}
    fake_db = SimpleNamespace(commit=lambda: None)

    result = auth_router.admin_revoke_token(body, request, current_user, fake_db)

    assert result == {"status": "ok"}
    assert request.app.state.denylist.contains("rev-jti-failwrite") is True


# ---------------------------------------------------------------------------
# 8. SS-7 Slice K — ``DENYLIST_BACKEND_MODE`` explicit operator controls
#
# These tests pin the mode-parsing surface of
# :func:`gdx_dispatch.routers.auth._denylist_redis_client`. Every case monkeypatches
# ``REDIS_URL`` and ``DENYLIST_BACKEND_MODE`` via ``setenv`` / ``delenv``
# and replaces ``from_url`` at the router seam so no real Redis server is
# ever touched. The mode matrix under test:
#
# * ``memory`` — explicit local-only; ignores ``REDIS_URL``.
# * ``redis``  — explicit Redis; fail-open to local on missing/bad URL.
# * unset / blank — preserve Slice J default (Redis iff ``REDIS_URL`` set).
# * any other value — warn + degrade to the unset default.
#
# Identity contract: mode parsing lives at the router seam only; the
# :class:`Denylist` core class must remain adapter-agnostic (pinned by a
# companion test in ``gdx_dispatch/tests/test_denylist.py``).
# ---------------------------------------------------------------------------


class _SliceKSentinelClient:
    """Marker object returned by the Slice K stub ``from_url`` factory.

    Using a dedicated class (not ``object()``) so assertion failures point
    at a meaningful repr when the wiring regresses. Distinct from the
    Slice J ``_FakeRedis`` class: this one is never expected to receive
    ``setex`` / ``get`` calls — it is only asserted against by identity.
    """


def _slice_k_stub_from_url(url: str, decode_responses: bool = True) -> _SliceKSentinelClient:
    """Stub replacement for ``redis.from_url`` used by Slice K tests.

    Mirrors the ``decode_responses=True`` keyword the real call uses so a
    regression that drops the flag would land as a signature mismatch
    here rather than as a bytes/str bug at runtime.
    """
    return _SliceKSentinelClient()


def test_slice_k_memory_mode_ignores_redis_url(monkeypatch):
    # Contract: ``memory`` is an explicit operator opt-out. Even when
    # ``REDIS_URL`` is configured the factory must NOT be called — the
    # helper short-circuits to ``None`` so the attached Denylist falls
    # back to Slice H / Slice C local-only behavior.
    from_url_calls: list[str] = []

    def tracking_from_url(url: str, decode_responses: bool = True) -> _SliceKSentinelClient:
        from_url_calls.append(url)
        return _SliceKSentinelClient()

    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", "memory")
    monkeypatch.setattr(auth_router, "from_url", tracking_from_url)

    assert auth_router._denylist_redis_client() is None
    # The factory was never invoked — not just that the result was None.
    # A regression that drops the short-circuit and relies on fall-through
    # would still return None in isolation but would call the factory.
    assert from_url_calls == []


def test_slice_k_memory_mode_case_insensitive_with_whitespace(monkeypatch):
    # Contract: mode value is parsed case-insensitively with surrounding
    # whitespace stripped — ``  MEMORY  `` resolves to ``memory``. Makes
    # the env var robust against operator copy-paste hazards (shell
    # heredoc trailing newline, YAML indent leak).
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", "  MEMORY\t")
    # Poison the factory so a missed short-circuit fails loudly.
    monkeypatch.setattr(
        auth_router,
        "from_url",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("from_url called despite memory mode")
        ),
    )

    assert auth_router._denylist_redis_client() is None


def test_slice_k_redis_mode_builds_client_from_redis_url(monkeypatch):
    # Contract: ``redis`` mode attaches a Redis client when ``REDIS_URL``
    # is configured and the factory succeeds. The stub returns a
    # ``_SliceKSentinelClient`` so identity proves the return value is
    # exactly what the factory produced.
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", "redis")
    monkeypatch.setattr(auth_router, "from_url", _slice_k_stub_from_url)

    client = auth_router._denylist_redis_client()

    assert isinstance(client, _SliceKSentinelClient)


def test_slice_k_redis_mode_case_insensitive_with_whitespace(monkeypatch):
    # Contract: same trim+lower logic as memory mode. ``  Redis  `` →
    # ``redis``. Regression guard for a refactor that drops either the
    # strip or the lower.
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", "  Redis  ")
    monkeypatch.setattr(auth_router, "from_url", _slice_k_stub_from_url)

    assert isinstance(auth_router._denylist_redis_client(), _SliceKSentinelClient)


def test_slice_k_redis_mode_fails_open_when_redis_url_missing(
    monkeypatch, caplog,
):
    # Contract: ``redis`` mode without ``REDIS_URL`` degrades to local-only
    # with a warning — never raises into the auth path. Mirrors the
    # Slice J fail-open pattern: a misconfigured deployment cannot convert
    # the revoke endpoint into a 500.
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", "redis")

    # Poison the factory so a regression that called it anyway would fail
    # loudly instead of silently succeeding on an unrelated fallback.
    def poisoned(*_a, **_kw):
        raise AssertionError("from_url must not be called when REDIS_URL is unset")

    monkeypatch.setattr(auth_router, "from_url", poisoned)

    with caplog.at_level("WARNING", logger=auth_router.__name__):
        result = auth_router._denylist_redis_client()

    assert result is None
    assert any(
        "denylist_backend_mode_redis_missing_redis_url" in rec.getMessage()
        for rec in caplog.records
    ), f"expected missing-URL fail-open warning, got: {[r.getMessage() for r in caplog.records]}"


def test_slice_k_redis_mode_fails_open_when_from_url_raises(monkeypatch, caplog):
    # Contract: a bad REDIS_URL (unparseable scheme, TLS config error)
    # must degrade to local-only — NOT crash the revoke endpoint at call
    # time. Preserves the Slice J ``log.exception`` path verbatim.
    def broken_from_url(url: str, decode_responses: bool = True) -> None:
        raise ValueError(f"bad url: {url}")

    monkeypatch.setenv("REDIS_URL", "redis://bad::scheme")
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", "redis")
    monkeypatch.setattr(auth_router, "from_url", broken_from_url)

    with caplog.at_level("ERROR", logger=auth_router.__name__):
        result = auth_router._denylist_redis_client()

    assert result is None
    assert any(
        "denylist_redis_client_build_failed" in rec.getMessage()
        for rec in caplog.records
    ), f"expected build-failed log, got: {[r.getMessage() for r in caplog.records]}"


def test_slice_k_invalid_mode_degrades_to_default_with_warning(
    monkeypatch, caplog,
):
    # Contract: an unknown mode value warns and degrades to the unset
    # default — which, when ``REDIS_URL`` is set, still attempts Redis.
    # This keeps a typo (``DENYLIST_BACKEND_MODE=rediss``) from silently
    # disabling cross-worker fan-out; the operator sees a warning and
    # Redis still tries to connect via the Slice J path.
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", "rediss")
    monkeypatch.setattr(auth_router, "from_url", _slice_k_stub_from_url)

    with caplog.at_level("WARNING", logger=auth_router.__name__):
        result = auth_router._denylist_redis_client()

    # Degraded path still built a client — invalid mode is NOT "memory".
    assert isinstance(result, _SliceKSentinelClient)
    assert any(
        "denylist_backend_mode_invalid" in rec.getMessage()
        for rec in caplog.records
    ), f"expected invalid-mode warning, got: {[r.getMessage() for r in caplog.records]}"


def test_slice_k_invalid_mode_without_redis_url_returns_none(monkeypatch):
    # Regression: invalid mode + no REDIS_URL matches the unset+no-URL
    # branch exactly — local-only, no raise. No regression of the Slice J
    # test-environment default.
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", "totally-bogus")

    assert auth_router._denylist_redis_client() is None


def test_slice_k_unset_mode_preserves_slice_j_default_when_redis_url_set(
    monkeypatch,
):
    # Contract: env UNSET (truly absent, not blank) must preserve Slice J
    # default — build a client iff REDIS_URL is set. This is the
    # production path today; a regression would silently disable
    # cross-worker fan-out for deployments that never set the mode.
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("DENYLIST_BACKEND_MODE", raising=False)
    monkeypatch.setattr(auth_router, "from_url", _slice_k_stub_from_url)

    assert isinstance(auth_router._denylist_redis_client(), _SliceKSentinelClient)


def test_slice_k_blank_mode_preserves_slice_j_default(monkeypatch):
    # Contract: blank-after-strip value is treated exactly like unset
    # — Slice J default. Pins the trim+lower branch.
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", "   ")
    monkeypatch.setattr(auth_router, "from_url", _slice_k_stub_from_url)

    assert isinstance(auth_router._denylist_redis_client(), _SliceKSentinelClient)


def test_slice_k_unset_mode_without_redis_url_returns_none(monkeypatch):
    # Regression: no env, no REDIS_URL → None. This is the unit-test
    # environment default and must keep working unchanged.
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("DENYLIST_BACKEND_MODE", raising=False)

    assert auth_router._denylist_redis_client() is None


def test_slice_f_fallback_behavior_survives_slice_h_wiring(
    monkeypatch, isolated_denylist,
):
    # Regression guard: moving the denylist onto ``app.state`` and adding
    # the ``request`` parameter must not disturb the HS256 legacy fallback
    # Slice F restored. A legacy token minted by ``_issue`` still
    # authenticates even when the core validator is forced to reject it
    # (because the token shape doesn't match Authentik's iss/aud).
    denylist, request = isolated_denylist

    def force_fallthrough(token: str, **kwargs: Any) -> Principal:
        # Prove the app-scoped denylist is still routed through on
        # fallthroughs — future refactors must not drop the kwarg silently.
        assert kwargs.get("denylist") is denylist
        raise InvalidSignature("not authentik-shaped — use legacy")

    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        force_fallthrough,
    )

    # Mint the legacy token for the same tenant the fixture host advertises
    # (Slice 6 cross-check rejects token tenant ≠ host tenant; this test
    # exercises HS256 fallback, not the cross-check, so the tenants align).
    host_tid = request.state.tenant["id"]
    token = _legacy_token(sub="legacy-user", tenant_id=host_tid, role="admin")

    result = asyncio.run(auth_router.get_current_user(request=request, token=token))

    assert result["user_id"] == "legacy-user"
    assert result["tenant_id"] == host_tid
    assert result["role"] == "admin"
    # Impersonation markers added cc2-s46; None on non-impersonation tokens.
    assert result["imp_actor_id"] is None
    assert result["imp_purpose"] is None


# ---------------------------------------------------------------------------
# 9. SS-7 Slice N — docs ↔ code log-event-name parity guard
#
# The denylist backend emits four operator-facing log events across two
# source files (``gdx_dispatch/routers/auth/core.py`` for the helper-layer events,
# ``gdx_dispatch/app.py`` for the ``/health`` probe-layer event). The runbook
# ``docs/ops/denylist_backend_mode.md`` names each event verbatim so
# on-call engineers can grep logs for them. If code renames an event
# without updating the runbook (or vice versa), alerts silently stop
# matching — this test is the cheapest regression guard against that
# drift. The check is hermetic: it parses source text, it does not
# execute any runtime path, spin up a FastAPI client, or touch Redis.
# ---------------------------------------------------------------------------


def test_slice_n_denylist_event_names_parity_between_docs_and_code():
    # Event-name set under contract. Auth-helper events live in
    # ``_denylist_redis_client``; the probe event lives in the ``/health``
    # handler. Keeping the sets split by file makes a rename land as a
    # precise failure ("auth.py expected X, missing") rather than a
    # diffuse set-difference.
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    docs_path = repo_root / "docs" / "ops" / "denylist_backend_mode.md"
    auth_path = repo_root / "gdx_dispatch" / "routers" / "auth" / "core.py"
    app_path = repo_root / "gdx_dispatch" / "app.py"

    auth_events = {
        "denylist_backend_mode_redis_missing_redis_url",
        "denylist_backend_mode_invalid",
        "denylist_redis_client_build_failed",
    }
    app_events = {"denylist_backend_probe_failed"}
    required = auth_events | app_events

    docs_text = docs_path.read_text(encoding="utf-8")
    auth_text = auth_path.read_text(encoding="utf-8")
    app_text = app_path.read_text(encoding="utf-8")

    # Forward parity: every required event must be documented in the
    # runbook AND emitted from its expected source file. Case-sensitive
    # substring match — a rename to a different casing would land here.
    for event in sorted(required):
        assert event in docs_text, (
            f"event {event!r} missing from runbook {docs_path} — "
            "docs/code drift; update the runbook or rename the emitter"
        )
    for event in sorted(auth_events):
        assert event in auth_text, (
            f"event {event!r} missing from {auth_path} — "
            "runbook mentions it but no emitter is present"
        )
    for event in sorted(app_events):
        assert event in app_text, (
            f"event {event!r} missing from {app_path} — "
            "runbook mentions it but no emitter is present"
        )

    # Reverse parity: scan for any ``"denylist_..."`` string literal
    # passed as the first positional argument to ``log.warning(...)``,
    # ``log.exception(...)``, or ``logging.getLogger(...).warning|exception(...)``
    # in these two files. Every match MUST be in the required set — a
    # new emitter landing without a runbook update lands here as
    # "extra events not in the runbook".
    #
    # The pattern is deliberately narrow so it does not flag the
    # ``"denylist_backend"`` dict key returned in the ``/health`` body
    # (``app.py`` line ~1221), which is not a log event.
    log_call_re = re.compile(
        r'(?:log|logging\.getLogger\([^)]*\))'
        r'\.(?:warning|exception)\(\s*"(denylist_[a-z0-9_]+)',
    )
    auth_found = set(log_call_re.findall(auth_text))
    app_found = set(log_call_re.findall(app_text))

    auth_extras = auth_found - auth_events
    assert not auth_extras, (
        f"{auth_path} emits denylist log events not in the runbook: "
        f"{sorted(auth_extras)} — document them in "
        f"{docs_path} before landing"
    )
    app_extras = app_found - app_events
    assert not app_extras, (
        f"{app_path} emits denylist log events not in the runbook: "
        f"{sorted(app_extras)} — document them in "
        f"{docs_path} before landing"
    )

    # Belt-and-suspenders: confirm the regex-based scan actually saw the
    # required emitters. A regex regression that matched zero events
    # would make the reverse-parity check vacuously green; asserting
    # equality (not subset) here catches that.
    assert auth_found == auth_events, (
        f"auth.py event-literal scan drifted: expected {sorted(auth_events)}, "
        f"got {sorted(auth_found)}"
    )
    assert app_found == app_events, (
        f"app.py event-literal scan drifted: expected {sorted(app_events)}, "
        f"got {sorted(app_found)}"
    )


# ---------------------------------------------------------------------------
# 10. SS-7 Slice P — runbook probe-block wording + event-token guard
#
# Slice N pins that every required denylist event name exists SOMEWHERE
# in the runbook. Slice P narrows the guard for the ``/health`` probe
# event so a careless edit that moves the token into an unrelated
# section (mode matrix, helper-event list, Related files) still fails
# here even though the Slice N substring check would pass.
#
# The runbook names two probe-focused blocks that must continue to pin
# the token ``denylist_backend_probe_failed``:
#
#   A. The "Fail-open guarantee" paragraph that narrates the /health
#      probe-failure path (starts with "``GET /health`` extends the same
#      fail-open contract to its visibility probe.").
#   B. The "Log events to alert on" probe sub-section (starts with
#      "Operator-facing log event emitted by ``gdx_dispatch/app.py`` at the
#      ``/health`` probe layer"), containing the bullet for the event.
#
# Each block is extracted by a stable anchor phrase and closed at the
# next Markdown heading so a future reflow or rewording of surrounding
# sections does not shift the assertion surface. The checks are
# hermetic: source-text only, no runtime path.
# ---------------------------------------------------------------------------


def test_slice_p_denylist_probe_event_in_probe_focused_docs_blocks():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    docs_path = repo_root / "docs" / "ops" / "denylist_backend_mode.md"
    docs_text = docs_path.read_text(encoding="utf-8")

    event_token = "denylist_backend_probe_failed"

    # --- Block A: "Fail-open guarantee" probe paragraph ---------------------
    # The paragraph's opening sentence is a stable anchor — it has been
    # present since Slice M and is referenced by name in the Slice M
    # commit. Close the block at the next Markdown "## " section header
    # so a reflow of Observability / Alerting cannot satisfy this check.
    anchor_a = "`GET /health` extends the same fail-open contract"
    start_a = docs_text.find(anchor_a)
    assert start_a != -1, (
        f"runbook {docs_path} missing fail-open probe paragraph anchor "
        f"{anchor_a!r} — the probe-focused wording drifted"
    )
    end_a_match = docs_text.find("\n## ", start_a + 1)
    block_a = docs_text[start_a:end_a_match if end_a_match != -1 else len(docs_text)]
    assert event_token in block_a, (
        f"event {event_token!r} missing from the 'Fail-open guarantee' "
        f"probe paragraph in {docs_path} — the token may have drifted "
        f"into an unrelated section. Block scanned was:\n{block_a}"
    )
    # Pin the surrounding wording so a rewrite that drops "fail-open"
    # or the 200 semantics fails loudly; these phrases are what an
    # operator greps for when auditing /health behavior.
    for phrase in ("fail-open", "`200 OK`", "probe"):
        assert phrase in block_a, (
            f"'Fail-open guarantee' probe paragraph in {docs_path} no "
            f"longer contains required phrase {phrase!r}; block was:\n{block_a}"
        )

    # --- Block B: "Log events to alert on" probe sub-block -----------------
    # The sub-block anchor is the lead-in sentence that distinguishes the
    # probe event from the three helper events above it. Close the block
    # at the next Markdown "### " sub-section header so a future addition
    # of alerting guidance or Related files does not dilute the check.
    anchor_b = "Operator-facing log event emitted by `gdx_dispatch/app.py` at the `/health`"
    start_b = docs_text.find(anchor_b)
    assert start_b != -1, (
        f"runbook {docs_path} missing 'Log events' probe sub-block anchor "
        f"{anchor_b!r} — the probe-event bullet may have been relocated"
    )
    end_b_match = docs_text.find("\n### ", start_b + 1)
    if end_b_match == -1:
        end_b_match = docs_text.find("\n## ", start_b + 1)
    block_b = docs_text[start_b:end_b_match if end_b_match != -1 else len(docs_text)]
    # The token must appear as a Markdown bullet backticked for grep
    # friendliness. A bullet is the on-call-facing contract — a bare
    # mention in prose is not sufficient to qualify as alert-grep text.
    assert f"- `{event_token}`" in block_b, (
        f"event bullet '- `{event_token}`' missing from the 'Log events "
        f"to alert on' probe sub-block in {docs_path}; sub-block was:\n{block_b}"
    )
    # Pin the severity + emitter wording so a drop to WARNING or a
    # reassignment of the emitter file misleads operators silently.
    for phrase in ("ERROR", "log.exception", "fail-open"):
        assert phrase in block_b, (
            f"'Log events' probe sub-block in {docs_path} no longer "
            f"contains required phrase {phrase!r}; sub-block was:\n{block_b}"
        )

    # Belt-and-suspenders: the two probe blocks must be distinct slices
    # of the runbook — if the anchors collapsed onto the same start
    # offset (e.g. a reflow merged them), future assertions would
    # silently double-cover the same text and miss a real regression.
    assert start_a != start_b, (
        "probe-paragraph anchor and log-events anchor resolved to the "
        "same offset — runbook structure collapsed; re-audit section "
        "layout in docs/ops/denylist_backend_mode.md"
    )


# ---------------------------------------------------------------------------
# 11. SS-7 Slice Q — runbook helper-event block wording + event-token guard
#
# Slice N pins that every required denylist event name exists SOMEWHERE
# in the runbook. Slice P narrows the guard for the ``/health`` probe
# event. Slice Q closes the symmetry by narrowing the guard for the
# three helper-layer events emitted by ``_denylist_redis_client()`` so a
# careless edit that moves any of those tokens into an unrelated section
# (mode matrix, probe block, alerting guidance, Related files) still
# fails here even though the Slice N substring check would pass.
#
# Spec-vs-docs reconciliation (supervisor resolution Q-2026-04-17T08:20:00Z,
# option A): the original Slice Q spec named an anchor sentence
# ``When the helper in `gdx_dispatch/routers/auth/core.py` falls back to in-memory
# mode...`` and a wording contract that included ``log.warning`` and
# ``in-memory denylist``. Verified at HEAD: that sentence does not exist
# in the runbook, and neither do the two phrases. The supervisor
# resolved by substituting the real helper-events anchor (the
# ``Operator-facing log events emitted by `_denylist_redis_client()`:``
# sentence, the exact structural mirror of Slice P's probe anchor) and
# reducing the wording contract to phrases the runbook actually
# guarantees today (``WARNING`` and ``fail-open``). Adding the dropped
# phrases to the runbook is deferred to a separate docs-only slice if
# operators want them in the contract.
#
# The block is extracted by the helper anchor and closed at the next
# Markdown ``### `` (or ``## ``) heading so a future reflow of
# "Health endpoint visibility" or "Alerting guidance" does not silently
# shift the assertion surface. The check is hermetic: source-text only,
# no runtime path, no FastAPI ``TestClient``, no import of ``gdx_dispatch.app``,
# ``gdx_dispatch.routers.auth``, or ``gdx_dispatch.core.denylist``.
# ---------------------------------------------------------------------------


def test_slice_q_helper_events_live_in_helper_focused_docs_block():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    docs_path = repo_root / "docs" / "ops" / "denylist_backend_mode.md"
    docs_text = docs_path.read_text(encoding="utf-8")

    # Helper-events anchor: the sentence that introduces the three
    # ``_denylist_redis_client()`` event bullets. Exact structural mirror
    # of Slice P's probe anchor (``Operator-facing log event emitted by
    # `gdx_dispatch/app.py` at the `/health```), differing in singular/plural and
    # emitter so a ``find`` cannot accidentally match the probe anchor.
    helper_anchor = (
        "Operator-facing log events emitted by `_denylist_redis_client()`:"
    )
    start_helper = docs_text.find(helper_anchor)
    assert start_helper != -1, (
        f"runbook {docs_path} missing helper-events anchor "
        f"{helper_anchor!r} — helper-focused wording drifted; if the "
        f"anchor was reworded, file a fresh Q rather than guessing a "
        f"new substitution"
    )

    # Slice P probe anchor — needed for the structural-distinctness
    # guard below so a runbook reflow that collapses the helper and
    # probe sub-blocks onto the same offset fails here, not silently.
    probe_anchor = (
        "Operator-facing log event emitted by `gdx_dispatch/app.py` at the `/health`"
    )
    start_probe = docs_text.find(probe_anchor)
    assert start_probe != -1, (
        f"runbook {docs_path} missing Slice P probe-events anchor "
        f"{probe_anchor!r} — cannot evaluate the helper-vs-probe "
        f"structural guard; re-audit section layout"
    )

    # Close the helper slice at the next Markdown ``### `` sub-section
    # heading (or ``## `` section heading if no sub-section follows) so
    # a future reflow of unrelated sub-sections cannot satisfy this
    # check via stray text. Note this slice may legitimately span the
    # adjacent probe sub-block — the assertions below only require the
    # three helper-event bullets to be present, and Slice P pins the
    # probe-block tokens independently.
    end_helper = docs_text.find("\n### ", start_helper + 1)
    if end_helper == -1:
        end_helper = docs_text.find("\n## ", start_helper + 1)
    block_helper = docs_text[
        start_helper : end_helper if end_helper != -1 else len(docs_text)
    ]

    # Each helper event must appear as a Markdown bullet backticked for
    # grep friendliness. A bare prose mention is not the on-call
    # contract — the alert-grep keyword is the bulleted ``- `name``` form.
    for event in (
        "denylist_backend_mode_redis_missing_redis_url",
        "denylist_backend_mode_invalid",
        "denylist_redis_client_build_failed",
    ):
        assert f"- `{event}`" in block_helper, (
            f"helper-event bullet '- `{event}`' missing from helper-"
            f"focused sub-block in {docs_path}; sub-block was:\n"
            f"{block_helper}"
        )

    # Wording contract — phrases the runbook guarantees today.
    # ``WARNING`` covers the severity label of two of three helper
    # events; ``fail-open`` is the operator-grep keyword tying the
    # helper block to the "Fail-open guarantee" section. Slice S
    # re-widened the contract to also pin ``log.warning`` (the emitter
    # method for the two WARNING-level helper events, parallel to
    # ``log.exception`` on the ERROR-level bullet) and ``in-memory
    # denylist`` (the fallback-state phrase operators grep for when
    # auditing cross-worker revocation gaps).
    for phrase in ("WARNING", "log.warning", "fail-open", "in-memory denylist"):
        assert phrase in block_helper, (
            f"helper-events sub-block in {docs_path} no longer contains "
            f"required phrase {phrase!r}; sub-block was:\n{block_helper}"
        )

    # Structural guard: helper and probe anchors must resolve to
    # distinct offsets so a block-collapse/reflow in the runbook cannot
    # double-cover the same text and silently pass both Slice P and
    # Slice Q assertions on identical bytes.
    assert start_helper != start_probe, (
        "helper-events anchor and probe-events anchor resolved to the "
        "same offset — runbook structure collapsed; re-audit section "
        "layout in docs/ops/denylist_backend_mode.md"
    )


# ---------------------------------------------------------------------------
# 11. SS-8 Slice E — execution_context() adoption at the auth dependency
#
# This slice is the first production consumer of
# :func:`gdx_dispatch.core.contexts.execution_context`. :func:`get_current_user` now
# wraps the primary-path ``validate_principal`` call with a scoped override
# that pins ``installation_id=None`` / ``act_chain=()`` for this request.
#
# The contract this test pins:
#
# A. **Isolation** — pre-seeded outer contextvar values
#    (``current_installation_id`` / ``current_act_chain``) MUST NOT leak
#    into the core validator call. The validator sees the helper's
#    explicit defaults regardless of what the outer scope had set.
# B. **Restoration** — after :func:`get_current_user` returns, the outer
#    contextvar values are restored byte-for-byte. A future slice that
#    quietly drops the ``reset(token)`` half of the lifecycle (or swaps
#    ``with execution_context(...)`` for bare ``.set(...)`` calls) would
#    regress this assertion.
#
# Hermetic: no FastAPI TestClient, no network, no DB. A fake
# ``validate_principal`` captures the contextvar state observed inside
# the wrapped call; a second probe captures the state observed inside
# the same asyncio context after ``get_current_user`` returns. Outer
# contextvar pre-seeding happens inside the same ``asyncio.run`` scope
# so the test cannot accidentally pollute sibling tests.
# ---------------------------------------------------------------------------


def test_slice_e_execution_context_overrides_and_restores_at_auth_boundary(
    monkeypatch,
):
    # Lazy import kept local to the test — keeps the module-level import
    # surface stable for the rest of this file.
    from gdx_dispatch.core.contexts import (
        current_act_chain,
        current_installation_id,
        execution_context,
    )

    outer_installation_id = "installation-outer-slice-e"
    outer_act_chain: tuple[str, ...] = ("svc-upstream", "svc-midstream")

    seen_inside_validator: dict[str, Any] = {}
    seen_after_return: dict[str, Any] = {}

    principal = _principal(
        subject="user-slice-e",
        tenant_id="tenant-gdx",
        role="user",
    )

    def fake_validate_principal(token: str, **kwargs: Any) -> Principal:
        # Capture the contextvar state the validator observes. If
        # ``execution_context`` is wired correctly, these are the helper's
        # defaults; if a regression drops the wrapper, these would be the
        # pre-seeded outer values instead.
        seen_inside_validator["installation_id"] = current_installation_id.get()
        seen_inside_validator["act_chain"] = current_act_chain.get()
        return principal

    monkeypatch.setattr(
        "gdx_dispatch.core.auth.validate_principal",
        fake_validate_principal,
    )

    request = _fake_request(denylist=Denylist())

    async def exercise() -> None:
        # Open the outer execution context INSIDE the same asyncio scope
        # that ``get_current_user`` will use, so this test cannot bleed
        # into sibling tests — ``asyncio.run`` copies the current context
        # on entry and discards the copy on exit, and the helper's
        # ``finally`` unwind handles in-scope restoration.
        with execution_context(
            installation_id=outer_installation_id,
            act_chain=outer_act_chain,
        ):
            result = await auth_router.get_current_user(
                request=request,
                token="opaque-token-slice-e",
            )

            # Capture outer-state visibility immediately after the
            # dependency returns but still inside the outer
            # ``execution_context`` scope. If ``execution_context.__exit__``
            # inside ``get_current_user`` skipped either half of the LIFO
            # unwind, these assertions fail.
            seen_after_return["installation_id"] = current_installation_id.get()
            seen_after_return["act_chain"] = current_act_chain.get()
            seen_after_return["result"] = result

    asyncio.run(exercise())

    # Contract A — isolation: the validator MUST see the helper's
    # defaults, not the outer scope's pre-seeded values.
    assert seen_inside_validator["installation_id"] is None, (
        f"Slice E isolation broken: outer installation_id "
        f"{outer_installation_id!r} leaked into validate_principal"
    )
    assert seen_inside_validator["act_chain"] == (), (
        f"Slice E isolation broken: outer act_chain "
        f"{outer_act_chain!r} leaked into validate_principal"
    )

    # Contract B — restoration: after get_current_user returns, the outer
    # values are restored byte-for-byte.
    assert seen_after_return["installation_id"] == outer_installation_id, (
        "Slice E restoration broken: outer installation_id not restored "
        "after get_current_user returned"
    )
    assert seen_after_return["act_chain"] == outer_act_chain, (
        "Slice E restoration broken: outer act_chain not restored "
        "after get_current_user returned"
    )

    # Regression anchor — the dict-shape return contract is untouched by
    # Slice E. If this drifts, the auth-dependency return type changed
    # and every downstream consumer breaks; catch it here. The
    # impersonation markers (cc2-s46) are part of the contract too.
    res = seen_after_return["result"]
    assert res["user_id"] == "user-slice-e"
    assert res["tenant_id"] == "tenant-gdx"
    assert res["role"] == "user"
    assert res["imp_actor_id"] is None
    assert res["imp_purpose"] is None


# ═══════════════════════════════════════════════════════════════════════════
# D-S119-refresh-denylist-gap regression tests
# ═══════════════════════════════════════════════════════════════════════════
#
# Pre-fix: /auth/refresh consulted ONLY `used_refresh_jtis` (family-replay
# detection). An admin revoke via /auth/admin/revoke wrote the jti to the
# SS-7 denylist but the refresh endpoint never looked at it — so the user
# whose access-token jti was "revoked" could just hit /auth/refresh and
# mint a fresh access token within ≤15 min. Auditor flagged this as the
# keystone of the D-S119 closure (2026-05-10).
#
# Fix: refresh decode → check SS-7 denylist on the refresh jti → if listed,
# 401 AND revoke the entire family (mirror the existing reuse-detection
# family-revoke logic). Order: denylist runs BEFORE replay-detection.
#
# Industry alignment: RFC 9700 — "If the token is a refresh token and the
# authorization server supports the revocation of access tokens, then the
# authorization server SHOULD also invalidate all access tokens based on
# the same authorization grant." flask-jwt-extended fires its blocklist
# on both access AND refresh tokens.


class _FakeRefreshRedis:
    """Stand-in for the module-level redis client used by /auth/refresh.

    Tracks `used_refresh_jtis` and `refresh_family:{sub}` sets in-process so
    tests can pre-seed family membership and inspect post-state. The
    `pipeline()` accumulates writes via the same object (last execute() is
    a no-op since each call mutates state immediately).
    """

    def __init__(self) -> None:
        self.used: set[str] = set()
        self.families: dict[str, set[str]] = {}
        self.redemptions: dict[str, dict[str, str]] = {}
        self.kv: dict[str, str] = {}

    # Standalone (non-pipeline) ops
    def sismember(self, key: str, value: str) -> bool:
        if key == "used_refresh_jtis":
            return value in self.used
        return False

    def smembers(self, key: str) -> set[str]:
        if key.startswith("refresh_family:"):
            sub = key.split(":", 1)[1]
            return set(self.families.get(sub, set()))
        return set()

    def sadd(self, key: str, *vals: str) -> int:
        if key == "used_refresh_jtis":
            self.used.update(vals)
        elif key.startswith("refresh_family:"):
            sub = key.split(":", 1)[1]
            self.families.setdefault(sub, set()).update(vals)
        return len(vals)

    def expire(self, key: str, ttl: int) -> bool:  # noqa: ARG002
        return True

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.redemptions.get(key, {}))

    def hset(self, key: str, mapping: dict[str, str] | None = None) -> int:
        if mapping:
            self.redemptions[key] = dict(mapping)
        return 1

    def delete(self, key: str) -> int:
        if key.startswith("refresh_family:"):
            sub = key.split(":", 1)[1]
            self.families.pop(sub, None)
            return 1
        return 0

    def set(  # noqa: ARG002 - ex unused; fake has no TTL clock
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None,
    ) -> bool | None:
        """redis-py SET semantics: with nx=True, return None (no-op) if the
        key already exists, else set and return True. Used by the
        replay-sink dedup guard in /auth/refresh."""
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    def pipeline(self) -> "_FakeRefreshRedis":
        return self

    def execute(self) -> list[Any]:
        return []


class _FakeRefreshDb:
    """Minimal db stub: log_audit_event_sync writes succeed silently
    (they're try/except-wrapped at the caller) and the user lookup returns
    a happy admin row when the denylist+replay paths flow through.
    """

    def __init__(self, user_id: str = "user-42") -> None:
        self.user_id = user_id
        self.commits = 0
        self.added = []
        self.audit_events: list[dict[str, Any]] = []

    def execute(self, _stmt: Any) -> Any:
        # Refresh handler calls .execute(...).scalar_one_or_none() to load the user.
        # Return a happy admin row by default — the denylist tests don't reach this branch.
        user_row = SimpleNamespace(
            id=self.user_id, role="admin", deleted_at=None, active=True,
        )
        return SimpleNamespace(scalar_one_or_none=lambda: user_row)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


def _mint_refresh_token(
    *, sub: str, jti: str, tenant_id: str = "t", role: str = "admin",
) -> str:
    """Mint a locally-signed refresh JWT shaped like prod's /auth/login._issue()."""
    claims = {
        "sub": sub,
        "tenant_id": tenant_id,
        "role": role,
        "jti": jti,
        "typ": "refresh",
        "exp": int((datetime.now(UTC) + timedelta(seconds=REFRESH_TTL_FOR_TEST)).timestamp()),
    }
    return pyjwt.encode(claims, auth_router.SIGN_KEY, algorithm=auth_router.ALG)


REFRESH_TTL_FOR_TEST = 30 * 24 * 60 * 60  # match REFRESH_TTL in the module


def _patch_auth_revoke_redis(monkeypatch, fake_redis):
    """Wire auth_revoke._redis_client to return our in-test fake so the
    refresh-handler's family-revoke (which delegates to revoke_user_sessions)
    can observe the same family state the test pre-seeded."""
    from gdx_dispatch.core import auth_revoke as _revoke
    monkeypatch.setattr(_revoke, "_redis_client", lambda: fake_redis)


def test_refresh_with_denylisted_jti_returns_401(monkeypatch):
    """D-S119-refresh-denylist-gap regression: a refresh token whose jti is
    on the SS-7 denylist MUST be rejected at /auth/refresh, even though it
    is well-formed and signature-valid. Pre-fix this returned 200 + a fresh
    access token.
    """
    sub = "u-refresh-deny"
    jti = "denylisted-refresh-jti"
    token = _mint_refresh_token(sub=sub, jti=jti)

    fake_redis = _FakeRefreshRedis()
    fake_redis.families[sub] = {jti}  # family contains just this one
    monkeypatch.setattr(auth_router, "redis", fake_redis)
    _patch_auth_revoke_redis(monkeypatch, fake_redis)

    denylist = Denylist()
    denylist.add(jti, datetime.now(UTC) + timedelta(hours=1))

    request = _fake_request(denylist=denylist, cookies={"refresh_token": token})
    with pytest.raises(HTTPException) as exc:
        auth_router.refresh(request=request, db=_FakeRefreshDb(user_id=sub))
    assert exc.value.status_code == 401
    # The "Token revoked" message distinguishes denylist hits from replay
    # detection ("session revoked") and from invalid-token ("Invalid or
    # expired refresh token"). Pin the specific shape.
    assert "revoked" in str(exc.value.detail).lower()


def test_refresh_with_denylisted_jti_revokes_family(monkeypatch):
    """RFC 9700 §refresh-token security: revocation must invalidate the entire
    token family, not just the listed jti. A sibling refresh token MUST NOT
    survive after the family is denylisted.
    """
    sub = "u-family-revoke"
    target_jti = "denylisted-jti"
    sibling_a = "sibling-jti-a"
    sibling_b = "sibling-jti-b"
    token = _mint_refresh_token(sub=sub, jti=target_jti)

    fake_redis = _FakeRefreshRedis()
    fake_redis.families[sub] = {target_jti, sibling_a, sibling_b}
    monkeypatch.setattr(auth_router, "redis", fake_redis)
    _patch_auth_revoke_redis(monkeypatch, fake_redis)

    denylist = Denylist()
    denylist.add(target_jti, datetime.now(UTC) + timedelta(hours=1))

    request = _fake_request(denylist=denylist, cookies={"refresh_token": token})
    with pytest.raises(HTTPException):
        auth_router.refresh(request=request, db=_FakeRefreshDb(user_id=sub))

    # Every sibling jti now in the used set — they can never refresh again.
    assert sibling_a in fake_redis.used, "Sibling A must be revoked with the family"
    assert sibling_b in fake_redis.used, "Sibling B must be revoked with the family"
    # And the family marker is gone — no new tokens can join the lineage.
    assert sub not in fake_redis.families or not fake_redis.families[sub]


def test_refresh_with_un_denylisted_jti_still_works(monkeypatch):
    """Negative control: a refresh whose jti is NOT denylisted continues to
    work normally. Pins that the new gate only rejects truly-revoked jtis.
    """
    sub = "u-happy-refresh"
    jti = "happy-refresh-jti"
    token = _mint_refresh_token(sub=sub, jti=jti)

    fake_redis = _FakeRefreshRedis()
    fake_redis.families[sub] = {jti}
    monkeypatch.setattr(auth_router, "redis", fake_redis)

    denylist = Denylist()  # empty — no jtis revoked

    request = _fake_request(denylist=denylist, cookies={"refresh_token": token})
    resp = auth_router.refresh(request=request, db=_FakeRefreshDb(user_id=sub))
    # Refresh succeeded — got a JSONResponse with new access token.
    import json as _json
    body = _json.loads(resp.body.decode())
    assert "access_token" in body
    assert body["token_type"] == "bearer"


def test_refresh_denylist_check_runs_before_replay_detection(monkeypatch):
    """Order pin: if a jti is BOTH on the denylist AND in used_refresh_jtis
    (already-used), the denylist's "Token revoked" path wins. The two
    detect different threat models (admin revoke vs reuse attack) and
    forensic accuracy matters — the audit trail must reflect the actual
    cause.

    Auditor flag (2026-05-10): the original assertion checked error-message
    text ("'replay' not in detail"), which is fragile to string-wording
    changes. Strengthen to pin the AUDIT ACTION — the durable forensic
    contract: denylist branch fires action="refresh_denied_token_revoked",
    replay branch fires action="refresh_replay_detected". The audit action
    is what SOC 2 auditors actually read, so it's the contract.
    """
    sub = "u-order"
    jti = "both-denylisted-and-used"
    token = _mint_refresh_token(sub=sub, jti=jti)

    fake_redis = _FakeRefreshRedis()
    fake_redis.families[sub] = {jti}
    fake_redis.used.add(jti)  # ALREADY MARKED USED
    monkeypatch.setattr(auth_router, "redis", fake_redis)
    _patch_auth_revoke_redis(monkeypatch, fake_redis)

    denylist = Denylist()
    denylist.add(jti, datetime.now(UTC) + timedelta(hours=1))  # ALSO DENYLISTED

    audit_actions: list[str] = []

    def capture_audit(db: Any, *args: Any, **kwargs: Any) -> None:
        audit_actions.append(kwargs.get("action", ""))

    monkeypatch.setattr(auth_router, "log_audit_event_sync", capture_audit)

    request = _fake_request(denylist=denylist, cookies={"refresh_token": token})
    with pytest.raises(HTTPException) as exc:
        auth_router.refresh(request=request, db=_FakeRefreshDb(user_id=sub))
    assert exc.value.status_code == 401

    # CONTRACT: the denylist branch must have fired its specific audit action.
    # The replay branch's action MUST NOT appear — it would mean the order
    # was reversed and the replay path captured the request before the
    # denylist check.
    assert "refresh_denied_token_revoked" in audit_actions, (
        f"denylist branch did not fire — audit_actions={audit_actions}"
    )
    assert "refresh_replay_detected" not in audit_actions, (
        f"replay branch fired before denylist — audit_actions={audit_actions}"
    )


def test_refresh_replay_sinks_server_error_only_once_per_jti(monkeypatch):
    """2026-05-14 incident regression. A client looping on a dead refresh
    cookie (logout() pre-fix didn't clear the HttpOnly cookie) re-presents
    the SAME jti every poll tick. Pre-fix the replay branch sank one
    server_errors row per hit — one stuck tab wrote 1,526 identical rows
    (6,717 total over 4 days) and buried the real signal in CC
    support/errors.

    Fix: dedup the sink per jti via Redis SET NX. Security is unchanged —
    the family revoke + 401 + audit still fire on EVERY hit; only the
    dashboard sink is collapsed to one row per jti.
    """
    sub = "u-replay-flood"
    jti = "looping-dead-jti"
    token = _mint_refresh_token(sub=sub, jti=jti)

    fake_redis = _FakeRefreshRedis()
    fake_redis.used.add(jti)              # already used → TRUE-replay branch
    fake_redis.families[sub] = {jti}
    monkeypatch.setattr(auth_router, "redis", fake_redis)
    _patch_auth_revoke_redis(monkeypatch, fake_redis)
    monkeypatch.setattr(auth_router, "log_audit_event_sync", lambda *_a, **_k: None)

    sink_calls: list[str] = []

    def fake_sink(*, request: Any, exc: BaseException, status_code: int,
                  request_id: str | None = None) -> None:  # noqa: ARG001
        sink_calls.append(str(exc))

    monkeypatch.setattr(
        "gdx_dispatch.modules.error_sink.record_server_error", fake_sink, raising=False,
    )

    # Five replays of the SAME jti — exactly what a stuck client does.
    for _ in range(5):
        request = _fake_request(cookies={"refresh_token": token})
        with pytest.raises(HTTPException) as exc:
            auth_router.refresh(request=request, db=_FakeRefreshDb(user_id=sub))
        # Security contract: every hit still 401s.
        assert exc.value.status_code == 401

    # Dashboard contract: exactly ONE server_errors row for this jti,
    # not five — the flood is collapsed.
    assert len(sink_calls) == 1, (
        f"expected 1 sink for a looping jti, got {len(sink_calls)}: {sink_calls}"
    )
    assert jti in sink_calls[0]


def test_refresh_replay_distinct_jtis_each_sink(monkeypatch):
    """Negative control for the dedup guard: the collapse is keyed per
    jti, so two DIFFERENT replayed jtis must still produce two rows. This
    pins that we deduped noise, not signal — a genuine multi-token replay
    attack is still fully visible on the dashboard.
    """
    sub = "u-replay-distinct"
    jti_a, jti_b = "replay-jti-a", "replay-jti-b"

    fake_redis = _FakeRefreshRedis()
    fake_redis.used.update({jti_a, jti_b})
    fake_redis.families[sub] = {jti_a, jti_b}
    monkeypatch.setattr(auth_router, "redis", fake_redis)
    _patch_auth_revoke_redis(monkeypatch, fake_redis)
    monkeypatch.setattr(auth_router, "log_audit_event_sync", lambda *_a, **_k: None)

    sink_calls: list[str] = []
    monkeypatch.setattr(
        "gdx_dispatch.modules.error_sink.record_server_error",
        lambda *, request, exc, status_code, request_id=None: sink_calls.append(str(exc)),  # noqa: ARG005
        raising=False,
    )

    for jti in (jti_a, jti_b, jti_a, jti_b):  # interleaved replays
        token = _mint_refresh_token(sub=sub, jti=jti)
        request = _fake_request(cookies={"refresh_token": token})
        with pytest.raises(HTTPException):
            auth_router.refresh(request=request, db=_FakeRefreshDb(user_id=sub))

    assert len(sink_calls) == 2, (
        f"two distinct replayed jtis must sink twice, got {len(sink_calls)}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# D-S119-revoke-by-user-vs-jti regression tests
# ═══════════════════════════════════════════════════════════════════════════
#
# Industry pattern (Auth0, Okta): two distinct admin APIs — revoke-token
# (per-jti, narrow) AND revoke-user-sessions (cascade: refresh family).
# We already have revoke_user_sessions() in gdx_dispatch/core/auth_revoke.py;
# this set of tests pins the new admin endpoint that exposes it.


def test_admin_revoke_user_kills_refresh_family(monkeypatch):
    """POST /auth/admin/revoke-user must revoke the user's entire refresh
    family via revoke_user_sessions(), and report the count back."""
    from gdx_dispatch.core import auth_revoke as _revoke
    target_user_id = "user-to-kill"

    revoke_calls: list[dict[str, Any]] = []

    def fake_revoke(sub: str, *, reason: str = "user_lifecycle_event") -> int:
        revoke_calls.append({"sub": sub, "reason": reason})
        return 3  # pretend 3 family members were revoked

    monkeypatch.setattr(_revoke, "revoke_user_sessions", fake_revoke)
    # The endpoint imports from gdx_dispatch.core.auth_revoke, so patch BOTH sites
    # to be defensive (depends on the import shape).
    monkeypatch.setattr(
        "gdx_dispatch.routers.auth.core.revoke_user_sessions", fake_revoke, raising=False,
    )

    monkeypatch.setattr(auth_router, "log_audit_event_sync", lambda *_a, **_k: None)

    body = auth_router.RevokeUserBody(user_id=target_user_id, reason="admin_revoke")
    request = _fake_request()
    fake_db = SimpleNamespace(commit=lambda: None)
    current_user = {"user_id": "admin-1", "tenant_id": "tenant-gdx", "role": "admin"}

    result = auth_router.admin_revoke_user(body, request, current_user, fake_db)

    assert result["status"] == "ok"
    assert result["sessions_revoked"] == 3
    assert len(revoke_calls) == 1
    assert revoke_calls[0]["sub"] == target_user_id
    assert revoke_calls[0]["reason"] == "admin_revoke"


def test_admin_revoke_user_emits_audit_event(monkeypatch):
    """Admin user-revoke must emit a `user_sessions_revoked` audit event
    with the target user_id and revoked-count. Audit forensic trail is
    required by the SOC 2 policy that already covers token revoke."""
    from gdx_dispatch.core import auth_revoke as _revoke
    monkeypatch.setattr(
        _revoke, "revoke_user_sessions",
        lambda sub, *, reason="user_lifecycle_event": 2,
    )
    monkeypatch.setattr(
        "gdx_dispatch.routers.auth.core.revoke_user_sessions",
        lambda sub, *, reason="user_lifecycle_event": 2, raising=False,
    )

    audit_calls: list[dict[str, Any]] = []

    def fake_audit(db: Any, *args: Any, **kwargs: Any) -> None:
        audit_calls.append({"db": db, "args": args, "kwargs": kwargs})

    monkeypatch.setattr(auth_router, "log_audit_event_sync", fake_audit)

    body = auth_router.RevokeUserBody(user_id="target-42", reason="security_incident")
    request = _fake_request()
    fake_db = SimpleNamespace(commit=lambda: None)
    current_user = {"user_id": "admin-9", "tenant_id": "tenant-gdx", "role": "admin"}

    auth_router.admin_revoke_user(body, request, current_user, fake_db)

    assert len(audit_calls) == 1
    kw = audit_calls[0]["kwargs"]
    assert kw["action"] == "user_sessions_revoked"
    assert kw["entity_type"] == "auth"
    assert kw["entity_id"] == "target-42"
    assert kw["user_id"] == "admin-9"  # the actor — who pulled the trigger
    assert kw["details"]["target_user_id"] == "target-42"
    assert kw["details"]["sessions_revoked"] == 2
    assert kw["details"]["reason"] == "security_incident"


def test_admin_revoke_user_idempotent_on_unknown_user(monkeypatch):
    """Unknown user_id (no family marker) returns 200 with
    sessions_revoked=0. Matches Auth0's idempotent-revoke pattern — calling
    the endpoint twice or against a never-logged-in user is safe.
    """
    from gdx_dispatch.core import auth_revoke as _revoke
    monkeypatch.setattr(
        _revoke, "revoke_user_sessions",
        lambda sub, *, reason="user_lifecycle_event": 0,  # nothing to revoke
    )
    monkeypatch.setattr(
        "gdx_dispatch.routers.auth.core.revoke_user_sessions",
        lambda sub, *, reason="user_lifecycle_event": 0, raising=False,
    )
    monkeypatch.setattr(auth_router, "log_audit_event_sync", lambda *_a, **_k: None)

    body = auth_router.RevokeUserBody(user_id="never-logged-in")
    request = _fake_request()
    result = auth_router.admin_revoke_user(
        body, request,
        {"user_id": "admin-1", "tenant_id": "t", "role": "admin"},
        SimpleNamespace(commit=lambda: None),
    )
    assert result == {"status": "ok", "sessions_revoked": 0}


def test_admin_revoke_user_audit_failure_still_returns_ok(monkeypatch):
    """Fail-open contract (matches admin_revoke_token's audit policy):
    an audit-helper exception must not convert a successful revoke into
    a 500."""
    from gdx_dispatch.core import auth_revoke as _revoke
    monkeypatch.setattr(
        _revoke, "revoke_user_sessions",
        lambda sub, *, reason="user_lifecycle_event": 5,
    )
    monkeypatch.setattr(
        "gdx_dispatch.routers.auth.core.revoke_user_sessions",
        lambda sub, *, reason="user_lifecycle_event": 5, raising=False,
    )

    def fake_audit_raises(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("audit chain offline")

    monkeypatch.setattr(auth_router, "log_audit_event_sync", fake_audit_raises)

    body = auth_router.RevokeUserBody(user_id="target")
    request = _fake_request()
    result = auth_router.admin_revoke_user(
        body, request,
        {"user_id": "admin-1", "tenant_id": "t", "role": "admin"},
        SimpleNamespace(commit=lambda: None),
    )
    assert result["status"] == "ok"
    assert result["sessions_revoked"] == 5
