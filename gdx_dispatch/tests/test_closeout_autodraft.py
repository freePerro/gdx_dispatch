"""Closeout autodraft (Doug 2026-08-07): closing out a job mints a DRAFT
invoice priced from the closeout, so Ready-for-Billing reviews an existing
draft instead of a blank form.

Contract, pinned here:
1. A service closeout with hours + a catalog-priced part → one draft
   invoice: labor line (billing_lanes math) + part line at catalog sell
   price; the closeout part row is claimed (billed_invoice_id); origin =
   'closeout_autodraft'; the response carries the id + number; an
   invoice_autodrafted audit event lands in the same transaction.
2. The draft does NOT settle billing: job_billing_resolved stays false
   (the job remains in Ready-for-Billing, which now carries the draft),
   while the narrow job_billed_exists says true (no second invoice may be
   minted). The two deliberately diverge on priced drafts.
3. No autodraft when a live invoice already exists, or an accepted
   estimate exists (§15.1 — the estimate outranks the lanes), or nothing
   priceable landed (no empty $0 queue noise).
4. A re-closeout REBUILDS the untouched draft in place — same invoice id
   and number, lines replaced not appended, part claims re-stamped.
5. A draft a human advanced (verified_at) is never rebuilt — and it
   blocks a new autodraft (a live invoice exists).
6. Not-billable voids the untouched autodraft in the same stroke and
   releases its part claims; a human/finalized invoice still 409s.

Harness mirrors test_closeout_return_visit.py: handlers invoked directly
as functions against in-memory SQLite.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.billing_predicates import job_billed_exists, job_billing_resolved
from gdx_dispatch.models.pricing_engine import PricingSettings
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobCloseout,
    JobNote,
    JobPartNeeded,
    Payment,
    Technician,
    TimeEntry,
)
from gdx_dispatch.modules.inventory.models import JobPart, Part
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.modules.tax.models import TaxConfig
from gdx_dispatch.routers.jobs import (
    CloseoutPart,
    CloseoutPayload,
    NotBillablePayload,
    closeout_job,
    mark_job_not_billable,
    ready_for_billing,
)

TENANT = "tenant-autodraft"
USER = "user-doug"


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
        JobNote.__table__,
        Part.__table__,
        JobPart.__table__,
        Technician.__table__,
        TimeEntry.__table__,
        Estimate.__table__,
        PricingSettings.__table__,
        TaxConfig.__table__,
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


def _seed_job(db, *, job_type: str = "Service Call", customer_id=None) -> Job:
    job = Job(
        customer_id=customer_id if customer_id is not None else uuid4(),
        title="Door repair",
        description="t",
        job_type=job_type,
        lifecycle_stage="in_progress",
        dispatch_status="on_site",
        billing_status="unbilled",
        priority="Normal",
        company_id=TENANT,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _seed_part(db, *, sku: str = "SEAL-12", sell: float = 45.00) -> Part:
    part = Part(
        id=uuid4(),
        sku=sku,
        name="Bottom seal 12ft",
        unit_price=sell,
        qty_on_hand=10,
    )
    db.add(part)
    db.commit()
    return part


def _closeout(db, job, **overrides):
    payload = CloseoutPayload(**{"parts": [], "hours": 2.0, "no_parts_used": True, **overrides})
    return closeout_job(
        payload=payload,
        job_id=str(job.id),
        request=_request(),
        current_user={"user_id": USER, "tenant_id": TENANT, "role": "technician"},
        db=db,
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


def _invoices(db, job) -> list[Invoice]:
    return list(
        db.execute(select(Invoice).where(Invoice.job_id == job.id)).scalars()
    )


def _lines(db, inv) -> list[InvoiceLine]:
    return list(
        db.execute(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == inv.id)
            .order_by(InvoiceLine.sort_order)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# 1. The happy path: labor + priced part → one reviewable draft.
# ---------------------------------------------------------------------------


def test_service_closeout_autodrafts_priced_invoice(db) -> None:
    job = _seed_job(db)
    part = _seed_part(db, sell=45.00)
    resp = _closeout(
        db, job,
        hours=2.0,
        techs_on_site=1,
        no_parts_used=False,
        parts=[CloseoutPart(part_id=str(part.id), sku=part.sku, name=part.name, qty=1, unit_cost=20.0)],
    )
    assert resp.status_code == 201
    body = _body(resp)
    assert body["autodraft_invoice_id"] is not None
    assert body["autodraft_invoice_number"] is not None

    invs = _invoices(db, job)
    assert len(invs) == 1
    inv = invs[0]
    assert str(inv.id) == body["autodraft_invoice_id"]
    assert inv.status == "draft"
    assert inv.origin == "closeout_autodraft"
    assert inv.verified_at is None, "autodraft must ride the §11 verify gate"
    # Service lane, 2.0h × 1 tech → 2.0 man-hours → $100 first + $100 → $200,
    # plus the $45 catalog part.
    lines = _lines(db, inv)
    assert len(lines) == 2
    assert float(inv.total) == pytest.approx(245.00)
    assert float(inv.balance_due) == pytest.approx(245.00)

    # The closeout part row is claimed by the draft (stamp-first rule).
    claimed = db.execute(
        select(JobPartNeeded).where(
            JobPartNeeded.job_id == str(job.id),
            JobPartNeeded.source == "closeout",
        )
    ).scalars().all()
    assert len(claimed) == 1
    assert claimed[0].billed_invoice_id == inv.id

    events = list(
        db.execute(select(AuditLog).where(AuditLog.action == "invoice_autodrafted")).scalars()
    )
    assert len(events) == 1


def test_priced_draft_keeps_job_in_rfb_but_blocks_second_invoice(db) -> None:
    job = _seed_job(db)
    resp = _closeout(db, job, hours=1.0)
    assert _body(resp)["autodraft_invoice_id"] is not None

    # The deliberate divergence: billed-exists (don't mint another) vs
    # billing-resolved (but billing is not finished).
    assert db.execute(
        select(Job.id).where(Job.id == job.id, job_billed_exists())
    ).first() is not None
    assert db.execute(
        select(Job.id).where(Job.id == job.id, job_billing_resolved())
    ).first() is None

    # And the queue row carries the draft for the Review button.
    rows = ready_for_billing(
        request=_request(),
        current_user={"user_id": USER, "role": "owner"},
        db=db,
    )
    mine = [r for r in rows if r["id"] == str(job.id)]
    assert len(mine) == 1
    assert mine[0]["draft_invoice_id"] == _body(resp)["autodraft_invoice_id"]
    assert mine[0]["draft_origin"] == "closeout_autodraft"


# ---------------------------------------------------------------------------
# 2. Eligibility: never a second invoice, never over an estimate, never empty.
# ---------------------------------------------------------------------------


def test_no_autodraft_when_live_invoice_exists(db) -> None:
    job = _seed_job(db)
    existing = Invoice(
        id=uuid4(),
        job_id=job.id,
        customer_id=job.customer_id,
        invoice_number="INV-900001",
        subtotal=50, tax_amount=0, total=50, balance_due=50,
        status="draft",
        public_token=f"tok-{uuid4().hex}",
        company_id=TENANT,
    )
    db.add(existing)
    db.commit()

    resp = _closeout(db, job, hours=2.0)
    assert resp.status_code == 201
    assert _body(resp)["autodraft_invoice_id"] is None
    assert len(_invoices(db, job)) == 1, "the human's draft stands alone"


def test_no_autodraft_with_accepted_estimate(db) -> None:
    job = _seed_job(db)
    db.add(Estimate(
        id=uuid4(),
        job_id=job.id,
        customer_id=None,
        estimate_number=f"EST-{uuid4().hex[:8]}",
        total=1500,
        status="accepted",
        accepted_at=datetime.now(UTC),
        company_id=TENANT,
        public_token=f"est-tok-{uuid4().hex}",
    ))
    db.commit()

    resp = _closeout(db, job, hours=2.0)
    assert resp.status_code == 201
    assert _body(resp)["autodraft_invoice_id"] is None
    assert _invoices(db, job) == []


def test_unpriceable_closeout_creates_no_empty_draft(db) -> None:
    # Install lane with no matrix pick → labor unpriced; no parts → nothing
    # to line. An empty $0 draft would just be queue noise.
    job = _seed_job(db, job_type="Installation")
    resp = _closeout(db, job, hours=3.0)
    assert resp.status_code == 201
    assert _body(resp)["autodraft_invoice_id"] is None
    assert _invoices(db, job) == []


# ---------------------------------------------------------------------------
# 3. Restatement: rebuild while untouched, hands off once a human moved it.
# ---------------------------------------------------------------------------


def test_reclose_rebuilds_untouched_autodraft_in_place(db) -> None:
    job = _seed_job(db)
    first = _body(_closeout(db, job, hours=1.0))
    inv_id = first["autodraft_invoice_id"]
    assert inv_id is not None

    part = _seed_part(db, sell=45.00)
    second = _body(_closeout(
        db, job,
        hours=3.0,
        no_parts_used=False,
        parts=[CloseoutPart(part_id=str(part.id), sku=part.sku, name=part.name, qty=2, unit_cost=20.0)],
    ))
    # Same invoice, same number — "the invoice for this job" stays one thing.
    assert second["autodraft_invoice_id"] == inv_id
    assert second["autodraft_invoice_number"] == first["autodraft_invoice_number"]

    invs = _invoices(db, job)
    assert len(invs) == 1
    inv = invs[0]
    # 3.0h → $300 labor, + 2 × $45 part = $390; lines replaced, not appended.
    lines = _lines(db, inv)
    assert len(lines) == 2
    assert float(inv.total) == pytest.approx(390.00)
    # The new closeout row is claimed by the same draft.
    claimed = db.execute(
        select(JobPartNeeded).where(
            JobPartNeeded.source == "closeout",
            JobPartNeeded.billed_invoice_id == inv.id,
        )
    ).scalars().all()
    assert len(claimed) == 1
    assert int(claimed[0].quantity) == 2


def test_reclose_never_touches_verified_draft(db) -> None:
    job = _seed_job(db)
    first = _body(_closeout(db, job, hours=1.0))
    inv = _invoices(db, job)[0]
    inv.verified_at = datetime.now(UTC)
    db.commit()
    total_before = float(inv.total)

    second = _body(_closeout(db, job, hours=5.0))
    assert second["autodraft_invoice_id"] is None, "a live invoice exists — no new draft"
    db.refresh(inv)
    assert float(inv.total) == pytest.approx(total_before), "verified draft untouched"
    assert len(_invoices(db, job)) == 1
    assert first["autodraft_invoice_id"] == str(inv.id)


# ---------------------------------------------------------------------------
# 4. Not-billable: voids the machine's own draft, still refuses on real ones.
# ---------------------------------------------------------------------------


def _mark_not_billable(db, job):
    return mark_job_not_billable(
        job_id=str(job.id),
        payload=NotBillablePayload(reason="warranty work"),
        request=_request(),
        current_user={"user_id": USER, "tenant_id": TENANT, "role": "owner"},
        db=db,
    )


def test_not_billable_voids_untouched_autodraft(db) -> None:
    job = _seed_job(db)
    part = _seed_part(db)
    _closeout(
        db, job,
        hours=1.0,
        no_parts_used=False,
        parts=[CloseoutPart(part_id=str(part.id), sku=part.sku, name=part.name, qty=1, unit_cost=0)],
    )
    inv = _invoices(db, job)[0]

    resp = _mark_not_billable(db, job)
    assert resp.status_code == 200

    db.refresh(inv)
    assert inv.status == "void"
    assert float(inv.balance_due) == 0
    # Part claims released — the checklist row is billable again if the
    # office ever reverses the mark.
    unclaimed = db.execute(
        select(JobPartNeeded).where(JobPartNeeded.billed_invoice_id == inv.id)
    ).scalars().all()
    assert unclaimed == []
    db.refresh(job)
    assert job.not_billable_at is not None
    assert db.execute(
        select(Job.id).where(Job.id == job.id, job_billing_resolved())
    ).first() is not None


def test_autodraft_taxes_parts_not_labor_with_resolved_rate(db) -> None:
    """2026-08-08 audit: the autodraft was the only creation path with
    tax_rate NULL (the legacy flat-tax branch) — its parts were structurally
    untaxable through every later office edit. Now it resolves the same
    customer-aware rate the office create path uses; parts taxed, labor not
    (tenant tax-labor flag off by default), and the rate persists so office
    line edits recalc correctly."""
    db.add(TaxConfig(default_rate=Decimal("0.07375")))
    db.commit()

    job = _seed_job(db)
    part = _seed_part(db, sell=100.00)
    resp = _closeout(
        db, job,
        hours=1.0,  # service lane → $100 labor
        no_parts_used=False,
        parts=[CloseoutPart(part_id=str(part.id), sku=part.sku, name=part.name, qty=1, unit_cost=50.0)],
    )
    body = _body(resp)
    inv = db.get(Invoice, UUID(body["autodraft_invoice_id"]))
    assert float(inv.tax_rate) == pytest.approx(0.07375)
    # Tax on the $100 part only, never the $100 labor.
    assert float(inv.tax_amount) == pytest.approx(7.38, abs=0.01)
    assert float(inv.total) == pytest.approx(207.38, abs=0.01)
    lines = _lines(db, inv)
    by_cat = {ln.category: ln for ln in lines}
    assert by_cat["Labor"].taxable is False
    assert by_cat["Parts"].taxable is True

    # Provenance now serialized — the office sees WHAT it is reviewing.
    from gdx_dispatch.routers.invoices import _serialize_invoice
    assert _serialize_invoice(inv)["origin"] == "closeout_autodraft"


def test_closeout_billing_suggestion_prices_labor_and_carries_notes(db) -> None:
    """The office create-invoice screen prefills from this endpoint (Doug
    2026-08-07: 'click invoice — it does not show hours or notes from the
    job'). Service closeout → priced labor line + the attested context;
    read-only (no invoice rows created, nothing claimed)."""
    from gdx_dispatch.routers.jobs import closeout_billing_suggestion

    job = _seed_job(db)
    _closeout(db, job, hours=4.0, notes="Add snirtstopper bottom seal 12ft")
    # The tech's real work summary usually lives in job_notes (the mobile
    # Add-note flow), NOT the closeout note — round 2 of the complaint was
    # exactly this note being invisible on the create screen.
    db.add(JobNote(
        job_id=str(job.id),
        company_id=TENANT,
        author_id=USER,
        author_name="doug",
        body="Bottom panel was mechanically adjusted out of shape.",
        visibility="internal",
    ))
    # The autodraft minted a draft — delete it so this mirrors a pre-autodraft
    # job (the bake house case): suggestion must work with NO invoice at all.
    for inv in _invoices(db, job):
        db.delete(inv)
    db.commit()

    resp = closeout_billing_suggestion(
        job_id=str(job.id),
        request=_request(),
        current_user={"user_id": USER, "role": "owner"},
        db=db,
    )
    body = json.loads(resp.body)
    assert body["has_closeout"] is True
    assert body["estimate_exists"] is False
    assert body["closeout"]["hours_worked"] == 4.0
    assert body["closeout"]["notes"] == "Add snirtstopper bottom seal 12ft"
    # 4.0 h × 1 tech, service lane → $100 first hour + 3 × $100 = $400.
    assert body["labor_line"] is not None
    assert body["labor_line"]["line_total"] == 400.0
    # Job notes ride along, visibility included (the UI badges internal
    # notes before the operator copies one to a customer-facing field).
    assert len(body["job_notes"]) == 1
    assert body["job_notes"][0]["body"].startswith("Bottom panel was mechanically")
    assert body["job_notes"][0]["visibility"] == "internal"
    assert _invoices(db, job) == [], "suggestion must be read-only"


def test_closeout_billing_suggestion_without_closeout(db) -> None:
    from gdx_dispatch.routers.jobs import closeout_billing_suggestion

    job = _seed_job(db)
    resp = closeout_billing_suggestion(
        job_id=str(job.id),
        request=_request(),
        current_user={"user_id": USER, "role": "owner"},
        db=db,
    )
    body = json.loads(resp.body)
    assert body["has_closeout"] is False
    assert body["labor_line"] is None


def test_not_billable_still_409s_on_finalized_invoice(db) -> None:
    job = _seed_job(db)
    db.add(Invoice(
        id=uuid4(),
        job_id=job.id,
        customer_id=job.customer_id,
        invoice_number="INV-900002",
        subtotal=200, tax_amount=0, total=200, balance_due=200,
        status="sent",
        sent_at=datetime.now(UTC),
        public_token=f"tok-{uuid4().hex}",
        company_id=TENANT,
    ))
    job.lifecycle_stage = "completed"
    db.commit()

    resp = _mark_not_billable(db, job)
    assert resp.status_code == 409
    db.refresh(job)
    assert job.not_billable_at is None
