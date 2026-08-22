"""The job detail payload carries what the route card always carried.

PR A of docs/design/mobile-one-job-card-plan.md. `/api/mobile/job/{id}` was
built from a raw SELECT that never read job_type, priority or is_return_visit,
and computed no alerts or parts roll-up. A tech who reached a job from the Jobs
list rather than today's route saw strictly less about it — no priority, no
"beware of dog", no sign it was a second trip.

The load-bearing test here is TestCustomerShapeParity. The pre-code audit found
that "unify the two customer payloads by swapping detail onto _job_card"
silently deletes customer.email — rendered as a mailto row at
MobileJobDetailView.vue:172 — while a TOP-LEVEL key-parity assertion goes green.
So parity is asserted by DESCENDING into customer, and in the direction that
cannot regress: detail must be a superset.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Customer,
    Job,
    JobPartNeeded,
    Tag,
    TagAssignment,
    Technician,
)
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"


def _now() -> datetime:
    today = datetime.now(UTC).date()
    return datetime(today.year, today.month, today.day, 12, 0, tzinfo=UTC)


@pytest.fixture
def app_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()

    from gdx_dispatch.core.modules import require_module

    app = FastAPI()
    app.include_router(mobile_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": USER, "tenant_id": TENANT, "role": "technician",
    }
    app.dependency_overrides[require_module("mobile")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        request.state.user = {"user_id": USER, "tenant_id": TENANT, "role": "technician"}
        return await call_next(request)

    client = TestClient(app)
    yield client, db
    db.close()
    engine.dispose()


def _seed_customer(db, *, notes=None, email="ops@example.test") -> Customer:
    db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True))
    c = Customer(
        id=uuid4(), name="Acme", phone="555-1111", email=email,
        address="100 Billing Rd", notes=notes, company_id=TENANT,
    )
    db.add(c)
    db.commit()
    return c


def _job(db, customer_id: UUID, **kw) -> Job:
    j = Job(
        id=uuid4(), company_id=TENANT, customer_id=customer_id,
        title="Install", description="desc", scheduled_at=_now(),
        assigned_to=TECH, dispatch_status="assigned", **kw,
    )
    db.add(j)
    db.commit()
    return j


def _tag(db, customer, name: str) -> None:
    t = Tag(id=uuid4(), company_id=TENANT, name=name, color="#f00")
    db.add(t)
    db.commit()
    db.add(TagAssignment(
        id=uuid4(), company_id=TENANT, tag_id=t.id,
        entity_type="customer", entity_id=str(customer.id),
    ))
    db.commit()


def _detail(client, job) -> dict:
    r = client.get(f"/api/mobile/job/{job.id.hex}")
    assert r.status_code == 200, r.text
    return r.json()["job"]


class TestCardFields:
    def test_carries_service_type_priority_and_return_visit(self, app_and_db):
        client, db = app_and_db
        c = _seed_customer(db)
        j = _job(db, c.id, job_type="Install", priority="Emergency", is_return_visit=True)
        body = _detail(client, j)
        assert body["service_type"] == "Install"
        assert body["priority"] == "Emergency"
        assert body["is_return_visit"] is True

    def test_defaults_match_the_route_card(self, app_and_db):
        """Asserted as PARITY, not against a literal.

        An earlier version of this test asserted service_type == "Service" and
        failed with "Service Call": Job.job_type carries a python-side default,
        so passing None still stores a value and the `or "Service"` fallback
        never fires. The property that actually matters is that both surfaces
        answer the SAME thing for the same job -- which a hardcoded literal
        would have pinned to whichever surface was written second.
        """
        client, db = app_and_db
        c = _seed_customer(db)
        j = _job(db, c.id, job_type=None, priority=None)
        body = _detail(client, j)
        cards = client.get("/api/mobile/today").json().get("jobs") or []
        assert cards, "today's route returned no card to compare against"
        card = cards[0]
        assert body["service_type"] == card["service_type"]
        assert body["priority"] == card["priority"]
        assert body["is_return_visit"] == card["is_return_visit"]
        # And neither may be empty -- parity between two blanks is not parity.
        assert body["service_type"]
        assert body["priority"]

    def test_alerts_are_the_customer_tag_names(self, app_and_db):
        client, db = app_and_db
        c = _seed_customer(db)
        _tag(db, c, "dog_warning")
        _tag(db, c, "gate_code")
        j = _job(db, c.id)
        assert _detail(client, j)["alerts"] == ["dog_warning", "gate_code"]

    def test_alerts_empty_not_missing_when_no_tags(self, app_and_db):
        """The key must always exist — the frontend does `job.alerts || []` but a
        missing key on a customer-less job would also skip the whole context row."""
        client, db = app_and_db
        c = _seed_customer(db)
        j = _job(db, c.id)
        assert _detail(client, j)["alerts"] == []

    def test_parts_summary_counts_by_status(self, app_and_db):
        client, db = app_and_db
        c = _seed_customer(db)
        j = _job(db, c.id)
        for status in ("needed", "needed", "ordered"):
            db.add(JobPartNeeded(
                id=str(uuid4()), company_id=TENANT, job_id=str(j.id),
                part_name="spring", status=status,
            ))
        db.commit()
        summary = _detail(client, j)["parts_summary"]
        assert summary["total"] == 3
        assert summary["needed"] == 2
        assert summary["ordered"] == 1
        assert summary["received"] == 0

    def test_parts_summary_is_zeroed_not_absent(self, app_and_db):
        client, db = app_and_db
        c = _seed_customer(db)
        j = _job(db, c.id)
        assert _detail(client, j)["parts_summary"] == {
            "total": 0, "needed": 0, "ordered": 0, "received": 0,
        }


class TestCustomerShapeParity:
    """The audit's A3 finding, pinned.

    A top-level key comparison passes while customer.email disappears. These
    assertions descend, and they assert the SUPERSET direction: the detail
    payload may gain _job_card's keys but must never lose its own.
    """

    def test_detail_customer_keeps_email_and_gains_notes_and_tags(self, app_and_db):
        client, db = app_and_db
        c = _seed_customer(db, notes="Beware of dog", email="ops@example.test")
        _tag(db, c, "gate_code")
        j = _job(db, c.id)
        cust = _detail(client, j)["customer"]
        # The half that _job_card does NOT have. Losing this deletes the mailto
        # row and re-offers "Add email" to a customer who has one.
        assert cust["email"] == "ops@example.test"
        # The half the detail payload did not have.
        assert cust["notes"] == "Beware of dog"
        assert [t["name"] for t in cust["tags"]] == ["gate_code"]

    def test_detail_customer_is_a_superset_of_the_route_card_customer(self, app_and_db):
        """Descends one level. A top-level parity check cannot see this."""
        client, db = app_and_db
        c = _seed_customer(db, notes="n")
        _tag(db, c, "dog_warning")
        j = _job(db, c.id)
        detail_keys = set(_detail(client, j)["customer"])

        today = client.get("/api/mobile/today")
        assert today.status_code == 200, today.text
        cards = today.json().get("jobs") or []
        assert cards, "today's route returned no card to compare against"
        card_keys = set(cards[0]["customer"])

        missing = card_keys - detail_keys
        assert not missing, f"detail customer is missing route-card keys: {sorted(missing)}"
        assert "email" in detail_keys, "detail lost customer.email"

    def test_all_three_surfaces_agree_on_the_customer_shape(self, app_and_db):
        """PR B: the jobs LIST was the third shape and the odd one out.

        It emitted flat customer_name/customer_address while /today and
        /job/{id} nested under _job_card, so one shared card component had to
        read both spellings to render either. All three now answer with the
        same nested customer, and email survives on every one of them.
        """
        client, db = app_and_db
        c = _seed_customer(db, notes="Beware of dog")
        _tag(db, c, "dog_warning")
        j = _job(db, c.id)

        detail = _detail(client, j)["customer"]
        cards = client.get("/api/mobile/today").json().get("jobs") or []
        assert cards, "today's route returned no card"
        today = cards[0]["customer"]
        listing = client.get("/api/mobile/jobs").json().get("jobs") or []
        assert listing, "jobs list returned nothing"
        listed = listing[0]["customer"]

        # The list must no longer answer in the old flat spelling at all —
        # leaving both is the divergence trap this exists to close.
        assert "customer_name" not in listing[0]
        assert "customer_address" not in listing[0]

        # Same key SPELLING everywhere — that is what the shared card needs.
        for shape in (detail, today, listed):
            assert shape["id"] == str(c.id)
            assert shape["name"] == "Acme"
            assert [t["name"] for t in shape["tags"]] == ["dog_warning"]

        # Content is deliberately NOT uniform, and this pins the asymmetry so
        # nobody "fixes" it later. The DETAIL screen is one job and carries the
        # contact record. The LIST runs to 500 rows and under scope=company
        # spans the whole customer book, so it ships identity + location only:
        # handing a tech every customer's phone, email and private notes to
        # render a card that displays none of them is a PII expansion with no
        # consumer. (Audit finding, 2026-08-22.)
        assert detail["email"] == "ops@example.test"
        assert detail["notes"] == "Beware of dog"
        assert "email" not in listed
        assert "phone" not in listed
        assert "notes" not in listed

    def test_list_carries_the_card_fields_a_shared_card_renders(self, app_and_db):
        client, db = app_and_db
        c = _seed_customer(db)
        _tag(db, c, "gate_code")
        j = _job(db, c.id, priority="Emergency", is_return_visit=True)
        row = (client.get("/api/mobile/jobs").json().get("jobs") or [None])[0]
        assert row is not None
        assert row["priority"] == "Emergency"
        assert row["is_return_visit"] is True
        assert row["alerts"] == ["gate_code"]
        assert "navigation_link" in row
        _ = j

    def test_top_level_card_keys_reach_the_detail_payload(self, app_and_db):
        client, db = app_and_db
        c = _seed_customer(db)
        j = _job(db, c.id, job_type="Install", priority="High")
        detail = _detail(client, j)
        cards = client.get("/api/mobile/today").json().get("jobs") or []
        assert cards
        # Keys the card has that the detail screen genuinely needs. Not the full
        # set: map/route-only keys (location, time_window, appointment_id) are
        # properties of a ROUTE, not of a job opened from anywhere.
        for key in ("service_type", "priority", "is_return_visit", "alerts",
                    "site_address", "navigation_link", "dispatch_status"):
            assert key in cards[0], f"route card lost {key}"
            assert key in detail, f"detail payload missing {key}"
