"""
Tenant onboarding wizard — GDX multi-tenant SaaS.

Provides:
  - OnboardingStep dataclass (in-memory / Redis-backed state)
  - Six wizard steps: company_info, first_technician, service_area,
    first_job_type, payment_setup, branding
  - get_onboarding_status(tenant_id)  -> list[OnboardingStepState]
  - complete_step(tenant_id, step_name) -> OnboardingStepState
  - get_next_step(tenant_id)          -> str | None
  - is_onboarding_complete(tenant_id) -> bool
  - API router  (GET /onboarding)
  - UI router   (GET /onboarding,
                 GET /onboarding/{step},
                 POST /onboarding/{step})
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text  # noqa: F401 – available for callers
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db

logger = logging.getLogger(__name__)

# ── Template setup ─────────────────────────────────────────────────────────────
_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

# ── Ordered wizard steps ───────────────────────────────────────────────────────
WIZARD_STEPS: list[str] = [
    "company_info",
    "first_technician",
    "service_area",
    "first_job_type",
    "payment_setup",
    "branding",
]

STEP_TITLES: dict[str, str] = {
    "company_info": "Company Information",
    "first_technician": "Add Your First Technician",
    "service_area": "Define Service Area",
    "first_job_type": "Set Up Job Types",
    "payment_setup": "Payment Setup",
    "branding": "Branding & Appearance",
}

# TTL for onboarding state in Redis (30 days)
_ONBOARDING_TTL = 30 * 24 * 60 * 60

# ── Redis helper (optional — falls back to in-memory dict) ────────────────────
_mem_store: dict[str, dict[str, Any]] = {}


def _get_redis():
    """Return a Redis client or None if unavailable."""
    try:
        from redis import from_url as redis_from_url
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        client = redis_from_url(url, decode_responses=True, socket_connect_timeout=1)
        client.ping()
        return client
    except Exception:  # returns None if redis is unavailable
        logging.getLogger(__name__).exception("_get_redis caught exception")
        return None


def _redis_key(tenant_id: str) -> str:
    return f"onboarding:{tenant_id}"


def _load_state(tenant_id: str) -> dict[str, Any]:
    """Load per-tenant onboarding state (Redis preferred, in-memory fallback)."""
    redis = _get_redis()
    if redis is not None:
        try:
            raw = redis.get(_redis_key(tenant_id))
            if raw:
                return json.loads(raw)
        except Exception:
            logging.getLogger(__name__).exception("_load_state caught exception")
            pass
    return _mem_store.get(tenant_id, {})


def _save_state(tenant_id: str, state: dict[str, Any]) -> None:
    """Persist per-tenant onboarding state."""
    redis = _get_redis()
    if redis is not None:
        try:
            redis.set(_redis_key(tenant_id), json.dumps(state), ex=_ONBOARDING_TTL)
            return
        except Exception:
            logging.getLogger(__name__).exception("_save_state caught exception")
            pass
    _mem_store[tenant_id] = state


# ── Domain model ───────────────────────────────────────────────────────────────

@dataclass
class OnboardingStepState:
    """State of a single onboarding wizard step for a tenant."""
    step_name: str
    title: str
    is_complete: bool
    completed_at: str | None = None   # ISO-8601 string when completed
    position: int = 0                    # 0-based index in wizard


@dataclass
class OnboardingStep:
    """Legacy-compatible dataclass (kept for test_13 compatibility)."""
    key: str
    title: str
    complete: bool
    action_url: str | None = None


# ── Core logic ─────────────────────────────────────────────────────────────────

def get_onboarding_status(tenant_id: str) -> list[OnboardingStepState]:
    """Return all wizard steps with current completion status for *tenant_id*."""
    state = _load_state(tenant_id)
    steps: list[OnboardingStepState] = []
    for idx, name in enumerate(WIZARD_STEPS):
        entry = state.get(name, {})
        steps.append(
            OnboardingStepState(
                step_name=name,
                title=STEP_TITLES[name],
                is_complete=bool(entry.get("is_complete", False)),
                completed_at=entry.get("completed_at"),
                position=idx,
            )
        )
    return steps


def complete_step(tenant_id: str, step_name: str) -> OnboardingStepState:
    """Mark *step_name* as complete for *tenant_id* and return its updated state."""
    if step_name not in WIZARD_STEPS:
        raise ValueError(f"Unknown onboarding step: {step_name!r}")
    state = _load_state(tenant_id)
    now = datetime.now(timezone.utc).isoformat()
    state[step_name] = {"is_complete": True, "completed_at": now}
    _save_state(tenant_id, state)
    idx = WIZARD_STEPS.index(step_name)
    return OnboardingStepState(
        step_name=step_name,
        title=STEP_TITLES[step_name],
        is_complete=True,
        completed_at=now,
        position=idx,
    )


def get_next_step(tenant_id: str) -> str | None:
    """Return the name of the next incomplete step, or None if all done."""
    state = _load_state(tenant_id)
    for name in WIZARD_STEPS:
        if not state.get(name, {}).get("is_complete", False):
            return name
    return None


def is_onboarding_complete(tenant_id: str) -> bool:
    """Return True when every wizard step is marked complete."""
    state = _load_state(tenant_id)
    return all(
        state.get(name, {}).get("is_complete", False) for name in WIZARD_STEPS
    )


def reset_onboarding(tenant_id: str) -> None:
    """Clear all onboarding state for a tenant (useful in tests / re-onboarding)."""
    redis = _get_redis()
    if redis is not None:
        with contextlib.suppress(Exception):
            redis.delete(_redis_key(tenant_id))
    _mem_store.pop(tenant_id, None)


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _get_current_user(request: Request) -> dict[str, Any] | None:
    return getattr(request.state, "current_user", None)


def _require_auth(request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        # 302 redirect to login — documented in route `responses` below
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/auth/login"},
        )
    return user


def _tenant_id_from_request(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None)
    if not isinstance(tenant, dict):
        return "unknown"
    return str(tenant.get("id", "unknown"))


# ── Annotated dependency / form aliases ───────────────────────────────────────

TenantDB = Annotated[Session, Depends(get_db)]
OptForm = Annotated[str | None, Form(None)]

# ── API router (JSON) — registered at /api prefix in app.py ──────────────────

router = APIRouter()


@router.get("/onboarding")
def get_onboarding_api(request: Request) -> dict:
    """JSON endpoint: returns wizard progress for the current tenant."""
    try:
        tenant_id = _tenant_id_from_request(request)
        steps = get_onboarding_status(tenant_id)
        complete_count = sum(1 for s in steps if s.is_complete)
        total = len(steps)
        return {
            "tenant_id": tenant_id,
            "total": total,
            "complete": complete_count,
            "percent": round(complete_count / total * 100) if total else 0,
            "is_complete": is_onboarding_complete(tenant_id),
            "next_step": get_next_step(tenant_id),
            "steps": [
                {
                    "step_name": s.step_name,
                    "title": s.title,
                    "is_complete": s.is_complete,
                    "completed_at": s.completed_at,
                    "position": s.position,
                }
                for s in steps
            ],
        }
    except Exception:
        logger.exception("get_onboarding_api failed")
        return {
            "tenant_id": "unknown",
            "total": len(WIZARD_STEPS),
            "complete": 0,
            "percent": 0,
            "is_complete": False,
            "next_step": WIZARD_STEPS[0],
            "steps": [],
        }


# ── Wizard form helpers ────────────────────────────────────────────────────────

@dataclass
class WizardFormData:
    """All possible form fields across all wizard steps.

    Consolidating them here keeps the POST handler under the 13-parameter limit.
    """
    company_name: str | None
    industry: str | None
    timezone_name: str | None   # "timezone" is a stdlib name — aliased
    tech_name: str | None
    tech_email: str | None
    tech_phone: str | None
    tech_role: str | None
    zip_codes: str | None
    service_radius: str | None
    job_types: str | None
    primary_color: str | None
    secondary_color: str | None


def _validate_company_info(data: WizardFormData) -> list[str]:
    if not (data.company_name or "").strip():
        return ["Company name is required."]
    return []


def _validate_first_technician(data: WizardFormData) -> list[str]:
    errors = []
    if not (data.tech_name or "").strip():
        errors.append("Technician name is required.")
    if not (data.tech_email or "").strip():
        errors.append("Technician email is required.")
    return errors


def _validate_service_area(data: WizardFormData) -> list[str]:
    has_zips = bool((data.zip_codes or "").strip())
    has_radius = bool((data.service_radius or "").strip())
    if not has_zips and not has_radius:
        return ["Enter at least one ZIP code or a service radius."]
    return []


def _validate_first_job_type(data: WizardFormData) -> list[str]:
    if not (data.job_types or "").strip():
        return ["Select at least one job type."]
    return []


_STEP_VALIDATORS = {
    "company_info": _validate_company_info,
    "first_technician": _validate_first_technician,
    "service_area": _validate_service_area,
    "first_job_type": _validate_first_job_type,
}


def _validate_step(step: str, data: WizardFormData) -> list[str]:
    """Return validation error strings for *step* given submitted *data*."""
    validator = _STEP_VALIDATORS.get(step)
    if validator is None:
        return []
    return validator(data)


def _step_template_ctx(
    request: Request,
    current_user: dict[str, Any],
    step: str,
    steps: list[OnboardingStepState],
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Build the common Jinja2 template context for a wizard step page."""
    step_obj = next(s for s in steps if s.step_name == step)
    ctx: dict[str, Any] = {
        "request": request,
        "current_user": current_user,
        "step": step,
        "step_title": STEP_TITLES[step],
        "step_obj": step_obj,
        "steps": steps,
        "wizard_steps": WIZARD_STEPS,
        "step_titles": STEP_TITLES,
        "current_idx": WIZARD_STEPS.index(step),
        "total_steps": len(WIZARD_STEPS),
        "flash_messages": getattr(request.state, "flash_messages", []),
    }
    if errors is not None:
        ctx["errors"] = errors
    return ctx


