"""
gdx_dispatch/api/public_router.py — Public REST API v1 for GDX (single-tenant,
self-hosted; see the SINGLE-TENANT INVARIANT note in verify_api_key below).

Authentication: X-API-Key header validated against the api_keys table.
All routes require a valid, non-revoked API key.  The key is looked up and
the tenant context is injected onto request.state so the standard get_db()
dependency works transparently.

Response envelope for lists:
    {"data": [...], "meta": {"page": N, "per_page": N, "total": N}}

Response envelope for single items / mutations:
    {"data": {...}}
"""
import contextlib
import logging
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.api_keys import scope_required
from gdx_dispatch.core.audit import audit_or_rollback, ensure_audit_table
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.tenant import company_id, single_tenant
from gdx_dispatch.core.webhooks.models import WebhookEndpoint

# ---------------------------------------------------------------------------
# DB dependency for API-key auth
# ---------------------------------------------------------------------------
#
# Same database as everything else. The auth lookup deliberately does NOT
# ride the `get_db` dependency the data routes use: tests override `get_db`
# with a fixture database that has no api_keys table, and the key check must
# keep reaching the real (or separately-overridden) session.


def get_auth_db() -> Any:
    """Yield a DB session for API-key verification."""
    from gdx_dispatch.core.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API key auth dependency
# ---------------------------------------------------------------------------


async def _require_api_key(
    request: Request,
    auth_db: Annotated[Session, Depends(get_auth_db)],
) -> dict[str, Any]:
    """Validate X-API-Key header and inject tenant context onto request.state.

    Raises HTTP 401 if the header is missing, the key is unknown, or revoked.
    """
    raw_key = (
        request.headers.get("X-API-Key")
        or request.headers.get("x-api-key")
        or ""
    )
    if not raw_key:
        raise HTTPException(status_code=401, detail="X-API-Key header required")

    # Defer import to avoid circular deps at module load time
    from gdx_dispatch.core.api_keys import verify_api_key

    api_key = verify_api_key(auth_db, raw_key)
    if api_key is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    tenant_id = str(api_key.tenant_id)

    # SINGLE-TENANT INVARIANT (was a multi-tenant cross-tenant guard, audit §1).
    #
    # Pre-collapse this compared the API key's tenant against a tenant the
    # middleware resolved from the request host (e.g. tenanta.example.com), to
    # stop a key for tenant A being POSTed to tenant B's subdomain (which would
    # write to B's DB while stamping company_id=A — wrong DB, wrong owner, no FK
    # to catch it). There are no subdomains and no second tenant any more, so
    # that comparison can never meaningfully differ.
    #
    # We do NOT silently drop the check (deleting a security guard defaults to
    # "allow" — the worst failure mode if single-tenancy is ever violated by a
    # restored backup, a bad seed, or a fork that re-adds tenants). Instead we
    # keep the *guarantee* as one fail-closed assertion ("parse, don't
    # validate"): this deployment serves exactly one company, so a key bound to
    # any other company id is rejected rather than honoured. This single
    # tripwire is what lets every downstream caller safely assume company_id().
    if tenant_id != company_id():
        raise HTTPException(
            status_code=403,
            detail="API key does not belong to this single-tenant deployment",
        )

    # Populate request.state.tenant idempotently for any middleware/handler that
    # still reads it (currently only the rate limiter, pending its single-tenant
    # keying redesign). The invariant above guarantees this is the one company,
    # so single_tenant() is authoritative — no per-request control-DB lookup is
    # needed. Only set when the (outermost) TenantMiddleware did not already.
    if not getattr(request.state, "tenant", None):
        request.state.tenant = single_tenant()

    request.state.api_key_tenant_id = tenant_id
    request.state.api_key_scopes = list(api_key.scopes or [])
    request.state.api_key_prefix = api_key.key_prefix

    return {
        "tenant_id": tenant_id,
        "scopes": list(api_key.scopes or []),
        "key_prefix": api_key.key_prefix,
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1", tags=["public-api"])


def _ok(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"data": jsonable_encoder(data)})


