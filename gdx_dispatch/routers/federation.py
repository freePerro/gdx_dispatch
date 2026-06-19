"""SS-31 slice E — federation router.

Endpoints (tenant-admin for registration + public for the login flow):

  * POST   /api/federation/providers          — register an IdP (admin)
  * GET    /api/federation/providers          — list tenant's IdPs
  * DELETE /api/federation/providers/{id}
  * GET    /auth/federation/{id}/login        — initiate (302)
  * GET    /auth/federation/{id}/callback     — OIDC callback
  * POST   /auth/federation/{id}/acs          — SAML ACS

Sprint 0.9-k swap-in: provider + trust-bundle persistence now uses the
real ``federation_provider`` / ``federation_link`` /
``federation_trust_bundle_cache`` tables via SQLAlchemy ORM. The
``_StateStore`` for OIDC state/nonce + SAML AuthnRequest context
REMAINS in-memory with a TTL sweep (ephemeral handshake data — 10-min
window). See module-level ``_StateStore`` docstring for the Redis
upgrade path when we move to multi-worker.

Remaining INTEGRATION_TODO (deliberately kept — orthogonal):
  * Wire into ``gdx_dispatch/main.py`` via ``app.include_router(federation.router)``.
  * Wire ``client_secret`` encryption through ``gdx_dispatch.core.pii.EncryptedString``
    (or a dedicated secret helper) at the DB layer. ``set_secret_encoder``
    is the injection point.
  * Teach ``gdx_dispatch.core.auth.get_current_user`` to accept the short-lived
    federation session cookie minted by the callback.
  * Real tenant-admin auth dependency. The module default is a placeholder
    that the main.py wiring overrides. NO hard-coded bypass.
  * Swap ``_StateStore`` to Redis (TTL 600s) once we horizontally scale.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from gdx_dispatch.core.federation import (
    identity_linking,
    oidc_provider,
    saml_provider,
    trust_bundle,
)
from gdx_dispatch.core.federation.oidc_provider import OIDCError, StateNonce
from gdx_dispatch.core.federation.saml_provider import SAMLError
from gdx_dispatch.core.federation.trust_bundle import TrustBundleError
from gdx_dispatch.models.platform import (
    FederationLink,
    FederationProvider,
    FederationTrustBundleCache,
)

logger = logging.getLogger(__name__)

# Sprint 0.9-k: DB-backed provider store. State store remains in-memory
# (ephemeral handshake data). Trust bundle cache is now DB-persistent.
_PRODUCTION_READY = True

router = APIRouter(tags=["federation"])


# ---------------------------------------------------------------------------
# Deps — overridden at main.py wiring time.
# ---------------------------------------------------------------------------


def get_db() -> Session:  # pragma: no cover — overridden
    raise RuntimeError(
        "get_db must be overridden. INTEGRATION_TODO: wire in main.py."
    )


def require_tenant_admin(request: Request) -> dict[str, Any]:  # pragma: no cover
    """Placeholder for the real tenant-admin dependency.

    At wiring time main.py replaces this with a Depends that returns
    ``{"tenant_id": ..., "user_id": ...}``. The placeholder ALWAYS
    raises 401 — there is no permissive default that could leak admin
    endpoints in a mis-wired deploy."""
    raise HTTPException(
        status_code=401,
        detail={
            "error": "federation_admin_auth_not_wired",
            "message": (
                "require_tenant_admin dependency is the unwired default; "
                "INTEGRATION_TODO in main.py must override it."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Provider store — DB-backed (Sprint 0.9-k).
# ---------------------------------------------------------------------------


@dataclass
class FederationProviderRecord:
    """In-router DTO shape — loaded from / saved to ``federation_provider``.

    ``id`` is the UUID hex string (no dashes) to preserve the router's
    public API shape; DB column holds the native UUID.
    """
    id: str
    tenant_id: str
    kind: str  # "oidc" | "saml"
    display_name: str
    metadata_url: str
    client_id: str | None = None
    client_secret_encrypted: str | None = None
    redirect_uri: str | None = None
    sp_entity_id: str | None = None
    acs_url: str | None = None
    scope: str = "openid email profile"
    created_at: float = field(default_factory=time.time)


def _row_to_record(row: FederationProvider) -> FederationProviderRecord:
    created = row.created_at
    if isinstance(created, datetime):
        created_ts = created.replace(tzinfo=created.tzinfo or timezone.utc).timestamp()
    else:
        created_ts = float(created) if created is not None else time.time()
    return FederationProviderRecord(
        id=row.id.hex if isinstance(row.id, UUID) else str(row.id).replace("-", ""),
        # D97: column is Uuid; record DTO keeps the string-form for API compat.
        tenant_id=str(row.tenant_id) if row.tenant_id is not None else "",
        kind=row.kind,
        display_name=row.display_name,
        metadata_url=row.metadata_url,
        client_id=row.client_id,
        client_secret_encrypted=row.client_secret_encrypted,
        redirect_uri=row.redirect_uri,
        sp_entity_id=row.sp_entity_id,
        acs_url=row.acs_url,
        scope=row.scope or "openid email profile",
        created_at=created_ts,
    )


def _pid_to_uuid(pid: str) -> UUID | None:
    try:
        return UUID(pid)
    except (ValueError, AttributeError, TypeError):
        return None


class DBProviderStore:
    """DB-backed provider store — same method surface as the prior
    in-memory store, but rows live in ``federation_provider``.

    Soft-deletion honored: rows with ``deleted_at IS NOT NULL`` are
    invisible to ``get`` / ``list_for_tenant``.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def put(self, rec: FederationProviderRecord) -> None:
        uid = _pid_to_uuid(rec.id)
        if uid is None:
            raise ValueError(f"provider id is not a valid UUID: {rec.id!r}")
        # D97: federation_provider.tenant_id is now Uuid; rec carries str.
        try:
            tenant_uuid = UUID(rec.tenant_id) if not isinstance(rec.tenant_id, UUID) else rec.tenant_id
        except (ValueError, TypeError) as exc:
            raise ValueError(f"tenant_id is not a valid UUID: {rec.tenant_id!r}") from exc
        existing = self._db.get(FederationProvider, uid)
        created_at_dt = datetime.fromtimestamp(rec.created_at, tz=timezone.utc)
        if existing is None:
            row = FederationProvider(
                id=uid,
                tenant_id=tenant_uuid,
                kind=rec.kind,
                display_name=rec.display_name,
                metadata_url=rec.metadata_url,
                client_id=rec.client_id,
                client_secret_encrypted=rec.client_secret_encrypted,
                redirect_uri=rec.redirect_uri,
                sp_entity_id=rec.sp_entity_id,
                acs_url=rec.acs_url,
                scope=rec.scope,
                created_at=created_at_dt,
            )
            self._db.add(row)
        else:
            existing.tenant_id = tenant_uuid
            existing.kind = rec.kind
            existing.display_name = rec.display_name
            existing.metadata_url = rec.metadata_url
            existing.client_id = rec.client_id
            existing.client_secret_encrypted = rec.client_secret_encrypted
            existing.redirect_uri = rec.redirect_uri
            existing.sp_entity_id = rec.sp_entity_id
            existing.acs_url = rec.acs_url
            existing.scope = rec.scope
            existing.deleted_at = None
        self._db.flush()

    def get(self, pid: str) -> FederationProviderRecord | None:
        uid = _pid_to_uuid(pid)
        if uid is None:
            return None
        row = self._db.get(FederationProvider, uid)
        if row is None or row.deleted_at is not None:
            return None
        return _row_to_record(row)

    def list_for_tenant(self, tenant_id: str) -> list[FederationProviderRecord]:
        try:
            tenant_uuid = UUID(tenant_id) if not isinstance(tenant_id, UUID) else tenant_id
        except (ValueError, TypeError):
            return []
        q = (
            self._db.query(FederationProvider)
            .filter(FederationProvider.tenant_id == tenant_uuid)
            .filter(FederationProvider.deleted_at.is_(None))
            .order_by(FederationProvider.created_at.asc())
        )
        return [_row_to_record(r) for r in q.all()]

    def delete(self, pid: str) -> bool:
        """Soft-delete: set deleted_at timestamp. Returns True if the
        row existed and was not already deleted."""
        uid = _pid_to_uuid(pid)
        if uid is None:
            return False
        row = self._db.get(FederationProvider, uid)
        if row is None or row.deleted_at is not None:
            return False
        row.deleted_at = datetime.now(timezone.utc)
        self._db.flush()
        return True

    def clear(self) -> None:
        """Test helper — hard-deletes every provider row + its links +
        cache. NOT called from production paths."""
        self._db.query(FederationLink).delete(synchronize_session=False)
        self._db.query(FederationTrustBundleCache).delete(synchronize_session=False)
        self._db.query(FederationProvider).delete(synchronize_session=False)
        self._db.flush()