# ── UI router (HTML wizard) — registered separately in app.py ─────────────────

_REDIRECT_302_RESPONSES = {302: {"description": "Redirect to login or next step"}}
_NOT_FOUND_RESPONSES = {404: {"description": "Unknown onboarding step"}}

ui_router = APIRouter(tags=["onboarding-ui"])


@ui_router.get(
    "/onboarding",
    response_class=HTMLResponse,
    responses=_REDIRECT_302_RESPONSES,
)
def onboarding_index(request: Request) -> Any:
    """Redirect to the current (next incomplete) wizard step."""
    _require_auth(request)
    tenant_id = _tenant_id_from_request(request)
    next_step = get_next_step(tenant_id)
    if next_step is None:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url=f"/onboarding/{next_step}", status_code=302)


@ui_router.get(
    "/onboarding/{step}",
    response_class=HTMLResponse,
    responses={**_REDIRECT_302_RESPONSES, **_NOT_FOUND_RESPONSES},
)
def onboarding_step_get(step: str, request: Request) -> HTMLResponse:
    """Render the wizard HTML for *step*."""
    current_user = _require_auth(request)
    if step not in WIZARD_STEPS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown onboarding step: {step!r}",
        )
    tenant_id = _tenant_id_from_request(request)
    steps = get_onboarding_status(tenant_id)
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        _step_template_ctx(request, current_user, step, steps),
    )


