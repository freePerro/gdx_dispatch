"""PR4-billing-capture (2026-07-07) — parts capture unification.

Parts capture was 4 write paths with only ONE able to reach an invoice.
job_parts_needed is now the single billable spine; closeout, mobile
parts-used, and van usage insert source-tagged rows (per-event identity, NO
fuzzy matching/overwrites — audit round 1: upsert-matching undercounted).

Pinned here:
1. Closeout inserts one billable row per closeout line, 1:1 — two same-sku
   lines stay TWO rows (the v1-design failure case).
2. Re-closeout replaces the job's UNBILLED closeout rows only; billed rows
   are never touched; request-sourced rows coexist untouched (no clobber).
3. unit_price = catalog SELL price when the part resolves; free-text rows
   carry NULL (office prices at invoicing) — never the closeout cost.
4. Closeout now decrements Part.qty_on_hand (allow-negative, non-blocking),
   matching the mobile path it previously disagreed with.
5. Mobile parts-used and job-linked van usage each add accumulating
   source-tagged rows; van usage without a job adds none.
6. GET /api/parts-needed/unbilled-consumed groups leaked parts by completed
   job with a suggested total.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobCloseout,
    JobPartNeeded,
    Payment,
    VanInventoryItem,
    VanInventoryLog,
)
from gdx_dispatch.modules.inventory.models import JobPart, Part
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.jobs import CloseoutPayload, closeout_job
from gdx_dispatch.routers.parts_needed import unbilled_consumed_parts

TENANT = "tenant-1"


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    for tbl in [
        Job.__table__,
        Customer.__table__,
        Estimate.__table__,
        EstimateLine.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        JobPartNeeded.__table__,
        JobCloseout.__table__,
        Part.__table__,
        JobPart.__table__,
        VanInventoryItem.__table__,
        VanInventoryLog.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)

    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def _user() -> dict[str, str]:
    return {"user_id": "tech-1", "tenant_id": TENANT, "role": "technician"}


def _seed_job(db, stage: str = "in_progress") -> Job:
    job = Job(
        customer_id=uuid4(),
        title="Door repair",
        description="t",
        lifecycle_stage=stage,
        dispatch_status="on_site",
        billing_status="unbilled",
        company_id=TENANT,
        completed_at=datetime.now(UTC) if stage == "completed" else None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _seed_part(db, *, sku: str = "SPR-200", price: float = 89.5, cost: float = 40.0, qty: int = 5) -> Part:
    part = Part(
        id=uuid4(),
        sku=sku,
        name=f"Torsion spring {sku}",
        unit_cost=Decimal(str(cost)),
        unit_price=Decimal(str(price)),
        qty_on_hand=qty,
        reorder_point=1,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _closeout(db, job, parts: list[dict], hours: float = 1.0):
    return closeout_job(
        payload=CloseoutPayload(parts=parts, hours=hours),
        job_id=str(job.id),
        request=_request(),
        current_user=_user(),
        db=db,
    )


def _checklist(db, job, source: str | None = None) -> list[JobPartNeeded]:
    stmt = select(JobPartNeeded).where(JobPartNeeded.job_id == str(job.id))
    if source:
        stmt = stmt.where(JobPartNeeded.source == source)
    return db.execute(stmt.order_by(JobPartNeeded.created_at)).scalars().all()


def test_closeout_feeds_checklist_one_row_per_line(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db)

    resp = _closeout(db, job, [
        {"part_id": str(part.id), "sku": part.sku, "name": part.name, "qty": 1, "unit_cost": 40.0},
        {"part_id": str(part.id), "sku": part.sku, "name": part.name, "qty": 1, "unit_cost": 40.0},
        {"name": "Custom strut (fabricated)", "qty": 2, "unit_cost": 15.0},
    ])
    assert resp.status_code == 201

    rows = _checklist(db, job, source="closeout")
    # 1:1 — the two same-sku lines stay DISTINCT rows (v1 upsert design
    # collapsed them = undercharge).
    assert len(rows) == 3
    assert all(r.status == "used" for r in rows)
    priced = [r for r in rows if r.sku == part.sku]
    assert len(priced) == 2
    # Sell price from catalog — NOT the 40.0 closeout cost.
    assert all(float(r.unit_price) == 89.5 for r in priced)
    free_text = next(r for r in rows if r.sku is None)
    assert free_text.unit_price is None
    assert free_text.part_name == "Custom strut (fabricated)"


def test_closeout_decrements_stock_allow_negative(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, qty=1)

    _closeout(db, job, [
        {"part_id": str(part.id), "sku": part.sku, "name": part.name, "qty": 3, "unit_cost": 40.0},
    ])

    db.refresh(part)
    # 1 on hand - 3 used = -2: negative allowed, completion never blocked
    # on a stock count (Doug 2026-05-10).
    assert int(part.qty_on_hand) == -2


def test_recloseout_replaces_unbilled_closeout_rows_only(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)

    # A tech REQUEST row must survive re-closeouts untouched (no clobber —
    # the v1 failure case: request qty 2 overwritten by closeout qty 1).
    request_row = JobPartNeeded(
        id=str(uuid4()),
        company_id=TENANT,
        job_id=str(job.id),
        part_name="Opener bracket",
        quantity=2,
        status="received",
        source="request",
        created_at=datetime.now(UTC),
    )
    db.add(request_row)
    db.commit()

    _closeout(db, job, [{"name": "Strut", "qty": 1, "unit_cost": 10.0}])
    first_rows = _checklist(db, job, source="closeout")
    assert len(first_rows) == 1

    # Mark the first closeout row billed — a re-closeout must NOT touch it.
    inv = Invoice(
        company_id=TENANT,
        customer_id=job.customer_id,
        invoice_number=f"INV-{uuid4().hex[:8]}",
        billing_type="standard",
        sequence_number=1,
        subtotal=Decimal("10"),
        tax_amount=Decimal("0"),
        total=Decimal("10"),
        balance_due=Decimal("10"),
        status="draft",
        public_token=uuid4().hex,
        locked=False,
    )
    db.add(inv)
    db.commit()
    first_rows[0].billed_invoice_id = inv.id
    db.commit()

    _closeout(db, job, [
        {"name": "Strut", "qty": 1, "unit_cost": 10.0},
        {"name": "Hinge set", "qty": 1, "unit_cost": 5.0},
    ])

    closeout_rows = _checklist(db, job, source="closeout")
    # billed row survived; the re-attested identical Strut was SUPPRESSED
    # (audit round 2 dedup) so only the new Hinge lands unbilled.
    assert len(closeout_rows) == 2
    billed = [r for r in closeout_rows if r.billed_invoice_id is not None]
    assert len(billed) == 1
    unbilled_names = {r.part_name for r in closeout_rows if r.billed_invoice_id is None}
    assert unbilled_names == {"Hinge set"}
    # request row untouched, qty intact
    req_rows = _checklist(db, job, source="request")
    assert len(req_rows) == 1
    assert req_rows[0].quantity == 2


def test_mobile_parts_used_accumulates_source_rows(tenant_db_session, monkeypatch):
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, qty=10)

    from gdx_dispatch.routers import mobile as mobile_router

    monkeypatch.setattr(mobile_router, "_assert_job_access", lambda *a, **k: None)
    monkeypatch.setattr(mobile_router, "_get_job", lambda *a, **k: job)

    body = mobile_router.PartsUsedBody(parts=[
        {"part_id": str(part.id), "qty": 2},
    ])
    for _ in range(2):  # two separate events must accumulate, never merge
        resp = mobile_router.mobile_job_parts_used(
            job_id=str(job.id), payload=body, request=_request(),
            current_user=_user(), db=db,
        )
        assert getattr(resp, "status_code", 200) in (200, 201), getattr(resp, "body", resp)

    rows = _checklist(db, job, source="mobile")
    assert len(rows) == 2
    assert all(r.status == "used" and r.quantity == 2 for r in rows)
    assert all(float(r.unit_price) == 89.5 for r in rows)
    db.refresh(part)
    assert int(part.qty_on_hand) == 6


def test_van_usage_feeds_checklist_only_when_job_linked(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)
    item = VanInventoryItem(
        id=uuid4(),
        company_id=TENANT,
        truck_id="truck-1",
        sku="HNG-14",
        name="14ga hinge",
        quantity=8,
    )
    db.add(item)
    db.commit()

    from gdx_dispatch.routers import van_inventory as van_router

    van_router.use_van_item(
        request=_request(),
        payload=van_router.VanUseIn(van_inventory_id=str(item.id), quantity=3, job_id=str(job.id), reason="install"),
        user=_user(),
        db=db,
    )
    van_router.use_van_item(
        request=_request(),
        payload=van_router.VanUseIn(van_inventory_id=str(item.id), quantity=1, reason="shop restock"),
        user=_user(),
        db=db,
    )

    rows = _checklist(db, job, source="van")
    assert len(rows) == 1
    assert rows[0].quantity == 3
    assert rows[0].unit_price is None  # van stock carries no sell price
    db.refresh(item)
    assert item.quantity == 4


def test_unbilled_consumed_report_groups_by_completed_job(tenant_db_session):
    db = tenant_db_session
    done_job = _seed_job(db, stage="completed")
    open_job = _seed_job(db, stage="in_progress")

    for j, price in ((done_job, 50.0), (open_job, 99.0)):
        db.add(JobPartNeeded(
            id=str(uuid4()),
            company_id=TENANT,
            job_id=str(j.id),
            part_name="Leaked part",
            quantity=2,
            status="used",
            source="closeout",
            unit_price=Decimal(str(price)),
            created_at=datetime.now(UTC),
        ))
    db.commit()

    out = unbilled_consumed_parts(request=_request(), user=_user(), db=db)

    assert len(out) == 1, "only COMPLETED jobs are leak-review candidates"
    entry = out[0]
    assert entry["job_id"] == str(done_job.id)
    assert len(entry["parts"]) == 1
    assert entry["suggested_total"] == 100.0
    assert entry["parts"][0]["source"] == "closeout"


def test_one_click_invoice_pulls_attested_closeout_parts(tenant_db_session):
    """One-click create-invoice pulls the tech's attested (closeout-sourced)
    parts as priced lines and stamps them billed — the leak, closed
    end-to-end. Request/mobile/van rows stay on the operator checklist."""
    from starlette.requests import Request as _Req

    from gdx_dispatch.routers.jobs import create_invoice_from_job

    db = tenant_db_session
    job = _seed_job(db, stage="completed")
    part = _seed_part(db)
    _closeout(db, job, [
        {"part_id": str(part.id), "sku": part.sku, "name": part.name, "qty": 2, "unit_cost": 40.0},
    ])
    # A request-sourced row must NOT be auto-pulled by one-click.
    db.add(JobPartNeeded(
        id=str(uuid4()),
        company_id=TENANT,
        job_id=str(job.id),
        part_name="Requested bracket",
        quantity=1,
        status="received",
        source="request",
        created_at=datetime.now(UTC),
    ))
    db.commit()

    request = _Req({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.tenant = {"id": TENANT}
    out = create_invoice_from_job(
        job_id=str(job.id), request=request,
        current_user={"sub": "user-1", "tenant_id": TENANT}, db=db,
    )

    # $0 fallback line + 2 × $89.50 closeout part = 179.00 (no TaxConfig
    # seeded → rate 0).
    assert out["total"] == 179.0
    rows = _checklist(db, job, source="closeout")
    assert all(str(r.billed_invoice_id) == out["invoice_id"] for r in rows)
    req_rows = _checklist(db, job, source="request")
    assert all(r.billed_invoice_id is None for r in req_rows)
    lines = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == UUID(out["invoice_id"]))
    ).scalars().all()
    part_lines = [ln for ln in lines if ln.part_id is not None]
    assert len(part_lines) == 1
    assert float(part_lines[0].line_total) == 179.0


# ---------------------------------------------------------------------------
# Audit round 2 — reproduced double-bill/stock-drain repros, pinned fixed
# ---------------------------------------------------------------------------


def test_recloseout_is_stock_neutral(tenant_db_session):
    """Audit repro: identical resubmit drained qty_on_hand 10 → 6. The
    replace step must reverse the deleted rows' decrements."""
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, qty=10)
    line = [{"part_id": str(part.id), "sku": part.sku, "name": part.name, "qty": 2, "unit_cost": 40.0}]

    _closeout(db, job, line)
    db.refresh(part)
    assert int(part.qty_on_hand) == 8

    _closeout(db, job, line)  # fix-a-note resubmit
    db.refresh(part)
    assert int(part.qty_on_hand) == 8, "re-closeout must not drain stock"


