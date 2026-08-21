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
from decimal import Decimal
from uuid import uuid4

import pytest

from gdx_dispatch.core.billing_lanes import InstallLaborLine, ServiceLaborLine


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


class TestAutodraftSource:
    """Pins the SOURCE STRINGS the autodraft writes, at the call sites.

    Read from the module text rather than executing a full closeout: the point
    is that each lane writes the value that matches what it actually knows, and
    a future edit that swaps them (or drops one) should fail loudly.
    """

    def test_install_lane_claims_matrix_and_names_the_row(self, src):
        i = src.index('lane == "install"')
        span = src[i:i + 1800]
        assert 'labor_source="matrix"' in span
        assert "labor_price_item_id=_install.matrix_item_id" in span

    def test_install_lane_makes_no_hours_claim(self, src):
        """A quoted flat price is not a statement about duration."""
        i = src.index('lane == "install"')
        span = src[i:src.index('lane == "service"')]
        assert "estimated_man_hours" not in span, (
            "the install lane recorded an hours figure — the matrix's assumed "
            "hours are an assumption about a job of that shape, not evidence "
            "about this one"
        )

    def test_service_lane_claims_attested_and_records_the_hours(self, src):
        i = src.index('lane == "service"')
        span = src[i:i + 1800]
        assert 'labor_source="attested"' in span
        assert "estimated_man_hours=Decimal(str(labor.attested_hours))" in span

    def test_service_lane_names_no_matrix_row(self, src):
        """Nothing quoted attested hours; claiming a row would be the lane
        confusion the column exists to prevent."""
        i = src.index('lane == "service"')
        span = src[i:i + 1800]
        assert "labor_price_item_id" not in span


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
