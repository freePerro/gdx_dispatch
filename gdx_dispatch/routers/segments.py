from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Segment

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/segments", tags=["segments"], dependencies=[Depends(require_module("segments"))])


BUILTIN_SEGMENTS: list[dict[str, Any]] = [
    {
        "id": "at-risk",
        "name": "At Risk",
        "rules": {"field": "last_job_date", "operator": "older_than", "value": "180 days"},
    },
    {
        "id": "high-value",
        "name": "High Value",
        "rules": {"field": "lifetime_value", "operator": "greater_than", "value": 5000},
    },
    {
        "id": "new",
        "name": "New",
        "rules": {"field": "created_at", "operator": "within_last", "value": "30 days"},
    },
    {
        "id": "inactive",
        "name": "Inactive",
        "rules": {"field": "last_job_date", "operator": "older_than", "value": "365 days"},
    },
]
BUILTIN_BY_ID = {segment["id"]: segment for segment in BUILTIN_SEGMENTS}


class SegmentCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=120)
    rules: dict[str, Any]


class SegmentOut(BaseModel):
    id: str
    name: str
    rules: dict[str, Any]
    is_builtin: bool
    matching_customer_count: int | None = None
    created_at: str | None = None


class SegmentListOut(BaseModel):
    items: list[SegmentOut]


class SegmentCustomersOut(BaseModel):
    items: list[dict[str, Any]]
    total: int