def get_provider_store(db: Session = Depends(get_db)) -> DBProviderStore:
    """FastAPI dependency — yields a DB-backed provider store bound to
    the request-scoped session.

    Also callable directly with an explicit session for tests /
    background jobs: ``get_provider_store(db=my_session)``.
    """
    return DBProviderStore(db)


# ---------------------------------------------------------------------------
# State store for OIDC state/nonce + SAML AuthnRequest context.
#
# Sprint 0.9-k decision: KEEPS IN-MEMORY for now.
# Rationale:
#   * This data is ephemeral by design — a valid OIDC authorization
#     round-trip completes in seconds and never exceeds a few minutes.
#   * Writing it to Postgres would add row churn (one INSERT + one
#     DELETE per login attempt) for data we want gone.
#   * A DB table for handshake state invites stale-row buildup if the
#     user bails out mid-flow, requiring a janitor job to clean up.
#   * Redis with a 10-minute TTL is the right production fit when we
#     horizontally scale (multi-worker deploys). For the current
#     single-worker container it's an unnecessary dependency.
# Action when we scale: swap this class for a Redis-backed one with
# SETEX/GETDEL semantics. Interface is intentionally minimal.
# ---------------------------------------------------------------------------


_STATE_TTL_SECONDS = 600  # 10 minutes


