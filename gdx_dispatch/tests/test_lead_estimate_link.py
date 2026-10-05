"""Estimates carry their lead; accepting one wins it — lead-to-paid PR A.

Real routers over a real
(SQLite) schema built from the ORM; the only override is the signed-in user.
"""
from __future__ import annotations

import ast
import pathlib
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Lead
from gdx_dispatch.modules.proposals.models import Estimate, ProposalTier
from gdx_dispatch.modules.proposals.router import router as proposals_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.estimates import router as estimates_router
from gdx_dispatch.routers.leads import router as leads_router

TENANT = "tenant-test"
UID = "00000000-0000-0000-0000-0000000000s1"


def _client(role: str = "sales") -> TestClient:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(text(
        "CREATE TABLE IF NOT EXISTS company_module_grants (id TEXT PRIMARY KEY, company_id TEXT, "
        "module_key TEXT, granted_at TEXT, created_at TEXT, expires_at TEXT, UNIQUE(company_id, module_key))"
    ))
    for mod in ("customers", "estimates", "jobs", "proposals"):
        setup.execute(text(
            "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, :mod, datetime('now'), datetime('now'))"
        ), {"id": f"g-{mod}", "tid": TENANT, "mod": mod})
    setup.commit()
    setup.close()

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def _tenant(request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(leads_router)
    app.include_router(estimates_router)
    app.include_router(proposals_router)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": UID, "sub": UID, "role": role, "tenant_id": TENANT,
    }
    tc = TestClient(app, raise_server_exceptions=True)
    tc._Session = Session  # type: ignore[attr-defined]
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def sales():
    tc = _client("sales")
    yield tc
    tc._engine.dispose()  # type: ignore[attr-defined]


def _db(tc):
    return tc._Session()  # type: ignore[attr-defined]


