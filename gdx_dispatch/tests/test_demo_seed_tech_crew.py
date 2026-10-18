"""The demo tech can open chat on every job of their seeded route (GDXA-1551).

The seed set ``Job.assigned_to`` to the Technician.id and wrote no
``job_assignments`` row. Mobile chat finds a tech's job by that row (its
``assigned_to`` branch compares a user id), so every route job answered 404
"job not found or not visible" on the public demo. This runs the real
``seed()`` and drives the real chat handlers as the seeded tech.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from starlette.requests import Request

from gdx_dispatch.models.tenant_models import Job, JobAssignment, Technician, User
from gdx_dispatch.routers import mobile_chat
from gdx_dispatch.tests.conftest import make_fresh_db, production_sessionmaker

SEED_PATH = Path(__file__).resolve().parents[1] / "docker" / "demo" / "seed_demo.py"
TENANT = "00000000-0000-0000-0000-0000000000d1"


@pytest.fixture
def seeded(monkeypatch):
    spec = importlib.util.spec_from_file_location("seed_demo_crew_under_test", SEED_PATH)
    seed_demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_demo)

    engine = make_fresh_db()
    Session = production_sessionmaker(engine)
    numbers = itertools.count(1)
    monkeypatch.setattr(seed_demo, "SessionLocal", Session)
    monkeypatch.setattr(seed_demo, "company_id", lambda: TENANT)
    monkeypatch.setattr(
        seed_demo, "allocate_job_number",
        lambda _tenant, customer_name=None: f"J-{next(numbers):04d}",
    )
    seed_demo.seed()

    db = Session()
    try:
        tech_user = db.execute(
            select(User).where(User.email == "tech@gdxdispatch.com")  # pii-ok: public demo login
        ).scalar_one()
        tech = db.execute(
            select(Technician).where(Technician.user_id == str(tech_user.id))
        ).scalar_one()
        route = db.execute(
            select(Job).where(Job.assigned_to == tech.id)
        ).scalars().all()
        yield {
            "db": db, "tech": tech, "route": route,
            "user": {"user_id": str(tech_user.id), "role": "technician", "tenant_id": TENANT},
        }
    finally:
        db.close()
        engine.dispose()


def _req():
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def test_every_route_job_has_a_lead_crew_row_for_the_tech(seeded):
    db, tech, route = seeded["db"], seeded["tech"], seeded["route"]
    # TECH_JOBS (5) + the install job.
    assert len(route) == 6
    for job in route:
        rows = db.execute(
            select(JobAssignment).where(
                JobAssignment.job_id == str(job.id), JobAssignment.deleted_at.is_(None),
            )
        ).scalars().all()
        assert [(r.tech_id, r.is_lead) for r in rows] == [(tech.id, True)], job.title


def test_seeded_tech_can_get_and_post_chat_on_every_route_job(seeded):
    db, user = seeded["db"], seeded["user"]
    for job in seeded["route"]:
        job_id = str(job.id)
        sent = mobile_chat.send_job_chat(
            job_id=job_id,
            payload=mobile_chat.SendChatIn(kind="quick_action", quick_action="on_my_way"),
            request=_req(), current_user=user, db=db,
        )
        assert sent.status_code == 201, (job.title, sent.body)

        got = mobile_chat.get_job_chat(job_id=job_id, request=_req(), current_user=user, db=db)
        assert got.status_code == 200, (job.title, got.body)
        bodies = [m["body"] for m in json.loads(got.body)["messages"]]
        assert bodies == ["On my way"], job.title


def test_without_the_crew_row_the_seeded_tech_is_refused(seeded):
    """Control: the crew row is what grants access. Delete it and the same
    job is invisible, which is the demo as it was before this fix."""
    db, user = seeded["db"], seeded["user"]
    job_id = str(seeded["route"][0].id)
    assert mobile_chat._job_visible_to_user(db, job_id, user) is True
    for row in db.execute(
        select(JobAssignment).where(JobAssignment.job_id == job_id)
    ).scalars():
        db.delete(row)
    db.flush()
    assert mobile_chat._job_visible_to_user(db, job_id, user) is False
    got = mobile_chat.get_job_chat(job_id=job_id, request=_req(), current_user=user, db=db)
    assert got.status_code == 404
    db.rollback()
