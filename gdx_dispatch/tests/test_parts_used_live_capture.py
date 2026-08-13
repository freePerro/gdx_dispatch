"""Live parts capture (2026-08-12) — logging parts while the job is worked.

Before this, the ONLY way to record a part as used was the closeout form, so
everything installed before the last five minutes of the job had to be held in
the tech's head or typed into a note (where nothing orders, counts, or bills
it). The mobile parts-used endpoint existed but could not carry the job: it
demanded an inventory ``parts.id`` — which catalog rows don't have — and 400'd
on a stock shortage, i.e. refused the part that was already in the door.

Pinned here:
1. A free-text line (no part_id) records a billable row and no job_parts row —
   job_parts.part_id is an FK to parts.id.
2. A line with a sku that matches stocked inventory resolves to it: job_parts
   row + stock decrement + catalog sell price on the billable row.
3. Short stock does NOT block the capture (allow-negative, same rule closeout
   follows — Doug 2026-05-10).
4. Undo removes an unbilled live row, gives the stock back, and drops the
   job_parts cost row; a BILLED row is refused (409), and the endpoint refuses
   to touch rows from other capture sources.
5. The closeout's require-parts gate is satisfied by parts logged live — the
   gate as written forced a retype, and a retype mints a SECOND billable row.
6. A closeout does not clobber live rows (different source), and the two
   coexist as separate billable rows.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

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
)
from gdx_dispatch.modules.inventory.models import JobPart, Part
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.jobs import CloseoutPayload, closeout_job

TENANT = "tenant-1"

GATES_PARTS_REQUIRED = {
    "lock_schedule_on_start": False,
    "post_arrival_event": False,
    "sms_arrival_notify": False,
    "require_parts_on_complete": True,
    "require_hours_on_complete": False,
    "require_signature_on_complete": False,
    "require_invoice_on_complete": False,
}


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
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        JobPartNeeded.__table__,
        JobCloseout.__table__,
        Part.__table__,
        JobPart.__table__,
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


@pytest.fixture
def mobile_job(monkeypatch):
    """The ownership gate and job lookup are exercised elsewhere; these tests
    are about what the write records."""
    def _bind(db, job):
        monkeypatch.setattr(mobile_router, "_assert_job_access", lambda *a, **k: None)
        monkeypatch.setattr(mobile_router, "_get_job", lambda *a, **k: job)
    return _bind


def _log_used(db, job, parts: list[dict]):
    return mobile_router.mobile_job_parts_used(
        job_id=str(job.id),
        payload=mobile_router.PartsUsedBody(parts=parts),
        request=_request(),
        current_user=_user(),
        db=db,
    )


def _undo(db, job, row_id: str):
    return mobile_router.mobile_job_parts_used_undo(
        job_id=str(job.id),
        row_id=str(row_id),
        request=_request(),
        current_user=_user(),
        db=db,
    )


def _rows(db, job, source: str | None = None) -> list[JobPartNeeded]:
    stmt = select(JobPartNeeded).where(JobPartNeeded.job_id == str(job.id))
    if source:
        stmt = stmt.where(JobPartNeeded.source == source)
    return db.execute(stmt.order_by(JobPartNeeded.created_at)).scalars().all()


def test_free_text_part_records_without_inventory(tenant_db_session, mobile_job):
    """The case the old endpoint could not express at all: a tech installs a
    part that isn't a row in `parts`. It must still reach billing."""
    db = tenant_db_session
    job = _seed_job(db)
    mobile_job(db, job)

    resp = _log_used(db, job, [{"name": "Fabricated strut", "qty": 2}])
    assert resp.status_code == 200

    rows = _rows(db, job, source="mobile")
    assert len(rows) == 1
    assert rows[0].part_name == "Fabricated strut"
    assert rows[0].quantity == 2
    assert rows[0].status == "used"
    # NULL sell price — the office prices a free-text part at invoicing; the
    # capture must never invent one.
    assert rows[0].unit_price is None
    # No cost row: job_parts.part_id is an FK to parts.id and there is no part.
    assert db.execute(select(JobPart)).scalars().all() == []


def test_sku_match_resolves_to_inventory(tenant_db_session, mobile_job):
    """A catalog pick carries a sku but no parts.id. When that sku IS stocked
    it's the same physical part — cost it and take it off the shelf."""
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, qty=5)
    mobile_job(db, job)

    resp = _log_used(db, job, [{"name": part.name, "sku": part.sku, "qty": 2}])
    assert resp.status_code == 200

    rows = _rows(db, job, source="mobile")
    assert len(rows) == 1
    assert float(rows[0].unit_price) == 89.5  # catalog SELL price, not cost
    jps = db.execute(select(JobPart)).scalars().all()
    assert len(jps) == 1
    assert jps[0].qty_used == 2
    assert float(jps[0].unit_cost_at_time) == 40.0
    db.refresh(part)
    assert int(part.qty_on_hand) == 3


