"""Picked email attachments: one access rule for the picker and the send.

Real SQLite, real permission resolution (``_load_user_permissions`` falls back
to the built-in role when no assignment row exists), real files on disk. The
contract pinned here:

- a technician reaches their own jobs' photos and files, those jobs'
  customers' estimates and documents, and invoices on their own jobs — and
  nothing of another tech's (404, the non-disclosure answer);
- an office role reaches everything;
- what ``list_attachment_sources`` offers is exactly what
  ``resolve_attachment_refs`` accepts;
- a stored filename can never read outside the upload root;
- the total is capped (413) and the whole send stops — never a partial set.
"""
from __future__ import annotations

import base64
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core import email_attachments as ea
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.email_attachments import AttachmentRef
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import (
    Customer,
    Document,
    Invoice,
    Job,
    JobPhoto,
    Technician,
)
from gdx_dispatch.modules.proposals import models as proposal_models  # noqa: F401
from gdx_dispatch.modules.proposals.models import Estimate

TENANT = "tenant-a"
TECH_A = {"user_id": "user-a", "role": "technician", "tenant_id": TENANT}
TECH_B = {"user_id": "user-b", "role": "technician", "tenant_id": TENANT}
OWNER = {"user_id": "user-o", "role": "owner", "tenant_id": TENANT}


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    return req


@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("UPLOAD_DIR", str(root))
    return root


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'attach.sqlite3'}", connect_args={"check_same_thread": False}
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def fake_pdfs(monkeypatch):
    """The renderers are exercised by their own suites; here the bytes only
    have to be identifiable. test_pdf_refs_render_the_real_documents runs
    the real estimate and invoice renderers once each."""
    monkeypatch.setattr(ea, "_estimate_pdf", lambda db, est, tid: b"%PDF est " + est.estimate_number.encode())
    monkeypatch.setattr(ea, "_invoice_pdf", lambda db, inv: b"%PDF inv " + inv.invoice_number.encode())


def _file(upload_dir, name: str, data: bytes) -> str:
    (upload_dir / name).write_bytes(data)
    return name


def _seed(db, upload_dir) -> dict:
    """Tech A owns job A (customer A); tech B owns job B (customer B)."""
    ids: dict = {}
    for key, user in (("a", "user-a"), ("b", "user-b")):
        tech = Technician(id=f"tech-{key}", company_id=TENANT, user_id=user, name=f"Tech {key}")
        cust = Customer(name=f"Customer {key.upper()}", company_id=TENANT)
        db.add_all([tech, cust])
        db.flush()
        job = Job(
            title=f"Job {key}", company_id=TENANT, customer_id=cust.id,
            assigned_to=tech.id, dispatch_status="assigned", job_number=f"J-{key}",
        )
        db.add(job)
        db.flush()
        est1 = Estimate(customer_id=cust.id, job_id=job.id, estimate_number=f"E-{key}1",
                        total=Decimal("100"), company_id=TENANT, label="Opener", public_token=uuid4().hex)
        # A second estimate for the same customer, on no job — the
        # "several estimates for one customer" case.
        est2 = Estimate(customer_id=cust.id, estimate_number=f"E-{key}2",
                        total=Decimal("250"), company_id=TENANT, label="Door",
                        public_token=uuid4().hex)
        inv = Invoice(job_id=job.id, customer_id=cust.id, invoice_number=f"I-{key}",
                      company_id=TENANT, public_token=uuid4().hex)
        photo_doc = Document(filename=_file(upload_dir, f"photo-{key}.jpg", b"JPEG" + key.encode()),
                             original_name=f"photo-{key}.jpg", content_type="image/jpeg",
                             job_id=job.id)
        job_doc = Document(filename=_file(upload_dir, f"spec-{key}.pdf", b"SPEC" + key.encode()),
                           original_name=f"spec-{key}.pdf", content_type="application/pdf",
                           job_id=job.id)
        cust_doc = Document(filename=_file(upload_dir, f"w9-{key}.pdf", b"W9" + key.encode()),
                            original_name=f"w9-{key}.pdf", content_type="application/pdf",
                            customer_id=cust.id)
        db.add_all([est1, est2, inv, photo_doc, job_doc, cust_doc])
        db.flush()
        photo = JobPhoto(company_id=TENANT, job_id=job.id, kind="after",
                         url=f"/api/documents/{photo_doc.id}/download",
                         filename=f"photo-{key}.jpg", caption=f"Photo {key}",
                         customer_visible=(key == "a"))
        db.add(photo)
        db.flush()
        ids[key] = {
            "job": job.id, "customer": cust.id, "est1": est1.id, "est2": est2.id,
            "invoice": inv.id, "photo": photo.id, "photo_doc": photo_doc.id,
            "job_doc": job_doc.id, "cust_doc": cust_doc.id,
        }
    loose = Document(filename=_file(upload_dir, "loose.pdf", b"LOOSE"),
                     original_name="loose.pdf", content_type="application/pdf")
    db.add(loose)
    db.commit()
    ids["loose"] = loose.id
    return ids


