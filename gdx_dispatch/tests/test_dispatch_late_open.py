"""Dispatch "past their date, not closed out" — GET /api/dispatch/late-open.

The dispatch board loads undated jobs plus the dates in view, so an open job
whose scheduled day had passed was on no screen at all. On 2026-09-27 prod had
six of them, the oldest scheduled 2026-05-13, found only by SQL. This
endpoint is the list the board renders for them.

Contract, pinned here:
1. An open job scheduled before the start of TODAY in the shop's zone is
   listed, with its tech's name and how many shop days late it is.
2. The boundary is the shop's calendar day: a job earlier today (already in
   the past as an instant) is not late, and a 7pm-yesterday job — which is
   TODAY in UTC for a Central shop — is.
3. Completed and cancelled jobs never appear; every other stage does.
4. A holding-area stamp does not hide a job (a stale Ready to Schedule stamp
   is how several of the prod six sat unseen).
5. Soft-deleted and undated jobs never appear.
6. Oldest first.
7. The route is gated on jobs.read_all.
8. Each row carries the jobs list's display_state, so the board's state chip
   does not render every row as "unverified — refresh to sync".

Harness mirrors test_dashboard_return_visits.py: router function invoked
directly against in-memory SQLite, schema built from the ORM.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Invoice, Job, Technician
from gdx_dispatch.routers import dispatch_scheduling
from gdx_dispatch.routers.dispatch_scheduling import late_open_jobs

TENANT = "tenant-late-open"
USER = {"user_id": "user-office", "tenant_id": TENANT, "role": "dispatcher"}
SHOP_TZ = "America/Chicago"
ZONE = ZoneInfo(SHOP_TZ)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__, Customer.__table__, Technician.__table__, AppSettings.__table__,
        Invoice.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    session.add(AppSettings(id=uuid4(), timezone=SHOP_TZ))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _shop_midnight(days_ago: int) -> datetime:
    """Start of the shop-local day ``days_ago`` days before today, as UTC."""
    today = datetime.now(timezone.utc).astimezone(ZONE).date() - timedelta(days=days_ago)
    return datetime.combine(today, time(0, 0), tzinfo=ZONE).astimezone(timezone.utc)


def _customer(db, name="Paula Vance") -> Customer:
    cust = Customer(id=uuid4(), name=name, company_id=TENANT)
    db.add(cust)
    db.commit()
    return cust


def _job(db, customer, scheduled_at, **overrides) -> Job:
    fields = dict(
        id=uuid4(),
        title="Replace broken spring",
        customer_id=customer.id if customer else None,
        company_id=TENANT,
        status="Scheduled",
        lifecycle_stage="scheduled",
        dispatch_status="assigned",
        billing_status="unbilled",
        is_return_visit=False,
        scheduled_at=scheduled_at,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    job = Job(**fields)
    db.add(job)
    db.commit()
    return job


def _ids(db) -> list[str]:
    return [row["id"] for row in late_open_jobs(db=db, user=USER)["items"]]


def test_open_job_from_a_past_day_is_listed_with_tech_and_days_late(db) -> None:
    cust = _customer(db)
    tech = Technician(id=str(uuid4()), name="Mike T", company_id=TENANT)
    db.add(tech)
    db.commit()
    job = _job(db, cust, _shop_midnight(3) + timedelta(hours=9), assigned_to=tech.id, job_number="JOB-1")

    body = late_open_jobs(db=db, user=USER)
    assert body["timezone"] == SHOP_TZ
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["id"] == str(job.id)
    assert row["job_number"] == "JOB-1"
    assert row["customer_name"] == "Paula Vance"
    assert row["tech_name"] == "Mike T"
    assert row["days_late"] == 3
    # Offset-explicit, so the browser does not read a naive value as local.
    assert row["scheduled_at"].endswith("+00:00")


def test_boundary_is_the_shop_day_not_the_instant_or_utc(db) -> None:
    cust = _customer(db)
    earlier_today = _job(db, cust, _shop_midnight(0) + timedelta(minutes=1))
    # 19:00 yesterday shop time is already today's date in UTC for a Central
    # shop — a UTC-day cutoff would miss it.
    yesterday_evening = _job(db, cust, _shop_midnight(1) + timedelta(hours=19))
    just_before_midnight = _job(db, cust, _shop_midnight(0) - timedelta(minutes=1))

    ids = _ids(db)
    assert str(earlier_today.id) not in ids
    assert str(yesterday_evening.id) in ids
    assert str(just_before_midnight.id) in ids


@pytest.mark.parametrize("stage", ["completed", "cancelled"])
def test_terminal_stages_never_appear(db, stage) -> None:
    cust = _customer(db)
    _job(db, cust, _shop_midnight(5), lifecycle_stage=stage, status=stage.title())
    assert _ids(db) == []


@pytest.mark.parametrize("stage", ["service_call", "estimate", "scheduled", "in_progress"])
def test_every_open_stage_appears(db, stage) -> None:
    cust = _customer(db)
    job = _job(db, cust, _shop_midnight(5), lifecycle_stage=stage)
    assert _ids(db) == [str(job.id)]


def test_holding_area_stamp_does_not_hide_a_late_job(db) -> None:
    cust = _customer(db)
    job = _job(db, cust, _shop_midnight(10), holding_area_id=str(uuid4()))
    assert _ids(db) == [str(job.id)]


def test_soft_deleted_and_undated_jobs_never_appear(db) -> None:
    cust = _customer(db)
    _job(db, cust, _shop_midnight(5), deleted_at=datetime.now(timezone.utc))
    _job(db, cust, None, lifecycle_stage="service_call", status="Service Call")
    assert _ids(db) == []


def test_future_jobs_do_not_appear(db) -> None:
    cust = _customer(db)
    _job(db, cust, _shop_midnight(-2))
    assert _ids(db) == []


def test_oldest_first(db) -> None:
    cust = _customer(db)
    newer = _job(db, cust, _shop_midnight(2))
    older = _job(db, cust, _shop_midnight(40))
    middle = _job(db, cust, _shop_midnight(9))
    assert _ids(db) == [str(older.id), str(middle.id), str(newer.id)]


def test_route_is_gated_on_jobs_read_all() -> None:
    route = next(
        r for r in dispatch_scheduling.router.routes
        if getattr(r, "path", "") == "/api/dispatch/late-open"
    )
    keys: set[str] = set()
    for dep in route.dependant.dependencies:
        call = dep.call
        if getattr(call, "__qualname__", "") == "require_permission.<locals>._dependency":
            for cell in call.__closure__ or ():
                if isinstance(cell.cell_contents, set):
                    keys |= cell.cell_contents
    assert keys == {"jobs.read_all"}


def test_rows_carry_the_jobs_list_display_state(db) -> None:
    from gdx_dispatch.routers.jobs import _display_state_for_jobs

    cust = _customer(db)
    job = _job(db, cust, _shop_midnight(4))
    row = late_open_jobs(db=db, user=USER)["items"][0]
    assert isinstance(row["display_state"], dict) and row["display_state"]
    # Exactly what /api/jobs would say for the same job.
    assert row["display_state"] == _display_state_for_jobs(db, [(job.id, job.lifecycle_stage)])[str(job.id)]
