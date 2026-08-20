"""Invoice line labor provenance — migration 071 contract guards.

Doug 2026-08-19, asked whether Add Labor should bill the matrix flat price or
the tech's attested hours: "it could be either." Both lanes ship, so the line
has to record WHICH one priced it — otherwise "is this labor quoted or
attested?" is unanswerable after the fact, which is invariant #1 on a money row.

`labor_source` is deliberately constrained rather than free text: it is the
field that separates a QUOTED contract price from ATTESTED hours, and billed
labor comes from attested hours only.
"""
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from gdx_dispatch.models.tenant_models import Invoice
from gdx_dispatch.routers.invoices import InvoiceLineCreateIn

# The in-memory tenant session + job seeder live in test_invoices.py rather than
# a conftest. Import them rather than standing up a second, slightly-different
# schema fixture that could drift from the one the rest of the invoice tests use.
from gdx_dispatch.tests.test_invoices import (  # noqa: E402
    _current_user,
    _seed_job,
    tenant_db_session,  # noqa: F401  — pytest fixture, used by name
)

# pytest resolves fixtures by PARAMETER NAME, so the parameter below must be
# called `tenant_db_session` — which ruff reads as redefining the import above.
# The shadowing is inherent to importing a fixture rather than putting it in a
# conftest, and moving it there would touch every invoice test. Silenced per
# use rather than raising the ruff baseline.


def _line(**kw):
    base = {"description": "16x7 Sectional Install", "quantity": 1, "unit_price": 650}
    base.update(kw)
    return InvoiceLineCreateIn(**base)


class TestLaborSourceContract:
    def test_ordinary_line_carries_no_provenance(self):
        """Every existing caller keeps working — all three fields optional."""
        line = _line()
        assert line.labor_source is None
        assert line.labor_price_item_id is None
        assert line.estimated_man_hours is None

    def test_matrix_line_records_the_row_that_quoted_it(self):
        line = _line(
            labor_source="matrix",
            labor_price_item_id="11111111-1111-1111-1111-111111111111",
            estimated_man_hours=6.5,
        )
        assert line.labor_source == "matrix"
        assert str(line.labor_price_item_id) == "11111111-1111-1111-1111-111111111111"
        assert line.estimated_man_hours == 6.5

    def test_matrix_without_an_id_is_rejected(self):
        """A 'matrix' claim with no row id cannot be checked by anyone later.

        That unverifiable claim is exactly the provenance gap the column was
        added to close, so the contract refuses it rather than storing it.
        """
        with pytest.raises(ValidationError) as exc:
            _line(labor_source="matrix")
        assert "requires labor_price_item_id" in str(exc.value)

    def test_an_id_without_a_source_is_rejected(self):
        with pytest.raises(ValidationError) as exc:
            _line(labor_price_item_id="11111111-1111-1111-1111-111111111111")
        assert "requires labor_source" in str(exc.value)

    def test_attested_may_not_name_a_matrix_row(self):
        """Attested hours come from a closeout, not the matrix. Carrying a row
        id claims a quoted price priced them — the lane confusion the field
        exists to prevent, and the bug the picker shipped with 2026-08-20."""
        with pytest.raises(ValidationError) as exc:
            _line(
                labor_source="attested",
                labor_price_item_id="11111111-1111-1111-1111-111111111111",
            )
        assert "not valid with labor_source='attested'" in str(exc.value)

    def test_manual_may_keep_the_row_it_started_from(self):
        """"Started from matrix row X, then a human repriced it" is a true and
        useful statement. Forbidding it forced the estimate copy to destroy the
        linkage migration 071 exists to preserve."""
        line = _line(
            labor_source="manual",
            labor_price_item_id="11111111-1111-1111-1111-111111111111",
        )
        assert line.labor_source == "manual"
        assert str(line.labor_price_item_id) == "11111111-1111-1111-1111-111111111111"

    def test_hours_without_a_lane_are_rejected(self):
        """An hours figure with no lane is an unattributable claim about how
        long work took."""
        with pytest.raises(ValidationError) as exc:
            _line(estimated_man_hours=6.5)
        assert "requires labor_source" in str(exc.value)

    def test_attested_line_needs_no_matrix_row(self):
        """Attested hours come from the tech's closeout, not the matrix."""
        line = _line(description="Labor", labor_source="attested", estimated_man_hours=9)
        assert line.labor_source == "attested"
        assert line.labor_price_item_id is None

    def test_manual_is_a_first_class_answer(self):
        assert _line(labor_source="manual").labor_source == "manual"

    @pytest.mark.parametrize("bad", ["Matrix", "guessed", "tech", "", "quoted"])
    def test_free_text_source_is_rejected(self, bad):
        """Free text would make quoted-vs-attested unreadable within a release."""
        with pytest.raises(ValidationError):
            _line(labor_source=bad)

    def test_negative_hours_are_rejected(self):
        with pytest.raises(ValidationError):
            _line(labor_source="attested", estimated_man_hours=-1)

    def test_absurd_hours_are_rejected(self):
        with pytest.raises(ValidationError):
            _line(labor_source="attested", estimated_man_hours=100000)

    def test_extra_fields_still_forbidden(self):
        """The contract stayed strict — a typo'd field must fail loudly."""
        with pytest.raises(ValidationError):
            _line(labor_sources="matrix")


