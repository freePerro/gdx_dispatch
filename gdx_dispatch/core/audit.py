from __future__ import annotations

import hashlib
import inspect
import json
import logging
import time
import weakref
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends
from sqlalchemy import JSON, DateTime, String, event, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid

# Module scope on purpose: ``audit_ready_db`` must declare ``Depends(get_db)``
# itself, so ``app.dependency_overrides[get_db]`` reaches it. Its old wrapper
# called ``get_db()`` inside a function and silently ignored every override
# (GDXA-218). No cycle: core.database imports nothing from gdx_dispatch.
from gdx_dispatch.core.database import get_db


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


class _DeliberateSystemActor(str):
    """A str that equals "system" but is identity-distinguishable from it.

    The stored value must stay "system" — existing rows, queries and the
    activity feed all key on it. But the request-principal fallback in
    ``_log_audit_event_impl`` needs to tell a considered "no human did this"
    apart from a handler that simply failed to resolve its actor, and both
    arrive as the same six characters. Identity (``is``) carries the intent
    that the value cannot.
    """

    __slots__ = ()


#: Deliberate machine attribution. Pass this — not the bare string "system" —
#: when an audit row genuinely has no human actor (scheduled jobs, webhook
#: receivers, automatic side effects). It suppresses the request-principal
#: fallback, is greppable, and lets the attribution gate in
#: tests/test_audit_actor_attribution.py distinguish intent from omission.
SYSTEM_ACTOR = _DeliberateSystemActor("system")


def resolve_audit_actor(candidate: Any = None, request: Any = None) -> str:
    """Actor id for an audit row, from whatever the handler has to hand.

    Handlers bind their auth dependency to wildly different things: a JWT claim
    dict, a ``User`` ORM row, a ``CustomerUser`` (portal endpoints), or nothing
    at all. The generated audit blocks assumed a dict and called ``.get('sub')``
    on it. Against the portal's ``CustomerUser`` that raises AttributeError
    inside the block's own try/except, so the audit row was never written —
    silently, on the payment endpoints.

    Order: the explicit candidate, then the authenticated principal stashed on
    ``request.state.user`` by ``routers/auth/core.py``, then "system".
    """
    # A deliberate machine declaration wins outright — it is a statement that
    # no human performed this, not a failure to look, so the request principal
    # must not override it.
    if candidate is SYSTEM_ACTOR:
        return SYSTEM_ACTOR
    for source in (candidate, _request_user_dict(request)):
        actor = _actor_id_of(source)
        if actor:
            return actor
    return "system"


def _actor_id_of(obj: Any) -> str | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        val = obj.get("sub") or obj.get("user_id") or obj.get("id")
        return str(val) if val else None
    # ORM rows (User, CustomerUser) and anything else with an id.
    val = getattr(obj, "id", None) or getattr(obj, "sub", None)
    return str(val) if val else None


def _request_user_dict(request: Any) -> Any:
    """The principal stashed on the request, under either key.

    ``routers/auth/core.py`` sets ``request.state.user``; service-account auth
    (``core/service_accounts.py``) and the module guards set
    ``request.state.current_user``. core/modules.py already reads both — a
    resolver that reads only one silently leaves every service-account row
    attributed to "system".
    """
    if request is None:
        return None
    state = getattr(request, "state", None)
    if state is None:
        return None
    return getattr(state, "user", None) or getattr(state, "current_user", None)


def _extract_request_actor(request: Any) -> str | None:
    """The authenticated principal for this request, or None.

    ``routers/auth/core.py`` stashes the decoded user on ``request.state.user``
    precisely so audit helpers can find it "without per-route plumbing". A
    batch of generated audit blocks never used it — they sniffed
    ``locals().get('user')`` in handlers whose auth dependency is bound to
    ``_``, so the lookup always missed and every row fell through to the
    literal string "system". On the live tenant that was 1909 of 2251 non-auth
    rows in 30 days: 85% of the audit trail with no actor, silently.

    Anything genuinely running without a request — Celery beat, webhook
    receivers, CLI tools — has no ``request.state.user`` and correctly stays
    "system". The presence of an authenticated principal is the discriminator.
    """
    return _actor_id_of(_request_user_dict(request))


