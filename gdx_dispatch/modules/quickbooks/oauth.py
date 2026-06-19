"""QuickBooks OAuth2 token management.

Stores tokens encrypted via Fernet (same pattern as tenant db_url_enc).
Handles token exchange and refresh using httpx — no SDK dependencies.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
from sqlalchemy import DateTime, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core import pii
from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.models.tenant_models import Base
from gdx_dispatch.modules.quickbooks.client import TOKEN_ENDPOINT, QBAuthError, QBClient

log = logging.getLogger(__name__)


class QBTokenStore(Base):
    """Encrypted OAuth2 token storage — one row per (tenant, realm).

    S122-4 (N1): pre-fix this table was a single global row with UNIQUE on
    realm_id alone. ``select(QBTokenStore).limit(1)`` was used in 4 sites,
    meaning the modular QB module could only serve ONE tenant globally.
    The second tenant to connect QB would overwrite the first.

    S122-4 (B9): ``environment`` column ties tokens to the sandbox/production
    Intuit base URL they were minted against. Switching ``QB_ENVIRONMENT``
    without reconnecting silently used tokens against the wrong host;
    ``get_qb_client`` now refuses to serve mismatched env.
    """
    __tablename__ = "qb_token_store"
    __table_args__ = (
        UniqueConstraint("tenant_id", "realm_id", name="uq_qb_token_tenant_realm"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    realm_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="production")
    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # S122-13: tracks whether the connection is usable. Set by ``get_qb_client``
    # when a refresh fails — pre-fix the next sync attempt would burn another
    # Intuit call + Celery retry trying the same dead refresh_token. With this
    # flag, ``connection_healthy()`` short-circuits sync and the frontend
    # surfaces a "Reconnect QuickBooks" CTA.
    # Values: ``healthy`` (default), ``refresh_failed`` (one failure — will
    # retry next sync), ``needs_reconnect`` (refresh token rejected — user
    # must reconnect; sync paused).
    auth_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="healthy", server_default="healthy",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


def _encrypt(value: str) -> str:
    f = getattr(pii, "_FERNET", None)
    if value and f:
        return f.encrypt(value.encode("utf-8")).decode("utf-8")
    return value


def _decrypt(value: str) -> str:
    # Plaintext passthrough on InvalidToken — mirrors the precedent at
    # ``gdx_dispatch.core.database._decrypt_db_url:102``. Pre-S122-9 rows on prod
    # are plaintext (Intuit tokens start ``RT1-``/``eyJ``, which Fernet
    # treats as invalid). The passthrough keeps QB sync working during
    # the transition window while the re-encrypt tool runs. WARN events
    # are deduped per (call_site, 6-char prefix) per process — see
    # ``pii._emit_passthrough_warning``.
    f = getattr(pii, "_FERNET", None)
    if not value or not f:
        return value
    from cryptography.fernet import InvalidToken  # noqa: PLC0415
    try:
        return f.decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        pii._emit_passthrough_warning("qb_oauth._decrypt", value)
        return value


_REFRESH_LOCK_NAMESPACE = "qb_token_refresh"


def _refresh_lock_key(tenant_id: str, realm_id: str) -> int:
    """Stable 64-bit lock key for ``pg_advisory_xact_lock(bigint)`` derived from
    (namespace, tenant_id, realm_id). Same value across processes + restarts —
    Python's ``hash()`` is salted via PYTHONHASHSEED so it can't be used here.
    """
    digest = hashlib.sha256(
        f"{_REFRESH_LOCK_NAMESPACE}:{tenant_id}:{realm_id}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _is_postgres(db: Session) -> bool:
    """True if the bound Engine is a Postgres dialect. The advisory-lock + FOR
    UPDATE refresh-serialization path only runs on Postgres; SQLite-backed
    tests fall through to the legacy (unlocked) path.
    """
    bind = getattr(db, "bind", None)
    dialect = getattr(bind, "dialect", None)
    return getattr(dialect, "name", None) == "postgresql"


def _basic_auth_header() -> str:
    client_id = os.getenv("QB_CLIENT_ID", "")
    client_secret = os.getenv("QB_CLIENT_SECRET", "")
    return base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("utf-8")


async def exchange_code_for_tokens(code: str) -> dict[str, object]:
    """Exchange an OAuth2 authorization code for access + refresh tokens."""
    redirect_uri = os.getenv("QB_REDIRECT_URI", "")
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={
                "Authorization": f"Basic {_basic_auth_header()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    if not resp.is_success:
        log.error("qb_token_exchange_failed status=%d body=%s", resp.status_code, resp.text[:200])
        raise QBAuthError(f"Token exchange failed: HTTP {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict):
        raise QBAuthError("Token response was not a JSON object")
    return data


async def refresh_access_token(refresh_token: str) -> dict[str, object]:
    """Use a refresh token to get a new access token + refresh token pair."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={
                "Authorization": f"Basic {_basic_auth_header()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    if not resp.is_success:
        log.error("qb_token_refresh_failed status=%d body=%s", resp.status_code, resp.text[:200])
        raise QBAuthError(f"Token refresh failed: HTTP {resp.status_code}")
    return resp.json()


