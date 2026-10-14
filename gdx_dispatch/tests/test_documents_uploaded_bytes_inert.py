"""Uploaded bytes are served so they cannot run as the app (GDXA-972).

`GET /api/documents/{id}/download` served a file under the content type its
uploader claimed. An uploaded text/html or image/svg+xml came back as exactly
that, and the frontend opened it as a blob tab or framed it: a document of the
app's own origin, its script in reach of the session. CSP is Report-Only.
Every job photo is served by this same route (job_photos.url points at it),
and `as_photo` accepted any `image/*`, svg included.

Driven over HTTP through the real router: upload, then download, and read the
headers a browser would act on. No mocks of the response path.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Document, Job, JobPhoto
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"

HTML = b"<html><body><script>fetch('/api/users')</script></body></html>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(document.domain)"/>'
PNG = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63000100000005000100"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    from gdx_dispatch.core.auth import get_current_user as core_get_current_user
    from gdx_dispatch.core.modules import require_module
    from gdx_dispatch.routers import documents as documents_router
    from gdx_dispatch.routers.auth import get_current_user as routers_get_current_user

    caller = {"user_id": "office-1", "sub": "office-1", "tenant_id": TENANT, "role": "admin"}
    app = FastAPI()
    app.include_router(documents_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[core_get_current_user] = lambda: caller
    app.dependency_overrides[routers_get_current_user] = lambda: caller
    app.dependency_overrides[require_module("documents")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        request.state.user = caller
        return await call_next(request)

    cust = Customer(id=uuid4(), name="Acme", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    job = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Fix",
              description="", scheduled_at=datetime.now(UTC))
    db.add(job)
    db.commit()
    yield TestClient(app), db, job
    db.close()


def _upload(client, name: str, body: bytes, ctype: str, **form):
    return client.post("/api/documents", files={"file": (name, body, ctype)}, data=form)


def _download(client, doc_id: str):
    return client.get(f"/api/documents/{doc_id}/download")


@pytest.mark.parametrize(
    ("name", "body", "ctype"),
    [
        ("page.html", HTML, "text/html"),
        ("logo.svg", SVG, "image/svg+xml"),
        # The filename is no defence: the old preview framed anything ".pdf".
        ("invoice.pdf", HTML, "text/html; charset=utf-8"),
        ("data.xml", b"<?xml version='1.0'?><x/>", "application/xml"),
        ("page.xhtml", HTML, "application/xhtml+xml"),
        # audio/* and video/* are inline, but the uploader picks the type.
        ("clip.svg", SVG, "video/svg+xml"),
        # A browser takes the LAST type of a comma list: echoed back, this is
        # html in a video's clothing. Run in Chrome by the GDXA-972 audit.
        ("clip.mp4", HTML, "video/mp4, text/html"),
        ("song.mp3", HTML, "audio/mpeg,text/html"),
        ("door.png", HTML, "image/png, text/html"),
    ],
    ids=["html", "svg", "html-named-pdf", "xml", "xhtml", "video-svg-xml",
         "video-comma-html", "audio-comma-html", "png-comma-html"],
)
def test_markup_upload_is_served_as_an_inert_attachment(ctx, name, body, ctype):
    client, _db, _job = ctx
    up = _upload(client, name, body, ctype)
    assert up.status_code == 201, up.text

    resp = _download(client, up.json()["id"])
    assert resp.status_code == 200
    # Bytes intact: a document library that mangles files is no fix.
    assert resp.content == body
    # Not the uploader's type: a blob built from this response is octet-stream,
    # so even a tab opened on it downloads instead of rendering.
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith("attachment")
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    ("name", "body", "ctype"),
    [
        ("door.png", PNG, "image/png"),
        ("spec.pdf", b"%PDF-1.4\n%%EOF", "application/pdf"),
        ("voicemail.mp3", b"ID3", "audio/mpeg"),
    ],
    ids=["png", "pdf", "mp3"],
)
def test_inert_types_keep_their_type_and_render_inline(ctx, name, body, ctype):
    """The preview pane, image tags and media players still work."""
    client, _db, _job = ctx
    up = _upload(client, name, body, ctype)
    assert up.status_code == 201, up.text

    resp = _download(client, up.json()["id"])
    assert resp.status_code == 200
    assert resp.content == body
    assert resp.headers["content-type"] == ctype
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_plain_text_keeps_its_type_as_an_attachment(ctx):
    client, _db, _job = ctx
    up = _upload(client, "notes.txt", b"<b>not markup</b>", "text/plain")
    resp = _download(client, up.json()["id"])
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-disposition"].startswith("attachment")
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    ("name", "body", "ctype"),
    [
        ("logo.svg", SVG, "image/svg+xml"),
        ("page.html", HTML, "text/html"),
        ("door.png", HTML, "image/png, text/html"),
    ],
    ids=["svg", "html", "png-comma-html"],
)
def test_markup_is_refused_as_a_photo_and_writes_nothing(ctx, name, body, ctype):
    client, db, job = ctx
    resp = _upload(client, name, body, ctype, job_id=str(job.id), as_photo="true")
    assert resp.status_code == 415
    assert db.query(Document).count() == 0
    assert db.query(JobPhoto).count() == 0


@pytest.mark.parametrize("ctype", ["image/png", "image/heic", "IMAGE/JPEG"])
def test_raster_photo_is_still_accepted(ctx, ctype):
    """A 415 is permanent to the phone's photo queue: a camera format refused
    here is a photo lost, so HEIC from an iPhone library pick stays in."""
    client, db, job = ctx
    resp = _upload(client, "door.img", PNG, ctype, job_id=str(job.id), as_photo="true")
    assert resp.status_code == 201, resp.text
    photo = db.query(JobPhoto).one()
    assert photo.url == f"/api/documents/{resp.json()['id']}/download"
    # And it comes back as a picture: a photo the upload takes and the strip
    # cannot show is a photo lost too.
    served = client.get(photo.url)
    assert served.headers["content-type"] == ctype.lower()
    assert served.headers["content-disposition"].startswith("inline")


@pytest.mark.parametrize(
    ("stored", "served"),
    [("application/x-pdf", "application/pdf"), ("image/x-png", "image/png")],
)
def test_legacy_spellings_still_preview(ctx, stored, served):
    """Old client spellings of a safe type keep their preview, under the
    canonical type, rather than falling to a download."""
    client, _db, _job = ctx
    up = _upload(client, "f.bin", b"bytes", stored)
    resp = _download(client, up.json()["id"])
    assert resp.headers["content-type"] == served
    assert resp.headers["content-disposition"].startswith("inline")