def _list_ok(data: list, page: int, per_page: int, total: int) -> JSONResponse:
    return JSONResponse(
        content={
            "data": jsonable_encoder(data),
            "meta": {"page": page, "per_page": per_page, "total": total},
        }
    )


# ---------------------------------------------------------------------------
# Audit trail for API-key writes (invariant #1)
# ---------------------------------------------------------------------------
#
# There is no human on a public-API request: the API key IS the principal, so
# its prefix is the actor. `log_audit_event` treats a falsy — or literally
# "system" — user_id as a broken call site and tries to recover the actor from
# `request.state.user`, which no API-key request has, so the prefix falls back
# to a named constant rather than to nothing.


def _api_key_actor(request: Request) -> tuple[str | None, str, str | None]:
    """(tenant_id, actor, key_prefix) for the key that signed this request.

    All three come off `request.state`, which `_require_api_key` stamped after
    the single-tenant tripwire passed — so tenant_id here is this deployment's
    one company by construction, not a value the caller chose.
    """
    key_prefix = getattr(request.state, "api_key_prefix", None)
    return (
        getattr(request.state, "api_key_tenant_id", None),
        key_prefix or "api_key",
        key_prefix,
    )


def _audit_public_write(
    db: Session,
    request: Request,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Stage the trail row INSIDE the caller's transaction, before its commit.

    This is the canonical shape and every mutation route below uses it: the
    change and its record land together or neither does. Auditing *after* the
    commit is the weaker half of the pair — it exists for a change committed by
    a helper the handler cannot reach into, and it can lose the row (bug class
    #700, and `tools/audit_after_commit_scan.py` is the gate for it). Each of
    these handlers owns its single `db.commit()`, so none of them needs it.

    Two requirements on the caller, both load-bearing:

    * Run `ensure_audit_table(db)` before staging anything — its first run on
      an engine commits, which would otherwise harden a half-finished write.
      It is called as the first statement inside each handler's
      `_write_errors_as_500` body, so a broken session surfaces through that
      handler's own error path.
    * Let `HTTPException` out ahead of the generic database-error translation.
      On a refused row `audit_or_rollback` has already rolled the change back
      and raised "audit failure — change rolled back"; reporting that as the
      generic 500 would name a different failure than the one that happened.
      `_write_errors_as_500` is what satisfies this, for every handler at once.

    The one audit write in this file that does NOT go through here is
    `create_public_landing_lead`. That is deliberate and is the exception, not
    a second convention: a lead from a marketing site must never be lost to a
    trail failure, so it commits first and swallows. Everything reachable with
    an API key belongs here.
    """
    tenant_id, actor, key_prefix = _api_key_actor(request)
    audit_or_rollback(
        db,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        # Explicit, not left to the request fallback: `_extract_tenant_id`
        # reads `request.state.tenant`, and `routers/activity.py` filters the
        # feed on this column, so a row that falls back to None is invisible
        # there.
        tenant_id=tenant_id,
        # `_actor_id_of` reads `.id`/`.sub`/dict keys — a bare prefix string
        # resolves to None and the row would land attributed to "system".
        actor={"user_id": actor},
        request=request,
        # `channel` is what separates these rows from the in-app ones sharing
        # the same action name, so the audit viewer can show both together and
        # still answer "did a person or a key do this?".
        details={"channel": "public_api", "api_key_prefix": key_prefix, **(details or {})},
    )


@contextlib.contextmanager
def _write_errors_as_500(db: Session, route: str) -> Iterator[None]:
    """Translate anything raised in the body into the opaque 500 — nothing more.

    Read that first line literally: *anything raised*, not "a failed write". The
    body is only the pre-commit region of a write, and it is the caller's job to
    keep it that way — `db.commit()` must be the last statement in the block.
    A statement left after the commit is still inside this translation, and the
    500 it produces would name a failure that did not happen: the row is on
    disk, this wrapper's `db.rollback()` is a no-op on an already-committed
    transaction, and the consumer retries into a duplicate row with a duplicate
    `*_created` trail entry (GDXA-145 — three handlers held a `db.refresh()`
    there). Build the response body off the flushed instance *before* the
    commit; `id` and `created_at` are populated by the flush on all three ORM
    models here. `test_every_write_block_ends_at_its_commit` is the guard, and
    it checks position, not semantics — see its docstring for what it cannot see.

    What this does NOT fix, so that nobody reads the class as closed: these
    routes have no idempotency key and no unique constraint on what they insert
    (measured: two identical `POST /api/v1/webhooks` calls produce two endpoints
    for one URL). A connection that dies *during* the COMMIT is still an
    ambiguous write the wrapper answers as 500, and any 500 a consumer retries
    still duplicates the row. Closing that needs an idempotency contract on the
    public API, which is a design decision and not this helper's job.

    Deliberately named for what it does and not for where it is used: all five
    of this file's write handlers wrap their primary write in it, but it audits
    nothing and guarantees nothing about the trail. Invariant #1 stays entirely
    with the caller (`_audit_public_write`, inside this block, before the
    commit) — a name like `_audited_write` would make an unaudited handler
    *read* as compliant, which is exactly the GDXA-85 defect wearing a helper's
    name.

    Both arms are load-bearing:

    * `HTTPException` passes through untouched. On a refused audit row
      `audit_or_rollback` has already rolled the change back and raised
      "audit failure — change rolled back"; reporting that as the generic
      message would name a different failure than the one that happened.
      Dropping this arm turns the three `TestAuditFailureSemantics` tests red —
      measured, not assumed.
    * Anything else is logged against `route` — an opaque 500 with no log left
      an integration failure undebuggable on a box with no Sentry — and then
      answered as that opaque 500, with the rollback suppressed: a rollback on
      a dead session can itself raise, and since the real error is already
      logged, a rollback failure must not replace the 500 with an unhandled
      crash (repo pattern per #751).

    On the success path this wrapper executes no statement of its own — the
    `db.rollback()` above is the only one, and it runs only on the way to a 500.
    So the caller still owns `ensure_audit_table(db)` as the first line of its
    body and its own `db.commit()` as the last, for the reasons
    `_audit_public_write` gives and the reason above. ("Its own", not "its
    single": on a cold engine `ensure_audit_table` commits the table creation
    too, so the block can hold two.)
    """
    try:
        yield
    except HTTPException:
        raise
    except Exception:
        logging.getLogger(__name__).exception("public api %s failed", route)
        with contextlib.suppress(Exception):
            db.rollback()
        raise HTTPException(status_code=500, detail="A database error occurred") from None


def _swallow_post_commit_failure(db: Session, what: str, entity_id: str) -> None:
    """Contain a failed best-effort write that follows a commit.

    Both of `create_public_landing_lead`'s trailing blocks end this way, and the
    containment is the point: the rollback is suppressed because a rollback on a
    session that just failed raises again, and out of an `except` block that
    raise escapes the response the handler already owes for a committed row —
    measured as a bare "Internal Server Error", with nothing logged (GDXA-145).
    `_write_errors_as_500` suppresses its own rollback for the same reason.

    Call it from inside the `except` block: `logging.exception` reads the live
    `sys.exc_info()`, so the traceback is the caller's, not this frame's.

    `what` is a description, not a format string, and deliberately: a caller
    passing a message with two placeholders and one argument gets no exception
    and no log line — `logging` prints "--- Logging error ---" to stderr and
    returns (measured). Owning the format here means the one thing this helper
    exists to leave behind cannot be lost to a caller's typo.
    """
    with contextlib.suppress(Exception):
        db.rollback()
    logging.getLogger(__name__).exception("%s for id=%s", what, entity_id)


class _PageParams:
    """Reusable pagination dependency — FastAPI injects page/per_page as query params."""

    def __init__(
        self,
        page: int = Query(1, ge=1, description="Page number (1-based)"),
        per_page: int = Query(20, ge=1, le=100, description="Results per page (max 100)"),
    ) -> None:
        self.page = page
        self.per_page = per_page
        self.offset = (page - 1) * per_page


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------


class JobCreate(BaseModel):
    title: str
    customer_id: str | None = None
    scheduled_at: datetime | None = None
    status: str = "lead"


class JobUpdate(BaseModel):
    title: str | None = None
    status: str | None = None
    scheduled_at: datetime | None = None


class CustomerCreate(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None


class WebhookCreate(BaseModel):
    url: str
    events: list[str] = []
    secret: str | None = None


class LandingLeadPublicIn(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    message: str | None = None
    source: str | None = None
    referrer: str | None = None
    utm_campaign: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    cf_turnstile_token: str | None = None
    # Honeypot — real humans never fill a hidden field. Bots fill every input.
    # Named `website` because bots target plausible-sounding fields and skip
    # obvious traps like `hp` / `honeypot`.
    website: str | None = None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@router.get("/jobs")
def list_jobs(
    request: Request,
    pg: Annotated[_PageParams, Depends(_PageParams)],
    _auth: Annotated[dict, Depends(_require_api_key)],
    db: Annotated[Session, Depends(get_db)],
    status: Annotated[str | None, Query(description="Filter by lifecycle_stage")] = None,
    date_from: Annotated[datetime | None, Query(description="Filter by created_at >= date_from")] = None,
    date_to: Annotated[datetime | None, Query(description="Filter by created_at <= date_to")] = None,
) -> JSONResponse:
    page, per_page, offset = pg.page, pg.per_page, pg.offset

    conditions = ["deleted_at IS NULL"]
    params: dict[str, Any] = {"limit": per_page, "offset": offset}

    if status:
        conditions.append("lifecycle_stage = :status")
        params["status"] = status
    if date_from:
        conditions.append("created_at >= :date_from")
        params["date_from"] = date_from
    if date_to:
        conditions.append("created_at <= :date_to")
        params["date_to"] = date_to

    where = " AND ".join(conditions)

    try:
        total_row = db.execute(
            text(f"SELECT COUNT(*) AS cnt FROM jobs WHERE {where}"),  # noqa: S608 — WHERE is joined from literal fragments; every filter value is a bound :param
            params,
        ).mappings().first()
        total = int((total_row or {}).get("cnt", 0))

        rows = db.execute(
            text(
                f"""
                SELECT id, title, lifecycle_stage AS status,
                       customer_id, scheduled_at, created_at
                FROM jobs
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """  # noqa: S608 — WHERE is joined from literal fragments; every filter value is a bound :param
            ),
            params,
        ).mappings().all()

        return _list_ok([dict(r) for r in rows], page, per_page, total)
    except Exception:
        logging.getLogger(__name__).exception("public api list_jobs failed")
        raise HTTPException(status_code=500, detail="A database error occurred") from None


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    request: Request,
    _auth: Annotated[dict, Depends(_require_api_key)],
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    try:
        row = db.execute(
            text(
                """
                SELECT id, title, lifecycle_stage AS status,
                       customer_id, scheduled_at, created_at
                FROM jobs
                WHERE id = :job_id
                  AND deleted_at IS NULL
                """
            ),
            {"job_id": job_id},
        ).mappings().first()
    except Exception:
        logging.getLogger(__name__).exception("public api get_job failed")
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return _ok(dict(row))


@router.post("/jobs", status_code=201)
def create_job(
    payload: JobCreate,
    request: Request,
    _auth: Annotated[dict, Depends(_require_api_key)],
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title is required")

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    with _write_errors_as_500(db, "create_job"):
        # First, before the INSERT is staged: its first run on an engine commits.
        ensure_audit_table(db)
        row = db.execute(
            text(
                """
                INSERT INTO jobs (id, title, lifecycle_stage, customer_id, scheduled_at, created_at, job_type)
                VALUES (:id, :title, :status, :customer_id, :scheduled_at, :created_at, 'Service Call')
                RETURNING id, title, lifecycle_stage AS status,
                          customer_id, scheduled_at, created_at
                """
            ),
            {
                "id": job_id,
                "title": title,
                "status": payload.status or "lead",
                "customer_id": payload.customer_id,
                "scheduled_at": payload.scheduled_at,
                "created_at": now,
            },
        ).mappings().first()
        _audit_public_write(
            db,
            request,
            action="job_created",
            entity_type="job",
            entity_id=job_id,
            details={
                "title": title,
                "status": payload.status or "lead",
                "customer_id": payload.customer_id,
            },
        )
        db.commit()

    return _ok(dict(row), status_code=201)


@router.patch("/jobs/{job_id}")
def update_job(
    job_id: str,
    payload: JobUpdate,
    request: Request,
    _auth: Annotated[dict, Depends(_require_api_key)],
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    updates: dict[str, Any] = {}
    if payload.title is not None:
        updates["title"] = payload.title.strip()
    if payload.status is not None:
        updates["lifecycle_stage"] = payload.status
    if payload.scheduled_at is not None:
        updates["scheduled_at"] = payload.scheduled_at

    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    set_clauses = ", ".join(f"{col} = :{col}" for col in updates)
    params = {**updates, "job_id": job_id}

    with _write_errors_as_500(db, "update_job"):
        # First, before the UPDATE is staged: its first run on an engine commits.
        ensure_audit_table(db)
        row = db.execute(
            text(
                f"""
                UPDATE jobs
                   SET {set_clauses}
                 WHERE id = :job_id
                   AND deleted_at IS NULL
                RETURNING id, title, lifecycle_stage AS status,
                          customer_id, scheduled_at, created_at
                """  # noqa: S608 — SET keys are the hardcoded column names above; values are bound
            ),
            params,
        ).mappings().first()
        if row:
            # Only when a row matched. An id that matched nothing (or a
            # soft-deleted job) changed nothing, and a row recording an update
            # that never happened is worse than no row — it is a false entry.
            _audit_public_write(
                db,
                request,
                action="job_updated",
                entity_type="job",
                entity_id=job_id,
                # The columns actually written, not the request body: `status`
                # lands in `lifecycle_stage`, and the trail should say what
                # changed in the table.
                details={"changed": dict(updates)},
            )
        db.commit()

    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return _ok(dict(row))


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


@router.get("/customers")
def list_customers(
    request: Request,
    pg: Annotated[_PageParams, Depends(_PageParams)],
    _auth: Annotated[dict, Depends(_require_api_key)],
    db: Annotated[Session, Depends(get_db)],
    search: Annotated[str | None, Query(description="Search by name (case-insensitive prefix)")] = None,
) -> JSONResponse:
    page, per_page, offset = pg.page, pg.per_page, pg.offset

    # S122-9 slice 3: ORM-routed so EncryptedString columns (address)
    # decrypt cleanly. Pre-S122-9 raw SQL bypassed process_result_value
    # and would render ciphertext to public API consumers.
    from sqlalchemy import func as _func  # noqa: PLC0415

    from gdx_dispatch.models.tenant_models import Customer  # noqa: PLC0415

    filters = [Customer.deleted_at.is_(None)]
    if search:
        filters.append(_func.lower(Customer.name).like(f"%{search.lower()}%"))

    try:
        total = (
            db.query(_func.count(Customer.id)).filter(*filters).scalar() or 0
        )
        rows = (
            db.query(Customer)
            .filter(*filters)
            .order_by(Customer.created_at.desc())
            .limit(per_page)
            .offset(offset)
            .all()
        )
        items = [
            {
                "id": str(c.id),
                "name": c.name,
                "email": c.email,
                "phone": c.phone,
                "address": c.address,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ]
        return _list_ok(items, page, per_page, int(total))
    except Exception:
        logging.getLogger(__name__).exception("public api list_customers failed")
        raise HTTPException(status_code=500, detail="A database error occurred") from None


@router.post("/customers", status_code=201)
def create_customer(
    payload: CustomerCreate,
    request: Request,
    _auth: Annotated[dict, Depends(_require_api_key)],
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    # ORM write routes payload.address through EncryptedString.process_bind_param.
    from gdx_dispatch.models.tenant_models import Customer  # noqa: PLC0415
    # Customer.company_id is NOT NULL in legacy schemas. Tenant plane
    # isolates by connection, so the value is informational only — but
    # we still pull from request.state.tenant explicitly so a missing
    # tenant_context raises 500 loudly rather than silently insert "".
    tenant = getattr(request.state, "tenant", None)
    if not tenant or not tenant.get("id"):
        raise HTTPException(status_code=500, detail="Tenant context missing")
    customer = Customer(
        name=name,
        email=payload.email,
        phone=payload.phone,
        address=payload.address,
        company_id=str(tenant["id"]),
    )
    with _write_errors_as_500(db, "create_customer"):
        # First, before anything is staged: its first run on an engine commits.
        ensure_audit_table(db)
        db.add(customer)
        db.flush()  # assigns customer.id, which the audit row has to name
        _audit_public_write(
            db,
            request,
            action="customer_created",
            entity_type="customer",
            entity_id=str(customer.id),
            # Name only, matching routers/customers.py:create_customer.
            # `details` is not redacted (core.audit only JSON-normalizes it), so
            # email, phone and the encrypted address stay out of the trail.
            details={"name": customer.name},
        )
        # Read the response off the instance while the session is still known
        # good, and BEFORE the commit. Every column named here is populated by
        # the flush above (`id` from default=uuid4, `created_at` from
        # default=utcnow), so none of it needs a re-read — and none of it may
        # take one: a statement after the commit is inside
        # `_write_errors_as_500`, so a connection lost in that window would
        # answer 500 for a customer that exists (GDXA-145). This replaces the
        # `db.refresh(customer)` that used to sit below the commit.
        body = {
            "id": str(customer.id),
            "name": customer.name,
            "email": customer.email,
            "phone": customer.phone,
            "address": customer.address,
            "created_at": customer.created_at.isoformat() if customer.created_at else None,
        }
        db.commit()  # last statement in the block, by contract

    return _ok(body, status_code=201)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


@router.get("/invoices")
def list_invoices(
    request: Request,
    pg: Annotated[_PageParams, Depends(_PageParams)],
    _auth: Annotated[dict, Depends(_require_api_key)],
    db: Annotated[Session, Depends(get_db)],
    status: Annotated[str | None, Query(description="Filter by invoice status")] = None,
    job_id: Annotated[str | None, Query(description="Filter by job_id")] = None,
) -> JSONResponse:
    page, per_page, offset = pg.page, pg.per_page, pg.offset

    conditions = ["deleted_at IS NULL"]
    params: dict[str, Any] = {"limit": per_page, "offset": offset}

    if status:
        conditions.append("status = :status")
        params["status"] = status
    if job_id:
        conditions.append("job_id = :job_id")
        params["job_id"] = job_id

    where = " AND ".join(conditions)

    try:
        total_row = db.execute(
            text(f"SELECT COUNT(*) AS cnt FROM invoices WHERE {where}"),  # noqa: S608 — WHERE is joined from literal fragments; every filter value is a bound :param
            params,
        ).mappings().first()
        total = int((total_row or {}).get("cnt", 0))

        rows = db.execute(
            text(
                f"""
                SELECT id, job_id, invoice_number, total, status, created_at
                FROM invoices
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """  # noqa: S608 — WHERE is joined from literal fragments; every filter value is a bound :param
            ),
            params,
        ).mappings().all()

        return _list_ok([dict(r) for r in rows], page, per_page, total)
    except Exception:
        logging.getLogger(__name__).exception("public api list_invoices failed")
        raise HTTPException(status_code=500, detail="A database error occurred") from None


# ---------------------------------------------------------------------------
# Landing leads (public intake from tenant marketing sites)
# ---------------------------------------------------------------------------


@router.post("/landing-leads", status_code=201)
async def create_public_landing_lead(
    payload: LandingLeadPublicIn,
    request: Request,
    _auth: Annotated[dict, Depends(_require_api_key)],
    db: Annotated[Session, Depends(get_db)],
    _scope: None = scope_required("landing_leads:write"),
) -> JSONResponse:
    # Honeypot — bots fill every input, humans never fill a hidden field.
    # Per industry convention: 201 silently without DB insert. Don't teach the
    # bot what tripped the trap. Audit §5: return a synthetic UUID rather
    # than null so consumers that build URLs from `data.id` don't crash on
    # `/leads/null`.
    if payload.website:
        return JSONResponse(
            status_code=201,
            content={"data": {"id": str(uuid.uuid4()), "status": "new"}},
        )

    from gdx_dispatch.core.turnstile import verify_turnstile  # noqa: PLC0415

    remote_ip = request.client.host if request.client else None
    turnstile_ok, turnstile_errors = await verify_turnstile(
        payload.cf_turnstile_token, remote_ip
    )
    if not turnstile_ok:
        raise HTTPException(
            status_code=400,
            detail={"error": "challenge_failed", "codes": turnstile_errors},
        )

    tenant_id = request.state.api_key_tenant_id
    key_prefix = getattr(request.state, "api_key_prefix", None)

    from gdx_dispatch.models.tenant_models import LandingLead  # noqa: PLC0415

    ll = LandingLead(
        company_id=tenant_id,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        source=payload.source or "website",
        message=payload.message,
        referrer=payload.referrer,
        utm_campaign=payload.utm_campaign,
        utm_source=payload.utm_source,
        utm_medium=payload.utm_medium,
        status="new",
    )
    # Same translation as the four audited handlers, and the fifth copy of it
    # the extraction collapses — the `except HTTPException` arm is inert here
    # (nothing in this body raises one) but costs nothing. What is NOT shared is
    # the audit: this handler's trail row is written after the commit, in the
    # best-effort block below, and that is deliberate — see `_audit_public_write`.
    with _write_errors_as_500(db, "create_public_landing_lead"):
        db.add(ll)
        db.flush()  # assigns ll.id, which the snapshot below and the trail name
        # Everything the rest of this handler needs about the lead, read while
        # the session is still known good. `SessionLocal` leaves
        # `expire_on_commit` at its default True, so each of these reads would
        # otherwise be a SELECT after the commit — one of them inside this block
        # (the `db.refresh(ll)` this replaces), which answered 500 for a lead
        # that was on disk and invited the visitor's form to submit it twice
        # (GDXA-145). The two best-effort blocks below are allowed to lose their
        # own writes; they are not allowed to lose the lead's id.
        lead_id = str(ll.id)
        lead_status = ll.status
        lead_source = ll.source
        lead_campaign = ll.utm_campaign
        db.commit()  # last statement in the block, by contract

    try:
        from gdx_dispatch.core.audit import log_audit_event_sync  # noqa: PLC0415

        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=key_prefix or "api_key",
            action="landing_lead_created",
            entity_type="landing_lead",
            entity_id=lead_id,
            details={
                "source": lead_source,
                "origin": request.headers.get("origin"),
                "ip": remote_ip,
                "turnstile_pass": turnstile_ok,
                "honeypot_pass": True,
                "key_prefix": key_prefix,
                "utm_campaign": lead_campaign,
            },
            request=request,
        )
        # log_audit_event_sync only flushes; the get_db generator's
        # cleanup rolls back uncommitted writes (mirrors leads.py:_audit).
        db.commit()
    except Exception:
        # Audit-log write failure must NOT break the lead insert (which is
        # the user-facing contract). Roll back, log, continue — and see
        # `_swallow_post_commit_failure` for why the rollback is suppressed.
        _swallow_post_commit_failure(db, "landing-lead audit write failed", lead_id)

    # In-app notification — user_id=NULL means visible to every user on this
    # tenant (the topbar badge query joins on `OR user_id IS NULL`). The
    # existing notification-count poll (60s) in `stores/notifications.js` will
    # surface it as a red Badge on the topbar bell icon, and clicking opens
    # the notifications drawer. Failure here is non-fatal — the lead is
    # already committed; we just lose the UX ping.
    try:
        from gdx_dispatch.models.tenant_models import Notification  # noqa: PLC0415

        display_name = (payload.name or "").strip() or (payload.email or "anonymous")
        notif = Notification(
            id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            user_id=None,  # broadcast to all users on this tenant
            title="New lead",
            message=f"{display_name} — {lead_source or 'website'}",
            category="lead",
            is_read=0,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(notif)
        db.commit()
    except Exception:
        _swallow_post_commit_failure(db, "landing-lead notification write failed", lead_id)

    return JSONResponse(
        status_code=201,
        content={"data": {"id": lead_id, "status": lead_status}},
    )


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


@router.post("/webhooks", status_code=201)
def register_webhook(
    payload: WebhookCreate,
    request: Request,
    _auth: Annotated[dict, Depends(_require_api_key)],
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="url is required")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="url must start with http:// or https://")

    # ORM write: routes payload.secret through EncryptedString.process_bind_param
    # (S122-9 slice 2 contract). A raw text("INSERT … VALUES … :secret …")
    # bind here would skip the TypeDecorator and silently land plaintext
    # in the column. Pinned by gdx_dispatch/tools/raw_sql_on_encrypted_columns_scan.py.
    endpoint = WebhookEndpoint(
        url=url,
        events=payload.events or [],
        secret=payload.secret or "",
        is_active=True,
    )
    with _write_errors_as_500(db, "register_webhook"):
        # First, before anything is staged: its first run on an engine commits.
        ensure_audit_table(db)
        db.add(endpoint)
        db.flush()  # assigns endpoint.id, which the audit row has to name
        # Registering an endpoint grants a standing data-egress channel to a URL
        # the key holder chose, with a secret stored beside it. The attack this
        # row defends against: a leaked API key adds an exfiltration target, and
        # the only trace is a row the attacker never had to write. Staged inside
        # the transaction, so an unauditable endpoint is never registered.
        _audit_public_write(
            db,
            request,
            action="webhook_endpoint_registered",
            entity_type="webhook_endpoint",
            entity_id=str(endpoint.id),
            details={
                "url": url,
                "events": list(payload.events or []),
                # Never the secret itself. That one was set is the auditable
                # fact; the value is a credential and `details` is readable in
                # the audit viewer.
                "secret_set": bool(payload.secret),
            },
        )
        # Before the commit, off the flushed instance — see `create_customer`
        # and `_write_errors_as_500`. A 500 for a registration that landed is
        # the worst of the three: the key holder retries and a second endpoint
        # delivers every event to the same URL (GDXA-145).
        body = {
            "id": str(endpoint.id),
            "url": endpoint.url,
            "events": list(endpoint.events or []),
            "active": endpoint.is_active,
            "created_at": endpoint.created_at.isoformat() if endpoint.created_at else None,
        }
        db.commit()  # last statement in the block, by contract

    return _ok(body, status_code=201)


# ---------------------------------------------------------------------------
# Door listings — the feed garagedoorxperts.com/used-doors renders
# ---------------------------------------------------------------------------


@router.get("/listings")
def list_public_listings(
    request: Request,
    _auth: Annotated[dict, Depends(_require_api_key)],
    db: Annotated[Session, Depends(get_db)],
    _scope: None = scope_required("listings:read"),
    listing_type: Annotated[str | None, Query(description="used | in_stock | quick_ship")] = None,
) -> JSONResponse:
    """Published door listings, GDX stock ranked above consignment.

    `published` is filtered in the query, not by the caller — a draft or a
    pending-review door must be unreachable even with a valid key. Ordering is
    computed here rather than in the website template so the rule lives in one
    place and the two cannot drift (plan §7.1).

    Photos are referenced by id only; their bytes come from the unauthenticated
    `/public/door-listings/{id}.jpg` route, which keeps a page of thumbnails
    off this key's 60 req/min budget.
    """
    from gdx_dispatch.modules.door_listings import service as _listing_service
    from gdx_dispatch.modules.door_listings.models import LISTING_TYPES, DoorListing

    if listing_type and listing_type not in LISTING_TYPES:
        raise HTTPException(
            status_code=422, detail=f"listing_type must be one of {list(LISTING_TYPES)}"
        )

    stmt = select(DoorListing).where(
        DoorListing.status == "published",
        DoorListing.deleted_at.is_(None),
    )
    if listing_type:
        stmt = stmt.where(DoorListing.listing_type == listing_type)

    try:
        rows = db.execute(stmt.order_by(*_listing_service.public_ordering())).scalars().all()
        # Serialization is INSIDE the guard on purpose: it lazy-loads the photo
        # relationship, so on a not-yet-migrated environment the 500 would come
        # from here, not from the query above.
        payload = [_listing_service.serialize(r, public=True) for r in rows]
    except Exception:
        # Degrade to "no doors" — the marketing page has a static fallback for
        # exactly this — rather than 500 the whole public API.
        logging.getLogger(__name__).exception("public listings feed failed")
        payload = []

    return _ok(payload)
