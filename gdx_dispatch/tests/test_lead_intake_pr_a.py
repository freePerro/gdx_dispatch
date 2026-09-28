"""Tests for PR A: Lead intake, follow-up dates, estimate linking, and permissions.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Lead
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.custom_fields import DEFAULT_LEAD_FIELDS, seed_default_lead_fields
from gdx_dispatch.routers.leads import (
    DEFAULT_LEAD_SOURCES,
    business_today,
    next_business_day,
)
from gdx_dispatch.routers.leads import (
    router as leads_router,
)

_ADMIN_UID = "00000000-0000-0000-0000-0000000000a1"
_TECH_UID = "00000000-0000-0000-0000-0000000000t1"
_SALES_UID = "00000000-0000-0000-0000-0000000000s1"
_NO_PERM_UID = "00000000-0000-0000-0000-0000000000n1"


def _make_client(
    role: str = "admin",
    uid: str = _ADMIN_UID,
    tenant_id: str = "tenant-test",
) -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS company_module_grants (
                id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT,
                UNIQUE(company_id, module_key)
            )
            """
        )
    )
    for mod in ("customers", "estimates", "jobs"):
        setup.execute(
            text(
                """
                INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
                VALUES (:id, :tid, :mod, datetime('now'), datetime('now'))
                """
            ),
            {"id": f"g-{mod}-{tenant_id}", "tid": tenant_id, "mod": mod},
        )
    setup.commit()
    # The real seeder, as bootstrap_app runs it on boot.
    seed_default_lead_fields(setup, tenant_id)
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(leads_router)
    app.dependency_overrides[get_db] = _override_db
    if role:
        app.dependency_overrides[get_current_user] = lambda: {
            "user_id": uid,
            "sub": uid,
            "role": role,
            "tenant_id": tenant_id,
        }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    tc._Session = Session  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def admin_client():
    tc = _make_client(role="admin", uid=_ADMIN_UID)
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


@pytest.fixture()
def tech_client():
    tc = _make_client(role="technician", uid=_TECH_UID)
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


@pytest.fixture()
def sales_client():
    tc = _make_client(role="sales", uid=_SALES_UID)
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Unit tests: next_business_day
# ---------------------------------------------------------------------------


def test_next_business_day_weekdays():
    settings = AppSettings(company_name="t1", timezone="America/Chicago", default_workdays=31)
    # Mon 2026-09-28 -> Tue 2026-09-29
    assert next_business_day(settings, from_date=date(2026, 9, 28)) == date(2026, 9, 29)
    # Thu 2026-10-01 -> Fri 2026-10-02
    assert next_business_day(settings, from_date=date(2026, 10, 1)) == date(2026, 10, 2)


def test_next_business_day_weekend_rolls_to_monday():
    settings = AppSettings(company_name="t1", timezone="America/Chicago", default_workdays=31)
    # Fri 2026-10-02 -> Mon 2026-10-05
    assert next_business_day(settings, from_date=date(2026, 10, 2)) == date(2026, 10, 5)
    # Sat 2026-10-03 -> Mon 2026-10-05
    assert next_business_day(settings, from_date=date(2026, 10, 3)) == date(2026, 10, 5)


def test_next_business_day_skips_holiday():
    settings = AppSettings(
        company_name="t1",
        timezone="America/Chicago",
        default_workdays=31,
        holiday_calendar=[{"name": "Labor Day", "date": "2026-09-07"}],
    )
    # Fri 2026-09-04 -> Mon is holiday 2026-09-07 -> Tue 2026-09-08
    assert next_business_day(settings, from_date=date(2026, 9, 4)) == date(2026, 9, 8)


# ---------------------------------------------------------------------------
# Intake Form Schema
# ---------------------------------------------------------------------------


def test_intake_form_accessible_by_tech(tech_client: TestClient):
    r = tech_client.get("/api/leads/intake-form")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "custom_fields" in data
    assert "sources" in data
    assert data["sources"] == DEFAULT_LEAD_SOURCES
    field_keys = {f["field_key"] for f in data["custom_fields"]}
    assert {"job_kind", "door_count", "door_size", "door_options", "opener"}.issubset(field_keys)


def test_intake_form_forbidden_for_unauthorized():
    client = _make_client(role="vendor", uid=_NO_PERM_UID)
    r = client.get("/api/leads/intake-form")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/leads/intake