class TestLaborProvenanceActuallyPersists:
    """The schema tests above pass against code that ACCEPTS these fields and
    then drops them on the way to the database — which is exactly the bug the
    2026-08-20 review found in `add_invoice_line`, and the same class of bug
    that handler already had once for `part_id` and `includes_labor`.

    A contract test that never touches a row cannot see it. These do.
    """

    def test_create_persists_all_three_fields(self, tenant_db_session):  # noqa: F811
        from gdx_dispatch.routers.invoices import InvoiceCreateIn, create_invoice

        job = _seed_job(tenant_db_session)
        item_id = "11111111-1111-1111-1111-111111111111"
        created = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                line_items=[{
                    "description": "16x7 Sectional Install",
                    "quantity": 1,
                    "unit_price": 650,
                    "labor_source": "matrix",
                    "labor_price_item_id": item_id,
                    "estimated_man_hours": 6.5,
                }],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )

        row = tenant_db_session.get(Invoice, UUID(created["id"]))
        line = row.lines[0]
        assert line.labor_source == "matrix"
        assert str(line.labor_price_item_id) == item_id
        assert float(line.estimated_man_hours) == 6.5

    def test_add_line_persists_all_three_fields(self, tenant_db_session):  # noqa: F811
        """POST /lines — the handler the Add Labor button on the invoice DETAIL
        screen uses. It accepted these and stored none of them."""
        from gdx_dispatch.routers.invoices import (
            InvoiceCreateIn,
            add_invoice_line,
            create_invoice,
        )

        job = _seed_job(tenant_db_session)
        created = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                # A starter line so the invoice has content; what is under
                # test is the line ADDED below. (The fixture has no
                # tenant_settings table, so billing-terms resolution logs a
                # handled read failure — noise, not a failure.)
                line_items=[{"description": "Part", "quantity": 1, "unit_price": 10}],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
        item_id = "22222222-2222-2222-2222-222222222222"
        add_invoice_line(
            invoice_id=UUID(created["id"]),
            payload=InvoiceLineCreateIn(
                description="9x7 Sectional Install",
                quantity=1,
                unit_price=475,
                labor_source="matrix",
                labor_price_item_id=item_id,
                estimated_man_hours=4.75,
            ),
            current_user=_current_user(),
            db=tenant_db_session,
        )

        row = tenant_db_session.get(Invoice, UUID(created["id"]))
        line = next(x for x in row.lines if x.description == "9x7 Sectional Install")
        assert line.labor_source == "matrix", "POST /lines dropped labor_source"
        assert str(line.labor_price_item_id) == item_id
        assert float(line.estimated_man_hours) == 4.75

    def test_serializer_round_trips_provenance(self, tenant_db_session):  # noqa: F811
        from gdx_dispatch.routers.invoices import InvoiceCreateIn, create_invoice, get_invoice

        job = _seed_job(tenant_db_session)
        created = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                line_items=[{
                    "description": "Labor",
                    "quantity": 1,
                    "unit_price": 900,
                    "labor_source": "attested",
                    "estimated_man_hours": 9,
                }],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
        full = get_invoice(UUID(created["id"]), _=_current_user(), db=tenant_db_session)
        line = full["lines"][0]
        assert line["labor_source"] == "attested"
        assert line["estimated_man_hours"] == 9.0
        assert line["labor_price_item_id"] is None


