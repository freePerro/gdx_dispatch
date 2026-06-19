from __future__ import annotations

import logging
import os
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Document, DocumentFolder, User

log = logging.getLogger(__name__)

router = APIRouter(tags=["documents"], dependencies=[Depends(require_module("documents"))])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _upload_dir() -> Path:
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads/"))


def _normalize_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _to_uuid(value: str) -> UUID:
    """Convert a string to UUID for ORM column comparison."""
    return _uuid.UUID(value)


class DocumentOut(BaseModel):
    id: str
    filename: str
    original_name: str
    file_size: int
    content_type: str | None = None
    uploaded_by: str | None = None
    title: str | None = None
    description: str | None = None
    folder_id: str | None = None
    job_id: str | None = None
    customer_id: str | None = None
    tags: str | None = None
    uploaded_at: str | None = None
    deleted_at: str | None = None


class DocumentFolderOut(BaseModel):
    id: str
    name: str
    parent_id: str | None = None
    description: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    deleted_at: str | None = None
    doc_count: int = 0


class DocumentFolderCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1)
    parent_id: str | None = None
    description: str | None = None


class DocumentFolderRenameIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1)


class DocumentFolderMoveIn(BaseModel):
    """Payload for moving a folder. parent_id=null moves it to root."""
    parent_id: str | None = None


MAX_FOLDER_DEPTH = 15


def _validate_uuid(value: str, entity: str = "Resource") -> None:
    try:
        _uuid.UUID(value)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail=f"{entity} not found") from None


def _serialize_document(doc: Document, uploaded_by_display: str | None = None) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "filename": doc.filename,
        "original_name": doc.original_name,
        "file_size": int(doc.file_size),
        "content_type": doc.content_type,
        "uploaded_by": uploaded_by_display or doc.uploaded_by,
        "title": doc.title,
        "description": doc.description,
        "folder_id": str(doc.folder_id) if doc.folder_id else None,
        "job_id": str(doc.job_id) if doc.job_id else None,
        "customer_id": str(doc.customer_id) if doc.customer_id else None,
        "tags": doc.tags,
        "uploaded_at": _normalize_datetime(doc.uploaded_at),
        "deleted_at": _normalize_datetime(doc.deleted_at),
    }


def _serialize_folder(folder: DocumentFolder, doc_count: int = 0) -> dict[str, Any]:
    return {
        "id": str(folder.id),
        "name": folder.name,
        "parent_id": str(folder.parent_id) if folder.parent_id else None,
        "description": folder.description,
        "created_by": folder.created_by,
        "created_at": _normalize_datetime(folder.created_at),
        "deleted_at": _normalize_datetime(folder.deleted_at),
        "doc_count": doc_count,
    }


def _is_descendant_of(db: Session, candidate_id: UUID, ancestor_id: UUID) -> bool:
    """Returns True if candidate_id appears anywhere up the parent chain from
    a folder whose parent is ancestor_id. Used to block moves that would
    create a cycle (e.g. moving Top under one of its own descendants)."""
    cursor_id: UUID | None = ancestor_id
    seen: set[UUID] = set()
    depth = 0
    while cursor_id is not None and depth < MAX_FOLDER_DEPTH * 2:
        if cursor_id == candidate_id:
            return True
        if cursor_id in seen:
            return False  # malformed chain; caller raises separately
        seen.add(cursor_id)
        row = db.execute(
            select(DocumentFolder.parent_id).where(DocumentFolder.id == cursor_id)
        ).scalar_one_or_none()
        cursor_id = row
        depth += 1
    return False