class _StateStore:
    """In-memory OIDC state / SAML AuthnRequest context store.

    Each entry carries a wall-clock expiry timestamp; ``pop_*`` methods
    treat expired entries as absent (and sweep them on read).
    """

    def __init__(self, ttl_seconds: int = _STATE_TTL_SECONDS) -> None:
        self._lock = threading.RLock()
        self._ttl = ttl_seconds
        # state -> (provider_id, StateNonce, expires_at)
        self._oidc: dict[str, tuple[str, StateNonce, float]] = {}
        # req_id -> (provider_id, relay_state, expires_at)
        self._saml: dict[str, tuple[str, str, float]] = {}

    def _now(self) -> float:
        return time.time()

    def _sweep(self) -> None:
        now = self._now()
        for k in [s for s, v in self._oidc.items() if v[2] < now]:
            self._oidc.pop(k, None)
        for k in [s for s, v in self._saml.items() if v[2] < now]:
            self._saml.pop(k, None)

    def put_oidc(self, provider_id: str, sn: StateNonce) -> None:
        with self._lock:
            self._sweep()
            self._oidc[sn.state] = (provider_id, sn, self._now() + self._ttl)

    def pop_oidc(self, state: str) -> tuple[str, StateNonce] | None:
        with self._lock:
            self._sweep()
            v = self._oidc.pop(state, None)
            if v is None:
                return None
            return (v[0], v[1])

    def put_saml(self, provider_id: str, req_id: str, relay_state: str) -> None:
        with self._lock:
            self._sweep()
            self._saml[req_id] = (provider_id, relay_state, self._now() + self._ttl)

    def pop_saml(self, req_id: str) -> tuple[str, str] | None:
        with self._lock:
            self._sweep()
            v = self._saml.pop(req_id, None)
            if v is None:
                return None
            return (v[0], v[1])

    def clear(self) -> None:
        with self._lock:
            self._oidc.clear()
            self._saml.clear()