def test_short_stock_does_not_block_the_capture(tenant_db_session, mobile_job):
    """The part is already in the door. A stock count must not be the thing
    that stops it being recorded — that only pushes the tech back to a note."""
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, qty=1)
    mobile_job(db, job)

    resp = _log_used(db, job, [{"part_id": str(part.id), "qty": 4}])
    assert resp.status_code == 200
    assert len(_rows(db, job, source="mobile")) == 1
    db.refresh(part)
    assert int(part.qty_on_hand) == -3


def test_undo_removes_row_and_returns_stock(tenant_db_session, mobile_job):
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, qty=5)
    mobile_job(db, job)

    _log_used(db, job, [{"part_id": str(part.id), "qty": 2}])
    row = _rows(db, job, source="mobile")[0]
    db.refresh(part)
    assert int(part.qty_on_hand) == 3

    resp = _undo(db, job, row.id)
    assert resp.status_code == 200
    assert _rows(db, job, source="mobile") == []
    db.refresh(part)
    assert int(part.qty_on_hand) == 5
    # The cost row goes too — otherwise job costing keeps a phantom part.
    assert db.execute(select(JobPart)).scalars().all() == []


def test_undo_refuses_a_billed_row(tenant_db_session, mobile_job):
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, qty=5)
    mobile_job(db, job)

    _log_used(db, job, [{"part_id": str(part.id), "qty": 1}])
    row = _rows(db, job, source="mobile")[0]
    row.billed_invoice_id = uuid4()
    db.commit()

    resp = _undo(db, job, row.id)
    assert resp.status_code == 409
    assert len(_rows(db, job, source="mobile")) == 1
    db.refresh(part)
    assert int(part.qty_on_hand) == 4  # stock NOT given back


def test_undo_will_not_touch_another_sources_row(tenant_db_session, mobile_job):
    """A closeout attestation is corrected by re-closing out, and a request row
    belongs to dispatch. This endpoint owns only its own writes."""
    db = tenant_db_session
    job = _seed_job(db)
    mobile_job(db, job)
    now = datetime.now(UTC)
    foreign = JobPartNeeded(
        id=str(uuid4()),
        company_id=TENANT,
        job_id=str(job.id),
        part_name="Hinge set",
        quantity=1,
        status="used",
        source="closeout",
        created_at=now,
        updated_at=now,
    )
    db.add(foreign)
    db.commit()

    resp = _undo(db, job, foreign.id)
    assert resp.status_code == 404
    assert len(_rows(db, job, source="closeout")) == 1


def test_closeout_gate_is_satisfied_by_parts_logged_live(tenant_db_session, mobile_job, monkeypatch):
    """The gate used to demand a parts list at closeout even when the tech had
    already logged every part on the job — and retyping them there mints a
    SECOND billable row per part, because live capture and closeout are
    different sources. The gate manufactured the double-billing it looks like
    it prevents."""
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, qty=5)
    mobile_job(db, job)
    monkeypatch.setattr(
        "gdx_dispatch.routers.jobs._load_workflow_flags",
        lambda tenant_id: dict(GATES_PARTS_REQUIRED),
    )

    _log_used(db, job, [{"part_id": str(part.id), "qty": 2}])

    resp = closeout_job(
        payload=CloseoutPayload(parts=[], hours=1.5),
        job_id=str(job.id),
        request=_request(),
        current_user=_user(),
        db=db,
    )
    assert resp.status_code == 201, getattr(resp, "body", resp)

    # Still exactly one billable row for that part — the closeout added none.
    rows = _rows(db, job)
    assert len(rows) == 1
    assert rows[0].source == "mobile"


def test_closeout_gate_still_bites_with_nothing_logged(tenant_db_session, monkeypatch):
    """The relaxation is evidence-based, not a hole: with no parts logged and
    no attestation, the gate still refuses."""
    db = tenant_db_session
    job = _seed_job(db)
    monkeypatch.setattr(
        "gdx_dispatch.routers.jobs._load_workflow_flags",
        lambda tenant_id: dict(GATES_PARTS_REQUIRED),
    )

    resp = closeout_job(
        payload=CloseoutPayload(parts=[], hours=1.5),
        job_id=str(job.id),
        request=_request(),
        current_user=_user(),
        db=db,
    )
    assert resp.status_code == 422
    assert "parts" in resp.body.decode()


def test_closeout_does_not_clobber_live_rows(tenant_db_session, mobile_job):
    """Re-closeout replaces its OWN unbilled rows. A part logged during the job
    is a separate event and must survive — and must not be merged into the
    closeout's list either."""
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, qty=10)
    mobile_job(db, job)

    _log_used(db, job, [{"part_id": str(part.id), "qty": 1}])

    for _ in range(2):  # closeout twice — the replace step runs on the second
        resp = closeout_job(
            payload=CloseoutPayload(
                parts=[{"name": "Hinge set", "qty": 2, "unit_cost": 3.0}],
                hours=1.0,
            ),
            job_id=str(job.id),
            request=_request(),
            current_user=_user(),
            db=db,
        )
        assert resp.status_code == 201, getattr(resp, "body", resp)

    live = _rows(db, job, source="mobile")
    closeout_rows = _rows(db, job, source="closeout")
    assert len(live) == 1 and live[0].quantity == 1
    assert len(closeout_rows) == 1 and closeout_rows[0].part_name == "Hinge set"


