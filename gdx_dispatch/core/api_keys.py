"""API Key management for tenant public API access.

Provides:
- APIKey ORM model (control plane table)
- Key generation / hashing helpers
- FastAPI router: GET/POST/DELETE /api/developer/keys
- APIKeyMiddleware: validates X-API-Key header, rate-limits via Redis
- scope_required() dependency factory
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from redis import from_url as redis_from_url
from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# Import at module level so tests can patch gdx_dispatch.core.api_keys.SessionLocal
try:
    from gdx_dispatch.core.database import SessionLocal
except Exception:
    logging.getLogger(__name__).exception("<module> caught exception")
    SessionLocal = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Uses TenantBase since api_keys are tenant-scoped (have tenant_id column)
import contextlib

from gdx_dispatch.core.audit import TenantBase as APIKeyBase  # noqa: E402


def _utcnow() -> datetime:
    return datetime.now(UTC)


class APIKey(APIKeyBase):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_tenant_id", "tenant_id"),
        Index("ix_api_keys_key_hash", "key_hash", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True, native_uuid=False), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True, native_uuid=False), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------


def generate_api_key() -> tuple[str, str, str]:
    """Return (raw_key, key_hash, key_prefix).

    raw_key format: gdx_live_<32 hex chars>
    key_prefix: first 16 chars of raw_key  → "gdx_live_xxxxxxx"
    key_hash:   SHA-256 hex of raw_key
    """
    raw_key = f"gdx_live_{secrets.token_hex(16)}"
    key_prefix = raw_key[:16]
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return raw_key, key_hash, key_prefix


def hash_key(raw_key: str) -> str:
    """Return SHA-256 hex digest of raw_key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def verify_api_key(db: Session, raw_key: str) -> APIKey | None:
    """Look up APIKey by hash; return None if not found, revoked, or expired."""
    key_hash = hash_key(raw_key)
    row = db.query(APIKey).filter(APIKey.key_hash == key_hash).first()
    if row is None:
        return None
    if row.revoked_at is not None:
        return None
    if row.expires_at is not None:
        # SQLite stores naive datetimes; strip tz for comparison if needed
        now = datetime.now(UTC)
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp < now:
            return None
    return row


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class CreateKeyBody(BaseModel):
    name: str
    scopes: list[str] = []
    expires_in_days: int | None = None


class KeyListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    key_prefix: str
    name: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


# ---------------------------------------------------------------------------
# Redis client (lazy singleton)
# ---------------------------------------------------------------------------

_redis_client: Any = None


def _get_redis() -> Any:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
    return _redis_client


# ---------------------------------------------------------------------------
# FastAPI router  (/api/developer/keys)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/developer", tags=["developer"])

VALID_SCOPES = {
    "read:jobs",
    "write:jobs",
    "read:customers",
    "write:customers",
    "landing_leads:write",
}


def _get_db_session():
    # Uses module-level SessionLocal so tests can patch gdx_dispatch.core.api_keys.SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _current_user_dep():
    try:
        from gdx_dispatch.routers.auth import get_current_user

        return Depends(get_current_user)
    except Exception:
        logging.getLogger(__name__).exception("_current_user_dep caught exception")
        async def _fallback() -> dict[str, str]:
            return {}

        return Depends(_fallback)


# We define the dependency at function level to avoid module-load failures.


async def _get_current_user_safe(request: Request) -> dict[str, str]:
    try:
        from fastapi.security import OAuth2PasswordBearer

        from gdx_dispatch.routers.auth import get_current_user

        oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login")
        token = await oauth2(request)
        return await get_current_user(request, token)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Authentication required") from None


