"""Voice features — missed call detection, voice notes, voice-to-text.

Routes:
  POST /api/communications/missed-call — Twilio webhook for missed calls
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

from gdx_dispatch.core.twilio_signature import verify_twilio_signature
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])


def _tenant_id(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


# ---------------------------------------------------------------------------
# Missed Call Detection (#187)
# ---------------------------------------------------------------------------

@router.post("/api/communications/missed-call", response_class=PlainTextResponse)
async def missed_call_webhook(request: Request, db: Session = Depends(get_db), _sig: None = Depends(verify_twilio_signature)) -> str:
    """Twilio voice webhook — auto-SMS when call is missed."""
    try:
        form = await request.form()
        call_status = form.get("CallStatus", "")
        caller = form.get("From", "")
        form.get("To", "")

        if call_status not in ("no-answer", "busy", "failed"):
            return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

        tenant_id = _tenant_id(request)

        # Look up caller in customers
        from gdx_dispatch.models.tenant_models import Customer
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        _cust = db.execute(
            select(Customer).where(Customer.phone == caller)
        ).scalars().first()
        customer = {"id": str(_cust.id), "name": _cust.name, "phone": _cust.phone} if _cust else None

        # Auto-send SMS
        if caller:
            try:
                import os as _os
                from gdx_dispatch.core import sms as sms_service
                from_phone = _os.getenv("TWILIO_PHONE_NUMBER", "").strip()
                sms_service.send_sms(
                    to_phone=caller,
                    body="Sorry we missed your call! We'll get back to you as soon as possible. Reply to this message if you need immediate assistance.",
                    from_phone=from_phone,
                    tenant_id=tenant_id,
                )
            except Exception:
                log.exception("missed_call_auto_sms_failed")

        log_audit_event_sync(
            db=db, tenant_id=tenant_id, user_id="system",
            action="missed_call_detected", entity_type="call",
            entity_id=str(uuid4()),
            details={"caller": caller, "status": call_status, "customer_id": str(customer["id"]) if customer else None},
            request=request,
        )
        db.commit()

        return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

    except Exception:
        log.exception("missed_call_webhook_failed")
        return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


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
