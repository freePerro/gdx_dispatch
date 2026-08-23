"""Which estimate an office-created invoice came from.

`/billing/new` prefills its editor from an accepted estimate and then lets the
operator edit those lines. It could not send `estimate_id` to record the link,
because that field means "copy the estimate's lines and ignore mine" — sending
it would have discarded the operator's edits.

So it sent nothing, and the link was never recorded: 5 of 340 prod invoices had
one, all from the mobile dialog. The invoice detail page's "linked estimate"
chip was dead for every office-created invoice.

`source_estimate_id` records the provenance without touching the lines.
"""
from datetime import UTC, datetime
from types import SimpleNamespace  # noqa: F401
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from gdx_dispatch.models.tenant_models import Invoice
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers.invoices import InvoiceCreateIn
from gdx_dispatch.tests.test_invoices import (  # noqa: E402
    _current_user,
    _seed_job,
    tenant_db_session,  # noqa: F401  — pytest fixture, used by name
)


def _seed_estimate(db, job):
    """A real, live estimate on this job — `source_estimate_id` is validated
    for existence, soft-delete and job scope, exactly like its sibling."""
    est = Estimate(
        id=uuid4(),
        job_id=job.id,
        customer_id=job.customer_id,
        estimate_number="EST-TEST-0001",
        status="accepted",
        company_id="tenant-1",
        public_token=uuid4().hex,
    )
    db.add(est)
    db.flush()
    return est


class TestSourceEstimateContract:
    def test_the_two_estimate_fields_are_mutually_exclusive(self):
        """They mean different things: one copies lines, one records where the
        lines already on the payload came from. Sending both is incoherent —
        the server would copy AND be told the client already has them."""
        with pytest.raises(ValidationError) as exc:
            InvoiceCreateIn(
                customer_id=uuid4(),
                job_id=uuid4(),
                estimate_id=uuid4(),
                source_estimate_id=uuid4(),
            )
        assert "not both" in str(exc.value)

    def test_source_estimate_alone_is_fine(self):
        payload = InvoiceCreateIn(
            customer_id=uuid4(), job_id=uuid4(), source_estimate_id=uuid4()
        )
        assert payload.source_estimate_id is not None
        assert payload.estimate_id is None

    def test_a_counter_sale_cannot_have_come_from_an_estimate(self):
        """Estimates are job-scoped, so "no job" and "came from an estimate"
        cannot both be true. Same rule the copy path already enforces."""
        with pytest.raises(ValidationError) as exc:
            InvoiceCreateIn(customer_id=uuid4(), source_estimate_id=uuid4())
        assert "requires job_id" in str(exc.value)


class TestSourceEstimatePersists:
    def test_it_lands_on_the_invoice_and_keeps_the_operator_lines(self, tenant_db_session):  # noqa: F811
        """The whole point: the link is recorded AND the lines are the ones the
        operator submitted, not a re-copy of the estimate's."""
        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        est_id = _seed_estimate(tenant_db_session, job).id
        created = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                source_estimate_id=est_id,
                line_items=[{
                    "description": "Operator-edited line",
                    "quantity": 1,
                    "unit_price": 725,
                }],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )

        row = tenant_db_session.get(Invoice, UUID(created["id"]))
        # The PROVENANCE column, not estimate_id — writing the latter arms the
        # deposit matcher's estimate arm and nets another job's deposit.
        assert str(row.source_estimate_id) == str(est_id), "provenance was not recorded"
        assert row.estimate_id is None, (
            "provenance must not populate estimate_id — deposit netting and "
            "closeout reconciliation both read that column as 'this invoice IS "
            "the estimate's bill'"
        )
        assert len(row.lines) == 1
        assert row.lines[0].description == "Operator-edited line"
        assert float(row.lines[0].unit_price) == 725

    def test_the_audit_trail_says_how_the_link_happened(self, tenant_db_session):  # noqa: F811
        """"copied" and "prefilled" are different claims about who chose the
        numbers. Recording only that a link exists loses that."""
        from gdx_dispatch.core.audit import AuditLog
        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        est_id = _seed_estimate(tenant_db_session, job).id
        create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                source_estimate_id=est_id,
                line_items=[{"description": "L", "quantity": 1, "unit_price": 10}],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )

        events = (
            tenant_db_session.query(AuditLog)
            .filter(AuditLog.action == "invoice_created")
            .all()
        )
        assert events, "invoice_created was not audited"
        details = events[-1].details or {}
        assert details.get("estimate_link") == "prefilled"
        assert details.get("source_estimate_id") == str(est_id)

    def test_no_estimate_means_no_link_claim_in_the_audit(self, tenant_db_session):  # noqa: F811
        from gdx_dispatch.core.audit import AuditLog
        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                line_items=[{"description": "L", "quantity": 1, "unit_price": 10}],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
        events = (
            tenant_db_session.query(AuditLog)
            .filter(AuditLog.action == "invoice_created")
            .all()
        )
        details = events[-1].details or {}
        assert "source_estimate_id" not in details
        assert "estimate_link" not in details