async def _wizard_form(request: Request) -> WizardFormData:
    """FastAPI dependency: parse all wizard form fields from the raw request body."""
    form = await request.form()

    def _get(key: str) -> str | None:
        val = form.get(key)
        return str(val) if val is not None else None

    return WizardFormData(
        company_name=_get("company_name"),
        industry=_get("industry"),
        timezone_name=_get("timezone"),
        tech_name=_get("tech_name"),
        tech_email=_get("tech_email"),
        tech_phone=_get("tech_phone"),
        tech_role=_get("tech_role"),
        zip_codes=_get("zip_codes"),
        service_radius=_get("service_radius"),
        job_types=_get("job_types"),
        primary_color=_get("primary_color"),
        secondary_color=_get("secondary_color"),
    )


WizardForm = Annotated[WizardFormData, Depends(_wizard_form)]


@ui_router.post(
    "/onboarding/{step}",
    response_class=HTMLResponse,
    responses={**_REDIRECT_302_RESPONSES, **_NOT_FOUND_RESPONSES},
)
async def onboarding_step_post(
    step: str,
    request: Request,
    form_data: WizardForm,
) -> Any:
    """Save the submitted wizard step and advance to the next one."""
    current_user = _require_auth(request)
    if step not in WIZARD_STEPS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown onboarding step: {step!r}",
        )

    tenant_id = _tenant_id_from_request(request)
    errors = _validate_step(step, form_data)

    if errors:
        steps = get_onboarding_status(tenant_id)
        return templates.TemplateResponse(
            request,
            "onboarding.html",
            _step_template_ctx(request, current_user, step, steps, errors=errors),
            status_code=422,
        )

    complete_step(tenant_id, step)
    next_step = get_next_step(tenant_id)
    if next_step is None:
        return RedirectResponse(url="/dashboard?onboarded=1", status_code=303)
    return RedirectResponse(url=f"/onboarding/{next_step}", status_code=303)
