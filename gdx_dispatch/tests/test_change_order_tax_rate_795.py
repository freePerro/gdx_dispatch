"""GDXA-795 — a change order quotes the rate its job bills.

The CO detail used to resolve the tenant default live on every read, while
the invoice bills the accepted estimate's quoted rate (M24, GDXA-842): a CO on
a repair quoted at 0% was shown and signed with tax the invoice never charged.
A CO now stamps ``tax_rate`` on create, on a re-link and again on approval
through ``modules/tax/service.resolve_rate_for_job``, invoice create's own
helper, with the job's accepted estimate's rate as the quote:

- install-lane job → 0 (the lane comes first);
- exempt customer → 0, on any lane;
- the job's accepted estimate carries a rate → that rate;
- otherwise (service/repair, no job) → the tenant default.

The maintainer's rulings on GDXA-795: a failed re-stamp keeps the prior stamp;
a signed (approved or completed) CO refuses a re-link with 409 until it is
reopened; approval re-stamps, so the signed number is current, and a signed CO
keeps that rate afterwards. An unsigned CO shows the rate approval would stamp
now; a signed pre-124 row (NULL) keeps the live resolve it always had.

Signed equals billed only when the CO is billed before any rate input moves:
invoice create reads the form's rate, not the stamp (parity at creation is
enough: the maintainer's ruling). The handlers run for real against an
ORM-built SQLite schema; nothing is mocked.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import (
    ChangeOrderLine,
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobPartNeeded,
    Payment,
)
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption
from gdx_dispatch.modules.tax.router import resolve_rate_endpoint
from gdx_dispatch.routers.change_orders import (
    ChangeOrder,
    ChangeOrderIn,
    approve_change_order,
    create_change_order,
    get_change_order,
    update_change_order,
)
from gdx_dispatch.routers.invoices import InvoiceCreateIn, create_invoice

DEFAULT = Decimal("0.0738")


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for tbl in (
        Job.__table__, Customer.__table__, Estimate.__table__, EstimateLine.__table__,
        Invoice.__table__, InvoiceLine.__table__, Payment.__table__,
        JobPartNeeded.__table__, ChangeOrder.__table__, ChangeOrderLine.__table__,
        TaxConfig.__table__, TaxExemption.__table__,
    ):
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(TaxConfig(default_rate=DEFAULT))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user() -> dict[str, str]:
    return {"user_id": "office-1", "tenant_id": "tenant-1", "role": "admin"}


def _job(db, *, job_type: str = "Service Call") -> Job:
    job = Job(
        customer_id=uuid4(), title="t", description="t", job_type=job_type,
        lifecycle_stage="in_progress", dispatch_status="assigned",
        billing_status="unbilled", company_id="tenant-1",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _accepted_estimate(db, job: Job, *, tax_rate: Decimal | None) -> Estimate:
    est = Estimate(
        id=uuid4(), estimate_number=f"EST-{uuid4().hex[:6]}", job_id=job.id,
        customer_id=job.customer_id, status="accepted", tax_rate=tax_rate,
        total=Decimal("1000.00"), accepted_at=datetime.now(UTC),
        company_id="tenant-1", public_token=uuid4().hex,
    )
    db.add(est)
    db.commit()
    return est


def _exempt(db, customer_id: UUID) -> None:
    db.add(TaxExemption(customer_id=customer_id, exempt=True, reason="resale"))
    db.commit()


def _create(db, job: Job | None, *, customer_id: UUID | None = None) -> dict:
    cid = customer_id or (job.customer_id if job else None)
    payload = ChangeOrderIn(
        job_id=str(job.id) if job else None,
        customer_id=str(cid) if cid else None,
        title="Add an opener",
        line_items=[{"description": "Opener", "quantity": 1, "unit_price": 400.0}],
    )
    return create_change_order(payload=payload, user=_user(), db=db)


def _detail(db, co_id: str) -> dict:
    return get_change_order(co_id=UUID(co_id), _=_user(), db=db)


def test_co_on_a_zero_percent_estimate_shows_signs_and_bills_zero(db):
    job = _job(db, job_type="Service Call")
    est = _accepted_estimate(db, job, tax_rate=Decimal("0"))
    co_id = _create(db, job)["id"]

    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == Decimal("0")
    shown = _detail(db, co_id)
    assert (shown["tax_rate"], shown["tax_amount"], shown["total"]) == (0.0, 0.0, 400.0)

    approve_change_order(co_id=UUID(co_id), user=_user(), db=db)
    signed = _detail(db, co_id)
    assert (signed["status"], signed["tax_amount"], signed["total"]) == ("approved", 0.0, 400.0)

    # The invoice the CO lands on charges what was signed: no tax.
    inv = _bill_like_the_office_form(db, job, co_id, est)
    assert Decimal(str(inv.tax_amount)) == Decimal("0")
    assert float(inv.total) == signed["total"]


def _bill_like_the_office_form(db, job: Job, co_id: str, est: Estimate | None) -> Invoice:
    """InvoiceCreateView's path: pick the job, which prefills from its accepted
    estimate and seeds the rate from /api/tax/resolve with the job and that
    estimate; claim the CO by id; send the estimate as provenance only."""
    seed = resolve_rate_endpoint(
        customer_id=str(job.customer_id), job_id=str(job.id),
        estimate_id=str(est.id) if est else None, user=_user(), db=db,
    )["rate"]
    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id, customer_id=job.customer_id, tax_rate=seed,
            source_estimate_id=est.id if est else None,
            from_change_order_ids=[UUID(co_id)],
        ),
        _=_user(),
        db=db,
    )
    return db.get(Invoice, UUID(created["id"]))


@pytest.mark.parametrize(
    ("job_type", "estimate_rate", "exempt"),
    [
        ("Service Call", Decimal("0"), False),
        ("Service Call", DEFAULT, True),
        ("Repair", None, False),
        ("Installation", None, False),
        ("Installation", Decimal("0.05"), False),
    ],
)
def test_the_signed_total_is_the_billed_total_when_billed_at_once(db, job_type, estimate_rate, exempt):
    job = _job(db, job_type=job_type)
    est = None
    if estimate_rate is not None:
        est = _accepted_estimate(db, job, tax_rate=estimate_rate)
    if exempt:
        _exempt(db, job.customer_id)
    co_id = _create(db, job)["id"]
    approve_change_order(co_id=UUID(co_id), user=_user(), db=db)
    signed = _detail(db, co_id)

    inv = _bill_like_the_office_form(db, job, co_id, est)
    assert (float(inv.tax_amount), float(inv.total)) == (signed["tax_amount"], signed["total"])


def test_exempt_customer_is_zero_even_against_a_taxed_estimate(db):
    job = _job(db)
    _accepted_estimate(db, job, tax_rate=DEFAULT)
    _exempt(db, job.customer_id)
    co_id = _create(db, job)["id"]

    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == Decimal("0")
    assert _detail(db, co_id)["tax_amount"] == 0.0


def test_service_job_with_no_estimate_gets_the_tenant_default(db):
    job = _job(db, job_type="Repair")
    co_id = _create(db, job)["id"]

    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == DEFAULT
    shown = _detail(db, co_id)
    assert shown["tax_rate"] == float(DEFAULT)
    assert shown["tax_amount"] == round(400 * float(DEFAULT), 2)


def test_install_job_with_no_estimate_is_zero(db):
    job = _job(db, job_type="Installation")
    co_id = _create(db, job)["id"]

    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == Decimal("0")
    assert _detail(db, co_id)["tax_amount"] == 0.0


def test_accepted_estimate_with_no_rate_falls_to_the_lane_rule(db):
    install = _job(db, job_type="Installation")
    _accepted_estimate(db, install, tax_rate=None)
    service = _job(db, job_type="Service Call")
    _accepted_estimate(db, service, tax_rate=None)

    assert db.get(ChangeOrder, UUID(_create(db, install)["id"])).tax_rate == Decimal("0")
    assert db.get(ChangeOrder, UUID(_create(db, service)["id"])).tax_rate == DEFAULT


def test_a_draft_or_declined_estimate_is_not_the_jobs_estimate(db):
    job = _job(db)
    for status in ("sent", "declined"):
        db.add(Estimate(
            id=uuid4(), estimate_number=f"EST-{uuid4().hex[:6]}", job_id=job.id,
            status=status, tax_rate=Decimal("0"), company_id="tenant-1",
            public_token=uuid4().hex,
        ))
    db.commit()
    assert db.get(ChangeOrder, UUID(_create(db, job)["id"])).tax_rate == DEFAULT


def _set_default(db, rate: str) -> None:
    db.execute(TaxConfig.__table__.update().values(default_rate=Decimal(rate)))
    db.commit()


def test_a_signed_co_keeps_its_rate_after_a_tenant_default_change(db):
    # A repair: the live resolve would follow the new default, the stamp holds.
    job = _job(db, job_type="Repair")
    co_id = _create(db, job)["id"]
    approve_change_order(co_id=UUID(co_id), user=_user(), db=db)
    _set_default(db, "0.09")
    shown = _detail(db, co_id)
    assert shown["tax_rate"] == float(DEFAULT)
    assert shown["tax_amount"] == round(400 * float(DEFAULT), 2)
    # Approving it again through the API does not re-quote a signed CO.
    approve_change_order(co_id=UUID(co_id), user=_user(), db=db)
    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == DEFAULT


def test_approval_restamps_at_the_rate_in_force_and_audits_it(db):
    """The maintainer's ruling: re-stamp at approval so the signed number is
    current. A draft written at 7.38% and approved after the default moved to
    9% signs at 9%, the rate its invoice will be seeded with."""
    job = _job(db, job_type="Repair")
    co_id = _create(db, job)["id"]
    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == DEFAULT
    _set_default(db, "0.09")

    approve_change_order(co_id=UUID(co_id), user=_user(), db=db)
    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == Decimal("0.09")
    signed = _detail(db, co_id)
    assert signed["tax_amount"] == 36.0

    row = db.execute(
        select(AuditLog).where(AuditLog.action == "approve_change_order")
    ).scalar_one()
    assert row.user_id == "office-1"
    moved = row.details["tax_rate"]
    assert (Decimal(moved["old"]), Decimal(moved["new"])) == (DEFAULT, Decimal("0.09"))

    inv = _bill_like_the_office_form(db, job, co_id, None)
    assert (float(inv.tax_amount), float(inv.total)) == (signed["tax_amount"], signed["total"])


def test_approving_through_the_edit_dialog_restamps_too(db):
    """The edit dialog's status select reaches `approved` by PATCH, not POST
    /approve; the signed number has to be current on that path as well."""
    job = _job(db, job_type="Repair")
    co_id = _create(db, job)["id"]
    _set_default(db, "0.09")
    update_change_order(
        co_id=UUID(co_id),
        payload=ChangeOrderIn(title="Add an opener", status="approved"),
        current_user=_user(),
        db=db,
    )
    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == Decimal("0.09")
    row = db.execute(
        select(AuditLog).where(AuditLog.action == "update_change_order")
    ).scalar_one()
    moved = row.details["changed"]["tax_rate"]
    assert (Decimal(moved["old"]), Decimal(moved["new"])) == (DEFAULT, Decimal("0.09"))


def test_completing_a_draft_signs_it_at_the_current_rate(db):
    """The second audit's finding: the edit dialog reaches `completed`, the
    other signed status, straight from a draft. That froze the create-time
    stamp, so an exempt customer's CO showed tax the invoice never charges,
    and a later move to approved kept it (completed is already signed)."""
    job = _job(db, job_type="Service Call")
    co_id = _create(db, job)["id"]
    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == DEFAULT
    _exempt(db, job.customer_id)

    for status in ("completed", "approved"):
        update_change_order(
            co_id=UUID(co_id),
            payload=ChangeOrderIn(title="Add an opener", status=status),
            current_user=_user(),
            db=db,
        )
        assert db.get(ChangeOrder, UUID(co_id)).tax_rate == Decimal("0"), status
        assert _detail(db, co_id)["tax_amount"] == 0.0, status


@pytest.mark.parametrize("signed_status", ["approved", "completed"])
def test_a_signed_co_refuses_a_relink_until_it_is_reopened(db, signed_status):
    from fastapi import HTTPException

    service = _job(db, job_type="Service Call")
    co_id = _create(db, service)["id"]
    update_change_order(
        co_id=UUID(co_id),
        payload=ChangeOrderIn(title="Add an opener", status=signed_status),
        current_user=_user(),
        db=db,
    )
    install = _job(db, job_type="Installation")
    relink = ChangeOrderIn(
        title="Add an opener", job_id=str(install.id),
        customer_id=str(service.customer_id), status=signed_status,
    )
    with pytest.raises(HTTPException) as refused:
        update_change_order(co_id=UUID(co_id), payload=relink, current_user=_user(), db=db)
    assert refused.value.status_code == 409
    assert "draft or pending approval" in refused.value.detail
    db.rollback()
    co = db.get(ChangeOrder, UUID(co_id))
    assert (co.job_id, co.status, co.tax_rate) == (service.id, signed_status, DEFAULT)

    # Reopening and re-linking in the same save is still refused: the stored
    # status decides, so the reopen is a save of its own.
    both = relink.model_copy(update={"status": "draft"})
    with pytest.raises(HTTPException):
        update_change_order(co_id=UUID(co_id), payload=both, current_user=_user(), db=db)
    db.rollback()

    reopen = ChangeOrderIn(
        title="Add an opener", job_id=str(service.id),
        customer_id=str(service.customer_id), status="pending_approval",
    )
    update_change_order(co_id=UUID(co_id), payload=reopen, current_user=_user(), db=db)
    update_change_order(
        co_id=UUID(co_id),
        payload=relink.model_copy(update={"status": "pending_approval"}),
        current_user=_user(),
        db=db,
    )
    co = db.get(ChangeOrder, UUID(co_id))
    assert (co.job_id, co.tax_rate) == (install.id, Decimal("0"))


def test_an_unmoved_save_of_a_signed_co_is_not_refused(db):
    """The edit dialog re-sends the job and customer on every save; the same
    ids are not a re-link."""
    job = _job(db)
    co_id = _create(db, job)["id"]
    approve_change_order(co_id=UUID(co_id), user=_user(), db=db)
    update_change_order(
        co_id=UUID(co_id),
        payload=ChangeOrderIn(
            title="Renamed", job_id=str(job.id), customer_id=str(job.customer_id), status="approved",
        ),
        current_user=_user(),
        db=db,
    )
    assert db.get(ChangeOrder, UUID(co_id)).title == "Renamed"


def test_the_stamp_stages_no_write_inside_the_read_savepoint(db, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    _create(db, _job(db))
    assert "contained_read_staged_a_write" not in caplog.text


def test_a_signed_pre_124_row_keeps_the_live_resolve(db):
    job = _job(db, job_type="Service Call")
    _accepted_estimate(db, job, tax_rate=Decimal("0"))
    co_id = _create(db, job)["id"]
    approve_change_order(co_id=UUID(co_id), user=_user(), db=db)
    db.execute(ChangeOrder.__table__.update().values(tax_rate=None))
    db.commit()
    db.expire_all()
    # Legacy signed NULL: the live resolve it always had (lane-aware since
    # GDXA-797), which never reads the estimate — not the 0 a stamp would carry.
    assert _detail(db, co_id)["tax_rate"] == float(DEFAULT)


def test_a_draft_shows_the_rate_approval_would_stamp_now(db):
    """The first audit's finding: a draft showed its create-time stamp, so a
    job turned into an installation after the CO was written still showed tax,
    and approval re-quoted the number without anyone seeing it first."""
    job = _job(db, job_type="Repair")
    co_id = _create(db, job)["id"]
    _set_default(db, "0.09")
    shown = _detail(db, co_id)
    assert (shown["tax_rate"], shown["total"]) == (0.09, 436.0)

    db.execute(Job.__table__.update().where(Job.id == job.id).values(job_type="Installation"))
    db.commit()
    shown = _detail(db, co_id)
    assert (shown["tax_amount"], shown["total"]) == (0.0, 400.0)

    approve_change_order(co_id=UUID(co_id), user=_user(), db=db)
    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == Decimal("0")
    assert _detail(db, co_id)["total"] == 400.0


def test_create_audits_the_stamped_rate_and_the_acting_user(db):
    co_id = _create(db, _job(db, job_type="Installation"))["id"]
    row = db.execute(
        select(AuditLog).where(AuditLog.action == "create_change_order")
    ).scalar_one()
    assert row.entity_id == co_id
    assert row.user_id == "office-1"
    assert Decimal(row.details["tax_rate"]) == Decimal("0")
    assert db.get(ChangeOrder, UUID(co_id)).created_by == "office-1"


def test_relinking_to_another_job_restamps_and_audits_the_change(db):
    service = _job(db, job_type="Service Call")
    co_id = _create(db, service)["id"]
    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == DEFAULT

    install = _job(db, job_type="Installation")
    update_change_order(
        co_id=UUID(co_id),
        payload=ChangeOrderIn(
            title="Add an opener", job_id=str(install.id),
            customer_id=str(service.customer_id),
        ),
        current_user=_user(),
        db=db,
    )
    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == Decimal("0")
    row = db.execute(
        select(AuditLog).where(AuditLog.action == "update_change_order")
    ).scalar_one()
    assert row.user_id == "office-1"
    moved = row.details["changed"]["tax_rate"]
    assert (Decimal(moved["old"]), Decimal(moved["new"])) == (DEFAULT, Decimal("0"))

    # An edit that keeps the job and customer leaves the rate alone.
    _set_default(db, "0.09")
    update_change_order(
        co_id=UUID(co_id),
        payload=ChangeOrderIn(title="Renamed"),
        current_user=_user(),
        db=db,
    )
    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == Decimal("0")


def test_a_failed_restamp_keeps_the_prior_rate(db, monkeypatch, caplog):
    """The maintainer's ruling: a re-link whose rate read fails keeps the
    stamp it had, with a loud log — not NULL, which would hand a signed CO
    back to the live resolve."""
    from gdx_dispatch.modules.tax import service

    service_job = _job(db, job_type="Service Call")
    co_id = _create(db, service_job)["id"]
    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == DEFAULT

    def broken(*_a, **_k):
        raise RuntimeError("tax read failed")

    monkeypatch.setattr(service, "resolve_rate_for_job", broken)
    install = _job(db, job_type="Installation")
    update_change_order(
        co_id=UUID(co_id),
        payload=ChangeOrderIn(
            title="Add an opener", job_id=str(install.id),
            customer_id=str(service_job.customer_id),
        ),
        current_user=_user(),
        db=db,
    )
    co = db.get(ChangeOrder, UUID(co_id))
    assert co.job_id == install.id, "the re-link itself still lands"
    assert co.tax_rate == DEFAULT
    assert "change_order_tax_stamp_failed" in caplog.text


def test_an_install_job_is_zero_even_against_a_rated_estimate(db):
    """The lane comes before the quote, as on invoice create (GDXA-842), so
    the CO signs what the invoice bills."""
    job = _job(db, job_type="Installation")
    _accepted_estimate(db, job, tax_rate=Decimal("0.05"))
    assert db.get(ChangeOrder, UUID(_create(db, job)["id"])).tax_rate == Decimal("0")


def test_a_co_with_no_customer_is_exempt_through_its_job(db):
    """A tech's CO can be saved with no customer; exemption follows the job's."""
    job = _job(db)
    _exempt(db, job.customer_id)
    payload = ChangeOrderIn(
        job_id=str(job.id), title="Add an opener",
        line_items=[{"description": "Opener", "quantity": 1, "unit_price": 400.0}],
    )
    co_id = create_change_order(payload=payload, user=_user(), db=db)["id"]
    assert db.get(ChangeOrder, UUID(co_id)).tax_rate == Decimal("0")
