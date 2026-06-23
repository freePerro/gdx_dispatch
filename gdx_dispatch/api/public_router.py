"""
gdx_dispatch/api/public_router.py — Public REST API v1 for GDX multi-tenant SaaS.

Authentication: X-API-Key header validated against the control-plane api_keys table.
All routes require a valid, non-revoked API key.  The key is looked up in the
control DB and the tenant context is injected onto request.state so the
standard get_db() dependency works transparently.

Response envelope for lists:
    {"data": [...], "meta": {"page": N, "per_page": N, "total": N}}

Response envelope for single items / mutations:
    {"data": {...}}
"""
import os
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.api_keys import scope_required
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.tenant import company_id, single_tenant
from gdx_dispatch.core.webhooks.models import WebhookEndpoint

# ---------------------------------------------------------------------------
# Control-plane DB dependency for API-key auth
# ---------------------------------------------------------------------------
#
# The api_keys table lives in the control plane. In production the control and
# tenant databases are the same physical DB, so a plain SessionLocal() session
# resolves api_keys fine. But the auth lookup MUST NOT ride the same get_db
# dependency the data routes use: tests (and any future split-DB deployment)
# override get_db to point at the tenant database, which has no api_keys table.
# Keeping a distinct dependency lets the auth path always reach the control DB
# (or its test double) independently of the tenant-data override.


def get_control_db() -> Any:
    """Yield a control-plane DB session for API-key verification."""
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
    control_db: Annotated[Session, Depends(get_control_db)],
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

    api_key = verify_api_key(control_db, raw_key)
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
            text(f"SELECT COUNT(*) AS cnt FROM jobs WHERE {where}"),
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
                """
            ),
            params,
        ).mappings().all()

        return _list_ok([dict(r) for r in rows], page, per_page, total)
    except Exception as exc:
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
    except Exception as exc:
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

    try:
        row = db.execute(
            text(
                """
                INSERT INTO jobs (id, title, lifecycle_stage, customer_id, scheduled_at, created_at)
                VALUES (:id, :title, :status, :customer_id, :scheduled_at, :created_at)
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
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="A database error occurred") from None

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

    try:
        row = db.execute(
            text(
                f"""
                UPDATE jobs
                   SET {set_clauses}
                 WHERE id = :job_id
                   AND deleted_at IS NULL
                RETURNING id, title, lifecycle_stage AS status,
                          customer_id, scheduled_at, created_at
                """
            ),
            params,
        ).mappings().first()
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="A database error occurred") from None

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
    except Exception as exc:
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
    try:
        db.add(customer)
        db.commit()
        db.refresh(customer)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    return _ok(
        {
            "id": str(customer.id),
            "name": customer.name,
            "email": customer.email,
            "phone": customer.phone,
            "address": customer.address,
            "created_at": customer.created_at.isoformat() if customer.created_at else None,
        },
        status_code=201,
    )


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
            text(f"SELECT COUNT(*) AS cnt FROM invoices WHERE {where}"),
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
                """
            ),
            params,
        ).mappings().all()

        return _list_ok([dict(r) for r in rows], page, per_page, total)
    except Exception as exc:
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
    try:
        db.add(ll)
        db.commit()
        db.refresh(ll)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    try:
        from gdx_dispatch.core.audit import log_audit_event_sync  # noqa: PLC0415

        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=key_prefix or "api_key",
            action="landing_lead_created",
            entity_type="landing_lead",
            entity_id=str(ll.id),
            details={
                "source": ll.source,
                "origin": request.headers.get("origin"),
                "ip": remote_ip,
                "turnstile_pass": turnstile_ok,
                "honeypot_pass": True,
                "key_prefix": key_prefix,
                "utm_campaign": ll.utm_campaign,
            },
            request=request,
        )
        # log_audit_event_sync only flushes; the get_db generator's
        # cleanup rolls back uncommitted writes (mirrors leads.py:_audit).
        db.commit()
    except Exception:
        # Audit-log write failure must NOT break the lead insert (which is
        # the user-facing contract). Log and continue.
        db.rollback()
        import logging  # noqa: PLC0415
        logging.getLogger(__name__).exception(
            "landing-lead audit write failed for id=%s", ll.id
        )

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
            message=f"{display_name} — {ll.source or 'website'}",
            category="lead",
            is_read=0,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(notif)
        db.commit()
    except Exception:
        db.rollback()
        import logging  # noqa: PLC0415
        logging.getLogger(__name__).exception(
            "landing-lead notification write failed for id=%s", ll.id
        )

    return JSONResponse(
        status_code=201,
        content={"data": {"id": str(ll.id), "status": ll.status}},
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
    try:
        db.add(endpoint)
        db.commit()
        db.refresh(endpoint)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    return _ok(
        {
            "id": str(endpoint.id),
            "url": endpoint.url,
            "events": list(endpoint.events or []),
            "active": endpoint.is_active,
            "created_at": endpoint.created_at.isoformat() if endpoint.created_at else None,
        },
        status_code=201,
    )