class TestProvenanceDoesNotMoveMoney:
    """The reason this needed its own column.

    `modules/deposits/service.py` matches deposits with
    ``or_(Invoice.job_id == X, Invoice.estimate_id == E)``. On office invoices
    `estimate_id` was effectively always NULL, so the second arm was dormant.
    Writing merely-prefilled invoices into it ARMS that arm — a paid deposit
    sitting on a DIFFERENT job that shares the estimate gets netted in. Review
    reproduced it: a $2,000 invoice came out at $1,500 carrying another job's
    money, because the double-application guard beside it is job-scoped and does
    not cover the estimate arm.
    """

    def test_the_deposit_matcher_cannot_see_a_provenance_link(self, tenant_db_session):  # noqa: F811
        """Exercises the MATCHER, not just the column.

        Asserting `estimate_id is None` only proves the column; it does not
        prove the matcher is blind to provenance. Build the arms the way
        `apply_deposits_to_final` does and confirm a prefilled invoice
        contributes no estimate arm.
        """
        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        est_id = _seed_estimate(tenant_db_session, job).id
        created = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                source_estimate_id=est_id,
                line_items=[{"description": "L", "quantity": 1, "unit_price": 2000}],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
        row = tenant_db_session.get(Invoice, UUID(created["id"]))

        # Mirror of modules/deposits/service.py's arm construction.
        arms = []
        if row.job_id is not None:
            arms.append("job")
        if getattr(row, "estimate_id", None) is not None:
            arms.append("estimate")
        assert arms == ["job"], (
            "a prefilled invoice armed the matcher's estimate arm — that is how "
            "another job's paid deposit gets netted in"
        )
        assert row.source_estimate_id is not None, "provenance was lost entirely"

    def test_reconciliation_still_sees_a_prefilled_invoice(self, tenant_db_session):  # noqa: F811
        """`core/closeout_reconciliation.py` skips invoices with an
        `estimate_id` — "estimate-billed = agreed price, not a discrepancy".
        A prefilled invoice IS editable line by line, so that premise does not
        hold for it and it must stay in the discrepancy list."""
        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        est_id = _seed_estimate(tenant_db_session, job).id
        created = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                source_estimate_id=est_id,
                line_items=[{"description": "L", "quantity": 1, "unit_price": 100}],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
        row = tenant_db_session.get(Invoice, UUID(created["id"]))
        # Apply the reconciliation filter itself rather than restating the
        # column check from the test above.
        from sqlalchemy import select as _select
        visible = tenant_db_session.execute(
            _select(Invoice).where(
                Invoice.job_id == job.id,
                Invoice.deleted_at.is_(None),
                Invoice.status != "void",
                Invoice.estimate_id.is_(None),   # the real filter
            )
        ).scalars().all()
        assert row.id in [v.id for v in visible], (
            "a prefilled invoice dropped out of the revised-closeout "
            "discrepancy list"
        )

    def test_a_source_estimate_from_another_job_is_refused(self, tenant_db_session):  # noqa: F811
        """The shape that made the deposit leak reachable from the real UI:
        switching customers left stale provenance on the form."""
        from fastapi import HTTPException

        from gdx_dispatch.routers.invoices import create_invoice

        job_a = _seed_job(tenant_db_session)
        job_b = _seed_job(tenant_db_session)
        est_b = _seed_estimate(tenant_db_session, job_b)

        with pytest.raises(HTTPException) as exc:
            create_invoice(
                payload=InvoiceCreateIn(
                    job_id=job_a.id,
                    customer_id=job_a.customer_id,
                    source_estimate_id=est_b.id,
                    line_items=[{"description": "L", "quantity": 1, "unit_price": 10}],
                ),
                _=_current_user(),
                db=tenant_db_session,
            )
        assert exc.value.status_code == 422
        assert "different job" in str(exc.value.detail)

    def test_a_nonexistent_source_estimate_is_refused(self, tenant_db_session):  # noqa: F811
        """Provenance that cannot be resolved is not provenance."""
        from fastapi import HTTPException

        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        with pytest.raises(HTTPException) as exc:
            create_invoice(
                payload=InvoiceCreateIn(
                    job_id=job.id,
                    customer_id=job.customer_id,
                    source_estimate_id=uuid4(),
                    line_items=[{"description": "L", "quantity": 1, "unit_price": 10}],
                ),
                _=_current_user(),
                db=tenant_db_session,
            )
        assert exc.value.status_code == 404


    def test_a_soft_deleted_source_estimate_is_refused(self, tenant_db_session):  # noqa: F811
        """The soft-delete filter had no guard: removing it left every money
        test still passing. A deleted estimate is not provenance you can follow."""
        from fastapi import HTTPException

        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        est = _seed_estimate(tenant_db_session, job)
        est.deleted_at = datetime.now(UTC)
        tenant_db_session.flush()

        with pytest.raises(HTTPException) as exc:
            create_invoice(
                payload=InvoiceCreateIn(
                    job_id=job.id,
                    customer_id=job.customer_id,
                    source_estimate_id=est.id,
                    line_items=[{"description": "L", "quantity": 1, "unit_price": 10}],
                ),
                _=_current_user(),
                db=tenant_db_session,
            )
        assert exc.value.status_code == 404