def save_tokens(
    tenant_id: str,
    realm_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int = 3600,
    refresh_expires_in: int = 8726400,
    *,
    db: Session,
    environment: str | None = None,
) -> QBTokenStore:
    """Persist tokens (encrypted) for a specific (tenant, realm) pair.

    S122-4 (N1): tenant_id is now mandatory — the old API persisted into a
    single global row keyed on realm_id alone, so the second tenant to
    connect overwrote the first.

    S122-4 (B9): ``environment`` defaults to ``QB_ENVIRONMENT`` env at write
    time. ``get_qb_client`` refuses to serve tokens minted against a
    different env than the running process.
    """
    if not tenant_id:
        raise QBAuthError("save_tokens requires tenant_id")
    now = datetime.now(timezone.utc)
    env_value = (environment or os.getenv("QB_ENVIRONMENT", "production")).strip().lower()
    row = db.execute(
        select(QBTokenStore).where(
            QBTokenStore.tenant_id == tenant_id,
            QBTokenStore.realm_id == realm_id,
        )
    ).scalar_one_or_none()

    if row is None:
        row = QBTokenStore(
            tenant_id=tenant_id, realm_id=realm_id, environment=env_value,
            created_at=now, updated_at=now,
            access_token_enc="", refresh_token_enc="",
            access_token_expires_at=now, refresh_token_expires_at=now,
        )
        db.add(row)

    row.environment = env_value
    row.access_token_enc = _encrypt(access_token)
    row.refresh_token_enc = _encrypt(refresh_token)
    row.access_token_expires_at = now + timedelta(seconds=int(expires_in))
    row.refresh_token_expires_at = now + timedelta(seconds=int(refresh_expires_in))
    row.updated_at = now
    db.commit()

    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id="system",
        action="qb_tokens_saved",
        entity_type="quickbooks",
        entity_id=realm_id,
        details={"realm_id": realm_id, "tenant_id": tenant_id, "environment": env_value},
    )
    db.commit()
    return row


