"""Email Settings — configurable email provider per tenant."""
from __future__ import annotations

import base64
import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import EmailSetting
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["email-settings"])

PROVIDER_DEFAULTS = {
    "microsoft365": {"host": "smtp.office365.com", "port": 587},
    "gmail": {"host": "smtp.gmail.com", "port": 587},
    "smtp": {"host": "", "port": 587},
    "sendgrid": {"host": "smtp.sendgrid.net", "port": 587},
}


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


class EmailConfigIn(BaseModel):
    provider: str = Field(pattern="^(microsoft365|gmail|smtp|sendgrid|disabled)$")
    smtp_host: str = Field(default="", max_length=200)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=500)
    from_email: str = Field(default="", max_length=254)
    from_name: str = Field(default="", max_length=100)


@router.get("/email")
def get_email_config(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    row = db.query(EmailSetting).first()
    if row:
        return {
            "provider": row.provider,
            "smtp_host": row.smtp_host,
            "smtp_port": row.smtp_port,
            "username": row.username,
            "from_email": row.from_email,
            "from_name": row.from_name,
            "is_verified": row.is_verified,
        }
    return {"provider": "disabled", "smtp_host": "", "smtp_port": 587,
            "username": "", "from_email": "", "from_name": "", "is_verified": False}


@router.put("/email")
def save_email_config(
    request: Request, payload: EmailConfigIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    now = datetime.now(timezone.utc)
    pw_enc = base64.b64encode(payload.password.encode()).decode() if payload.password else ""

    # Auto-fill SMTP host/port from provider
    defaults = PROVIDER_DEFAULTS.get(payload.provider, {})
    host = payload.smtp_host or defaults.get("host", "")
    port = payload.smtp_port or defaults.get("port", 587)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    existing = db.query(EmailSetting).first()

    if existing:
        existing.provider = payload.provider
        existing.smtp_host = host
        existing.smtp_port = port
        existing.username = payload.username
        existing.password_enc = pw_enc
        existing.from_email = payload.from_email
        existing.from_name = payload.from_name
        existing.is_verified = False
        existing.updated_at = now
    else:
        new_setting = EmailSetting(
            id=str(uuid4()),
            company_id=tid,
            provider=payload.provider,
            smtp_host=host,
            smtp_port=port,
            username=payload.username,
            password_enc=pw_enc,
            from_email=payload.from_email,
            from_name=payload.from_name,
            is_verified=False,
            created_at=now,
            updated_at=now,
        )
        db.add(new_setting)
    db.commit()

    log_audit_event_sync(db, tenant_id=tid, user_id=str(user.get("sub", "system")),
                         action="update", entity_type="email_settings", entity_id=tid,
                         details={"provider": payload.provider}, request=request)
    return {"status": "saved", "provider": payload.provider}


@router.post("/email/test")
def test_email(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    row = db.query(EmailSetting).first()
    if not row or row.provider == "disabled":
        raise HTTPException(400, "Email not configured")

    try:
        pw = base64.b64decode(row.password_enc).decode() if row.password_enc else ""
        msg = EmailMessage()
        msg.set_content("This is a test email from GDX Platform. If you received this, your email settings are working correctly.")
        msg["Subject"] = "GDX Email Configuration Test"
        msg["From"] = f"{row.from_name} <{row.from_email}>"
        msg["To"] = row.from_email

        with smtplib.SMTP(row.smtp_host, row.smtp_port, timeout=10) as server:
            server.starttls()
            server.login(row.username, pw)
            server.send_message(msg)

        row.is_verified = True
        db.commit()
        return {"status": "success", "message": f"Test email sent to {row.from_email}"}
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(401, "Authentication failed — check username/password") from None
    except smtplib.SMTPConnectError:
        raise HTTPException(502, "Could not connect to SMTP server — check host/port") from None
    except Exception as e:
        log.exception("email_test_failed")
        raise HTTPException(500, f"Email test failed: {str(e)[:200]}") from None
