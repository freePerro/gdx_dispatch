"""The closeout autodraft records HOW its labor line was priced.

Migration 071 added `labor_source` so an invoice line can answer "was this
labor quoted or attested?". Only the hand-add picker set it, so on prod the
column read NULL for 29 of 30 labor lines — the autodraft creates the
overwhelming majority and said nothing. A column that answers the question for
3% of rows answers nothing.

The two lanes mean different things and must not be conflated:

  install lane -> a QUOTED FLAT PRICE from a matrix row. Names the row. Carries
                  NO hours claim: `assumed_man_hours` is the matrix's
                  assumption about a job of that shape, not a record of this
                  one.
  service lane -> the tech's ATTESTED hours. Records the hours, names no row.
"""
import datetime as _dt
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.billing_lanes import InstallLaborLine, ServiceLaborLine
from gdx_dispatch.core.closeout_billing import build_closeout_lines
from gdx_dispatch.core.job_taxonomy import INSTALLATION, SERVICE_CALL
from gdx_dispatch.models.labor_pricing import LaborPriceItem
from gdx_dispatch.models.pricing_engine import PricingSettings
from gdx_dispatch.models.tenant_models import (
    Invoice,
    InvoiceLine,
    JobCloseout,
    JobPartNeeded,
)

TENANT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


