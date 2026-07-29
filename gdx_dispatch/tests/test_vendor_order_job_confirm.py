"""Order → job: suggest, then confirm, then the paperwork lands.

This is the leg nothing had ever connected. On production all ten captured
bills had their PDF and threaded to a statement line, and not one was matched
to a job — so nothing was filed anywhere. Confirming is what closes it, and one
confirmation must file EVERY document held for that order number, because the
order confirmation and the bill are two documents about one purchase.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from gdx_dispatch.models.tenant_models import Customer, Document, Job
from gdx_dispatch.modules.vendor_invoices.models import VendorInvoice
from gdx_dispatch.modules.vendor_orders.confirm import OrderConfirmError, confirm_order_job
from gdx_dispatch.modules.vendor_orders.matching import suggest_order_job_matches
from gdx_dispatch.modules.vendor_orders.models import VendorOrder

VENDOR = "Example Door Supply"
TID = "tenant-test"


def _customer(db, name):
    c = Customer(name=name, company_id=TID)
    db.add(c)
    db.flush()
    return c


def _job(db, customer, number="JOB-1", stage="scheduled"):
    j = Job(
        customer_id=customer.id, company_id=TID, job_number=number,
        title=f"{name_of(customer)} job", lifecycle_stage=stage,
    )
    db.add(j)
    db.flush()
    return j


def name_of(customer):
    return customer.name


def _document(db, name="doc.pdf"):
    d = Document(
        filename=f"{name}", original_name=name, file_size=10,
        content_type="application/pdf", uploaded_by="outlook", title=name,
    )
    db.add(d)
    db.flush()
    return d


def _order(db, number="20635854", *, ship_to=None, po=None, document=None):
    o = VendorOrder(
        vendor_name=VENDOR, vendor_code="ACME01", order_number=number,
        order_date=date(2026, 7, 23), ship_to=ship_to, customer_po=po,
        estimated_total=Decimal("3707.74"), parser_name="midwest_order_v1",
        parser_version=1, line_count=1, source="email",
        document_id=document.id if document else None,
    )
    db.add(o)
    db.flush()
    return o


def _bill(db, number, document=None, vendor=VENDOR):
    inv = VendorInvoice(
        vendor_name_raw=vendor, vendor_key=vendor.lower(), invoice_number=number,
        total=Decimal("3707.74"), source="email", extraction_method="parser",
        document_id=document.id if document else None,
    )
    db.add(inv)
    db.flush()
    return inv


# --------------------------------------------------------------------------- #
# suggesting
# --------------------------------------------------------------------------- #
def test_suggests_a_job_from_the_jobsite_it_ships_to(tenant_db):
    customer = _customer(tenant_db, "Trende")
    job = _job(tenant_db, customer)
    _order(tenant_db, ship_to="SFL Trende")
    tenant_db.commit()

    suggestions = suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one()).suggestions
    assert suggestions
    assert suggestions[0].job_id == str(job.id)
    assert "ship_to" in suggestions[0].reason


def test_falls_back_to_the_typed_reference_when_there_is_no_ship_to(tenant_db):
    customer = _customer(tenant_db, "Kreinke")
    job = _job(tenant_db, customer)
    _order(tenant_db, ship_to=None, po="Kreinke")
    tenant_db.commit()

    suggestions = suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one()).suggestions
    assert suggestions[0].job_id == str(job.id)
    assert "customer_po" in suggestions[0].reason


def test_the_reason_names_the_text_that_produced_the_match(tenant_db):
    """A human confirming needs to see WHY, not just a score."""
    customer = _customer(tenant_db, "Wickham")
    _job(tenant_db, customer)
    _order(tenant_db, ship_to="Wickham A+")
    tenant_db.commit()

    reason = suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one()).suggestions[0].reason
    assert "Wickham A+" in reason
    assert "Wickham" in reason


def test_an_unmatchable_reference_suggests_nothing(tenant_db):
    _customer(tenant_db, "Kreinke")
    _order(tenant_db, ship_to="5.19.26", po="5.19.26")   # a date, not a name
    tenant_db.commit()
    assert suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one()).suggestions == []


def test_an_order_with_no_reference_at_all_suggests_nothing(tenant_db):
    _customer(tenant_db, "Kreinke")
    _order(tenant_db, ship_to=None, po=None)
    tenant_db.commit()
    assert suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one()).suggestions == []


def test_cancelled_jobs_are_never_suggested(tenant_db):
    customer = _customer(tenant_db, "Trende")
    _job(tenant_db, customer, stage="cancelled")
    _order(tenant_db, ship_to="SFL Trende")
    tenant_db.commit()
    assert suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one()).suggestions == []


def test_suggesting_never_writes_anything(tenant_db):
    customer = _customer(tenant_db, "Trende")
    _job(tenant_db, customer)
    order = _order(tenant_db, ship_to="SFL Trende")
    tenant_db.commit()

    suggest_order_job_matches(tenant_db, order)
    tenant_db.expire_all()
    assert tenant_db.query(VendorOrder).one().matched_job_id is None


def test_matches_the_job_title_when_the_customer_name_does_not(tenant_db):
    """The real failure this fixes. Measured on production: the office titles
    the JOB after the jobsite and types that same shorthand on the supplier
    order, while the customer record carries a different name entirely.
    "SFL Swenstad" scored 0.52 against every customer and 1.00 against the job
    title "Swenstad"."""
    customer = _customer(tenant_db, "Bill Greenwaldt")     # nothing like the order text
    job = Job(customer_id=customer.id, company_id=TID, job_number="JOB-9",
              title="Swenstad", lifecycle_stage="scheduled")
    tenant_db.add(job)
    tenant_db.flush()
    _order(tenant_db, ship_to="SFL Swenstad")
    tenant_db.commit()

    result = suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one())
    assert result.suggestions, "job-title matching should have found this"
    assert result.suggestions[0].job_id == str(job.id)
    assert 'job "Swenstad"' in result.suggestions[0].reason


def test_the_customer_route_still_works_for_a_uselessly_titled_job(tenant_db):
    """Both signals are kept, not swapped — a job called "Service call" is
    still reachable through its customer."""
    customer = _customer(tenant_db, "Kreinke")
    job = Job(customer_id=customer.id, company_id=TID, job_number="JOB-8",
              title="Service call", lifecycle_stage="scheduled")
    tenant_db.add(job)
    tenant_db.flush()
    _order(tenant_db, ship_to="Kreinke")
    tenant_db.commit()

    result = suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one())
    assert result.suggestions[0].job_id == str(job.id)
    assert "customer" in result.suggestions[0].reason


def test_a_job_reached_by_both_signals_is_offered_once(tenant_db):
    customer = _customer(tenant_db, "Trende")
    job = Job(customer_id=customer.id, company_id=TID, job_number="JOB-7",
              title="Trende", lifecycle_stage="scheduled")
    tenant_db.add(job)
    tenant_db.flush()
    _order(tenant_db, ship_to="Trende", po="Trende")
    tenant_db.commit()

    result = suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one())
    assert len(result.suggestions) == 1


def test_a_matched_customer_with_no_job_is_reported_not_called_no_match(tenant_db):
    """The UI said "the reference doesn't look like a customer name" for six
    real orders that matched a customer at up to 0.89. Those customers simply
    had no job — which means CREATE ONE, not "junk reference"."""
    customer = _customer(tenant_db, "Chad Bryniarski")     # no job at all
    _order(tenant_db, ship_to="A+ Bryniarski")
    tenant_db.commit()

    result = suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one())
    assert result.suggestions == []
    assert len(result.customers_without_jobs) == 1
    assert result.customers_without_jobs[0].customer_name == "Chad Bryniarski"
    assert result.customers_without_jobs[0].customer_id == str(customer.id)


def test_a_customer_hint_is_suppressed_when_a_job_was_found(tenant_db):
    """It is only useful as a fallback; alongside a real suggestion it is noise."""
    with_job = _customer(tenant_db, "Trende")
    _job(tenant_db, with_job)
    _customer(tenant_db, "Trendel")          # close, but no job
    _order(tenant_db, ship_to="Trende")
    tenant_db.commit()

    result = suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one())
    assert result.suggestions
    assert result.customers_without_jobs == []


def test_a_reference_too_thin_to_be_a_name_is_not_matched(tenant_db):
    """The scorer returns 1.0 when a single token appears anywhere in the other
    string — which is what makes "Wickham A+" ≈ job "Wickham" work, and would
    equally make "A+" match any title containing that letter as a word, at full
    confidence. A confidently-wrong suggestion is the worst outcome here."""
    customer = _customer(tenant_db, "Someone")
    job = Job(customer_id=customer.id, company_id=TID, job_number="JOB-6",
              title="A new door", lifecycle_stage="scheduled")
    tenant_db.add(job)
    tenant_db.flush()
    _order(tenant_db, ship_to="A+", po="A+")
    tenant_db.commit()

    result = suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one())
    assert result.suggestions == []
    assert result.customers_without_jobs == []


def test_a_junk_reference_reports_neither(tenant_db):
    _customer(tenant_db, "Kreinke")
    _order(tenant_db, ship_to="5.19.26", po="5.19.26")
    tenant_db.commit()

    result = suggest_order_job_matches(tenant_db, tenant_db.query(VendorOrder).one())
    assert result.suggestions == []
    assert result.customers_without_jobs == []


# --------------------------------------------------------------------------- #
# confirming — the circle closes
# --------------------------------------------------------------------------- #
def test_one_confirmation_files_both_the_order_and_the_bill(tenant_db):
    """The order number IS the invoice number, so both documents describe one
    purchase. Making someone confirm twice invites filing them to two jobs."""
    customer = _customer(tenant_db, "Trende")
    job = _job(tenant_db, customer)
    order_doc = _document(tenant_db, "order_confirmation.pdf")
    bill_doc = _document(tenant_db, "bill.pdf")
    order = _order(tenant_db, "20386788", ship_to="SFL Trende", document=order_doc)
    _bill(tenant_db, "20386788", document=bill_doc)
    tenant_db.commit()

    result = confirm_order_job(tenant_db, order, job_id=job.id, actor_id="user-1")
    tenant_db.commit()

    assert result.newly_filed_count == 2
    assert {d.kind for d in result.documents} == {"order_confirmation", "bill"}
    assert tenant_db.get(Document, order_doc.id).job_id == job.id
    assert tenant_db.get(Document, bill_doc.id).job_id == job.id


def test_confirming_also_files_by_customer(tenant_db):
    """Nothing set Document.customer_id before — not even the bill path."""
    customer = _customer(tenant_db, "Trende")
    job = _job(tenant_db, customer)
    doc = _document(tenant_db)
    order = _order(tenant_db, ship_to="SFL Trende", document=doc)
    tenant_db.commit()

    confirm_order_job(tenant_db, order, job_id=job.id)
    tenant_db.commit()
    assert tenant_db.get(Document, doc.id).customer_id == customer.id


def test_confirming_marks_the_order_and_the_bill(tenant_db):
    customer = _customer(tenant_db, "Trende")
    job = _job(tenant_db, customer)
    order = _order(tenant_db, "X1", ship_to="SFL Trende")
    _bill(tenant_db, "X1")
    tenant_db.commit()

    confirm_order_job(tenant_db, order, job_id=job.id, actor_id="user-1")
    tenant_db.commit()

    assert tenant_db.query(VendorOrder).one().matched_job_id == job.id
    assert tenant_db.query(VendorOrder).one().job_confirmed_by == "user-1"
    assert tenant_db.query(VendorInvoice).one().matched_job_id == job.id


def test_a_document_already_filed_elsewhere_is_left_alone(tenant_db):
    """Whoever filed it had more context than this. Silently moving a
    customer's paperwork is worse than leaving it."""
    customer = _customer(tenant_db, "Trende")
    job = _job(tenant_db, customer, number="JOB-NEW")
    other_job = _job(tenant_db, customer, number="JOB-OLD")
    doc = _document(tenant_db)
    doc.job_id = other_job.id
    order = _order(tenant_db, ship_to="SFL Trende", document=doc)
    tenant_db.commit()

    result = confirm_order_job(tenant_db, order, job_id=job.id)
    tenant_db.commit()

    assert tenant_db.get(Document, doc.id).job_id == other_job.id   # unmoved
    assert result.newly_filed_count == 0                            # reported honestly
    assert result.documents[0].newly_filed is False