def _parse_days(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.search(r"(\d+)", value)
        if match:
            return int(match.group(1))
    raise HTTPException(status_code=422, detail="Invalid rule value for day-based operator")


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        parsed = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(parsed)
        except ValueError:  # silent failure on invalid datetime format is expected for this parser
            log.exception("segment_datetime_parse_failed")
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt
    return None


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _coerce_rules(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            log.exception("segment_rules_json_decode_failed")
            raise HTTPException(status_code=422, detail="Stored segment rules are invalid JSON") from None
        if isinstance(loaded, dict):
            return loaded
    raise HTTPException(status_code=422, detail="Segment rules must be a JSON object")


def _iter_rules(rules: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if "rules" in rules and isinstance(rules["rules"], list):
        match_mode = str(rules.get("match") or "all").lower()
        return match_mode, [rule for rule in rules["rules"] if isinstance(rule, dict)]
    return "all", [rules]


def _rule_match(customer: dict[str, Any], rule: dict[str, Any], now: datetime) -> bool:
    field = str(rule.get("field") or "").strip()
    operator = str(rule.get("operator") or "").strip().lower()
    expected = rule.get("value")
    actual = customer.get(field)

    if operator == "older_than":
        if field not in {"last_job_date", "created_at"}:
            raise HTTPException(status_code=422, detail=f"Unsupported date field '{field}'")
        days = _parse_days(expected)
        dt = _parse_dt(actual)
        if dt is None:
            return True
        return dt <= (now - timedelta(days=days))

    if operator == "within_last":
        if field not in {"last_job_date", "created_at"}:
            raise HTTPException(status_code=422, detail=f"Unsupported date field '{field}'")
        days = _parse_days(expected)
        dt = _parse_dt(actual)
        if dt is None:
            return False
        return dt >= (now - timedelta(days=days))

    if operator == "greater_than":
        left = float(actual or 0)
        right = float(expected or 0)
        return left > right

    if operator == "less_than":
        left = float(actual or 0)
        right = float(expected or 0)
        return left < right

    if operator == "equals":
        return actual == expected

    raise HTTPException(status_code=422, detail=f"Unsupported segment operator '{operator}'")


def _apply_rules(customers: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    match_mode, rule_list = _iter_rules(rules)
    if not rule_list:
        return customers

    matched: list[dict[str, Any]] = []
    for customer in customers:
        outcomes = [_rule_match(customer, rule, now) for rule in rule_list]
        is_match = any(outcomes) if match_mode == "any" else all(outcomes)
        if is_match:
            matched.append(customer)
    return matched


def _customer_stats(db: Session) -> list[dict[str, Any]]:
    # Detect optional columns portably (SQLite uses PRAGMA, PostgreSQL uses information_schema)
    try:
        customer_columns = {
            str(row["column_name"])
            for row in db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'customers'"
                )
            ).mappings().all()
        }
    except Exception:
        logging.getLogger(__name__).exception("_customer_stats caught exception")
        db.rollback()
        customer_columns = {
            str(row["name"])
            for row in db.execute(text("PRAGMA table_info(customers)")).mappings().all()
        }
    has_customer_type = "customer_type" in customer_columns
    has_metadata = "metadata" in customer_columns

    customer_type_select = "c.customer_type AS customer_type," if has_customer_type else "NULL AS customer_type,"
    metadata_select = "c.metadata AS metadata," if has_metadata else "NULL AS metadata,"
    customer_type_group = ", c.customer_type" if has_customer_type else ""
    metadata_group = ", c.metadata" if has_metadata else ""

    rows = db.execute(
        text(
            f"""
            SELECT
                c.id,
                c.name,
                c.email,
                c.phone,
                c.address,
                {customer_type_select}
                {metadata_select}
                c.created_at,
                MAX(j.created_at) AS last_job_date,
                COALESCE(SUM(i.total), 0) AS lifetime_value
            FROM customers c
            LEFT JOIN jobs j
                ON j.customer_id = c.id
               AND j.deleted_at IS NULL
            LEFT JOIN invoices i
               ON i.job_id = j.id
               AND i.deleted_at IS NULL
            WHERE c.deleted_at IS NULL
            GROUP BY c.id, c.name, c.email, c.phone, c.address{customer_type_group}{metadata_group}, c.created_at
            ORDER BY c.created_at DESC
            """
        )
    ).mappings().all()
    payload: list[dict[str, Any]] = []
    for row in rows:
        item = {k: _normalize_value(v) for k, v in dict(row).items()}
        metadata = item.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                log.exception("segment_customer_metadata_parse_failed")
                metadata = {}
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                item.setdefault(str(key), value)
        payload.append(item)
    return payload


def _resolve_segment_or_404(db: Session, segment_id: str) -> dict[str, Any]:
    if segment_id in BUILTIN_BY_ID:
        return {
            "id": segment_id,
            "name": BUILTIN_BY_ID[segment_id]["name"],
            "rules": BUILTIN_BY_ID[segment_id]["rules"],
            "is_builtin": True,
            "created_at": None,
        }

    try:
        segment_uuid = UUID(segment_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Segment not found") from None

    row = db.query(Segment).filter(
        Segment.id == segment_uuid,
        Segment.deleted_at.is_(None),
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Segment not found")

    return {
        "id": str(row.id),
        "name": str(row.name),
        "rules": _coerce_rules(row.rules),
        "is_builtin": False,
        "created_at": _normalize_value(row.created_at),
    }


@router.get("", response_model=SegmentListOut)
async def list_segments(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SegmentListOut:
    custom_rows = db.query(Segment).filter(
        Segment.deleted_at.is_(None),
    ).order_by(Segment.created_at.desc()).all()
    custom_items = [
        SegmentOut(
            id=str(row.id),
            name=str(row.name),
            rules=_coerce_rules(row.rules),
            is_builtin=False,
            created_at=str(_normalize_value(row.created_at)),
        )
        for row in custom_rows
    ]
    builtin_items = [
        SegmentOut(
            id=segment["id"],
            name=segment["name"],
            rules=segment["rules"],
            is_builtin=True,
            created_at=None,
        )
        for segment in BUILTIN_SEGMENTS
    ]
    return SegmentListOut(items=[*builtin_items, *custom_items])


@router.post("", response_model=SegmentOut, status_code=201)
async def create_segment(
    payload: SegmentCreateIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SegmentOut:
    segment_uuid = uuid4()
    segment_id = str(segment_uuid)
    now = datetime.now(UTC)
    created_at = now.isoformat()
    new_segment = Segment(
        id=segment_uuid,
        name=payload.name,
        rules=payload.rules,
        created_at=now,
    )
    db.add(new_segment)
    db.commit()

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="segment_created",
        entity_type="segment",
        entity_id=segment_id,
        details={"name": payload.name, "rules": payload.rules},
    )
    db.commit()

    return SegmentOut(
        id=segment_id,
        name=payload.name,
        rules=payload.rules,
        is_builtin=False,
        created_at=created_at,
    )


@router.get("/{segment_id}", response_model=SegmentOut)
async def get_segment(
    segment_id: str,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SegmentOut:
    segment = _resolve_segment_or_404(db, segment_id)
    matches = _apply_rules(_customer_stats(db), segment["rules"])
    return SegmentOut(
        id=segment["id"],
        name=segment["name"],
        rules=segment["rules"],
        is_builtin=bool(segment["is_builtin"]),
        created_at=segment["created_at"],
        matching_customer_count=len(matches),
    )


@router.get("/{segment_id}/customers", response_model=SegmentCustomersOut)
async def list_segment_customers(
    segment_id: str,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SegmentCustomersOut:
    segment = _resolve_segment_or_404(db, segment_id)
    matches = _apply_rules(_customer_stats(db), segment["rules"])
    return SegmentCustomersOut(items=matches, total=len(matches))


@router.get("/{segment_id}/count", response_model=None)
async def get_segment_count(
    segment_id: str,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    segment = _resolve_segment_or_404(db, segment_id)
    matches = _apply_rules(_customer_stats(db), segment["rules"])
    return {"segment_id": segment["id"], "count": len(matches)}


@router.delete("/{segment_id}", status_code=204)
async def delete_segment(
    segment_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    if segment_id in BUILTIN_BY_ID:
        raise HTTPException(status_code=400, detail="Built-in segments cannot be deleted")

    try:
        segment_uuid = UUID(segment_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Segment not found") from None

    row = db.query(Segment).filter(
        Segment.id == segment_uuid,
        Segment.deleted_at.is_(None),
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Segment not found")

    row.deleted_at = datetime.now(UTC)
    db.commit()

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="segment_deleted",
        entity_type="segment",
        entity_id=segment_id,
        details={"segment_id": segment_id},
    )
    db.commit()

    return Response(status_code=204)
