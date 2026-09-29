"""The link between a lead and its estimates.

An estimate made for a lead carries ``estimates.lead_id`` (many per lead).
Accepting one wins the lead; the lead remembers which estimate counts in
``leads.selected_estimate_id`` (written on the FIRST accept, then only by
staff). Two entry points:

* ``resolve_lead_link`` — the gate a client-chosen ``estimates.lead_id`` goes
  through (create). Refuses without ``leads.write``, a live lead, and a
  customer that is the lead's. Duplicate copies an existing link under the
  same ``leads.write`` check; Start estimate sets it for the lead it is on.
* ``mark_lead_won_for_estimate`` — called after each of the five places an
  estimate becomes ``accepted`` has COMMITTED. It commits its own small
  change and never raises into the accept: a failure rolls back only the
  lead change, is logged, and leaves an audit row.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import audit_best_effort, log_audit_event_sync, utcnow
from gdx_dispatch.core.modules import has_permission
from gdx_dispatch.models.tenant_models import Lead

log = logging.getLogger(__name__)

WON = "won"


def resolve_lead_link(
    db: Session,
    request: Request,
    *,
    lead_id: UUID | None,
    customer_id: UUID | None,
) -> Lead | None:
    """Validate a client-supplied ``lead_id`` for a new estimate.

    Returns the lead (or None when no lead was asked for). A lead link must
    mean "this person's estimate", so the lead must already BE a customer
    (Start estimate and the Leads page's Create estimate both convert it
    first) and the estimate must be for that customer. An unconverted lead
    has no person to match, and linking it to anyone would let a stranger's
    accepted estimate win it (audit 2026-09-29).
    """
    if lead_id is None:
        return None
    if not has_permission(request, db, "leads.write"):
        raise HTTPException(status_code=403, detail="Linking an estimate to a lead needs leads.write")
    lead = db.execute(
        select(Lead).where(Lead.id == lead_id, Lead.deleted_at.is_(None))
    ).scalar_one_or_none()
    if lead is None:
        raise HTTPException(status_code=422, detail="lead not found")
    if lead.converted_customer_id is None:
        raise HTTPException(
            status_code=422,
            detail="Convert the lead to a customer before linking an estimate to it",
        )
    if customer_id is None or lead.converted_customer_id != customer_id:
        raise HTTPException(
            status_code=422,
            detail="This estimate's customer is not the lead's customer",
        )
    return lead


def lead_for_estimate(db: Session, estimate: Any, *, for_update: bool = False) -> Lead | None:
    lead_id = getattr(estimate, "lead_id", None)
    if lead_id is None:
        return None
    stmt = select(Lead).where(Lead.id == lead_id, Lead.deleted_at.is_(None))
    if for_update:
        # Two of the lead's estimates accepted at once (office + portal) must
        # not both see "no pick" and both write one. Postgres serializes on
        # the row; SQLite ignores FOR UPDATE and is single-writer anyway.
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def pick_is_live(db: Session, lead: Lead) -> bool:
    """The stored pick still names one of this lead's live estimates."""
    from gdx_dispatch.modules.proposals.models import Estimate

    if lead.selected_estimate_id is None:
        return False
    return db.execute(
        select(Estimate.id).where(
            Estimate.id == lead.selected_estimate_id,
            Estimate.lead_id == lead.id,
            Estimate.deleted_at.is_(None),
        )
    ).scalar_one_or_none() is not None


def mark_lead_won_for_estimate(
    db: Session,
    estimate: Any,
    *,
    actor: Any,
    tenant_id: str | None = None,
    request: Request | None = None,
) -> bool:
    """The estimate was just accepted (and committed). Win its lead.

    Sets ``selected_estimate_id`` when the lead has none, moves ``stage`` to
    ``won`` when it is not already, and writes one ``lead_won`` audit row —
    only when something changed, so a re-accept (``accept_tier`` on an
    accepted estimate) is a no-op. Returns True when the lead changed.
    Never raises.

    It ends its own transaction (commit or rollback), which expires every
    object in the caller's session — so it re-reads ``estimate`` before
    returning. A caller that returns the ORM object (``accept_tier`` does)
    would otherwise serialize an expired, empty row.
    """
    if getattr(estimate, "lead_id", None) is None:
        return False
    try:
        return _mark_lead_won(db, estimate, actor=actor, tenant_id=tenant_id, request=request)
    finally:
        try:
            db.refresh(estimate)
        except Exception:
            log.exception("lead_won_refresh_failed estimate=%s", getattr(estimate, "id", None))


def _mark_lead_won(
    db: Session,
    estimate: Any,
    *,
    actor: Any,
    tenant_id: str | None,
    request: Request | None,
) -> bool:
    lead_id = estimate.lead_id
    try:
        lead = lead_for_estimate(db, estimate, for_update=True)
        if lead is None:
            db.rollback()  # end the read; the caller's accept is already committed
            return False
        previous_stage = lead.stage
        previous_selected = lead.selected_estimate_id
        # An empty pick, or one naming a since-deleted estimate, is taken by
        # this accept — otherwise a deleted pick would leave the lead won with
        # nothing counting, forever.
        if not pick_is_live(db, lead):
            lead.selected_estimate_id = estimate.id
        if lead.stage != WON:
            lead.stage = WON
        changed = (
            lead.stage != previous_stage
            or lead.selected_estimate_id != previous_selected
        )
        if not changed:
            # Release the FOR UPDATE lock now, not at the caller's next commit
            # (job creation follows an accept).
            db.rollback()
            return False
        lead.updated_at = utcnow()
        log_audit_event_sync(
            db=db,
            tenant_id=tenant_id,
            user_id=str(actor) if actor is not None else None,
            action="lead_won",
            entity_type="lead",
            entity_id=str(lead.id),
            details={
                "estimate_id": str(estimate.id),
                "estimate_number": getattr(estimate, "estimate_number", None),
                "previous_stage": previous_stage,
                "selected_estimate_id": str(lead.selected_estimate_id),
                "selected_changed": lead.selected_estimate_id != previous_selected,
            },
            request=request,
        )
        db.commit()
        return True
    except Exception:
        log.exception("lead_won_failed estimate=%s lead=%s", getattr(estimate, "id", None), lead_id)
        try:
            db.rollback()
        except Exception:
            log.exception("lead_won_rollback_failed estimate=%s", getattr(estimate, "id", None))
        audit_best_effort(
            db,
            action="lead_won_failed",
            entity_type="lead",
            entity_id=str(lead_id),
            tenant_id=tenant_id,
            user_id=str(actor) if actor is not None else None,
            request=request,
            details={"estimate_id": str(getattr(estimate, "id", ""))},
        )
        return False
