"""Vendor invoice dedup + vendor-resolution — matching-layer tests.

Drives the matching functions against ORM rows in an isolated tenant DB. No
PDF needed, fully deterministic.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from gdx_dispatch.models.tenant_models import Customer, Job, Vendor
from gdx_dispatch.modules.vendor_invoices.matching import (
    compute_vendor_key,
    find_duplicate_invoice,
    flag_possible_duplicate,
    resolve_vendor,
    suggest_job_matches,
)
from gdx_dispatch.modules.vendor_invoices.models import VendorInvoice


def _mk_invoice(db, *, number, total, vendor="Midwest Wholesale Doors",
                inv_date=date(2026, 1, 15), vendor_id=None):
    inv = VendorInvoice(
        vendor_id=vendor_id,
        vendor_key=compute_vendor_key(vendor_id, vendor),
        vendor_name_raw=vendor,
        invoice_number=number,
        invoice_date=inv_date,
        subtotal=Decimal(total),
        tax=Decimal("0.00"),
        shipping=Decimal("0.00"),
        total=Decimal(total),
    )
    db.add(inv)
    db.flush()
    return inv


# --------------------------------------------------------------------------- #
# Vendor resolution (name + aliases)
# --------------------------------------------------------------------------- #
def test_resolve_vendor_exact_and_case(tenant_db):
    v = Vendor(name="Midwest Wholesale Doors")
    tenant_db.add(v)
    tenant_db.flush()

    assert resolve_vendor(tenant_db, "Midwest Wholesale Doors") is v
    assert resolve_vendor(tenant_db, "midwest   wholesale doors") is v  # normalized
    assert resolve_vendor(tenant_db, "Nobody Inc") is None
    assert resolve_vendor(tenant_db, None) is None


def test_resolve_vendor_via_alias(tenant_db):
    v = Vendor(
        name="Midwest Wholesale Doors",
        name_aliases='["Midwest Whsle Doors", "MWD"]',
    )
    tenant_db.add(v)
    tenant_db.flush()

    assert resolve_vendor(tenant_db, "Midwest Whsle Doors") is v
    assert resolve_vendor(tenant_db, "mwd") is v


# --------------------------------------------------------------------------- #
# Layer 2 — (vendor, invoice_number) uniqueness
# --------------------------------------------------------------------------- #
def test_layer2_same_vendor_number_is_duplicate(tenant_db):
    _mk_invoice(tenant_db, number="90000001", total="275.00")

    hit = find_duplicate_invoice(
        tenant_db,
        vendor_id=None,
        vendor_name_raw="Midwest Wholesale Doors",
        invoice_number="90000001",
    )
    assert hit is not None
    assert hit.invoice_number == "90000001"


def test_layer2_case_insensitive_number(tenant_db):
    _mk_invoice(tenant_db, number="ABC-100", total="10.00")
    hit = find_duplicate_invoice(
        tenant_db, vendor_id=None,
        vendor_name_raw="Midwest Wholesale Doors", invoice_number="abc-100",
    )
    assert hit is not None


def test_layer2_different_number_not_duplicate(tenant_db):
    _mk_invoice(tenant_db, number="90000001", total="275.00")
    hit = find_duplicate_invoice(
        tenant_db, vendor_id=None,
        vendor_name_raw="Midwest Wholesale Doors", invoice_number="99999999",
    )
    assert hit is None


def test_layer2_different_vendor_not_duplicate(tenant_db):
    _mk_invoice(tenant_db, number="90000001", total="275.00", vendor="Midwest Wholesale Doors")
    hit = find_duplicate_invoice(
        tenant_db, vendor_id=None,
        vendor_name_raw="Some Other Supplier", invoice_number="90000001",
    )
    assert hit is None


# --------------------------------------------------------------------------- #
# Layer 3 — advisory possible-duplicate flag
# --------------------------------------------------------------------------- #
def test_layer3_same_total_within_window_flags(tenant_db):
    a = _mk_invoice(tenant_db, number="1001", total="275.00", inv_date=date(2026, 1, 1))
    b = _mk_invoice(tenant_db, number="1002", total="275.00", inv_date=date(2026, 1, 20))

    other = flag_possible_duplicate(tenant_db, b)
    assert other is not None and other.id == a.id
    assert b.possible_duplicate_of_id == a.id


def test_layer3_outside_window_not_flagged(tenant_db):
    _mk_invoice(tenant_db, number="1001", total="275.00", inv_date=date(2026, 1, 1))
    b = _mk_invoice(tenant_db, number="1002", total="275.00", inv_date=date(2026, 4, 1))  # >45d

    assert flag_possible_duplicate(tenant_db, b) is None
    assert b.possible_duplicate_of_id is None


def test_layer3_different_total_not_flagged(tenant_db):
    _mk_invoice(tenant_db, number="1001", total="275.00", inv_date=date(2026, 1, 1))
    b = _mk_invoice(tenant_db, number="1002", total="300.00", inv_date=date(2026, 1, 10))

    assert flag_possible_duplicate(tenant_db, b) is None


# --------------------------------------------------------------------------- #
# DB dedup backstop — partial unique index on (vendor_key, invoice_number)
# --------------------------------------------------------------------------- #
def test_compute_vendor_key():
    from uuid import uuid4
    vid = uuid4()
    assert compute_vendor_key(vid, "anything") == str(vid)
    assert compute_vendor_key(None, "Midwest Wholesale Doors") == "midwest wholesale doors"
    assert compute_vendor_key(None, "Midwest  Whsle,  Doors!") == "midwest whsle doors"
    assert compute_vendor_key(None, None) == ""


def test_dedup_index_blocks_same_vendor_and_number(tenant_db):
    """The DB backstop: a second insert with the same (vendor_key, invoice_number)
    is rejected even if the app-level check is bypassed (the concurrency case)."""
    _mk_invoice(tenant_db, number="90000001", total="275.00")
    dup = VendorInvoice(
        vendor_key=compute_vendor_key(None, "Midwest Wholesale Doors"),
        vendor_name_raw="Midwest Wholesale Doors",
        invoice_number="90000001",
        invoice_date=date(2026, 1, 15),
        subtotal=Decimal("275.00"), tax=Decimal("0"), shipping=Decimal("0"),
        total=Decimal("275.00"),
    )
    tenant_db.add(dup)
    with pytest.raises(IntegrityError):
        tenant_db.flush()


def test_dedup_index_blocks_duplicate_document(tenant_db):
    """uq_vendor_invoice_document: one invoice per document (concurrent reuse)."""
    from uuid import uuid4
    doc = uuid4()
    a = _mk_invoice(tenant_db, number="D1", total="1.00")
    a.document_id = doc
    tenant_db.flush()
    b = _mk_invoice(tenant_db, number="D2", total="1.00")  # different number, same doc
    b.document_id = doc
    with pytest.raises(IntegrityError):
        tenant_db.flush()


def test_dedup_index_allows_reimport_after_soft_delete(tenant_db):
    """Partial WHERE deleted_at IS NULL: a voided/deleted bill can be re-imported."""
    first = _mk_invoice(tenant_db, number="90000002", total="10.00")
    first.deleted_at = datetime(2026, 2, 1)  # soft-delete
    tenant_db.flush()
    # Same key again now succeeds because the first is excluded from the index.
    again = _mk_invoice(tenant_db, number="90000002", total="10.00")
    assert again.id != first.id


# --------------------------------------------------------------------------- #
# Job suggestions — PO# text → customer → jobs
# --------------------------------------------------------------------------- #
def test_suggest_matches_customer_by_po_text(tenant_db):
    cust = Customer(name="Example Contractor", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()
    j1 = Job(title="job one", company_id="tenant-test", customer_id=cust.id,
             lifecycle_stage="scheduled")
    j2 = Job(title="job two", company_id="tenant-test", customer_id=cust.id,
             lifecycle_stage="in_progress")
    tenant_db.add_all([j1, j2])
    tenant_db.flush()

    inv = _mk_invoice(tenant_db, number="5001", total="10.00")
    inv.po_reference = "Example Contractor"
    tenant_db.flush()

    suggestions = suggest_job_matches(tenant_db, inv)
    assert any(s.customer_name == "Example Contractor" for s in suggestions)


def test_job_sort_key_tolerates_null_created_at():
    """The tie-break sort must not compare datetime with int when a job's
    created_at is missing (that raised TypeError → 500 on suggestions)."""
    from types import SimpleNamespace

    from gdx_dispatch.modules.vendor_invoices.matching import _job_sort_key

    a = SimpleNamespace(lifecycle_stage="scheduled", created_at=None)
    b = SimpleNamespace(lifecycle_stage="scheduled", created_at=datetime(2026, 1, 1))
    # Would raise TypeError before the fix.
    ordered = sorted([a, b], key=_job_sort_key, reverse=True)
    assert len(ordered) == 2 and a in ordered and b in ordered
