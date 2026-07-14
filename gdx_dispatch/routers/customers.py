from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.core.audit import log_audit_event, log_audit_event_sync
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.cache import cached
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.log_redact import redact_email
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.name_normalize import humanize_name
from gdx_dispatch.models.tenant_models import Customer, Job

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/customers", tags=["customers"], dependencies=[Depends(require_module("customers"))])


class CustomerCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1)
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    customer_type: str | None = "Residential"
    # Sprint 1.0.5 — canonical pricing-engine input. Independent of legacy
    # free-form customer_type; engine reads pricing_class.
    pricing_class: Literal["retail", "contractor", "wholesale"] | None = None
    margin_override_pct: float | None = Field(default=None, ge=0, lt=1)
    # 2026-05-21 audit catch — CustomerFormDialog had been shipping `notes`
    # and `referral_source` since before the extraction; Pydantic was silently
    # dropping both. Accepted now so the dialog's saved-toast isn't a lie.
    # `referral_source` maps to Customer.source (the column has always been
    # there, just unwired); aliased in the request schema so the UI doesn't
    # have to know the column name. max_length=50 matches the underlying
    # column (`tenant_models.Customer.source`) — without it, sqlite-backed
    # tests pass on a 200-char campaign tag while Postgres 500s in prod.
    notes: str | None = None
    referral_source: str | None = Field(default=None, max_length=50)


class CustomerUpdateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1)
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    customer_type: str | None = None
    pricing_class: Literal["retail", "contractor", "wholesale"] | None = None
    margin_override_pct: float | None = Field(default=None, ge=0, lt=1)
    # Sentinel: PATCH can't otherwise distinguish "set to None" from "not set"
    clear_margin_override: bool = False
    notes: str | None = None
    referral_source: str | None = Field(default=None, max_length=50)


class CustomerOut(BaseModel):
    id: str
    name: str
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    customer_type: str | None = None
    pricing_class: str | None = None
    margin_override_pct: float | None = None
    cached_rolling_volume_paid_12mo: float | None = None
    cached_rolling_volume_at: str | None = None
    created_at: str | None = None
    # Sprint customer-multi-location (2026-05-21) — non-deleted location
    # rows for this customer. 0 for single-address customers (the common
    # case). The CustomersView row shows an "N sites" badge when > 1.
    location_count: int = 0
    # Surfaced 2026-05-21 alongside the contract widening — without these on
    # CustomerOut, the dialog would write the field, the round-trip GET would
    # drop it, and the next reopen would show stale data.
    notes: str | None = None
    referral_source: str | None = None


class CustomerListOut(BaseModel):
    items: list[CustomerOut]
    total: int
    page: int
    per_page: int


class CustomerLocationCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str | None = "Service Address"
    address: str = Field(..., min_length=1)
    access_notes: str | None = None
    is_primary: bool = False


class CustomerLocationOut(BaseModel):
    id: str
    customer_id: str
    label: str | None = None
    address: str
    access_notes: str | None = None
    is_primary: bool
    created_at: str | None = None


class CustomerLocationPatchIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str | None = None
    address: str | None = None
    access_notes: str | None = None
    is_primary: bool | None = None


def _normalize_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _customer_dict(row: Any) -> dict[str, Any]:
    """Convert a Customer ORM object or row mapping to a dict."""
    if isinstance(row, Customer):
        return {
            "id": str(row.id),
            "name": row.name,
            "phone": row.phone,
            "email": row.email,
            "address": row.address,
            "customer_type": row.customer_type,
            "pricing_class": row.pricing_class,
            "margin_override_pct": float(row.margin_override_pct) if row.margin_override_pct is not None else None,
            "cached_rolling_volume_paid_12mo": (
                float(row.cached_rolling_volume_paid_12mo)
                if row.cached_rolling_volume_paid_12mo is not None else None
            ),
            "cached_rolling_volume_at": _normalize_datetime(row.cached_rolling_volume_at),
            "created_at": _normalize_datetime(row.created_at),
            "notes": row.notes,
            "referral_source": row.source,
        }
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "phone": row.get("phone"),
        "email": row.get("email"),
        "address": row.get("address"),
        "customer_type": row.get("customer_type"),
        "pricing_class": row.get("pricing_class"),
        "margin_override_pct": float(row["margin_override_pct"]) if row.get("margin_override_pct") is not None else None,
        "cached_rolling_volume_paid_12mo": (
            float(row["cached_rolling_volume_paid_12mo"])
            if row.get("cached_rolling_volume_paid_12mo") is not None else None
        ),
        "cached_rolling_volume_at": _normalize_datetime(row.get("cached_rolling_volume_at")),
        "created_at": _normalize_datetime(row.get("created_at")),
        "notes": row.get("notes"),
        "referral_source": row.get("source"),
    }


