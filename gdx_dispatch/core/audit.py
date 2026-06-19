from __future__ import annotations

import hashlib
import inspect
import json
import weakref
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, String, event, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid


class TenantBase(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class AuditLog(TenantBase):
    __tablename__ = "audit_logs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    # New compliance schema
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False, index=True, default="unknown")
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(), index=True
    )

    # Legacy compatibility columns used throughout existing code/tests
    event_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


@event.listens_for(AuditLog, "before_insert")
def _backfill_legacy_fields(_mapper: Any, _connection: Any, target: AuditLog) -> None:
    if not target.action and target.event_type:
        target.action = target.event_type
    if not target.event_type and target.action:
        target.event_type = target.action
    if not target.user_id and target.actor_id:
        target.user_id = target.actor_id
    if not target.actor_id and target.user_id:
        target.actor_id = target.user_id
    if target.details is None and target.payload is not None:
        target.details = target.payload
    if target.payload is None and target.details is not None:
        target.payload = target.details
    if (not target.row_hash) and target.hash:
        target.row_hash = target.hash
    if (not target.hash) and target.row_hash:
        target.hash = target.row_hash


def _sanitize_details(d: dict) -> dict:
    """Convert UUID objects and other non-JSON-serializable types to strings."""
    from uuid import UUID as _UUID
    result = {}
    for k, v in d.items():
        if isinstance(v, _UUID):
            result[k] = str(v)
        elif isinstance(v, dict):
            result[k] = _sanitize_details(v)
        elif isinstance(v, (list, tuple)):
            result[k] = [str(x) if isinstance(x, _UUID) else x for x in v]
        else:
            result[k] = v
    return result


def _payload_json(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)


def _extract_ip(request: Any) -> str | None:
    if request is None:
        return None
    headers = getattr(request, "headers", {}) or {}
    xff = None
    get_header = getattr(headers, "get", None)
    if callable(get_header):
        xff = get_header("x-forwarded-for") or get_header("X-Forwarded-For")
    elif isinstance(headers, dict):
        xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if xff:
        return str(xff).split(",", 1)[0].strip()
    return getattr(getattr(request, "client", None), "host", None) or getattr(request, "remote_addr", None)


def _extract_request_id(request: Any) -> str | None:
    if request is None:
        return None
    req_id = getattr(getattr(request, "state", None), "request_id", None) or getattr(request, "request_id", None)
    if req_id:
        return str(req_id)
    headers = getattr(request, "headers", {}) or {}
    get_header = getattr(headers, "get", None)
    if callable(get_header):
        return get_header("x-request-id") or get_header("X-Request-ID")
    if isinstance(headers, dict):
        return headers.get("x-request-id") or headers.get("X-Request-ID")
    return None


def _extract_tenant_id(request: Any) -> str | None:
    tenant = getattr(getattr(request, "state", None), "tenant", None) or {}
    return tenant.get("id") if isinstance(tenant, dict) else None  # type: ignore[return-value]


def _extract_impersonation(request: Any) -> tuple[str | None, str | None]:
    """Return (imp_actor_id, imp_purpose) from request.state.user if present.

    Tenant auth (``gdx_dispatch.routers.auth.get_current_user``) surfaces these
    claims from impersonation tokens minted by CC. They live on
    ``request.state.user`` after the auth dependency runs. Returns
    (None, None) for normal user tokens.
    """
    if request is None:
        return None, None
    state = getattr(request, "state", None)
    user = getattr(state, "user", None) if state is not None else None
    if not isinstance(user, dict):
        return None, None
    return user.get("imp_actor_id"), user.get("imp_purpose")


_AUDIT_GUARD_INITIALIZED: weakref.WeakSet[Any] = weakref.WeakSet()


