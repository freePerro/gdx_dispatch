"""Job photos, shown to the customer (Doug 2026-08-12: "make the photos
customer facing").

The tech photographs every job; until now the customer never saw one. Two
surfaces now show them, and both are exposure boundaries, so what is pinned
here is mostly what must NOT happen:

1. The portal serves a customer their OWN job's photos, and 404s another
   customer's job — a job id alone is never a key to someone else's pictures.
2. The public pay page shows ONLY the photos attached to that invoice, inlined
   into the page rather than served from a new anonymous route — the pay page
   is unauthenticated by design, and the ungated-route baseline is a ratchet to
   work down, not somewhere to record a new exception for a picture.
3. NOTHING is customer-visible by default. Doug 2026-08-12: "per photo default
   off" — a tech also photographs damage found on arrival, hazards and other
   people's messes, so a photo reaches the customer when someone decides it
   should, not because it exists. One flag (customer_visible, migration 063)
   gates the portal, the pay page and the invoice PDF together.
4. One resolver decides what is servable (core/job_photos.resolve_photo_file),
   so the PDF, the portal and the pay page cannot disagree about which photos
   a customer sees. It refuses non-images, missing files, deleted documents,
   and the dead legacy /mobile/uploads urls.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.job_photos import resolve_photo_file
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
from gdx_dispatch.routers import portal as portal_router

TENANT = "tenant-photos-cust"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__, Customer.__table__, Invoice.__table__, InvoiceLine.__table__,
        InvoiceAdjustment.__table__, Payment.__table__, JobPhoto.__table__, Document.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    session._upload_dir = tmp_path  # handy for tests that write bytes
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _customer_with_job(db, name: str) -> tuple[Customer, Job]:
    cust = Customer(id=uuid4(), name=name, company_id=TENANT)
    db.add(cust)
    db.commit()
    job = Job(customer_id=cust.id, title=f"{name} door", lifecycle_stage="completed", company_id=TENANT)
    db.add(job)
    db.commit()
    db.refresh(job)
    return cust, job


def _photo_on_disk(db, job, *, kind="after", caption=None, content_type="image/jpeg",
                   write=True, shared=True):
    """A photo whose bytes really exist, wired the way link_job_photo wires them.

    ``shared`` defaults True here so the ownership/resolution tests below stay
    about what they are about. The default in the SYSTEM is the opposite —
    customer_visible is False on every new photo — and that is pinned in its
    own section rather than smuggled into a fixture.
    """
    doc_id = uuid4()
    stored = f"{doc_id.hex}-photo.jpg"
    db.add(Document(
        id=doc_id,
        filename=stored,
        original_name="photo.jpg",
        file_size=3,
        content_type=content_type,
        job_id=job.id,
    ))
    if write:
        # A REAL jpeg, not b"jpg": the pay page decodes and downscales the
        # bytes to inline them, so a placeholder would pass the portal path
        # and silently fail the one that matters.
        from PIL import Image

        img = Image.new("RGB", (40, 30), (90, 140, 90))
        img.save(db._upload_dir / stored, "JPEG", quality=70)
    photo = JobPhoto(
        id=uuid4(),
        company_id=TENANT,
        job_id=job.id,
        kind=kind,
        caption=caption,
        url=f"/api/documents/{doc_id}/download",
        filename=stored,
        mime_type=content_type,
        customer_visible=shared,
        uploaded_at=datetime.now(UTC),
    )
    db.add(photo)
    db.commit()
    db.refresh(photo)
    return photo


def _principal(customer_id: UUID) -> portal_router.PortalPrincipal:
    return portal_router.PortalPrincipal(user_id=uuid4(), customer_id=customer_id, role="customer")


# ---------------------------------------------------------------------------
# 1. The portal shows a customer their own job's photos — and nobody else's.
# ---------------------------------------------------------------------------


def test_customer_sees_their_own_job_photos(db) -> None:
    cust, job = _customer_with_job(db, "Alice")
    _photo_on_disk(db, job, kind="before")
    _photo_on_disk(db, job, kind="after", caption="New spring installed")

    rows = portal_router.portal_job_photos(job_id=job.id, principal=_principal(cust.id), db=db)
    assert len(rows) == 2
    assert {r["kind"] for r in rows} == {"before", "after"}
    # Portal-scoped url — never the staff /api/documents route, which needs a
    # staff Bearer token the customer will never have.
    assert all(r["url"].startswith(f"/portal/jobs/{job.id}/photos/") for r in rows)


def test_another_customers_job_is_not_reachable(db) -> None:
    """The whole exposure question in one test."""
    _alice, alice_job = _customer_with_job(db, "Alice")
    bob, _bob_job = _customer_with_job(db, "Bob")
    _photo_on_disk(db, alice_job)

    with pytest.raises(HTTPException) as exc:
        portal_router.portal_job_photos(job_id=alice_job.id, principal=_principal(bob.id), db=db)
    assert exc.value.status_code == 404

    photo = db.query(JobPhoto).first()
    with pytest.raises(HTTPException) as exc:
        portal_router.portal_job_photo_file(
            job_id=alice_job.id, photo_id=photo.id, principal=_principal(bob.id), db=db
        )
    assert exc.value.status_code == 404


def test_a_photo_from_a_different_job_is_refused(db) -> None:
    """Owning ONE job must not unlock a photo filed under another."""
    cust, job_one = _customer_with_job(db, "Alice")
    job_two = Job(customer_id=cust.id, title="second", lifecycle_stage="completed", company_id=TENANT)
    db.add(job_two)
    db.commit()
    db.refresh(job_two)
    other_photo = _photo_on_disk(db, job_two)

    with pytest.raises(HTTPException) as exc:
        portal_router.portal_job_photo_file(
            job_id=job_one.id, photo_id=other_photo.id, principal=_principal(cust.id), db=db
        )
    assert exc.value.status_code == 404


def test_unservable_photos_are_never_advertised(db) -> None:
    """A thumbnail the portal lists must actually load — otherwise the customer
    sees broken frames and assumes the company is sloppy."""
    cust, job = _customer_with_job(db, "Alice")
    good = _photo_on_disk(db, job)
    _photo_on_disk(db, job, write=False)                      # row exists, file doesn't
    _photo_on_disk(db, job, content_type="application/pdf")   # not an image
    db.add(JobPhoto(                                          # dead legacy url
        id=uuid4(), company_id=TENANT, job_id=job.id, kind="during",
        url="/mobile/uploads/job_photos/gone.jpg", uploaded_at=datetime.now(UTC),
    ))
    db.commit()

    rows = portal_router.portal_job_photos(job_id=job.id, principal=_principal(cust.id), db=db)
    assert [r["id"] for r in rows] == [str(good.id)]


def test_job_list_carries_a_photo_count(db) -> None:
    """So the portal can offer the photos without a request per row."""
    cust, job = _customer_with_job(db, "Alice")
    _photo_on_disk(db, job)
    _photo_on_disk(db, job)

    rows = portal_router.portal_jobs(principal=_principal(cust.id), db=db)
    assert len(rows) == 1
    assert rows[0]["photo_count"] == 2


# ---------------------------------------------------------------------------
# 2. The public pay page: attached photos only, non-draft only, token-scoped.
# ---------------------------------------------------------------------------


def _invoice(db, job, *, status="sent", token="pub-tok", attached=None):
    inv = Invoice(
        id=uuid4(),
        customer_id=job.customer_id,
        job_id=job.id,
        invoice_number=f"INV-{uuid4().hex[:6]}",
        subtotal=100, tax_amount=0, total=100, balance_due=100,
        status=status,
        public_token=token,
        attached_photo_ids=json.dumps([str(p.id) for p in attached]) if attached else None,
        company_id=TENANT,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def test_pay_page_shows_only_the_attached_photos(db) -> None:
    from gdx_dispatch.core.payments import _invoice_public_photos

    _cust, job = _customer_with_job(db, "Alice")
    attached = _photo_on_disk(db, job, kind="after", caption="Finished")
    _photo_on_disk(db, job, kind="before")  # on the job, NOT on the invoice
    inv = _invoice(db, job, attached=[attached])

    rows = _invoice_public_photos(inv, db)
    assert len(rows) == 1
    assert rows[0]["label"] == "Finished"
    # Inlined, not a URL to a new public endpoint.
    assert rows[0]["src"].startswith("data:image/jpeg;base64,")


def test_pay_page_adds_no_public_photo_route(db) -> None:
    """The pay page discloses the pictures WITHOUT widening the anonymous
    surface. If someone later adds an ungated image endpoint here, this fails
    before the authz baseline ratchet has to."""
    from gdx_dispatch.app import create_app

    paths = {getattr(r, "path", "") for r in create_app().routes}
    assert not any("/photos/" in p and p.startswith("/pay") for p in paths)


def test_pay_page_caps_how_many_it_inlines(db) -> None:
    """A payment page is not a gallery: twenty full photos inlined is megabytes
    on a phone. The PDF still carries the complete set."""
    from gdx_dispatch.core.payments import _PAY_PAGE_MAX_PHOTOS, _invoice_public_photos

    _cust, job = _customer_with_job(db, "Alice")
    photos = [_photo_on_disk(db, job) for _ in range(_PAY_PAGE_MAX_PHOTOS + 3)]
    inv = _invoice(db, job, attached=photos)

    assert len(_invoice_public_photos(inv, db)) == _PAY_PAGE_MAX_PHOTOS


def test_pay_page_shows_nothing_for_an_invoice_without_picks(db) -> None:
    from gdx_dispatch.core.payments import _invoice_public_photos

    _cust, job = _customer_with_job(db, "Alice")
    _photo_on_disk(db, job)
    inv = _invoice(db, job)  # photos exist on the job; none attached
    assert _invoice_public_photos(inv, db) == []


def test_an_unresolvable_photo_never_breaks_the_payment_page(db) -> None:
    """The customer is here to pay. A photo whose bytes are gone is skipped,
    not raised."""
    from gdx_dispatch.core.payments import _invoice_public_photos

    _cust, job = _customer_with_job(db, "Alice")
    good = _photo_on_disk(db, job)
    missing = _photo_on_disk(db, job, write=False)
    inv = _invoice(db, job, attached=[missing, good])

    rows = _invoice_public_photos(inv, db)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 3. The one resolver every surface shares.
# ---------------------------------------------------------------------------


def test_resolver_refuses_what_should_not_be_served(db) -> None:
    _cust, job = _customer_with_job(db, "Alice")

    assert resolve_photo_file(db, _photo_on_disk(db, job)) is not None
    assert resolve_photo_file(db, _photo_on_disk(db, job, write=False)) is None
    assert resolve_photo_file(db, _photo_on_disk(db, job, content_type="application/pdf")) is None

    legacy = JobPhoto(
        id=uuid4(), company_id=TENANT, job_id=job.id, kind="during",
        url="/mobile/uploads/job_photos/x.jpg", uploaded_at=datetime.now(UTC),
    )
    db.add(legacy)
    db.commit()
    assert resolve_photo_file(db, legacy) is None


def test_resolver_does_not_trust_the_filename_column(db) -> None:
    """job_photos.filename is unreliable — the office route stores the on-disk
    name, the documents route stores the ORIGINAL upload name. Resolution goes
    through the document, so a photo still serves when they differ."""
    _cust, job = _customer_with_job(db, "Alice")
    photo = _photo_on_disk(db, job)
    photo.filename = "whatever-the-tech-called-it.jpg"
    db.commit()

    resolved = resolve_photo_file(db, photo)
    assert resolved is not None
    path, content_type = resolved
    assert content_type == "image/jpeg"
    assert path.endswith(".jpg")


# ---------------------------------------------------------------------------
# 4. The share gate — off by default, and it means the same thing everywhere.
# ---------------------------------------------------------------------------


def test_a_new_photo_is_not_customer_visible(db) -> None:
    """The default IS the feature. If this ever flips, every photo a tech has
    ever taken becomes customer-facing the moment it uploads."""
    from gdx_dispatch.core.job_photos import link_job_photo

    _cust, job = _customer_with_job(db, "Alice")
    doc_id = uuid4()
    db.add(Document(id=doc_id, filename="x.jpg", original_name="x.jpg", file_size=1,
                    content_type="image/jpeg", job_id=job.id))
    db.commit()
    photo_id = link_job_photo(
        db, tenant_id=TENANT, job_id=str(job.id), document_id=str(doc_id),
        filename="x.jpg", content_type="image/jpeg", size_bytes=1, uploaded_by="tech",
    )
    db.commit()

    photo = db.get(JobPhoto, UUID(photo_id))
    assert photo.customer_visible is False


def test_an_unshared_photo_is_invisible_to_its_own_customer(db) -> None:
    cust, job = _customer_with_job(db, "Alice")
    shared = _photo_on_disk(db, job, kind="after")
    _photo_on_disk(db, job, kind="before", shared=False)

    rows = portal_router.portal_job_photos(job_id=job.id, principal=_principal(cust.id), db=db)
    assert [r["id"] for r in rows] == [str(shared.id)]


def test_the_byte_route_refuses_an_unshared_photo(db) -> None:
    """The gate belongs on the bytes too — a customer holding the old url must
    not keep loading a photo the office took back."""
    cust, job = _customer_with_job(db, "Alice")
    photo = _photo_on_disk(db, job)

    # Shared: serves.
    resp = portal_router.portal_job_photo_file(
        job_id=job.id, photo_id=photo.id, principal=_principal(cust.id), db=db
    )
    assert resp.path

    photo.customer_visible = False
    db.commit()
    with pytest.raises(HTTPException) as exc:
        portal_router.portal_job_photo_file(
            job_id=job.id, photo_id=photo.id, principal=_principal(cust.id), db=db
        )
    assert exc.value.status_code == 404


def test_the_photo_count_only_counts_shared_photos(db) -> None:
    """A badge that counted internal photos would tell the customer they
    exist — most of what keeping them internal is for."""
    cust, job = _customer_with_job(db, "Alice")
    _photo_on_disk(db, job)
    _photo_on_disk(db, job, shared=False)
    _photo_on_disk(db, job, shared=False)

    rows = portal_router.portal_jobs(principal=_principal(cust.id), db=db)
    assert rows[0]["photo_count"] == 1


def test_the_pay_page_respects_the_same_gate(db) -> None:
    """Un-sharing has to pull the photo off the bill too, or "internal" would
    mean internal everywhere except the one page the customer opens to pay."""
    from gdx_dispatch.core.payments import _invoice_public_photos

    _cust, job = _customer_with_job(db, "Alice")
    photo = _photo_on_disk(db, job)
    inv = _invoice(db, job, attached=[photo])
    assert len(_invoice_public_photos(inv, db)) == 1

    photo.customer_visible = False
    db.commit()
    assert _invoice_public_photos(inv, db) == []


def test_attaching_a_photo_to_an_invoice_shares_it(db) -> None:
    """One decision, not two: putting a photo on the customer's bill IS saying
    the customer may see it. Without this the office ticks a photo, watches it
    not print, and goes hunting for a second switch."""
    from gdx_dispatch.routers.invoices import _validated_attached_photo_ids

    _cust, job = _customer_with_job(db, "Alice")
    photo = _photo_on_disk(db, job, shared=False)
    assert photo.customer_visible is False

    _validated_attached_photo_ids(db, job_id=job.id, raw_ids=[str(photo.id)])
    db.commit()
    db.refresh(photo)
    assert photo.customer_visible is True


def test_the_pdf_renders_only_shared_photos(db, tmp_path) -> None:
    from gdx_dispatch.routers.pdf import _invoice_photos_for_pdf

    _cust, job = _customer_with_job(db, "Alice")
    shared = _photo_on_disk(db, job)
    hidden = _photo_on_disk(db, job, shared=False)
    inv = _invoice(db, job, attached=[shared, hidden])

    images = _invoice_photos_for_pdf(db, inv)
    assert len(images) == 1
