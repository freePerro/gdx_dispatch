"""Job photos on the invoice PDF (Doug 2026-08-07, migration 059).

The office picks job photos on a DRAFT invoice; the pick is stored as a
JSON array of job_photos.id in invoices.attached_photo_ids and renders as
a "Job Photos" grid on the invoice PDF — so photos ride every delivery
channel (email attachment, print, postal mail).

Contract, pinned here:
1. PATCH accepts ids of live photos on THIS invoice's job; stores JSON;
   the serializer decodes it back to a list.
2. A photo id from another job → 422 (never render someone else's photo
   onto a customer's bill). Ids on a job-less invoice → 422.
3. Empty list clears the selection (NULL stored).
4. The PDF resolver returns file:// images in SELECTION order, skipping
   anything unresolvable (deleted photo, missing file, legacy
   /mobile/uploads URL) — the PDF must always render.
5. The payload builder emits attachment_images for every PDF call site,
   and generate_invoice_pdf produces a real PDF with a photo embedded.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    Customer,
    Document,
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Job,
    JobPhoto,
    Payment,
)

TENANT = "tenant-photos"
USER = {"user_id": "user-1", "tenant_id": TENANT, "role": "admin"}


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__,
        Customer.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        InvoiceAdjustment.__table__,
        Payment.__table__,
        JobPhoto.__table__,
        Document.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_job(db) -> Job:
    job = Job(
        customer_id=uuid4(),
        title="Door repair",
        lifecycle_stage="completed",
        company_id=TENANT,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _seed_invoice(db, job, *, status: str = "draft") -> Invoice:
    inv = Invoice(
        id=uuid4(),
        job_id=job.id if job is not None else None,
        customer_id=(job.customer_id if job is not None else uuid4()),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        subtotal=100, tax_amount=0, total=100, balance_due=100,
        status=status,
        public_token=f"tok-{uuid4().hex}",
        company_id=TENANT,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _seed_photo(db, job, *, doc_id=None, kind: str = "after", caption: str = "") -> JobPhoto:
    photo = JobPhoto(
        id=uuid4(),
        company_id=TENANT,
        job_id=job.id,
        kind=kind,
        caption=caption or None,
        url=f"/api/documents/{doc_id or uuid4()}/download",
        filename="p.jpg",
        mime_type="image/jpeg",
        # Shared with the customer (migration 063, 2026-08-12). Photos are
        # internal by default now; in the real flow ATTACHING one to an invoice
        # sets this, so a PDF fixture that seeds the row directly has to say so
        # itself. That the PDF skips an unshared photo is pinned in
        # test_customer_facing_job_photos.py.
        customer_visible=True,
        uploaded_at=datetime.now(UTC),
    )
    db.add(photo)
    db.commit()
    db.refresh(photo)
    return photo


def _patch(db, invoice, ids):
    from gdx_dispatch.routers.invoices import InvoicePatchIn, patch_invoice

    return patch_invoice(
        invoice_id=invoice.id,
        payload=InvoicePatchIn(attached_photo_ids=ids),
        current_user=USER,
        db=db,
    )


# ---------------------------------------------------------------------------
# 1-3. PATCH validation + round-trip.
# ---------------------------------------------------------------------------


def test_patch_stores_and_serializes_photo_ids(db) -> None:
    job = _seed_job(db)
    inv = _seed_invoice(db, job)
    p1 = _seed_photo(db, job, kind="before")
    p2 = _seed_photo(db, job, kind="after")

    out = _patch(db, inv, [str(p1.id), str(p2.id)])
    assert out["attached_photo_ids"] == [str(p1.id), str(p2.id)]
    db.refresh(inv)
    assert json.loads(inv.attached_photo_ids) == [str(p1.id), str(p2.id)]


def test_patch_rejects_photo_from_another_job(db) -> None:
    job = _seed_job(db)
    other = _seed_job(db)
    inv = _seed_invoice(db, job)
    foreign = _seed_photo(db, other)

    with pytest.raises(HTTPException) as exc:
        _patch(db, inv, [str(foreign.id)])
    assert exc.value.status_code == 422
    db.refresh(inv)
    assert inv.attached_photo_ids is None


def test_patch_rejects_photos_on_jobless_invoice(db) -> None:
    inv = _seed_invoice(db, None)
    with pytest.raises(HTTPException) as exc:
        _patch(db, inv, [str(uuid4())])
    assert exc.value.status_code == 422


def test_patch_empty_list_clears_selection(db) -> None:
    job = _seed_job(db)
    inv = _seed_invoice(db, job)
    p1 = _seed_photo(db, job)
    _patch(db, inv, [str(p1.id)])
    out = _patch(db, inv, [])
    assert out["attached_photo_ids"] == []
    db.refresh(inv)
    assert inv.attached_photo_ids is None


def test_patch_still_409s_on_non_draft(db) -> None:
    job = _seed_job(db)
    inv = _seed_invoice(db, job, status="sent")
    p1 = _seed_photo(db, job)
    with pytest.raises(HTTPException) as exc:
        _patch(db, inv, [str(p1.id)])
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# 4-5. PDF resolution + render.
# ---------------------------------------------------------------------------


def _make_image_file(dirpath: Path, name: str) -> Path:
    from PIL import Image

    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / name
    Image.new("RGB", (320, 240), color=(180, 40, 40)).save(path, format="JPEG")
    return path


def _seed_doc_with_file(db, upload_dir: Path, name: str) -> Document:
    _make_image_file(upload_dir, name)
    doc = Document(
        id=uuid4(),
        filename=name,
        original_name=name,
        content_type="image/jpeg",
        entity_type="job_photo",
    )
    db.add(doc)
    db.commit()
    return doc


def test_pdf_resolver_returns_selection_order_and_skips_unresolvable(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    from gdx_dispatch.routers.pdf import _invoice_photos_for_pdf

    job = _seed_job(db)
    inv = _seed_invoice(db, job)
    doc_a = _seed_doc_with_file(db, tmp_path, "a.jpg")
    doc_b = _seed_doc_with_file(db, tmp_path, "b.jpg")
    p_a = _seed_photo(db, job, doc_id=doc_a.id, caption="Broken spring")
    p_b = _seed_photo(db, job, doc_id=doc_b.id, kind="after")
    p_missing = _seed_photo(db, job, doc_id=uuid4())          # doc doesn't exist
    p_legacy = _seed_photo(db, job)
    p_legacy.url = "/mobile/uploads/job_photos/x.jpg"          # dead legacy shape
    db.commit()

    # Selection order b-then-a is display order; unresolvables drop out.
    inv.attached_photo_ids = json.dumps(
        [str(p_b.id), str(p_missing.id), str(p_legacy.id), str(p_a.id)]
    )
    db.commit()

    images = _invoice_photos_for_pdf(db, inv)
    assert [i["name"] for i in images] == ["After", "Broken spring"]
    for img in images:
        assert img["src"].startswith("file://")
        assert Path(img["src"].removeprefix("file://")).exists()


def test_invoice_payload_and_real_pdf_render(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    from gdx_dispatch.core.pdf_generator import generate_invoice_pdf
    from gdx_dispatch.routers.pdf import _invoice_payload

    job = _seed_job(db)
    inv = _seed_invoice(db, job)
    doc = _seed_doc_with_file(db, tmp_path, "spring.jpg")
    photo = _seed_photo(db, job, doc_id=doc.id, caption="New spring installed")
    inv.attached_photo_ids = json.dumps([str(photo.id)])
    db.commit()

    payload = _invoice_payload(inv, None, db)
    assert len(payload["attachment_images"]) == 1
    assert payload["attachment_images"][0]["name"] == "New spring installed"

    pdf = generate_invoice_pdf(invoice_data=payload, tenant_branding=None, template_config=None)
    assert pdf[:5] == b"%PDF-"
    # An embedded JPEG makes the PDF meaningfully bigger than a bare one.
    bare = generate_invoice_pdf(
        invoice_data={**payload, "attachment_images": []},
        tenant_branding=None,
        template_config=None,
    )
    assert len(pdf) > len(bare)
