"""M30 cost-side guards (money audit 2026-08-04).

Two of the eight M30 items are code-fixed here: the Midwest statement
parser silently dropping credit rows (a −$2,900.98 credit vanished and the
payable was overstated by exactly that amount), and vendor-invoice confirm
silently TRUNCATING fractional stock quantities (2.5 → 2, half a unit
gone from every count).
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase, ensure_audit_table
from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
    _LINE_A,
    _looks_like_line_a,
    _money,
)

TENANT = "tenant-m30"


# ── the parser sees credits ────────────────────────────────────────────────

def test_money_parses_every_credit_shape():
    """Audit round 2 proved the first cut booked $-x POSITIVE (the sign was
    stripped after the $) — worse than the silent drop. All five shapes."""
    assert _money("-$2,900.98") == Decimal("-2900.98")
    assert _money("$-2,900.98") == Decimal("-2900.98")
    assert _money("($123.45)") == Decimal("-123.45")
    assert _money("( $500.00)") == Decimal("-500.00")
    assert _money("$2,900.98-") == Decimal("-2900.98")
    assert _money("$1,234.56") == Decimal("1234.56")


def test_unparseable_looks_like_row_raises_loudly():
    """The CLASS fix, driven through the REAL loop: a row with 8 currency
    tokens that fails the shape raises — never a silently shorter payable."""
    from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
        MidwestStatementStructureError,
        _parse_statement_lines,
    )

    bad = ("1234567 REP 01/15/2026 $1.00 $2.00 $3.00 $4.00 $5.00 "
           "$6.00 $7.00 $8.00 TRAILING JUNK")
    with pytest.raises(MidwestStatementStructureError):
        _parse_statement_lines([bad, "7654321 PO-1 / 58 desc"])


def test_credit_pair_parses_signed_through_the_real_loop():
    from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
        _parse_statement_lines,
    )

    a = ("1234567 REP 01/15/2026 -$2,900.98 -$2,900.98 $0.00 $0.00 "
         "$0.00 $0.00 -$2,900.98 $0.00")
    b = "1234567 PO-99 / 58 Credit memo"
    parsed, raw_total = _parse_statement_lines([a, b])
    assert parsed[0].amount == Decimal("-2900.98")
    assert raw_total == Decimal("-2900.98")


def test_normal_row_still_parses():
    row = ("7654321 REP 02/20/2026 $500.00 $500.00 $500.00 $0.00 "
           "$0.00 $0.00 $0.00 $0.00")
    assert _looks_like_line_a(row)
    m = _LINE_A.match(row)
    assert m and _money(m.group("amount")) == Decimal("500.00")


# ── fractional stock quantities refuse, not truncate ───────────────────────

@pytest.fixture(autouse=True)
def _tmp_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


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


def _bill_with_fractional_line(db):
    from gdx_dispatch.modules.vendor_invoices import service as svc
    from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
        ParsedInvoice,
        ParsedInvoiceLine,
    )

    parsed = ParsedInvoice(
        invoice_number=f"MW-{uuid.uuid4().hex[:6]}", invoice_date=None,
        po_reference=None, terms=None, net_days=None, due_date=None,
        tax=Decimal("0"), shipping=Decimal("0"), total=Decimal("250.00"),
        credits_pending=Decimal("0"), amount_due=None,
        lines=[ParsedInvoiceLine(line_no=1, item_label="P", description="Track ft",
                                 quantity=Decimal("2.5"), package=None,
                                 unit_price=Decimal("100.00"), line_total=Decimal("250.00"))],
    )
    r = svc._persist_parsed_invoice(
        db, pdf_bytes=b"%PDF-m30", content_hash=f"h-{uuid.uuid4().hex[:8]}",
        existing_doc=None, parsed=parsed, vendor_name_raw="Midwest Door Co",
        extraction_method="parser", extractor_label="parser",
        original_filename="m30.pdf", content_type="application/pdf",
        uploaded_by="tester", source="upload",
    )
    return r.invoice, sorted(r.invoice.lines, key=lambda x: x.line_no)[0]


def test_fractional_stock_disposition_refuses_instead_of_truncating(db):
    from gdx_dispatch.models.tenant_models import InventoryItem
    from gdx_dispatch.modules.vendor_invoices.confirm import confirm_line

    inv, line = _bill_with_fractional_line(db)
    item = InventoryItem(id=uuid.uuid4(), part_name="Track", quantity=10)
    db.add(item)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        confirm_line(db, inv, line, disposition="stock", company_id=TENANT,
                     actor_id="tester", inventory_item_id=item.id)
    assert exc.value.status_code == 409
    assert "whole units" in str(exc.value.detail)
    db.rollback()
    db.refresh(item)
    assert item.quantity == 10, "the refused confirm must not have moved stock"


def test_whole_quantity_stock_disposition_still_works(db):
    from gdx_dispatch.models.tenant_models import InventoryItem
    from gdx_dispatch.modules.vendor_invoices import service as svc
    from gdx_dispatch.modules.vendor_invoices.confirm import confirm_line
    from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
        ParsedInvoice,
        ParsedInvoiceLine,
    )

    parsed = ParsedInvoice(
        invoice_number=f"MW-{uuid.uuid4().hex[:6]}", invoice_date=None,
        po_reference=None, terms=None, net_days=None, due_date=None,
        tax=Decimal("0"), shipping=Decimal("0"), total=Decimal("300.00"),
        credits_pending=Decimal("0"), amount_due=None,
        lines=[ParsedInvoiceLine(line_no=1, item_label="P", description="Rollers",
                                 quantity=Decimal("3"), package=None,
                                 unit_price=Decimal("100.00"), line_total=Decimal("300.00"))],
    )
    r = svc._persist_parsed_invoice(
        db, pdf_bytes=b"%PDF-m30b", content_hash=f"h-{uuid.uuid4().hex[:8]}",
        existing_doc=None, parsed=parsed, vendor_name_raw="Midwest Door Co",
        extraction_method="parser", extractor_label="parser",
        original_filename="m30b.pdf", content_type="application/pdf",
        uploaded_by="tester", source="upload",
    )
    line = sorted(r.invoice.lines, key=lambda x: x.line_no)[0]
    item = InventoryItem(id=uuid.uuid4(), part_name="Rollers", quantity=10)
    db.add(item)
    db.commit()
    confirm_line(db, r.invoice, line, disposition="stock", company_id=TENANT,
                 actor_id="tester", inventory_item_id=item.id)
    db.commit()
    db.refresh(item)
    assert item.quantity == 13


def test_fractional_job_disposition_still_takes_fractional_coverage(db):
    """Audit round 2: the first 409 fired on the JOB path too — while its
    own remedy text said 'route it to a job'. Fractions are coverage there."""
    from gdx_dispatch.modules.vendor_invoices.confirm import confirm_line

    inv, line = _bill_with_fractional_line(db)
    from gdx_dispatch.models.tenant_models import Customer, Job

    cust = Customer(id=uuid.uuid4(), name="C", company_id=TENANT)
    db.add(cust)
    job = Job(id=uuid.uuid4(), title="Install", customer_id=cust.id, company_id=TENANT)
    db.add(job)
    db.commit()
    confirm_line(db, inv, line, disposition="job", company_id=TENANT,
                 actor_id="tester", job_id=job.id)
    db.commit()
    assert line.status == "confirmed"


def test_po_workflow_double_receive_409s(db):
    """The third (mounted) PO system duplicated van-inventory rows on a
    repeat receive — now the sibling 409 guard refuses."""
    from starlette.requests import Request

    from gdx_dispatch.routers.po_workflow import PORequest, receive_po

    po = PORequest(id=uuid.uuid4(), status="pending", company_id=TENANT, requested_by="tester")
    db.add(po)
    db.commit()
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    receive_po(po.id, request=req, user={"sub": "u"}, db=db, truck_id=None)
    with pytest.raises(HTTPException) as exc:
        receive_po(po.id, request=req, user={"sub": "u"}, db=db, truck_id=None)
    assert exc.value.status_code == 409