#: Engines whose guard has been OBSERVED installed (or installed by us and
#: committed). Only a success lands here — a failed install used to be added as
#: well, which made one refused DDL at boot permanent for the life of the
#: process (GDXA-352).
_AUDIT_GUARD_INITIALIZED: weakref.WeakSet[Any] = weakref.WeakSet()

#: engine -> ``time.monotonic()`` before which a FAILED install is not retried.
#: Without it, a runtime role that cannot issue the DDL would re-run it, and
#: re-log it, on every single audit write.
_AUDIT_GUARD_RETRY_AT: weakref.WeakKeyDictionary[Any, float] = weakref.WeakKeyDictionary()
_AUDIT_GUARD_RETRY_SECONDS = 300.0

_AUDIT_GUARD_TRIGGERS = ("audit_logs_no_update", "audit_logs_no_delete")

_SQLITE_AUDIT_DDL = (
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
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete
    BEFORE DELETE ON audit_logs
    BEGIN
        SELECT RAISE(ABORT, 'audit_logs is immutable');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_logs_no_update
    BEFORE UPDATE ON audit_logs
    BEGIN
        SELECT RAISE(ABORT, 'audit_logs is immutable');
    END;
    """,
)

# One source of truth for the Postgres guard: ensure_audit_table runs it at
# runtime and migration 107 runs it at upgrade, the way migration 012 shares
# ``modules/ledger/ddl.py`` with the GL trigger tests.
_PG_AUDIT_GUARD_DDL = (
    """
    CREATE OR REPLACE FUNCTION audit_logs_immutable_guard()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'audit_logs is immutable (op=%)', TG_OP
            USING HINT = 'audit rows are append-only; see D45';
    END;
    $$ LANGUAGE plpgsql;
    """,
    "DROP TRIGGER IF EXISTS audit_logs_no_update ON audit_logs",
    """
    CREATE TRIGGER audit_logs_no_update
        BEFORE UPDATE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable_guard();
    """,
    "DROP TRIGGER IF EXISTS audit_logs_no_delete ON audit_logs",
    """
    CREATE TRIGGER audit_logs_no_delete
        BEFORE DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable_guard();
    """,
)

# The function alone is not the guard: a database holding the function with no
# (or disabled) triggers refuses nothing, and the old check stopped at pg_proc.
# ``to_regclass`` returns NULL for a missing table instead of raising, so this
# read cannot abort the caller's transaction.
_PG_AUDIT_GUARD_PRESENT_SQL = """
    SELECT
        EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'audit_logs_immutable_guard')
        AND (
            SELECT count(*) FROM pg_trigger
            WHERE tgrelid = to_regclass('audit_logs')
              AND tgname IN ('audit_logs_no_update', 'audit_logs_no_delete')
              AND NOT tgisinternal
              AND tgenabled <> 'D'
        ) = 2