def _location_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "customer_id": str(row["customer_id"]),
        "label": row.get("label"),
        "address": row["address"],
        "access_notes": row.get("access_notes"),
        "is_primary": bool(row.get("is_primary", False)),
        "created_at": _normalize_datetime(row.get("created_at")),
    }


def _user_id(user: dict[str, Any]) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


def _tenant_id(request: Request | None) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    return str(tenant.get("id") or "")


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for") if hasattr(request, "headers") else None
    if xff:
        return str(xff).split(",", 1)[0].strip()
    return request.client.host if request.client else None


def _ensure_customer_exists(db: Session, customer_id: str) -> Customer:
    """Return an active Customer ORM object or raise 404."""
    import uuid as _uuid
    try:
        cid = _uuid.UUID(customer_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Customer not found") from None
    customer = db.query(Customer).filter(
        Customer.id == cid,
        Customer.deleted_at.is_(None),
    ).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


@router.get("", response_model=CustomerListOut)
async def list_customers(
    request: Request,
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=1000),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerListOut:
    tenant_id = company_id()
    cache_key = f"customers:q={q or ''}:page={page}:per={per_page}"

    def _fetch() -> dict[str, Any]:
        offset = (page - 1) * per_page
        # S122-9 slice 3: ORM-routed so EncryptedString columns (today only
        # ``address``, ``email``/``phone``/``name`` still plaintext per the
        # search-UX constraint) decrypt cleanly via process_result_value.
        # The pre-S122-9 raw-SQL form rendered ciphertext to the UI for 269
        # customer pages — see ``feedback_research_first_encryption_rollout.md``.
        from sqlalchemy import func as _func, or_  # noqa: PLC0415

        base_filters = [
            Customer.deleted_at.is_(None),
            ~_func.lower(_func.coalesce(Customer.name, "")).like("%(deleted)%"),
        ]
        if q:
            qpat = f"%{q.lower()}%"
            clauses = [
                _func.lower(Customer.name).like(qpat),
                _func.lower(Customer.email).like(qpat),
                _func.lower(Customer.phone).like(qpat),
            ]
            # Phone is stored free-form ("(555) 123-4567"); the at-entry dedup
            # UI queries with digits only ("5551234567"). A plain LIKE on the
            # raw column never matches a formatted number, so also compare a
            # separator-stripped phone. Chained replace() is portable to both
            # sqlite (tests) and Postgres (prod) — regexp_replace is PG-only.
            # /audit feat/daily-ux-improvements caught the silent phone miss.
            q_digits = "".join(ch for ch in q if ch.isdigit())
            if len(q_digits) >= 7:
                stripped_phone = Customer.phone
                for sep in (" ", "-", "(", ")", ".", "+"):
                    stripped_phone = _func.replace(stripped_phone, sep, "")
                clauses.append(stripped_phone.like(f"%{q_digits}%"))
            base_filters.append(or_(*clauses))

        total = (
            db.query(_func.count(Customer.id))
            .filter(*base_filters)
            .scalar()
            or 0
        )
        rows = (
            db.query(Customer)
            .filter(*base_filters)
            .order_by(Customer.created_at.desc())
            .limit(per_page)
            .offset(offset)
            .all()
        )

        # Sprint customer-multi-location — one batched COUNT for the page,
        # not N+1 per customer. Uses IN (expanding bindparam) instead of
        # ANY() so the query runs identically on sqlite-backed test
        # tenants and Postgres prod. /audit 2026-05-21 catch — ANY(:ids)
        # is Postgres-only and would 500 sqlite test fixtures.
        page_ids = [str(r.id) for r in rows]
        location_counts: dict[str, int] = {}
        if page_ids:
            from sqlalchemy import bindparam, text as _text  # noqa: PLC0415
            stmt = _text(
                "SELECT customer_id, COUNT(*) AS n FROM customer_locations "
                "WHERE deleted_at IS NULL AND customer_id IN :ids "
                "GROUP BY customer_id"
            ).bindparams(bindparam("ids", expanding=True))
            count_rows = db.execute(stmt, {"ids": page_ids}).all()
            location_counts = {str(cid): int(n) for cid, n in count_rows}

        items = []
        for c in rows:
            d = _customer_dict(c)
            d["location_count"] = location_counts.get(str(c.id), 0)
            items.append(d)
        return {
            "items": items,
            "total": int(total),
            "page": page,
            "per_page": per_page,
        }

    data = await cached(tenant_id, cache_key, ttl_seconds=30, fetcher=_fetch)
    return CustomerListOut(
        items=[CustomerOut(**item) for item in data["items"]],
        total=data["total"],
        page=data["page"],
        per_page=data["per_page"],
    )


@router.post("", status_code=201, response_model=CustomerOut)
async def create_customer(
    payload: CustomerCreateIn,
    request: Request = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerOut:
    now = datetime.now(timezone.utc)
    from decimal import Decimal as _D

    customer = Customer(
        # humanize_name fixes the "mike wendt" → "Mike Wendt" data hygiene
        # without touching acronyms or already-mixed-case names. See
        # gdx_dispatch/core/name_normalize.py for the conservative rule.
        name=humanize_name((payload.name or "").replace("\x00", "")),
        email=payload.email,
        phone=payload.phone,
        address=payload.address,
        customer_type=payload.customer_type or "Residential",
        pricing_class=payload.pricing_class,
        margin_override_pct=_D(str(payload.margin_override_pct)) if payload.margin_override_pct is not None else None,
        notes=payload.notes,
        source=payload.referral_source,
        company_id=_tenant_id(request),
        created_at=now,
    )

    try:
        db.add(customer)
        db.commit()
        db.refresh(customer)
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("create_customer_failed", extra={"email": redact_email(payload.email)})
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    customer_id = str(customer.id)
    await log_audit_event(
        db=db,
        tenant_id=_tenant_id(request),
        user_id=_user_id(_),
        action="customer_created",
        entity_type="customer",
        entity_id=customer_id,
        details={"name": payload.name},
        ip_address=_client_ip(request),
        request=request,
    )
    db.commit()
    log.info("customer_created", extra={"customer_id": customer_id})

    return CustomerOut(**_customer_dict(customer))


@router.get("/search", response_model=list[CustomerOut])
async def search_customers(
    q: str = Query(..., min_length=1),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CustomerOut]:
    like_q = f"%{q.lower()}%"
    rows = db.execute(
        select(Customer).where(
            Customer.deleted_at.is_(None),
            (
                func.lower(func.coalesce(Customer.name, "")).like(like_q)
                | func.lower(func.coalesce(Customer.email, "")).like(like_q)
                | func.lower(func.coalesce(Customer.phone, "")).like(like_q)
            ),
        ).order_by(Customer.name.asc()).limit(50)
    ).scalars().all()
    return [CustomerOut(**_customer_dict(r)) for r in rows]


@router.get("/{customer_id}", response_model=None)
async def get_customer(
    customer_id: str,
    request: Request = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    customer = _ensure_customer_exists(db, customer_id)
    # Embed jobs so the Jobs tab in CustomerDetailView populates without a
    # second round-trip. Frontend reads `customer.jobs` (line 1022) — when
    # this was missing, the tab silently rendered "No jobs for this customer"
    # even when jobs existed.
    job_rows = db.execute(
        select(Job)
        .where(Job.customer_id == customer.id, Job.deleted_at.is_(None))
        .order_by(Job.scheduled_at.desc().nullslast(), Job.created_at.desc())
        .limit(200)
    ).scalars().all()
    # Canonical display_state for the embedded jobs — the same batched
    # enrichment the /api/jobs list runs, so CustomerDetailView's
    # JobStateChip renders the authoritative label instead of falling back
    # to the raw lifecycle_stage (which reads "scheduled" even for jobs
    # with no appointment date). Local import: first cross-router use of
    # the helper; jobs.py does not import from this module, so no cycle.
    # Degrades to {} on any failure — the customer payload never breaks
    # over a display field (mirrors the helper's own contract).
    try:
        from gdx_dispatch.routers.jobs import _display_state_for_jobs

        ds_map = _display_state_for_jobs(
            db, [(j.id, j.lifecycle_stage) for j in job_rows]
        )
    except Exception:
        log.exception("customer_jobs_display_state_failed")
        ds_map = {}
    jobs = [
        {
            "id": str(j.id),
            "job_number": getattr(j, "job_number", None),
            "title": j.title,
            "status": j.status,
            "lifecycle_stage": j.lifecycle_stage,
            "priority": j.priority,
            "scheduled_at": j.scheduled_at.isoformat() if j.scheduled_at else None,
            "completed_at": j.completed_at.isoformat() if j.completed_at else None,
            "display_state": ds_map.get(str(j.id)),
        }
        for j in job_rows
    ]
    await log_audit_event(
        db=db,
        tenant_id=_tenant_id(request),
        user_id=_user_id(_),
        action="data_accessed",
        entity_type="customer",
        entity_id=customer_id,
        details={"scope": "single_customer"},
        ip_address=_client_ip(request),
        request=request,
    )
    db.commit()
    return {**_customer_dict(customer), "jobs": jobs}


@router.patch("/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: str,
    payload: CustomerUpdateIn,
    request: Request = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerOut:
    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"] is None:
        raise HTTPException(status_code=422, detail="name cannot be null")

    # Sprint 1.0.5 — sentinel handling: clear_margin_override beats any
    # margin_override_pct value (NULL semantics PATCH otherwise can't express).
    clear_override = updates.pop("clear_margin_override", False)
    if "margin_override_pct" in updates and updates["margin_override_pct"] is not None:
        from decimal import Decimal as _D
        updates["margin_override_pct"] = _D(str(updates["margin_override_pct"]))
    if clear_override:
        updates["margin_override_pct"] = None
    # Capture the operator-facing field set BEFORE we rename referral_source
    # → source. The audit trail should reflect what the user actually
    # changed in the UI, not the post-translation column name (2026-05-21
    # audit catch).
    audit_details = dict(updates)
    # referral_source is the UI's name for Customer.source — translate at the
    # boundary so the dialog doesn't have to know the column name.
    if "referral_source" in updates:
        updates["source"] = updates.pop("referral_source")
    # Same humanize pass as POST — keep PATCH consistent so a "mike wendt"
    # → "Mike Wendt" rename through the UI sticks.
    if "name" in updates and isinstance(updates["name"], str):
        updates["name"] = humanize_name(updates["name"])

    customer = _ensure_customer_exists(db, customer_id)

    if updates:
        try:
            for key, value in updates.items():
                setattr(customer, key, value)
            db.commit()
            db.refresh(customer)
        except SQLAlchemyError as exc:
            db.rollback()
            log.exception("update_customer_failed", extra={"customer_id": customer_id})
            raise HTTPException(status_code=500, detail="A database error occurred") from None
        await log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            user_id=_user_id(_),
            action="customer_updated",
            entity_type="customer",
            entity_id=customer_id,
            details=audit_details,
            ip_address=_client_ip(request),
            request=request,
        )
        db.commit()

    return CustomerOut(**_customer_dict(customer))


@router.delete("/{customer_id}", status_code=204)
async def delete_customer(
    customer_id: str,
    request: Request = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    import uuid as _uuid
    try:
        cid = _uuid.UUID(customer_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Customer not found") from None
    customer = db.query(Customer).filter(
        Customer.id == cid,
        Customer.deleted_at.is_(None),
    ).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    customer.deleted_at = datetime.now(timezone.utc)
    db.commit()
    await log_audit_event(
        db=db,
        tenant_id=_tenant_id(request),
        user_id=_user_id(_),
        action="customer_deleted",
        entity_type="customer",
        entity_id=customer_id,
        details={"soft_delete": True},
        ip_address=_client_ip(request),
        request=request,
    )
    await log_audit_event(
        db=db,
        tenant_id=_tenant_id(request),
        user_id=_user_id(_),
        action="data_deleted",
        entity_type="customer",
        entity_id=customer_id,
        details={"gdpr": True},
        ip_address=_client_ip(request),
        request=request,
    )
    db.commit()
    return Response(status_code=204)


@router.get("/{customer_id}/locations", response_model=list[CustomerLocationOut])
async def list_customer_locations(
    customer_id: str,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CustomerLocationOut]:
    _ensure_customer_exists(db, customer_id)
    rows = db.execute(
        text(
            """
            SELECT id, customer_id, label, address, access_notes, is_primary, created_at
            FROM customer_locations
            WHERE customer_id = :customer_id AND deleted_at IS NULL
            ORDER BY created_at ASC
            """
        ),
        {"customer_id": customer_id},
    ).mappings().all()
    return [CustomerLocationOut(**_location_dict(r)) for r in rows]


@router.post("/{customer_id}/locations", status_code=201, response_model=CustomerLocationOut)
async def create_customer_location(
    customer_id: str,
    payload: CustomerLocationCreateIn,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerLocationOut:
    _ensure_customer_exists(db, customer_id)

    location_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        if payload.is_primary:
            db.execute(
                text(
                    """
                    UPDATE customer_locations
                    SET is_primary = 0
                    WHERE customer_id = :customer_id AND deleted_at IS NULL
                    """
                ),
                {"customer_id": customer_id},
            )

        db.execute(
            text(
                """
                INSERT INTO customer_locations
                    (id, company_id, customer_id, label, address, access_notes, is_primary, created_at, deleted_at)
                VALUES
                    (:id, :company_id, :customer_id, :label, :address, :access_notes, :is_primary, :created_at, NULL)
                """
            ),
            {
                "id": location_id,
                "company_id": company_id(),
                "customer_id": customer_id,
                "label": payload.label or "Service Address",
                "address": payload.address,
                "access_notes": payload.access_notes,
                "is_primary": 1 if payload.is_primary else 0,
                "created_at": now,
            },
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("create_customer_location_failed", extra={"customer_id": customer_id})
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    # Defense-in-depth: bind the refetch to the parent customer_id so an
    # attacker with a guessed location_id from a peer tenant can't reach
    # this code path even if the tenant session binding is bypassed.
    row = db.execute(
        text(
            """
            SELECT id, customer_id, label, address, access_notes, is_primary, created_at
            FROM customer_locations
            WHERE id = :location_id AND customer_id = :customer_id
            LIMIT 1
            """
        ),
        {"location_id": location_id, "customer_id": customer_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create location")
    log.info("customer_location_created", extra={"customer_id": customer_id, "location_id": location_id})
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="create_customer_location",
                entity_type="customer_location",
                entity_id=str(customer_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_customer_location_audit_failed')
    return CustomerLocationOut(**_location_dict(row))


@router.patch("/{customer_id}/locations/{location_id}", response_model=CustomerLocationOut)
async def update_customer_location(
    customer_id: str,
    location_id: str,
    payload: CustomerLocationPatchIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerLocationOut:
    """Partial update of a customer service location.

    Closes CustomerDetailView.vue → PATCH /api/customers/{id}/locations/{id}.
    """
    _ensure_customer_exists(db, customer_id)
    existing = db.execute(
        text(
            """
            SELECT id FROM customer_locations
            WHERE id = :location_id AND customer_id = :customer_id AND deleted_at IS NULL
            """
        ),
        {"location_id": location_id, "customer_id": customer_id},
    ).first()
    if not existing:
        raise HTTPException(status_code=404, detail="location not found")

    data = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else payload.dict(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="no fields to update")
    if "address" in data and not (data["address"] or "").strip():
        raise HTTPException(status_code=422, detail="address cannot be empty")

    try:
        # If this location becomes the new primary, unset other primaries
        if data.get("is_primary") is True:
            db.execute(
                text(
                    "UPDATE customer_locations SET is_primary = 0 "
                    "WHERE customer_id = :customer_id AND deleted_at IS NULL AND id != :location_id"
                ),
                {"customer_id": customer_id, "location_id": location_id},
            )

        set_parts = []
        params: dict[str, Any] = {"location_id": location_id}
        for col in ("label", "address", "access_notes"):
            if col in data:
                set_parts.append(f"{col} = :{col}")
                params[col] = data[col]
        if "is_primary" in data:
            set_parts.append("is_primary = :is_primary")
            params["is_primary"] = 1 if data["is_primary"] else 0
        if not set_parts:
            raise HTTPException(status_code=400, detail="no fields to update")
        set_sql = ", ".join(set_parts)
        db.execute(
            text(f"UPDATE customer_locations SET {set_sql} WHERE id = :location_id"),
            params,
        )
        db.commit()
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("update_customer_location_failed", extra={"customer_id": customer_id, "location_id": location_id})
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    # Defense-in-depth: bind the refetch to the parent customer_id so an
    # attacker with a guessed location_id from a peer tenant can't reach
    # this code path even if the tenant session binding is bypassed.
    row = db.execute(
        text(
            """
            SELECT id, customer_id, label, address, access_notes, is_primary, created_at
            FROM customer_locations
            WHERE id = :location_id AND customer_id = :customer_id
            LIMIT 1
            """
        ),
        {"location_id": location_id, "customer_id": customer_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="location not found after update")
    log.info(
        "customer_location_updated",
        extra={"customer_id": customer_id, "location_id": location_id, "fields": list(data.keys())},
    )
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="update_customer_location",
                entity_type="customer_location",
                entity_id=str(customer_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('update_customer_location_audit_failed')
    return CustomerLocationOut(**_location_dict(row))


@router.delete("/{customer_id}/locations/{location_id}")
async def delete_customer_location(
    customer_id: str,
    location_id: str,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Soft-delete a customer location."""
    _ensure_customer_exists(db, customer_id)
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = db.execute(
            text(
                "UPDATE customer_locations SET deleted_at = :now "
                "WHERE id = :location_id AND customer_id = :customer_id AND deleted_at IS NULL"
            ),
            {"location_id": location_id, "customer_id": customer_id, "now": now},
        )
        db.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="location not found")
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("delete_customer_location_failed", extra={"customer_id": customer_id, "location_id": location_id})
        raise HTTPException(status_code=500, detail="A database error occurred") from None
    log.info("customer_location_deleted", extra={"customer_id": customer_id, "location_id": location_id})
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="delete_customer_location",
                entity_type="customer_location",
                entity_id=str(customer_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('delete_customer_location_audit_failed')
    return {"ok": True, "id": location_id}


# ─────────────────────────── Duplicate review / merge ───────────────────────────
#
# Doug's ask (2026-04-13): "we need a spot that those kind of issues can be
# reviewed in the app and fixed in the app." After the Apr 9 rewind, 56 legacy
# name-duplicate groups remain. This gives a UI to review and merge them.

class DuplicateMember(BaseModel):
    id: str
    name: str
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    created_at: str | None = None
    job_count: int = 0
    invoice_count: int = 0
    has_qb_link: bool = False


class DuplicateGroup(BaseModel):
    normalized_name: str
    count: int
    members: list[DuplicateMember]


class DuplicateListOut(BaseModel):
    groups: list[DuplicateGroup]
    total_groups: int


@router.get("/duplicates", response_model=DuplicateListOut)
def list_duplicates(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(200, ge=1, le=1000),
) -> DuplicateListOut:
    """Return groups of customers that share a normalized name.

    Normalization: lowercased, whitespace-collapsed. Groups of size >= 2 only.
    Per member we return job/invoice counts + QB link presence so the reviewer
    has evidence for picking the keep candidate.
    """
    # Find names that appear more than once (active customers only)
    name_rows = db.execute(
        text(
            """
            SELECT LOWER(TRIM(REGEXP_REPLACE(name, '\\s+', ' ', 'g'))) AS norm,
                   COUNT(*) AS n
            FROM customers
            WHERE deleted_at IS NULL
            GROUP BY norm
            HAVING COUNT(*) > 1
            ORDER BY n DESC, norm
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).all()

    if not name_rows:
        return DuplicateListOut(groups=[], total_groups=0)

    norms = [r[0] for r in name_rows]

    # Pull every customer whose normalized name is in the duplicate
    # set. Two-phase to keep the Postgres REGEXP_REPLACE normalization
    # (the hard part) while ORM-loading the full row so `address`
    # (EncryptedString since S122-9 slice 3) decrypts.
    id_norm_rows = db.execute(
        text(
            """
            SELECT id, LOWER(TRIM(REGEXP_REPLACE(name, '\\s+', ' ', 'g'))) AS norm
            FROM customers
            WHERE deleted_at IS NULL
              AND LOWER(TRIM(REGEXP_REPLACE(name, '\\s+', ' ', 'g'))) = ANY(:norms)
            ORDER BY created_at ASC NULLS LAST, id
            """
        ),
        {"norms": norms},
    ).mappings().all()
    import uuid as _uuid  # noqa: PLC0415
    norm_by_id: dict[str, str] = {str(r["id"]): r["norm"] for r in id_norm_rows}
    customer_rows = (
        db.query(Customer)
        .filter(Customer.id.in_([_uuid.UUID(str(r["id"])) for r in id_norm_rows]))
        .all()
    )
    # Preserve the (created_at, id) ordering from the SQL above.
    by_id = {str(c.id): c for c in customer_rows}
    members = [
        {
            "id": r["id"],
            "name": by_id[str(r["id"])].name if str(r["id"]) in by_id else None,
            "phone": by_id[str(r["id"])].phone if str(r["id"]) in by_id else None,
            "email": by_id[str(r["id"])].email if str(r["id"]) in by_id else None,
            "address": by_id[str(r["id"])].address if str(r["id"]) in by_id else None,
            "created_at": by_id[str(r["id"])].created_at if str(r["id"]) in by_id else None,
            "norm": r["norm"],
        }
        for r in id_norm_rows
    ]

    # Job counts per customer. CAST AS TEXT so the same SQL works on tenants
    # where customer_id is UUID and tenants where it's TEXT (Flask-era schema).
    cust_ids = [str(m["id"]) for m in members]
    job_counts: dict[str, int] = {}
    if cust_ids:
        for row in db.execute(
            text("SELECT CAST(customer_id AS TEXT), COUNT(*) FROM jobs WHERE CAST(customer_id AS TEXT) = ANY(:ids) GROUP BY customer_id"),
            {"ids": cust_ids},
        ).all():
            job_counts[str(row[0])] = int(row[1])

    # Invoice counts per customer (invoices table exists; guard with try)
    inv_counts: dict[str, int] = {}
    try:
        if cust_ids:
            for row in db.execute(
                text("SELECT CAST(customer_id AS TEXT), COUNT(*) FROM invoices WHERE CAST(customer_id AS TEXT) = ANY(:ids) GROUP BY customer_id"),
                {"ids": cust_ids},
            ).all():
                inv_counts[str(row[0])] = int(row[1])
    except SQLAlchemyError:
        logging.getLogger(__name__).exception("list_duplicates caught exception")
        db.rollback()

    # QB entity map lookups
    qb_linked: set[str] = set()
    try:
        if cust_ids:
            for row in db.execute(
                text(
                    "SELECT local_id FROM qb_entity_maps "
                    "WHERE entity_type = 'customer' AND local_id = ANY(:ids)"
                ),
                {"ids": cust_ids},
            ).all():
                qb_linked.add(str(row[0]))
    except SQLAlchemyError:
        logging.getLogger(__name__).exception("list_duplicates caught exception")
        db.rollback()

    # Group by normalized name
    groups_map: dict[str, list[DuplicateMember]] = {}
    for m in members:
        cid = str(m["id"])
        dm = DuplicateMember(
            id=cid,
            name=m["name"],
            phone=m.get("phone"),
            email=m.get("email"),
            address=m.get("address"),
            created_at=_normalize_datetime(m.get("created_at")),
            job_count=job_counts.get(cid, 0),
            invoice_count=inv_counts.get(cid, 0),
            has_qb_link=cid in qb_linked,
        )
        groups_map.setdefault(m["norm"], []).append(dm)

    groups = [
        DuplicateGroup(normalized_name=norm, count=len(mems), members=mems)
        for norm, mems in groups_map.items()
    ]
    groups.sort(key=lambda g: (-g.count, g.normalized_name))

    return DuplicateListOut(groups=groups, total_groups=len(groups))


class MergeIn(BaseModel):
    keep_id: str = Field(..., min_length=1)
    merge_ids: list[str] = Field(..., min_length=1)


class MergeOut(BaseModel):
    keep_id: str
    merged_count: int
    rows_updated: dict[str, int]


# Tables whose customer_id column should be rewritten during merge. Discovered
# at startup via information_schema — cached so we don't pay it per-request.
_MERGE_TABLES_CACHE: dict[str, list[tuple[str, str]]] = {}


def _discover_customer_fk_tables(db: Session) -> list[tuple[str, str]]:
    """Return [(table_name, column_name)] for every column that references
    a customer. Matches any column named customer_id, related_customer_id,
    or converted_customer_id. Excludes stripe_customer_id etc. which reference
    Stripe, not our customers table.
    """
    if "tables" in _MERGE_TABLES_CACHE:
        return _MERGE_TABLES_CACHE["tables"]
    rows = db.execute(
        text(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND column_name IN ('customer_id', 'related_customer_id', 'converted_customer_id')
              AND table_name NOT IN ('customers')
            ORDER BY table_name, column_name
            """
        )
    ).all()
    tables = [(str(r[0]), str(r[1])) for r in rows]
    _MERGE_TABLES_CACHE["tables"] = tables
    return tables


@router.post("/merge", response_model=MergeOut)
def merge_customers(
    payload: MergeIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MergeOut:
    """Merge one or more customers into a 'keep' customer.

    For every table that references customers, rewrite the FK to keep_id.
    Then soft-delete the merged customers (deleted_at = now). This is reversible
    via the audit log — we never hard-delete.
    """
    if payload.keep_id in payload.merge_ids:
        raise HTTPException(status_code=400, detail="keep_id cannot appear in merge_ids")

    tenant_id = _tenant_id(request)
    uid = _user_id(user)
    now = datetime.now(timezone.utc)

    # Verify keep customer exists
    keep_row = db.execute(
        text("SELECT id, name FROM customers WHERE id = :id AND deleted_at IS NULL"),
        {"id": payload.keep_id},
    ).first()
    if keep_row is None:
        raise HTTPException(status_code=404, detail="keep customer not found")

    # Verify merge customers exist
    merge_rows = db.execute(
        text("SELECT id, name FROM customers WHERE id = ANY(:ids) AND deleted_at IS NULL"),
        {"ids": payload.merge_ids},
    ).all()
    if len(merge_rows) != len(payload.merge_ids):
        raise HTTPException(status_code=404, detail="one or more merge customers not found")

    rows_updated: dict[str, int] = {}
    try:
        fk_tables = _discover_customer_fk_tables(db)
        for table, column in fk_tables:
            stmt = text(
                f"UPDATE {table} SET {column} = :keep WHERE {column} = ANY(:ids)"
            )
            result = db.execute(stmt, {"keep": payload.keep_id, "ids": payload.merge_ids})
            if result.rowcount:
                rows_updated[f"{table}.{column}"] = int(result.rowcount)

        # Soft-delete the merged customers
        result = db.execute(
            text(
                "UPDATE customers SET deleted_at = :now, updated_at = :now "
                "WHERE id = ANY(:ids) AND deleted_at IS NULL"
            ),
            {"now": now, "ids": payload.merge_ids},
        )
        rows_updated["customers.soft_deleted"] = int(result.rowcount)

        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("merge_customers_failed", extra={"keep_id": payload.keep_id, "merge_ids": payload.merge_ids})
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=uid,
            action="merge_customers",
            entity_type="customer",
            entity_id=str(payload.keep_id),
            details={
                "keep_id": payload.keep_id,
                "merged_ids": payload.merge_ids,
                "rows_updated": rows_updated,
                "keep_name": keep_row[1],
                "merged_names": [r[1] for r in merge_rows],
            },
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("merge_customers_audit_failed")

    log.info(
        "customers_merged",
        extra={"keep_id": payload.keep_id, "merged_count": len(payload.merge_ids)},
    )
    return MergeOut(
        keep_id=payload.keep_id,
        merged_count=len(payload.merge_ids),
        rows_updated=rows_updated,
    )


# ── Route-order fix ─────────────────────────────────────────────────────────
# GET /{customer_id} is declared before literal-path routes like /duplicates
# and /search. FastAPI matches in registration order, so /duplicates was
# captured by /{customer_id} (→ 404 "Customer not found"). Reorder so literal
# paths win.
def _reorder_literal_paths_first() -> None:
    # FastAPI stores routes with the router prefix already applied, so match
    # by path suffix rather than bare literal.
    literal_suffixes = ("/duplicates", "/search", "/merge")
    literal_routes = [
        r for r in router.routes
        if any(getattr(r, "path", "").endswith(s) for s in literal_suffixes)
    ]
    other_routes = [r for r in router.routes if r not in literal_routes]
    router.routes = literal_routes + other_routes


_reorder_literal_paths_first()
