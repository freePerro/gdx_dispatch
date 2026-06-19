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

    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/api/jobs/{job_id}/photos" in paths
    assert "/api/jobs/{job_id}/signature" in paths
