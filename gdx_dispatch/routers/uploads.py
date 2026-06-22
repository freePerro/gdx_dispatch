from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module

log = logging.getLogger(__name__)

try:
    from PIL import Image as _PILImage

    _HAS_PILLOW = True
except ImportError:
    log.exception("unknown_failed")
    _HAS_PILLOW = False
    log_pil = logging.getLogger(__name__)
    log_pil.warning("Pillow not installed — image compression disabled")

router = APIRouter(tags=["uploads"], dependencies=[Depends(require_module("documents"))])

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_IMAGE_DIMENSION = 2048
JPEG_QUALITY = 85
ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _compress_image(data: bytes, content_type: str) -> tuple[bytes, str]:
    """Compress an image: resize to MAX_IMAGE_DIMENSION, save JPEG@85 (or optimised PNG if transparent).

    Returns (compressed_bytes, new_content_type). Falls back to the original
    bytes when Pillow is unavailable or the image cannot be processed.
    """
    if not _HAS_PILLOW:
        return data, content_type

    import io

    try:
        img = _PILImage.open(io.BytesIO(data))

        # Resize if larger than MAX_IMAGE_DIMENSION on either axis
        w, h = img.size
        if w > MAX_IMAGE_DIMENSION or h > MAX_IMAGE_DIMENSION:
            ratio = min(MAX_IMAGE_DIMENSION / w, MAX_IMAGE_DIMENSION / h)
            new_w = int(w * ratio)
            new_h = int(h * ratio)
            img = img.resize((new_w, new_h), _PILImage.Resampling.LANCZOS)

        buf = io.BytesIO()

        # PNGs with transparency stay as optimised PNG; everything else -> JPEG
        has_alpha = img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        )
        if has_alpha:
            img.save(buf, format="PNG", optimize=True)
            out_ct = "image/png"
        else:
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            out_ct = "image/jpeg"

        compressed = buf.getvalue()

        log.info(
            "image_compressed",
            extra={
                "original_size": len(data),
                "compressed_size": len(compressed),
                "ratio": f"{len(compressed) / len(data):.1%}",
            },
        )
        return compressed, out_ct

    except Exception:
        log.exception("image_compression_failed — returning original bytes")
        return data, content_type


class DocumentOut(BaseModel):
    id: str
    tenant_id: str
    filename: str
    original_name: str
    content_type: str
    size_bytes: int
    entity_type: str
    entity_id: str
    uploaded_by: str | None = None
    created_at: str
    deleted_at: str | None = None


class SignatureUploadIn(BaseModel):
    # data URL / base64 signature image — cap at ~1MB raw
    signature: str = Field(min_length=1, max_length=1_400_000)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upload_dir() -> Path:
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads"))


def _sanitize_filename(filename: str | None, max_length: int = 120) -> str:
    candidate = os.path.basename((filename or "").replace("\\", "/"))
    candidate = candidate.replace("\x00", "")
    candidate = re.sub(r"[^A-Za-z0-9._-]", "_", candidate)
    candidate = candidate.strip("._")

    if not candidate:
        candidate = f"file-{uuid4().hex}"

    if len(candidate) <= max_length:
        return candidate

    stem, ext = os.path.splitext(candidate)
    ext = ext[:20]
    trim = max_length - len(ext)
    if trim <= 0:
        return candidate[:max_length]
    return f"{stem[:trim]}{ext}"


