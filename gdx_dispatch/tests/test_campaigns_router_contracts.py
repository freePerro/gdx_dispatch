"""Direct-call contracts for the canonical campaigns router (#631).

`tests/test_marketing.py` used to pin list / create / send by importing them
from `modules/campaigns/router.py`. That copy was **shadowed** — `app.py`
includes `routers/campaigns.py` first, so FastAPI served the survivor and the
tests drove a module that never answered a request. The dead copy was deleted
2026-09-06 (#569) and its three tests went with it, leaving
`routers/campaigns.py` — the one that actually serves — with no direct test at
all, and its audit trail unproven by anything.

These are those three contracts, re-pinned against the handler that serves:
list, create, and send; a send writes an audit row; a send on an unknown
campaign 404s.

Direct-call rather than through TestClient, matching `test_marketing.py`: the
module gate and auth are dependencies, and what is under test here is handler
behaviour, not wiring. `test_marketing.py::test_module_requirements_wired_for_
segments_campaigns_loyalty` already pins the gate.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import ensure_audit_table
from gdx_dispatch.models.tenant_models import MarketingCampaign
from gdx_dispatch.routers.campaigns import (
    CampaignCreate,
    create_campaign,
    list_campaigns,
    send_campaign,
)

TENANT = "tenant-631"
USER_ID = "user-631"


def _request(tenant_id: str = TENANT) -> MagicMock:
    r = MagicMock()
    r.state.tenant = {"id": tenant_id}
    r.client.host = "127.0.0.1"
    return r


def _user() -> dict:
    return {"sub": USER_ID, "user_id": USER_ID, "email": "tester@example.com"}


def _body(response) -> dict | list:
    """JSONResponse -> parsed body. The handlers return JSONResponse, not dicts."""
    return json.loads(response.body.decode())


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    MarketingCampaign.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    ensure_audit_table(session)
    yield session
    session.close()


def _audit_rows(db, action: str | None = None) -> list[dict]:
    sql = "SELECT action, entity_type, entity_id, user_id, tenant_id FROM audit_logs"
    params: dict = {}
    if action is not None:
        sql += " WHERE action = :action"
        params["action"] = action
    return [dict(r._mapping) for r in db.execute(text(sql), params)]


def _make(db, **over) -> MarketingCampaign:
    now = datetime.now(UTC)
    c = MarketingCampaign(
        id=over.pop("id", uuid.uuid4()),
        company_id=over.pop("company_id", TENANT),
        name=over.pop("name", "Spring tune-up"),
        type=over.pop("type", "email"),
        status=over.pop("status", "draft"),
        audience=over.pop("audience", "all"),
        sent_count=0,
        created_at=now,
        updated_at=now,
        **over,
    )
    db.add(c)
    db.commit()
    return c


# ── create ────────────────────────────────────────────────────────────────


def test_create_campaign_persists_and_audits(db):
    resp = create_campaign(
        payload=CampaignCreate(name="Fall promo", type="email", status="draft", audience="all"),
        request=_request(),
        current_user=_user(),
        db=db,
    )
    assert resp.status_code == 201
    body = _body(resp)
    assert body["name"] == "Fall promo"

    stored = db.query(MarketingCampaign).filter_by(name="Fall promo").one()
    assert str(stored.company_id) == TENANT
    assert stored.sent_count == 0

    rows = _audit_rows(db, "campaign_created")
    assert len(rows) == 1, f"expected exactly one audit row, got {rows}"
    assert rows[0]["entity_id"] == str(stored.id)
    assert rows[0]["user_id"] == USER_ID
    assert rows[0]["entity_type"] == "campaign"


def test_create_campaign_rejects_unknown_type_without_writing(db):
    resp = create_campaign(
        payload=CampaignCreate(name="Bad", type="carrier-pigeon", status="draft"),
        request=_request(),
        current_user=_user(),
        db=db,
    )
    assert resp.status_code == 400
    assert db.query(MarketingCampaign).count() == 0
    assert _audit_rows(db) == []


# ── list ──────────────────────────────────────────────────────────────────


def test_list_campaigns_returns_the_connections_rows(db):
    """No company_id filter, deliberately — and that is NOT a leak.

    `list_campaigns` carries the comment "Three-plane (2026-04-24 B1): tenant
    isolation is the connection; company_id filter removed", which is the
    single-tenant rule in CLAUDE.md: one tenant per database, isolation is the
    connection, never design for multi-tenancy. A first draft of this test
    asserted cross-tenant filtering and failed — the test was wrong, not the
    handler. Pinned here so the next reader does not re-add that filter and
    call it a fix; `tests/test_saas_surfaces_retired.py` scans for exactly that
    redundancy.
    """
    _make(db, name="First")
    _make(db, name="Second")

    body = _body(list_campaigns(request=_request(), current_user=_user(), db=db))
    assert {c["name"] for c in body["items"]} == {"First", "Second"}
    assert body["total"] == 2


def test_list_campaigns_excludes_soft_deleted(db):
    _make(db, name="Live")
    _make(db, name="Gone", deleted_at=datetime.now(UTC))

    body = _body(list_campaigns(request=_request(), current_user=_user(), db=db))
    assert {c["name"] for c in body["items"]} == {"Live"}


# ── send ──────────────────────────────────────────────────────────────────


def test_send_campaign_marks_sending_and_writes_an_audit_row(db):
    campaign = _make(db, name="Blast", type="email", audience="all")

    resp = send_campaign(
        campaign_id=str(campaign.id),
        request=_request(),
        current_user=_user(),
        db=db,
    )
    assert resp.status_code == 200
    body = _body(resp)
    assert body["status"] == "sending"
    assert body["sent_count"] == 1

    db.refresh(campaign)
    assert campaign.status == "sending"
    assert campaign.last_sent_at is not None

    rows = _audit_rows(db, "campaign_send_triggered")
    assert len(rows) == 1, f"a send must leave exactly one audit row, got {rows}"
    assert rows[0]["entity_id"] == str(campaign.id)
    assert rows[0]["user_id"] == USER_ID
    assert rows[0]["tenant_id"] == TENANT


def test_send_campaign_404s_on_unknown_id(db):
    resp = send_campaign(
        campaign_id=str(uuid.uuid4()),
        request=_request(),
        current_user=_user(),
        db=db,
    )
    assert resp.status_code == 404
    assert _audit_rows(db) == [], "a 404 must not write an audit row"


def test_send_campaign_404s_on_malformed_id(db):
    """`_validate_uuid` guards before the query — a non-UUID is 404, not a 500."""
    resp = send_campaign(
        campaign_id="not-a-uuid",
        request=_request(),
        current_user=_user(),
        db=db,
    )
    assert resp.status_code == 404
    assert _audit_rows(db) == []


def test_send_campaign_refuses_to_resend_an_in_flight_campaign(db):
    """status in (sending, completed) is 409 — the double-send guard."""
    campaign = _make(db, name="Already going", status="sending")

    resp = send_campaign(
        campaign_id=str(campaign.id),
        request=_request(),
        current_user=_user(),
        db=db,
    )
    assert resp.status_code == 409

    db.refresh(campaign)
    assert campaign.sent_count == 0, "a refused re-send must not bump sent_count"
    assert _audit_rows(db, "campaign_send_triggered") == []