def _resolve(db, user, refs, **kw):
    return ea.resolve_attachment_refs(db, _request(), user, TENANT, refs, **kw)


def _ref(kind, id_):
    return AttachmentRef(kind=kind, id=str(id_))


# ── the real renderers ──────────────────────────────────────────────


def test_pdf_refs_render_the_real_documents(db, upload_dir):
    """No fakes: the estimate and invoice picks come back as real PDFs from
    the same renderers /pdf and /send use."""
    ids = _seed(db, upload_dir)["a"]
    out = _resolve(db, TECH_A, [_ref("estimate_pdf", ids["est1"]), _ref("invoice_pdf", ids["invoice"])])
    assert [o["name"] for o in out] == ["estimate-E-a1.pdf", "invoice-I-a.pdf"]
    for o in out:
        data = base64.b64decode(o["content_base64"])
        assert data.startswith(b"%PDF-"), o["name"]
        assert o["content_type"] == "application/pdf"
        assert o["size_bytes"] == len(data) > 1000


# ── reach ───────────────────────────────────────────────────────────────


def test_tech_attaches_everything_on_their_own_job(db, upload_dir, fake_pdfs):
    ids = _seed(db, upload_dir)["a"]
    out = _resolve(db, TECH_A, [
        _ref("job_photo", ids["photo"]),
        _ref("document", ids["job_doc"]),
        _ref("document", ids["cust_doc"]),
        _ref("estimate_pdf", ids["est1"]),
        _ref("estimate_pdf", ids["est2"]),
        _ref("invoice_pdf", ids["invoice"]),
    ])
    by_name = {o["name"]: base64.b64decode(o["content_base64"]) for o in out}
    assert by_name == {
        "photo-a.jpg": b"JPEGa",
        "spec-a.pdf": b"SPECa",
        "w9-a.pdf": b"W9a",
        "estimate-E-a1.pdf": b"%PDF est E-a1",
        "estimate-E-a2.pdf": b"%PDF est E-a2",
        "invoice-I-a.pdf": b"%PDF inv I-a",
    }
    assert [o["kind"] for o in out] == [
        "job_photo", "document", "document", "estimate_pdf", "estimate_pdf", "invoice_pdf",
    ]


@pytest.mark.parametrize("key", ["photo", "job_doc", "cust_doc", "est1", "est2", "invoice"])
def test_tech_cannot_reach_another_techs_records(db, upload_dir, fake_pdfs, key):
    other = _seed(db, upload_dir)["b"]
    kind = {"photo": "job_photo", "job_doc": "document", "cust_doc": "document",
            "est1": "estimate_pdf", "est2": "estimate_pdf", "invoice": "invoice_pdf"}[key]
    with pytest.raises(HTTPException) as exc:
        _resolve(db, TECH_A, [_ref(kind, other[key])])
    assert exc.value.status_code == 404


def test_tech_cannot_reach_a_document_attached_to_nothing(db, upload_dir, fake_pdfs):
    ids = _seed(db, upload_dir)
    with pytest.raises(HTTPException) as exc:
        _resolve(db, TECH_A, [_ref("document", ids["loose"])])
    assert exc.value.status_code == 404


def test_owner_reaches_every_record(db, upload_dir, fake_pdfs):
    ids = _seed(db, upload_dir)
    refs = [_ref("document", ids["loose"])]
    for key in ("a", "b"):
        refs += [_ref("job_photo", ids[key]["photo"]), _ref("estimate_pdf", ids[key]["est2"]),
                 _ref("invoice_pdf", ids[key]["invoice"]), _ref("document", ids[key]["cust_doc"])]
    assert len(_resolve(db, OWNER, refs)) == 9


