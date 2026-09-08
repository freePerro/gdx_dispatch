from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest


def _mock_request(tenant_id="test-tenant"):
    r = MagicMock()
    r.state.tenant = {"id": tenant_id}
    r.client.host = "127.0.0.1"
    return r

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.models.tenant_models import Segment
from gdx_dispatch.routers import segments as segments_router
from gdx_dispatch.routers.segments import (
    SegmentCreateIn,
    SegmentPatch,
    create_segment,
    delete_segment,
    get_segment,
    list_segment_customers,
    list_segments,
    update_segment,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup_db = Session()

    setup_db.execute(
        text(
            """
            CREATE TABLE customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                address TEXT,
                company_id TEXT,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            )
            """
        )
    )
    setup_db.execute(
        text(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                company_id TEXT,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            )
            """
        )
    )
    setup_db.execute(
        text(
            """
            CREATE TABLE invoices (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                total NUMERIC NOT NULL,
                company_id TEXT,
                deleted_at TEXT
            )
            """
        )
    )
    # `segments` is built from the ORM, NOT hand-written SQL. A hand-written
    # table here declared `deleted_at TEXT` (nullable) while the model said
    # NOT NULL and the real database — built by create_all — agreed with the
    # model. So the fixture was kinder than production, and 31 tests stayed
    # green over a feature that could not insert a single row (#455). Build
    # the table the way the app builds it, so the schema under test is the
    # schema that ships.
    Segment.__table__.create(setup_db.get_bind(), checkfirst=True)
    setup_db.commit()
    setup_db.close()

    try:
        yield Session
    finally:
        engine.dispose()


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _seed_customer(Session, *, name: str, created_days_ago: int) -> str:
    cid = str(uuid.uuid4())
    db = Session()
    db.execute(
        text(
            """
            INSERT INTO customers (id, name, email, phone, address, company_id, created_at, deleted_at)
            VALUES (:id, :name, :email, :phone, :address, 'tenant-test', :created_at, NULL)
            """
        ),
        {
            "id": cid,
            "name": name,
            "email": f"{name.lower().replace(' ', '.')}@example.com",
            "phone": "555-0000",
            "address": "100 Main",
            "created_at": _iso_days_ago(created_days_ago),
        },
    )
    db.commit()
    db.close()
    return cid


def _seed_job(Session, *, customer_id: str, days_ago: int) -> str:
    jid = str(uuid.uuid4())
    db = Session()
    db.execute(
        text(
            """
            INSERT INTO jobs (id, customer_id, company_id, created_at, deleted_at)
            VALUES (:id, :customer_id, 'tenant-test', :created_at, NULL)
            """
        ),
        {"id": jid, "customer_id": customer_id, "created_at": _iso_days_ago(days_ago)},
    )
    db.commit()
    db.close()
    return jid


def _seed_invoice(Session, *, job_id: str, total: float) -> str:
    iid = str(uuid.uuid4())
    db = Session()
    db.execute(
        text(
            """
            INSERT INTO invoices (id, job_id, total, company_id, deleted_at)
            VALUES (:id, :job_id, :total, 'tenant-test', NULL)
            """
        ),
        {"id": iid, "job_id": job_id, "total": total},
    )
    db.commit()
    db.close()
    return iid


def test_all_segment_routes_require_auth_dependency():
    guarded_paths = set()
    for route in segments_router.router.routes:
        if not hasattr(route, "dependant"):
            continue
        for dep in route.dependant.dependencies:
            if dep.call is get_current_user:
                guarded_paths.add(route.path)
                break

    assert "/api/segments" in guarded_paths
    assert "/api/segments/{segment_id}" in guarded_paths
    assert "/api/segments/{segment_id}/customers" in guarded_paths


async def test_list_segments_includes_builtins(tenant_db_session):
    db = tenant_db_session()
    out = await list_segments(_={}, db=db)
    db.close()
    ids = {seg.id for seg in out.items}
    assert {"at-risk", "high-value", "new", "inactive"} <= ids


async def test_create_custom_segment_and_list(tenant_db_session):
    db = tenant_db_session()
    payload = SegmentCreateIn(
        name="Dormant 90d",
        rules={"field": "last_job_date", "operator": "older_than", "value": "90 days"},
    )
    created = await create_segment(payload=payload, request=_mock_request(), user={}, db=db)
    assert created.name == "Dormant 90d"
    assert created.rules["field"] == "last_job_date"
    assert created.is_builtin is False

    listed = await list_segments(_={}, db=db)
    custom = [x for x in listed.items if x.name == "Dormant 90d"]
    assert len(custom) == 1
    db.close()


def test_create_segment_requires_name_and_rules():
    with pytest.raises(Exception):
        SegmentCreateIn(name="", rules=None)


async def test_get_builtin_segment_returns_count(tenant_db_session):
    Session = tenant_db_session
    high_value = _seed_customer(Session, name="High Value Co", created_days_ago=120)
    normal = _seed_customer(Session, name="Normal Co", created_days_ago=120)

    hv_job = _seed_job(Session, customer_id=high_value, days_ago=20)
    normal_job = _seed_job(Session, customer_id=normal, days_ago=20)
    _seed_invoice(Session, job_id=hv_job, total=6001.00)
    _seed_invoice(Session, job_id=normal_job, total=4999.99)

    db = Session()
    out = await get_segment(segment_id="high-value", _={}, db=db)
    db.close()
    assert out.id == "high-value"
    assert out.matching_customer_count == 1


async def test_get_segment_customers_for_at_risk(tenant_db_session):
    Session = tenant_db_session
    stale = _seed_customer(Session, name="Stale Co", created_days_ago=300)
    fresh = _seed_customer(Session, name="Fresh Co", created_days_ago=300)
    _seed_customer(Session, name="Never Job Co", created_days_ago=300)

    _seed_job(Session, customer_id=stale, days_ago=210)
    _seed_job(Session, customer_id=fresh, days_ago=15)

    db = Session()
    out = await list_segment_customers(segment_id="at-risk", _={}, db=db)
    db.close()
    names = {item["name"] for item in out.items}
    assert "Stale Co" in names
    assert "Never Job Co" in names
    assert "Fresh Co" not in names


async def test_get_custom_segment_details_with_count(tenant_db_session):
    Session = tenant_db_session
    target = _seed_customer(Session, name="Target Co", created_days_ago=200)
    _seed_customer(Session, name="Recent Co", created_days_ago=200)
    _seed_job(Session, customer_id=target, days_ago=120)

    db = Session()
    created = await create_segment(
        payload=SegmentCreateIn(
            name="No recent jobs",
            rules={"field": "last_job_date", "operator": "older_than", "value": "90 days"},
        ),
        request=_mock_request(),
        user={},
        db=db,
    )
    out = await get_segment(segment_id=created.id, _={}, db=db)
    assert out.id == created.id
    assert out.matching_customer_count >= 1
    db.close()


async def test_unknown_segment_returns_404(tenant_db_session):
    db = tenant_db_session()
    with pytest.raises(HTTPException) as exc:
        await get_segment(segment_id=str(uuid.uuid4()), _={}, db=db)
    db.close()
    assert exc.value.status_code == 404


async def test_delete_custom_segment(tenant_db_session):
    db = tenant_db_session()
    created = await create_segment(
        payload=SegmentCreateIn(
            name="Temporary Segment",
            rules={"field": "created_at", "operator": "older_than", "value": "1 days"},
        ),
        request=_mock_request(),
        user={},
        db=db,
    )
    resp = await delete_segment(segment_id=created.id, request=_mock_request(), user={}, db=db)
    assert resp.status_code == 204
    with pytest.raises(HTTPException) as exc:
        await get_segment(segment_id=created.id, _={}, db=db)
    db.close()
    assert exc.value.status_code == 404


async def test_delete_builtin_segment_rejected(tenant_db_session):
    db = tenant_db_session()
    with pytest.raises(HTTPException) as exc:
        await delete_segment(segment_id="at-risk", request=_mock_request(), user={}, db=db)
    db.close()
    assert exc.value.status_code == 400


# ── PATCH /api/segments/{id} ─────────────────────────────────────────────
# Added 2026-09-07 (#455). The path served GET and DELETE only, so the Edit
# button on every row 405'd. The Vue also sent `{name, criteria, tags}` —
# neither column exists and `rules` is required, so create answered 422.


async def _make_segment(db, name="Dormant 90d", days="90 days"):
    return await create_segment(
        payload=SegmentCreateIn(
            name=name,
            rules={"field": "last_job_date", "operator": "older_than", "value": days},
        ),
        request=_mock_request(),
        user={},
        db=db,
    )


async def test_update_segment_persists_name_and_rules(tenant_db_session):
    db = tenant_db_session()
    created = await _make_segment(db)

    out = await update_segment(
        segment_id=created.id,
        payload=SegmentPatch(
            name="Dormant 45d",
            rules={
                "match": "all",
                "rules": [
                    {"field": "last_job_date", "operator": "older_than", "value": "45 days"}
                ],
            },
        ),
        request=_mock_request(),
        user={},
        db=db,
    )
    assert out.name == "Dormant 45d"
    assert out.rules["rules"][0]["value"] == "45 days"

    # ...and it is really stored, not just echoed back.
    reread = await get_segment(segment_id=created.id, _={}, db=db)
    db.close()
    assert reread.name == "Dormant 45d"
    assert reread.rules["rules"][0]["value"] == "45 days"


async def test_update_segment_leaves_unsent_fields_alone(tenant_db_session):
    db = tenant_db_session()
    created = await _make_segment(db)

    await update_segment(
        segment_id=created.id,
        payload=SegmentPatch(name="Renamed only"),
        request=_mock_request(),
        user={},
        db=db,
    )
    reread = await get_segment(segment_id=created.id, _={}, db=db)
    db.close()
    assert reread.name == "Renamed only"
    assert reread.rules["value"] == "90 days"


async def test_update_segment_empty_body_is_422_not_silent_ok(tenant_db_session):
    """A write that reports success and changes nothing is the worse bug."""
    db = tenant_db_session()
    created = await _make_segment(db)
    with pytest.raises(HTTPException) as exc:
        await update_segment(
            segment_id=created.id,
            payload=SegmentPatch(),
            request=_mock_request(),
            user={},
            db=db,
        )
    db.close()
    assert exc.value.status_code == 422


async def test_update_builtin_segment_rejected(tenant_db_session):
    db = tenant_db_session()
    with pytest.raises(HTTPException) as exc:
        await update_segment(
            segment_id="at-risk",
            payload=SegmentPatch(name="Nope"),
            request=_mock_request(),
            user={},
            db=db,
        )
    db.close()
    assert exc.value.status_code == 400


async def test_update_unknown_segment_is_404(tenant_db_session):
    db = tenant_db_session()
    with pytest.raises(HTTPException) as exc:
        await update_segment(
            segment_id=str(uuid.uuid4()),
            payload=SegmentPatch(name="Nope"),
            request=_mock_request(),
            user={},
            db=db,
        )
    db.close()
    assert exc.value.status_code == 404


async def test_update_deleted_segment_is_404(tenant_db_session):
    db = tenant_db_session()
    created = await _make_segment(db)
    await delete_segment(segment_id=created.id, request=_mock_request(), user={}, db=db)
    with pytest.raises(HTTPException) as exc:
        await update_segment(
            segment_id=created.id,
            payload=SegmentPatch(name="Zombie"),
            request=_mock_request(),
            user={},
            db=db,
        )
    db.close()
    assert exc.value.status_code == 404


async def test_update_segment_writes_an_audit_row(tenant_db_session):
    """Invariant #1 — every mutation answers who/what/when."""
    db = tenant_db_session()
    created = await _make_segment(db)
    await update_segment(
        segment_id=created.id,
        payload=SegmentPatch(name="Audited"),
        request=_mock_request(),
        user={"sub": "user-42"},
        db=db,
    )
    rows = db.execute(
        text("SELECT user_id, entity_id FROM audit_logs WHERE action = 'segment_updated'")
    ).mappings().all()
    db.close()
    assert len(rows) == 1
    assert rows[0]["entity_id"] == created.id
    assert rows[0]["user_id"] == "user-42"


# ── rules validation on write ────────────────────────────────────────────
# Rule evaluation only happens on the READ path, so an unsupported operator
# used to save happily and then 422 every attempt to open the segment.


@pytest.mark.parametrize(
    "bad_rules",
    [
        {"field": "last_job_date", "operator": "sounds_like", "value": "90 days"},
        {"field": "lifetime_value", "operator": "older_than", "value": "90 days"},
        {"field": "", "operator": "equals", "value": "x"},
        {"match": "all", "rules": []},
    ],
)
async def test_create_segment_refuses_rules_the_reader_would_reject(
    tenant_db_session, bad_rules
):
    db = tenant_db_session()
    with pytest.raises(HTTPException) as exc:
        await create_segment(
            payload=SegmentCreateIn(name="Bad", rules=bad_rules),
            request=_mock_request(),
            user={},
            db=db,
        )
    db.close()
    assert exc.value.status_code == 422


async def test_update_segment_refuses_bad_rules(tenant_db_session):
    db = tenant_db_session()
    created = await _make_segment(db)
    with pytest.raises(HTTPException) as exc:
        await update_segment(
            segment_id=created.id,
            payload=SegmentPatch(
                rules={"field": "lifetime_value", "operator": "older_than", "value": "5"}
            ),
            request=_mock_request(),
            user={},
            db=db,
        )
    # the stored rules must be untouched
    reread = await get_segment(segment_id=created.id, _={}, db=db)
    db.close()
    assert exc.value.status_code == 422
    assert reread.rules["operator"] == "older_than"
    assert reread.rules["field"] == "last_job_date"


# ── list counts ──────────────────────────────────────────────────────────
# SegmentOut declared matching_customer_count and list_segments never set it,
# so the Customers column and every segment chip rendered 0 forever.


async def test_list_segments_reports_matching_customer_counts(tenant_db_session):
    stale = _seed_customer(tenant_db_session, name="Stale Sam", created_days_ago=400)
    _seed_job(tenant_db_session, customer_id=stale, days_ago=300)
    fresh = _seed_customer(tenant_db_session, name="Fresh Fran", created_days_ago=10)
    _seed_job(tenant_db_session, customer_id=fresh, days_ago=5)

    db = tenant_db_session()
    out = await list_segments(_={}, db=db)
    db.close()
    by_id = {seg.id: seg for seg in out.items}

    # at-risk = last job older than 180 days -> the stale customer only.
    assert by_id["at-risk"].matching_customer_count == 1
    # new = created within the last 30 days -> the fresh customer only.
    assert by_id["new"].matching_customer_count == 1
    assert by_id["inactive"].matching_customer_count == 0


async def test_list_segments_survives_a_segment_with_unreadable_rules(tenant_db_session):
    """One bad row must not 422 the whole library — its count is unknown, not 0."""
    # A customer must exist, or _apply_rules returns [] without ever calling
    # _rule_match and the bad operator is never reached.
    _seed_customer(tenant_db_session, name="Someone", created_days_ago=5)

    db = tenant_db_session()
    db.execute(
        text(
            "INSERT INTO segments (id, name, rules, created_at, deleted_at) "
            "VALUES (:id, :name, :rules, :created_at, NULL)"
        ),
        {
            "id": uuid.uuid4().hex,  # SQLAlchemy Uuid stores dash-less hex on SQLite
            "name": "Legacy junk",
            "rules": '{"field": "last_job_date", "operator": "sounds_like", "value": "x"}',
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    db.commit()

    out = await list_segments(_={}, db=db)
    db.close()
    by_name = {seg.name: seg for seg in out.items}
    assert by_name["Legacy junk"].matching_customer_count is None
    # ...while the built-ins are still counted. The seeded customer has no
    # jobs, and _rule_match treats a missing last_job_date as older than any
    # window, so At Risk matches it.
    assert by_name["At Risk"].matching_customer_count == 1


# ── the operators the rule builder can actually emit ─────────────────────
# Until 2026-09-07 the only rules exercised here were date-based, plus two
# deliberately invalid ones. greater_than / less_than / equals and the "any"
# match mode had no passing-path coverage at all.


async def test_greater_than_on_lifetime_value_selects_by_invoice_total(tenant_db_session):
    big = _seed_customer(tenant_db_session, name="Big Spender", created_days_ago=100)
    big_job = _seed_job(tenant_db_session, customer_id=big, days_ago=50)
    _seed_invoice(tenant_db_session, job_id=big_job, total=9000)

    small = _seed_customer(tenant_db_session, name="Small Spender", created_days_ago=100)
    small_job = _seed_job(tenant_db_session, customer_id=small, days_ago=50)
    _seed_invoice(tenant_db_session, job_id=small_job, total=100)

    db = tenant_db_session()
    out = await list_segment_customers(segment_id="high-value", _={}, db=db)
    db.close()
    assert [c["name"] for c in out.items] == ["Big Spender"]
    assert out.total == 1


async def test_less_than_and_equals_on_a_custom_segment(tenant_db_session):
    rich = _seed_customer(tenant_db_session, name="Rich", created_days_ago=10)
    rich_job = _seed_job(tenant_db_session, customer_id=rich, days_ago=5)
    _seed_invoice(tenant_db_session, job_id=rich_job, total=8000)
    poor = _seed_customer(tenant_db_session, name="Poor", created_days_ago=10)
    poor_job = _seed_job(tenant_db_session, customer_id=poor, days_ago=5)
    _seed_invoice(tenant_db_session, job_id=poor_job, total=25)

    db = tenant_db_session()
    cheap = await create_segment(
        payload=SegmentCreateIn(
            name="Under 1k",
            rules={"field": "lifetime_value", "operator": "less_than", "value": 1000},
        ),
        request=_mock_request(), user={}, db=db,
    )
    by_name = await create_segment(
        payload=SegmentCreateIn(
            name="Named Rich",
            rules={"field": "name", "operator": "equals", "value": "Rich"},
        ),
        request=_mock_request(), user={}, db=db,
    )

    cheap_out = await list_segment_customers(segment_id=cheap.id, _={}, db=db)
    named_out = await list_segment_customers(segment_id=by_name.id, _={}, db=db)
    db.close()
    assert [c["name"] for c in cheap_out.items] == ["Poor"]
    assert [c["name"] for c in named_out.items] == ["Rich"]


async def test_match_any_unions_the_rules_where_match_all_intersects(tenant_db_session):
    """The rule builder's Match control is the only thing that sets this."""
    # New customer, no jobs -> matches "created within 30 days" but not
    # "lifetime value > 5000".
    _seed_customer(tenant_db_session, name="New Nobody", created_days_ago=5)

    rules = [
        {"field": "created_at", "operator": "within_last", "value": "30 days"},
        {"field": "lifetime_value", "operator": "greater_than", "value": 5000},
    ]

    db = tenant_db_session()
    any_seg = await create_segment(
        payload=SegmentCreateIn(name="Either", rules={"match": "any", "rules": rules}),
        request=_mock_request(), user={}, db=db,
    )
    all_seg = await create_segment(
        payload=SegmentCreateIn(name="Both", rules={"match": "all", "rules": rules}),
        request=_mock_request(), user={}, db=db,
    )
    any_out = await list_segment_customers(segment_id=any_seg.id, _={}, db=db)
    all_out = await list_segment_customers(segment_id=all_seg.id, _={}, db=db)
    db.close()
    assert any_out.total == 1
    assert all_out.total == 0


async def test_ordering_a_text_field_numerically_is_refused_not_a_500(tenant_db_session):
    """`float('someone@example.com')` raises ValueError, not HTTPException.

    list_segments now evaluates every segment, so one such row would have
    taken the whole Segments page down instead of one detail call.
    """
    db = tenant_db_session()
    poison = {"field": "email", "operator": "greater_than", "value": 1}
    with pytest.raises(HTTPException) as exc:
        await create_segment(
            payload=SegmentCreateIn(name="Poison", rules=poison),
            request=_mock_request(), user={}, db=db,
        )
    assert exc.value.status_code == 422

    # A row that predates the guard still must not break the list.
    _seed_customer(tenant_db_session, name="Someone", created_days_ago=5)
    db.execute(
        text(
            "INSERT INTO segments (id, name, rules, created_at, deleted_at) "
            "VALUES (:id, :name, :rules, :created_at, NULL)"
        ),
        {
            "id": uuid.uuid4().hex,
            "name": "Legacy poison",
            "rules": json.dumps(poison),
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    db.commit()
    out = await list_segments(_={}, db=db)
    db.close()
    by_name = {seg.name: seg for seg in out.items}
    # It evaluates to "matches nobody" rather than raising: _rule_match now
    # treats a non-numeric customer value as a non-match. (An operator it
    # cannot interpret at all still yields an unknown count — see
    # test_list_segments_survives_a_segment_with_unreadable_rules.)
    assert by_name["Legacy poison"].matching_customer_count == 0


@pytest.mark.parametrize(
    ("label", "stored"),
    [
        ("a JSON string, not an object", '"older_than 90 days"'),
        ("a JSON array, not an object", '[{"field": "created_at"}]'),
        # Deliberately not covered: a value that is not JSON at all. The
        # `rules` column is SQLAlchemy JSON, so json.loads runs in the type's
        # result processor while the row is being loaded — before any code in
        # this router. No guard here can catch it, and only hand-written SQL
        # can put the database in that state.
    ],
)
async def test_list_segments_survives_rules_that_are_not_a_json_object(
    tenant_db_session, label, stored
):
    """_coerce_rules raises for these, and it used to run outside the guard.

    A single row in this shape 422'd the entire Segments page rather than
    just its own count.
    """
    _seed_customer(tenant_db_session, name="Someone", created_days_ago=5)
    db = tenant_db_session()
    db.execute(
        text(
            "INSERT INTO segments (id, name, rules, created_at, deleted_at) "
            "VALUES (:id, :name, :rules, :created_at, NULL)"
        ),
        {
            "id": uuid.uuid4().hex,
            "name": "Malformed",
            "rules": stored,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    db.commit()

    out = await list_segments(_={}, db=db)
    db.close()
    by_name = {seg.name: seg for seg in out.items}
    assert by_name["Malformed"].matching_customer_count is None, label
    assert by_name["Malformed"].rules == {}
    # the rest of the library is unaffected
    assert by_name["At Risk"].matching_customer_count == 1


async def test_negative_day_window_is_refused(tenant_db_session):
    r"""`re.search(r"(\d+)", "-30 days")` returned 30, slipping past the gate."""
    db = tenant_db_session()
    with pytest.raises(HTTPException) as exc:
        await create_segment(
            payload=SegmentCreateIn(
                name="Backwards",
                rules={"field": "last_job_date", "operator": "older_than", "value": "-30 days"},
            ),
            request=_mock_request(), user={}, db=db,
        )
    db.close()
    assert exc.value.status_code == 422