def _lead(tc, name="Pat Lead", phone="6125550100") -> str:
    r = tc.post("/api/leads", json={"name": name, "phone": phone, "stage": "new"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _started(tc, lead_id) -> dict:
    r = tc.post(f"/api/leads/{lead_id}/start-estimate")
    assert r.status_code == 200, r.text
    return r.json()


def _audits(tc, action):
    db = _db(tc)
    try:
        return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()
    finally:
        db.close()


def _row(tc, model, id_):
    db = _db(tc)
    try:
        return db.execute(select(model).where(model.id == UUID(str(id_)))).scalar_one()
    finally:
        db.close()


def _mark_sent(tc, est_id):
    r = tc.post(f"/api/estimates/{est_id}/mark-sent", json={"channel": "email"})
    assert r.status_code == 200, r.text


# ── §2 carrying the lead ───────────────────────────────────────────────────


def test_start_estimate_stamps_the_estimate_with_its_lead(sales):
    lead_id = _lead(sales)
    body = _started(sales, lead_id)
    assert body["estimate"]["lead_id"] == lead_id
    assert str(_row(sales, Estimate, body["estimate"]["id"]).lead_id) == lead_id


def test_create_estimate_for_the_leads_own_customer_links_it_and_audits(sales):
    lead_id = _lead(sales)
    cust = _started(sales, lead_id)["customer"]["id"]
    r = sales.post("/api/estimates", json={"customer_id": cust, "lead_id": lead_id})
    assert r.status_code == 201, r.text
    assert r.json()["lead_id"] == lead_id
    created = [a for a in _audits(sales, "estimate_created") if a.entity_id == r.json()["id"]]
    assert created and "lead_id" in str(created[0].details or created[0].payload or "")


def test_create_estimate_refuses_another_customers_lead(sales):
    lead_id = _lead(sales)
    _started(sales, lead_id)  # converts the lead to its customer
    db = _db(sales)
    other = Customer(name="Someone Else", company_id=TENANT)
    db.add(other)
    db.commit()
    other_id = str(other.id)
    db.close()
    r = sales.post("/api/estimates", json={"customer_id": other_id, "lead_id": lead_id})
    assert r.status_code == 422
    assert "not the lead's customer" in r.json()["detail"]


def test_create_estimate_refuses_an_unknown_lead(sales):
    db = _db(sales)
    c = Customer(name="Walk In", company_id=TENANT)
    db.add(c)
    db.commit()
    cid = str(c.id)
    db.close()
    r = sales.post("/api/estimates", json={
        "customer_id": cid, "lead_id": "99999999-9999-9999-9999-999999999999",
    })
    assert r.status_code == 422


def test_an_unconverted_lead_cannot_be_linked_to_anyone(sales):
    """No person to match yet — linking it would let a stranger's accept win it."""
    lead_id = _lead(sales)  # never converted
    db = _db(sales)
    stranger = Customer(name="Stranger", company_id=TENANT)
    db.add(stranger)
    db.commit()
    sid = str(stranger.id)
    db.close()
    r = sales.post("/api/estimates", json={"customer_id": sid, "lead_id": lead_id})
    assert r.status_code == 422
    assert "Convert the lead" in r.json()["detail"]


def test_a_deleted_pick_is_taken_by_the_next_accept(sales):
    lead_id = _lead(sales)
    first = _started(sales, lead_id)["estimate"]["id"]
    second = sales.post(f"/api/estimates/{first}/duplicate", json={}).json()["id"]
    _mark_sent(sales, first)
    sales.post(f"/api/estimates/{first}/accept", json={})
    assert sales.delete(f"/api/estimates/{first}").status_code in (200, 204)
    _mark_sent(sales, second)
    assert sales.post(f"/api/estimates/{second}/accept", json={}).status_code == 200
    assert str(_row(sales, Lead, lead_id).selected_estimate_id) == second
    assert sales.get(f"/api/leads/{lead_id}/estimates").json()["selected_estimate_id"] == second


def test_linking_needs_leads_write():
    tc = _client("accounting")  # leads.read, no leads.write
    try:
        db = _db(tc)
        lead = Lead(name="L", company_id=TENANT, stage="new")
        cust = Customer(name="C", company_id=TENANT)
        db.add_all([lead, cust])
        db.commit()
        lid, cid = str(lead.id), str(cust.id)
        db.close()
        r = tc.post("/api/estimates", json={"customer_id": cid, "lead_id": lid})
        assert r.status_code == 403
    finally:
        tc._engine.dispose()  # type: ignore[attr-defined]


def test_duplicate_keeps_the_lead(sales):
    lead_id = _lead(sales)
    est_id = _started(sales, lead_id)["estimate"]["id"]
    r = sales.post(f"/api/estimates/{est_id}/duplicate", json={})
    assert r.status_code == 201, r.text
    assert str(_row(sales, Estimate, r.json()["id"]).lead_id) == lead_id


def test_duplicate_without_leads_write_drops_the_link():
    """Duplicate must not be a way around the leads.write gate on create."""
    tc = _client("sales")
    try:
        lead_id = _lead(tc)
        est_id = _started(tc, lead_id)["estimate"]["id"]
        tc.app.dependency_overrides[get_current_user] = lambda: {
            "user_id": UID, "sub": UID, "role": "technician", "tenant_id": TENANT,
        }
        r = tc.post(f"/api/estimates/{est_id}/duplicate", json={})
        assert r.status_code == 201, r.text
        assert _row(tc, Estimate, r.json()["id"]).lead_id is None
    finally:
        tc._engine.dispose()  # type: ignore[attr-defined]


def test_reassigning_to_someone_else_clears_the_link_and_says_so(sales):
    lead_id = _lead(sales)
    est_id = _started(sales, lead_id)["estimate"]["id"]
    db = _db(sales)
    other = Customer(name="Other Person", company_id=TENANT)
    db.add(other)
    db.commit()
    oid = str(other.id)
    db.close()
    r = sales.post(f"/api/estimates/{est_id}/reassign-customer",
                   json={"customer_id": oid, "reason": "wrong household"})
    assert r.status_code == 200, r.text
    assert _row(sales, Estimate, est_id).lead_id is None
    rows = _audits(sales, "estimate_customer_reassigned")
    detail = str(rows[-1].details or rows[-1].payload or "")
    assert "estimate_lead_cleared" in detail and "lead_start_pointers_cleared" in detail
    assert detail.count(lead_id) == 2  # both links, both named


def test_reassigning_also_frees_the_leads_start_estimate(sales):
    """Both links go: the estimate's lead_id AND the lead's own pointer, or
    Start estimate on the lead reopens another customer's estimate."""
    lead_id = _lead(sales)
    est_id = _started(sales, lead_id)["estimate"]["id"]
    db = _db(sales)
    other = Customer(name="Other Household", company_id=TENANT)
    db.add(other)
    db.commit()
    oid = str(other.id)
    db.close()
    assert sales.post(f"/api/estimates/{est_id}/reassign-customer",
                      json={"customer_id": oid, "reason": "wrong household"}).status_code == 200
    assert sales.get(f"/api/leads/by-estimate/{est_id}").status_code == 404
    assert _row(sales, Lead, lead_id).estimate_id is None
    again = _started(sales, lead_id)
    assert again["reused"] is False
    assert again["estimate"]["id"] != est_id


def test_by_estimate_finds_the_lead_of_a_duplicate(sales):
    lead_id = _lead(sales)
    est_id = _started(sales, lead_id)["estimate"]["id"]
    copy_id = sales.post(f"/api/estimates/{est_id}/duplicate", json={}).json()["id"]
    r = sales.get(f"/api/leads/by-estimate/{copy_id}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == lead_id


# ── §3 accept wins the lead ────────────────────────────────────────────────


def test_accepting_wins_the_lead_and_selects_the_estimate(sales):
    lead_id = _lead(sales)
    est_id = _started(sales, lead_id)["estimate"]["id"]
    _mark_sent(sales, est_id)
    r = sales.post(f"/api/estimates/{est_id}/accept", json={})
    assert r.status_code == 200, r.text
    lead = _row(sales, Lead, lead_id)
    assert lead.stage == "won"
    assert str(lead.selected_estimate_id) == est_id
    won = _audits(sales, "lead_won")
    assert len(won) == 1 and won[0].entity_id == lead_id


def test_a_second_accept_keeps_the_first_pick_and_writes_nothing(sales):
    lead_id = _lead(sales)
    first = _started(sales, lead_id)["estimate"]["id"]
    second = sales.post(f"/api/estimates/{first}/duplicate", json={}).json()["id"]
    for est in (first, second):
        _mark_sent(sales, est)
        assert sales.post(f"/api/estimates/{est}/accept", json={}).status_code == 200
    assert str(_row(sales, Lead, lead_id).selected_estimate_id) == first
    assert len(_audits(sales, "lead_won")) == 1


def test_accept_tier_re_accept_does_not_move_the_pick(sales):
    """accept_tier re-stamps accepted_at on an already-accepted estimate —
    the reason the pick is stored rather than derived (plan audit)."""
    from gdx_dispatch.modules.proposals.service import accept_tier

    lead_id = _lead(sales)
    first = _started(sales, lead_id)["estimate"]["id"]
    second = sales.post(f"/api/estimates/{first}/duplicate", json={}).json()["id"]
    db = _db(sales)
    tiers = {}
    for est in (first, second):
        t = ProposalTier(estimate_id=UUID(est), tier_name="good", total_price=100)
        db.add(t)
        db.flush()
        tiers[est] = t.id
    db.commit()
    accept_tier(UUID(first), tiers[first], db, actor=UID)
    accept_tier(UUID(second), tiers[second], db, actor=UID)
    accept_tier(UUID(first), tiers[first], db, actor=UID)  # re-accept: newer accepted_at
    db.close()
    assert str(_row(sales, Lead, lead_id).selected_estimate_id) == first
    assert len(_audits(sales, "lead_won")) == 1


def test_tier_accept_route_still_returns_the_estimate():
    """The helper ends its own transaction; the route returns the ORM
    object it was handed — which must not come back expired and empty."""
    tc = _client("dispatcher")
    try:
        lead_id = _lead(tc)
        est = _started(tc, lead_id)["estimate"]["id"]
        db = _db(tc)
        tier = ProposalTier(estimate_id=UUID(est), tier_name="better", total_price=250)
        db.add(tier)
        db.commit()
        tier_id = str(tier.id)
        db.close()
        r = tc.post(f"/api/estimates/{est}/proposal/accept", json={"tier_id": tier_id})
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "accepted"
        assert r.json().get("id") == est
        assert _row(tc, Lead, lead_id).stage == "won"
    finally:
        tc._engine.dispose()  # type: ignore[attr-defined]


def test_a_lost_lead_whose_estimate_is_accepted_is_won_and_the_row_says_it_was_lost(sales):
    lead_id = _lead(sales)
    est_id = _started(sales, lead_id)["estimate"]["id"]
    assert sales.patch(f"/api/leads/{lead_id}", json={"stage": "lost"}).status_code == 200
    _mark_sent(sales, est_id)
    assert sales.post(f"/api/estimates/{est_id}/accept", json={}).status_code == 200
    assert _row(sales, Lead, lead_id).stage == "won"
    assert "lost" in str(_audits(sales, "lead_won")[-1].details or "")


def test_a_failed_lead_update_never_fails_the_accept(sales, monkeypatch):
    import gdx_dispatch.core.lead_estimates as le

    def boom(*a, **k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(le, "log_audit_event_sync", boom)
    lead_id = _lead(sales)
    est_id = _started(sales, lead_id)["estimate"]["id"]
    _mark_sent(sales, est_id)
    r = sales.post(f"/api/estimates/{est_id}/accept", json={})
    assert r.status_code == 200, r.text
    assert _row(sales, Estimate, est_id).status == "accepted"
    lead = _row(sales, Lead, lead_id)
    assert lead.stage != "won" and lead.selected_estimate_id is None
    assert _audits(sales, "lead_won_failed")


def test_an_estimate_without_a_lead_is_untouched(sales):
    db = _db(sales)
    c = Customer(name="No Lead", company_id=TENANT)
    db.add(c)
    db.commit()
    cid = str(c.id)
    db.close()
    est_id = sales.post("/api/estimates", json={"customer_id": cid}).json()["id"]
    _mark_sent(sales, est_id)
    assert sales.post(f"/api/estimates/{est_id}/accept", json={}).status_code == 200
    assert _audits(sales, "lead_won") == []


# ── §4 which estimate counts ───────────────────────────────────────────────


def test_lead_estimates_lists_them_with_the_pick(sales):
    lead_id = _lead(sales)
    first = _started(sales, lead_id)["estimate"]["id"]
    second = sales.post(f"/api/estimates/{first}/duplicate", json={}).json()["id"]
    _mark_sent(sales, first)
    sales.post(f"/api/estimates/{first}/accept", json={})
    r = sales.get(f"/api/leads/{lead_id}/estimates")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["id"] for e in body["estimates"]] == [first, second]
    assert body["selected_estimate_id"] == first
    assert {e["status"] for e in body["estimates"]} == {"accepted", "draft"}


def test_staff_can_move_the_pick_to_another_accepted_estimate_audited(sales):
    lead_id = _lead(sales)
    first = _started(sales, lead_id)["estimate"]["id"]
    second = sales.post(f"/api/estimates/{first}/duplicate", json={}).json()["id"]
    for est in (first, second):
        _mark_sent(sales, est)
        sales.post(f"/api/estimates/{est}/accept", json={})
    r = sales.put(f"/api/leads/{lead_id}/selected-estimate", json={"estimate_id": second})
    assert r.status_code == 200, r.text
    assert r.json()["selected_estimate_id"] == second
    rows = _audits(sales, "lead_selected_estimate_changed")
    assert len(rows) == 1
    assert first in str(rows[0].details) and second in str(rows[0].details)


def test_the_pick_must_be_this_leads_accepted_estimate(sales):
    lead_id = _lead(sales)
    first = _started(sales, lead_id)["estimate"]["id"]
    draft = sales.post(f"/api/estimates/{first}/duplicate", json={}).json()["id"]
    other_lead = _lead(sales, name="Other", phone="6125550199")
    other_est = _started(sales, other_lead)["estimate"]["id"]
    _mark_sent(sales, other_est)
    sales.post(f"/api/estimates/{other_est}/accept", json={})
    assert sales.put(f"/api/leads/{lead_id}/selected-estimate",
                     json={"estimate_id": draft}).status_code == 422
    assert sales.put(f"/api/leads/{lead_id}/selected-estimate",
                     json={"estimate_id": other_est}).status_code == 422


def test_null_clears_the_pick(sales):
    lead_id = _lead(sales)
    est = _started(sales, lead_id)["estimate"]["id"]
    _mark_sent(sales, est)
    sales.post(f"/api/estimates/{est}/accept", json={})
    r = sales.put(f"/api/leads/{lead_id}/selected-estimate", json={"estimate_id": None})
    assert r.status_code == 200
    assert r.json()["selected_estimate_id"] is None


def test_listing_needs_estimates_read_all():
    tc = _client("accounting")  # leads.read but no estimates.read_all
    try:
        db = _db(tc)
        lead = Lead(name="L", company_id=TENANT, stage="new")
        db.add(lead)
        db.commit()
        lid = str(lead.id)
        db.close()
        assert tc.get(f"/api/leads/{lid}/estimates").status_code == 403
    finally:
        tc._engine.dispose()  # type: ignore[attr-defined]


# ── guard: every accept wins its lead ──────────────────────────────────────

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _accept_writers():
    """(file, function) for every `<est|estimate>.status = "accepted"` outside
    tests and migrations."""
    hits = []
    for path in _ROOT.rglob("*.py"):
        rel = path.relative_to(_ROOT).as_posix()
        if rel.startswith(("tests/", "migrations/")) or "/node_modules/" in rel:
            continue
        src = path.read_text(encoding="utf-8", errors="ignore")
        if '"accepted"' not in src:
            continue
        tree = ast.parse(src)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == "accepted"
                    and any(
                        isinstance(t, ast.Attribute) and t.attr == "status"
                        and isinstance(t.value, ast.Name) and t.value.id in ("est", "estimate")
                        for t in node.targets
                    )
                ):
                    hits.append((rel, fn))
    return hits


def test_every_estimate_accept_calls_the_lead_helper():
    hits = _accept_writers()
    # Five known writers today. A new one must be wired, then counted here.
    assert len(hits) == 5, [(f, fn.name) for f, fn in hits]
    missing = [
        (f, fn.name) for f, fn in hits
        if not any(
            isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", None)) == "mark_lead_won_for_estimate"
            for n in ast.walk(fn)
        )
    ]
    assert missing == [], missing
