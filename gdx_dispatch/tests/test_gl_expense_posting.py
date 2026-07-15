"""GL Phase 1 (S8) — P5/P6 expense posting + receipts + promote-from-field.

Plan gates: validated category; lines sum to header; gt=0; P6 on every
mutation incl. the line-add endpoint; receipt chain (sha256, soft-delete-
only); JobReceipt promotion.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from starlette.datastructures import Headers, UploadFile

from gdx_dispatch.models.tenant_models import Expense, JobReceipt
from gdx_dispatch.modules.ledger.models import (
    ExpenseReceipt,
    GlAccount,
    GlJournalEntry,
    GlJournalLine,
)
from gdx_dispatch.modules.ledger.service import ensure_gl_seed
from gdx_dispatch.routers.expenses import (
    ExpenseCreate,
    ExpenseLineCreate,
    ExpensePatch,
    PromoteReceiptIn,
    create_expense,
    create_expense_line,
    delete_expense,
    download_expense_receipt,
    list_expense_receipts,
    promote_job_receipt,
    soft_delete_expense_receipt,
    update_expense,
    upload_expense_receipt,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "tester"}


class _Req:
    class _State:
        tenant = {"id": COMPANY}

    state = _State()


@pytest.fixture
def db(tenant_db, monkeypatch, tmp_path):
    monkeypatch.delenv("GDX_ENV", raising=False)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    ensure_gl_seed(tenant_db, COMPANY)
    tenant_db.commit()
    return tenant_db


def _enable(db):
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = True
    db.commit()


def _create(db, amount=125.0, category="Fuel", **kw):
    payload = ExpenseCreate(
        vendor=kw.pop("vendor", "Casey's"),
        amount=amount,
        date=kw.pop("date", dt.date(2026, 7, 2)),
        category=category,
        **kw,
    )
    return create_expense(payload, request=_Req(), _=USER, db=db)


def _entries(db):
    return db.scalars(select(GlJournalEntry).order_by(GlJournalEntry.entry_no)).all()


def _lines_by_code(db, entry):
    out = {}
    for line in db.scalars(select(GlJournalLine).where(GlJournalLine.entry_id == entry.id)):
        acct = db.get(GlAccount, line.account_id)
        out[acct.code] = out.get(acct.code, 0) + line.amount_cents
    return out


# ---------------------------------------------------------------------------
# P5 — creation posting
# ---------------------------------------------------------------------------

def test_flag_off_create_posts_nothing(db):
    _create(db)
    assert _entries(db) == []


def test_p5_posts_category_mapped_debit_and_bank_credit(db):
    _enable(db)
    _create(db, amount=125.0, category="Fuel")
    entries = _entries(db)
    assert len(entries) == 1
    by_code = _lines_by_code(db, entries[0])
    assert by_code["6100"] == 12_500     # Fuel → mapped expense account
    assert by_code["1000"] == -12_500    # paid-on-date: Operating Bank


def test_p5_parts_category_hits_cogs(db):
    _enable(db)
    _create(db, amount=300.0, category="Parts/Supplies")
    by_code = _lines_by_code(db, _entries(db)[0])
    assert by_code["5000"] == 30_000


def test_invalid_category_rejected(db):
    with pytest.raises(HTTPException) as exc:
        _create(db, category="Snacks")
    assert exc.value.status_code == 422
    db.rollback()


def test_legacy_frontend_vocabulary_accepted_and_canonicalized(db):
    """Audit round 4 (executed repro): the shipped frontend speaks
    materials/travel/meals/… — strict validation bricked the Expenses page.
    Legacy values canonicalize at the boundary and post to real accounts."""
    _enable(db)
    created = _create(db, amount=80.0, category="materials")
    assert created["category"] == "Parts/Supplies"
    by_code = _lines_by_code(db, _entries(db)[0])
    assert by_code["5000"] == 8_000            # not 6900 fallback

    created2 = _create(db, amount=20.0, category="other")  # case-insensitive
    assert created2["category"] == "Other"


def test_legacy_category_on_historical_row_posts_mapped_account(db):
    """Rows written before S8 keep legacy category strings — posting (and
    the S10 backfill) must canonicalize on the fly, not dump them in 6900."""
    from gdx_dispatch.modules.ledger.rules import post_expense_recorded

    _enable(db)
    expense = Expense(
        vendor="Legacy Depot", amount=40.0, date=dt.date(2026, 7, 1),
        category="equipment",  # pre-S8 vocabulary, written raw to the DB
        company_id=COMPANY,
    )
    db.add(expense)
    db.flush()
    post_expense_recorded(db, expense)
    db.commit()
    by_code = _lines_by_code(db, _entries(db)[0])
    assert by_code["6200"] == 4_000            # Tools/Equipment account


def test_zero_amount_rejected_by_schema():
    with pytest.raises(ValidationError):
        ExpenseCreate(vendor="X", amount=0, date=dt.date(2026, 7, 2), category="Fuel")


# ---------------------------------------------------------------------------
# P6 — mutations
# ---------------------------------------------------------------------------

def test_p6_patch_reposts_at_new_content(db):
    _enable(db)
    created = _create(db, amount=100.0, category="Fuel")
    expense = db.get(Expense, UUID(created["id"]))
    update_expense(expense.id, ExpensePatch(amount=140.0), _=USER, db=db)

    entries = _entries(db)
    live = [e for e in entries if e.status == "posted" and e.reverses_entry_id is None]
    assert len(live) == 1
    assert _lines_by_code(db, live[0])["6100"] == 14_000
    assert len([e for e in entries if e.status == "reversed"]) == 1


def test_p6_category_change_moves_account(db):
    _enable(db)
    created = _create(db, amount=50.0, category="Fuel")
    expense = db.get(Expense, UUID(created["id"]))
    update_expense(expense.id, ExpensePatch(category="Advertising"), _=USER, db=db)
    live = [e for e in _entries(db) if e.status == "posted" and e.reverses_entry_id is None]
    assert _lines_by_code(db, live[0])["6300"] == 5_000


def test_p6_soft_delete_reverses(db):
    _enable(db)
    created = _create(db)
    expense = db.get(Expense, UUID(created["id"]))
    delete_expense(expense.id, _=USER, db=db)
    live = [e for e in _entries(db) if e.status == "posted" and e.reverses_entry_id is None]
    assert live == []


def test_line_add_rejects_overshoot_allows_incremental_build(db):
    """Lines build incrementally: undersum defers the repost (header entry
    stays live), overshoot 409s (it can never reconcile), equality reposts."""
    _enable(db)
    created = _create(db, amount=100.0)
    expense = db.get(Expense, UUID(created["id"]))

    create_expense_line(expense.id, ExpenseLineCreate(account="parts", amount=30.0), _=USER, db=db)
    assert len(_entries(db)) == 1  # under-complete: no repost, header entry live

    with pytest.raises(HTTPException) as exc:  # 30 + 80 = 110 > 100
        create_expense_line(expense.id, ExpenseLineCreate(account="misc", amount=80.0), _=USER, db=db)
    assert exc.value.status_code == 409 and "over the header" in exc.value.detail
    db.rollback()

    create_expense_line(expense.id, ExpenseLineCreate(account="labor", amount=70.0), _=USER, db=db)
    live = [e for e in _entries(db) if e.status == "posted" and e.reverses_entry_id is None]
    assert len(live) == 1  # complete set — still one live, header-level entry


def test_p6_noop_when_content_unchanged(db):
    _enable(db)
    created = _create(db)
    expense = db.get(Expense, UUID(created["id"]))
    update_expense(expense.id, ExpensePatch(description="notes only... wait"), _=USER, db=db)
    # description isn't economic content — but vendor is in the memo, not the
    # hash; amount/category/date unchanged → same key → no repost
    assert len(_entries(db)) == 1


# ---------------------------------------------------------------------------
# Receipts (spec §3.7)
# ---------------------------------------------------------------------------

def _upload(db, expense_id, blob=b"fake-jpeg-bytes", filename="receipt.jpg", ctype="image/jpeg"):
    file = UploadFile(
        file=io.BytesIO(blob),
        filename=filename,
        headers=Headers({"content-type": ctype}),
    )
    return upload_expense_receipt(expense_id, file=file, _=USER, db=db)


def test_receipt_upload_hashes_and_dedupes(db):
    created = _create(db)
    expense = db.get(Expense, UUID(created["id"]))
    blob = b"the-receipt-image"
    first = _upload(db, expense.id, blob=blob)
    assert first["sha256"] == hashlib.sha256(blob).hexdigest()
    again = _upload(db, expense.id, blob=blob, filename="same-content.jpg")
    assert again["id"] == first["id"]  # content-identical → idempotent
    assert len(list_expense_receipts(expense.id, _=USER, db=db)) == 1


def test_receipt_download_roundtrips_original(db):
    created = _create(db)
    expense = db.get(Expense, UUID(created["id"]))
    blob = b"%PDF-1.7 fake"
    uploaded = _upload(db, expense.id, blob=blob, filename="inv.pdf", ctype="application/pdf")
    response = download_expense_receipt(expense.id, UUID(uploaded["id"]), _=USER, db=db)
    assert open(response.path, "rb").read() == blob


def test_receipt_soft_delete_keeps_row_and_file(db):
    created = _create(db)
    expense = db.get(Expense, UUID(created["id"]))
    uploaded = _upload(db, expense.id)
    soft_delete_expense_receipt(expense.id, UUID(uploaded["id"]), _=USER, db=db)

    assert list_expense_receipts(expense.id, _=USER, db=db) == []  # hidden
    row = db.get(ExpenseReceipt, UUID(uploaded["id"]))
    assert row is not None and row.deleted_at is not None            # retained
    assert open(row.storage_path, "rb").read()                        # file kept


def test_receipt_rejects_wrong_type_and_empty(db):
    created = _create(db)
    expense = db.get(Expense, UUID(created["id"]))
    with pytest.raises(HTTPException) as exc:
        _upload(db, expense.id, ctype="application/zip")
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        _upload(db, expense.id, blob=b"")
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# Promote-from-field
# ---------------------------------------------------------------------------

def _job_receipt(db, amount="87.50", vendor="Menards"):
    receipt = JobReceipt(
        job_id=uuid4(),
        vendor=vendor,
        amount=Decimal(amount),
        notes="springs + brackets",
        purchased_at=dt.datetime(2026, 7, 3, 14, 0, tzinfo=dt.UTC),
    )
    db.add(receipt)
    db.commit()
    return receipt


def test_promote_creates_prefilled_expense_and_links(db):
    _enable(db)
    receipt = _job_receipt(db)
    result = promote_job_receipt(
        PromoteReceiptIn(job_receipt_id=receipt.id), request=_Req(), _=USER, db=db
    )
    assert result["vendor"] == "Menards"
    assert result["amount"] == 87.5
    assert result["category"] == "Parts/Supplies"
    assert result["date"] == "2026-07-03"
    db.refresh(receipt)
    assert str(receipt.promoted_expense_id) == result["id"]
    by_code = _lines_by_code(db, _entries(db)[0])
    assert by_code["5000"] == 8_750  # posted through P5

    # idempotent — a second promote returns the same expense
    again = promote_job_receipt(
        PromoteReceiptIn(job_receipt_id=receipt.id), request=_Req(), _=USER, db=db
    )
    assert again["id"] == result["id"]
    assert db.scalar(select(GlJournalEntry.entry_no).order_by(GlJournalEntry.entry_no.desc())) == 1


def test_promote_requires_amount(db):
    receipt = _job_receipt(db)
    receipt.amount = None
    db.commit()
    with pytest.raises(HTTPException) as exc:
        promote_job_receipt(PromoteReceiptIn(job_receipt_id=receipt.id), request=_Req(), _=USER, db=db)
    assert exc.value.status_code == 422
