"""Closeout return-visit + parts-to-order (Doug 2026-08-04).

The gap this closes: the tech's only way to say "we have to come back" was
a free-text note ("add a note and a tag" — tech-mobile-flow.md), and the
only way to say "order this part" mid-closeout was to back out of the sheet
and use the job screen's Parts card. Now the closeout sheet asks both:

* ``needs_return_visit`` + ``return_visit_reason`` → an unscheduled child
  job (``is_return_visit=True``, parent linked, the WHY in description) is
  created in the SAME transaction as the closeout, so dispatch sees it the
  moment the job completes.
* ``parts_to_order`` → job_parts_needed rows with status='needed',
  source='request' — identical to the job-screen Parts card's rows, so they
  land in the office Parts-to-Order queue unchanged.

Contract, pinned here:
1. Flag + reason → exactly one open child, linked and described; the
   closeout audit event carries the reason durably (the snapshot has no
   column for it and child.description is editable).
2. Flag without reason → 422 (missing: return_visit_reason), NOTHING
   written — the job stays open.
3. A re-closeout (restatement or replayed offline submit) REUSES the open
   child — never a sibling — and a NEW reason is APPENDED to the child
   (an identical replayed reason is not; the 422 forces the reason, so
   discarding it on reuse would make the requirement theater).
4. parts_to_order rows land status='needed'/source='request' and SURVIVE a
   re-closeout — the restatement replace step only touches source='closeout'.
5. A replayed/restated parts_to_order list dedups exact-match (requester +
   name + sku + qty) against the job's open request rows — including rows
   the Parts card already filed — so the office never orders double; a
   different qty is a new ask and lands.
6. urgency='critical' is refused at the payload boundary: the C5 critical
   dispatcher push only fires on the Parts card path, and a critical that
   skips its own alarm is worse than a rejected one.

Harness mirrors test_job_closeout_supersede.py: `closeout_job` invoked
directly as a function against in-memory SQLite.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobCloseout,
    JobPartNeeded,
    Payment,
    Technician,
    TimeEntry,
)
from gdx_dispatch.modules.inventory.models import JobPart, Part
from gdx_dispatch.routers.jobs import (
    CloseoutPartToOrder,
    CloseoutPayload,
    closeout_job,
)

TENANT = "tenant-return-visit"
USER = "user-gus"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__,
        Customer.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        JobPartNeeded.__table__,
        JobCloseout.__table__,
        Part.__table__,
        JobPart.__table__,
        Technician.__table__,
        TimeEntry.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def _seed_job(db) -> Job:
    job = Job(
        customer_id=uuid4(),
        title="Broken torsion spring",
        description="t",
        lifecycle_stage="in_progress",
        dispatch_status="on_site",
        billing_status="unbilled",
        priority="High",
        company_id=TENANT,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _closeout(db, job, **overrides):
    payload = CloseoutPayload(
        parts=[], hours=1.0, no_parts_used=True, **overrides
    )
    return closeout_job(
        payload=payload,
        job_id=str(job.id),
        request=_request(),
        current_user={"user_id": USER, "tenant_id": TENANT, "role": "technician"},
        db=db,
    )


def _children(db, job) -> list[Job]:
    return list(
        db.execute(
            select(Job).where(Job.parent_job_id == job.id, Job.deleted_at.is_(None))
        ).scalars()
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


def test_closeout_spawns_return_visit_child(db) -> None:
    job = _seed_job(db)
    resp = _closeout(
        db, job,
        needs_return_visit=True,
        return_visit_reason="Spring on order — needs a second trip to install.",
    )
    assert resp.status_code == 201
    body = _body(resp)

    children = _children(db, job)
    assert len(children) == 1
    child = children[0]
    assert body["return_visit_job_id"] == str(child.id)
    assert child.is_return_visit is True
    assert child.title == "Return visit: Broken torsion spring"
    assert child.description == "Spring on order — needs a second trip to install."
    assert child.lifecycle_stage == "service_call", "no fake scheduled state — dispatch slots it"
    assert child.dispatch_status == "unassigned"
    assert child.scheduled_at is None
    assert child.priority == "High", "urgency carries over from the original"

    db.refresh(job)
    assert job.lifecycle_stage == "completed"

    # The reason's durable home is the append-only closeout audit event —
    # the snapshot has no column for it and child.description is editable.
    closeout_events = list(
        db.execute(select(AuditLog).where(AuditLog.action == "job_closeout")).scalars()
    )
    assert len(closeout_events) == 1
    assert "Spring on order" in str(closeout_events[0].details)
    spawn_events = list(
        db.execute(select(AuditLog).where(AuditLog.action == "return_visit_spawned")).scalars()
    )
    assert len(spawn_events) == 1


def test_return_visit_without_reason_is_refused_before_any_write(db) -> None:
    """'Yes but I won't say why' is the exact silence the field exists to
    end — refused with the tenant-gate 422 shape, and the job stays open."""
    job = _seed_job(db)
    resp = _closeout(db, job, needs_return_visit=True, return_visit_reason="   ")
    assert resp.status_code == 422
    assert _body(resp)["missing"] == ["return_visit_reason"]

    assert _children(db, job) == []
    db.refresh(job)
    assert job.lifecycle_stage == "in_progress", "a refused closeout completes nothing"
    assert not list(
        db.execute(select(JobCloseout).where(JobCloseout.job_id == job.id)).scalars()
    )


def test_recloseout_reuses_the_open_child_and_keeps_every_reason(db) -> None:
    """Restatement (or an offline replay past the idempotency cache) must
    not mint a sibling — dispatch schedules ONE return trip. And the reuse
    branch must not discard the attested WHY: a new reason is appended to
    the child; an identical replayed reason is not duplicated."""
    job = _seed_job(db)
    first = _body(_closeout(db, job, needs_return_visit=True, return_visit_reason="waiting on parts"))
    second = _body(_closeout(db, job, needs_return_visit=True, return_visit_reason="waiting on parts"))

    children = _children(db, job)
    assert len(children) == 1
    child = children[0]
    assert first["return_visit_job_id"] == second["return_visit_job_id"] == str(child.id)
    assert child.description.count("waiting on parts") == 1, "identical replay must not duplicate"

    # A restatement with a DIFFERENT reason reaches dispatch too.
    _closeout(db, job, needs_return_visit=True, return_visit_reason="also needs new struts")
    db.refresh(child)
    assert "waiting on parts" in child.description
    assert "also needs new struts" in child.description
    assert len(_children(db, job)) == 1


def test_parts_to_order_land_as_needed_requests_and_survive_recloseout(db) -> None:
    job = _seed_job(db)
    resp = _closeout(
        db, job,
        parts_to_order=[
            CloseoutPartToOrder(name="Torsion spring 218x2x30", qty=2, urgency="urgent"),
            CloseoutPartToOrder(name="End bearing plate", sku="EBP-400", qty=1, note="left side"),
        ],
    )
    assert resp.status_code == 201
    assert _body(resp)["parts_to_order_count"] == 2

    rows = list(
        db.execute(
            select(JobPartNeeded).where(JobPartNeeded.job_id == str(job.id))
        ).scalars()
    )
    assert len(rows) == 2
    by_name = {r.part_name: r for r in rows}
    spring = by_name["Torsion spring 218x2x30"]
    assert (spring.status, spring.source, spring.urgency) == ("needed", "request", "urgent")
    assert spring.sku is None, "free-text request — no catalog match required"
    assert spring.quantity == 2
    assert spring.requested_by_user_id == USER
    plate = by_name["End bearing plate"]
    assert (plate.sku, plate.notes, plate.urgency) == ("EBP-400", "left side", "normal")

    # Restatement with no parts_to_order: the needed rows are requests, not
    # attestations — the source='closeout' replace step must not eat them.
    _closeout(db, job)
    survivors = list(
        db.execute(
            select(JobPartNeeded).where(
                JobPartNeeded.job_id == str(job.id), JobPartNeeded.status == "needed"
            )
        ).scalars()
    )
    assert len(survivors) == 2, "re-closeout must not delete open part requests"


def test_replayed_parts_list_dedups_exact_match(db) -> None:
    """The failure mode this pins: an offline-queued closeout replayed past
    the redis idempotency cache re-submits the same parts_to_order list —
    without the guard, the office orders double springs."""
    job = _seed_job(db)
    # A row the tech already filed from the job screen's Parts card — the
    # same person asking for the same thing on the same job is ONE ask.
    db.add(JobPartNeeded(
        id="card-row-1",
        company_id=TENANT,
        job_id=str(job.id),
        part_name="Torsion spring 218x2x30",
        quantity=2,
        status="needed",
        source="request",
        requested_by_user_id=USER,
    ))
    db.commit()

    order = [
        CloseoutPartToOrder(name="Torsion spring 218x2x30", qty=2),  # dupes the card row
        CloseoutPartToOrder(name="Torsion spring 218x2x30", qty=1),  # different qty = new ask
        CloseoutPartToOrder(name="End bearing plate", qty=1),
    ]
    resp = _body(_closeout(db, job, parts_to_order=order))
    assert resp["parts_to_order_count"] == 2, "card-row duplicate skipped, the other two land"

    # The replay: identical list again — nothing new may land.
    replay = _body(_closeout(db, job, parts_to_order=order))
    assert replay["parts_to_order_count"] == 0

    rows = list(
        db.execute(
            select(JobPartNeeded).where(
                JobPartNeeded.job_id == str(job.id), JobPartNeeded.status == "needed"
            )
        ).scalars()
    )
    assert len(rows) == 3, "one card row + two closeout rows — never doubled"


def test_critical_urgency_is_refused_at_the_boundary(db) -> None:
    """C5's critical-part dispatcher push only fires on the Parts card
    path — a closeout-path critical would skip its own alarm silently, so
    the payload refuses it outright."""
    with pytest.raises(ValidationError):
        CloseoutPartToOrder(name="Torsion spring", urgency="critical")


def test_plain_closeout_is_untouched(db) -> None:
    """Defaults leave the legacy path byte-identical: no child, no needed
    rows, response fields present but empty."""
    job = _seed_job(db)
    resp = _closeout(db, job)
    assert resp.status_code == 201
    body = _body(resp)
    assert body["return_visit_job_id"] is None
    assert body["parts_to_order_count"] == 0
    assert _children(db, job) == []
    db.refresh(job)
    assert job.lifecycle_stage == "completed"