@pytest.fixture
def db():
    """In-memory SQLite — deliberately NOT Postgres.

    The str/UUID crash this file now guards against is invisible on Postgres
    (psycopg casts the string) and fatal on SQLite. Prod is Postgres, but CI
    runs SQLite, and the repo rule is that every path must work on both.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in (
        Invoice.__table__,
        InvoiceLine.__table__,
        JobCloseout.__table__,
        JobPartNeeded.__table__,
        LaborPriceItem.__table__,
        PricingSettings.__table__,
    ):
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _invoice(db) -> Invoice:
    inv = Invoice(
        id=uuid4(),
        job_id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:6]}",
        # NOT NULL on the model — the pay-link token every invoice carries.
        public_token=uuid4().hex,
        status="draft",
        company_id=TENANT,
    )
    db.add(inv)
    db.flush()
    return inv


def _install_closeout(db, *, flat_price="650.00"):
    """A matrix row + an install-lane closeout that picked it.

    `labor_matrix_item_id` is stored as a STRING, because
    `job_closeouts.labor_matrix_item_id` is varchar(36) — that mismatch against
    the UUID invoice column is the whole point of this fixture.
    """
    item = LaborPriceItem(
        id=uuid4(),
        sku="INST-16x7",
        description="16x7 door install",
        service_type="install",
        width_ft=16,
        height_ft=7,
        flat_price=Decimal(flat_price),
        assumed_man_hours=Decimal("3.5"),
        active=True,
    )
    db.add(item)
    db.flush()
    inv = _invoice(db)
    closeout = JobCloseout(
        id=uuid4(),
        job_id=inv.job_id,
        hours_worked=0,
        labor_matrix_item_id=str(item.id),
        closed_by_user_id=str(uuid4()),
        closed_at=_dt.datetime.now(_dt.UTC),
    )
    db.add(closeout)
    db.flush()
    return item, closeout, inv


def _service_closeout(db, *, hours: float):
    inv = _invoice(db)
    closeout = JobCloseout(
        id=uuid4(),
        job_id=inv.job_id,
        hours_worked=hours,
        techs_on_site=1,
        closed_by_user_id=str(uuid4()),
        closed_at=_dt.datetime.now(_dt.UTC),
    )
    db.add(closeout)
    db.flush()
    return closeout, inv


def _labor_line(db, invoice) -> InvoiceLine:
    # The fixture session is autoflush=False (matching the app's), so the lines
    # build_closeout_lines just db.add()ed are still pending. Flushing is also
    # what makes this a real guard: the str/UUID crash happens AT the flush.
    db.flush()
    rows = list(
        db.execute(
            select(InvoiceLine).where(
                InvoiceLine.invoice_id == invoice.id,
                InvoiceLine.category == "Labor",
            )
        ).scalars()
    )
    assert len(rows) == 1, f"expected exactly one labor line, got {len(rows)}"
    return rows[0]


class TestLanesCarryWhatProvenanceNeeds:
    """The helpers already expose the provenance fields — pin that, because the
    autodraft reads them directly and a rename would silently reintroduce NULL."""

    def test_install_lane_names_its_matrix_row(self):
        assert "matrix_item_id" in InstallLaborLine.__dataclass_fields__

    def test_service_lane_carries_attested_hours(self):
        assert "attested_hours" in ServiceLaborLine.__dataclass_fields__


@pytest.fixture(scope="module")
def src():
    from pathlib import Path

    import gdx_dispatch.core.closeout_billing as m
    return Path(m.__file__).read_text()


@pytest.fixture(scope="module")
def mobile_src():
    from pathlib import Path

    import gdx_dispatch.routers.mobile_invoicing as m
    return Path(m.__file__).read_text()


class TestAutodraftWritesProvenance:
    """Runs the autodraft and reads the row it wrote.

    This class replaces four source-text assertions (`assert
    'labor_source="matrix"' in src[i:i+1800]`). They were worse than useless
    twice over:

      1. They **passed while the code was broken.** One of them asserted
         `labor_price_item_id=_install.matrix_item_id` was present — and it was,
         and it crashed on SQLite, because `matrix_item_id` is a `str` and the
         column is a UUID. CI shard 4 caught what this file was supposed to.
      2. They broke on a **comment**. Adding nine lines of explanation above the
         call pushed the string past the 1800-character window and the suite
         went red for a cosmetic edit.

    A test that reads source text asserts that someone typed something. These
    assert that the autodraft wrote it.
    """

    def test_install_lane_claims_matrix_and_names_the_row(self, db):
        item, closeout, invoice = _install_closeout(db)
        added, _total, _taxable = build_closeout_lines(
            db,
            tenant_id=TENANT,
            invoice=invoice,
            closeout=closeout,
            job_type=INSTALLATION,
            job_id=str(closeout.job_id),
        )
        assert added == 1
        line = _labor_line(db, invoice)
        assert line.labor_source == "matrix"
        # Names the ACTUAL row, as a UUID. Asserting the id round-trips is what
        # would have caught the str/UUID crash: on SQLite the flush raises,
        # on Postgres the string is cast silently.
        assert line.labor_price_item_id == item.id

    def test_every_lane_stamps_its_line_as_machine_authored(self, db):
        """Migration 075. `is_untouched_autodraft` refuses to let the machine
        rebuild a draft carrying any line that is not `source='autodraft'`, so
        an unstamped builder line makes the machine disown its own work the
        instant it writes it.

        This exists because the INSTALL lane was the one missed. The builder
        writes THREE kinds of line — install matrix labor, service attested
        labor, and parts — and the first pass stamped only the last two. The
        existing stamp assertion ran on a Service Call, so the whole install
        lane (the dominant path) was uncovered. Consequences on prod would
        have been a re-closeout that silently no-ops and a "not billable" that
        409s, neither with a trace.
        """
        from gdx_dispatch.core.closeout_billing import (
            AUTODRAFT_LINE_SOURCE,
            AUTODRAFT_ORIGIN,
            is_untouched_autodraft,
        )

        _item, closeout, invoice = _install_closeout(db)
        # The shared fixture leaves `origin` unset; a real autodraft carries
        # it, and the guard checks origin BEFORE it ever looks at lines. Set
        # it so the line arm is what this test actually exercises — otherwise
        # the assertion below would fail for the wrong reason and pass for the
        # wrong reason once someone "fixed" it.
        invoice.origin = AUTODRAFT_ORIGIN
        added, _total, _taxable = build_closeout_lines(
            db,
            tenant_id=TENANT,
            invoice=invoice,
            closeout=closeout,
            job_type=INSTALLATION,
            job_id=str(closeout.job_id),
        )
        assert added == 1
        assert _labor_line(db, invoice).source == AUTODRAFT_LINE_SOURCE

        # The consequence, not just the column: the machine still owns it.
        db.commit()
        assert is_untouched_autodraft(invoice, db) is True, (
            "the builder disowned its own install draft"
        )

    def test_install_lane_makes_no_hours_claim(self, db):
        """A quoted flat price is not a statement about duration.

        The matrix row carries `assumed_man_hours=3.5` — an assumption about a
        job of that shape, not evidence about this one. It must not be copied
        onto the customer's line.
        """
        _item, closeout, invoice = _install_closeout(db)
        build_closeout_lines(
            db,
            tenant_id=TENANT,
            invoice=invoice,
            closeout=closeout,
            job_type=INSTALLATION,
            job_id=str(closeout.job_id),
        )
        assert _labor_line(db, invoice).estimated_man_hours is None

    def test_service_lane_claims_attested_and_records_the_hours(self, db):
        closeout, invoice = _service_closeout(db, hours=2.0)
        build_closeout_lines(
            db,
            tenant_id=TENANT,
            invoice=invoice,
            closeout=closeout,
            job_type=SERVICE_CALL,
            job_id=str(closeout.job_id),
        )
        line = _labor_line(db, invoice)
        assert line.labor_source == "attested"
        assert line.estimated_man_hours is not None
        assert Decimal(str(line.estimated_man_hours)) > 0

    def test_service_lane_names_no_matrix_row(self, db):
        """Nothing quoted attested hours; claiming a row would be the lane
        confusion the column exists to prevent."""
        closeout, invoice = _service_closeout(db, hours=2.0)
        build_closeout_lines(
            db,
            tenant_id=TENANT,
            invoice=invoice,
            closeout=closeout,
            job_type=SERVICE_CALL,
            job_id=str(closeout.job_id),
        )
        assert _labor_line(db, invoice).labor_price_item_id is None

    def test_the_two_lanes_never_agree_on_source(self, db):
        """The whole point of the column: one invoice cannot claim both."""
        _item, i_closeout, i_invoice = _install_closeout(db)
        build_closeout_lines(
            db, tenant_id=TENANT, invoice=i_invoice, closeout=i_closeout,
            job_type=INSTALLATION, job_id=str(i_closeout.job_id),
        )
        s_closeout, s_invoice = _service_closeout(db, hours=2.0)
        build_closeout_lines(
            db, tenant_id=TENANT, invoice=s_invoice, closeout=s_closeout,
            job_type=SERVICE_CALL, job_id=str(s_closeout.job_id),
        )
        assert _labor_line(db, i_invoice).labor_source == "matrix"
        assert _labor_line(db, s_invoice).labor_source == "attested"


class TestMobileTierCopy:
    def test_it_records_manual_for_labor_and_nothing_for_goods(self, mobile_src):
        assert 'labor_source="manual" if _is_labor_line(tl) else None' in mobile_src

    def test_it_never_claims_matrix(self, mobile_src):
        """ProposalTierLine has no labor_price_item_id, and the contract
        rejects a matrix claim without one."""
        i = mobile_src.index('labor_source="manual"')
        span = mobile_src[max(0, i - 900):i + 200]
        assert 'labor_source="matrix"' not in span


def test_the_contract_still_forbids_an_unbacked_matrix_claim():
    """The guard the mobile decision leans on."""
    from pydantic import ValidationError

    from gdx_dispatch.routers.invoices import InvoiceLineCreateIn

    with pytest.raises(ValidationError):
        InvoiceLineCreateIn(
            description="Labor", quantity=1, unit_price=100, labor_source="matrix"
        )
    ok = InvoiceLineCreateIn(
        description="Labor", quantity=1, unit_price=100, labor_source="manual"
    )
    assert ok.labor_source == "manual"
    assert ok.labor_price_item_id is None
    assert Decimal(str(ok.unit_price)) == Decimal("100")
    assert uuid4()