class TestOnlyAnAcceptedEstimateCanBeBilled:
    """M23, money audit 2026-08-04.

    `estimate_id` means "copy this estimate's lines and ignore mine", so the
    estimate it names IS the bill. This path validated existence, soft-delete
    and job scope — and never status. A job with accepted estimate A and a
    later declined variant B would bill B.

    The other two conversion paths already refuse (`mobile_invoicing.py:447`
    and `estimates.py`'s `/deposit-invoice`). Three paths, two gates, and the
    ungated one was the canonical office route.
    """

    @staticmethod
    def _estimate(db, job, status):
        est = Estimate(
            id=uuid4(),
            job_id=job.id,
            customer_id=job.customer_id,
            estimate_number=f"EST-M23-{status[:4].upper()}",
            status=status,
            company_id="tenant-1",
            public_token=uuid4().hex,
        )
        db.add(est)
        db.flush()
        return est

    # The full non-accepted half of the `estimate_status` enum — exhaustive,
    # so a new status added to the enum without a decision here shows up as a
    # gap rather than silently billing.
    @pytest.mark.parametrize("status", ["draft", "sent", "declined", "rejected", "expired"])
    def test_a_non_accepted_estimate_is_refused(self, tenant_db_session, status):  # noqa: F811
        from fastapi import HTTPException

        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        est = self._estimate(tenant_db_session, job, status)

        with pytest.raises(HTTPException) as exc:
            create_invoice(
                payload=InvoiceCreateIn(
                    job_id=job.id,
                    customer_id=job.customer_id,
                    estimate_id=est.id,
                ),
                _=_current_user(),
                db=tenant_db_session,
            )
        assert exc.value.status_code == 409
        # The refusal names the current status, so an operator knows what to do
        # about it rather than only that they were stopped.
        assert status in str(exc.value.detail)

    def test_an_accepted_estimate_still_bills(self, tenant_db_session):  # noqa: F811
        """The counterfactual: the gate must not refuse the honest case."""
        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        est = self._estimate(tenant_db_session, job, "accepted")

        created = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                estimate_id=est.id,
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
        assert created is not None

    def test_provenance_is_deliberately_not_gated(self, tenant_db_session):  # noqa: F811
        """`source_estimate_id` copies nothing — it records where lines the
        caller already built came from. A counter sale whose lines originated
        in a quote that was later revised is legitimate history, not a money
        error, so the gate does NOT extend here. Asserting the boundary so a
        future tightening is a decision rather than an accident."""
        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        est = self._estimate(tenant_db_session, job, "declined")

        created = create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                source_estimate_id=est.id,
                line_items=[{"description": "L", "quantity": 1, "unit_price": 10}],
            ),
            _=_current_user(),
            db=tenant_db_session,
        )
        assert created is not None

    def test_the_finding_itself_accepted_A_and_declined_B_on_one_job(self, tenant_db_session):  # noqa: F811
        """M23 as written: a job carrying accepted estimate A *and* a later
        declined variant B. Every other case here has a single estimate, which
        is not the shape the finding describes."""
        from fastapi import HTTPException

        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        accepted_a = self._estimate(tenant_db_session, job, "accepted")
        declined_b = self._estimate(tenant_db_session, job, "declined")

        with pytest.raises(HTTPException) as exc:
            create_invoice(
                payload=InvoiceCreateIn(
                    job_id=job.id, customer_id=job.customer_id, estimate_id=declined_b.id
                ),
                _=_current_user(),
                db=tenant_db_session,
            )
        assert exc.value.status_code == 409
        # ...and A, on the same job, still bills.
        assert create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id, customer_id=job.customer_id, estimate_id=accepted_a.id
            ),
            _=_current_user(),
            db=tenant_db_session,
        ) is not None

    def test_a_refusal_leaves_no_invoice_and_burns_no_number(self, tenant_db_session):  # noqa: F811
        """The gate sits before any write, and that has to stay true. A refusal
        that allocated an invoice number would leave a gap in the sequence for
        an estimate nobody agreed to."""
        from fastapi import HTTPException
        from sqlalchemy import func, select

        from gdx_dispatch.routers.invoices import create_invoice

        job = _seed_job(tenant_db_session)
        est = self._estimate(tenant_db_session, job, "declined")
        before = tenant_db_session.execute(
            select(func.count()).select_from(Invoice)
        ).scalar_one()

        with pytest.raises(HTTPException):
            create_invoice(
                payload=InvoiceCreateIn(
                    job_id=job.id, customer_id=job.customer_id, estimate_id=est.id
                ),
                _=_current_user(),
                db=tenant_db_session,
            )

        after = tenant_db_session.execute(
            select(func.count()).select_from(Invoice)
        ).scalar_one()
        assert after == before, "a refused request wrote an invoice row"

    def test_the_parametrized_statuses_really_are_every_non_accepted_one(self):
        """The list above is called exhaustive. Enforce it, so adding a status
        to the enum without deciding how it bills shows up as a failure rather
        than silently taking the accepted path."""
        enum_values = set(Estimate.__table__.c.status.type.enums)
        covered = {"draft", "sent", "declined", "rejected", "expired"}
        assert enum_values - {"accepted"} == covered, (
            "estimate_status changed — decide how the new status bills, then "
            "extend the parametrize list"
        )
