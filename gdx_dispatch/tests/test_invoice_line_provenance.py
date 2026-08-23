"""Provenance: who priced this part, and who authored this invoice line.

Follow-ups 1 and 3 of `closeout-parts-autopricing-plan.md`, closed together by
migration 075 because they are the same missing thing — a record of where a
number came from, kept beside the number.

**Follow-up 1.** Four lanes wrote `job_parts_needed.unit_price` — the office's
own figure, bench inventory, a catalog sell price, and the margin engine
marking a cost up — and all four landed in the same `Numeric(10,2)`. "Who
priced this part and why" was unanswerable from the records, which is
invariant #1 on money code.

**Follow-up 3.** `release_untouched_autodraft` empties an untouched autodraft
so the closeout can rebuild it. Its guard asked six questions about the
INVOICE and none about its LINES, so a part the office added by hand — which
is exactly what the unbilled-parts banner tells them to do — was deleted on
the next re-closeout.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.closeout_billing import (
    AUTODRAFT_LINE_SOURCE,
    AUTODRAFT_ORIGIN,
    is_untouched_autodraft,
    release_untouched_autodraft,
)
from gdx_dispatch.core.part_pricing import PriceSource
from gdx_dispatch.models.tenant_models import (
    Invoice,
    InvoiceLine,
    Job,
    JobPartNeeded,
    Payment,
)

TENANT = "tenant-provenance"


@pytest.fixture
def db():
    """A minimal in-memory schema. Deliberately NOT the whole metadata: this
    file only exercises the autodraft guard and its lines."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for tbl in (
        Job.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        JobPartNeeded.__table__,
    ):
        tbl.create(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
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


def _autodraft(db, job: Job) -> Invoice:
    inv = Invoice(
        id=uuid.uuid4(),
        job_id=job.id,
        customer_id=job.customer_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        status="draft",
        origin=AUTODRAFT_ORIGIN,
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


def _line(db, inv: Invoice, description: str, source: str | None) -> InvoiceLine:
    line = InvoiceLine(
        id=uuid.uuid4(),
        invoice_id=inv.id,
        description=description,
        quantity=1,
        unit_price=Decimal("100.00"),
        line_total=Decimal("100.00"),
        source=source,
        company_id=TENANT,
    )
    db.add(line)
    db.commit()
    return line


def _live_lines(db, inv: Invoice) -> list[InvoiceLine]:
    return list(
        db.execute(
            select(InvoiceLine).where(
                InvoiceLine.invoice_id == inv.id, InvoiceLine.deleted_at.is_(None)
            )
        ).scalars()
    )


# ── follow-up 3: the machine stops deleting the office's work ───────────────


def test_a_draft_of_only_machine_lines_is_still_the_machines_to_rebuild(db):
    """The counterfactual for everything below: without it, tightening the
    guard would simply have broken the autodraft rebuild."""
    job = _job(db)
    inv = _autodraft(db, job)
    _line(db, inv, "Labor — 2.0 hrs attested", AUTODRAFT_LINE_SOURCE)
    _line(db, inv, "Torsion spring", AUTODRAFT_LINE_SOURCE)

    assert is_untouched_autodraft(inv, db) is True
    assert release_untouched_autodraft(db, job=job) is not None
    assert _live_lines(db, inv) == [], "the machine must still empty its own draft"


def test_one_office_added_line_ends_machine_ownership(db):
    """The bug. The office follows the unbilled-parts banner, adds the part it
    names, and the next re-closeout deleted that line along with the rest."""
    job = _job(db)
    inv = _autodraft(db, job)
    _line(db, inv, "Labor — 2.0 hrs attested", AUTODRAFT_LINE_SOURCE)
    office_line = _line(db, inv, "Cable drum the office added", "office")

    assert is_untouched_autodraft(inv, db) is False, (
        "a human line must end the machine's claim on this invoice"
    )
    assert release_untouched_autodraft(db, job=job) is None
    kept = {line.id for line in _live_lines(db, inv)}
    assert office_line.id in kept, "the office's line was deleted"


def test_a_line_with_no_recorded_author_is_treated_as_possibly_human(db):
    """NULL means unknown, not machine.

    Every line written before migration 075 has `source IS NULL`. What nobody
    recorded cannot now be inferred, and deleting an operator's work on a
    guess is the worse of the two errors — so an unknown line is protected.

    This is also the SQL trap the guard is written around: `NULL != 'autodraft'`
    evaluates to NULL, not true, so a plain inequality would match nothing and
    every pre-075 line would read as machine-authored. `IS DISTINCT FROM` is
    what makes this pass.
    """
    job = _job(db)
    inv = _autodraft(db, job)
    _line(db, inv, "A line from before provenance existed", None)

    assert is_untouched_autodraft(inv, db) is False
    assert release_untouched_autodraft(db, job=job) is None
    assert len(_live_lines(db, inv)) == 1


def test_a_line_the_office_already_deleted_does_not_protect_the_draft(db):
    """Soft-deleted lines are not work to protect — otherwise one removed line
    would freeze the machine out of that invoice forever."""
    job = _job(db)
    inv = _autodraft(db, job)
    _line(db, inv, "Machine line", AUTODRAFT_LINE_SOURCE)
    gone = _line(db, inv, "Office line, since removed", "office")
    gone.deleted_at = datetime.now(UTC)
    db.commit()

    assert is_untouched_autodraft(inv, db) is True
    assert release_untouched_autodraft(db, job=job) is not None


def test_without_a_session_the_line_check_is_skipped_not_faked(db):
    """`db` is optional and the payment arm already documents why: an arm that
    cannot be answered is skipped rather than silently answered wrong. The
    line arm follows the same rule, so no caller gets a confident False from a
    check that never ran."""
    job = _job(db)
    inv = _autodraft(db, job)
    _line(db, inv, "Office line", "office")

    assert is_untouched_autodraft(inv, None) is True
    assert is_untouched_autodraft(inv, db) is False


# ── follow-up 1: a stored price says where it came from ────────────────────
#
# (That the autodraft actually stamps its own lines is asserted for real in
# test_closeout_autodraft.py, by running the builder — a test that greps this
# module's source would prove authorship, not behaviour.)


def test_price_source_vocabulary_is_what_the_column_stores():
    """The column is VARCHAR(24). A tag longer than that is silently truncated
    by SQLite and rejected by Postgres — a split-brain the tests must not
    discover in production."""
    tags = [
        v for k, v in vars(PriceSource).items()
        if not k.startswith("_") and isinstance(v, str)
    ]
    assert tags, "PriceSource exposes no tags"
    for tag in tags:
        assert len(tag) <= 24, f"{tag!r} does not fit price_source VARCHAR(24)"
        assert tag == tag.lower(), f"{tag!r} should be lowercase for readable rows"