def test_another_vendors_bill_on_the_same_number_is_not_filed(tenant_db):
    """Invoice numbers are unique per supplier, not globally."""
    customer = _customer(tenant_db, "Trende")
    job = _job(tenant_db, customer)
    theirs = _document(tenant_db, "someone_elses.pdf")
    order = _order(tenant_db, "20386788", ship_to="SFL Trende")
    _bill(tenant_db, "20386788", document=theirs, vendor="A Different Supplier")
    tenant_db.commit()

    result = confirm_order_job(tenant_db, order, job_id=job.id)
    tenant_db.commit()

    assert result.documents == []
    assert tenant_db.get(Document, theirs.id).job_id is None


def test_confirming_to_a_job_that_does_not_exist_is_refused(tenant_db):
    from uuid import uuid4

    order = _order(tenant_db, ship_to="SFL Trende")
    tenant_db.commit()
    with pytest.raises(OrderConfirmError):
        confirm_order_job(tenant_db, order, job_id=uuid4())


def test_an_order_with_no_pdf_still_confirms(tenant_db):
    """Filing is a side effect of confirming, not a precondition for it."""
    customer = _customer(tenant_db, "Trende")
    job = _job(tenant_db, customer)
    order = _order(tenant_db, ship_to="SFL Trende", document=None)
    tenant_db.commit()

    result = confirm_order_job(tenant_db, order, job_id=job.id)
    tenant_db.commit()
    assert result.documents == []
    assert tenant_db.query(VendorOrder).one().matched_job_id == job.id


def test_a_soft_deleted_document_is_not_filed(tenant_db):
    from datetime import datetime, timezone

    customer = _customer(tenant_db, "Trende")
    job = _job(tenant_db, customer)
    doc = _document(tenant_db)
    doc.deleted_at = datetime.now(timezone.utc)
    order = _order(tenant_db, ship_to="SFL Trende", document=doc)
    tenant_db.commit()

    result = confirm_order_job(tenant_db, order, job_id=job.id)
    tenant_db.commit()
    assert result.documents == []