def test_recloseout_does_not_resurrect_billed_parts(tenant_db_session):
    """Audit repro: after billing, a full re-attestation re-inserted the
    billed springs as UNBILLED rows → leak card + second billing. Exact
    (sku/name + qty) matches of BILLED closeout rows are suppressed; a
    differing qty still lands for operator review."""
    db = tenant_db_session
    job = _seed_job(db)
    _closeout(db, job, [{"name": "Strut", "qty": 1, "unit_cost": 10.0}])
    row = _checklist(db, job, source="closeout")[0]
    inv = Invoice(
        company_id=TENANT,
        customer_id=job.customer_id,
        invoice_number=f"INV-{uuid4().hex[:8]}",
        billing_type="standard",
        sequence_number=1,
        subtotal=Decimal("10"),
        tax_amount=Decimal("0"),
        total=Decimal("10"),
        balance_due=Decimal("10"),
        status="draft",
        public_token=uuid4().hex,
        locked=False,
    )
    db.add(inv)
    db.commit()
    row.billed_invoice_id = inv.id
    db.commit()

    # Tech re-attests the SAME strut (full-list statement) + a new hinge.
    _closeout(db, job, [
        {"name": "Strut", "qty": 1, "unit_cost": 10.0},
        {"name": "Hinge", "qty": 1, "unit_cost": 5.0},
    ])
    rows = _checklist(db, job, source="closeout")
    unbilled = [r for r in rows if r.billed_invoice_id is None]
    assert {r.part_name for r in unbilled} == {"Hinge"}, (
        "the already-billed strut must NOT resurrect as an unbilled duplicate"
    )

    # Differing qty is NOT suppressed — over-shows for operator review.
    _closeout(db, job, [{"name": "Strut", "qty": 2, "unit_cost": 10.0}])
    unbilled = [r for r in _checklist(db, job, source="closeout") if r.billed_invoice_id is None]
    assert [(r.part_name, r.quantity) for r in unbilled] == [("Strut", 2)]


