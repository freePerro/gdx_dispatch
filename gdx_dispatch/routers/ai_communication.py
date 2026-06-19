"""AI Communication Drafts — generate contextual SMS/email text.

Uses local AI (Gemma/LocalAI) when AI_PROVIDER_URL is configured,
falls back to built-in templates otherwise.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.ai_provider import generate_sync
from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/ai/communication",
    tags=["ai-communication"],
    dependencies=[Depends(require_module("communications"))],
)

_TEMPLATES = {
    "appointment_reminder": {
        "sms": "Hi {name}, this is a reminder for your upcoming appointment. Please call us if you need to reschedule.",
        "email_subject": "Appointment Reminder — {company}",
        "email_body": "Hi {name},\n\nThis is a friendly reminder regarding your scheduled service. Please let us know if you need to reschedule.\n\nThank you,\n{company}",
    },
    "estimate_followup": {
        "sms": "Hi {name}, just checking in on the estimate we sent. Any questions? We'd love to help.",
        "email_subject": "Following Up on Your Estimate — {company}",
        "email_body": "Hi {name},\n\nI wanted to follow up on the estimate we recently provided. Do you have any questions, or would you like to move forward?\n\nBest regards,\n{company}",
    },
    "payment_reminder": {
        "sms": "Hi {name}, a friendly reminder that you have an outstanding balance. Thank you for your prompt attention.",
        "email_subject": "Payment Reminder — {company}",
        "email_body": "Hi {name},\n\nThis is a friendly reminder regarding your outstanding balance. You can pay online or call us directly.\n\nThank you for your business,\n{company}",
    },
    "thank_you": {
        "sms": "Hi {name}, thank you for choosing {company}! It was a pleasure serving you.",
        "email_subject": "Thank You for Your Business — {company}",
        "email_body": "Hi {name},\n\nThank you for choosing {company}. We appreciate your business and look forward to serving you again.\n\nBest regards,\n{company}",
    },
    "job_complete": {
        "sms": "Hi {name}, your service has been completed. Please don't hesitate to reach out with any questions.",
        "email_subject": "Service Completed — {company}",
        "email_body": "Hi {name},\n\nWe're pleased to let you know your service has been completed. If you have any questions or concerns, please don't hesitate to reach out.\n\nThank you,\n{company}",
    },
}


class DraftRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    type: str = Field(pattern="^(sms|email)$")
    context: str = Field(min_length=1, max_length=50)
    custom_prompt: str | None = Field(default=None, max_length=500)


def _template_draft(context: str, msg_type: str, name: str, company: str) -> dict:
    template = _TEMPLATES.get(context)
    if not template:
        return {"draft_text": f"Hi {name}, thank you for your business with {company}.", "source": "fallback"}
    if msg_type == "sms":
        return {"draft_text": template["sms"].format(name=name, company=company), "source": "template"}
    return {
        "draft_text": template["email_body"].format(name=name, company=company),
        "subject": template["email_subject"].format(name=name, company=company),
        "source": "template",
    }


def _ai_draft(context: str, msg_type: str, name: str, company: str, custom_prompt: str | None) -> dict | None:
    prompt = custom_prompt or (
        f"Write a {msg_type.upper()} message for a garage door service company called '{company}'. "
        f"The customer's name is '{name}'. Context: {context}. "
        f"{'Keep it under 160 characters.' if msg_type == 'sms' else 'Include a subject line on the first line, then a blank line, then the body.'}"
    )
    result = generate_sync(
        prompt=prompt,
        system=f"You are a professional communication assistant for {company}, a garage door service company. Write friendly, professional messages.",
    )
    if not result:
        return None

    if msg_type == "email" and "\n" in result:
        lines = result.strip().split("\n", 1)
        subject = lines[0].replace("Subject:", "").strip()
        body = lines[1].strip() if len(lines) > 1 else result
        return {"draft_text": body, "subject": subject, "source": "ai"}
    return {"draft_text": result.strip(), "source": "ai"}


@router.post("/draft")
def generate_draft(
    body: DraftRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", ""))

    from gdx_dispatch.models.tenant_models import Customer
    customer = db.execute(
        select(Customer).where(Customer.id == body.customer_id)
    ).scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    row = {"name": customer.name, "email": customer.email, "phone": customer.phone}

    name = row["name"] or "Valued Customer"
    company = "DispatchApp"

    # Try AI first, fall back to templates
    result = _ai_draft(body.context, body.type, name, company, body.custom_prompt)
    if not result:
        result = _template_draft(body.context, body.type, name, company)

    result["type"] = body.type
    result["customer_name"] = name

    try:
        log_audit_event_sync(
            db, tenant_id=tenant_id,
            user_id=str(user.get("sub") or "system"),
            action="ai_draft_generated",
            entity_type="communication",
            entity_id=body.customer_id,
            details={"context": body.context, "type": body.type, "source": result.get("source")},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("ai_draft_audit_failed")

    return result


@router.get("/templates")
def list_templates(_: dict = Depends(get_current_user)) -> dict:
    return {"templates": list(_TEMPLATES.keys())}
