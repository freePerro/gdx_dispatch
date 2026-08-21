"""Money correctness probes — executable versions of the 2026-08-04 money audit.

Every test here asserts what SHOULD be true. A failure is a proven defect, not
an opinion; a pass proves the invariant actually holds on this branch. This is
the difference between "I read the code and think X" and evidence.

Companion to docs/design/money-audit-2026-08-04.md — each test names the finding
it decides (M1, M7, …).

These began as DIAGNOSTIC probes — 9 of the 10 failed by design when this file
was written, so it carried the `health` marker that pytest.ini excludes from
the default suite (`-m "not e2e and not load and not health"`), and adding it
did not turn CI red.

**All ten now pass, so the marker is gone and these run on every merge.** That
was the condition this docstring set for itself, and it was met on 2026-08-04
when the last finding was fixed — the marker then sat here for another two
weeks, which meant ten green probes guarding the money invariants and not one
of them executing. A test excluded from the default gate is not a regression
net; it is a file. Do not re-add the marker to quiet a failure: a red probe
here is a proven money defect, and that is exactly the signal this file exists
to raise.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    Invoice,
    InvoiceLine,
    Job,
    Payment,
)
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    PaymentCreateIn,
    _recalculate_invoice,
    create_invoice,
    delete_invoice,
    record_payment,
)

# No module-level marker: these run in the default suite. See the docstring for
# why the `health` marker was here and why removing it was the whole point.

TENANT = "tenant-test"


def _user():
    return {"sub": "u-1", "user_id": "u-1", "role": "admin", "tenant_id": TENANT}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    # `tenant_settings` is read by raw SQL (estimate feature flags) and is not
    # part of TenantBase metadata, so create_all doesn't make it. Seed the row
    # the estimate paths read; without it the conversion probes die on a
    # missing table and look like findings when they are harness gaps.
    from sqlalchemy import text as _t

    with engine.begin() as conn:
        conn.execute(_t("""
            CREATE TABLE IF NOT EXISTS tenant_settings (
                tenant_id TEXT PRIMARY KEY,
                estimates_allow_line_margin_override BOOLEAN DEFAULT 0,
                estimates_default_terms TEXT DEFAULT '',
                estimate_email_subject_template TEXT DEFAULT '',
                estimate_email_body_template TEXT DEFAULT '',
                estimate_deposit_pct NUMERIC DEFAULT 50,
                estimates_hide_line_prices BOOLEAN DEFAULT 0,
                estimate_expiry_days INTEGER DEFAULT 60
            )
        """))
        conn.execute(_t("INSERT INTO tenant_settings (tenant_id) VALUES (:t)"), {"t": TENANT})
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = Session()
    yield sess
    sess.close()
    engine.dispose()


def _seed_job(db) -> Job:
    job = Job(
        customer_id=uuid4(),
        title="Install",
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


def _create_invoice(db, *, tax_rate=None, lines):
    job = _seed_job(db)
    payload = InvoiceCreateIn(
        job_id=job.id,
        customer_id=job.customer_id,
        tax_rate=tax_rate,
        line_items=[InvoiceLineCreateIn(**ln) for ln in lines],
        invoice_date=date.today(),
    )
    return create_invoice(payload=payload, _=_user(), db=db)


def _f(v) -> float:
    return float(v or 0)


def _totals_invariant(inv: Invoice, db) -> tuple[float, float]:
    """Return (stored total, Σ active lines + tax) — these must be equal."""
    rows = db.execute(
        select(InvoiceLine).where(
            InvoiceLine.invoice_id == inv.id, InvoiceLine.deleted_at.is_(None)
        )
    ).scalars().all()
    line_sum = sum(Decimal(str(r.line_total or 0)) for r in rows)
    return _f(inv.total), float(line_sum + Decimal(str(inv.tax_amount or 0)))


# --------------------------------------------------------------------------
# M2 — payment idempotency needs a DB constraint, not check-then-insert
# --------------------------------------------------------------------------

def test_m2_payments_have_a_unique_constraint_on_invoice_and_reference(db):
    """Two rows with the same (invoice_id, reference) must be impossible.

    `_mark_invoice_paid` dedupes by SELECT-then-INSERT, which two concurrent
    transactions both pass. Only a DB constraint actually closes it.
    """
    insp = inspect(db.get_bind())
    uniques = insp.get_unique_constraints("payments")
    indexes = [ix for ix in insp.get_indexes("payments") if ix.get("unique")]
    covering = [
        c for c in (*uniques, *indexes)
        if set(c.get("column_names") or []) >= {"invoice_id", "reference"}
    ]
    assert covering, (
        "no unique constraint/index on payments(invoice_id, reference) — "
        "confirm+webhook can both insert the same Stripe charge"
    )


def test_m2_recording_the_same_reference_twice_yields_one_payment(db):
    """Sequential double-record of one charge must not double-count."""
    inv = _create_invoice(db, lines=[{"description": "Door", "quantity": 1, "unit_price": 500.0}])
    inv_id = inv["id"]
    from uuid import UUID as _U

    for _ in range(2):
        try:
            record_payment(
                invoice_id=_U(inv_id),
                payload=PaymentCreateIn(
                    amount=500.0, method="card", date=date.today(), reference="pi_dup_test"
                ),
                _=_user(),
                db=db,
            )
        except Exception:
            pass  # a refusal is a PASS for this invariant

    db.expire_all()
    n = db.execute(
        select(Payment).where(Payment.invoice_id == _U(inv_id), Payment.reference == "pi_dup_test")
    ).scalars().all()
    assert len(n) == 1, f"{len(n)} payment rows recorded for one reference — money double-counted"


# --------------------------------------------------------------------------
# M11 — overpayment must be visible somewhere
# --------------------------------------------------------------------------

def test_m11_overpayment_is_detectable(db):
    """Paying more than the total must leave a visible trace.

    balance_due clamps at 0, so if nothing else surfaces the excess it is
    invisible to every screen and report.
    """
    from uuid import UUID as _U

    inv = _create_invoice(db, lines=[{"description": "Door", "quantity": 1, "unit_price": 500.0}])
    record_payment(
        invoice_id=_U(inv["id"]),
        payload=PaymentCreateIn(amount=600.0, method="cash", date=date.today()),
        _=_user(),
        db=db,
    )
    db.expire_all()
    row = db.get(Invoice, _U(inv["id"]))
    paid = sum(
        _f(p.amount)
        for p in db.execute(
            select(Payment).where(Payment.invoice_id == row.id, Payment.voided_at.is_(None))
        ).scalars().all()
    )
    assert paid == 600.0 and _f(row.total) == 500.0  # setup sanity

    from gdx_dispatch.routers.invoices import _serialize_invoice

    payload = _serialize_invoice(row)
    surfaced = any(
        "overpaid" in k or "overpayment" in k or "credit" in k for k in payload
    ) or _f(payload.get("balance_due")) < 0
    assert surfaced, (
        "invoice payload exposes no overpayment/credit field and balance clamps to "
        f"{payload.get('balance_due')} — $100.00 of customer money is invisible"
    )


# --------------------------------------------------------------------------
# M37 — deleting a draft must not orphan its payments
# --------------------------------------------------------------------------

def test_m37_cannot_delete_a_draft_that_has_payments(db):
    """void_invoice refuses while payments exist; delete must too."""
    from fastapi import HTTPException
    from uuid import UUID as _U

    inv = _create_invoice(db, lines=[{"description": "Door", "quantity": 1, "unit_price": 500.0}])
    record_payment(
        invoice_id=_U(inv["id"]),
        payload=PaymentCreateIn(amount=200.0, method="check", date=date.today()),
        _=_user(),
        db=db,
    )
    db.expire_all()
    assert db.get(Invoice, _U(inv["id"])).status == "draft"  # setup sanity

    with pytest.raises(HTTPException) as exc:
        delete_invoice(invoice_id=_U(inv["id"]), current_user=_user(), db=db)
    assert exc.value.status_code == 409, (
        "a draft carrying a $200 payment was deletable — the payment row survives "
        "but every AR surface joins through non-deleted invoices, so the cash vanishes"
    )


# --------------------------------------------------------------------------
# M9 / M10 — the totals invariant must survive a line edit on every invoice
# --------------------------------------------------------------------------

def test_m9_a_taxed_invoice_carries_its_rate_so_tax_tracks_the_lines(db):
    """A taxed invoice must store the RATE, not just the dollar amount.

    The mobile and one-click paths used to stamp tax_amount with tax_rate
    NULL, which puts the invoice on the legacy branch of
    `_recalculate_invoice`: the flat tax is preserved verbatim while the
    subtotal moves. Both paths now store the rate, and migration 056 derives
    one for existing rows. The invariant that matters: if an invoice carries
    tax, adding a line must move the tax with it.
    """
    from uuid import UUID as _U

    inv = _create_invoice(
        db,
        tax_rate=0.07375,
        lines=[{"description": "Door", "quantity": 1, "unit_price": 1000.0}],
    )
    row = db.get(Invoice, _U(inv["id"]))
    assert row.tax_rate is not None, "a taxed invoice was created with no rate"
    baseline_tax = _f(row.tax_amount)
    assert baseline_tax == pytest.approx(73.75, abs=0.005)  # setup sanity

    db.add(
        InvoiceLine(
            invoice_id=row.id, company_id=TENANT, description="Extra",
            quantity=1, unit_price=Decimal("500.00"), line_total=Decimal("500.00"),
            sort_order=2,
        )
    )
    db.flush()
    _recalculate_invoice(row, db)
    db.commit()
    db.expire_all()

    row = db.get(Invoice, _U(inv["id"]))
    assert _f(row.subtotal) == 1500.0  # setup sanity
    assert _f(row.tax_amount) == pytest.approx(110.63, abs=0.005), (
        f"subtotal grew 1000 -> 1500 but tax is {_f(row.tax_amount)} "
        "(expected 110.63) — tax did not follow the lines"
    )
    stored, derived = _totals_invariant(row, db)
    assert stored == pytest.approx(derived, abs=0.005), (
        f"total {stored} != Σlines+tax {derived}"
    )


# --------------------------------------------------------------------------
# M1 — imported invoices whose lines disagree with their total
# --------------------------------------------------------------------------

def test_m1_recalc_does_not_destroy_an_imported_invoice_total(db):
    """The QB-import shape: correct stored total, lossy/duplicated lines.

    Reproduces prod invoice #1111 (lines $2,741.50, persisted total $1,471.84)
    and asserts that recording the settling payment does not re-open it.
    """
    from uuid import UUID as _U

    job = _seed_job(db)
    inv = Invoice(
        job_id=job.id, customer_id=job.customer_id, company_id=TENANT,
        invoice_number="QB-IMPORT-A", billing_type="standard", sequence_number=1,
        subtotal=Decimal("1471.84"), tax_amount=Decimal("0"),
        total=Decimal("1471.84"), balance_due=Decimal("1471.84"),
        status="sent", invoice_date=date.today(), public_token=uuid4().hex,
        notes="Imported from QuickBooks",
        # The importer stamps this (and migration 056 backfills it for rows
        # imported before the fix): the QB header total is authoritative and
        # the local lines are lossy, so recalc must not re-derive from them.
        totals_locked=True,
    )
    db.add(inv)
    db.flush()
    # QB returns the item line AND a SubTotalLine; the create path writes both.
    for desc in ("Garage door service", "Subtotal"):
        db.add(
            InvoiceLine(
                invoice_id=inv.id, company_id=TENANT, description=desc, quantity=1,
                unit_price=Decimal("1471.84"), line_total=Decimal("1471.84"), sort_order=1,
            )
        )
    db.commit()

    record_payment(
        invoice_id=_U(str(inv.id)),
        payload=PaymentCreateIn(amount=1471.84, method="check", date=date.today()),
        _=_user(),
        db=db,
    )
    db.expire_all()
    row = db.get(Invoice, inv.id)
    assert _f(row.total) == pytest.approx(1471.84, abs=0.005), (
        f"imported total was rewritten {1471.84} -> {_f(row.total)} from duplicated lines"
    )
    assert _f(row.balance_due) == pytest.approx(0.0, abs=0.005), (
        f"a fully-paid imported invoice re-opened owing {_f(row.balance_due)}"
    )


def test_m1_recalc_does_not_zero_a_lineless_imported_invoice(db):
    """282 imported invoices historically had no lines at all."""
    from uuid import UUID as _U

    job = _seed_job(db)
    inv = Invoice(
        job_id=job.id, customer_id=job.customer_id, company_id=TENANT,
        invoice_number="QB-IMPORT-B", billing_type="standard", sequence_number=1,
        subtotal=Decimal("650.00"), tax_amount=Decimal("0"),
        total=Decimal("650.00"), balance_due=Decimal("650.00"),
        status="sent", invoice_date=date.today(), public_token=uuid4().hex,
        notes="Imported from QuickBooks",
        # The importer stamps this (and migration 056 backfills it for rows
        # imported before the fix): the QB header total is authoritative and
        # the local lines are lossy, so recalc must not re-derive from them.
        totals_locked=True,
    )
    db.add(inv)
    db.commit()

    record_payment(
        invoice_id=_U(str(inv.id)),
        payload=PaymentCreateIn(amount=650.0, method="check", date=date.today()),
        _=_user(),
        db=db,
    )
    db.expire_all()
    row = db.get(Invoice, inv.id)
    assert _f(row.total) == pytest.approx(650.00, abs=0.005), (
        f"line-less imported invoice total destroyed: 650.00 -> {_f(row.total)}"
    )


# --------------------------------------------------------------------------
# M7 / M24 — estimate -> invoice must preserve what the customer accepted
# --------------------------------------------------------------------------

def _seed_estimate(db, *, discount=None, tax_rate=None, lines):
    """An accepted estimate with lines. `lines` = [(desc, category, price)]."""
    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine

    job = _seed_job(db)
    gross = sum(Decimal(str(p)) for _, _, p in lines)
    disc = Decimal(str(discount or 0))
    est = Estimate(
        job_id=job.id, customer_id=job.customer_id, company_id=TENANT,
        estimate_number=f"EST-{uuid4().hex[:6]}", public_token=uuid4().hex,
        tax_rate=Decimal(str(tax_rate)) if tax_rate is not None else None,
        discount=disc or None,
        # `Estimate.total` is the GROSS line sum — `compute_estimate_totals`
        # subtracts the discount from it. Seeding the net here would make the
        # probe subtract it twice and test the wrong thing.
        total=gross,
        status="accepted",
    )
    db.add(est)
    db.flush()
    for i, (desc, cat, price) in enumerate(lines, start=1):
        db.add(
            EstimateLine(
                estimate_id=est.id, company_id=TENANT, description=desc, category=cat,
                quantity=1, unit_price=Decimal(str(price)), line_total=Decimal(str(price)),
                sort_order=i,
            )
        )
    db.commit()
    db.refresh(est)
    return job, est


def test_m7_estimate_discount_survives_conversion_and_recalc(db):
    """A $500 discount the customer accepted must not reappear as billable."""
    from uuid import UUID as _U

    from gdx_dispatch.modules.proposals.totals import compute_estimate_totals

    job, est = _seed_estimate(
        db, discount=500, lines=[("Door", "materials", 3000.0), ("Opener", "materials", 2000.0)]
    )
    # Setup sanity: `est.total` is the GROSS line sum; the number the customer
    # actually accepted is what compute_estimate_totals returns (gross − discount).
    assert _f(est.total) == 5000.0
    assert _f(compute_estimate_totals(est, db)["total"]) == 4500.0

    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id, customer_id=job.customer_id, estimate_id=est.id,
            invoice_date=date.today(),
        ),
        _=_user(), db=db,
    )
    assert _f(inv["total"]) == pytest.approx(4500.0, abs=0.005), (
        f"invoice born at {_f(inv['total'])} against an accepted estimate of 4500.00 — "
        "the discount was never carried across"
    )

    # And it must still hold after any recalc (e.g. recording a payment).
    row = db.get(Invoice, _U(inv["id"]))
    _recalculate_invoice(row, db)
    db.commit()
    db.expire_all()
    row = db.get(Invoice, _U(inv["id"]))
    assert _f(row.total) == pytest.approx(4500.0, abs=0.005), (
        f"discount evaporated on recalc: total sprang to {_f(row.total)}"
    )


def test_m24_labor_line_taxability_carries_to_the_invoice(db):
    """Labor excluded from tax on the estimate must stay excluded on the invoice."""
    from uuid import UUID as _U

    job, est = _seed_estimate(
        db, tax_rate=0.0738,
        lines=[("Door", "materials", 2000.0), ("Install labor", "labor", 1000.0)],
    )
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id, customer_id=job.customer_id, estimate_id=est.id,
            tax_rate=0.0738, invoice_date=date.today(),
        ),
        _=_user(), db=db,
    )
    rows = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == _U(inv["id"]))
    ).scalars().all()
    labor = [r for r in rows if (r.category or "").lower() == "labor"]
    assert labor, "labor line did not survive the copy"  # setup sanity
    assert all(not bool(r.taxable) for r in labor), (
        "labor line copied onto the invoice as taxable — estimate quoted tax on "
        "$2,000 ($147.60), invoice will tax $3,000 ($221.40), a $73.80 overbill"
    )


# --------------------------------------------------------------------------
# Baseline — the canonical path should be clean (proves the harness works)
# --------------------------------------------------------------------------

def test_baseline_canonical_invoice_holds_the_totals_invariant(db):
    """Rate-mode invoice through the canonical path: the control case."""
    from uuid import UUID as _U

    inv = _create_invoice(
        db,
        tax_rate=0.0738,
        lines=[
            {"description": "Door", "quantity": 1, "unit_price": 1000.0, "taxable": True},
            {"description": "Labor", "quantity": 4, "unit_price": 100.0, "taxable": False},
        ],
    )
    row = db.get(Invoice, _U(inv["id"]))
    stored, derived = _totals_invariant(row, db)
    assert stored == pytest.approx(derived, abs=0.005)
    assert _f(row.tax_amount) == pytest.approx(73.80, abs=0.005)
    assert _f(row.total) == pytest.approx(1473.80, abs=0.005)