def ensure_audit_table(db: Any) -> None:
    """Ensure audit table and immutability guard exist for the active DB dialect.

    D45 (2026-04-17): the PG branch was missing entirely — tenant DBs on
    live PG had no DB-level guard despite CLAUDE.md claiming "immutable
    audit trail controls verified." A raw UPDATE on audit_logs would
    silently corrupt the row_hash chain with nothing to stop it. The PG
    branch now installs BEFORE UPDATE + BEFORE DELETE triggers; the
    SQLite branch gains the matching UPDATE guard (previously only
    DELETE was blocked).
    """
    bind = getattr(db, "bind", None) or getattr(db, "get_bind", lambda: None)()
    if bind is None:
        return
    engine = getattr(bind, "engine", bind)
    # WeakSet: entry disappears when the engine is GC'd, so a new engine
    # with the same Python id() is never incorrectly treated as initialized.
    if engine in _AUDIT_GUARD_INITIALIZED:
        return

    dialect = bind.dialect.name
    if dialect == "sqlite":
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT,
                    user_id TEXT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    details JSON,
                    ip_address TEXT,
                    request_id TEXT,
                    row_hash TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    event_type TEXT,
                    actor_id TEXT,
                    actor_role TEXT,
                    payload JSON,
                    hash TEXT
                )
                """
            )
        )
        db.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete
                BEFORE DELETE ON audit_logs
                BEGIN
                    SELECT RAISE(ABORT, 'audit_logs is immutable');
                END;
                """
            )
        )
        db.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS audit_logs_no_update
                BEFORE UPDATE ON audit_logs
                BEGIN
                    SELECT RAISE(ABORT, 'audit_logs is immutable');
                END;
                """
            )
        )
        db.commit()
    elif dialect == "postgresql":
        # Table is created by the ORM via metadata.create_all() or by the
        # tenant bootstrap pipeline — don't CREATE TABLE IF NOT EXISTS here
        # because the ORM column set is authoritative. We only install the
        # guard trigger.
        #
        # D97 Phase 1 (2026-04-26): the runtime role is now ``gdx_app`` with
        # NOSUPERUSER NOBYPASSRLS and no CREATE on schema public. The DDL
        # below requires CREATE FUNCTION + CREATE TRIGGER privileges, which
        # gdx_app lacks. The function + triggers are installed once at
        # bootstrap (via the migration step that runs as ``gdx`` superuser).
        # If they already exist, skip the DDL — there's nothing for gdx_app
        # to do.
        guard_present = db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_proc
                    WHERE proname = 'audit_logs_immutable_guard'
                )
                """
            )
        ).scalar()
        if guard_present:
            # Function + triggers were installed during bootstrap; skip DDL.
            _AUDIT_GUARD_INITIALIZED.add(engine)
            return

        try:
            db.execute(text(
                """
                CREATE OR REPLACE FUNCTION audit_logs_immutable_guard()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'audit_logs is immutable (op=%)', TG_OP
                        USING HINT = 'audit rows are append-only; see D45';
                END;
                $$ LANGUAGE plpgsql;
                """
            ))
            db.execute(text("DROP TRIGGER IF EXISTS audit_logs_no_update ON audit_logs"))
            db.execute(text(
                """
                CREATE TRIGGER audit_logs_no_update
                    BEFORE UPDATE ON audit_logs
                    FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable_guard();
                """
            ))
            db.execute(text("DROP TRIGGER IF EXISTS audit_logs_no_delete ON audit_logs"))
            db.execute(text(
                """
                CREATE TRIGGER audit_logs_no_delete
                    BEFORE DELETE ON audit_logs
                    FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable_guard();
                """
            ))
            db.commit()
        except Exception:
            # Privilege denied OR another concurrent caller already installed
            # the guard. Don't crash audit writes on the back of a no-op DDL.
            db.rollback()
    _AUDIT_GUARD_INITIALIZED.add(engine)