class TestRepriceDowngradePersists:
    """A repriced matrix line must stop claiming the matrix quoted it — on the
    DETAIL screen too, not just at create.

    Found on audit pass 7: the detail view's normalizer dropped these columns,
    so edit mode had no provenance and the PATCH contract had nowhere to put a
    downgrade. Repricing a $650 matrix-quoted line to $900 left the row saying
    "matrix quoted row X" at $900.
    """

    def test_patch_records_the_downgrade_and_keeps_the_origin_row(self, tenant_db_session):  # noqa: F811
        from gdx_dispatch.routers.invoices import (
            InvoiceCreateIn,
            InvoiceLinePatchIn,
            create_invoice,
            patch_invoice_line,
        )

        job = _seed_job(tenant_db_session)
        item_id = "33333333-3333-3333-3333-333333333333"
        created = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                line_items=[{
                    "description": "16x7 Sectional Install",
                    "quantity": 1,
                    "unit_price": 650,
                    "labor_source": "matrix",
                    "labor_price_item_id": item_id,
                }],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
        inv = tenant_db_session.get(Invoice, UUID(created["id"]))
        line_id = inv.lines[0].id

        patch_invoice_line(
            invoice_id=inv.id,
            line_id=line_id,
            payload=InvoiceLinePatchIn(unit_price=900, labor_source="manual"),
            user=_current_user(),
            db=tenant_db_session,
        )

        tenant_db_session.refresh(inv.lines[0])
        line = inv.lines[0]
        assert float(line.unit_price) == 900
        assert line.labor_source == "manual", "the downgrade never reached the DB"
        # "Started from row X, then repriced" — the linkage survives.
        assert str(line.labor_price_item_id) == item_id

    def test_patch_cannot_claim_a_line_became_matrix_quoted(self):
        """Editing a line into a matrix quote is the same falsehood reversed."""
        from gdx_dispatch.routers.invoices import InvoiceLinePatchIn

        with pytest.raises(ValidationError):
            InvoiceLinePatchIn(labor_source="matrix")


class TestEstimateConversionProvenance:
    """Converting an accepted estimate must not assert the matrix quoted a
    price a human typed.

    Audit pass 8: `_labor_provenance_for` inferred "was it repriced" from
    `margin_pct_override`, which is never populated for labor lines — the
    estimate UI sends `cost: null` for anything carrying a matrix id, so the
    branch that would set it cannot fire. Pick $650 from the matrix, type $900,
    accept, convert, and the invoice said the matrix quoted $900.

    The signal is the MATRIX ROW'S OWN PRICE.
    """

    def _matrix_row(self, db, price):
        from gdx_dispatch.models.labor_pricing import LaborPriceItem
        row = LaborPriceItem(
            id=uuid4(), description="16x7 Sectional Install", service_type="install",
            flat_price=price, assumed_man_hours=6.5, active=True,
        )
        db.add(row)
        db.flush()
        return row

    def test_untouched_matrix_price_stays_matrix(self, tenant_db_session):  # noqa: F811
        from gdx_dispatch.routers.invoices import _labor_provenance_for
        row = self._matrix_row(tenant_db_session, 650)
        line = SimpleNamespace(
            labor_price_item_id=row.id, estimated_man_hours=6.5,
            margin_pct_override=None, unit_price=650,
        )
        out = _labor_provenance_for(line, tenant_db_session)
        assert out["labor_source"] == "matrix"
        assert out["labor_price_item_id"] == row.id

    def test_a_repriced_matrix_line_becomes_manual(self, tenant_db_session):  # noqa: F811
        from gdx_dispatch.routers.invoices import _labor_provenance_for
        row = self._matrix_row(tenant_db_session, 650)
        line = SimpleNamespace(
            labor_price_item_id=row.id, estimated_man_hours=6.5,
            # No margin override — that is the whole point: for labor lines it
            # is never set, so the old signal could not see this reprice.
            margin_pct_override=None, unit_price=900,
        )
        out = _labor_provenance_for(line, tenant_db_session)
        assert out["labor_source"] == "manual", "a typed price still claimed the matrix quoted it"
        # The row it started from survives — "started from X, then repriced".
        assert out["labor_price_item_id"] == row.id

    def test_an_archived_matrix_row_does_not_invent_an_override(self, tenant_db_session):  # noqa: F811
        """Unresolvable row => keep matrix. Guessing "a human repriced this"
        from missing data would invent the provenance this column exists to
        prevent."""
        from gdx_dispatch.routers.invoices import _labor_provenance_for
        line = SimpleNamespace(
            labor_price_item_id=uuid4(), estimated_man_hours=6.5,
            margin_pct_override=None, unit_price=900,
        )
        out = _labor_provenance_for(line, tenant_db_session)
        assert out["labor_source"] == "matrix"
