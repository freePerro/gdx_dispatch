"""Picked attachments on the estimate and invoice /send composers.

The access rule and the per-kind loading are pinned by
test_email_attachments.py; this suite pins what each send endpoint does with
the result:

- the picked files go out beside the document's own PDF;
- a pick that cannot be loaded (404) stops the send before anything changes:
  no email, no status flip;
- the document's own PDF gets only what the picked files leave of the
  per-email budget, and is skipped (reported) rather than refused;
- ``pdf_attached`` means THIS document's PDF, not a picked estimate PDF that
  happens to share its name shape;
- the audit row names what was picked.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

import gdx_dispatch.core.transactional_email as tx
from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.email_attachments import AttachmentRef
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, Job
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers import estimates as est_router
from gdx_dispatch.routers import invoices as inv_router

TENANT = "11111111-1111-1111-1111-111111111111"
OFFICE = {"user_id": "office-user", "sub": "office-user", "tenant_id": TENANT, "role": "owner"}


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    return req


def _upload(name: str, n_bytes: int) -> AttachmentRef:
    return AttachmentRef(kind="upload", name=name, content_type="application/pdf",
                         content_base64=base64.b64encode(b"x" * n_bytes).decode())


def _missing_document() -> AttachmentRef:
    return AttachmentRef(kind="document", id=str(uuid4()))


@pytest.fixture()
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    monkeypatch.setattr(tx, "recently_sent", lambda *a, **k: False)
    monkeypatch.setattr("gdx_dispatch.core.webhooks.emit.emit_domain_event", lambda *a, **k: 0)
    return tenant_db


@pytest.fixture()
def outbox(monkeypatch):
    """Every send_transactional_email call, delivered."""
    sent: list[dict] = []

    def fake_send(**kw):
        sent.append(kw)
        return True, "outlook_graph", None

    monkeypatch.setattr(tx, "send_transactional_email", fake_send)
    return sent


def _customer(db) -> Customer:
    c = Customer(id=uuid4(), name="Pat Payer", email="pat@example.com", company_id=TENANT)
    db.add(c)
    db.commit()
    return c


def _recipient():
    return SimpleNamespace(ok=True, email="pat@example.com", to_name="Pat",
                           source="customer", contact_id=None)


def _audit(db, action):
    return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def _names(call) -> list[str]:
    return [a["name"] for a in call["attachments"] or ()]


# ── estimate ───────────────────────────────────────────────────────────


@pytest.fixture()
def estimate(db, monkeypatch):
    cust = _customer(db)
    est = Estimate(id=uuid4(), customer_id=cust.id, estimate_number="E-100", label="Door",
                   proposal_mode=False, total=Decimal("1850"), status="draft",
                   public_token=uuid4().hex, company_id=TENANT)
    db.add(est)
    db.commit()
    monkeypatch.setattr(est_router, "_prepare_estimate_email", lambda *a, **k: {
        "customer": cust, "subject": "Your estimate", "html": "<p>h</p>", "recipient": _recipient(),
        "pdf_valid_until": None,
    })
    monkeypatch.setattr(est_router, "_estimate_pdf_bytes", lambda *a, **k: b"%PDF" + b"e" * 96)
    return est


def _send_estimate(db, est, refs):
    return est_router.send_estimate(
        estimate_id=est.id, payload=est_router.SendEstimateIn(attachment_refs=refs),
        _=OFFICE, db=db, request=_request(),
    )


def test_estimate_picked_files_go_beside_its_own_pdf(db, estimate, outbox):
    out = _send_estimate(db, estimate, [_upload("warranty.pdf", 50)])

    assert out["email_sent"] is True and out["pdf_attached"] is True
    (call,) = outbox
    assert _names(call) == ["estimate-E-100.pdf", "warranty.pdf"]
    (row,) = _audit(db, "estimate_sent")
    assert row.details["attachments"] == [
        {"name": "warranty.pdf", "size_bytes": 50, "kind": "upload", "ref_id": None},
    ]


def test_estimate_bad_pick_sends_nothing_and_changes_nothing(db, estimate, outbox):
    with pytest.raises(HTTPException) as exc:
        _send_estimate(db, estimate, [_upload("ok.pdf", 10), _missing_document()])

    assert exc.value.status_code == 404
    assert outbox == []
    db.refresh(estimate)
    assert estimate.status == "draft" and estimate.sent_at is None


def test_estimate_pdf_is_skipped_when_the_picks_fill_the_budget(db, estimate, outbox, monkeypatch):
    monkeypatch.setattr(tx, "MAX_ATTACHMENT_BYTES", 120)

    out = _send_estimate(db, estimate, [_upload("estimate-E-200.pdf", 60)])

    (call,) = outbox
    # Only the pick went; it carries the estimate- name shape, and still
    # pdf_attached is False — this estimate's own PDF did not go.
    assert _names(call) == ["estimate-E-200.pdf"]
    assert out["pdf_attached"] is False
    assert "estimate PDF" in out["attachments_skipped"]


def test_estimate_without_picks_is_unchanged(db, estimate, outbox):
    out = _send_estimate(db, estimate, None)
    (call,) = outbox
    assert _names(call) == ["estimate-E-100.pdf"] and out["pdf_attached"] is True
    (row,) = _audit(db, "estimate_sent")
    assert row.details["attachments"] is None


# ── invoice ────────────────────────────────────────────────────────────


@pytest.fixture()
def invoice(db, monkeypatch):
    cust = _customer(db)
    job = Job(customer_id=cust.id, title="Spring", lifecycle_stage="completed", company_id=TENANT)
    db.add(job)
    db.flush()
    inv = Invoice(id=uuid4(), job_id=job.id, customer_id=cust.id, invoice_number="I-100",
                  status="draft", verified_at=datetime.now(UTC), subtotal=Decimal("100"),
                  tax_amount=Decimal("0"), total=Decimal("100"), balance_due=Decimal("100"),
                  public_token=uuid4().hex, company_id=TENANT)
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, description="Work", quantity=1, unit_price=Decimal("100"),
                       line_total=Decimal("100"), taxable=False, company_id=TENANT))
    db.commit()
    monkeypatch.setattr(inv_router, "_prepare_invoice_email", lambda *a, **k: {
        "customer": cust, "subject": "Your invoice", "html": "<p>h</p>", "recipient": _recipient(),
    })
    budgets: list[int | None] = []

    def fake_pdf(db, invoice, cust, *, budget_bytes=None):
        budgets.append(budget_bytes)
        if budget_bytes is not None and budget_bytes < 100:
            return None
        return [{"name": "invoice-I-100.pdf", "content_type": "application/pdf",
                 "content_base64": base64.b64encode(b"p" * 100).decode()}]

    monkeypatch.setattr(inv_router, "_invoice_pdf_attachments", fake_pdf)
    inv.pdf_budgets = budgets
    return inv


def _send_invoice(db, inv, refs):
    return inv_router.send_invoice(
        invoice_id=inv.id, payload=inv_router.SendInvoiceIn(attachment_refs=refs),
        _=OFFICE, db=db, request=_request(),
    )


def test_invoice_picked_files_go_beside_its_pdf(db, invoice, outbox):
    out = _send_invoice(db, invoice, [_upload("photo-sheet.pdf", 40)])

    assert out["email_sent"] is True and out["pdf_attached"] is True
    (call,) = outbox
    assert _names(call) == ["invoice-I-100.pdf", "photo-sheet.pdf"]
    assert invoice.pdf_budgets == [tx.MAX_ATTACHMENT_BYTES - 40]
    (row,) = _audit(db, "invoice_sent")
    assert row.details["attachments"][0]["name"] == "photo-sheet.pdf"


def test_invoice_bad_pick_leaves_the_draft_a_draft(db, invoice, outbox):
    with pytest.raises(HTTPException) as exc:
        _send_invoice(db, invoice, [_missing_document()])

    assert exc.value.status_code == 404
    assert outbox == []
    db.expire_all()
    assert db.get(Invoice, invoice.id).status == "draft"
    assert _audit(db, "invoice_sent") == []


def test_invoice_pdf_is_skipped_when_the_picks_fill_the_budget(db, invoice, outbox, monkeypatch):
    monkeypatch.setattr(tx, "MAX_ATTACHMENT_BYTES", 150)

    out = _send_invoice(db, invoice, [_upload("big.pdf", 80)])

    (call,) = outbox
    assert _names(call) == ["big.pdf"]
    assert out["pdf_attached"] is False