def test_one_unreachable_pick_stops_the_whole_set(db, upload_dir, fake_pdfs):
    ids = _seed(db, upload_dir)
    with pytest.raises(HTTPException) as exc:
        _resolve(db, TECH_A, [_ref("estimate_pdf", ids["a"]["est1"]),
                              _ref("estimate_pdf", ids["b"]["est1"])])
    assert exc.value.status_code == 404


def test_deleted_records_are_gone(db, upload_dir, fake_pdfs):
    from gdx_dispatch.core.audit import utcnow

    ids = _seed(db, upload_dir)["a"]
    est = db.get(Estimate, ids["est2"])
    est.deleted_at = utcnow()
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _resolve(db, OWNER, [_ref("estimate_pdf", ids["est2"])])
    assert exc.value.status_code == 404


def test_missing_file_on_disk_is_404_not_an_empty_attachment(db, upload_dir, fake_pdfs):
    ids = _seed(db, upload_dir)["a"]
    (upload_dir / "spec-a.pdf").unlink()
    with pytest.raises(HTTPException) as exc:
        _resolve(db, TECH_A, [_ref("document", ids["job_doc"])])
    assert exc.value.status_code == 404


@pytest.mark.parametrize("stored", ["../secret.txt", "/etc/hostname", "sub/../../secret.txt"])
def test_stored_filename_cannot_leave_the_upload_root(db, upload_dir, fake_pdfs, stored):
    (upload_dir.parent / "secret.txt").write_bytes(b"TOP SECRET")
    ids = _seed(db, upload_dir)["a"]
    doc = db.get(Document, ids["job_doc"])
    doc.filename = stored
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _resolve(db, OWNER, [_ref("document", ids["job_doc"])])
    assert exc.value.status_code == 404


def test_estimate_attachment_document_reads_from_the_estimate_dir(db, upload_dir, fake_pdfs):
    ids = _seed(db, upload_dir)["a"]
    est_dir = upload_dir / TENANT / "estimate" / str(ids["est1"])
    est_dir.mkdir(parents=True)
    (est_dir / "brochure.pdf").write_bytes(b"BROCHURE")
    doc = Document(filename="brochure.pdf", original_name="Brochure.pdf",
                   content_type="application/pdf", estimate_id=ids["est1"])
    db.add(doc)
    db.commit()
    out = _resolve(db, TECH_A, [_ref("document", doc.id)])
    assert base64.b64decode(out[0]["content_base64"]) == b"BROCHURE"


# ── uploads and the cap ─────────────────────────────────────────────────


def test_device_upload_passes_through_with_a_safe_name(db, upload_dir):
    data = b"\x89PNG device photo"
    out = _resolve(db, TECH_A, [AttachmentRef(
        kind="upload", name="../../garage door.png", content_type="image/png",
        content_base64=base64.b64encode(data).decode(),
    )])
    assert out[0]["name"] == "garage door.png"
    assert base64.b64decode(out[0]["content_base64"]) == data
    assert out[0]["size_bytes"] == len(data)


def test_upload_with_bad_base64_is_422(db, upload_dir):
    with pytest.raises(HTTPException) as exc:
        _resolve(db, TECH_A, [AttachmentRef(kind="upload", name="x.png", content_base64="!!not b64!!")])
    assert exc.value.status_code == 422


def test_over_the_total_cap_is_413_naming_the_total(db, upload_dir):
    chunk = base64.b64encode(b"x" * 600).decode()
    refs = [AttachmentRef(kind="upload", name=f"{i}.bin", content_base64=chunk) for i in range(2)]
    with pytest.raises(HTTPException) as exc:
        _resolve(db, TECH_A, refs, budget_bytes=1000)
    assert exc.value.status_code == 413
    assert "Remove a file" in exc.value.detail


def test_ref_shape_is_validated():
    with pytest.raises(ValueError):
        AttachmentRef(kind="estimate_pdf")
    with pytest.raises(ValueError):
        AttachmentRef(kind="upload", name="x.png")
    with pytest.raises(ValueError):
        AttachmentRef(kind="upload", name="x.png", content_base64="eA==", extra="no")