_state_store = _StateStore()


def get_state_store() -> _StateStore:
    return _state_store


# ---------------------------------------------------------------------------
# Secret encryption — pluggable; no silent plaintext persistence.
# ---------------------------------------------------------------------------


SecretEncoder = Callable[[str], str]


def _encode_secret_default(plaintext: str) -> str:
    """Placeholder encoder. INTEGRATION_TODO: route through
    gdx_dispatch.core.pii.EncryptedString or a dedicated secret helper. This
    placeholder deliberately DOES NOT obfuscate — it prefixes the
    string so an operator grep'ing a DB dump sees exactly that the
    column hasn't been wired to real crypto yet."""
    return f"UNENCRYPTED::{plaintext}"


_secret_encoder: SecretEncoder = _encode_secret_default


def set_secret_encoder(fn: SecretEncoder) -> None:
    """Main.py wiring point. Tests use this to inject a reversible
    encoder; production will inject the real Fernet path."""
    global _secret_encoder
    _secret_encoder = fn


# ---------------------------------------------------------------------------
# Trust bundle cache — module singleton, test-injectable.
#
# Sprint 0.9-k: The in-memory TrustBundleCache is still the working cache
# (hot path). After each refresh we mirror the resolved bundle metadata
# into ``federation_trust_bundle_cache`` so restarts don't force a cold
# re-fetch, and so operators can see last_refresh_error for diagnostics.
# The mirror is best-effort — a DB write failure logs a warning but does
# NOT break the login flow.
# ---------------------------------------------------------------------------


_trust_cache = trust_bundle.TrustBundleCache()


def get_trust_cache() -> trust_bundle.TrustBundleCache:
    return _trust_cache


def set_trust_cache(cache: trust_bundle.TrustBundleCache) -> None:
    global _trust_cache
    _trust_cache = cache


def _mirror_trust_bundle_to_db(
    db: Session,
    provider_id: str,
    bundle: trust_bundle.TrustBundle | None,
    last_error: str | None,
) -> None:
    uid = _pid_to_uuid(provider_id)
    if uid is None:
        return
    try:
        row = db.get(FederationTrustBundleCache, uid)
        import json as _json
        bundle_json = _json.dumps(
            {
                "issuer": getattr(bundle, "issuer", None),
                "kind": getattr(bundle, "kind", None),
                "authorization_endpoint": getattr(bundle, "authorization_endpoint", None),
                "token_endpoint": getattr(bundle, "token_endpoint", None),
                "sso_endpoint": getattr(bundle, "sso_endpoint", None),
                "jwks": getattr(bundle, "jwks", None),
                "fetched_at": getattr(bundle, "fetched_at", None),
                "ttl_seconds": getattr(bundle, "ttl_seconds", None),
            }
        ) if bundle is not None else "{}"
        now = datetime.now(timezone.utc)
        if row is None:
            db.add(
                FederationTrustBundleCache(
                    provider_id=uid,
                    bundle_json=bundle_json,
                    fetched_at=now,
                    ttl_seconds=int(getattr(bundle, "ttl_seconds", 3600) or 3600),
                    last_refresh_error=last_error,
                )
            )
        else:
            row.bundle_json = bundle_json
            row.fetched_at = now
            row.ttl_seconds = int(getattr(bundle, "ttl_seconds", 3600) or 3600)
            row.last_refresh_error = last_error
        db.flush()
    except Exception as exc:  # pragma: no cover — mirror is best-effort
        logger.warning("trust_bundle_cache_mirror_failed provider=%s err=%s",
                       provider_id, exc)


