"""Customer statements — preview, PDF and email send from the customer record.

The arithmetic lives in services/customer_statements.py; this module is HTTP, delivery and the audit
trail. Reads are gated on invoices.read_all, the send on invoices.send.
"""
from __future__ import annotations

import base64
import html
import logging
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.core.money_format import format_money
from gdx_dispatch.core.pay_periods import shop_today_from_settings
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.services.customer_statements import (
    StatementRange,
    StatementRangeError,
    build_statement,
    presets_for,
    render_statement_pdf,
    resolve_range,
    to_json,
)

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/customers",
    tags=["customer-statements"],
    dependencies=[Depends(require_module("customers")), Depends(require_module("invoices"))],
)


def _actor_id(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


def _customer_or_404(db: Session, customer_id: UUID) -> Customer:
    customer = db.execute(
        select(Customer).where(Customer.id == customer_id, Customer.deleted_at.is_(None))
    ).scalar_one_or_none()
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


def _range(db: Session, preset: str | None, start: date | None, end: date | None) -> StatementRange:
    try:
        return resolve_range(shop_today_from_settings(db), preset=preset, start=start, end=end)
    except StatementRangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _pdf_filename(rng: StatementRange) -> str:
    return f"statement-{rng.start.isoformat()}-to-{rng.end.isoformat()}.pdf"


@router.get(
    "/{customer_id}/statement",
    response_model=None,
    dependencies=[Depends(require_permission("invoices.read_all"))],
)
def get_statement(
    customer_id: UUID,
    preset: str | None = Query(default=None, max_length=20),
    start: date | None = None,
    end: date | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from gdx_dispatch.core.email_recipients import resolve_recipient

    customer = _customer_or_404(db, customer_id)
    rng = _range(db, preset, start, end)
    today = shop_today_from_settings(db)
    statement = to_json(build_statement(db, customer, rng, today=today))
    recipient = resolve_recipient(db, customer)
    statement["presets"] = presets_for(db, customer, today)
    statement["default_recipient"] = {
        "email": recipient.email,
        "name": recipient.to_name,
        "source": recipient.source,
    }
    statement["pdf_filename"] = _pdf_filename(rng)
    return statement


@router.get(
    "/{customer_id}/statement/pdf",
    response_model=None,
    dependencies=[Depends(require_permission("invoices.read_all"))],
)
def get_statement_pdf(
    customer_id: UUID,
    preset: str | None = Query(default=None, max_length=20),
    start: date | None = None,
    end: date | None = None,
    download: bool = False,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    customer = _customer_or_404(db, customer_id)
    rng = _range(db, preset, start, end)
    pdf_bytes = render_statement_pdf(db, build_statement(db, customer, rng))
    disposition = "attachment" if download else "inline"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{_pdf_filename(rng)}"'},
    )


class StatementSendIn(BaseModel):
    preset: str | None = Field(default=None, max_length=20)
    start: date | None = None
    end: date | None = None
    to_email: str | None = Field(default=None, max_length=320)
    contact_id: str | None = Field(default=None, max_length=64)


def _email_html(db: Session, statement: dict[str, Any], greeting: str) -> tuple[str, str]:
    from gdx_dispatch.core.email_layout import email_branding, render_email

    branding = email_branding(db)
    esc = html.escape
    rng = statement["range"]
    subject = f"Statement from {branding['company_name']}: {rng['start']} to {rng['end']}"
    parts = [
        f'<p style="margin:0 0 12px;">Hi {esc(greeting or "there")},</p>',
        f'<p style="margin:0 0 12px;">Your statement for {esc(rng["start"])} to '
        f'{esc(rng["end"])} is attached.</p>',
        f'<p style="margin:0 0 12px;"><strong>Total unpaid balance: '
        f'{esc(format_money(statement["total_unpaid"]))}</strong></p>',
    ]
    if statement["credit_on_account"] > 0:
        parts.append(
            f'<p style="margin:0 0 12px;">Credit on account: '
            f'{esc(format_money(statement["credit_on_account"]))}</p>'
        )
    open_rows = statement["open_invoices"]
    if open_rows:
        items = []
        for row in open_rows:
            label = f'Invoice {esc(row["invoice_number"] or "")} — {esc(format_money(row["balance"]))}'
            if row.get("pay_url"):
                label += f' — <a href="{esc(row["pay_url"])}">Pay online</a>'
            items.append(f'<li style="margin:0 0 6px;">{label}</li>')
        parts.append(f'<ul style="margin:0 0 12px;padding-left:18px;">{"".join(items)}</ul>')
    else:
        parts.append('<p style="margin:0 0 12px;">Nothing is unpaid. Thank you.</p>')
    body = render_email(
        branding=branding,
        body_html="".join(parts),
        title="Statement of account",
        preheader=f"Total unpaid balance {format_money(statement['total_unpaid'])}",
    )
    return subject, body


@router.post(
    "/{customer_id}/statement/send",
    response_model=None,
    dependencies=[Depends(require_permission("invoices.send"))],
)
def send_statement(
    customer_id: UUID,
    payload: StatementSendIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Email the statement.

    Every attempt leaves an audit row carrying the statement for the range
    that was sent — its figures, invoice lines, aging and warnings — whether
    it went out, was refused (no usable address, a duplicate inside the guard
    window) or failed outright. An attempt that reaches the send path also
    leaves an outbound_emails row; a refused one never gets that far."""
    from gdx_dispatch.core.email_recipients import override_recipient, resolve_recipient
    from gdx_dispatch.core.transactional_email import (
        MAX_INLINE_ATTACHMENT_BYTES,
        recently_sent,
        send_transactional_email,
    )

    customer = _customer_or_404(db, customer_id)
    rng = _range(db, payload.preset, payload.start, payload.end)
    statement = build_statement(db, customer, rng)

    if payload.to_email and payload.to_email.strip():
        recipient = override_recipient(payload.to_email, customer.name or "")
    else:
        recipient = resolve_recipient(db, customer, payload.contact_id)

    email_sent = False
    provider: str | None = None
    skip_reason: str | None = None
    pdf_attached = False
    if not recipient.ok:
        skip_reason = "invalid_recipient_email" if recipient.source == "invalid_override" else "customer_has_no_email"
    elif recently_sent(db, "customer", str(customer.id), kind="statement"):
        skip_reason = "duplicate_send_suppressed"
    else:
        try:
            attachments: list[dict[str, Any]] | None = None
            pdf_bytes = render_statement_pdf(db, statement)
            if len(pdf_bytes) <= MAX_INLINE_ATTACHMENT_BYTES:
                attachments = [{
                    "name": _pdf_filename(rng),
                    "content_type": "application/pdf",
                    "content_base64": base64.b64encode(pdf_bytes).decode("ascii"),
                }]
            else:
                log.warning("statement_pdf_too_large_to_attach customer=%s bytes=%s", customer.id, len(pdf_bytes))
            subject, body = _email_html(db, statement, recipient.greeting_name)
            email_sent, provider, skip_reason = send_transactional_email(
                tenant_db=db,
                tenant_id=str(customer.company_id),
                user_id=_actor_id(user),
                to_email=recipient.email,
                to_name=recipient.to_name,
                subject=subject,
                html_body=body,
                attachments=attachments,
                kind="statement",
                entity_type="customer",
                entity_id=str(customer.id),
                recipient_source=recipient.source,
                recipient_contact_id=recipient.contact_id,
            )
            pdf_attached = email_sent and bool(attachments)
        except Exception:
            # Roll back before touching the session again: a failed write leaves
            # it unusable, and even reading customer.id would raise. The URL's
            # id is plain data. The attempt is still recorded below.
            db.rollback()
            log.exception("statement_send_failed customer=%s", customer_id)
            email_sent, provider, skip_reason = False, None, "exception"

    # The send path records its own email-log row and swallows a failure to
    # write it — leaving the session unusable without raising. Roll back so the
    # audit row below can still be written; the send's own result stands.
    if not db.is_active:
        db.rollback()
    snapshot = to_json(statement)
    log_audit_event_sync(
        db=db,
        # The Activity feeds filter on the request's tenant; a NULL here made
        # every statement send invisible there.
        tenant_id=str(user.get("tenant_id") or customer.company_id),
        user_id=_actor_id(user),
        action="statement_sent" if email_sent else "statement_not_sent",
        entity_type="customer",
        entity_id=str(customer.id),
        details={
            "range": snapshot["range"],
            "previous_balance": snapshot["previous_balance"],
            "invoiced": snapshot["invoiced"],
            "payments_and_credits": snapshot["payments_and_credits_total"],
            "ending_balance": snapshot["ending_balance"],
            "total_unpaid": snapshot["total_unpaid"],
            "credit_on_account": snapshot["credit_on_account"],
            "aging": snapshot["aging"],
            "open_invoices": [
                {k: r[k] for k in ("invoice_number", "total", "balance", "days_past_due")}
                for r in snapshot["open_invoices"]
            ],
            "invoices": [
                {k: r[k] for k in ("invoice_number", "total", "paid", "balance")}
                for r in snapshot["invoices"]
            ],
            "warnings": [
                {k: w[k] for k in ("kind", "invoice_number")} for w in snapshot["warnings"]
            ],
            "recipient": recipient.email or None,
            "email_sent": email_sent,
            "provider": provider,
            "skip_reason": skip_reason,
            "pdf_attached": pdf_attached,
        },
    )
    db.commit()

    out: dict[str, Any] = {
        "email_sent": email_sent,
        "pdf_attached": pdf_attached,
        "to_email": recipient.email or None,
    }
    if provider:
        out["email_provider"] = provider
    if skip_reason:
        out["email_skip_reason"] = skip_reason
    return out