# ── the picker offers exactly what the send accepts ─────────────────────


def _sources(db, user, **kw):
    return ea.list_attachment_sources(db, _request(), user, TENANT, **kw)


def test_sources_for_a_tech_on_their_job(db, upload_dir):
    ids = _seed(db, upload_dir)["a"]
    src = _sources(db, TECH_A, job_id=str(ids["job"]))
    assert src["customer"]["id"] == str(ids["customer"])
    assert [p["id"] for p in src["job_photos"]] == [str(ids["photo"])]
    assert src["job_photos"][0]["customer_visible"] is True
    # The photo's own Document is not listed a second time as a job file.
    assert [d["id"] for d in src["job_documents"]] == [str(ids["job_doc"])]
    assert [d["id"] for d in src["customer_documents"]] == [str(ids["cust_doc"])]
    assert {e["number"] for e in src["estimates"]} == {"E-a1", "E-a2"}
    assert [i["number"] for i in src["invoices"]] == ["I-a"]
    assert src["max_total_bytes"] == ea.MAX_EMAIL_ATTACHMENTS_BYTES


def test_sources_hide_another_techs_job_and_customer(db, upload_dir):
    ids = _seed(db, upload_dir)["b"]
    by_job = _sources(db, TECH_A, job_id=str(ids["job"]))
    by_customer = _sources(db, TECH_A, customer_id=str(ids["customer"]))
    for src in (by_job, by_customer):
        assert src["job_photos"] == [] and src["job_documents"] == []
        assert src["estimates"] == [] and src["invoices"] == []
        assert src["customer_documents"] == []


def test_every_listed_source_resolves_for_the_same_caller(db, upload_dir, fake_pdfs):
    """The falsifier for "one rule": anything the picker lists that the send
    then refuses would be a dead control."""
    ids = _seed(db, upload_dir)
    for user, key in ((TECH_A, "a"), (TECH_B, "b"), (OWNER, "a")):
        src = _sources(db, user, job_id=str(ids[key]["job"]))
        refs = (
            [_ref("job_photo", p["id"]) for p in src["job_photos"]]
            + [_ref("document", d["id"]) for d in src["job_documents"] + src["customer_documents"]]
            + [_ref("estimate_pdf", e["id"]) for e in src["estimates"]]
            + [_ref("invoice_pdf", i["id"]) for i in src["invoices"]]
        )
        assert len(refs) == 6
        assert len(_resolve(db, user, refs)) == 6


def test_sources_route_is_registered():
    from gdx_dispatch.routers import email_attachments as route_mod

    paths = {r.path for r in route_mod.router.routes}
    assert "/api/email-attachments/sources" in paths


# ── photos leave without their location ─────────────────────────────


def _jpeg_with_gps(size=(64, 32), orientation=6) -> bytes:
    import io

    from PIL import Image

    img = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    exif[0x0112] = orientation  # rotate 90° CW to display
    exif[0x8825] = {1: "N", 2: (44.0, 58.0, 30.0), 3: "W", 4: (93.0, 15.0, 0.0)}  # GPS IFD
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _opened(item):
    import io

    from PIL import Image

    return Image.open(io.BytesIO(base64.b64decode(item["content_base64"])))


def test_device_photo_is_turned_upright_and_loses_its_gps(db, upload_dir):
    raw = _jpeg_with_gps()
    assert _opened({"content_base64": base64.b64encode(raw).decode()}).getexif().get_ifd(0x8825)
    (item,) = _resolve(db, TECH_A, [AttachmentRef(kind="upload", name="driveway.jpg",
                                                  content_type="image/jpeg",
                                                  content_base64=base64.b64encode(raw).decode())])
    img = _opened(item)
    assert img.size == (32, 64)  # the orientation tag was applied to the pixels
    exif = img.getexif()
    assert not exif.get_ifd(0x8825) and 0x0112 not in exif
    assert item["name"] == "driveway.jpg" and item["content_type"] == "image/jpeg"
    assert item["size_bytes"] == len(base64.b64decode(item["content_base64"]))