def _log_audit_event_impl(db: Any, *args: Any, **kwargs: Any) -> AuditLog:
    """Append-only, hash-chained audit log entry with backward-compatible signature."""
    # New style: log_audit_event(db=..., tenant_id=..., user_id=..., action=..., ...)
    if any(k in kwargs for k in ("action", "tenant_id", "user_id", "details", "ip_address", "request_id")):
        tenant_id = kwargs.get("tenant_id")
        user_id = kwargs.get("user_id")
        action = kwargs.get("action") or kwargs.get("event_type") or "unknown"
        entity_type = kwargs.get("entity_type") or "unknown"
        entity_id = kwargs.get("entity_id")
        details = kwargs.get("details")
        ip_address = kwargs.get("ip_address")
        request_id = kwargs.get("request_id")
        request = kwargs.get("request")
        actor_role = kwargs.get("actor_role")
    else:
        # Legacy style: log_audit_event(db, event_type, actor_id, entity_type, entity_id, payload, ...)
        action = kwargs.get("event_type") or (args[0] if len(args) > 0 else "unknown")
        user_id = kwargs.get("actor_id") or (args[1] if len(args) > 1 else None)
        entity_type = kwargs.get("entity_type") or (args[2] if len(args) > 2 else "unknown")
        entity_id = kwargs.get("entity_id") or (args[3] if len(args) > 3 else None)
        details = kwargs.get("payload") if "payload" in kwargs else (args[4] if len(args) > 4 else {})
        request = kwargs.get("request")
        actor_role = kwargs.get("actor_role")
        tenant_id = kwargs.get("tenant_id")
        ip_address = kwargs.get("ip_address")
        request_id = kwargs.get("request_id")

    ensure_audit_table(db)

    tenant_id = str(tenant_id or _extract_tenant_id(request) or "") or None
    user_id = str(user_id or "system")
    details = _sanitize_details(details or {})
    ip_address = ip_address or _extract_ip(request)
    request_id = request_id or _extract_request_id(request)

    # Phase D cc2-s46: stamp impersonation context onto every audit row
    # written during a request whose JWT carried imp_actor_id. The CC
    # impersonate endpoint mints these tokens; tenant-side auth surfaces
    # the claim into request.state.user. Without this hook every action
    # taken under impersonation looks like the impersonated user — the
    # imp_actor_id stamp is the only forensic trail back to the operator.
    imp_actor_id, imp_purpose = _extract_impersonation(request)
    if imp_actor_id:
        details = dict(details)
        details.setdefault("_impersonation", {
            "actor_id": imp_actor_id,
            "purpose": imp_purpose,
        })

    result = db.execute(select(AuditLog.row_hash).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(1))
    row = result
    if inspect.isawaitable(result):
        raise RuntimeError("Async DB execution is not supported in sync audit logging path")
    prev_hash = str(row.scalar_one_or_none() or "")

    now = utcnow()
    row_data = f"{tenant_id}:{user_id}:{action}:{entity_type}:{entity_id}:{_payload_json(details)}:{request_id}"
    row_hash = hashlib.sha256(f"{prev_hash}{row_data}".encode()).hexdigest()

    entry = AuditLog(
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        details=details,
        ip_address=ip_address,
        request_id=request_id,
        row_hash=row_hash,
        prev_hash=prev_hash,
        created_at=now,
        # Legacy mirror fields
        event_type=action,
        actor_id=user_id,
        actor_role=actor_role,
        payload=details,
        hash=row_hash,
    )
    db.add(entry)
    db.flush()
    return entry


async def log_audit_event(db: Any, *args: Any, **kwargs: Any) -> AuditLog:
    return _log_audit_event_impl(db, *args, **kwargs)


def log_audit_event_sync(db: Any, *args: Any, **kwargs: Any) -> AuditLog:
    return _log_audit_event_impl(db, *args, **kwargs)


def verify_audit_chain(db: Any, entity_type: str | None = None, entity_id: str | None = None) -> bool:
    q = select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    if entity_type is not None:
        q = q.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        q = q.where(AuditLog.entity_id == entity_id)

    rows = db.execute(q).scalars().all()
    if not rows:
        return True

    prev_hash = rows[0].prev_hash or ""
    for row in rows:
        actor = row.user_id or row.actor_id or "system"
        details = row.details if row.details is not None else (row.payload or {})
        action = row.action or row.event_type or "unknown"
        row_data = f"{row.tenant_id}:{actor}:{action}:{row.entity_type}:{row.entity_id}:{_payload_json(details)}:{row.request_id}"
        expected = hashlib.sha256(f"{prev_hash}{row_data}".encode()).hexdigest()
        stored_hash = row.row_hash or row.hash
        if row.prev_hash != prev_hash or stored_hash != expected:
            return False
        prev_hash = stored_hash
    return True
