"""Good/better/best tiers — the surviving proposal system.

This file used to test /api/proposals, a standalone CRUD router over a flat
`proposals` table (six tier columns, no line items, no estimate number, no tax)
whose line-items endpoint was a stub that persisted nothing. Migration 061
retired it. Tiers now hang off a real Estimate — `proposal_mode` plus rows in
`proposal_tiers` — so they inherit numbering, tax, lines, the customer token and
job conversion.

Mounts the estimates router AND the proposals module router on one app: the
editor drives both (PATCH the estimate to flip proposal_mode, then POST/PATCH/
DELETE tiers), so the interesting failures are at the seam between them.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.modules.proposals.models import Estimate, ProposalTier
from gdx_dispatch.modules.proposals.router import router as proposals_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.estimates import router as estimates_router

TENANT = "tenant-test"


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = Session()
    setup.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT
        )
    """))
    setup.execute(text("""
        CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key)
        )
    """))
    # require_module("proposals") normalizes to the "estimates" grant via
    # LEGACY_MODULE_ALIASES — granting "estimates" is what unlocks the tiers.
    setup.execute(text("""
        INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)
        VALUES ('g1', 'tenant-test', 'estimates', datetime('now'), datetime('now'))
    """))
    setup.execute(text("""
        INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
        VALUES ('g2', 'tenant-test', 'estimates', datetime('now'), datetime('now'))
    """))
    setup.commit()
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
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(estimates_router)
    app.include_router(proposals_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1", "sub": "user-1", "role": "admin", "tenant_id": TENANT,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def _create_customer(client: TestClient, name: str = "Acme Customer") -> str:
    db = next(client.app.dependency_overrides[get_db]())
    try:
        row = Customer(name=name, email="customer@example.com", company_id=TENANT)
        db.add(row)
        db.commit()
        db.refresh(row)
        return str(row.id)
    finally:
        db.close()


def _create_estimate(client: TestClient, **overrides) -> dict:
    # The create route requires job_id or customer_id.
    payload = {"label": "Tiered install", "customer_id": _create_customer(client)}
    payload.update(overrides)
    r = client.post("/api/estimates", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _db(client: TestClient):
    return next(client.app.dependency_overrides[get_db]())


def _add_tier(client: TestClient, est_id: str, name: str, **overrides) -> dict:
    body = {"tier_name": name, "total_price": 2500.0, "warranty_months": 12, "description": f"{name} package"}
    body.update(overrides)
    r = client.post(f"/api/estimates/{est_id}/proposal-tiers", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── the estimate carries the flag ────────────────────────────────────────────

def test_serializer_exposes_proposal_mode_and_accepted_tier(client: TestClient):
    """Both were unserialized, so the office UI could not see (let alone edit) a
    tiered estimate that mobile had created."""
    est = _create_estimate(client)
    assert est["proposal_mode"] is False
    assert est["accepted_tier_id"] is None


def test_patch_toggles_proposal_mode(client: TestClient):
    est = _create_estimate(client)
    r = client.patch(f"/api/estimates/{est['id']}", json={"proposal_mode": True})
    assert r.status_code == 200, r.text
    assert r.json()["proposal_mode"] is True

    r = client.patch(f"/api/estimates/{est['id']}", json={"proposal_mode": False})
    assert r.json()["proposal_mode"] is False


def test_patch_rejects_null_proposal_mode(client: TestClient):
    """proposal_mode is NOT NULL. Typed `bool | None` (like the tri-state
    hide_line_prices beside it) an explicit null would sail through the generic
    setattr loop and write NULL into the column."""
    est = _create_estimate(client)
    r = client.patch(f"/api/estimates/{est['id']}", json={"proposal_mode": None})
    assert r.status_code == 422, r.text


def test_patch_without_proposal_mode_leaves_it_alone(client: TestClient):
    """exclude_unset, not the field default, is what makes omission a no-op —
    the default is False, so a bug here would silently untier an estimate on any
    unrelated edit."""
    est = _create_estimate(client)
    client.patch(f"/api/estimates/{est['id']}", json={"proposal_mode": True})
    r = client.patch(f"/api/estimates/{est['id']}", json={"label": "renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["proposal_mode"] is True


# ── tier CRUD ────────────────────────────────────────────────────────────────

def test_create_list_and_order_tiers(client: TestClient):
    est = _create_estimate(client)
    _add_tier(client, est["id"], "best", total_price=5200.0)
    _add_tier(client, est["id"], "good", total_price=2500.0)
    _add_tier(client, est["id"], "better", total_price=3800.0)

    rows = client.get(f"/api/estimates/{est['id']}/proposal").json()
    # display_order is derived from the name, so the customer always sees
    # good → better → best regardless of the order they were entered.
    assert [t["tier_name"] for t in rows] == ["good", "better", "best"]


def test_second_post_for_a_name_updates_rather_than_duplicates(client: TestClient):
    """One card per tier name. A blind insert let a second POST stack a duplicate
    that the customer PDF then rendered twice."""
    est = _create_estimate(client)
    first = _add_tier(client, est["id"], "better", total_price=3800.0)
    second = _add_tier(client, est["id"], "better", total_price=3950.0, description="revised")

    assert second["id"] == first["id"]
    rows = client.get(f"/api/estimates/{est['id']}/proposal").json()
    assert len(rows) == 1
    assert float(rows[0]["total_price"]) == 3950.0
    assert rows[0]["description"] == "revised"


def test_patch_tier_updates_only_sent_fields(client: TestClient):
    est = _create_estimate(client)
    tier = _add_tier(client, est["id"], "good", total_price=2500.0, warranty_months=12, description="steel")

    r = client.patch(f"/api/estimates/{est['id']}/proposal-tiers/{tier['id']}", json={"total_price": 2650.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert float(body["total_price"]) == 2650.0
    assert body["warranty_months"] == 12       # untouched
    assert body["description"] == "steel"      # untouched


def test_patch_cannot_rename_a_tier_onto_an_existing_name(client: TestClient):
    """The POST path upserts so it can't duplicate a name — but PATCH accepts
    tier_name too, and there is no DB unique on (estimate_id, tier_name), so a
    rename was the back door to the same two-cards-on-the-PDF bug."""
    est = _create_estimate(client)
    _add_tier(client, est["id"], "good", total_price=2500.0)
    better = _add_tier(client, est["id"], "better", total_price=3800.0)

    r = client.patch(f"/api/estimates/{est['id']}/proposal-tiers/{better['id']}", json={"tier_name": "good"})
    assert r.status_code == 409, r.text

    rows = client.get(f"/api/estimates/{est['id']}/proposal").json()
    assert sorted(t["tier_name"] for t in rows) == ["better", "good"]


def test_patch_can_rename_a_tier_to_a_free_name(client: TestClient):
    """The clash guard must not block a legitimate rename — display_order has to
    follow the new name so the customer still sees good → better → best."""
    est = _create_estimate(client)
    good = _add_tier(client, est["id"], "good", total_price=2500.0)

    r = client.patch(f"/api/estimates/{est['id']}/proposal-tiers/{good['id']}", json={"tier_name": "best"})
    assert r.status_code == 200, r.text
    assert r.json()["tier_name"] == "best"
    assert r.json()["display_order"] == 2


def test_delete_tier(client: TestClient):
    est = _create_estimate(client)
    tier = _add_tier(client, est["id"], "good")
    r = client.delete(f"/api/estimates/{est['id']}/proposal-tiers/{tier['id']}")
    assert r.status_code == 204, r.text
    assert client.get(f"/api/estimates/{est['id']}/proposal").json() == []


def test_bad_tier_name_is_422_not_500(client: TestClient):
    """tier_name is a DB enum. Typed as a bare `str` it reached the driver and
    came back as a 500 instead of a validation error."""
    est = _create_estimate(client)
    r = client.post(f"/api/estimates/{est['id']}/proposal-tiers", json={"tier_name": "platinum", "total_price": 1.0})
    assert r.status_code == 422, r.text


def test_tier_on_missing_estimate_is_404(client: TestClient):
    r = client.post(f"/api/estimates/{uuid4()}/proposal-tiers", json={"tier_name": "good", "total_price": 1.0})
    assert r.status_code == 404, r.text


# ── the money guards ─────────────────────────────────────────────────────────

def test_tiers_are_locked_once_the_estimate_is_accepted(client: TestClient):
    """Re-pricing a tier after acceptance rewrites what the customer agreed to
    (and what the invoice was drafted from). Same rule as _ensure_editable."""
    est = _create_estimate(client)
    tier = _add_tier(client, est["id"], "good", total_price=2500.0)

    db = _db(client)
    try:
        row = db.get(Estimate, UUID(est["id"]))
        row.status = "accepted"
        db.commit()
    finally:
        db.close()

    assert client.post(f"/api/estimates/{est['id']}/proposal-tiers",
                       json={"tier_name": "better", "total_price": 1.0}).status_code == 409
    assert client.patch(f"/api/estimates/{est['id']}/proposal-tiers/{tier['id']}",
                        json={"total_price": 9999.0}).status_code == 409
    assert client.delete(f"/api/estimates/{est['id']}/proposal-tiers/{tier['id']}").status_code == 409


def test_cannot_delete_the_accepted_tier(client: TestClient):
    """accepted_tier_id is an FK and the invoice is drafted from the accepted
    tier's price. Belt-and-braces for an estimate reopened to draft with a stale
    accepted_tier_id still on it."""
    est = _create_estimate(client)
    tier = _add_tier(client, est["id"], "good", total_price=2500.0)

    db = _db(client)
    try:
        row = db.get(Estimate, UUID(est["id"]))
        row.accepted_tier_id = UUID(tier["id"])
        row.status = "draft"          # reopened, but the pointer survived
        db.commit()
    finally:
        db.close()

    r = client.delete(f"/api/estimates/{est['id']}/proposal-tiers/{tier['id']}")
    assert r.status_code == 409, r.text
    assert client.get(f"/api/estimates/{est['id']}/proposal").json()  # still there


def test_accept_records_the_chosen_tier(client: TestClient):
    est = _create_estimate(client)
    tier = _add_tier(client, est["id"], "better", total_price=3800.0)

    r = client.post(f"/api/estimates/{est['id']}/proposal/accept", json={"tier_id": tier["id"]})
    assert r.status_code == 200, r.text

    db = _db(client)
    try:
        row = db.get(Estimate, UUID(est["id"]))
        assert row.status == "accepted"
        assert str(row.accepted_tier_id) == tier["id"]
    finally:
        db.close()


# ── the public customer link ─────────────────────────────────────────────────

def test_public_proposal_never_leaks_internal_fields(client: TestClient):
    """This endpoint is public and unauthenticated. It was shadowed by the old
    /api/proposals/{proposal_id} handler and never actually served; retiring
    that router made it live, and it returned the ORM row whole — including the
    office's internal `notes`, company_id, and a copy of the token itself."""
    est = _create_estimate(client, notes="Customer haggles — do not go below 3200. Ask for cash.")
    client.patch(f"/api/estimates/{est['id']}", json={"proposal_mode": True})
    _add_tier(client, est["id"], "good", total_price=2500.0)

    db = _db(client)
    try:
        row = db.get(Estimate, UUID(est["id"]))
        row.sent_at = datetime.now(UTC)   # the route only serves sent estimates
        db.commit()
        token = row.public_token
    finally:
        db.close()

    body = client.get(f"/api/proposals/{token}").json()
    assert body["estimate"]["estimate_number"] == est["estimate_number"]
    assert [t["tier_name"] for t in body["tiers"]] == ["good"]

    leaked = {"notes", "company_id", "public_token", "id", "customer_id", "job_id"}
    assert leaked.isdisjoint(body["estimate"].keys()), f"leaked: {leaked & body['estimate'].keys()}"
    assert "haggles" not in str(body)


def test_public_proposal_rejects_an_unknown_token(client: TestClient):
    assert client.get("/api/proposals/not-a-real-token").status_code == 404


def test_public_proposal_hides_unsent_and_soft_deleted_estimates(client: TestClient):
    """public_token is minted at CREATE, not at send. Without deleted_at +
    sent_at in the lookup, every draft and every soft-deleted estimate is a live
    public URL the moment this route stops being shadowed. Both must 404, and
    with the same shape as a bad token so the response can't be used to probe."""
    est = _create_estimate(client)

    db = _db(client)
    try:
        token = db.get(Estimate, UUID(est["id"])).public_token
    finally:
        db.close()

    # Never sent → not public, even with a valid token.
    assert client.get(f"/api/proposals/{token}").status_code == 404

    # Sent → now readable.
    db = _db(client)
    try:
        row = db.get(Estimate, UUID(est["id"]))
        row.sent_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()
    assert client.get(f"/api/proposals/{token}").status_code == 200

    # Soft-deleted → gone again, despite still being "sent".
    db = _db(client)
    try:
        row = db.get(Estimate, UUID(est["id"]))
        row.deleted_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()
    assert client.get(f"/api/proposals/{token}").status_code == 404


def test_tier_writes_land_in_the_audit_trail(client: TestClient):
    """Tier writes change the priced offer. accept_tier always logged; create/
    update/delete did not, so a tier could be re-priced with no record of who."""
    from gdx_dispatch.core.audit import AuditLog

    est = _create_estimate(client)
    tier = _add_tier(client, est["id"], "good", total_price=2500.0)
    client.patch(f"/api/estimates/{est['id']}/proposal-tiers/{tier['id']}", json={"total_price": 9100.0})
    client.delete(f"/api/estimates/{est['id']}/proposal-tiers/{tier['id']}")

    db = _db(client)
    try:
        actions = [
            r.action for r in db.execute(
                select(AuditLog).where(AuditLog.entity_id == est["id"])
            ).scalars().all()
        ]
    finally:
        db.close()
    for expected in ("proposal_tier_created", "proposal_tier_updated", "proposal_tier_deleted"):
        assert expected in actions, f"{expected} missing from audit trail: {actions}"


def test_standalone_proposals_router_is_gone(client: TestClient):
    """Migration 061 retired the parallel `proposals` table and its CRUD router.
    Guard against someone reviving it: /api/proposals is now owned solely by the
    public token lookup, which is what {token} matches here."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("gdx_dispatch.routers.proposals")

    from gdx_dispatch import models as models_pkg
    assert not hasattr(models_pkg, "Proposal")

    # POST/PATCH/DELETE on the collection no longer exist (only GET {token}).
    assert client.post("/api/proposals", json={"title": "x"}).status_code in (404, 405)


def test_tiers_survive_an_estimate_soft_delete(client: TestClient):
    """Tiers are NOT cascade-deleted with the estimate (unlike estimate_lines,
    which declare delete-orphan). Because estimates are soft-deleted, that is
    correct today — the row is still there to restore. It becomes an orphan bug
    the moment anything hard-deletes an estimate, so this pins the coupling and
    the name says what it asserts.

    Note this runs on SQLite, where FK enforcement is off by default; it is
    asserting ORM/cascade behavior, not a database-level constraint."""
    est = _create_estimate(client)
    _add_tier(client, est["id"], "good")

    r = client.delete(f"/api/estimates/{est['id']}")
    assert r.status_code in (200, 204), r.text

    db = _db(client)
    try:
        rows = db.execute(select(ProposalTier).where(ProposalTier.estimate_id == UUID(est["id"]))).scalars().all()
        assert len(rows) == 1  # soft delete leaves the tier attached
    finally:
        db.close()


# ── the public approval surface (accept / decline / deposit) ────────────────
# The QuickBooks-style page: unauthenticated, token-addressed. get_features is
# monkeypatched everywhere below — its real read goes through SessionLocal
# (not the fixture session) and its deposit_pct DEFAULTS TO 50, so leaving it
# live would make deposit behavior depend on the host environment.

from types import SimpleNamespace  # noqa: E402

import gdx_dispatch.modules.proposals.router as proposals_router_module  # noqa: E402
from gdx_dispatch.models.tenant_models import Invoice  # noqa: E402
from gdx_dispatch.modules.proposals.models import EstimateLine  # noqa: E402


def _fake_features(monkeypatch, *, deposit_pct=0, hide_line_prices=False):
    monkeypatch.setattr(
        proposals_router_module,
        "get_features",
        lambda tenant_id: SimpleNamespace(
            deposit_pct=deposit_pct, hide_line_prices=hide_line_prices
        ),
    )


def _publish(client: TestClient, est_id: str, status: str = "sent", **fields) -> str:
    """Make an estimate publicly addressable (sent_at + status) and return
    its token."""
    db = _db(client)
    try:
        row = db.get(Estimate, UUID(est_id))
        row.sent_at = datetime.now(UTC)
        row.status = status
        for k, v in fields.items():
            setattr(row, k, v)
        db.commit()
        return row.public_token
    finally:
        db.close()


def _add_lines(client: TestClient, est_id: str, *prices: float) -> None:
    db = _db(client)
    try:
        for i, p in enumerate(prices, 1):
            db.add(EstimateLine(
                estimate_id=UUID(est_id), description=f"Line {i}", quantity=1,
                unit_price=p, line_total=p, sort_order=i, company_id=TENANT,
            ))
        row = db.get(Estimate, UUID(est_id))
        row.total = sum(prices)
        db.commit()
    finally:
        db.close()


def _deposit_rows(client: TestClient, est_id: str) -> list[dict]:
    db = _db(client)
    try:
        rows = db.execute(
            select(Invoice).where(
                Invoice.estimate_id == UUID(est_id),
                Invoice.billing_type == "deposit",
            )
        ).scalars().all()
        return [{"total": float(r.total), "status": r.status} for r in rows]
    finally:
        db.close()


def test_public_payload_totals_are_tax_inclusive_for_line_estimates(client: TestClient, monkeypatch):
    """The old payload returned est.total — the PRE-TAX line subtotal — labeled
    "total". The page must show the same number as the PDF and email."""
    _fake_features(monkeypatch)
    est = _create_estimate(client)
    _add_lines(client, est["id"], 2450.0, 150.0)
    token = _publish(client, est["id"], tax_rate=0.07)

    body = client.get(f"/api/proposals/{token}").json()
    assert body["totals"]["subtotal"] == pytest.approx(2600.0)
    assert body["totals"]["total"] == pytest.approx(2782.0)  # 2600 * 1.07
    assert [ln["line_total"] for ln in body["lines"]] == [2450.0, 150.0]
    # The mislabeled pre-tax "total" must not reappear on the estimate dict.
    assert "total" not in body["estimate"]


def test_public_payload_proposal_mode_has_no_single_total(client: TestClient, monkeypatch):
    """est.total can be the HIGHEST tier (mobile builder) and the totals engine
    is tier-blind — any single number would show the best-tier price to a
    customer picking good. Per-tier prices only. Also pins the
    stripe_payment_link removal: a payment path that bypasses the deposit."""
    _fake_features(monkeypatch)
    est = _create_estimate(client)
    client.patch(f"/api/estimates/{est['id']}", json={"proposal_mode": True})
    _add_tier(client, est["id"], "good", total_price=2500.0)
    _add_tier(client, est["id"], "best", total_price=8000.0)
    token = _publish(client, est["id"])

    body = client.get(f"/api/proposals/{token}").json()
    assert "totals" not in body
    assert [t["total_price"] for t in body["tiers"]] == [2500.0, 8000.0]
    assert all("stripe_payment_link" not in t for t in body["tiers"])


def test_public_payload_company_block_and_masked_rejected(client: TestClient, monkeypatch):
    """A bounced estimate reads 'sent' to the customer — 'rejected' would read
    as "we rejected YOU". The company block feeds the page header."""
    _fake_features(monkeypatch)
    est = _create_estimate(client)
    token = _publish(client, est["id"], status="rejected")

    body = client.get(f"/api/proposals/{token}").json()
    assert body["estimate"]["status"] == "sent"
    assert set(body["company"].keys()) == {"name", "phone"}


def test_public_payload_hides_line_prices_when_estimate_says_so(client: TestClient, monkeypatch):
    """Tri-state: the per-estimate override wins over the tenant default, and
    prices are STRIPPED server-side — hiding in the template would still leak
    them in the network response."""
    _fake_features(monkeypatch, hide_line_prices=False)
    est = _create_estimate(client)
    _add_lines(client, est["id"], 999.0)
    token = _publish(client, est["id"], hide_line_prices=True)

    body = client.get(f"/api/proposals/{token}").json()
    assert body["estimate"]["hide_line_prices"] is True
    assert body["lines"] and "unit_price" not in body["lines"][0]
    assert "999" not in str(body["lines"])


def test_public_accept_flips_status_creates_job_and_deposit(client: TestClient, monkeypatch):
    """The one-motion flow: accept → job on the dispatch board + deposit
    invoice at the tenant's % Down, its summary in the response."""
    _fake_features(monkeypatch, deposit_pct=50)
    est = _create_estimate(client)
    _add_lines(client, est["id"], 2600.0)
    token = _publish(client, est["id"])

    r = client.post(f"/api/proposals/{token}/accept", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["estimate"]["status"] == "accepted"
    assert body["deposit"]["amount"] == pytest.approx(1300.0)
    assert body["deposit"]["pct"] == 50

    db = _db(client)
    try:
        row = db.get(Estimate, UUID(est["id"]))
        assert row.status == "accepted"
        assert row.job_id is not None
    finally:
        db.close()
    assert len(_deposit_rows(client, est["id"])) == 1


def test_public_accept_no_deposit_when_pct_zero(client: TestClient, monkeypatch):
    _fake_features(monkeypatch, deposit_pct=0)
    est = _create_estimate(client)
    _add_lines(client, est["id"], 2600.0)
    token = _publish(client, est["id"])

    body = client.post(f"/api/proposals/{token}/accept", json={}).json()
    assert body["estimate"]["status"] == "accepted"
    assert "deposit" not in body
    assert _deposit_rows(client, est["id"]) == []


def test_public_tier_accept_office_shape_deposit_uses_tier_price(client: TestClient, monkeypatch):
    """THE audit-critique regression (§3): an office-built tier priced ABOVE
    the base lines. The deposit service caps against the tier-blind lines
    total, so without the cap_total override this deposit silently skipped —
    customer accepts, nobody is asked for money, nobody notices."""
    _fake_features(monkeypatch, deposit_pct=50)
    est = _create_estimate(client)
    _add_lines(client, est["id"], 500.0)  # base lines well below the tier
    client.patch(f"/api/estimates/{est['id']}", json={"proposal_mode": True})
    tier = _add_tier(client, est["id"], "best", total_price=8000.0)
    token = _publish(client, est["id"])

    r = client.post(f"/api/proposals/{token}/accept", json={"tier_id": tier["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["estimate"]["accepted_tier_id"] == tier["id"]
    assert body["deposit"]["amount"] == pytest.approx(4000.0)  # 50% of the TIER
    assert [d["total"] for d in _deposit_rows(client, est["id"])] == [4000.0]


def test_public_accept_requires_tier_choice_in_proposal_mode(client: TestClient, monkeypatch):
    _fake_features(monkeypatch)
    est = _create_estimate(client)
    client.patch(f"/api/estimates/{est['id']}", json={"proposal_mode": True})
    _add_tier(client, est["id"], "good", total_price=2500.0)
    token = _publish(client, est["id"])

    assert client.post(f"/api/proposals/{token}/accept", json={}).status_code == 422
    # A tier from some other estimate must not be acceptable here.
    other = _create_estimate(client)
    client.patch(f"/api/estimates/{other['id']}", json={"proposal_mode": True})
    foreign = _add_tier(client, other["id"], "good", total_price=1.0)
    r = client.post(f"/api/proposals/{token}/accept", json={"tier_id": foreign["id"]})
    assert r.status_code == 404


def test_public_accept_rejects_tier_for_line_estimate(client: TestClient, monkeypatch):
    _fake_features(monkeypatch)
    est = _create_estimate(client)
    tier_shaped_uuid = str(uuid4())
    token = _publish(client, est["id"])
    r = client.post(f"/api/proposals/{token}/accept", json={"tier_id": tier_shaped_uuid})
    assert r.status_code == 422


def test_public_accept_is_idempotent_on_reclick(client: TestClient, monkeypatch):
    """An emailed link gets double-clicked routinely; the second click must
    return the accepted state (200, not the portal's 409) and must NOT mint a
    second deposit invoice. Deliberate divergence — pinned here."""
    _fake_features(monkeypatch, deposit_pct=50)
    est = _create_estimate(client)
    _add_lines(client, est["id"], 1000.0)
    token = _publish(client, est["id"])

    assert client.post(f"/api/proposals/{token}/accept", json={}).status_code == 200
    r2 = client.post(f"/api/proposals/{token}/accept", json={})
    assert r2.status_code == 200
    body = r2.json()
    assert body["already_accepted"] is True
    assert body["estimate"]["status"] == "accepted"
    # Recovery path: the still-owed deposit rides the idempotent response too.
    assert body["deposit"]["balance_due"] == pytest.approx(500.0)
    assert len(_deposit_rows(client, est["id"])) == 1


def test_public_accept_gates(client: TestClient, monkeypatch):
    """declined/expired → 409; a draft (never sent) is invisible → 404 even
    with the right token; a bounced 'rejected' estimate accepts fine."""
    _fake_features(monkeypatch)

    declined = _create_estimate(client)
    token_declined = _publish(client, declined["id"], status="declined")
    assert client.post(f"/api/proposals/{token_declined}/accept", json={}).status_code == 409

    expired = _create_estimate(client)
    token_expired = _publish(client, expired["id"], status="expired")
    assert client.post(f"/api/proposals/{token_expired}/accept", json={}).status_code == 409

    draft = _create_estimate(client)
    db = _db(client)
    try:
        token_draft = db.get(Estimate, UUID(draft["id"])).public_token
    finally:
        db.close()
    assert client.post(f"/api/proposals/{token_draft}/accept", json={}).status_code == 404

    bounced = _create_estimate(client)
    token_bounced = _publish(client, bounced["id"], status="rejected")
    r = client.post(f"/api/proposals/{token_bounced}/accept", json={})
    assert r.status_code == 200
    assert r.json()["estimate"]["status"] == "accepted"


def test_public_decline_reason_optional_and_idempotent(client: TestClient, monkeypatch):
    _fake_features(monkeypatch)
    est = _create_estimate(client)
    token = _publish(client, est["id"])

    r = client.post(f"/api/proposals/{token}/decline", json={})
    assert r.status_code == 200, r.text
    assert r.json()["estimate"]["status"] == "declined"
    db = _db(client)
    try:
        assert db.get(Estimate, UUID(est["id"])).declined_reason is None
    finally:
        db.close()

    r2 = client.post(f"/api/proposals/{token}/decline", json={"reason": "too late"})
    assert r2.status_code == 200
    assert r2.json()["already_declined"] is True
    # Declined is terminal for the public page: accept now 409s.
    assert client.post(f"/api/proposals/{token}/accept", json={}).status_code == 409


def test_public_decline_records_reason(client: TestClient, monkeypatch):
    _fake_features(monkeypatch)
    est = _create_estimate(client)
    token = _publish(client, est["id"])

    r = client.post(f"/api/proposals/{token}/decline", json={"reason": "Went another way"})
    assert r.status_code == 200
    assert r.json()["estimate"]["declined_reason"] == "Went another way"


def test_public_accept_unknown_token_is_uniform_404(client: TestClient, monkeypatch):
    _fake_features(monkeypatch)
    r = client.post("/api/proposals/not-a-real-token/accept", json={})
    assert r.status_code == 404
    assert r.json()["detail"] == "Invalid proposal token"