def test_line_needs_a_name_or_a_part_id(tenant_db_session):
    """A qty with nothing to name it is not a part."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        mobile_router.PartsUsedBody(parts=[{"qty": 2}])


# ---------------------------------------------------------------------------
# Undo reverses through the COST ROW, never the sku (adversarial audit
# 2026-08-13). The sku route answered 200 OK while getting inventory wrong four
# different ways.
# ---------------------------------------------------------------------------


def test_undo_of_a_free_text_line_never_credits_stock(tenant_db_session, mobile_job) -> None:
    """The phantom-stock case.

    A free-text line decrements NOTHING. If the office later adds that sku to
    inventory, a sku-based reversal invents stock that never left: qty 5 became
    qty 8 on an undo. No job_parts row means no debit happened, so there is
    nothing to give back.
    """
    db = tenant_db_session
    job = _seed_job(db)
    mobile_job(db, job)
    _log_used(db, job, [{"name": "Weatherseal", "sku": "SEAL-99", "qty": 3}])
    row = _rows(db, job, source="mobile")[0]

    # The office adds that sku to the catalog AFTER the capture.
    later = _seed_part(db, sku="SEAL-99", qty=5)

    resp = _undo(db, job, row.id)
    assert resp.status_code == 200
    db.refresh(later)
    assert int(later.qty_on_hand) == 5, "credited stock that was never debited"


def test_undo_does_not_delete_another_events_cost_row(tenant_db_session, mobile_job) -> None:
    """A closeout writes its own job_parts row for the same part and qty. It is
    NEWER, so 'newest first' deleted the closeout's row and left the mobile
    one — the undo removed the wrong money."""
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, qty=10)
    mobile_job(db, job)
    _log_used(db, job, [{"part_id": str(part.id), "qty": 2}])
    row = _rows(db, job, source="mobile")[0]

    # A later, unrelated cost row for the same part+qty (what a closeout writes).
    other = JobPart(
        id=uuid4(), job_id=job.id, part_id=part.id, qty_used=2,
        unit_cost_at_time=11.50,
        created_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add(other)
    db.commit()

    _undo(db, job, row.id)

    remaining = db.execute(select(JobPart).where(JobPart.job_id == job.id)).scalars().all()
    assert len(remaining) == 1
    assert float(remaining[0].unit_cost_at_time) == 11.50, "deleted the wrong cost row"


def test_undo_still_credits_after_the_sku_is_renamed(tenant_db_session, mobile_job) -> None:
    """Reversal keys on the cost row's part_id, so an office sku edit between
    capture and undo no longer strands the stock."""
    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, sku="SPR-OLD", qty=10)
    mobile_job(db, job)
    _log_used(db, job, [{"part_id": str(part.id), "qty": 4}])
    row = _rows(db, job, source="mobile")[0]
    db.refresh(part)
    assert int(part.qty_on_hand) == 6

    part.sku = "SPR-NEW"
    db.commit()

    _undo(db, job, row.id)
    db.refresh(part)
    assert int(part.qty_on_hand) == 10, "stock stranded after a sku rename"
    assert db.execute(select(JobPart).where(JobPart.job_id == job.id)).scalars().all() == []


def test_live_logged_parts_reach_the_autodrafted_invoice(tenant_db_session, mobile_job, monkeypatch):
    """The gate change's other half (adversarial audit, 2026-08-13).

    Accepting live rows as evidence at the completion gate, while the autodraft
    billed only source='closeout', meant a tech who logged parts as they went
    got an invoice with the labor and NONE of the parts — and the
    unpriced-parts warning reported zero, so nothing said so. The gate is the
    mechanism; billing is the point, and the original test stopped at the
    mechanism.
    """
    from gdx_dispatch.core.closeout_billing import build_closeout_lines

    db = tenant_db_session
    job = _seed_job(db)
    part = _seed_part(db, price=60.0, qty=10)
    mobile_job(db, job)
    _log_used(db, job, [{"part_id": str(part.id), "qty": 2}])

    invoice = Invoice(
        id=uuid4(), job_id=job.id, customer_id=uuid4(),
        invoice_number="INV-AUTODRAFT", subtotal=0, tax_amount=0, total=0,
        balance_due=0, status="draft", public_token=f"tok-{uuid4().hex}",
        company_id=TENANT,
    )
    db.add(invoice)
    db.flush()

    closeout = JobCloseout(
        id=uuid4(), job_id=job.id, parts_used=[], no_parts_used=False,
        hours_worked=0, closed_by_user_id="tech-1", closed_at=datetime.now(UTC),
    )
    db.add(closeout)
    db.flush()

    lines_added, lines_total, _taxable = build_closeout_lines(
        db, tenant_id=TENANT, invoice=invoice, closeout=closeout,
        job_type=None, job_id=str(job.id),
    )
    db.commit()

    assert lines_added == 1, "the live-logged part never reached the invoice"
    assert float(lines_total) == 120.00  # 2 × the catalog sell price
    # And it is stamped, so a second run can't bill it twice.
    row = _rows(db, job, source="mobile")[0]
    assert row.billed_invoice_id is not None
