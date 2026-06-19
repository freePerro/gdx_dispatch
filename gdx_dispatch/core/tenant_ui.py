"""
Tenant self-serve admin UI routes.

Provides Jinja2-rendered HTML pages for:
  GET  /dashboard         → dashboard with KPIs, recent jobs, onboarding checklist
  GET  /settings          → company/terminology/notification/webhook settings
  POST /settings          → save settings
  GET  /team              → team management table + invite modal
  POST /team/invite       → invite a new user (sends email)
  GET  /billing           → redirect to Stripe customer portal
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.onboarding import get_onboarding_status

logger = logging.getLogger(__name__)

# ── Template setup ────────────────────────────────────────────────────────────
_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

router = APIRouter(tags=["tenant-ui"])


# ── Auth helper ───────────────────────────────────────────────────────────────

def _get_current_user(request: Request) -> dict[str, Any] | None:
    """Extract current user from request state (set by auth middleware/JWT)."""
    return getattr(request.state, "current_user", None)


def _require_auth(request: Request) -> dict[str, Any]:
    """Return current user or redirect to login."""
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})
    return user


def _base_ctx(request: Request, current_user: dict[str, Any]) -> dict[str, Any]:
    """Common template context for all UI pages."""
    return {
        "request": request,
        "current_user": current_user,
        "flash_messages": getattr(request.state, "flash_messages", []),
    }


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    tenant_db: Session = Depends(get_db),
) -> HTMLResponse:
    current_user = _require_auth(request)

    # KPI stats
    stats: dict[str, Any] = {
        "jobs_30d": 0,
        "revenue_30d": "0.00",
        "active_customers": 0,
        "technicians": 0,
    }
    with contextlib.suppress(Exception):
        stats["jobs_30d"] = tenant_db.execute(
            text("SELECT COUNT(*) FROM jobs WHERE created_at >= datetime('now', '-30 days') AND deleted_at IS NULL")
        ).scalar() or 0

    try:
        rev = tenant_db.execute(
            text(
                "SELECT COALESCE(SUM(total), 0) FROM invoice_headers "
                "WHERE created_at >= datetime('now', '-30 days') AND status IN ('paid','sent')"
            )
        ).scalar()
        stats["revenue_30d"] = f"{float(rev or 0):,.2f}"
    except Exception:
        logging.getLogger(__name__).exception("dashboard caught exception")
        pass

    with contextlib.suppress(Exception):
        stats["active_customers"] = tenant_db.execute(
            text("SELECT COUNT(*) FROM customers WHERE deleted_at IS NULL")
        ).scalar() or 0

    with contextlib.suppress(Exception):
        stats["technicians"] = tenant_db.execute(
            text("SELECT COUNT(*) FROM users WHERE role = 'technician' AND active = 1")
        ).scalar() or 0

    # Recent jobs (last 5)
    recent_jobs: list[dict[str, Any]] = []
    try:
        rows = tenant_db.execute(
            text(
                "SELECT j.id, j.job_number, j.status, j.total AS amount, j.created_at, "
                "       c.name AS customer_name, u.name AS technician_name "
                "FROM jobs j "
                "LEFT JOIN customers c ON c.id = j.customer_id "
                "LEFT JOIN users u ON u.id = j.assigned_tech_id "
                "WHERE j.deleted_at IS NULL "
                "ORDER BY j.created_at DESC LIMIT 5"
            )
        ).mappings().all()
        for r in rows:
            recent_jobs.append(dict(r))
    except Exception:
        logging.getLogger(__name__).exception("dashboard caught exception")
        pass

    # Onboarding status
    onboarding: dict[str, Any] = {"percent": 100, "steps": []}
    _tenant = getattr(request.state, "tenant", {}) or {}
    _tenant_id = str(_tenant.get("id") or "")
    try:
        steps = get_onboarding_status(_tenant_id)
        complete = sum(1 for s in steps if s.is_complete)
        pct = round(complete / len(steps) * 100) if steps else 100
        onboarding = {
            "percent": pct,
            "steps": [
                {"key": s.step_name, "title": s.title, "complete": s.is_complete, "action_url": None}
                for s in steps
            ],
        }
    except Exception:
        logging.getLogger(__name__).exception("dashboard caught exception")
        pass

    ctx = _base_ctx(request, current_user)
    ctx.update({"stats": stats, "recent_jobs": recent_jobs, "onboarding": onboarding})
    return templates.TemplateResponse("tenant_dashboard.html", ctx)


# ── Settings ──────────────────────────────────────────────────────────────────

def _load_settings(tenant_db: Session) -> dict[str, Any]:
    """Load company settings from DB, returning defaults if missing."""
    defaults: dict[str, Any] = {
        "company_name": "",
        "phone": "",
        "timezone": "America/New_York",
        "address": "",
        "term_job": "Job",
        "term_customer": "Customer",
        "term_technician": "Technician",
        "term_estimate": "Estimate",
        "term_invoice": "Invoice",
        "term_dispatcher": "Dispatcher",
        "notif_new_job_email": False,
        "notif_new_job_sms": False,
        "notif_job_completed_email": False,
        "notif_job_completed_sms": False,
        "notif_new_customer_email": False,
        "notif_payment_received_email": False,
        "webhook_url": "",
    }
    try:
        row = tenant_db.execute(
            text("SELECT * FROM company_settings LIMIT 1")
        ).mappings().first()
        if row:
            for k in defaults:
                if k in row and row[k] is not None:
                    defaults[k] = row[k]
    except Exception:
        logging.getLogger(__name__).exception("_load_settings caught exception")
        pass
    return defaults


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    tenant_db: Session = Depends(get_db),
) -> HTMLResponse:
    current_user = _require_auth(request)
    settings = _load_settings(tenant_db)
    ctx = _base_ctx(request, current_user)
    ctx["settings"] = settings
    return templates.TemplateResponse("tenant_settings.html", ctx)


@router.post("/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    company_name: str = Form(""),
    phone: str = Form(""),
    timezone: str = Form("America/New_York"),
    address: str = Form(""),
    term_job: str = Form("Job"),
    term_customer: str = Form("Customer"),
    term_technician: str = Form("Technician"),
    term_estimate: str = Form("Estimate"),
    term_invoice: str = Form("Invoice"),
    term_dispatcher: str = Form("Dispatcher"),
    notif_new_job_email: str = Form(""),
    notif_new_job_sms: str = Form(""),
    notif_job_completed_email: str = Form(""),
    notif_job_completed_sms: str = Form(""),
    notif_new_customer_email: str = Form(""),
    notif_payment_received_email: str = Form(""),
    webhook_url: str = Form(""),
    tenant_db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_auth(request)

    # Validate webhook URL
    if webhook_url and not webhook_url.startswith("https://"):
        raise HTTPException(status_code=422, detail="Webhook URL must use HTTPS")

    payload = {
        "company_name": company_name.strip(),
        "phone": phone.strip(),
        "timezone": timezone,
        "address": address.strip(),
        "term_job": term_job.strip() or "Job",
        "term_customer": term_customer.strip() or "Customer",
        "term_technician": term_technician.strip() or "Technician",
        "term_estimate": term_estimate.strip() or "Estimate",
        "term_invoice": term_invoice.strip() or "Invoice",
        "term_dispatcher": term_dispatcher.strip() or "Dispatcher",
        "notif_new_job_email": bool(notif_new_job_email),
        "notif_new_job_sms": bool(notif_new_job_sms),
        "notif_job_completed_email": bool(notif_job_completed_email),
        "notif_job_completed_sms": bool(notif_job_completed_sms),
        "notif_new_customer_email": bool(notif_new_customer_email),
        "notif_payment_received_email": bool(notif_payment_received_email),
        "webhook_url": webhook_url.strip(),
    }

    try:
        # Upsert into company_settings
        existing = tenant_db.execute(
            text("SELECT id FROM company_settings LIMIT 1")
        ).fetchone()
        if existing:
            set_clause = ", ".join(f"{k} = :{k}" for k in payload)
            tenant_db.execute(
                text(f"UPDATE company_settings SET {set_clause} WHERE id = :_id"),
                {**payload, "_id": existing[0]},
            )
        else:
            cols = ", ".join(payload.keys())
            vals = ", ".join(f":{k}" for k in payload)
            tenant_db.execute(
                text(f"INSERT INTO company_settings ({cols}) VALUES ({vals})"),
                payload,
            )
        tenant_db.commit()
    except Exception as exc:
        logger.exception("Failed to save settings: %s", exc)
        tenant_db.rollback()
        raise HTTPException(status_code=500, detail="Failed to save settings") from None

    return RedirectResponse(url="/settings?saved=1", status_code=303)


# ── Team ──────────────────────────────────────────────────────────────────────

@router.get("/team", response_class=HTMLResponse)
def team_page(
    request: Request,
    tenant_db: Session = Depends(get_db),
) -> HTMLResponse:
    current_user = _require_auth(request)

    team_members: list[dict[str, Any]] = []
    try:
        rows = tenant_db.execute(
            text(
                "SELECT id, name, email, role, last_login, "
                "       CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END AS active "
                "FROM users ORDER BY role, name"
            )
        ).mappings().all()
        team_members = [dict(r) for r in rows]
    except Exception:
        logging.getLogger(__name__).exception("team_page caught exception")
        pass

    ctx = _base_ctx(request, current_user)
    ctx["team_members"] = team_members
    return templates.TemplateResponse("tenant_team.html", ctx)


@router.post("/team/invite")
async def team_invite(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    role: str = Form("dispatcher"),
    tenant_db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_auth(request)

    # Validate role
    valid_roles = {"admin", "dispatcher", "technician", "viewer"}
    if role not in valid_roles:
        raise HTTPException(status_code=422, detail=f"Invalid role: {role}")

    # Validate email
    email = email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Invalid email address")

    try:
        # Check for existing user
        existing = tenant_db.execute(
            text("SELECT id FROM users WHERE email = :email LIMIT 1"),
            {"email": email},
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="A user with this email already exists")

        # Insert invite record / pending user
        tenant_db.execute(
            text(
                "INSERT INTO users (name, email, role, active, invited_at) "
                "VALUES (:name, :email, :role, 0, datetime('now'))"
            ),
            {"name": name.strip(), "email": email, "role": role},
        )
        tenant_db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to invite user: %s", exc)
        tenant_db.rollback()
        # Try to send invite even if DB insert fails (graceful degradation)

    # Send invite email (best-effort)
    try:
        _send_invite_email(email=email, name=name.strip(), role=role, request=request)
    except Exception as exc:
        logger.warning("Invite email failed for %s: %s", email, exc)

    return RedirectResponse(url="/team?invited=1", status_code=303)


def _send_invite_email(email: str, name: str, role: str, request: Request) -> None:
    """Send invite email via configured provider. Stub — replace with real mailer."""
    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_name = tenant.get("name", "DispatchApp")
    logger.info(
        "INVITE EMAIL → %s (name=%r, role=%s, tenant=%s)",
        email,
        name,
        role,
        tenant_name,
    )
    # from gdx_dispatch.core.email import send_email
    # send_email(
    #     to=email,
    #     subject=f"You've been invited to {tenant_name} on DispatchApp",
    #     template="invite",
    #     context={"name": name, "role": role, "tenant_name": tenant_name},
    # )


# ── Billing ───────────────────────────────────────────────────────────────────

@router.get("/billing")
def billing_portal(request: Request) -> RedirectResponse:
    _require_auth(request)

    # Build Stripe billing portal URL
    stripe_portal_url = _get_stripe_portal_url(request)
    if not stripe_portal_url:
        # Fallback: show a static billing page or error
        raise HTTPException(
            status_code=503,
            detail="Billing portal is not configured. Contact support.",
        )
    return RedirectResponse(url=stripe_portal_url, status_code=302)


def _get_stripe_portal_url(request: Request) -> str | None:
    """Create a Stripe billing portal session and return the URL."""
    stripe_secret = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe_secret:
        return None

    try:
        import stripe  # type: ignore

        stripe.api_key = stripe_secret

        tenant = getattr(request.state, "tenant", {}) or {}
        customer_id = tenant.get("stripe_customer_id", "")
        if not customer_id:
            return None

        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=str(request.url_for("dashboard")),
        )
        return session.url
    except ImportError:
        logger.warning("stripe package not installed; billing portal unavailable")
        return None
    except Exception as exc:
        logger.exception("Stripe portal error: %s", exc)
        return None