def test_stored_job_photo_loses_its_gps_too(db, upload_dir):
    ids = _seed(db, upload_dir)["a"]
    (upload_dir / "photo-a.jpg").write_bytes(_jpeg_with_gps())
    (item,) = _resolve(db, TECH_A, [_ref("job_photo", ids["photo"])])
    assert not _opened(item).getexif().get_ifd(0x8825)


def test_webp_goes_out_as_a_jpeg_and_non_photos_are_untouched(db, upload_dir):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="WEBP")
    pdf = b"%PDF-1.4 not an image"
    out = _resolve(db, TECH_A, [
        AttachmentRef(kind="upload", name="shot.webp", content_type="image/webp",
                      content_base64=base64.b64encode(buf.getvalue()).decode()),
        AttachmentRef(kind="upload", name="spec.pdf", content_type="application/pdf",
                      content_base64=base64.b64encode(pdf).decode()),
        # Says image, is not one: sent as given rather than refused.
        AttachmentRef(kind="upload", name="odd.jpg", content_type="image/jpeg",
                      content_base64=base64.b64encode(b"nope").decode()),
    ])
    assert (out[0]["name"], out[0]["content_type"]) == ("shot.jpg", "image/jpeg")
    assert _opened(out[0]).format == "JPEG"
    assert base64.b64decode(out[1]["content_base64"]) == pdf
    assert base64.b64decode(out[2]["content_base64"]) == b"nope"


def test_a_photo_too_large_to_clean_is_refused_not_sent_with_its_gps(db, upload_dir):
    import io

    from PIL import Image

    buf = io.BytesIO()
    # 1-bit and flat: a few KB on the wire, 81MP decoded — over the ceiling.
    Image.new("1", (9000, 9000)).save(buf, format="PNG")
    with pytest.raises(HTTPException) as exc:
        _resolve(db, TECH_A, [AttachmentRef(
            kind="upload", name="huge.png", content_type="image/png",
            content_base64=base64.b64encode(buf.getvalue()).decode())])
    assert exc.value.status_code == 422
    assert "huge.png" in exc.value.detail


def test_the_set_is_refused_at_the_first_file_over_the_budget(db, upload_dir):
    # The bad base64 after the over-budget file is never reached: nothing
    # past the cap is loaded.
    chunk = base64.b64encode(b"x" * 1200).decode()
    with pytest.raises(HTTPException) as exc:
        _resolve(db, TECH_A, [
            AttachmentRef(kind="upload", name="big.bin", content_base64=chunk),
            AttachmentRef(kind="upload", name="x.png", content_base64="!!not b64!!"),
        ], budget_bytes=1000)
    assert exc.value.status_code == 413


def test_a_photo_without_a_caption_is_listed_by_the_name_it_was_uploaded_as():
    # Stored as <uuid4 hex>-<name> (routers/uploads.py); the picker shows <name>.
    assert ea._shown_name("730fd6310874403fb55bffa212a4a8b4-door.jpg") == "door.jpg"
    assert ea._shown_name("door.jpg") == "door.jpg"
    assert ea._shown_name(None) is None


@pytest.mark.parametrize("size,cut", [((64, 32), 200), ((400, 300), 200), ((400, 300), 2000)])
def test_a_damaged_photo_never_leaves_with_its_gps(db, upload_dir, size, cut):
    # Cut short, Pillow either cannot open it, cannot finish decoding it, or
    # fills in the rest. Whichever: what leaves carries no EXIF, or nothing
    # leaves.
    raw = _jpeg_with_gps(size=size)[:-cut]
    assert ea._EXIF_MARK in raw
    try:
        (item,) = _resolve(db, TECH_A, [AttachmentRef(
            kind="upload", name="cut.jpg", content_type="image/jpeg",
            content_base64=base64.b64encode(raw).decode())])
    except HTTPException as exc:
        assert exc.status_code == 422 and "cut.jpg" in exc.detail
    else:
        assert ea._EXIF_MARK not in base64.b64decode(item["content_base64"])


def test_a_photo_pillow_cannot_open_is_refused_when_it_carries_exif(db, upload_dir):
    raw = _jpeg_with_gps(size=(64, 32))[:-200]
    with pytest.raises(HTTPException) as exc:
        ea._clean_photo("cut.jpg", "image/jpeg", raw)
    assert exc.value.status_code == 422


