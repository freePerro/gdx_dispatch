"""Sprint tech_mobile Phase 2.1 — On-Truck Quoting tests.

Mirrors the test_mobile_api.py shape: SQLite tenant DB, seed a job +
tech + customer, invoke router functions directly, assert response
shape and DB side-effects.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  register all tenant models
from gdx_dispatch.modules.proposals import models as proposals_models  # noqa: F401  register Estimate/EstimateLine/ProposalTier
from gdx_dispatch.routers import mobile_quoting

_TEST_USER = {"user_id": "user-1", "role": "technician", "tenant_id": "tenant-a"}


def _as_json(response) -> dict:
    return json.loads(response.body)


def _request(tenant_id: str = "tenant-a") -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": tenant_id}
    req.state.tenant_id = tenant_id
    return req


@pytest.fixture()
def session_factory(tmp_path):
    db_file = tmp_path / "mobile_quoting_test.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SessionLocal
    engine.dispose()


def _seed(SessionLocal) -> dict[str, str]:
    """Seed a job + customer + tech with the user assigned via assigned_to."""
    db = SessionLocal()
    now = datetime.now(UTC)
    job_id = str(uuid4())
    customer_id = str(uuid4())
    try:
        db.execute(
            text(
                """
                INSERT INTO customers (id, name, phone, email, address, company_id)
                VALUES (:id, 'Acme Customer', '555-1111', 'a@example.com', '123 Main', 'tenant-a')
                """
            ),
            {"id": customer_id},
        )
        db.execute(
            text(
                """
                INSERT INTO technicians (id, company_id, user_id, active, created_at)
                VALUES ('tech-1', 'tenant-a', 'user-1', 1, :created_at)
                """
            ),
            {"created_at": now},
        )
        db.execute(
            text(
                """
                INSERT INTO jobs (
                    id, company_id, customer_id, title, dispatch_status,
                    assigned_to, scheduled_at, created_at
                ) VALUES (
                    :id, 'tenant-a', :customer_id, 'Garage Door Repair',
                    'on_site', 'user-1', :scheduled_at, :created_at
                )
                """
            ),
            {
                "id": job_id,
                "customer_id": customer_id,
                "scheduled_at": now,
                "created_at": now,
            },
        )
        db.commit()
        return {"job_id": job_id, "customer_id": customer_id}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Catalog / decline reason listings
# ---------------------------------------------------------------------------


def test_list_quote_services_returns_default_catalog(session_factory):
    db = session_factory()
    try:
        resp = mobile_quoting.list_quote_services(
            request=_request(), current_user=_TEST_USER, db=db,
        )
        body = _as_json(resp)
        services = body["services"]
        assert len(services) >= 5  # spring, opener, tune-up, cable, section, door
        spring = next(s for s in services if s["service"] == "spring_replacement")
        assert spring["label"] == "Spring Replacement"
        assert len(spring["tiers"]) == 3
        assert {t["id"] for t in spring["tiers"]} == {"good", "better", "best"}
    finally:
        db.close()


def test_list_decline_reasons_returns_default_taxonomy(session_factory):
    db = session_factory()
    try:
        resp = mobile_quoting.list_decline_reasons(
            request=_request(), current_user=_TEST_USER, db=db,
        )
        body = _as_json(resp)
        reasons = body["reasons"]
        assert "Priced too high" in reasons
        assert "Other" in reasons
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Build quote — happy path (preset)
# ---------------------------------------------------------------------------


def test_build_quote_from_preset_creates_estimate_with_three_tiers(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        resp = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="spring_replacement"),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert resp.status_code == 201, resp.body
        body = _as_json(resp)
        assert body["status"] == "sent"
        assert body["proposal_mode"] is True
        assert len(body["tiers"]) == 3
        tier_names = {t["tier_name"] for t in body["tiers"]}
        assert tier_names == {"good", "better", "best"}
        # Total = highest tier (Premium ≈ $639).
        assert body["total"] >= 600
        # Lines copied — one good, two better, three best lines from preset
        assert len(body["lines"]) >= 6
    finally:
        db.close()


def test_build_quote_unknown_service_400(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        resp = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="bogus_service"),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert resp.status_code == 400
    finally:
        db.close()


def test_build_quote_unauthorized_job_404(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        # Use a different user_id that's NOT assigned to the job.
        resp = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="spring_replacement"),
            request=_request(),
            current_user={"user_id": "other-user", "tenant_id": "tenant-a"},
            db=db,
        )
        assert resp.status_code == 404
    finally:
        db.close()


def test_build_quote_with_custom_tiers(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        payload = mobile_quoting.BuildQuoteIn(
            label="Custom Cable Job",
            tiers=[
                mobile_quoting.QuoteTierIn(
                    tier_name="good",
                    label="Basic",
                    description="Basic option",
                    line_items=[
                        mobile_quoting.QuoteLineIn(description="Cable", quantity=1, unit_price=50.0),
                    ],
                ),
                mobile_quoting.QuoteTierIn(
                    tier_name="better",
                    label="Plus",
                    description="Plus option",
                    line_items=[
                        mobile_quoting.QuoteLineIn(description="Cable", quantity=1, unit_price=80.0),
                        mobile_quoting.QuoteLineIn(description="Drum", quantity=1, unit_price=40.0),
                    ],
                ),
            ],
        )
        resp = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=payload,
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert resp.status_code == 201
        body = _as_json(resp)
        assert len(body["tiers"]) == 2
        assert body["total"] == 120.0  # better tier total
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Accept / decline lifecycle
# ---------------------------------------------------------------------------


def test_accept_quote_with_signature(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        # Build first
        build = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="tune_up"),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        quote = _as_json(build)
        better_tier = next(t for t in quote["tiers"] if t["tier_name"] == "better")

        sig_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        accept = mobile_quoting.accept_quote(
            estimate_id=quote["id"],
            payload=mobile_quoting.AcceptQuoteIn(
                chosen_tier_id=better_tier["id"],
                signature_data=sig_b64,
                signed_by="John Customer",
            ),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert accept.status_code == 200, accept.body
        body = _as_json(accept)
        assert body["status"] == "accepted"
        assert body["accepted_tier_id"] == better_tier["id"]
        assert body["signed_by"] == "John Customer"
        assert body["has_signature"] is True
        assert body["total"] == better_tier["total_price"]
    finally:
        db.close()


def test_accept_quote_required_signature_missing_400(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        build = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="cable_repair"),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        quote = _as_json(build)
        good = next(t for t in quote["tiers"] if t["tier_name"] == "good")
        resp = mobile_quoting.accept_quote(
            estimate_id=quote["id"],
            payload=mobile_quoting.AcceptQuoteIn(chosen_tier_id=good["id"]),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        # Default tech_mobile.signature_required_quote == "required"
        assert resp.status_code == 400
        assert "Signature is required" in _as_json(resp)["detail"]
    finally:
        db.close()


def test_accept_quote_idempotency_double_accept_409(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        build = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="opener_replacement"),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        quote = _as_json(build)
        best = next(t for t in quote["tiers"] if t["tier_name"] == "best")
        sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        first = mobile_quoting.accept_quote(
            estimate_id=quote["id"],
            payload=mobile_quoting.AcceptQuoteIn(chosen_tier_id=best["id"], signature_data=sig),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert first.status_code == 200
        second = mobile_quoting.accept_quote(
            estimate_id=quote["id"],
            payload=mobile_quoting.AcceptQuoteIn(chosen_tier_id=best["id"], signature_data=sig),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert second.status_code == 409
    finally:
        db.close()


def test_decline_quote_with_taxonomy_reason(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        build = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="spring_replacement"),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        quote = _as_json(build)
        resp = mobile_quoting.decline_quote(
            estimate_id=quote["id"],
            payload=mobile_quoting.DeclineQuoteIn(
                reason="Priced too high",
                notes="said it was double what they expected",
            ),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert resp.status_code == 200
        body = _as_json(resp)
        assert body["status"] == "declined"
        assert "Priced too high" in body["declined_reason"]
        assert "double what they expected" in body["declined_reason"]
    finally:
        db.close()


def test_decline_then_accept_blocked_409(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        build = mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="tune_up"),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        quote = _as_json(build)
        good = next(t for t in quote["tiers"] if t["tier_name"] == "good")
        d = mobile_quoting.decline_quote(
            estimate_id=quote["id"],
            payload=mobile_quoting.DeclineQuoteIn(reason="Other"),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert d.status_code == 200
        a = mobile_quoting.accept_quote(
            estimate_id=quote["id"],
            payload=mobile_quoting.AcceptQuoteIn(chosen_tier_id=good["id"], signature_data="x"),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        assert a.status_code == 409
    finally:
        db.close()


# ---------------------------------------------------------------------------
# List + get quote
# ---------------------------------------------------------------------------


def test_list_job_quotes_returns_built_quote(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        mobile_quoting.build_quote(
            job_id=seed["job_id"],
            payload=mobile_quoting.BuildQuoteIn(service="cable_repair"),
            request=_request(), current_user=_TEST_USER, db=db,
        )
        resp = mobile_quoting.list_job_quotes(
            job_id=seed["job_id"], request=_request(), current_user=_TEST_USER, db=db,
        )
        body = _as_json(resp)
        assert len(body["quotes"]) == 1
        assert body["quotes"][0]["status"] == "sent"
    finally:
        db.close()
