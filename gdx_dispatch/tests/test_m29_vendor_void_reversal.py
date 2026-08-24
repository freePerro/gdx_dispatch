"""Voiding a vendor bill reverses what its confirmed lines created (M29).

PATCH used to flip status to void with no compensating action: confirmed
lines had already created Expense rows, incremented stock, and minted
checklist rows — voiding a $3,120 bill left all three standing, and the
corrected re-issue imported cleanly (the dedup index keys on deleted_at, not
status), so the costs existed twice.

Now the void reverses, keyed on the line: the Expense soft-deletes (and its
live ledger entry reverses when posting is on), stock takes the negative
delta through the same apply_stock_delta that added it, an UNBILLED checklist
row is removed — and a BILLED one blocks the void with a structured 409,
because billing supersedes and quietly deleting a billed row would break the
invoice's provenance.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, ensure_audit_table
from gdx_dispatch.models.tenant_models import Expense, InventoryItem, Job, JobPartNeeded

TENANT = "tenant-m29"
OFFICE = {"id": "office-user", "email": "office@example.com", "role": "admin"}


@pytest.fixture(autouse=True)
def _tmp_upload_dir(tmp_path, monkeypatch):
    """_persist_parsed_invoice writes the PDF under UPLOAD_DIR (default
    /app/uploads/) — writable in the local docker harness, read-only in CI.
    Same redirect test_vendor_invoice_upload.py uses."""
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


def _bill_with_confirmed_lines(db, *, with_stock=False):
    """A real bill via the real persistence + real confirms."""
    from gdx_dispatch.modules.vendor_invoices import service as svc
    from gdx_dispatch.modules.vendor_invoices.confirm import confirm_line
    from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
        ParsedInvoice,
        ParsedInvoiceLine,
    )

    job = Job(id=uuid.uuid4(), title="Install", customer_id=uuid.uuid4(), company_id=TENANT)
    db.add(job)
    item = None
    if with_stock:
        item = InventoryItem(id=uuid.uuid4(), part_name="Torsion spring",
                             quantity=10)
        db.add(item)
    db.commit()

    parsed = ParsedInvoice(
        invoice_number=f"MW-{uuid.uuid4().hex[:6]}", invoice_date=None,
        po_reference=None, terms=None, net_days=None, due_date=None,
        tax=Decimal("0"), shipping=Decimal("0"), total=Decimal("300.00"),
        credits_pending=Decimal("0"), amount_due=None,
        lines=[
            ParsedInvoiceLine(line_no=1, item_label="P1", description="Spring pair",
                              quantity=Decimal("2"), package=None,
                              unit_price=Decimal("100.00"), line_total=Decimal("200.00")),
            ParsedInvoiceLine(line_no=2, item_label="P2", description="Rollers",
                              quantity=Decimal("4"), package=None,
                              unit_price=Decimal("25.00"), line_total=Decimal("100.00")),
        ],
    )
    r = svc._persist_parsed_invoice(
        db, pdf_bytes=b"%PDF-m29", content_hash=f"h-{uuid.uuid4().hex[:8]}",
        existing_doc=None, parsed=parsed, vendor_name_raw="Midwest Door Co",
        extraction_method="parser", extractor_label="parser",
        original_filename="m29.pdf", content_type="application/pdf",
        uploaded_by="tester", source="upload",
    )
    inv = r.invoice
    l1, l2 = sorted(inv.lines, key=lambda x: x.line_no)
    confirm_line(db, inv, l1, disposition="job", company_id=TENANT,
                 actor_id="tester", job_id=job.id)
    if with_stock:
        confirm_line(db, inv, l2, disposition="stock", company_id=TENANT,
                     actor_id="tester", inventory_item_id=item.id)
    db.commit()
    return inv, job, item, (l1, l2)


def _void(db, inv):
    from gdx_dispatch.routers.vendor_invoices import InvoicePatch, patch_invoice

    class _Req:  # only .state.tenant is read
        class state:
            tenant = {"id": TENANT}

    return asyncio.run(patch_invoice(
        inv.id, InvoicePatch(status="void"), request=_Req(), user=OFFICE, db=db,
    ))


def test_void_reverses_the_expense(db):
    """THE FIX. The job-disposition Expense soft-deletes on void — the cost
    stops existing, so the corrected re-issue cannot double it."""
    inv, job, _, (l1, _) = _bill_with_confirmed_lines(db)
    exp_id = l1.expense_id
    assert exp_id is not None
    _void(db, inv)
    exp = db.get(Expense, exp_id)
    assert exp.deleted_at is not None, "the void left the Expense standing"
    assert inv.status == "void"


def test_void_reverses_the_stock_increment(db):
    inv, _, item, (_, l2) = _bill_with_confirmed_lines(db, with_stock=True)
    db.refresh(item)
    assert item.quantity == 14, "confirm should have added 4"
    _void(db, inv)
    db.refresh(item)
    assert item.quantity == 10, "the void must take the same 4 back out"


def test_void_removes_the_unbilled_checklist_row(db):
    inv, _, _, (l1, _) = _bill_with_confirmed_lines(db)
    jpn_id = l1.job_part_needed_id
    assert jpn_id is not None
    _void(db, inv)
    assert db.get(JobPartNeeded, jpn_id) is None


def test_a_billed_checklist_row_blocks_the_void(db):
    """Billing supersedes: the customer was invoiced for this part off the
    checklist row — deleting it silently would break that invoice's
    provenance. The void refuses with the line named."""
    from fastapi import HTTPException

    inv, _, _, (l1, _) = _bill_with_confirmed_lines(db)
    jpn = db.get(JobPartNeeded, l1.job_part_needed_id)
    jpn.billed_invoice_id = uuid.uuid4()
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _void(db, inv)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "line_billed"
    assert inv.status != "void"
    # and nothing was reversed
    assert db.get(Expense, l1.expense_id).deleted_at is None


def test_lines_return_to_pending_for_a_clean_reissue(db):
    inv, _, _, (l1, _) = _bill_with_confirmed_lines(db)
    _void(db, inv)
    db.refresh(l1)
    assert l1.status == "pending"
    assert l1.expense_id is None and l1.job_part_needed_id is None


def test_the_trail_records_the_reversal_counts(db):
    inv, _, _, _ = _bill_with_confirmed_lines(db, with_stock=True)
    _void(db, inv)
    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "vendor_invoice_void_reversed_lines")
        .first()
    )
    assert row is not None, "a reversal that leaves no trace is a silent write"
    assert row.details["expenses_reversed"] == 1
    assert row.details["stock_reversed"] == 1
    # posting flag OFF and no entry exists -> the trail must NOT claim a
    # ledger reversal that never posted (audit round 2: the counter used to
    # read True unconditionally, over a swallowed exception).
    assert row.details["ledger_reversed"] == 0


def test_a_bill_with_no_confirmed_lines_voids_exactly_as_before(db):
    from gdx_dispatch.modules.vendor_invoices import service as svc
    from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
        ParsedInvoice,
        ParsedInvoiceLine,
    )

    parsed = ParsedInvoice(
        invoice_number=f"MW-{uuid.uuid4().hex[:6]}", invoice_date=None,
        po_reference=None, terms=None, net_days=None, due_date=None,
        tax=Decimal("0"), shipping=Decimal("0"), total=Decimal("50.00"),
        credits_pending=Decimal("0"), amount_due=None,
        lines=[ParsedInvoiceLine(line_no=1, item_label="P", description="Hinge",
                                 quantity=Decimal("1"), package=None,
                                 unit_price=Decimal("50.00"), line_total=Decimal("50.00"))],
    )
    r = svc._persist_parsed_invoice(
        db, pdf_bytes=b"%PDF-m29b", content_hash=f"h-{uuid.uuid4().hex[:8]}",
        existing_doc=None, parsed=parsed, vendor_name_raw="Midwest Door Co",
        extraction_method="parser", extractor_label="parser",
        original_filename="m29b.pdf", content_type="application/pdf",
        uploaded_by="tester", source="upload",
    )
    _void(db, r.invoice)
    assert r.invoice.status == "void"


# ---------------------------------------------------------------------------
# Audit round 2 — the ledger half, under a REAL posting flag. All prior tests
# ran flag-off; the entire reverse-the-books branch had zero coverage.
# ---------------------------------------------------------------------------

def _enable_posting(db):
    from gdx_dispatch.modules.ledger.service import ensure_gl_seed

    settings = ensure_gl_seed(db, TENANT)
    settings.ledger_posting_enabled = True
    db.commit()
    return settings


def _expense_entries(db, expense_id):
    from sqlalchemy import select as _select

    from gdx_dispatch.modules.ledger.models import GlJournalEntry

    return db.scalars(_select(GlJournalEntry).where(
        GlJournalEntry.source_type == "expense",
        GlJournalEntry.source_id == str(expense_id),
    )).all()


def test_void_reverses_the_posted_ledger_entry_flag_on(db):
    """Flag ON: confirm posts a GL entry; the void must post its mirror and
    the trail must count it."""
    _enable_posting(db)
    inv, _, _, (l1, _) = _bill_with_confirmed_lines(db)
    exp_id = l1.expense_id
    entries = _expense_entries(db, exp_id)
    assert len(entries) == 1 and entries[0].status == "posted", "fixture: confirm must post"
    _void(db, inv)
    entries = _expense_entries(db, exp_id)
    assert len(entries) == 2, "the void posted no reversal entry"
    assert {e.status for e in entries} == {"reversed", "posted"}
    assert any(e.reverses_entry_id is not None for e in entries)
    row = (db.query(AuditLog)
           .filter(AuditLog.action == "vendor_invoice_void_reversed_lines").first())
    assert row.details["ledger_reversed"] == 1


def test_void_reverses_even_when_flag_turned_off_after_posting(db):
    """Existence is the predicate, not the flag: an entry posted while the
    flag was ON must still unwind when the flag is OFF at void time —
    otherwise the void orphans a live GL entry (audit round 2, finding 1b)."""
    settings = _enable_posting(db)
    inv, _, _, (l1, _) = _bill_with_confirmed_lines(db)
    exp_id = l1.expense_id
    assert len(_expense_entries(db, exp_id)) == 1
    settings.ledger_posting_enabled = False
    db.commit()
    _void(db, inv)
    assert len(_expense_entries(db, exp_id)) == 2, "flag-off void orphaned the entry"


def test_locked_month_void_posts_the_reversal_in_the_open_period(db):
    """June closed, bill confirmed in June, voided in the open period: the
    reversal posts at TODAY (payments.py's documented escape hatch), never a
    refusal or a swallowed failure. Per-account net across all dates is zero."""
    from collections import defaultdict
    from datetime import date

    from sqlalchemy import select as _select

    from gdx_dispatch.modules.ledger.models import GlJournalLine

    _enable_posting(db)
    inv, _, _, (l1, _) = _bill_with_confirmed_lines(db)
    exp = db.get(Expense, l1.expense_id)
    entry = _expense_entries(db, exp.id)[0]
    # move the books to June, then close June
    exp.date = date(2026, 6, 20)
    entry.effective_at = date(2026, 6, 20)
    from gdx_dispatch.modules.ledger.models import GlPeriodLock

    db.add(GlPeriodLock(lock_date=date(2026, 6, 30), company_id=TENANT))
    db.commit()

    _void(db, inv)

    entries = _expense_entries(db, exp.id)
    assert len(entries) == 2
    reversal = next(e for e in entries if e.reverses_entry_id is not None)
    assert reversal.effective_at > date(2026, 6, 30), "reversal must land in the open period"
    sums = defaultdict(int)
    ids = {e.id for e in entries}
    for line in db.scalars(_select(GlJournalLine).where(GlJournalLine.entry_id.in_(ids))).all():
        sums[line.account_id] += int(line.amount_cents)
    assert all(v == 0 for v in sums.values()), f"books diverge after void: {dict(sums)}"


def test_everything_locked_refuses_the_whole_void(db):
    """The expense month AND today are locked: the void must 409 whole —
    committing a soft-deleted expense whose GL entry still stands would be
    M29's own shape rebuilt inside its fix (audit round 2, finding 1)."""
    from datetime import date

    from fastapi import HTTPException

    _enable_posting(db)
    inv, _, _, (l1, _) = _bill_with_confirmed_lines(db)
    from gdx_dispatch.modules.ledger.models import GlPeriodLock

    db.add(GlPeriodLock(lock_date=date(2099, 12, 31), company_id=TENANT))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        _void(db, inv)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "period_locked"
    # nothing committed: discard the aborted request state like get_db does
    db.rollback()
    assert db.get(Expense, l1.expense_id).deleted_at is None
    db.refresh(inv)
    assert inv.status != "void"
    assert len(_expense_entries(db, l1.expense_id)) == 1


def test_stock_reversal_uses_the_recorded_delta_not_the_edited_quantity(db):
    """The reversal negates the STORED StockAdjustment.quantity_delta. A
    quantity edited after confirm must not change what comes back out
    (audit round 2, blind spot: re-derivation vs recorded artifact)."""
    inv, _, item, (_, l2) = _bill_with_confirmed_lines(db, with_stock=True)
    l2.quantity = Decimal("9")  # office "fixed" the line after confirm
    db.commit()
    _void(db, inv)
    db.refresh(item)
    assert item.quantity == 10, "reversal must undo the recorded +4, not the edited 9"


def test_double_void_reverses_nothing_twice(db):
    from gdx_dispatch.models.tenant_models import StockAdjustment

    inv, _, item, _ = _bill_with_confirmed_lines(db, with_stock=True)
    _void(db, inv)
    n_adj = db.query(StockAdjustment).count()
    _void(db, inv)  # second void: lines are pending, nothing to reverse
    assert db.query(StockAdjustment).count() == n_adj
    db.refresh(item)
    assert item.quantity == 10


def test_void_clears_the_review_stamp(db):
    """void->open is a legal transition; a reopened bill with every line
    back to pending must not claim it was reviewed (audit round 2, residue)."""
    from datetime import datetime, timezone

    inv, _, _, _ = _bill_with_confirmed_lines(db)
    inv.reviewed_at = datetime.now(timezone.utc)
    inv.reviewed_by_user_id = "office-user"
    db.commit()
    _void(db, inv)
    db.refresh(inv)
    assert inv.reviewed_at is None and inv.reviewed_by_user_id is None


def test_the_helper_itself_refuses_a_billed_row_past_a_stale_identity_map(db):
    """The router's fast-path check can race billing's bulk UPDATE. The
    helper's locked re-read with populate_existing is the authoritative
    check: poison the identity map with the unbilled object, stamp
    billed_invoice_id behind the ORM's back, and the helper must still
    refuse (without populate_existing it reads the stale object and
    deletes a billed row)."""
    from sqlalchemy import text as _text

    from gdx_dispatch.modules.vendor_invoices.confirm import (
        LineBilledError,
        reverse_confirmed_line,
    )

    inv, _, _, (l1, _) = _bill_with_confirmed_lines(db)
    stale = db.get(JobPartNeeded, l1.job_part_needed_id)  # poison the map
    assert stale.billed_invoice_id is None
    db.execute(
        _text("UPDATE job_parts_needed SET billed_invoice_id = :b WHERE id = :i"),
        {"b": str(uuid.uuid4()), "i": str(l1.job_part_needed_id)},
    )
    with pytest.raises(LineBilledError):
        reverse_confirmed_line(db, inv, l1, actor_id="tester")
