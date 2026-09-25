"""
Tenant onboarding wizard — GDX (single-tenant, self-hosted). The
``tenant_id`` arguments below address the one tenant this deployment
serves; they are not a cross-tenant selector.

Provides:
  - OnboardingStep dataclass (in-memory / Redis-backed state)
  - Six wizard steps: company_info, first_technician, service_area,
    first_job_type, payment_setup, branding
  - get_onboarding_status(tenant_id)  -> list[OnboardingStepState]
  - complete_step(tenant_id, step_name) -> OnboardingStepState
  - get_next_step(tenant_id)          -> str | None
  - is_onboarding_complete(tenant_id) -> bool
  - API router  (GET /onboarding, mounted under /api — read the caveat below)

The Flask-era Jinja wizard that used to live here — ``ui_router``, its
``GET /onboarding``, ``GET /onboarding/{step}`` and ``POST /onboarding/{step}``
HTML routes, and the form validators behind them — was deleted 2026-09-24
(GDXA-24). Nothing had ever mounted ``ui_router`` (``app.py`` imported it and
never included it), so no request could reach any of it; the Vue SPA's
``/onboarding`` route owns that surface. The ``templates/onboarding.html`` page
those routes rendered belongs to documents-media and is removed separately under
GDXA-25 — as of this commit the file is still in the tree, but nothing renders
it.

The state functions below are unchanged and ``GET /api/onboarding`` is built on
them. Do NOT read that as "the live onboarding surface": grep finds **no caller
of bare GET /api/onboarding anywhere** — not in ``frontend/src``, not in the
mobile routers, not in the MCP tools. What the app actually uses is
``gdx_dispatch/routers/onboarding.py`` (``/api/onboarding/state|step|complete|
checklist``, DB-backed, modelling a different six steps), and
``OnboardingView.vue`` calls ``/api/onboarding/complete`` on that router. This
module's Redis-or-process-memory state is a parallel implementation of the same
idea, so it is a candidate for the same treatment the wizard just got. That is a
separate decision and is NOT made here: GDXA-24's brief required this route to
stay mounted, and a guard now holds it there.
Guard: ``gdx_dispatch/tests/test_onboarding_jinja_wizard_retired.py``.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text  # noqa: F401 – available for callers

logger = logging.getLogger(__name__)

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


# ── Request helpers ────────────────────────────────────────────────────────────

def _tenant_id_from_request(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None)
    if not isinstance(tenant, dict):
        return "unknown"
    return str(tenant.get("id", "unknown"))


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
