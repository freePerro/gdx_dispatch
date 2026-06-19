from __future__ import annotations

import io
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.models.tenant_models import Document, DocumentFolder
from gdx_dispatch.routers import documents as documents_router


def _mock_request(tenant_id="test-tenant"):
    r = MagicMock()
    r.state.tenant = {"id": tenant_id}
    r.client.host = "127.0.0.1"
    return r


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed_document(
    db,
    *,
    title: str = "Test Doc",
    job_id: str | None = None,
    customer_id: str | None = None,
    folder_id: str | None = None,
    deleted: bool = False,
) -> str:
    now = datetime.now(timezone.utc)
    doc = Document(
        filename=f"{uuid.uuid4()}.txt",
        original_name="orig.txt",
        file_size=11,
        content_type="text/plain",
        uploaded_by="seed-user",
        title=title,
        description="seed",
        folder_id=uuid.UUID(folder_id) if folder_id else None,
        job_id=uuid.UUID(job_id) if job_id else None,
        customer_id=uuid.UUID(customer_id) if customer_id else None,
        tags="alpha,beta",
        uploaded_at=now,
        deleted_at=now if deleted else None,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return str(doc.id)


def _seed_folder(
    db,
    name: str = "General",
    deleted: bool = False,
    parent_id: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    folder = DocumentFolder(
        name=name,
        description="seed",
        created_by="seed-user",
        created_at=now,
        deleted_at=now if deleted else None,
        parent_id=uuid.UUID(parent_id) if parent_id else None,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return str(folder.id)


def test_document_folder_parent_id_round_trip(tenant_db_session):
    """S1: parent_id column round-trips on the model."""
    parent_id = _seed_folder(tenant_db_session, name="Parent")
    child_id = _seed_folder(tenant_db_session, name="Child", parent_id=parent_id)

    folders = {
        str(f.id): f
        for f in tenant_db_session.query(DocumentFolder).all()
    }
    assert folders[parent_id].parent_id is None
    assert str(folders[child_id].parent_id) == parent_id


@pytest.mark.anyio
async def test_create_folder_with_parent(tenant_db_session):
    parent = await documents_router.create_document_folder(
        request=_mock_request(),
        payload=documents_router.DocumentFolderCreateIn(name="Top"),
        user={"user_id": "u"},
        db=tenant_db_session,
    )
    child = await documents_router.create_document_folder(
        request=_mock_request(),
        payload=documents_router.DocumentFolderCreateIn(name="Sub", parent_id=parent.id),
        user={"user_id": "u"},
        db=tenant_db_session,
    )
    assert child.parent_id == parent.id
    assert parent.parent_id is None


@pytest.mark.anyio
async def test_create_folder_rejects_unknown_parent(tenant_db_session):
    with pytest.raises(Exception) as exc:
        await documents_router.create_document_folder(
            request=_mock_request(),
            payload=documents_router.DocumentFolderCreateIn(name="X", parent_id=str(uuid.uuid4())),
            user={"user_id": "u"},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.anyio
async def test_create_folder_rejects_invalid_parent_uuid(tenant_db_session):
    with pytest.raises(Exception) as exc:
        await documents_router.create_document_folder(
            request=_mock_request(),
            payload=documents_router.DocumentFolderCreateIn(name="X", parent_id="not-a-uuid"),
            user={"user_id": "u"},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.anyio
async def test_create_folder_rejects_deleted_parent(tenant_db_session):
    parent_id = _seed_folder(tenant_db_session, name="Gone", deleted=True)
    with pytest.raises(Exception) as exc:
        await documents_router.create_document_folder(
            request=_mock_request(),
            payload=documents_router.DocumentFolderCreateIn(name="X", parent_id=parent_id),
            user={"user_id": "u"},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.anyio
async def test_create_folder_enforces_max_depth(tenant_db_session):
    """At depth 15, creating a 16th must be rejected."""
    chain = [_seed_folder(tenant_db_session, name="L1")]
    for i in range(2, documents_router.MAX_FOLDER_DEPTH + 1):
        chain.append(_seed_folder(tenant_db_session, name=f"L{i}", parent_id=chain[-1]))

    # chain has MAX_FOLDER_DEPTH folders; adding a child to the last must fail.
    with pytest.raises(Exception) as exc:
        await documents_router.create_document_folder(
            request=_mock_request(),
            payload=documents_router.DocumentFolderCreateIn(name="TooDeep", parent_id=chain[-1]),
            user={"user_id": "u"},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.anyio
async def test_delete_folder_blocks_with_subfolders(tenant_db_session):
    parent_id = _seed_folder(tenant_db_session, name="Parent")
    _seed_folder(tenant_db_session, name="Child", parent_id=parent_id)

    with pytest.raises(Exception) as exc:
        await documents_router.delete_document_folder(
            folder_id=parent_id,
            request=_mock_request(),
            user={"user_id": "u"},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 409


@pytest.mark.anyio
async def test_list_folders_returns_parent_id(tenant_db_session):
    parent_id = _seed_folder(tenant_db_session, name="Top")
    _seed_folder(tenant_db_session, name="Sub", parent_id=parent_id)

    out = await documents_router.list_document_folders(_={}, db=tenant_db_session)
    by_name = {f.name: f for f in out}
    assert by_name["Top"].parent_id is None
    assert by_name["Sub"].parent_id == parent_id


@pytest.mark.anyio
async def test_list_folders_returns_doc_count(tenant_db_session):
    """Live (non-deleted) document count per folder, zero for empties."""
    busy = _seed_folder(tenant_db_session, name="Busy")
    empty = _seed_folder(tenant_db_session, name="Empty")
    _seed_document(tenant_db_session, title="A", folder_id=busy)
    _seed_document(tenant_db_session, title="B", folder_id=busy)
    _seed_document(tenant_db_session, title="C", folder_id=busy)

    out = await documents_router.list_document_folders(_={}, db=tenant_db_session)
    by_name = {f.name: f for f in out}
    assert by_name["Busy"].doc_count == 3
    assert by_name["Empty"].doc_count == 0


@pytest.mark.anyio
async def test_move_folder_to_new_parent(tenant_db_session):
    a = _seed_folder(tenant_db_session, name="A")
    b = _seed_folder(tenant_db_session, name="B")
    moved = await documents_router.move_document_folder(
        folder_id=b,
        payload=documents_router.DocumentFolderMoveIn(parent_id=a),
        request=_mock_request(),
        user={"user_id": "u"},
        db=tenant_db_session,
    )
    assert moved.parent_id == a


@pytest.mark.anyio
async def test_move_folder_to_root(tenant_db_session):
    a = _seed_folder(tenant_db_session, name="A")
    b = _seed_folder(tenant_db_session, name="B", parent_id=a)
    moved = await documents_router.move_document_folder(
        folder_id=b,
        payload=documents_router.DocumentFolderMoveIn(parent_id=None),
        request=_mock_request(),
        user={"user_id": "u"},
        db=tenant_db_session,
    )
    assert moved.parent_id is None


@pytest.mark.anyio
async def test_move_folder_rejects_self(tenant_db_session):
    a = _seed_folder(tenant_db_session, name="A")
    with pytest.raises(Exception) as exc:
        await documents_router.move_document_folder(
            folder_id=a,
            payload=documents_router.DocumentFolderMoveIn(parent_id=a),
            request=_mock_request(),
            user={"user_id": "u"},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.anyio
async def test_move_folder_rejects_cycle(tenant_db_session):
    """Moving Top under its own descendant must be rejected."""
    top = _seed_folder(tenant_db_session, name="Top")
    sub = _seed_folder(tenant_db_session, name="Sub", parent_id=top)
    inner = _seed_folder(tenant_db_session, name="Inner", parent_id=sub)
    with pytest.raises(Exception) as exc:
        await documents_router.move_document_folder(
            folder_id=top,
            payload=documents_router.DocumentFolderMoveIn(parent_id=inner),
            request=_mock_request(),
            user={"user_id": "u"},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.anyio
async def test_move_folder_rejects_unknown_parent(tenant_db_session):
    a = _seed_folder(tenant_db_session, name="A")
    with pytest.raises(Exception) as exc:
        await documents_router.move_document_folder(
            folder_id=a,
            payload=documents_router.DocumentFolderMoveIn(parent_id=str(uuid.uuid4())),
            request=_mock_request(),
            user={"user_id": "u"},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.anyio
async def test_list_folders_doc_count_excludes_soft_deleted(tenant_db_session):
    folder_id = _seed_folder(tenant_db_session, name="MixedBag")
    _seed_document(tenant_db_session, title="live", folder_id=folder_id)
    dead = _seed_document(tenant_db_session, title="dead", folder_id=folder_id)
    # Soft-delete one.
    from gdx_dispatch.models.tenant_models import Document
    tenant_db_session.query(Document).filter(Document.id == uuid.UUID(dead)).update(
        {"deleted_at": datetime.now(timezone.utc)}
    )
    tenant_db_session.commit()

    out = await documents_router.list_document_folders(_={}, db=tenant_db_session)
    by_name = {f.name: f for f in out}
    assert by_name["MixedBag"].doc_count == 1


def test_all_document_routes_require_auth_dependency():
    guarded_paths = set()
    for route in documents_router.router.routes:
        if not hasattr(route, "dependant"):
            continue
        for dep in route.dependant.dependencies:
            if dep.call is get_current_user:
                guarded_paths.add(route.path)
                break

    assert "/api/documents" in guarded_paths
    assert "/api/documents/{document_id}" in guarded_paths
    assert "/api/documents/{document_id}/download" in guarded_paths
    assert "/api/document-folders" in guarded_paths
    assert "/api/document-folders/{folder_id}" in guarded_paths


@pytest.mark.anyio
async def test_list_documents_empty(tenant_db_session):
    out = await documents_router.list_documents(
        job_id=None,
        customer_id=None,
        folder_id=None,
        _={},
        db=tenant_db_session,
    )
    assert out == []


@pytest.mark.anyio
async def test_list_documents_filters(tenant_db_session):
    job_1 = str(uuid.uuid4())
    job_2 = str(uuid.uuid4())
    cust_1 = str(uuid.uuid4())
    cust_2 = str(uuid.uuid4())

    folder_id = _seed_folder(tenant_db_session, name="Permits")
    _seed_document(tenant_db_session, title="A", job_id=job_1, customer_id=cust_1, folder_id=folder_id)
    _seed_document(tenant_db_session, title="B", job_id=job_2, customer_id=cust_1, folder_id=folder_id)
    _seed_document(tenant_db_session, title="C", job_id=job_2, customer_id=cust_2, folder_id=None)

    by_job = await documents_router.list_documents(
        job_id=job_1,
        customer_id=None,
        folder_id=None,
        _={},
        db=tenant_db_session,
    )
    assert len(by_job) == 1
    assert by_job[0].title == "A"

    by_customer = await documents_router.list_documents(
        job_id=None,
        customer_id=cust_2,
        folder_id=None,
        _={},
        db=tenant_db_session,
    )
    assert len(by_customer) == 1
    assert by_customer[0].title == "C"

    by_folder = await documents_router.list_documents(
        job_id=None,
        customer_id=None,
        folder_id=folder_id,
        _={},
        db=tenant_db_session,
    )
    assert len(by_folder) == 2


@pytest.mark.anyio
async def test_list_documents_excludes_soft_deleted(tenant_db_session):
    _seed_document(tenant_db_session, title="Active", deleted=False)
    _seed_document(tenant_db_session, title="Deleted", deleted=True)

    out = await documents_router.list_documents(
        job_id=None,
        customer_id=None,
        folder_id=None,
        _={},
        db=tenant_db_session,
    )
    assert [d.title for d in out] == ["Active"]


@pytest.mark.anyio
async def test_get_document_metadata_success(tenant_db_session):
    doc_id = _seed_document(tenant_db_session, title="Meta Doc")

    out = await documents_router.get_document(document_id=doc_id, _={}, db=tenant_db_session)
    assert out.id == doc_id
    assert out.title == "Meta Doc"


@pytest.mark.anyio
async def test_get_document_metadata_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        await documents_router.get_document(document_id=str(uuid.uuid4()), _={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.anyio
async def test_delete_document_soft_delete(tenant_db_session):
    doc_id = _seed_document(tenant_db_session, title="Delete Me")

    out = await documents_router.delete_document(document_id=doc_id, request=_mock_request(), user={}, db=tenant_db_session)
    assert out["ok"] is True

    from sqlalchemy import select
    row = tenant_db_session.execute(
        select(Document).where(Document.id == uuid.UUID(doc_id))
    ).scalar_one_or_none()
    assert row is not None
    assert row.deleted_at is not None


@pytest.mark.anyio
async def test_delete_document_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        await documents_router.delete_document(document_id=str(uuid.uuid4()), request=_mock_request(), user={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.anyio
async def test_upload_document_success(tenant_db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))

    upload = UploadFile(
        io.BytesIO(b"fake-pdf"),
        filename="proposal.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )
    out = await documents_router.upload_document(
        request=_mock_request(),
        file=upload,
        title="Proposal",
        description="Signed proposal",
        job_id=str(uuid.uuid4()),
        customer_id=str(uuid.uuid4()),
        folder_id=str(uuid.uuid4()),
        tags="signed,proposal",
        user={"user_id": "user-1"},
        db=tenant_db_session,
    )

    assert out.title == "Proposal"
    assert out.original_name == "proposal.pdf"
    assert out.file_size == 8

    stored_name = out.filename
    assert os.path.exists(tmp_path / stored_name)


@pytest.mark.anyio
async def test_upload_document_defaults_content_type(tenant_db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    upload = UploadFile(io.BytesIO(b"hello"), filename="plain.txt")
    out = await documents_router.upload_document(
        request=_mock_request(),
        file=upload,
        title="No Content Type",
        description=None,
        job_id=None,
        customer_id=None,
        folder_id=None,
        tags=None,
        user={"user_id": "user-1"},
        db=tenant_db_session,
    )
    assert out.content_type == "application/octet-stream"


@pytest.mark.anyio
async def test_download_document_success(tenant_db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    doc_id = _seed_document(tenant_db_session, title="Download Me")

    from sqlalchemy import select
    row = tenant_db_session.execute(
        select(Document).where(Document.id == uuid.UUID(doc_id))
    ).scalar_one_or_none()
    assert row is not None
    filename = row.filename
    (tmp_path / filename).write_bytes(b"hello world")

    resp = await documents_router.download_document(document_id=doc_id, _={}, db=tenant_db_session)
    assert os.path.samefile(resp.path, tmp_path / filename)
    assert resp.media_type == "text/plain"
    assert "attachment" in resp.headers.get("content-disposition", "")


@pytest.mark.anyio
async def test_download_document_missing_file_returns_404(tenant_db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    doc_id = _seed_document(tenant_db_session, title="Missing File")

    with pytest.raises(Exception) as exc:
        await documents_router.download_document(document_id=doc_id, _={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.anyio
async def test_list_folders_excludes_deleted(tenant_db_session):
    _seed_folder(tenant_db_session, name="Active", deleted=False)
    _seed_folder(tenant_db_session, name="Deleted", deleted=True)

    out = await documents_router.list_document_folders(_={}, db=tenant_db_session)
    assert [f.name for f in out] == ["Active"]


@pytest.mark.anyio
async def test_create_folder_and_rename(tenant_db_session):
    created = await documents_router.create_document_folder(
        request=_mock_request(),
        payload=documents_router.DocumentFolderCreateIn(name="Contracts", description="Legal"),
        user={"user_id": "user-2"},
        db=tenant_db_session,
    )
    assert created.name == "Contracts"

    updated = await documents_router.rename_document_folder(
        folder_id=created.id,
        payload=documents_router.DocumentFolderRenameIn(name="Signed Contracts"),
        request=_mock_request(),
        user={},
        db=tenant_db_session,
    )
    assert updated.name == "Signed Contracts"


@pytest.mark.anyio
async def test_rename_folder_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        await documents_router.rename_document_folder(
            folder_id=str(uuid.uuid4()),
            payload=documents_router.DocumentFolderRenameIn(name="x"),
            request=_mock_request(),
            user={},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.anyio
async def test_delete_folder_cascades_documents(tenant_db_session):
    folder_id = _seed_folder(tenant_db_session, name="ToDelete")
    other_id = _seed_folder(tenant_db_session, name="Keep")
    _seed_document(tenant_db_session, title="A", folder_id=folder_id)
    _seed_document(tenant_db_session, title="B", folder_id=folder_id)
    keep_doc = _seed_document(tenant_db_session, title="C", folder_id=other_id)

    await documents_router.delete_document_folder(
        folder_id=folder_id,
        request=_mock_request(),
        user={"user_id": "user-1"},
        db=tenant_db_session,
    )

    folders = await documents_router.list_document_folders(_={}, db=tenant_db_session)
    assert [f.name for f in folders] == ["Keep"]

    docs = await documents_router.list_documents(
        folder_id=None,
        job_id=None,
        customer_id=None,
        _={},
        db=tenant_db_session,
    )
    assert [d.id for d in docs] == [keep_doc]


@pytest.mark.anyio
async def test_delete_folder_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        await documents_router.delete_document_folder(
            folder_id=str(uuid.uuid4()),
            request=_mock_request(),
            user={},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.anyio
async def test_delete_folder_empty_succeeds(tenant_db_session):
    folder_id = _seed_folder(tenant_db_session, name="Empty")
    await documents_router.delete_document_folder(
        folder_id=folder_id,
        request=_mock_request(),
        user={"user_id": "user-1"},
        db=tenant_db_session,
    )
    folders = await documents_router.list_document_folders(_={}, db=tenant_db_session)
    assert folders == []