async def get_qb_client(tenant_id: str, db: Session) -> QBClient:
    """Load tokens for a specific tenant, refresh if needed, return QBClient.

    S122-4 (N1): tenant_id is now mandatory. The previous signature
    ``get_qb_client(db)`` did ``select(QBTokenStore).limit(1)`` — returning
    whichever tenant's tokens happened to be the global row. Cross-tenant
    contamination latent. Callers must thread the tenant_id from
    ``request.state.tenant["id"]`` (routers) or the task argument (celery).

    S122-4 (B9): refuses to return a client when ``QBTokenStore.environment``
    doesn't match the running ``QB_ENVIRONMENT``. Switching envs without
    reconnecting silently routed prod tokens against the sandbox URL etc.

    Caller is responsible for calling ``await client.close()`` or using
    ``async with await get_qb_client(tenant_id, db) as qb:``.
    """
    if not tenant_id:
        raise QBAuthError("get_qb_client requires tenant_id")
    try:
        row = db.execute(
            select(QBTokenStore).where(QBTokenStore.tenant_id == tenant_id)
            .order_by(QBTokenStore.updated_at.desc())
        ).scalar_one_or_none()
    except Exception:
        log.exception("qb_token_store_query_failed tenant=%s", tenant_id)
        db.rollback()
        raise QBAuthError(
            "QuickBooks token store table not found — run migrations or reconnect"
        ) from None
    if row is None:
        raise QBAuthError(f"QuickBooks not connected for tenant {tenant_id}")

    expected_env = (os.getenv("QB_ENVIRONMENT", "production") or "production").strip().lower()
    if row.environment and row.environment != expected_env:
        raise QBAuthError(
            f"QuickBooks tokens for tenant {tenant_id} were minted against environment "
            f"'{row.environment}' but QB_ENVIRONMENT is '{expected_env}'. Reconnect."
        )

    now = datetime.now(timezone.utc)
    if row.refresh_token_expires_at <= now:
        raise QBAuthError("QuickBooks refresh token expired — reconnect required")

    access_token = _decrypt(row.access_token_enc)

    # Fast path — access token is comfortably valid. No refresh, no lock.
    if row.access_token_expires_at > now + timedelta(minutes=5):
        return QBClient(
            access_token=access_token, realm_id=row.realm_id, environment=row.environment,
        )

    # Slow path — refresh is needed.
    #
    # S122-9: Intuit rotates the refresh_token on every refresh call. If two
    # workers (web request + Celery sync, two API replicas, etc.) hit the
    # expiring-soon window at the same time, both call ``refresh_access_token``
    # with the same refresh_token. The second call may invalidate the first
    # worker's session OR fail with ``invalid_grant`` — Intuit's behavior here
    # is "use the newest refresh_token returned" and any older one is a soft
    # error. We serialize per (tenant, realm) with a transaction-scoped
    # advisory lock + SELECT ... FOR UPDATE on the row.
    #
    # SQLite-backed tests don't have advisory locks. We fall through to the
    # legacy (unlocked) refresh, which is fine because tests run a single
    # event loop in a single process.
    realm_id = row.realm_id
    if _is_postgres(db):
        lock_key = _refresh_lock_key(tenant_id, realm_id)
        # Blocking acquire — second caller waits for first to finish refreshing.
        db.execute(select(func.pg_advisory_xact_lock(lock_key)))

        # Re-read inside the lock with row-level FOR UPDATE. Belt-and-braces:
        # advisory lock alone would serialize correctly, but FOR UPDATE blocks
        # any other transaction trying to mutate the row even if it bypassed
        # the advisory lock (e.g. via the legacy router).
        row = db.execute(
            select(QBTokenStore).where(
                QBTokenStore.tenant_id == tenant_id,
                QBTokenStore.realm_id == realm_id,
            ).with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise QBAuthError(
                f"QuickBooks token row vanished for tenant {tenant_id} during refresh wait"
            )

        # Double-check: did the worker we were waiting on already refresh?
        # ``access_token_expires_at`` was updated by their commit; we just need
        # the fresh access_token, no Intuit call.
        if row.access_token_expires_at > now + timedelta(minutes=5):
            log.info("qb_access_token_refreshed_by_peer tenant=%s realm=%s", tenant_id, realm_id)
            return QBClient(
                access_token=_decrypt(row.access_token_enc),
                realm_id=row.realm_id,
                environment=row.environment,
            )

    # We are the refresher.
    refresh_token = _decrypt(row.refresh_token_enc)
    log.info("qb_access_token_expiring tenant=%s realm=%s, refreshing", tenant_id, realm_id)
    try:
        token_data = await refresh_access_token(refresh_token)
        access_token = str(token_data.get("access_token", ""))
        new_refresh = str(token_data.get("refresh_token", "")) or refresh_token

        row.access_token_enc = _encrypt(access_token)
        row.refresh_token_enc = _encrypt(new_refresh)
        row.access_token_expires_at = now + timedelta(
            seconds=int(token_data.get("expires_in", 3600))
        )
        row.refresh_token_expires_at = now + timedelta(
            seconds=int(token_data.get("x_refresh_token_expires_in", 8726400))
        )
        row.updated_at = now
        # S122-13: clear any previous failure state on successful refresh.
        row.auth_state = "healthy"
        db.commit()  # Releases advisory lock + row FOR UPDATE lock.
        log.info("qb_access_token_refreshed tenant=%s realm=%s", tenant_id, realm_id)
    except Exception as exc:
        log.exception(
            "qb_token_refresh_failed tenant=%s realm=%s, using existing token",
            tenant_id, realm_id,
        )
        db.rollback()  # Releases locks; preserves existing row state.

        # S122-13: classify the failure and persist auth_state so
        # ``connection_healthy()`` can short-circuit future sync attempts
        # against a dead refresh_token. ``invalid_grant`` is the Intuit
        # response for "refresh token rejected — user must reconnect" —
        # treat any 4xx OAuth error from refresh_access_token as terminal
        # (no point retrying with the same dead token), and any other
        # exception class (network, 5xx, timeout) as transient.
        try:
            failure_str = str(exc).lower()
            is_terminal = (
                "invalid_grant" in failure_str
                or "http 400" in failure_str
                or "http 401" in failure_str
                or "http 403" in failure_str
            )
            new_state = "needs_reconnect" if is_terminal else "refresh_failed"
            db.execute(
                QBTokenStore.__table__.update()
                .where(QBTokenStore.tenant_id == tenant_id, QBTokenStore.realm_id == realm_id)
                .values(auth_state=new_state, updated_at=now)
            )
            db.commit()
        except Exception:
            log.exception("qb_token_auth_state_write_failed tenant=%s", tenant_id)
            db.rollback()

        # ``access_token`` still holds the pre-refresh value decrypted above.

    return QBClient(access_token=access_token, realm_id=row.realm_id, environment=row.environment)


def connection_healthy(tenant_id: str, db: Session) -> bool:
    """S122-13: True iff the tenant's QB connection is in ``healthy`` auth_state.

    Celery sync tasks call this before pulling QBClient. When False, the
    task no-ops with a log line instead of burning Intuit calls + retries
    against a dead refresh_token. The frontend surfaces a "Reconnect
    QuickBooks" CTA when /api/qb/status returns ``auth_state != 'healthy'``.
    """
    if not tenant_id:
        return False
    try:
        row = db.execute(
            select(QBTokenStore.auth_state).where(
                QBTokenStore.tenant_id == tenant_id,
            ).order_by(QBTokenStore.updated_at.desc())
        ).first()
    except Exception:
        log.exception("connection_healthy_query_failed tenant=%s", tenant_id)
        db.rollback()
        return False
    if row is None:
        return False
    return str(row[0] or "healthy") == "healthy"