"""


def install_pg_audit_guard(conn: Any) -> None:
    """Issue the Postgres guard DDL on ``conn``. Idempotent; raises on refusal."""
    for stmt in _PG_AUDIT_GUARD_DDL:
        conn.execute(text(stmt))


def audit_guard_present(conn: Any) -> bool | None:
    """True/False for whether ``audit_logs`` is guarded on this database.

    ``conn`` is a Connection or a Session. None for a dialect with no guard of
    its own here. Read-only.
    """
    dialect = (getattr(conn, "dialect", None) or conn.get_bind().dialect).name
    if dialect == "postgresql":
        return bool(conn.execute(text(_PG_AUDIT_GUARD_PRESENT_SQL)).scalar())
    if dialect == "sqlite":
        names = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE "
                    "(type = 'table' AND name = 'audit_logs') OR "
                    "(type = 'trigger' AND tbl_name = 'audit_logs')"
                )
            )
        }
        return {"audit_logs", *_AUDIT_GUARD_TRIGGERS} <= names
    return None


def _session_has_nothing_open(db: Any) -> bool:
    """True when committing ``db`` could not harden anything of the caller's.

    No transaction begun and nothing added, dirty or deleted. Anything that is
    not a Session answers False — the safe answer.
    """
    in_transaction = getattr(db, "in_transaction", None)
    if in_transaction is None:
        return False
    try:
        return not in_transaction() and not _staged_work(db)
    except Exception:
        return False


def _guard_install_failed(engine: Any, dialect: str, exc: BaseException) -> None:
    """Say so, and back off a refusal. Called from inside the ``except``.

    A privilege refusal (PG ``42501``), or any SQLite failure, will refuse again: back off
    ``_AUDIT_GUARD_RETRY_SECONDS`` so it is not re-run and re-logged on every
    audit write, but do not cache it for the life of the process — a guard
    installed later, by migration 107 or pave, is then picked up (GDXA-352;
    GDXA-350 cached it). Anything else (lock timeout, a concurrent install, a
    dropped connection) is likely transient: no backoff, the next audit write
    retries (GDXA-350).
    """
    log = logging.getLogger(__name__)
    pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
    if dialect == "postgresql" and pgcode != "42501":
        log.warning(
            "audit_guard_install_failed dialect=%s — will retry on the next audit "
            "write; caller's transaction left intact",
            dialect,
            exc_info=True,
        )
        return
    _AUDIT_GUARD_RETRY_AT[engine] = time.monotonic() + _AUDIT_GUARD_RETRY_SECONDS
    log.error(
        "audit_guard_missing dialect=%s — audit_logs has NO database-level "
        "immutability guard on this database (a raw UPDATE/DELETE would succeed); "
        "next attempt in %ds. Install it as the table owner: alembic upgrade head "
        "(migration 107) as a role that can CREATE FUNCTION and CREATE TRIGGER.",
        dialect,
        int(_AUDIT_GUARD_RETRY_SECONDS),
        exc_info=True,
    )


def ensure_audit_table(db: Any) -> None:
    """Ensure audit table and immutability guard exist for the active DB dialect.

    D45 (2026-04-17): the PG branch was missing entirely — tenant DBs on
    live PG had no DB-level guard despite CLAUDE.md claiming "immutable
    audit trail controls verified." A raw UPDATE on audit_logs would
    silently corrupt the row_hash chain with nothing to stop it. The PG
    branch now installs BEFORE UPDATE + BEFORE DELETE triggers; the
    SQLite branch gains the matching UPDATE guard (previously only
    DELETE was blocked).

    GDXA-352 (2026-10-06): it never commits or rolls back work the caller has
    open. The Postgres DDL runs on a connection of its own, and the SQLite DDL
    commits only when the session holds nothing; otherwise it runs inside a
    savepoint and rides the caller's own commit. An engine is marked
    initialized only once the guard is seen in place. A refused install is
    logged at ERROR and retried after ``_AUDIT_GUARD_RETRY_SECONDS``; a
    transient one is retried on the next write (``_guard_install_failed``).
    GDXA-350 had already moved the Postgres DDL off the caller's session.
    """
    bind = getattr(db, "bind", None) or getattr(db, "get_bind", lambda: None)()
    if bind is None:
        return
    engine = getattr(bind, "engine", bind)
    # WeakSet: entry disappears when the engine is GC'd, so a new engine
    # with the same Python id() is never incorrectly treated as initialized.
    if engine in _AUDIT_GUARD_INITIALIZED:
        return
    retry_at = _AUDIT_GUARD_RETRY_AT.get(engine)
    if retry_at is not None and time.monotonic() < retry_at:
        return

    dialect = bind.dialect.name
    if dialect == "sqlite":
        _ensure_sqlite_guard(db, engine)
    elif dialect == "postgresql":
        _ensure_pg_guard(db, engine)
    else:
        _AUDIT_GUARD_INITIALIZED.add(engine)


def _ensure_sqlite_guard(db: Any, engine: Any) -> None:
    # Read BEFORE the first execute: any statement autobegins a transaction.
    nothing_open = _session_has_nothing_open(db)
    if audit_guard_present(db):
        # Seen from an idle session the table and triggers are committed. Seen
        # from inside a transaction they may be our own uncommitted savepoint
        # below, which the caller could still roll back — so do not mark.
        if nothing_open:
            _AUDIT_GUARD_INITIALIZED.add(engine)
        return

    # Not a separate connection, unlike Postgres: an in-memory SQLite engine
    # hands every checkout the SAME DB-API connection (SingletonThreadPool /
    # StaticPool), so a "separate" commit would commit the caller's work too,
    # and on a file database it would wait on the caller's own write lock.
    if not nothing_open:
        # ``begin_nested`` flushes the caller's pending objects first. Do it
        # here, outside the ``try``: a constraint error in the caller's own data
        # is the caller's, not a missing audit guard to log and back off.
        db.flush()
    try:
        if nothing_open:
            for stmt in _SQLITE_AUDIT_DDL:
                db.execute(text(stmt))
            db.commit()  # the transaction the DDL above opened; nothing else in it
            _AUDIT_GUARD_INITIALIZED.add(engine)
        else:
            with db.begin_nested():
                for stmt in _SQLITE_AUDIT_DDL:
                    db.execute(text(stmt))
    except Exception as exc:
        _guard_install_failed(engine, "sqlite", exc)


def _ensure_pg_guard(db: Any, engine: Any) -> None:
    # Table is created by the ORM via metadata.create_all() or by the
    # tenant bootstrap pipeline — don't CREATE TABLE IF NOT EXISTS here
    # because the ORM column set is authoritative. We only install the
    # guard trigger.
    #
    # D97 Phase 1 (2026-04-26): the runtime role may be ``gdx_app`` with
    # NOSUPERUSER NOBYPASSRLS and no CREATE on schema public, which cannot
    # issue the DDL. Migration 107 installs it at upgrade; this is the
    # fallback for a database that skipped it.
    if audit_guard_present(db):  # a text() query: no autoflush of the caller's work
        _AUDIT_GUARD_INITIALIZED.add(engine)
        return

    # Its own connection and transaction (GDXA-350): the caller's staged
    # mutation is neither committed nor rolled back by this. Not a savepoint in
    # the caller's transaction: a successful CREATE TRIGGER would then hold
    # ShareRowExclusiveLock on audit_logs until the caller commits, and every
    # other connection's audit INSERT would wait behind it. lock_timeout because CREATE
    # and DROP TRIGGER want locks on audit_logs that the CALLER's open
    # transaction may already hold (any earlier audit read or insert in it) —
    # without a timeout that is a self-deadlock in one thread.
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '2s'"))
            install_pg_audit_guard(conn)
    except Exception as exc:
        _guard_install_failed(engine, "postgresql", exc)
        return
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

    # Actor resolution. A caller that supplies a real user_id always wins. When
    # it supplies nothing — or the literal "system", which is what the
    # locals()-sniffing blocks produce when their lookup misses — fall back to
    # the authenticated principal on the request. See _extract_request_actor.
    if user_id is not SYSTEM_ACTOR and (not user_id or str(user_id) == "system"):
        request_actor = _extract_request_actor(request)
        if request_actor:
            if str(user_id) == "system":
                # Loud on purpose: a handler that reached here under an
                # authenticated request had a broken actor lookup. The row is
                # now attributed correctly, but the call site is still wrong.
                logging.getLogger(__name__).warning(
                    "audit_actor_recovered_from_request action=%s entity_type=%s "
                    "— call site passed 'system' during an authenticated request",
                    action,
                    entity_type,
                )
            user_id = request_actor
    user_id = str(user_id or "system")
    details = _sanitize_details(details or {})
    ip_address = ip_address or _extract_ip(request)
    request_id = request_id or _extract_request_id(request)

    # JSON-sanitize ONCE, up front: dates/Decimals/UUIDs in details become
    # strings here, exactly as _payload_json hashes them — so the stored
    # JSON is byte-consistent with the hashed representation AND the row
    # insert can no longer fail on an unserializable VALUE. (Exotic dict
    # KEYS and NaN/Infinity floats can still refuse — default=str never
    # applies to keys — same as the hash line always did.) Proven on prod 2026-08-16: an expense PATCH's
    # audit details carried a raw `date`, the JSON column raised
    # StatementError AFTER the mutation had already committed — the user
    # saw a 500 while the change silently persisted UNAUDITED. Both halves
    # are unacceptable: an audit stamp must neither veto nor skip the
    # action it records over a representational issue.
    details = json.loads(_payload_json(details))

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


def audit_ready_db(db: Any = Depends(get_db)) -> Any:
    """A session whose audit table is already initialized, for use as a FastAPI
    dependency: ``db: Session = Depends(audit_ready_db)``.

    ``ensure_audit_table`` used to commit (SQLite) — or, on a Postgres missing
    the bootstrap guard function, roll back — the caller's session the
    **first** time it ran for an engine, hardening or discarding whatever the
    handler had staged. Since GDXA-352 it no longer touches open work (its
    Postgres DDL has its own connection; its SQLite DDL commits only an idle
    session), so this dependency is no longer what keeps the pair atomic.

    It still earns its place: run before the handler stages anything, the
    SQLite install commits on its own and the engine is marked initialized,
    instead of riding a savepoint in the handler's transaction and being
    re-checked on the next write. Every subsequent call for that engine is a
    no-op (see ``_AUDIT_GUARD_INITIALIZED``).
    """
    ensure_audit_table(db)
    return db


def audit_or_rollback(
    db: Any,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor: Any = None,
    request: Any = None,
    details: dict[str, Any] | None = None,
    tenant_id: str | None = None,
) -> None:
    """Record a mutation, or take the mutation down with it.

    For surfaces where an unaudited change is worse than a failed one — plugin
    installs, credential stores, anything that grants or executes code. Call it
    BEFORE ``db.commit()``: the audit row is flushed into the caller's
    transaction, so the change and its trail commit together or not at all.

    Callers whose write already committed (a helper that commits internally)
    must audit *before* that helper instead — after the fact this rollback
    cannot undo the change, and the 500 would be a lie.

    ``tenant_id`` is optional and, when omitted, falls back to
    ``request.state.tenant`` as it always did. Pass it where the caller knows
    the tenant from something other than the request — an API key's own
    ``tenant_id``, a token claim — because the fallback returns None on any
    path the tenant middleware does not cover, and ``routers/activity.py``
    filters the feed on this column, so a NULL row is invisible there
    (GDXA-85).
    """
    from fastapi import HTTPException

    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_actor_id_of(actor),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
    except Exception:
        logging.getLogger(__name__).exception("audit_write_failed action=%s", action)
        try:
            db.rollback()
        except Exception:
            logging.getLogger(__name__).exception("audit_rollback_failed action=%s", action)
        raise HTTPException(status_code=500, detail="audit failure — change rolled back") from None


def _staged_work(db: Any) -> str:
    """A short description of what the caller has pending, or "" if nothing.

    Read-only: touching ``new``/``dirty``/``deleted`` never flushes.
    """
    try:
        counts = [
            (name, len(getattr(db, name, ()) or ()))
            for name in ("new", "dirty", "deleted")
        ]
    except Exception:  # a session-shaped object that is not a Session
        return ""
    return " ".join(f"{name}={n}" for name, n in counts if n)


def audit_best_effort(
    db: Any,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    actor: Any = None,
    request: Any = None,
    details: dict[str, Any] | None = None,
) -> bool:
    """Record a mutation that has ALREADY committed, and never take the caller
    down with a failed trail row. Returns True if the row landed.

    The other half of ``audit_or_rollback``. Use that one where an unaudited
    change is worse than a failed one — it is called BEFORE ``db.commit()`` and
    rolls the change back. Use this one where the change is already durable and
    a 500 would be a lie: after the fact no rollback can undo it, and telling
    the user it failed only makes them do it twice.

    **Calling it.** Pass your own session positionally; everything else is
    keyword-only. It never raises — a failure is logged and reported as a
    ``False`` return, which you are free to ignore. You need nothing else:
    ``ensure_audit_table`` is hoisted inside (see 1 below), so there is no
    need for ``Depends(audit_ready_db)`` or an ``ensure_audit_table`` call of
    your own first.

    **Precondition, load-bearing: the caller has nothing staged.** Either it
    already committed, or it never wrote anything (a GET-side export, a webhook
    branch that only takes a note). This helper COMMITS, so a caller with
    pending work gets that work hardened whether it wanted it or not — measured
    on SQLite 2026-09-25: an uncommitted row that a bare ``close()`` discards
    survives if this runs first. ``audit_ready_db`` does not rescue that case;
    it moves ``ensure_audit_table``'s first-use commit out of the way, not this
    one. A caller that still has pending work wants ``audit_or_rollback``
    before its commit, not this.

    There is deliberately no ``commit=False`` variant. Whether the row is
    already durable when the ``with`` block ends is dialect- AND state-
    dependent — on SQLite, releasing a SAVEPOINT that is itself the outermost
    transaction boundary commits, but the same call inside an enclosing
    transaction (any prior read on the session opens one) does not, and on
    Postgres RELEASE never commits. A flag whose meaning flips with the dialect
    is worse than no flag; the explicit ``commit`` below is what makes the row
    land on both.

    Why it is shaped this way (GDXA-44, 2026-09-25):

    1. ``ensure_audit_table`` runs OUTSIDE the savepoint. It may commit an
       idle SQLite session the first time it runs for an engine; inside the
       savepoint that commit would release the very savepoint meant to contain
       the write. Idempotent — every call after the first successful one for
       an engine is a no-op.
    2. The write is inside ``begin_nested()``. ``log_audit_event_sync`` ends in
       a ``flush()``, and a failed flush DEACTIVATES the session: every later
       ``execute`` or ``commit`` raises ``PendingRollbackError``. Six helpers
       used to catch that exception, log it, and hand the dead session back —
       ``custom_fields.create_definition`` then 500ed reading back a definition
       it had already committed, and the user created it twice.
    3. The rollback in the ``except`` is CONDITIONAL, and that is the whole
       point of having both. An unconditional one would undo mechanism (2)
       entirely: ``begin_nested()`` re-raises after rolling back to the
       savepoint, so a blanket ``db.rollback()`` runs on exactly the failures
       the savepoint just contained, and expires every object the caller still
       holds. ``is_active`` distinguishes them — False only when the
       transaction really was deactivated, which the savepoint prevents.
    4. Why not simply mirror ``audit_or_rollback`` — a bare ``db.rollback()``
       in the ``except``, no savepoint? It is enough to un-poison the session,
       and at these ten sites nothing pending is lost, so on the HTTP contract
       alone the two are indistinguishable. The savepoint earns its place one
       level down: a rollback restores the session by discarding its entire
       identity map, and these handlers read their row back through that same
       session to build the response (``custom_fields._serialize_definition``
       is handed a ``defn`` that a rollback has expired). Containing the
       failure leaves the caller holding exactly what it held before.
       Measured, not argued — see
       ``tests/test_audit_best_effort.py::test_the_savepoint_is_load_bearing_not_decoration``,
       which fails if the savepoint is removed OR the rollback is made
       unconditional.

    This is the shape ``core/payments.py:_audit_money_event`` already proves for
    exactly this case. Proven on SQLite *and* on live Postgres 15, with a real
    ``BEFORE INSERT`` refusal on ``audit_logs`` in both
    (``tests/test_audit_best_effort.py``; the PG arm skips without a reachable
    server, as every PG arm here does).
    """
    log = logging.getLogger(__name__)
    staged = _staged_work(db)
    if staged:
        # Documented preconditions that nothing checks are how this defect class
        # got here. This one is cheap to check, so it is checked.
        log.warning(
            "audit_best_effort_caller_has_pending_work action=%s entity_type=%s staged=%s "
            "— this helper COMMITS, so that pending work is about to be hardened. A caller "
            "with staged work wants audit_or_rollback before its own commit instead.",
            action, entity_type, staged,
        )
    try:
        # Outside the savepoint on purpose — see (1) above.
        ensure_audit_table(db)

        with db.begin_nested():
            log_audit_event_sync(
                db,
                tenant_id=tenant_id,
                user_id=user_id if user_id is not None else _actor_id_of(actor),
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                details=details or {},
                request=request,
            )
        db.commit()
    except Exception:
        log.exception(
            "audit_best_effort_failed action=%s entity_type=%s entity_id=%s — the change "
            "stands and it is NOT in the audit trail; this ERROR is the only record of it.",
            action, entity_type, entity_id,
        )
        # Only when the savepoint could not contain it — see (3) above.
        if not getattr(db, "is_active", True):
            try:
                db.rollback()
            except Exception:
                log.exception("audit_best_effort_rollback_failed action=%s", action)
        return False
    return True


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