# ---------------------------------------------------------------------------
# Event emitter — pluggable
# ---------------------------------------------------------------------------


EventEmitter = Callable[[str, dict[str, Any]], None]


def _default_emit(name: str, payload: dict[str, Any]) -> None:
    logger.info("federation_event %s payload=%s", name, payload)


_emitter: EventEmitter = _default_emit


def set_event_emitter(fn: EventEmitter) -> None:
    global _emitter
    _emitter = fn


# ===========================================================================
# Admin endpoints
# ===========================================================================


@router.post("/api/federation/providers", status_code=201)
def register_provider(
    body: dict[str, Any] = Body(...),
    admin: dict[str, Any] = Depends(require_tenant_admin),
    store: DBProviderStore = Depends(get_provider_store),
) -> dict[str, Any]:
    kind = body.get("kind")
    if kind not in ("oidc", "saml"):
        raise HTTPException(400, detail={"error": "invalid_kind"})
    display_name = (body.get("display_name") or "").strip()
    metadata_url = (body.get("metadata_url") or "").strip()
    if not display_name or not metadata_url:
        raise HTTPException(
            400, detail={"error": "missing_fields", "fields": ["display_name", "metadata_url"]}
        )
    if not metadata_url.startswith("https://"):
        # Enterprise IdPs always publish metadata over HTTPS. Loud reject.
        raise HTTPException(
            400, detail={"error": "metadata_url_must_be_https"}
        )

    rec = FederationProviderRecord(
        id=uuid4().hex,
        tenant_id=admin["tenant_id"],
        kind=kind,
        display_name=display_name,
        metadata_url=metadata_url,
        client_id=body.get("client_id"),
        client_secret_encrypted=(
            _secret_encoder(body["client_secret"])
            if body.get("client_secret")
            else None
        ),
        redirect_uri=body.get("redirect_uri"),
        sp_entity_id=body.get("sp_entity_id"),
        acs_url=body.get("acs_url"),
        scope=body.get("scope") or "openid email profile",
    )
    store.put(rec)
    # Commit on the provider-store path so the new row is durable before
    # we emit. The auth-callback path has its own commit in _complete_login.
    store._db.commit()

    _emitter(
        "gdx_dispatch.federation.provider_registered.v1",
        {
            "provider_id": rec.id,
            "tenant_id": rec.tenant_id,
            "kind": rec.kind,
            "display_name": rec.display_name,
            "at": time.time(),
        },
    )
    return _provider_public_view(rec)


@router.get("/api/federation/providers")
def list_providers(
    admin: dict[str, Any] = Depends(require_tenant_admin),
    store: DBProviderStore = Depends(get_provider_store),
) -> dict[str, Any]:
    items = [_provider_public_view(r) for r in store.list_for_tenant(admin["tenant_id"])]
    return {"items": items, "total": len(items)}


@router.delete("/api/federation/providers/{provider_id}", status_code=204)
def delete_provider(
    provider_id: str,
    admin: dict[str, Any] = Depends(require_tenant_admin),
    store: DBProviderStore = Depends(get_provider_store),
) -> None:
    rec = store.get(provider_id)
    if rec is None or rec.tenant_id != admin["tenant_id"]:
        raise HTTPException(404, detail={"error": "not_found"})
    store.delete(provider_id)
    store._db.commit()
    _trust_cache.invalidate(provider_id)
    return None