# ---------------------------------------------------------------------------


def test_tech_creates_lead_intake_with_custom_fields(tech_client: TestClient):
    payload = {
        "name": "Jane Homeowner",
        "phone": "(612) 555-0144",
        "email": "jane.home@example.com",
        "address": "123 Elm St, Minneapolis, MN",
        "source": "Google",
        "notes": "Spring snapped this morning, 2-car garage stuck shut.",
        "custom_fields": {
            "job_kind": "Repair",
            "door_count": "1",
            "door_size": "16x7",
        },
    }
    r = tech_client.post("/api/leads/intake", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    lead = body["lead"]
    assert lead["id"]
    assert lead["name"] == "Jane Homeowner"
    assert lead["stage"] == "new"
    assert lead["assigned_to"] is None
    assert lead["follow_up_date"] is not None
    assert body["matched_customer"] is None
    assert body["possible_duplicate"] is None

    # A tech submits but does not browse: reading the answers back is refused.
    assert tech_client.get(f"/api/leads/{lead['id']}/custom-fields").status_code == 403

    from gdx_dispatch.routers.custom_fields import _list_values_for_entity

    with tech_client._Session() as db:  # type: ignore[attr-defined]
        cf_values = {
            f["field_key"]: f["value"]
            for f in _list_values_for_entity(db, "tenant-test", "lead", lead["id"])
        }
    assert cf_values["job_kind"] == "Repair"
    assert cf_values["door_count"] == "1"
    assert cf_values["door_size"] == "16x7"


def test_intake_duplicate_and_customer_matching(admin_client: TestClient):
    # 1. Create a customer
    Session = admin_client._Session  # type: ignore[attr-defined]
    with Session() as db:
        c = Customer(
            id=uuid4(),
            company_id="tenant-test",
            name="Existing Bob",
            phone="612-555-9988",
            email="bob@example.com",
            address="456 Oak Rd",
        )
        db.add(c)
        # Also create an open lead
        l1 = Lead(
            id=uuid4(),
            company_id="tenant-test",
            name="Existing Lead Alice",
            phone="612-555-1122",
            email="alice@example.com",
            origin_ref="WEB-1001",
            stage="new",
        )
        db.add(l1)
        db.commit()

    # 2. Intake matching existing customer phone
    r = admin_client.post(
        "/api/leads/intake",
        json={
            "name": "Bob Duplicate",
            "phone": "+1 (612) 555-9988",
            "notes": "Calling back",
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["matched_customer"] is not None
    assert data["matched_customer"]["name"] == "Existing Bob"

    # 3. Intake matching existing open lead origin_ref
    r2 = admin_client.post(
        "/api/leads/intake",
        json={
            "name": "Alice Repeat",
            "origin_ref": "WEB-1001",
            "notes": "Web form again",
        },
    )
    assert r2.status_code == 201
    data2 = r2.json()
    assert data2["possible_duplicate"] is not None
    assert data2["possible_duplicate"]["reason"] == "origin_ref"
    assert data2["possible_duplicate"]["name"] == "Existing Lead Alice"


# ---------------------------------------------------------------------------
# Follow-Up Filters & Summary
# ---------------------------------------------------------------------------


def test_follow_up_filters_and_summary(admin_client: TestClient):
    today = business_today()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    Session = admin_client._Session  # type: ignore[attr-defined]
    with Session() as db:
        l_overdue = Lead(
            id=uuid4(),
            company_id="tenant-test",
            name="Overdue Lead",
            stage="new",
            follow_up_date=yesterday,
        )
        l_today = Lead(
            id=uuid4(),
            company_id="tenant-test",
            name="Today Lead",
            stage="contacted",
            follow_up_date=today,
        )
        l_upcoming = Lead(
            id=uuid4(),
            company_id="tenant-test",
            name="Upcoming Lead",
            stage="qualified",
            follow_up_date=tomorrow,
        )
        # Won lead with past date should not count as overdue
        l_won = Lead(
            id=uuid4(),
            company_id="tenant-test",
            name="Won Lead",
            stage="won",
            follow_up_date=yesterday,
        )
        db.add_all([l_overdue, l_today, l_upcoming, l_won])
        db.commit()

    # Summary
    r_sum = admin_client.get("/api/leads/follow-up-summary")
    assert r_sum.status_code == 200
    summary = r_sum.json()
    assert summary["overdue"] == 1
    assert summary["due_today"] == 1

    # Filter: overdue
    r_od = admin_client.get("/api/leads?follow_up=overdue")
    assert r_od.status_code == 200
    names_od = [row["name"] for row in r_od.json()]
    assert "Overdue Lead" in names_od
    assert "Today Lead" not in names_od
    assert "Won Lead" not in names_od

    # Filter: today
    r_td = admin_client.get("/api/leads?follow_up=today")
    assert r_td.status_code == 200
    names_td = [row["name"] for row in r_td.json()]
    assert "Today Lead" in names_td
    assert "Overdue Lead" not in names_td

    # Filter: upcoming
    r_up = admin_client.get("/api/leads?follow_up=upcoming")
    assert r_up.status_code == 200
    names_up = [row["name"] for row in r_up.json()]
    assert "Upcoming Lead" in names_up
    assert "Today Lead" not in names_up


# ---------------------------------------------------------------------------
# PATCH follow_up_date
# ---------------------------------------------------------------------------


def test_patch_lead_follow_up_date(admin_client: TestClient):
    r_create = admin_client.post("/api/leads", json={"name": "Patch Test Lead"})
    lead_id = r_create.json()["id"]

    new_date = (business_today() + timedelta(days=5)).isoformat()
    r_patch = admin_client.patch(
        f"/api/leads/{lead_id}",
        json={"follow_up_date": new_date},
    )
    assert r_patch.status_code == 200
    assert r_patch.json()["follow_up_date"] == new_date

    # Clear follow_up_date
    r_clear = admin_client.patch(
        f"/api/leads/{lead_id}",
        json={"follow_up_date": None},
    )
    assert r_clear.status_code == 200
    assert r_clear.json()["follow_up_date"] is None


# ---------------------------------------------------------------------------
# Start Estimate (Decision 3 & 7: customer + jobsite filled, notes EMPTY)
# ---------------------------------------------------------------------------


def test_start_estimate_creates_draft_and_links(sales_client: TestClient):
    # Office role has leads.write and estimates.write
    r_create = sales_client.post(
        "/api/leads",
        json={
            "name": "Sarah Connor",
            "phone": "612-555-4321",
            "email": "sarah@example.com",
            "address": "789 Pine Way, Minneapolis, MN",
            "notes": "CONFIDENTIAL: Needs reinforced steel garage door.",
        },
    )
    assert r_create.status_code == 201
    lead_id = r_create.json()["id"]

    # Start estimate
    r_start = sales_client.post(f"/api/leads/{lead_id}/start-estimate")
    assert r_start.status_code == 200, r_start.text
    data = r_start.json()
    assert data["reused"] is False
    assert data["customer"]["name"] == "Sarah Connor"
    assert data["customer"]["status"] == "new"

    est = data["estimate"]
    assert est["status"] == "draft"
    assert est["customer_id"] == data["customer"]["id"]
    assert est["jobsite_address"] == "789 Pine Way, Minneapolis, MN"
    # Doug decision: INTAKE NOTES MUST NOT BE COPIED ONTO ESTIMATE
    assert est["notes"] is None or est["notes"] == ""
    assert est["description"] is None or est["description"] == ""

    # Check lead.estimate_id was updated
    r_lead = sales_client.get(f"/api/leads/{lead_id}")
    assert r_lead.json()["estimate_id"] == est["id"]

    # Check GET /api/leads/by-estimate/{estimate_id}
    r_by_est = sales_client.get(f"/api/leads/by-estimate/{est['id']}")
    assert r_by_est.status_code == 200
    lead_data = r_by_est.json()
    assert lead_data["id"] == lead_id
    assert "custom_fields" in lead_data

    # Idempotent re-call returns reused: True
    r_start_again = sales_client.post(f"/api/leads/{lead_id}/start-estimate")
    assert r_start_again.status_code == 200
    assert r_start_again.json()["reused"] is True
    assert r_start_again.json()["estimate"]["id"] == est["id"]


# ---------------------------------------------------------------------------
# Technician Permissions Gate (Decision 5: Techs submit only)
# ---------------------------------------------------------------------------


def test_technician_permission_gates(tech_client: TestClient):
    # Tech CAN access intake form
    assert tech_client.get("/api/leads/intake-form").status_code == 200

    # Tech CAN submit intake
    r_intake = tech_client.post(
        "/api/leads/intake",
        json={"name": "Tech Submit Lead", "phone": "555-1234"},
    )
    assert r_intake.status_code == 201
    lead_id = r_intake.json()["lead"]["id"]

    # Tech CANNOT view leads pipeline (no leads.read)
    assert tech_client.get("/api/leads").status_code == 403
    assert tech_client.get("/api/leads/follow-up-summary").status_code == 403

    # Tech CANNOT update lead (no leads.write)
    assert tech_client.patch(f"/api/leads/{lead_id}", json={"name": "Hacked"}).status_code == 403

    # Tech CANNOT start estimate (no leads.write / estimates.write)
    assert tech_client.post(f"/api/leads/{lead_id}/start-estimate").status_code == 403


# ---------------------------------------------------------------------------
# Planner Digest: Includes leads to call back and does not skip
# ---------------------------------------------------------------------------


def test_planner_digest_includes_leads(admin_client: TestClient):
    Session = admin_client._Session  # type: ignore[attr-defined]
    today = business_today()
    with Session() as db:
        urgent = Lead(
            id=uuid4(),
            company_id="tenant-test",
            name="Urgent Callback",
            phone="612-555-7766",
            stage="new",
            follow_up_date=today,
        )
        db.add(urgent)
        db.commit()

    from gdx_dispatch.tasks.planner_digest import send_planner_digest

    with (
        patch("gdx_dispatch.tasks.planner_digest.SessionLocal", Session),
        patch("gdx_dispatch.core.transactional_email.send_transactional_email") as mock_send,
    ):
        mock_send.return_value = (True, "mock", None)
        res = send_planner_digest(tenant_id="tenant-test", to_email="doug@example.com")
        assert res["status"] == "sent"
        assert res["leads_to_call"] >= 1

        # Check transactional email was called with leads count in subject and html
        assert mock_send.called
        kwargs = mock_send.call_args.kwargs
        assert "lead to call back" in kwargs["subject"]
        assert "Urgent Callback" in kwargs["html_body"]


# ---------------------------------------------------------------------------
# Review fixes (#817)
# ---------------------------------------------------------------------------


def _seed_customer_and_open_lead(Session) -> None:
    with Session() as db:
        db.add(Customer(
            id=uuid4(), company_id="tenant-test", name="Private Paula",
            phone="612-555-3030", email="paula@example.com", address="1 Hidden Ln",
        ))
        db.add(Lead(
            id=uuid4(), company_id="tenant-test", name="Open Olivia",
            phone="612-555-3030", stage="new",
        ))
        db.commit()


def test_tech_intake_does_not_reveal_who_owns_a_number(tech_client: TestClient):
    """A technician holds customers.read_own only. Typing a number into the
    intake form must not hand back the customer's (or another lead's) name
    and contact details — that would be a phone-number lookup service."""
    _seed_customer_and_open_lead(tech_client._Session)  # type: ignore[attr-defined]
    r = tech_client.post("/api/leads/intake", json={"name": "Probe", "phone": "6125553030"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["matched_customer"] is None
    assert body["possible_duplicate"] is None
    assert "Paula" not in r.text and "Olivia" not in r.text

    # The match is still recorded for the office, in the audit row.
    with tech_client._Session() as db:  # type: ignore[attr-defined]
        details = db.execute(text(
            "SELECT details FROM audit_logs WHERE action = 'lead_intake_created'"
        )).scalar_one()
    details = json.loads(details) if isinstance(details, str) else details
    assert details["matched_customer_id"] and details["possible_duplicate_id"]


def test_office_intake_still_sees_the_match(admin_client: TestClient):
    _seed_customer_and_open_lead(admin_client._Session)  # type: ignore[attr-defined]
    body = admin_client.post("/api/leads/intake", json={"name": "Probe", "phone": "6125553030"}).json()
    assert body["matched_customer"]["name"] == "Private Paula"
    assert body["possible_duplicate"]["name"] == "Open Olivia"


def test_a_deleted_default_field_stays_deleted(admin_client: TestClient):
    """The form used to re-create the defaults whenever no lead field was
    live, so an admin could never remove them."""
    with admin_client._Session() as db:  # type: ignore[attr-defined]
        db.execute(text(
            "UPDATE custom_field_definitions SET deleted_at = CURRENT_TIMESTAMP WHERE entity_type = 'lead'"
        ))
        db.commit()
    for _ in range(2):
        r = admin_client.get("/api/leads/intake-form")
        assert r.status_code == 200
        assert r.json()["custom_fields"] == []
    with admin_client._Session() as db:  # type: ignore[attr-defined]
        live = db.execute(text(
            "SELECT count(*) FROM custom_field_definitions WHERE entity_type = 'lead' AND deleted_at IS NULL"
        )).scalar_one()
    assert live == 0


def test_phone_dedupe_matches_the_trailing_ten_digits_only(admin_client: TestClient):
    """_find_matching_customer silently LINKS records at conversion, so it
    matches on the number's last ten digits — not anywhere inside it."""
    from gdx_dispatch.routers.leads import _find_matching_customer

    with admin_client._Session() as db:  # type: ignore[attr-defined]
        db.add(Customer(
            id=uuid4(), company_id="tenant-test", name="Ext Eddie",
            phone="612-555-4040 x12",
        ))
        db.commit()
        assert _find_matching_customer(db, email=None, phone="6125554040") is None
        db.add(Customer(id=uuid4(), company_id="tenant-test", name="Plain Pat", phone="(612) 555-5050"))
        db.commit()
        assert _find_matching_customer(db, email=None, phone="+1 612 555 5050").name == "Plain Pat"


def test_start_estimate_inherits_price_display_and_audits(sales_client: TestClient):
    lead_id = sales_client.post(
        "/api/leads", json={"name": "Audit Annie", "address": "2 Trail Rd"}
    ).json()["id"]
    r = sales_client.post(f"/api/leads/{lead_id}/start-estimate")
    assert r.status_code == 200, r.text
    est_id = r.json()["estimate"]["id"]
    cust_id = r.json()["customer"]["id"]

    with sales_client._Session() as db:  # type: ignore[attr-defined]
        est = db.get(Estimate, UUID(est_id))
        # NULL = inherit the tenant's total-only setting; False would force
        # line prices onto the customer's PDF.
        assert est.hide_line_prices is None
        rows = db.execute(text(
            "SELECT action, entity_id, tenant_id FROM audit_logs"
        )).fetchall()
    by_action = {row[0]: row for row in rows}
    assert {"customer_created", "estimate_created", "lead_estimate_started"} <= set(by_action)
    assert UUID(by_action["customer_created"][1]) == UUID(cust_id)
    assert all(by_action[a][2] == "tenant-test" for a in ("customer_created", "estimate_created", "lead_estimate_started"))


def test_convert_to_customer_writes_the_customer_row(sales_client: TestClient):
    lead_id = sales_client.post("/api/leads", json={"name": "Convert Carl"}).json()["id"]
    r = sales_client.post(f"/api/leads/{lead_id}/convert-to-customer")
    assert r.status_code == 200 and r.json()["converted"] is True
    with sales_client._Session() as db:  # type: ignore[attr-defined]
        actions = {a for (a,) in db.execute(text("SELECT action FROM audit_logs")).fetchall()}
    assert {"customer_created", "lead_converted_to_customer"} <= actions


def test_seeder_runs_once_and_is_audited():
    """bootstrap_app calls this on every boot: it must seed an existing
    install once, record that it did, and never put a deleted default back."""
    tc = _make_client(role="admin", uid=_ADMIN_UID)  # fixture already seeded once
    with tc._Session() as db:  # type: ignore[attr-defined]
        keys = {k for (k,) in db.execute(text(
            "SELECT field_key FROM custom_field_definitions WHERE entity_type = 'lead'"
        )).fetchall()}
        assert keys == {f["field_key"] for f in DEFAULT_LEAD_FIELDS}
        seeded_rows = db.execute(text(
            "SELECT count(*) FROM audit_logs WHERE action = 'lead_intake_fields_seeded'"
        )).scalar_one()
        assert seeded_rows == 1

        assert seed_default_lead_fields(db, "tenant-test") == 0  # a second boot
        db.execute(text("UPDATE custom_field_definitions SET deleted_at = CURRENT_TIMESTAMP"))
        db.commit()
        assert seed_default_lead_fields(db, "tenant-test") == 0  # after the admin deleted them
        live = db.execute(text(
            "SELECT count(*) FROM custom_field_definitions WHERE deleted_at IS NULL"
        )).scalar_one()
    assert live == 0
    tc._engine.dispose()  # type: ignore[attr-defined]
