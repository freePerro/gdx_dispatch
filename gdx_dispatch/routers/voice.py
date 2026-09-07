"""Voice features — voice notes, voice-to-text.

Routes:
  POST /api/mobile/voice-note — upload audio, transcribe to text note
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.upload_limits import assert_upload_within_limit
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])


def _tenant_id(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


# ---------------------------------------------------------------------------
# Missed Call Detection (#187)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Voice-to-Text Notes (#277)
# ---------------------------------------------------------------------------

@router.post("/api/mobile/voice-note")
async def upload_voice_note(
    request: Request,
    job_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Upload audio file, transcribe to text, save as job note."""
    tenant_id = _tenant_id(request)
    user_id = str(user.get("sub") or user.get("user_id") or "system")

    # Ceiling BEFORE the read (2026-08-26). This handler had none; the only bound
    # was nginx client_max_body_size, 50M on the prod vhost. Outside the try below:
    # that block only lets an HTTPException through because of an `except
    # HTTPException: raise` clause ahead of its catch-all, and a 413 should not
    # depend on the ordering of except clauses staying the way it is today.
    assert_upload_within_limit(file)

    try:
        # Save audio file
        upload_dir = Path(os.getenv("UPLOAD_DIR", "/app/uploads")) / tenant_id / "voice_notes"
        upload_dir.mkdir(parents=True, exist_ok=True)

        file_id = str(uuid4())
        ext = (file.filename or "audio.webm").rsplit(".", 1)[-1][:10]
        filename = f"{file_id}.{ext}"
        file_path = upload_dir / filename

        data = await file.read()
        with open(file_path, "wb") as f:
            f.write(data)

        # Transcribe using available service
        transcription = _transcribe(file_path)

        # Save as job note
        from gdx_dispatch.models.tenant_models import JobNote
        note_id = str(uuid4())
        now = datetime.now(timezone.utc)
        db.add(JobNote(
            id=note_id, company_id=tenant_id, job_id=str(job_id),
            body=transcription, author_id=user_id, visibility="internal",
            created_at=now, updated_at=now,
        ))
        db.commit()

        log_audit_event_sync(
            db=db, tenant_id=tenant_id, user_id=user_id,
            action="voice_note_created", entity_type="job_note", entity_id=note_id,
            details={"job_id": job_id, "audio_file": filename, "transcription_length": len(transcription)},
            request=request,
        )
        db.commit()

        return {
            "note_id": note_id,
            "job_id": job_id,
            "transcription": transcription,
            "audio_file": filename,
            "created_at": now.isoformat(),
        }

    except HTTPException:
        raise
    except Exception:
        log.exception("voice_note_failed")
        raise HTTPException(status_code=500, detail="Failed to process voice note") from None


def _transcribe(file_path: Path) -> str:
    """Transcribe audio using available service."""
    # Try OpenAI Whisper API
    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key:
        try:
            import httpx
            with open(file_path, "rb") as f:
                resp = httpx.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (file_path.name, f, "audio/webm")},
                    data={"model": "whisper-1"},
                    timeout=30,
                )
                if resp.status_code == 200:
                    return resp.json().get("text", "")
        except Exception:
            log.exception("whisper_transcription_failed")

    # Try local whisper
    try:
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(str(file_path))
        return result.get("text", "")
    except ImportError:
        log.exception("_transcribe_failed")
        pass
    except Exception:
        log.exception("local_whisper_failed")

    return f"[Voice note — audio saved as {file_path.name}, transcription unavailable]"