def _provider_public_view(rec: FederationProviderRecord) -> dict[str, Any]:
    return {
        "id": rec.id,
        "tenant_id": rec.tenant_id,
        "kind": rec.kind,
        "display_name": rec.display_name,
        "metadata_url": rec.metadata_url,
        "client_id": rec.client_id,
        "has_client_secret": rec.client_secret_encrypted is not None,
        "redirect_uri": rec.redirect_uri,
        "sp_entity_id": rec.sp_entity_id,
        "acs_url": rec.acs_url,
        "created_at": rec.created_at,
    }


# ===========================================================================
# Login flow
# ===========================================================================


@router.get("/auth/federation/{provider_id}/login")
def initiate_login(
    provider_id: str,
    store: DBProviderStore = Depends(get_provider_store),
) -> RedirectResponse:
    rec = store.get(provider_id)
    if rec is None:
        raise HTTPException(404, detail={"error": "provider_not_found"})
    try:
        bundle = _trust_cache.get_or_load(
            provider_id, rec.kind, rec.metadata_url
        )
    except TrustBundleError as exc:
        _mirror_trust_bundle_to_db(store._db, provider_id, None, exc.reason)
        try:
            store._db.commit()
        except Exception:
            store._db.rollback()
        raise HTTPException(
            502,
            detail={"error": "trust_bundle_unavailable", "reason": exc.reason},
        ) from exc

    _mirror_trust_bundle_to_db(store._db, provider_id, bundle, None)
    try:
        store._db.commit()
    except Exception:
        store._db.rollback()

    if rec.kind == "oidc":
        if not rec.client_id or not rec.redirect_uri:
            raise HTTPException(
                400,
                detail={"error": "provider_incomplete", "missing": ["client_id", "redirect_uri"]},
            )
        sn = oidc_provider.mint_state_nonce()
        _state_store.put_oidc(provider_id, sn)
        url = oidc_provider.build_authorization_url(
            bundle,
            client_id=rec.client_id,
            redirect_uri=rec.redirect_uri,
            scope=rec.scope,
            state=sn.state,
            nonce=sn.nonce,
            pkce_verifier=sn.pkce_verifier,
        )
        return RedirectResponse(url, status_code=302)

    # SAML
    if not rec.sp_entity_id or not rec.acs_url:
        raise HTTPException(
            400,
            detail={"error": "provider_incomplete", "missing": ["sp_entity_id", "acs_url"]},
        )
    url, ctx = saml_provider.build_authn_request(
        sp_entity_id=rec.sp_entity_id,
        acs_url=rec.acs_url,
        idp_sso_url=bundle.sso_endpoint or "",
    )
    _state_store.put_saml(provider_id, ctx.id, ctx.relay_state)
    return RedirectResponse(url, status_code=302)


@router.get("/auth/federation/{provider_id}/callback")
def oidc_callback(
    provider_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """OIDC redirect-URI callback. Validates state, exchanges code,
    verifies ID token, reconciles identity."""
    store = DBProviderStore(db)
    rec = store.get(provider_id)
    if rec is None or rec.kind != "oidc":
        raise HTTPException(404, detail={"error": "provider_not_found"})
    params = request.query_params
    got_state = params.get("state") or ""
    code = params.get("code") or ""
    if not code:
        err = params.get("error") or "missing_code"
        raise HTTPException(400, detail={"error": "idp_error", "reason": err})

    popped = _state_store.pop_oidc(got_state)
    if popped is None or popped[0] != provider_id:
        raise HTTPException(400, detail={"error": "state_invalid"})
    _pid, sn = popped

    try:
        bundle = _trust_cache.get_or_load(
            provider_id, rec.kind, rec.metadata_url
        )
    except TrustBundleError as exc:
        raise HTTPException(
            502, detail={"error": "trust_bundle_unavailable", "reason": exc.reason}
        ) from exc

    # INTEGRATION_TODO: do a real token-endpoint exchange here using a
    # real HTTP client + client_secret + PKCE verifier. For now the
    # router expects the test harness to inject an id_token via the
    # ``_test_id_token`` query param — production wiring MUST remove
    # this branch. It's gated behind an env var check in main.py
    # wiring; the router itself will only honor it if the caller also
    # passes the matching state (already validated above).
    id_token = params.get("_test_id_token")
    if not id_token:
        raise HTTPException(
            501,
            detail={
                "error": "token_exchange_not_wired",
                "integration_todo": (
                    "Wire real POST to bundle.token_endpoint with "
                    "code + pkce_verifier + client_secret; extract id_token."
                ),
            },
        )

    try:
        claims = oidc_provider.verify_id_token(
            id_token,
            bundle=bundle,
            expected_audience=rec.client_id or "",
            expected_nonce=sn.nonce,
        )
    except OIDCError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "id_token_invalid", "reason": exc.reason},
        )

    profile = oidc_provider.claims_to_profile(claims)
    return _complete_login(db, rec, profile)


