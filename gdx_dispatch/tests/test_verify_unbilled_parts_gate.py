"""Verify refuses while parts recorded on the job are billed to nothing.

Follow-up 2 of `closeout-parts-autopricing-plan.md`. The unbilled-parts warning
was a banner on `InvoiceDetailView` — client-side, so it could not help:

* the **accounting role**, which holds `invoices.write` but NOT
  `inventory.read`, so the banner's own fetch 403s and renders an empty
  all-clear on a money screen — the very user who verifies drafts,
* any **API caller**, which never runs the screen at all.

The plan also named the mobile lane. That part was rhetoric and is corrected
here: **no mobile surface calls `/verify`** — `routers/mobile_invoicing.py`
only READS `verified_at` and 409s on send. Mobile could never verify, so the
gate does not "bind" it; what it binds is every caller that can.

Verification is still the right place: it is the one approval an invoice must
pass before a customer can see it, and it already row-locks.

Draft-only, mirroring the banner. Prod carries thousands of pre-rail invoices
with `verified_at` NULL and status sent/paid; verifying one of those is a
backfill of an approval that already happened, not a review of work still to
be billed.

It refuses rather than warns, and lists what is missing — but an
acknowledgement lets the office through, because plenty of parts legitimately
go unbilled (warranty, goodwill, already covered by a flat price). The office
decides; the server only makes sure they were asked, and records the answer.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, ensure_audit_table
from gdx_dispatch.models.tenant_models import (
    Invoice,
    InvoiceLine,
    Job,
    JobPartNeeded,
)
from gdx_dispatch.routers.invoices import VerifyInvoiceIn, verify_invoice

TENANT = "tenant-verify-gate"
USER = {"sub": "user-office", "email": "office@example.com", "role": "accounting"}


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    ensure_audit_table(session)
    yield session
    session.close()
    engine.dispose()


def _job(db) -> Job:
    job = Job(
        customer_id=uuid.uuid4(),
        title="Opener install",
        description="",
        lifecycle_stage="estimate",
        dispatch_status="unassigned",
        billing_status="unbilled",
        company_id=TENANT,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _invoice(db, job: Job | None) -> Invoice:
    inv = Invoice(
        id=uuid.uuid4(),
        job_id=job.id if job else None,
        customer_id=(job.customer_id if job else uuid.uuid4()),
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        status="draft",
        total=Decimal("100.00"),
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("0"),
        balance_due=Decimal("100.00"),
        invoice_date=datetime.now(UTC).date(),
        public_token=uuid.uuid4().hex,
        company_id=TENANT,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _part(db, job: Job, name: str, *, status: str = "received", billed=None) -> JobPartNeeded:
    part = JobPartNeeded(
        id=str(uuid.uuid4()),
        company_id=TENANT,
        job_id=str(job.id),
        part_name=name,
        quantity=1,
        status=status,
        unit_price=Decimal("149.00"),
        billed_invoice_id=billed,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _line(db, inv: Invoice, description: str, part_id: str | None = None) -> InvoiceLine:
    line = InvoiceLine(
        id=uuid.uuid4(),
        invoice_id=inv.id,
        description=description,
        quantity=1,
        unit_price=Decimal("149.00"),
        line_total=Decimal("149.00"),
        part_id=part_id,
        company_id=TENANT,
    )
    db.add(line)
    db.commit()
    return line


def _verify(db, inv: Invoice, *, acknowledge: bool | None = None):
    payload = None if acknowledge is None else VerifyInvoiceIn(
        acknowledge_unbilled_parts=acknowledge
    )
    return verify_invoice(invoice_id=str(inv.id), payload=payload, db=db, _=USER)


# ── the gate ───────────────────────────────────────────────────────────────


def test_verify_refuses_while_a_recorded_part_is_billed_to_nothing(db):
    job = _job(db)
    inv = _invoice(db, job)
    _part(db, job, "Torsion spring")

    with pytest.raises(HTTPException) as exc:
        _verify(db, inv)
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["unbilled_parts"][0]["part_name"] == "Torsion spring"
    # Names the way out. A 409 that does not say how to proceed is a dead end.
    assert detail["acknowledge_field"] == "acknowledge_unbilled_parts"
    db.refresh(inv)
    assert inv.verified_at is None, "a refused verify must not stamp"


def test_an_absent_body_is_not_an_acknowledgement(db):
    """Every existing API caller sends no body. They must get the gate, not
    sail past it — the whole point is binding callers that never drew the
    banner."""
    job = _job(db)
    inv = _invoice(db, job)
    _part(db, job, "Cable drum")

    with pytest.raises(HTTPException) as exc:
        _verify(db, inv, acknowledge=None)
    assert exc.value.status_code == 409


def test_the_office_can_acknowledge_and_proceed(db):
    """Warranty, goodwill, already covered by a flat price — plenty of parts
    legitimately go unbilled. The office decides."""
    job = _job(db)
    inv = _invoice(db, job)
    _part(db, job, "Goodwill roller")

    out = _verify(db, inv, acknowledge=True)
    assert out["already_verified"] is False
    db.refresh(inv)
    assert inv.verified_at is not None


def test_acknowledging_past_a_warning_is_recorded_as_such(db):
    """"Who approved this and what did they know" is the point of the stamp.
    Verifying with nothing outstanding and verifying PAST a warning are
    different acts and the trail has to tell them apart."""
    job = _job(db)
    inv = _invoice(db, job)
    _part(db, job, "Goodwill roller")

    _verify(db, inv, acknowledge=True)
    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "invoice_verified")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row.details["acknowledged_unbilled_parts"] == ["Goodwill roller"]


def test_a_clean_invoice_verifies_with_no_ceremony_and_no_false_record(db):
    """The counterfactual. If this failed, the gate would be blocking every
    verify and the acknowledgement key would be meaningless."""
    job = _job(db)
    inv = _invoice(db, job)

    out = _verify(db, inv)
    assert out["already_verified"] is False
    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "invoice_verified")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row.details["acknowledged_unbilled_parts"] is None, (
        "an invoice with nothing outstanding must not record an acknowledgement"
    )


# ── what must NOT trip it ──────────────────────────────────────────────────


def test_a_part_already_on_this_invoice_is_not_reported_missing(db):
    """The trap. "Unbilled" is job-wide; this gate claims something narrower —
    not on THIS invoice. Reporting a part that is already charged here would
    push the office to add a second line for it, and the new claim would
    silence the warning: a false alarm laundering itself into a double charge.
    """
    job = _job(db)
    inv = _invoice(db, job)
    part = _part(db, job, "Torsion spring")
    _line(db, inv, "Torsion spring", part_id=part.id)

    out = _verify(db, inv)
    assert out["already_verified"] is False


def test_a_part_billed_on_another_invoice_is_not_reported(db):
    job = _job(db)
    inv = _invoice(db, job)
    other = _invoice(db, job)
    _part(db, job, "Roller", billed=other.id)

    assert _verify(db, inv)["already_verified"] is False


def test_the_offices_dismiss_verb_is_respected(db):
    """`wont_bill` is how the office declines a part — warranty, goodwill,
    already flat-priced. Re-raising it at verify is how a warning becomes
    wallpaper."""
    job = _job(db)
    inv = _invoice(db, job)
    _part(db, job, "Warranty spring", status="wont_bill")

    assert _verify(db, inv)["already_verified"] is False


def test_a_part_still_only_needed_does_not_block(db):
    """Status `needed` means nobody has confirmed the part exists yet — the
    banner filters it out for the same reason, and the two definitions of
    "missing" must not diverge."""
    job = _job(db)
    inv = _invoice(db, job)
    _part(db, job, "Maybe a spring", status="needed")

    assert _verify(db, inv)["already_verified"] is False


def test_a_counter_sale_with_no_job_has_no_parts_checklist(db):
    inv = _invoice(db, None)
    assert _verify(db, inv)["already_verified"] is False


def test_a_sent_invoice_is_not_gated(db):
    """Draft-only, mirroring the banner — and this is not academic.

    `require_deliverable` gates drafts only because prod carries thousands of
    pre-rail invoices with `verified_at` NULL and status sent/paid. Verifying
    one of those is a backfill of an approval that already happened in the
    world, not a review of work still to be billed. Firing here would raise a
    warning on invoices no banner has ever shown, about parts the office
    decided months ago — and the screen's own "Add them" route is closed,
    because editing is draft-only too.
    """
    job = _job(db)
    inv = _invoice(db, job)
    inv.status = "sent"
    db.commit()
    _part(db, job, "Spring nobody billed")

    assert _verify(db, inv)["already_verified"] is False


def test_a_paid_invoice_is_not_gated(db):
    job = _job(db)
    inv = _invoice(db, job)
    inv.status = "paid"
    db.commit()
    _part(db, job, "Spring nobody billed")

    assert _verify(db, inv)["already_verified"] is False


def test_an_already_verified_invoice_is_not_re_gated(db):
    """Idempotent verify keeps the FIRST stamp. Gating the second call would
    turn a no-op into a 409 for an approval that already happened."""
    job = _job(db)
    inv = _invoice(db, job)
    _verify(db, inv)
    _part(db, job, "Part recorded after approval")

    out = _verify(db, inv)
    assert out["already_verified"] is True
