from __future__ import annotations

import base64
import io
import uuid
from collections.abc import Generator

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers
from starlette.requests import Request

from gdx_dispatch.routers import uploads as uploads_router


def _create_schema(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                file_size INTEGER,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                uploaded_by TEXT,
                created_at TEXT NOT NULL,
                uploaded_at TEXT,
                deleted_at TEXT
            )
            """
        )
    )
    db.commit()


@pytest.fixture()
def tenant_db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # job_photos comes from the real ORM metadata rather than a hand-written
    # CREATE — the photo record is half of what this route produces, and the
    # table was simply absent here, which is why "the upload works" could stay
    # true while the photo went nowhere.
    from gdx_dispatch.models.tenant_models import JobPhoto

    JobPhoto.__table__.create(bind=engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    _create_schema(db)
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _upload_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))


def _request(tenant_id: str) -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": tenant_id}
    return req


def _file(name: str, content: bytes, content_type: str) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content), headers=Headers({"content-type": content_type}))


def test_upload_photo_success(tenant_db_session):
    out = uploads_router.upload_job_photo(
        job_id=str(uuid.uuid4()),
        request=_request("tenant-a"),
        file=_file("kitchen.jpg", b"jpeg-bytes", "image/jpeg"),
        user={"tenant_id": "tenant-a", "user_id": "user-a"},
        db=tenant_db_session,
    )

    assert out.content_type == "image/jpeg"
    assert out.size_bytes == len(b"jpeg-bytes")
    assert out.original_name == "kitchen.jpg"

    row = tenant_db_session.execute(text("SELECT * FROM documents WHERE id = :id"), {"id": out.id}).mappings().first()
    assert row is not None
    assert row["entity_type"] == "job_photo"
    assert row["deleted_at"] is None


def test_upload_photo_creates_the_photo_record(tenant_db_session):
    """Storing the bytes is not the job — the photo has to be findable.

    A document holds the bytes; a JobPhoto is the photo record, and every photo
    surface (the Photos page, the job's strip, the mobile job screen) reads
    job_photos. This route stored the file and never created that record, so
    the upload "succeeded" and the photo appeared nowhere — job_photos has 0
    rows in production despite a working UI. The old test asserted only the
    documents row, which is exactly why nobody noticed.

    The Photos page's second step (a JSON POST that was meant to create this
    record) has 422'd since it shipped: uploads.py claims the same path with a
    multipart signature and is included first, so the JSON hit a handler
    demanding a file.
    """
    job_id = str(uuid.uuid4())
    out = uploads_router.upload_job_photo(
        job_id=job_id,
        request=_request("tenant-a"),
        file=_file("door.jpg", b"jpeg-bytes", "image/jpeg"),
        kind="before",
        caption="Spring snapped",
        user={"tenant_id": "tenant-a", "user_id": "user-a"},
        db=tenant_db_session,
    )

    # Via the ORM: JobPhoto.job_id is a Uuid column, and SQLite stores that
    # 32-hex while Postgres stores it dashed — a raw `WHERE job_id = '<dashed>'`
    # matches nothing here and everything in prod.
    from gdx_dispatch.models.tenant_models import JobPhoto

    row = tenant_db_session.query(JobPhoto).filter(
        JobPhoto.job_id == uuid.UUID(job_id)
    ).first()
    assert row is not None, "bytes stored but the photo record was never created"
    # Points at the document's download route — the same url the Photos page
    # used to build by hand in its (broken) second step.
    assert row.url == f"/api/documents/{out.id}/download"
    assert row.kind == "before"
    assert row.caption == "Spring snapped"


def test_upload_photo_defaults_the_slot_when_the_tech_does_not_pick(tenant_db_session):
    """A tech shooting a door in a hurry never has to choose a slot."""
    job_id = str(uuid.uuid4())
    uploads_router.upload_job_photo(
        job_id=job_id,
        request=_request("tenant-a"),
        file=_file("door.jpg", b"jpeg-bytes", "image/jpeg"),
        user={"tenant_id": "tenant-a", "user_id": "user-a"},
        db=tenant_db_session,
    )
    from gdx_dispatch.models.tenant_models import JobPhoto

    row = tenant_db_session.query(JobPhoto).filter(
        JobPhoto.job_id == uuid.UUID(job_id)
    ).first()
    assert row is not None
    assert row.kind == "during"


def test_upload_too_large_rejected(tenant_db_session):
    with pytest.raises(HTTPException) as exc:
        uploads_router.upload_job_photo(
            job_id=str(uuid.uuid4()),
            request=_request("tenant-a"),
            file=_file("big.jpg", b"x" * (10 * 1024 * 1024 + 1), "image/jpeg"),
            user={"tenant_id": "tenant-a", "user_id": "user-a"},
            db=tenant_db_session,
        )
    assert exc.value.status_code == 413


def test_upload_wrong_mime_rejected(tenant_db_session):
    with pytest.raises(HTTPException) as exc:
        uploads_router.upload_job_photo(
            job_id=str(uuid.uuid4()),
            request=_request("tenant-a"),
            file=_file("bad.gif", b"gif", "image/gif"),
            user={"tenant_id": "tenant-a", "user_id": "user-a"},
            db=tenant_db_session,
        )
    assert exc.value.status_code == 415


# test_download_returns_file / test_delete_soft_deletes removed 2026-04-24
# (Phase B1). Their target functions (uploads_router.upload_document /
# download_document / delete_document) were deleted — canonical routes
# live in gdx_dispatch/routers/documents.py and are covered by test_documents.py.


def test_requires_auth():
    guarded_paths = set()
    for route in uploads_router.router.routes:
        if not hasattr(route, "dependant"):
            continue
        for dep in route.dependant.dependencies:
            if dep.call is uploads_router.get_current_user:
                guarded_paths.add(route.path)
                break

    assert "/api/jobs/{job_id}/photos" in guarded_paths
    assert "/api/jobs/{job_id}/signature" in guarded_paths


# test_audit_logged_on_upload + test_tenant_isolation removed 2026-04-24
# (Phase B1) — targeted deleted functions. Canonical routes in
# gdx_dispatch/routers/documents.py have their own tenant-isolation and audit
# coverage in test_documents.py.


def test_signature_upload_success(tenant_db_session):
    signature_bytes = b"\x89PNG\r\n\x1a\n--png--"
    payload = uploads_router.SignatureUploadIn(signature=base64.b64encode(signature_bytes).decode())

    out = uploads_router.upload_customer_signature(
        job_id=str(uuid.uuid4()),
        payload=payload,
        request=_request("tenant-a"),
        user={"tenant_id": "tenant-a", "user_id": "user-a"},
        db=tenant_db_session,
    )
    assert out.content_type == "image/png"

    row = tenant_db_session.execute(text("SELECT * FROM documents WHERE id = :id"), {"id": out.id}).mappings().first()
    assert row is not None
    assert row["entity_type"] == "job_signature"


def test_signature_invalid_rejected(tenant_db_session):
    with pytest.raises(HTTPException) as exc:
        uploads_router.upload_customer_signature(
            job_id=str(uuid.uuid4()),
            payload=uploads_router.SignatureUploadIn(signature="not-base64"),
            request=_request("tenant-a"),
            user={"tenant_id": "tenant-a", "user_id": "user-a"},
            db=tenant_db_session,
        )
    assert exc.value.status_code == 400


# test_document_upload_requires_exactly_one_link + test_filename_is_sanitized
# removed 2026-04-24 (Phase B1). Both targeted uploads_router.upload_document
# (deleted). Filename sanitization + link validation are enforced by the
# canonical POST /api/documents and covered in test_documents.py.


def test_uploads_router_registered_in_main_app():
    from gdx_dispatch.app import create_app
    from gdx_dispatch.tests.conftest import app_route_paths

    app = create_app()
    paths = app_route_paths(app)
    assert "/api/jobs/{job_id}/photos" in paths
    assert "/api/jobs/{job_id}/signature" in paths