def test_one_click_skips_parts_pull_when_estimate_priced_the_job(tenant_db_session):
    """Audit repro: estimate lines + auto-pulled closeout parts billed a
    $179 job at $358 with no human in the loop. With an estimate, closeout
    parts stay on the operator checklist."""
    from starlette.requests import Request as _Req

    from gdx_dispatch.routers.jobs import create_invoice_from_job

    db = tenant_db_session
    job = _seed_job(db, stage="completed")
    part = _seed_part(db)
    est = Estimate(
        job_id=job.id,
        customer_id=job.customer_id,
        estimate_number=f"EST-{uuid4().hex[:8]}",
        label="Quoted",
        proposal_mode=False,
        total=Decimal("179.00"),
        status="accepted",
        public_token=uuid4().hex,
        company_id=TENANT,
    )
    db.add(est)
    db.commit()
    db.refresh(est)
    db.add(EstimateLine(
        estimate_id=est.id,
        description=f"2x {part.name}",
        quantity=2,
        unit_price=Decimal("89.50"),
        line_total=Decimal("179.00"),
        sort_order=1,
        company_id=TENANT,
    ))
    db.commit()
    _closeout(db, job, [
        {"part_id": str(part.id), "sku": part.sku, "name": part.name, "qty": 2, "unit_cost": 40.0},
    ])

    request = _Req({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.tenant = {"id": TENANT}
    out = create_invoice_from_job(
        job_id=str(job.id), request=request,
        current_user={"sub": "user-1", "tenant_id": TENANT}, db=db,
    )

    assert out["total"] == 179.0, "estimate total only — no additive parts pull"
    rows = _checklist(db, job, source="closeout")
    assert all(r.billed_invoice_id is None for r in rows), (
        "closeout parts stay on the operator checklist when an estimate exists"
    )


def test_van_usage_invalid_job_id_fails_loudly(tenant_db_session):
    """Audit blind spot: a typo'd job_id minted a checklist row no query
    could display — an invisible unbillable row. Now 422, nothing written,
    stock untouched."""
    from fastapi import HTTPException as _HTTPExc

    from gdx_dispatch.routers import van_inventory as van_router

    db = tenant_db_session
    item = VanInventoryItem(
        id=uuid4(),
        company_id=TENANT,
        truck_id="truck-1",
        sku="HNG-14",
        name="14ga hinge",
        quantity=8,
    )
    db.add(item)
    db.commit()

    with pytest.raises(_HTTPExc) as exc_info:
        van_router.use_van_item(
            request=_request(),
            payload=van_router.VanUseIn(van_inventory_id=str(item.id), quantity=3, job_id="not-a-job", reason="oops"),
            user=_user(),
            db=db,
        )
    assert exc_info.value.status_code == 422
    db.rollback()
    db.refresh(item)
    assert item.quantity == 8, "failed usage must not decrement stock"
    assert db.execute(select(JobPartNeeded)).scalars().all() == []


def test_wont_bill_dismiss_leaves_billing_surfaces(tenant_db_session):
    """The office's dismiss verb: wont_bill rows leave the leak report (and
    the checklist filters) but keep their audit trail."""
    from gdx_dispatch.routers.parts_needed import PartStatusUpdate, update_part_status

    db = tenant_db_session
    job = _seed_job(db, stage="completed")
    _closeout(db, job, [{"name": "Goodwill part", "qty": 1, "unit_cost": 5.0}])

    before = unbilled_consumed_parts(request=_request(), user=_user(), db=db)
    assert len(before) == 1
    part_id = before[0]["parts"][0]["id"]

    update_part_status(
        part_id=part_id,
        payload=PartStatusUpdate(status="wont_bill"),
        request=_request(),
        user={"user_id": "office-1", "tenant_id": TENANT, "role": "admin"},
        db=db,
    )

    after = unbilled_consumed_parts(request=_request(), user=_user(), db=db)
    assert after == []
    row = db.execute(select(JobPartNeeded).where(JobPartNeeded.id == part_id)).scalar_one()
    assert row.status == "wont_bill"