def _resolve_parent_or_400(db: Session, parent_id: str) -> DocumentFolder:
    """Validate a proposed parent folder exists, is live, and chain depth < MAX.

    Caller must have already confirmed parent_id is a syntactically valid UUID.
    Walks the chain from parent to root counting depth; rejects if the new
    child would exceed MAX_FOLDER_DEPTH (root = depth 1).
    """
    try:
        parent_uuid = _to_uuid(parent_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid parent_id") from None

    parent = db.execute(
        select(DocumentFolder).where(
            DocumentFolder.id == parent_uuid,
            DocumentFolder.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=400, detail="Parent folder not found")

    depth = 1
    cursor = parent
    seen: set[str] = set()
    while cursor.parent_id is not None:
        depth += 1
        if depth >= MAX_FOLDER_DEPTH:
            raise HTTPException(
                status_code=400,
                detail=f"Folder nesting exceeds max depth of {MAX_FOLDER_DEPTH}",
            )
        cursor_id = str(cursor.parent_id)
        if cursor_id in seen:
            raise HTTPException(status_code=400, detail="Folder hierarchy contains a cycle")
        seen.add(cursor_id)
        cursor = db.execute(
            select(DocumentFolder).where(DocumentFolder.id == cursor.parent_id)
        ).scalar_one_or_none()
        if cursor is None:
            break
    return parent


def _get_document_or_404(db: Session, document_id: str) -> Document:
    doc = db.execute(
        select(Document).where(
            Document.id == _to_uuid(document_id),
            Document.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


def _get_folder_or_404(db: Session, folder_id: str) -> DocumentFolder:
    folder = db.execute(
        select(DocumentFolder).where(
            DocumentFolder.id == _to_uuid(folder_id),
            DocumentFolder.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder


@router.get("/api/documents", response_model=list[DocumentOut])
async def list_documents(
    job_id: str | None = None,
    customer_id: str | None = None,
    folder_id: str | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DocumentOut]:
    stmt = select(Document).where(Document.deleted_at.is_(None))

    if job_id:
        stmt = stmt.where(Document.job_id == _to_uuid(job_id))
    if customer_id:
        stmt = stmt.where(Document.customer_id == _to_uuid(customer_id))
    if folder_id:
        stmt = stmt.where(Document.folder_id == _to_uuid(folder_id))

    stmt = stmt.order_by(Document.uploaded_at.desc())
    docs = db.execute(stmt).scalars().all()

    results: list[DocumentOut] = []
    for doc in docs:
        # Resolve uploaded_by to email/username if possible
        uploaded_by_display = doc.uploaded_by
        if doc.uploaded_by:
            try:
                user_row = db.execute(
                    select(User).where(User.id == doc.uploaded_by)
                ).scalar_one_or_none()
                if user_row:
                    uploaded_by_display = user_row.email or user_row.username or doc.uploaded_by
            except Exception:
                log.exception("documents_user_lookup_failed uploaded_by=%s", doc.uploaded_by)
        results.append(DocumentOut(**_serialize_document(doc, uploaded_by_display)))
    return results


@router.post("/api/documents", status_code=201, response_model=DocumentOut)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    description: str | None = Form(default=None),
    folder_id: str | None = Form(default=None),
    job_id: str | None = Form(default=None),
    customer_id: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DocumentOut:
    ext = Path(file.filename or "").suffix
    stored_filename = f"{uuid4()}{ext.lower()}"

    upload_root = _upload_dir()
    upload_root.mkdir(parents=True, exist_ok=True)
    output_path = upload_root / stored_filename

    data = file.file.read()
    with output_path.open("wb") as out:
        out.write(data)

    doc = Document(
        filename=stored_filename,
        original_name=file.filename or stored_filename,
        file_size=len(data),
        content_type=file.content_type or "application/octet-stream",
        uploaded_by=user.get("user_id") or "",
        title=title,
        description=description,
        folder_id=_to_uuid(folder_id) if folder_id else None,
        job_id=_to_uuid(job_id) if job_id else None,
        customer_id=_to_uuid(customer_id) if customer_id else None,
        tags=tags,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="document_created",
        entity_type="document",
        entity_id=str(doc.id),
        details={"original_name": file.filename, "file_size": len(data), "folder_id": folder_id, "job_id": job_id, "customer_id": customer_id},
    )
    db.commit()

    return DocumentOut(**_serialize_document(doc))


@router.get("/api/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: str,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DocumentOut:
    _validate_uuid(document_id, "Document")
    doc = _get_document_or_404(db, document_id)
    return DocumentOut(**_serialize_document(doc))


@router.delete("/api/documents/{document_id}")
async def delete_document(
    document_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    _validate_uuid(document_id, "Document")
    doc = _get_document_or_404(db, document_id)

    doc.deleted_at = _utc_now()
    db.commit()

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="document_deleted",
        entity_type="document",
        entity_id=document_id,
        details={"document_id": document_id},
    )
    db.commit()
    return {"ok": True}


@router.get("/api/documents/{document_id}/download")
async def download_document(
    document_id: str,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    _validate_uuid(document_id, "Document")
    doc = _get_document_or_404(db, document_id)
    path = _upload_dir() / doc.filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=path,
        media_type=doc.content_type or "application/octet-stream",
        filename=doc.original_name,
    )


@router.get("/api/document-folders", response_model=list[DocumentFolderOut])
async def list_document_folders(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DocumentFolderOut]:
    folders = db.execute(
        select(DocumentFolder)
        .where(DocumentFolder.deleted_at.is_(None))
        .order_by(DocumentFolder.name.asc())
    ).scalars().all()

    counts = {
        fid: int(n)
        for fid, n in db.execute(
            select(Document.folder_id, func.count(Document.id))
            .where(Document.folder_id.is_not(None))
            .where(Document.deleted_at.is_(None))
            .group_by(Document.folder_id)
        ).all()
    }
    return [
        DocumentFolderOut(**_serialize_folder(f, doc_count=counts.get(f.id, 0)))
        for f in folders
    ]


@router.post("/api/document-folders", status_code=201, response_model=DocumentFolderOut)
async def create_document_folder(
    request: Request,
    payload: DocumentFolderCreateIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DocumentFolderOut:
    parent_uuid = None
    if payload.parent_id:
        parent = _resolve_parent_or_400(db, payload.parent_id)
        parent_uuid = parent.id

    folder = DocumentFolder(
        name=payload.name,
        parent_id=parent_uuid,
        description=payload.description,
        created_by=user.get("user_id") or "",
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="document_folder_created",
        entity_type="document_folder",
        entity_id=str(folder.id),
        details={
            "name": payload.name,
            "parent_id": str(parent_uuid) if parent_uuid else None,
            "description": payload.description,
        },
    )
    db.commit()

    return DocumentFolderOut(**_serialize_folder(folder))


@router.patch("/api/document-folders/{folder_id}", response_model=DocumentFolderOut)
async def rename_document_folder(
    folder_id: str,
    payload: DocumentFolderRenameIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DocumentFolderOut:
    _validate_uuid(folder_id, "Folder")
    folder = _get_folder_or_404(db, folder_id)

    folder.name = payload.name
    db.commit()
    db.refresh(folder)

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="document_folder_renamed",
        entity_type="document_folder",
        entity_id=folder_id,
        details={"new_name": payload.name},
    )
    db.commit()

    return DocumentFolderOut(**_serialize_folder(folder))


@router.patch("/api/document-folders/{folder_id}/move", response_model=DocumentFolderOut)
async def move_document_folder(
    folder_id: str,
    payload: DocumentFolderMoveIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DocumentFolderOut:
    _validate_uuid(folder_id, "Folder")
    folder = _get_folder_or_404(db, folder_id)
    folder_uuid = _to_uuid(folder_id)

    new_parent_uuid: UUID | None = None
    if payload.parent_id:
        parent = _resolve_parent_or_400(db, payload.parent_id)
        new_parent_uuid = parent.id
        if new_parent_uuid == folder_uuid:
            raise HTTPException(status_code=400, detail="Folder cannot be its own parent")
        if _is_descendant_of(db, folder_uuid, new_parent_uuid):
            raise HTTPException(
                status_code=400,
                detail="Cannot move a folder under one of its own descendants",
            )

    old_parent_uuid = folder.parent_id
    folder.parent_id = new_parent_uuid
    db.commit()
    db.refresh(folder)

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="document_folder_moved",
        entity_type="document_folder",
        entity_id=folder_id,
        details={
            "old_parent_id": str(old_parent_uuid) if old_parent_uuid else None,
            "new_parent_id": str(new_parent_uuid) if new_parent_uuid else None,
        },
    )
    db.commit()

    return DocumentFolderOut(**_serialize_folder(folder))


@router.delete("/api/document-folders/{folder_id}", status_code=204)
async def delete_document_folder(
    folder_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    _validate_uuid(folder_id, "Folder")
    folder = _get_folder_or_404(db, folder_id)
    folder_uuid = _to_uuid(folder_id)
    now = _utc_now()

    child_count = db.execute(
        select(func.count(DocumentFolder.id))
        .where(DocumentFolder.parent_id == folder_uuid)
        .where(DocumentFolder.deleted_at.is_(None))
    ).scalar_one()
    if child_count:
        raise HTTPException(
            status_code=409,
            detail=f"Folder has {child_count} subfolder(s); delete or move them first.",
        )

    doc_count = db.execute(
        select(func.count(Document.id))
        .where(Document.folder_id == folder_uuid)
        .where(Document.deleted_at.is_(None))
    ).scalar_one()

    db.execute(
        update(Document)
        .where(Document.folder_id == folder_uuid)
        .where(Document.deleted_at.is_(None))
        .values(deleted_at=now)
    )

    folder.deleted_at = now
    db.commit()

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action="document_folder_deleted",
        entity_type="document_folder",
        entity_id=folder_id,
        details={"name": folder.name, "documents_cascade_deleted": int(doc_count)},
    )
    db.commit()
