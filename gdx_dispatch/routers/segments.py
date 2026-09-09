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


class SegmentPatch(BaseModel):
    """Partial update. Only what is sent is written.

    The Vue used to send ``{name, criteria, tags}`` to a PATCH nothing served
    (405), and the same shape to POST, where ``rules`` is required (422) — so
    neither create nor edit has ever worked from the UI. The columns behind
    ``criteria`` and ``tags`` do not exist; the segments table is
    ``id, name, rules, created_at, deleted_at``. The editor now speaks
    ``rules``. See #455.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    rules: dict[str, Any] | None = None


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
        # Signed on purpose: the old pattern was `(\d+)`, so "-30 days"
        # parsed as 30 and slipped past the "at least 1 day" gate.
        match = re.search(r"(-?\d+)", value)
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


def _as_number(value: Any) -> float | None:
    """Float, or None when the value is not a number at all.

    ``None`` becomes 0.0 on purpose: a customer with no invoices has a
    lifetime value of zero, which is how ``float(actual or 0)`` treated it
    before, and segment membership must not change under this refactor.
    """
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
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


def _safe_rules(raw: Any) -> dict[str, Any]:
    """``_coerce_rules`` for list rendering, where one bad row must not 422
    the whole library. The segment still shows up, with rules the UI renders
    as "—", instead of taking the Segments page down with it."""
    try:
        return _coerce_rules(raw)
    except HTTPException:
        return {}


def _iter_rules(rules: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if "rules" in rules and isinstance(rules["rules"], list):
        match_mode = str(rules.get("match") or "all").lower()
        return match_mode, [rule for rule in rules["rules"] if isinstance(rule, dict)]
    return "all", [rules]


DATE_FIELDS = {"last_job_date", "created_at"}
# Fields a >/< comparison can be trusted on. `equals` stays open to any
# field (metadata keys are flattened onto the customer row), but ordering
# a text column by value is never what the author meant.
NUMERIC_FIELDS = {"lifetime_value"}
DATE_OPERATORS = {"older_than", "within_last"}
NUMERIC_OPERATORS = {"greater_than", "less_than"}
SUPPORTED_OPERATORS = DATE_OPERATORS | NUMERIC_OPERATORS | {"equals"}


def _rule_match(customer: dict[str, Any], rule: dict[str, Any], now: datetime) -> bool:
    field = str(rule.get("field") or "").strip()
    operator = str(rule.get("operator") or "").strip().lower()
    expected = rule.get("value")
    actual = customer.get(field)

    if operator == "older_than":
        if field not in DATE_FIELDS:
            raise HTTPException(status_code=422, detail=f"Unsupported date field '{field}'")
        days = _parse_days(expected)
        dt = _parse_dt(actual)
        if dt is None:
            return True
        return dt <= (now - timedelta(days=days))

    if operator == "within_last":
        if field not in DATE_FIELDS:
            raise HTTPException(status_code=422, detail=f"Unsupported date field '{field}'")
        days = _parse_days(expected)
        dt = _parse_dt(actual)
        if dt is None:
            return False
        return dt >= (now - timedelta(days=days))

    if operator in NUMERIC_OPERATORS:
        # A customer whose value for this field is not a number simply does
        # not match. `float(actual or 0)` used to raise a bare ValueError —
        # not an HTTPException — so one segment comparing a text field
        # numerically 500'd every read that evaluated it. Now that
        # list_segments evaluates the whole library, that would have taken
        # the Segments page down rather than one detail call. See #455.
        left = _as_number(actual)
        right = _as_number(expected)
        if left is None or right is None:
            return False
        return left > right if operator == "greater_than" else left < right

    if operator == "equals":
        return actual == expected

    raise HTTPException(status_code=422, detail=f"Unsupported segment operator '{operator}'")


def _validate_rules(rules: dict[str, Any]) -> None:
    """Reject on write what ``_rule_match`` would reject on read.

    Without this a segment saves fine and then 422s the moment anyone opens
    it, because rule evaluation only happens on the read path. Same vocabulary
    as ``_rule_match`` — keep the two in step.
    """
    match_mode, rule_list = _iter_rules(rules)
    if match_mode not in {"all", "any"}:
        raise HTTPException(status_code=422, detail=f"Unsupported match mode '{match_mode}'")
    if not rule_list:
        raise HTTPException(status_code=422, detail="A segment needs at least one rule")

    for rule in rule_list:
        field = str(rule.get("field") or "").strip()
        operator = str(rule.get("operator") or "").strip().lower()
        if not field:
            raise HTTPException(status_code=422, detail="Every rule needs a field")
        if operator not in SUPPORTED_OPERATORS:
            raise HTTPException(status_code=422, detail=f"Unsupported segment operator '{operator}'")
        # A rule with no value is not a narrower rule, it is a rule that
        # matches everyone: `older_than 0 days` and `lifetime_value > 0` both
        # select the whole customer list. Refuse it rather than save a segment
        # that quietly means "all customers".
        raw_value = rule.get("value")
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            raise HTTPException(status_code=422, detail=f"Rule on '{field}' needs a value")

        if operator in DATE_OPERATORS:
            if field not in DATE_FIELDS:
                raise HTTPException(status_code=422, detail=f"Unsupported date field '{field}'")
            if _parse_days(raw_value) < 1:
                raise HTTPException(
                    status_code=422, detail=f"Rule on '{field}' needs a window of at least 1 day"
                )
        if operator in NUMERIC_OPERATORS:
            if field not in NUMERIC_FIELDS:
                raise HTTPException(
                    status_code=422,
                    detail=f"'{field}' cannot be compared as a number — use 'equals'",
                )
            if _as_number(raw_value) is None:
                raise HTTPException(
                    status_code=422, detail=f"Rule on '{field}' needs a numeric value"
                )


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


def _customer_stats_sql(has_customer_type: bool, has_metadata: bool) -> str:
    """Build the per-customer rollup.

    **GROUP BY names the primary key and nothing else** (#681). The previous
    version listed every selected column, which put `c.metadata` in the GROUP BY
    — and on PostgreSQL `customers.metadata` is `json`, a type with no equality
    operator, so the whole statement raised:

        psycopg2.errors.UndefinedFunction:
        could not identify an equality operator for type json

    That 500'd `GET /api/segments`, i.e. the entire Segments page, on every
    Postgres deployment since the initial public release. It was invisible in
    development because the throwaway test container is SQLite, which has no
    `json` type — the column is TEXT there and grouping on it is legal.

    Grouping by `customers.id` alone is correct, not a workaround: it is the
    primary key, so every other `c.*` column is functionally dependent on it and
    both engines allow selecting them. Postgres has implemented that since 9.1;
    SQLite is permissive and each group holds exactly one row anyway.

    Keep json/jsonb columns out of GROUP BY. `jsonb` would work, but the fix is
    not to change the column type — it is to not group on a payload column.
    """
    customer_type_select = (
        "c.customer_type AS customer_type," if has_customer_type else "NULL AS customer_type,"
    )
    metadata_select = "c.metadata AS metadata," if has_metadata else "NULL AS metadata,"
    return f"""
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
            GROUP BY c.id
            ORDER BY c.created_at DESC
            """


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

    rows = db.execute(
        text(_customer_stats_sql(has_customer_type, has_metadata))
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
    # Order matters: _customer_stats probes information_schema first and, on
    # SQLite, always lands in its except branch — which calls db.rollback().
    # Loading the ORM rows first would leave every one of them expired, so
    # each attribute read below would re-query.
    #
    # One customer scan for the whole list, then rules applied in memory —
    # the same work get_segment does for a single segment. Without it
    # `matching_customer_count` was declared on SegmentOut and never
    # populated here, so the list's Customers column and the segment chips
    # rendered 0 for every row forever (#455).
    stats = _customer_stats(db)

    custom_rows = db.query(Segment).filter(
        Segment.deleted_at.is_(None),
    ).order_by(Segment.created_at.desc()).all()

    def _count(raw_rules: Any) -> int | None:
        try:
            # _coerce_rules is inside the guard on purpose: a row whose
            # stored rules is not a JSON object raises from here, and doing
            # it at the call site put that case outside the try — so the
            # guard could not fire for half of what its name covers.
            return len(_apply_rules(stats, _coerce_rules(raw_rules)))
        except (HTTPException, ValueError, TypeError):
            # A single unreadable segment must not take the whole library
            # down. Catching HTTPException alone was not enough: rows
            # predating _validate_rules can still raise from the comparison
            # itself, and this list is the Segments page's first call.
            # Unknown count, not zero.
            log.warning("segment_count_failed rules=%r", raw_rules)
            return None

    custom_items = [
        SegmentOut(
            id=str(row.id),
            name=str(row.name),
            rules=_safe_rules(row.rules),
            is_builtin=False,
            created_at=str(_normalize_value(row.created_at)),
            matching_customer_count=_count(row.rules),
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
            matching_customer_count=_count(segment["rules"]),
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
    _validate_rules(payload.rules)
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


@router.patch("/{segment_id}", response_model=SegmentOut)
async def update_segment(
    segment_id: str,
    payload: SegmentPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SegmentOut:
    """Edit a saved segment.

    Added 2026-09-07 (#455). Until now the path served GET and DELETE only,
    so the Edit button on every row 405'd.
    """
    if segment_id in BUILTIN_BY_ID:
        raise HTTPException(status_code=400, detail="Built-in segments cannot be edited")

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

    data = payload.model_dump(exclude_unset=True)
    changed: list[str] = []

    if data.get("name") is not None:
        row.name = str(data["name"])
        changed.append("name")
    if data.get("rules") is not None:
        _validate_rules(data["rules"])
        row.rules = data["rules"]
        changed.append("rules")

    if not changed:
        # Refuse rather than answer 200 to a write that changed nothing —
        # the "reports success, changes nothing" shape this repo audits for.
        raise HTTPException(status_code=422, detail="No updatable fields supplied")

    db.commit()
    db.refresh(row)

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="segment_updated",
        entity_type="segment",
        entity_id=segment_id,
        details={"fields": sorted(changed), "name": row.name, "rules": _coerce_rules(row.rules)},
    )
    db.commit()

    return SegmentOut(
        id=segment_id,
        name=str(row.name),
        rules=_coerce_rules(row.rules),
        is_builtin=False,
        created_at=str(_normalize_value(row.created_at)),
    )


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