@router.get("/keys")
async def list_api_keys(
    request: Request,
    current_user: dict = Depends(_get_current_user_safe),
    db: Session = Depends(_get_db_session),
) -> JSONResponse:
    """List all API keys for the authenticated tenant (prefix only, never full key)."""
    tenant_id_str = current_user.get("tenant_id", "")
    if not tenant_id_str:
        raise HTTPException(status_code=401, detail="tenant_id missing from token")
    try:
        tenant_uuid = UUID(tenant_id_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid tenant_id in token") from None

    try:
        keys = (
            db.query(APIKey)
            .filter(APIKey.tenant_id == tenant_uuid)
            .order_by(APIKey.created_at.desc())
            .all()
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("list_api_keys: api_keys table may not exist")
        with contextlib.suppress(Exception):
            db.rollback()
        return JSONResponse({"data": []})

    def _serialize(k: APIKey) -> dict:
        return {
            "id": str(k.id),
            "key_prefix": k.key_prefix,
            "name": k.name,
            "scopes": k.scopes or [],
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "expires_at": k.expires_at.isoformat() if k.expires_at else None,
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
        }

    return JSONResponse({"data": [_serialize(k) for k in keys]})


@router.post("/keys", status_code=201)
async def create_api_key(
    body: CreateKeyBody,
    request: Request,
    current_user: dict = Depends(_get_current_user_safe),
    db: Session = Depends(_get_db_session),
) -> JSONResponse:
    """Create a new API key. Returns full key ONCE — never shown again."""
    tenant_id_str = current_user.get("tenant_id", "")
    if not tenant_id_str:
        raise HTTPException(status_code=401, detail="tenant_id missing from token")
    try:
        tenant_uuid = UUID(tenant_id_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid tenant_id in token") from None

    # Validate name
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    # Validate scopes
    invalid = set(body.scopes) - VALID_SCOPES
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid scopes: {sorted(invalid)}. Valid: {sorted(VALID_SCOPES)}",
        )

    raw_key, key_hash, key_prefix = generate_api_key()
    expires_at = None
    if body.expires_in_days and body.expires_in_days > 0:
        expires_at = datetime.now(UTC) + timedelta(days=body.expires_in_days)

    api_key = APIKey(
        id=uuid4(),
        tenant_id=tenant_uuid,
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=name,
        scopes=body.scopes,
        created_at=datetime.now(UTC),
        expires_at=expires_at,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    return JSONResponse(
        status_code=201,
        content={
            "key": raw_key,
            "prefix": key_prefix,
            "id": str(api_key.id),
            "name": api_key.name,
            "scopes": api_key.scopes,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )


@router.delete("/keys/{key_id}", status_code=200)
async def revoke_api_key(
    key_id: str,
    request: Request,
    current_user: dict = Depends(_get_current_user_safe),
    db: Session = Depends(_get_db_session),
) -> JSONResponse:
    """Soft-revoke an API key (sets revoked_at). Cannot be undone."""
    tenant_id_str = current_user.get("tenant_id", "")
    if not tenant_id_str:
        raise HTTPException(status_code=401, detail="tenant_id missing from token")
    try:
        tenant_uuid = UUID(tenant_id_str)
        key_uuid = UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid UUID format") from None

    api_key = (
        db.query(APIKey)
        .filter(APIKey.id == key_uuid, APIKey.tenant_id == tenant_uuid)
        .first()
    )
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if api_key.revoked_at is not None:
        return JSONResponse({"ok": True, "message": "Already revoked"})

    api_key.revoked_at = datetime.now(UTC)
    db.commit()
    return JSONResponse({"ok": True, "id": key_id, "revoked_at": api_key.revoked_at.isoformat()})


# ---------------------------------------------------------------------------
# APIKeyMiddleware
# ---------------------------------------------------------------------------


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Validate X-API-Key header, enforce rate limit, set request.state attrs."""

    _BYPASS_PATHS = {"/health", "/docs", "/openapi.json", "/redoc", "/auth/login", "/auth/refresh"}

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        raw_key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
        if not raw_key:
            # No API key — let JWT auth handle the request
            return await call_next(request)

        if request.url.path in self._BYPASS_PATHS:
            return await call_next(request)

        # --- Look up key in control DB ---
        db = SessionLocal()
        try:
            api_key = verify_api_key(db, raw_key)
        finally:
            db.close()

        if api_key is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired API key"},
            )

        # --- Redis rate limit: 60 req/min per key prefix ---
        try:
            redis = _get_redis()
            rl_key = f"ratelimit:apikey:{api_key.key_prefix}"
            count = redis.incr(rl_key)
            if count == 1:
                redis.expire(rl_key, 60)
            if count > 60:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded: 60 requests/minute per API key"},
                )
        except Exception:
            # Redis unavailable — fail open (don't block requests)
            logging.getLogger(__name__).exception("dispatch caught exception")
            pass

        # --- Set request state ---
        request.state.api_key_tenant_id = str(api_key.tenant_id)
        request.state.api_key_scopes = list(api_key.scopes or [])
        # Audit §2: keep state shape in lockstep with public_router._require_api_key
        # so audit attribution (`user_id = request.state.api_key_prefix`) works
        # regardless of which auth path served the request.
        request.state.api_key_prefix = api_key.key_prefix

        # Propagate tenant_id so TenantMiddleware lookup is consistent
        # (api key requests bypass subdomain-based tenant resolution)
        if not getattr(request.state, "tenant", None):
            request.state.tenant = {"id": str(api_key.tenant_id)}

        # --- Best-effort: update last_used_at (synchronous) ---
        # This used to run in a per-request `threading.Thread(daemon=True)`.
        # That unmanaged daemon thread opened its own session and committed; it
        # raced with interpreter / pytest teardown mid-`fdatasync` and
        # segfaulted the process (a torn SQLite/ORM handle touched after
        # shutdown began — a non-deterministic native crash), and under api-key
        # load it spawned unbounded threads. The update is a single indexed
        # UPDATE, and this middleware already does sync DB + redis I/O on this
        # path, so do it inline.
        key_id_val = api_key.id
        upd_db = SessionLocal()
        try:
            k = upd_db.query(APIKey).filter(APIKey.id == key_id_val).first()
            if k:
                k.last_used_at = datetime.now(UTC)
                upd_db.commit()
        except Exception:
            logging.getLogger(__name__).exception("update last_used_at failed")
        finally:
            upd_db.close()

        return await call_next(request)


# ---------------------------------------------------------------------------
# scope_required dependency factory
# ---------------------------------------------------------------------------


def scope_required(scope: str) -> Any:
    """FastAPI dependency factory — raises 403 if the request's API key lacks the required scope."""

    async def _check_scope(request: Request) -> None:
        scopes: list[str] = getattr(request.state, "api_key_scopes", [])
        if scope not in scopes:
            raise HTTPException(
                status_code=403,
                detail=f"Scope '{scope}' required. Your key has: {scopes}",
            )

    # Return the dependency directly so callers can use it as a default value
    # e.g. _scope: None = scope_required("read:jobs")
    return Depends(_check_scope)