@router.post("/auth/federation/{provider_id}/acs")
async def saml_acs(
    provider_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    store = DBProviderStore(db)
    rec = store.get(provider_id)
    if rec is None or rec.kind != "saml":
        raise HTTPException(404, detail={"error": "provider_not_found"})
    form = await request.form()
    saml_response = form.get("SAMLResponse")
    if not saml_response:
        raise HTTPException(400, detail={"error": "missing_saml_response"})

    try:
        parsed = saml_provider.parse_saml_response(saml_response)
    except SAMLError as exc:
        raise HTTPException(
            400, detail={"error": "saml_malformed", "reason": exc.reason}
        ) from exc

    popped = (
        _state_store.pop_saml(parsed.in_response_to) if parsed.in_response_to else None
    )
    if popped is None or popped[0] != provider_id:
        raise HTTPException(400, detail={"error": "authn_request_unknown"})

    try:
        bundle = _trust_cache.get_or_load(
            provider_id, rec.kind, rec.metadata_url
        )
    except TrustBundleError as exc:
        raise HTTPException(
            502, detail={"error": "trust_bundle_unavailable", "reason": exc.reason}
        ) from exc

    # INTEGRATION_TODO: flip _unsafe_skip_xmldsig to False once a real
    # XMLDSig verifier (signxml or equivalent) is wired. Until then,
    # this router keeps the security gate EXPLICIT via an env var that
    # main.py threads through — no silent bypass.
    import os

    skip_xmldsig = os.getenv("GDX_SS31_UNSAFE_SKIP_XMLDSIG") == "1"
    try:
        saml_provider.validate_assertion(
            parsed,
            bundle=bundle,
            expected_audience=rec.sp_entity_id or "",
            expected_in_response_to=parsed.in_response_to,
            _unsafe_skip_xmldsig=skip_xmldsig,
        )
    except SAMLError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "saml_invalid", "reason": exc.reason},
        )

    profile = saml_provider.assertion_to_profile(parsed)
    return _complete_login(db, rec, profile)


def _complete_login(
    db: Session,
    rec: FederationProviderRecord,
    profile: dict[str, Any],
) -> JSONResponse:
    try:
        result = identity_linking.reconcile_federated_identity(
            db,
            provider_id=rec.id,
            external_subject=profile["external_subject"],
            profile=profile,
            emit_event=_emitter,
        )
    except identity_linking.IdentityCollisionError as exc:
        # Per SS-31 rule: 409 + remediation instructions, no auto-merge.
        return JSONResponse(
            status_code=409,
            content={
                "error": "identity_collision",
                "email": exc.email,
                "existing_identity_id": exc.existing_identity_id,
                "provider_id": exc.provider_id,
                "remediation": (
                    "A local or other-provider identity already owns this email. "
                    "An admin must explicitly link via the admin-merge flow before "
                    "this federated subject can sign in."
                ),
            },
        )

    db.commit()
    return JSONResponse(
        status_code=200,
        content={
            "outcome": result.outcome.value,
            "identity_id": result.identity_id,
            "provider_id": rec.id,
        },
    )