def _tenant_id_from(request: Request, user: dict[str, Any]) -> str:
    tenant_id = str(user.get("tenant_id") or getattr(request.state, "tenant", {}).get("id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tenant_id


def _user_id_from(user: dict[str, Any]) -> str:
    return str(user.get("user_id") or user.get("sub") or "system")


def _build_file_path(tenant_id: str, entity_type: str, entity_id: str, filename: str) -> Path:
    # Constrain the resolved path to the upload root so a crafted tenant_id /
    # entity_type / entity_id / filename can't traverse out. (CodeQL path-injection)
    base = _upload_dir().resolve()
    candidate = (base / tenant_id / entity_type / entity_id / filename).resolve()
    if not candidate.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Invalid upload path")
    return candidate


def _read_upload_with_limit(file: UploadFile, max_bytes: int) -> bytes:
    data = file.file.read()
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File exceeds {max_bytes // (1024 * 1024)}MB limit")
    return data


def _write_bytes_to_storage(path: Path, data: bytes) -> None:
    os.makedirs(path.parent, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(data)


def _insert_document(
    db: Session,
    *,
    tenant_id: str,
    filename: str,
    original_name: str,
    content_type: str,
    size_bytes: int,
    entity_type: str,
    entity_id: str,
    uploaded_by: str,
) -> dict[str, Any]:
    row = {
        "id": str(uuid4()),
        "tenant_id": tenant_id,
        "filename": filename,
        "original_name": original_name,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "uploaded_by": uploaded_by,
        "created_at": _utcnow_iso(),
    }
    db.execute(
        text(
            """
            INSERT INTO documents (
                id, tenant_id, filename, original_name, content_type, size_bytes,
                file_size, entity_type, entity_id, uploaded_by, created_at, uploaded_at, deleted_at
            ) VALUES (
                :id, :tenant_id, :filename, :original_name, :content_type, :size_bytes,
                :size_bytes, :entity_type, :entity_id, :uploaded_by, :created_at, :created_at, NULL
            )
            """
        ),
        row,
    )
    return row


@router.post("/api/jobs/{job_id}/photos", status_code=201, response_model=DocumentOut)
def upload_job_photo(
    job_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DocumentOut:
    try:
        if (file.content_type or "").strip().lower() not in ALLOWED_IMAGE_MIME_TYPES:
            raise HTTPException(status_code=415, detail="Only jpg/png/webp are supported")

        data = _read_upload_with_limit(file, MAX_PHOTO_BYTES)
        data, effective_ct = _compress_image(data, file.content_type or "application/octet-stream")
        tenant_id = _tenant_id_from(request, user)
        uploaded_by = _user_id_from(user)

        sanitized = _sanitize_filename(file.filename)
        stored = f"{uuid4().hex}-{sanitized}"
        output_path = _build_file_path(tenant_id, "job_photo", job_id, stored)
        _write_bytes_to_storage(output_path, data)

        row = _insert_document(
            db,
            tenant_id=tenant_id,
            filename=stored,
            original_name=_sanitize_filename(file.filename),
            content_type=effective_ct,
            size_bytes=len(data),
            entity_type="job_photo",
            entity_id=job_id,
            uploaded_by=uploaded_by,
        )

        asyncio.run(log_audit_event(
            db,
            tenant_id=tenant_id,
            user_id=uploaded_by,
            action="job_photo_uploaded",
            entity_type="job",
            entity_id=job_id,
            details={"document_id": row["id"], "filename": row["filename"], "size_bytes": row["size_bytes"]},
            request=request,
        ))
        db.commit()
        return DocumentOut(**row)
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        log.exception("upload_job_photo_failed", extra={"job_id": job_id})
        raise HTTPException(status_code=500, detail="Failed to upload job photo") from None


@router.post("/api/jobs/{job_id}/signature", status_code=201, response_model=DocumentOut)
def upload_customer_signature(
    job_id: str,
    payload: SignatureUploadIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DocumentOut:
    try:
        raw = payload.signature.strip()
        if raw.startswith("data:"):
            prefix, _, encoded = raw.partition(",")
            if "image/png" not in prefix.lower() or not encoded:
                raise HTTPException(status_code=400, detail="Signature must be base64 PNG")
            raw = encoded

        try:
            data = base64.b64decode(raw, validate=True)
        except binascii.Error as exc:
            log.exception("upload_signature_base64_decode_failed")
            raise HTTPException(status_code=400, detail="Invalid base64 signature") from exc

        if not data.startswith(PNG_SIGNATURE):
            raise HTTPException(status_code=400, detail="Signature must be PNG")
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds 10MB limit")

        data, effective_ct = _compress_image(data, "image/png")
        tenant_id = _tenant_id_from(request, user)
        uploaded_by = _user_id_from(user)

        ext = "png" if effective_ct == "image/png" else "jpg"
        stored = f"signature-{uuid4().hex}.{ext}"

        output_path = _build_file_path(tenant_id, "job_signature", job_id, stored)
        _write_bytes_to_storage(output_path, data)

        row = _insert_document(
            db,
            tenant_id=tenant_id,
            filename=stored,
            original_name=f"signature.{ext}",
            content_type=effective_ct,
            size_bytes=len(data),
            entity_type="job_signature",
            entity_id=job_id,
            uploaded_by=uploaded_by,
        )

        asyncio.run(log_audit_event(
            db,
            tenant_id=tenant_id,
            user_id=uploaded_by,
            action="signature_uploaded",
            entity_type="job",
            entity_id=job_id,
            details={"document_id": row["id"], "size_bytes": row["size_bytes"]},
            request=request,
        ))
        db.commit()
        return DocumentOut(**row)
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        log.exception("upload_customer_signature_failed", extra={"job_id": job_id})
        raise HTTPException(status_code=500, detail="Failed to upload signature") from None


# The /api/documents/{upload,download,delete} routes that previously
# lived here were deleted 2026-04-24 (Phase B1). Canonical document
# routes live in gdx_dispatch/routers/documents.py — POST /api/documents,
# GET /api/documents/{id}/download, DELETE /api/documents/{id}. Those
# write the tenant_id / entity_type / entity_id columns correctly; the
# versions that lived here left those columns NULL, which is the root
# cause of the 2026-04-22 upload/download bug. See Migration B in
# plans/sprint-three-plane-isolation.md.
