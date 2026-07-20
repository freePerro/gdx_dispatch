"""Bank Feeds OAuth — Banno Consumer API (OIDC authorization-code flow).

Security posture (audited plan, rev 3):

- CSRF state is SIGNED (itsdangerous, 10-min max-age) and SINGLE-USE — the
  per-state nonce is consumed on callback via Redis ``SET NX`` and is also
  sent as the OIDC ``nonce`` parameter, so the id_token must echo it back.
  A leaked authorize URL can therefore complete at most one callback, and
  an id_token minted for any other request is rejected.
- The token endpoint is PINNED to the documented
  ``{fi_host}/a/consumer/api/v0/oidc/token`` — never taken from discovery.
  Discovery-returned URLs (authorize, JWKS) must be HTTPS on ``fi_host``.
- id_tokens are verified (sig + iss + aud + exp + nonce) against the FI's
  JWKS, fetched with OUR OWN SSRF-guarded httpx client and loaded via
  ``jwt.PyJWK`` — deliberately NOT ``jwt.PyJWKClient``, which fetches with
  urllib (bypasses the SSRF guard/timeouts and can't be respx-mocked).
- Banno ROTATES the refresh token on every exchange. Refreshes are
  serialized per connection with ``pg_advisory_xact_lock`` + ``SELECT …
  FOR UPDATE`` + a peer-recheck (QB S122-9 pattern); the rotated token is
  persisted in the same commit that stores the new access token, before
  any use. On refresh failure we RAISE (never return the stale token — a
  600-second Banno token is already dead by the time refresh was needed).
- Never log token responses or the Basic auth header; error bodies are
  truncated.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets as _secrets
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core import pii
from gdx_dispatch.core.ssrf_guard import validate_outbound_url
from gdx_dispatch.modules.bank_feeds.models import (
    AUTH_DISCONNECTED,
    AUTH_HEALTHY,
    AUTH_NEEDS_RECONNECT,
    AUTH_REFRESH_FAILED,
    BannoConnection,
    BannoInstitution,
)

log = logging.getLogger(__name__)

STATE_MAX_AGE_S = 600
STATE_SALT = "bank-feeds-oauth-state"
# Refresh when the access token has less than this left. Banno tokens live
# 600s — QB's 5-minute margin would refresh on nearly every call, so 2 min.
REFRESH_MARGIN = timedelta(minutes=2)
DISCOVERY_TTL_S = 3600

OAUTH_SCOPES = (
    "openid "
    "https://api.banno.com/consumer/auth/offline_access "
    "https://api.banno.com/consumer/auth/transactions.detail.readonly "
    "https://api.banno.com/consumer/auth/documents.readonly"
)

_FI_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251})[a-z0-9]$")

ID_TOKEN_ALGORITHMS = ["RS256", "ES256"]
ID_TOKEN_LEEWAY_S = 60


class BankFeedsAuthError(RuntimeError):
    """Auth/OAuth failure in the bank-feeds module."""


class BankFeedsRefreshError(BankFeedsAuthError):
    """Token refresh failed — access token unavailable."""


# ── encryption (QBTokenStore pattern; core.pii._FERNET) ────────────────


def _encrypt(value: str) -> str:
    f = getattr(pii, "_FERNET", None)
    if value and f:
        return f.encrypt(value.encode("utf-8")).decode("utf-8")
    return value


# One warning per process — this module was born encrypted, so the
# passthrough only fires for keyless-dev rows; no need for per-value dedupe.
_PASSTHROUGH_WARNED = False


def _decrypt(value: str) -> str:
    f = getattr(pii, "_FERNET", None)
    if not value or not f:
        return value
    from cryptography.fernet import InvalidToken  # noqa: PLC0415
    try:
        return f.decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # This module never wrote plaintext rows (born encrypted), but the
        # keyless-dev fallback means dev-created rows can be plaintext.
        # Deliberately NOT pii._emit_passthrough_warning: that helper logs a
        # 6-char prefix of the value, and these values are live bank OAuth
        # tokens — no derivative of them may reach the log stream
        # (CodeQL py/clear-text-logging-sensitive-data, PR #164).
        global _PASSTHROUGH_WARNED
        if not _PASSTHROUGH_WARNED:
            _PASSTHROUGH_WARNED = True
            log.warning(
                "bank_feeds.oauth._decrypt: InvalidToken — passthrough returning "
                "raw value. Further events suppressed for this process."
            )
        return value


# ── fi_host validation ─────────────────────────────────────────────────


def validate_fi_host(host: str) -> str:
    """Normalize + validate a bare FI hostname. Raises ValueError.

    Hostname ONLY — no scheme, path, port, or userinfo. Then the SSRF
    guard vets the resulting https URL (blocks IP literals / internal
    ranges / metadata endpoints).
    """
    h = (host or "").strip().lower().rstrip(".")
    if not h or len(h) > 253 or not _FI_HOST_RE.match(h) or ".." in h or "." not in h:
        raise ValueError("fi_host must be a bare hostname like digital.example.com")
    validate_outbound_url(f"https://{h}/")
    return h


# ── signed single-use state ────────────────────────────────────────────


def _signing_secret() -> str:
    """Outlook `_state_signer` pattern: STATE_SIGNING_KEY | JWT_SECRET |
    SECRET_KEY (≥32 bytes)."""
    for env_name in ("STATE_SIGNING_KEY", "JWT_SECRET", "SECRET_KEY"):
        secret = os.getenv(env_name)
        if secret and len(secret) >= 32:
            return secret
    raise BankFeedsAuthError(
        "OAuth state signing key not configured. Set STATE_SIGNING_KEY, "
        "JWT_SECRET, or SECRET_KEY (>=32 bytes)."
    )


def _state_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_signing_secret(), salt=STATE_SALT)


def make_state(*, user_id: str, tenant_id: str, institution_id: str) -> tuple[str, str]:
    """Returns (signed_state, nonce). The nonce doubles as the OIDC
    ``nonce`` parameter."""
    nonce = _secrets.token_urlsafe(16)
    state = _state_signer().dumps(
        {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "institution_id": institution_id,
            "nonce": nonce,
        }
    )
    return state, nonce


def load_state(state: str) -> dict:
    """Verify signature + age. Raises BankFeedsAuthError on any failure."""
    try:
        payload = _state_signer().loads(state, max_age=STATE_MAX_AGE_S)
    except Exception as exc:  # noqa: BLE001 — itsdangerous raises several types
        raise BankFeedsAuthError("invalid or expired OAuth state") from exc
    if not isinstance(payload, dict) or not payload.get("nonce"):
        raise BankFeedsAuthError("malformed OAuth state")
    return payload


# ── PKCE (RFC 7636, S256) ──────────────────────────────────────────────
#
# Banno's authorization server REQUIRES PKCE (Garden rejects a bare
# auth-code request with "policy requires PKCE"). The verifier is DERIVED
# from the state nonce with the signing secret rather than stored: the
# nonce is public in the authorize URL, but computing the verifier needs
# the server secret, so an intercepted redirect still can't redeem the
# code. 32 HMAC bytes → 43-char base64url, the RFC 7636 minimum length.


def pkce_verifier_for_nonce(nonce: str) -> str:
    digest = hmac.new(
        _signing_secret().encode("utf-8"),
        f"bank-feeds-pkce:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# In-process fallback nonce store for dev/tests without Redis. Maps
# nonce -> expiry monotonic time. Single-worker only — production uses Redis.
_local_nonces: dict[str, float] = {}


def consume_nonce(nonce: str) -> bool:
    """True exactly once per nonce (single-use state enforcement).

    Redis ``SET NX EX`` when available; in-process fallback otherwise
    (tests / keyless dev — single process, so still correct there).
    """
    key = f"bankfeeds:oauth:nonce:{nonce}"
    try:
        from redis import from_url as redis_from_url  # noqa: PLC0415

        client = redis_from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_connect_timeout=1,
        )
        try:
            return bool(client.set(key, "1", nx=True, ex=STATE_MAX_AGE_S))
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 — redis missing/unreachable
        now = time.monotonic()
        for k, exp in list(_local_nonces.items()):
            if exp < now:
                _local_nonces.pop(k, None)
        if nonce in _local_nonces:
            return False
        _local_nonces[nonce] = now + STATE_MAX_AGE_S
        return True


# ── OIDC discovery ─────────────────────────────────────────────────────

_discovery_cache: dict[str, tuple[float, dict]] = {}


def token_endpoint_for(fi_host: str) -> str:
    """PINNED to the documented path — never taken from discovery."""
    return f"https://{fi_host}/a/consumer/api/v0/oidc/token"


def _require_same_host_https(url: str, fi_host: str, what: str) -> str:
    from urllib.parse import urlparse  # noqa: PLC0415

    parsed = urlparse(url or "")
    if parsed.scheme != "https" or parsed.hostname != fi_host:
        raise BankFeedsAuthError(
            f"discovery {what} is not an https URL on the institution host"
        )
    validate_outbound_url(url)
    return url


def discover_oidc(fi_host: str, *, force: bool = False) -> dict:
    """Fetch + cache ``/.well-known/openid-configuration`` for an FI host.

    Every URL we consume from the document is required to be HTTPS on the
    same fi_host — a compromised discovery document must not be able to
    point token/JWKS traffic anywhere else.
    """
    now = time.monotonic()
    cached = _discovery_cache.get(fi_host)
    if cached and not force and (now - cached[0] < DISCOVERY_TTL_S):
        return cached[1]

    url = f"https://{fi_host}/.well-known/openid-configuration"
    validate_outbound_url(url)
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(url)
    if not resp.is_success:
        raise BankFeedsAuthError(
            f"OIDC discovery failed for institution host (HTTP {resp.status_code})"
        )
    doc = resp.json()
    if not isinstance(doc, dict):
        raise BankFeedsAuthError("OIDC discovery response was not a JSON object")

    _require_same_host_https(str(doc.get("authorization_endpoint") or ""), fi_host, "authorization_endpoint")
    _require_same_host_https(str(doc.get("jwks_uri") or ""), fi_host, "jwks_uri")
    issuer = str(doc.get("issuer") or "")
    if not issuer:
        raise BankFeedsAuthError("OIDC discovery document has no issuer")

    _discovery_cache[fi_host] = (now, doc)
    return doc


# ── token exchange + refresh (HTTP) ────────────────────────────────────


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    return base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("utf-8")


def _token_request(fi_host: str, client_id: str, client_secret: str, form: dict) -> dict:
    endpoint = token_endpoint_for(fi_host)
    validate_outbound_url(endpoint)
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(
            endpoint,
            data=form,
            headers={
                "Authorization": f"Basic {_basic_auth_header(client_id, client_secret)}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    if not resp.is_success:
        # Truncated body, no token material — grant errors are short JSON.
        log.error(
            "bank_feeds_token_request_failed host=%s grant=%s status=%d body=%s",
            fi_host, form.get("grant_type"), resp.status_code, resp.text[:200],
        )
        raise BankFeedsAuthError(
            f"token request failed: HTTP {resp.status_code} {resp.text[:100]}"
        )
    data = resp.json()
    if not isinstance(data, dict) or not data.get("access_token"):
        raise BankFeedsAuthError("token response missing access_token")
    return data


def exchange_code_for_tokens(
    fi_host: str,
    client_id: str,
    client_secret: str,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict:
    return _token_request(
        fi_host, client_id, client_secret,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )


def refresh_access_token(
    fi_host: str, client_id: str, client_secret: str, *, refresh_token: str
) -> dict:
    return _token_request(
        fi_host, client_id, client_secret,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )


# ── id_token verification ──────────────────────────────────────────────


def fetch_jwks(jwks_uri: str, fi_host: str) -> dict:
    """Fetch the JWKS document with our own httpx client (SSRF-guarded,
    timeout-configured, respx-mockable). NOT PyJWKClient (urllib)."""
    _require_same_host_https(jwks_uri, fi_host, "jwks_uri")
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(jwks_uri)
    if not resp.is_success:
        raise BankFeedsAuthError(f"JWKS fetch failed (HTTP {resp.status_code})")
    doc = resp.json()
    if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
        raise BankFeedsAuthError("JWKS response malformed")
    return doc


def verify_id_token(
    id_token: str, *, fi_host: str, client_id: str, nonce: str, discovery: dict
) -> dict:
    """Full verification: signature (JWKS), iss, aud, exp, and the OIDC
    nonce claim must match the state's nonce. Returns the claims dict.
    Raises BankFeedsAuthError on ANY failure — the callback hard-fails."""
    import jwt as pyjwt  # noqa: PLC0415

    if not id_token:
        raise BankFeedsAuthError("token response missing id_token")

    try:
        header = pyjwt.get_unverified_header(id_token)
    except Exception as exc:  # noqa: BLE001
        raise BankFeedsAuthError("id_token header unparseable") from exc

    jwks = fetch_jwks(str(discovery.get("jwks_uri") or ""), fi_host)
    kid = header.get("kid")
    key_dict = None
    for jwk in jwks["keys"]:
        if kid is None or jwk.get("kid") == kid:
            key_dict = jwk
            break
    if key_dict is None:
        raise BankFeedsAuthError("id_token kid not found in JWKS")

    try:
        signing_key = pyjwt.PyJWK(key_dict).key
        claims = pyjwt.decode(
            id_token,
            key=signing_key,
            algorithms=ID_TOKEN_ALGORITHMS,
            audience=client_id,
            issuer=str(discovery.get("issuer") or ""),
            leeway=ID_TOKEN_LEEWAY_S,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except Exception as exc:  # noqa: BLE001
        raise BankFeedsAuthError("id_token verification failed") from exc

    if str(claims.get("nonce") or "") != nonce:
        raise BankFeedsAuthError("id_token nonce mismatch")
    if not claims.get("sub"):
        raise BankFeedsAuthError("id_token has no sub")
    return claims


# ── connection persistence ─────────────────────────────────────────────


def upsert_connection(
    db: Session,
    institution: BannoInstitution,
    *,
    banno_user_id: str,
    token_data: dict,
    connected_by: str | None,
) -> BannoConnection:
    """Create or reuse the connection row for (institution, sub).

    One ACTIVE connection per institution: if a different sub currently
    holds a non-disconnected connection, raise — the operator must
    disconnect first (prevents joint-account double-ingestion and an
    ambiguous per-institution status).
    """
    other = db.execute(
        select(BannoConnection).where(
            BannoConnection.institution_id == institution.id,
            BannoConnection.banno_user_id != banno_user_id,
            BannoConnection.auth_state != AUTH_DISCONNECTED,
        )
    ).scalars().first()
    if other is not None:
        raise BankFeedsAuthError(
            "This institution is already connected as another bank user. "
            "Disconnect it first."
        )

    row = db.execute(
        select(BannoConnection).where(
            BannoConnection.institution_id == institution.id,
            BannoConnection.banno_user_id == banno_user_id,
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if row is None:
        row = BannoConnection(
            institution_id=institution.id,
            fi_host=institution.fi_host,
            banno_user_id=banno_user_id,
        )
        db.add(row)

    row.fi_host = institution.fi_host
    _store_token_data(row, token_data, now)
    row.auth_state = AUTH_HEALTHY
    row.connected_by_user_id = connected_by
    row.updated_at = now
    db.commit()
    db.refresh(row)
    return row


def _store_token_data(row: BannoConnection, token_data: dict, now: datetime) -> None:
    row.access_token_enc = _encrypt(str(token_data.get("access_token") or ""))
    new_refresh = str(token_data.get("refresh_token") or "")
    if new_refresh:
        row.refresh_token_enc = _encrypt(new_refresh)
    row.access_token_expires_at = now + timedelta(seconds=int(token_data.get("expires_in") or 600))
    refresh_expires = token_data.get("refresh_token_expires_in")
    if refresh_expires:
        row.refresh_token_expires_at = now + timedelta(seconds=int(refresh_expires))


def soft_disconnect(db: Session, institution_id: UUID) -> int:
    """Null tokens + mark disconnected on every connection of the
    institution. Rows are KEPT (audited plan B1) — deleting them would
    orphan the account tree and duplicate everything on reconnect."""
    rows = db.execute(
        select(BannoConnection).where(BannoConnection.institution_id == institution_id)
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.access_token_enc = None
        row.refresh_token_enc = None
        row.access_token_expires_at = None
        row.refresh_token_expires_at = None
        row.auth_state = AUTH_DISCONNECTED
        row.updated_at = now
    db.commit()
    return len(rows)


def connection_healthy(connection: BannoConnection) -> bool:
    return connection.auth_state == AUTH_HEALTHY and bool(connection.refresh_token_enc)


# ── access-token acquisition (locked rotating refresh) ─────────────────

_REFRESH_LOCK_NAMESPACE = "banno_token_refresh"


def _refresh_lock_key(tenant_id: str, institution_id: str, sub: str) -> int:
    digest = hashlib.sha256(
        f"{_REFRESH_LOCK_NAMESPACE}:{tenant_id}:{institution_id}:{sub}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _is_postgres(db: Session) -> bool:
    bind = getattr(db, "bind", None)
    dialect = getattr(bind, "dialect", None)
    return getattr(dialect, "name", None) == "postgresql"


def get_valid_access_token(
    db: Session, connection_id: UUID, *, stale_token: str | None = None
) -> str:
    """Return a currently-valid access token for the connection, refreshing
    (with rotation-safe locking) when needed.

    ``stale_token``: pass the token that just got a 401 — forces a refresh
    even inside the expiry window, unless a peer already rotated past it
    (then the fresh token is returned without another Banno call).

    Raises BankFeedsRefreshError on failure (auth_state persisted for the
    health gate). Never returns a stale token.
    """
    row = db.get(BannoConnection, connection_id)
    if row is None:
        raise BankFeedsAuthError("bank connection not found")
    if row.auth_state == AUTH_DISCONNECTED or not row.refresh_token_enc:
        raise BankFeedsAuthError("bank connection is disconnected — reconnect required")

    institution = db.get(BannoInstitution, row.institution_id)
    if institution is None:
        raise BankFeedsAuthError("institution row missing for connection")
    if institution.fi_host != row.fi_host:
        _persist_auth_state(db, row.id, AUTH_NEEDS_RECONNECT)
        raise BankFeedsAuthError(
            "institution host changed since this connection was made — reconnect required"
        )
    client_secret = _decrypt(institution.client_secret_enc or "")
    if not institution.client_id or not client_secret:
        raise BankFeedsAuthError("institution credentials incomplete")

    def _aware(dt: datetime | None) -> datetime | None:
        # SQLite returns naive datetimes for DateTime(timezone=True) columns.
        if dt is not None and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    now = datetime.now(timezone.utc)
    refresh_expires = _aware(row.refresh_token_expires_at)
    if refresh_expires and refresh_expires <= now:
        _persist_auth_state(db, row.id, AUTH_NEEDS_RECONNECT)
        raise BankFeedsRefreshError("refresh token expired — reconnect required")

    current = _decrypt(row.access_token_enc or "")

    def _fresh_enough(r: BannoConnection) -> bool:
        expires = _aware(r.access_token_expires_at)
        return bool(expires and expires > now + REFRESH_MARGIN)

    # Fast path — token comfortably valid and not the one that just 401'd.
    if _fresh_enough(row) and (stale_token is None or current != stale_token):
        return current

    tenant_id = os.getenv("GDX_TENANT_ID", "") or os.getenv("TENANT_ID", "")
    if _is_postgres(db):
        lock_key = _refresh_lock_key(tenant_id, str(row.institution_id), row.banno_user_id)
        db.execute(select(func.pg_advisory_xact_lock(lock_key)))
        row = db.execute(
            select(BannoConnection)
            .where(BannoConnection.id == connection_id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise BankFeedsAuthError("bank connection vanished during refresh wait")
        # Peer recheck: did the worker we waited on already rotate?
        peer_token = _decrypt(row.access_token_enc or "")
        if _fresh_enough(row) and peer_token != (stale_token or ""):
            log.info("banno_access_token_refreshed_by_peer connection=%s", connection_id)
            return peer_token

    refresh_token = _decrypt(row.refresh_token_enc or "")
    log.info("banno_access_token_refreshing connection=%s host=%s", connection_id, row.fi_host)
    try:
        token_data = refresh_access_token(
            row.fi_host, institution.client_id, client_secret, refresh_token=refresh_token
        )
    except Exception as exc:
        db.rollback()  # releases locks
        failure = str(exc).lower()
        terminal = (
            "invalid_grant" in failure
            or "http 400" in failure
            or "http 401" in failure
            or "http 403" in failure
        )
        new_state = AUTH_NEEDS_RECONNECT if terminal else AUTH_REFRESH_FAILED
        _persist_auth_state(db, connection_id, new_state)
        log.warning(
            "banno_token_refresh_failed connection=%s state=%s", connection_id, new_state
        )
        raise BankFeedsRefreshError("token refresh failed — see auth_state") from exc

    # Persist the ROTATED refresh token in the same commit, before any use.
    _store_token_data(row, token_data, now)
    row.auth_state = AUTH_HEALTHY
    row.updated_at = now
    db.commit()  # releases advisory + FOR UPDATE locks
    log.info("banno_access_token_refreshed connection=%s", connection_id)
    return str(token_data["access_token"])


def _persist_auth_state(db: Session, connection_id: UUID, state: str) -> None:
    try:
        db.execute(
            BannoConnection.__table__.update()
            .where(BannoConnection.id == connection_id)
            .values(auth_state=state, updated_at=datetime.now(timezone.utc))
        )
        db.commit()
    except Exception:  # noqa: BLE001
        log.exception("banno_auth_state_write_failed connection=%s", connection_id)
        db.rollback()