def test_the_picker_does_not_offer_what_the_send_cannot_load(db, upload_dir, fake_pdfs):
    """A legacy /mobile/uploads photo, a photo whose file is gone and a
    document whose file is gone would each 404 the whole email at send."""
    ids = _seed(db, upload_dir)["a"]
    db.add(JobPhoto(company_id=TENANT, job_id=ids["job"], kind="after",
                    url="/mobile/uploads/legacy.jpg", filename="legacy.jpg", caption="legacy"))
    gone = Document(filename="gone.jpg", original_name="gone.jpg", content_type="image/jpeg",
                    job_id=ids["job"])
    db.add(gone)
    db.flush()
    db.add(JobPhoto(company_id=TENANT, job_id=ids["job"], kind="after",
                    url=f"/api/documents/{gone.id}/download", filename="gone.jpg", caption="gone"))
    db.add(Document(filename="lost.pdf", original_name="lost.pdf", content_type="application/pdf",
                    job_id=ids["job"]))
    db.add(Document(filename="lost-w9.pdf", original_name="lost-w9.pdf",
                    content_type="application/pdf", customer_id=ids["customer"]))
    db.commit()

    src = _sources(db, OWNER, job_id=str(ids["job"]))
    assert [p["name"] for p in src["job_photos"]] == ["Photo a"]
    assert [d["file_name"] for d in src["job_documents"]] == ["spec-a.pdf"]
    assert [d["file_name"] for d in src["customer_documents"]] == ["w9-a.pdf"]


def _gps_image(fmt: str) -> bytes:
    import io

    from PIL import Image

    img = Image.new("RGB", (16, 16), (10, 120, 10))
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (44.0, 58.0, 30.0), 3: "W", 4: (93.0, 15.0, 0.0)}
    buf = io.BytesIO()
    # As bytes: Pillow's TIFF writer cannot take the nested GPS dict itself.
    img.save(buf, format=fmt, exif=exif.tobytes())
    return buf.getvalue()


def _upload(name, content_type, raw):
    return AttachmentRef(kind="upload", name=name, content_type=content_type,
                         content_base64=base64.b64encode(raw).decode())


@pytest.mark.parametrize("name,content_type", [
    ("door", "application/octet-stream"),  # what the picker sends when the browser gives no type
    ("door.pdf", "application/pdf"),  # misnamed
])
def test_a_jpeg_loses_its_gps_whatever_it_is_called(db, upload_dir, name, content_type):
    (item,) = _resolve(db, TECH_A, [_upload(name, content_type, _gps_image("JPEG"))])
    assert item["content_type"] == "image/jpeg" and item["name"].endswith(".jpg")
    assert not _opened(item).getexif().get_ifd(0x8825)


def test_a_tiff_with_gps_is_refused(db, upload_dir):
    with pytest.raises(HTTPException) as exc:
        _resolve(db, TECH_A, [_upload("door.tif", "image/tiff", _gps_image("TIFF"))])
    assert exc.value.status_code == 422 and "location" in exc.value.detail


def test_a_tiff_without_gps_goes_as_stored(db, upload_dir):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="TIFF")
    (item,) = _resolve(db, TECH_A, [_upload("scan.tif", "image/tiff", buf.getvalue())])
    assert base64.b64decode(item["content_base64"]) == buf.getvalue()


def test_an_avif_photo_goes_out_as_a_jpeg_without_its_gps(db, upload_dir):
    from PIL import features

    if not features.check("avif"):
        pytest.skip("this Pillow build has no AVIF codec")
    (item,) = _resolve(db, TECH_A, [_upload("a.avif", "image/avif", _gps_image("AVIF"))])
    assert (item["name"], item["content_type"]) == ("a.jpg", "image/jpeg")
    assert not _opened(item).getexif().get_ifd(0x8825)


def test_a_pdf_with_a_photo_inside_is_the_senders_document_sent_as_written(db, upload_dir):
    pdf = b"%PDF-1.4\n" + _gps_image("JPEG") + b"\n%%EOF"
    assert ea._EXIF_MARK in pdf
    (item,) = _resolve(db, TECH_A, [_upload("report.pdf", "application/pdf", pdf)])
    assert base64.b64decode(item["content_base64"]) == pdf
