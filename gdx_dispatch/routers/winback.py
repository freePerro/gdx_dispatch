"""
Winback router — inactive customer re-engagement campaigns + follow-up queue.

Two connected features:
1. Win-back campaigns: identify customers with no jobs in N months, enqueue SMS/email sends.
2. Follow-ups queue: scheduled follow-up tasks on estimates / invoices / customers.

Pattern mirrors gdx_dispatch/routers/collections.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import contained_read, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["winback"],
    dependencies=[Depends(require_module("customers"))],
)


CAMPAIGN_STATUSES = ("draft", "sent", "cancelled")
CAMPAIGN_CHANNELS = ("sms", "email")
FOLLOWUP_STATUSES = ("open", "completed", "cancelled")
FOLLOWUP_ENTITY_TYPES = ("estimate", "invoice", "customer")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import FollowUp, WinbackCampaign, WinbackSend  # noqa: E402

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class WinbackCampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    channel: str = Field(default="sms", pattern=r"^(sms|email)$")
    subject: str | None = Field(default=None, max_length=200)
    body_template: str = Field(min_length=1, max_length=10000)
    inactivity_months: int = Field(default=6, ge=1, le=60)


class SendCampaignIn(BaseModel):
    override_customer_ids: list[str] | None = Field(default=None, max_length=5000)


class FollowUpIn(BaseModel):
    entity_type: str = Field(pattern=r"^(estimate|invoice|customer)$")
    entity_id: str = Field(min_length=1, max_length=64)
    assigned_to: str | None = Field(default=None, max_length=200)
    due_date: datetime
    note: str | None = Field(default=None, max_length=2000)


class BulkFollowUpIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_id(request: Request) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: Any) -> str:
    if not isinstance(user, dict):
        return "system"
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "system")


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("winback_audit_failed action=%s entity_id=%s", action, entity_id)
        try:
            db.rollback()
        except Exception:
            log.exception("winback_audit_rollback_failed")


def _serialize_campaign(c: WinbackCampaign) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "company_id": c.company_id,
        "name": c.name,
        "status": c.status,
        "channel": c.channel,
        "subject": c.subject,
        "body_template": c.body_template,
        "inactivity_months": c.inactivity_months,
        "sent_at": c.sent_at.isoformat() if c.sent_at else None,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _serialize_followup(f: FollowUp) -> dict[str, Any]:
    return {
        "id": str(f.id),
        "company_id": f.company_id,
        "entity_type": f.entity_type,
        "entity_id": f.entity_id,
        "assigned_to": f.assigned_to,
        "due_date": f.due_date.isoformat() if f.due_date else None,
        "note": f.note,
        "status": f.status,
        "completed_at": f.completed_at.isoformat() if f.completed_at else None,
        "created_by": f.created_by,
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


def _get_scoped_campaign(db: Session, cid: UUID, tenant_id: str) -> WinbackCampaign:
    row = db.execute(
        select(WinbackCampaign).where(
            WinbackCampaign.id == cid,
            WinbackCampaign.company_id == tenant_id,
            WinbackCampaign.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return row


def _get_scoped_followup(db: Session, fid: UUID, tenant_id: str) -> FollowUp:
    row = db.execute(
        select(FollowUp).where(
            FollowUp.id == fid,
            FollowUp.company_id == tenant_id,
            FollowUp.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Follow-up not found")
    return row


def _query_candidates(db: Session, tenant_id: str, months: int) -> list[dict[str, Any]]:
    """Return customers with no jobs in the last N months.

    Reads existing customers/jobs tables via raw SQL with bind params. If the tables
    don't exist (e.g. test DB without those tables), logs and returns [].

    That `[]` is the right answer for a screen and the wrong one for a send, so
    the read itself lives in `_query_candidates_checked`, which says which `[]`
    it is. Use this wrapper only where "no candidates" and "we could not tell"
    lead to the same behaviour — i.e. displaying a list or a count.
    """
    return _query_candidates_checked(db, tenant_id, months)[1]


def _query_candidates_checked(
    db: Session, tenant_id: str, months: int
) -> tuple[bool, list[dict[str, Any]]]:
    """`(read_succeeded, rows)` — customers with no jobs in the last N months.

    Two separate defects met at this read, and the fix needs both halves.

    **The transaction (GDXA-162).** The session belongs to whichever route called
    us, and on Postgres a failed statement aborts the WHOLE transaction — so the
    swallow below handed its caller an empty list on a session that could no
    longer commit, and the request died later at `db.commit()` naming
    `winback_campaigns`, a table this query never mentions. `contained_read`
    opens a SAVEPOINT on the connection, below the ORM, so the failure costs the
    caller nothing but this answer. Reads only — `contained_read` rule 2.

    **The answer.** Containing the damage is what *exposed* the second defect.
    The poisoned transaction had been protecting `send_campaign` by accident: its
    `commit()` blew up, which was loud, and left the campaign retryable in
    `draft`. Contained, that commit succeeds — the campaign flips `draft -> sent`
    having queued nothing, and `_audit` records `winback_campaign_sent` with
    `enqueued: 0`. So `send_campaign` refuses instead: 503 here (the flag below),
    409 at the write. Read its comment for why the guard belongs at the write.

    Two things this tuple does NOT fix, both real:

    - `winback_stats` still calls `_query_candidates`, so a degraded read reaches
      the operator as `candidates_count: 0` — rendered "0" by WinbackView,
      indistinguishable from "nobody is inactive", on the tile that decides
      whether a campaign gets built. Telling it apart needs a response field and
      the view to render it; an API field no screen reads is not a fix.
    - Nothing CONSUMES `winback_sends`: one writer (this router), no reader, no
      beat task. So `enqueued: N` means "N rows written", never "N customers
      reached", for every N and not only N=0. Be precise about the gap, because
      the first draft of this comment said "no transport since Twilio was
      dropped" and that is wrong in a decision-shaping way: a live SMS sender
      exists (`modules/phone_com/client.py`'s `send_message`, reached by
      `POST /api/phone-com/messages`) and so does
      `core/transactional_email.py`'s `send_transactional_email`. What is missing
      is the drainer between them and this table, not the transport. Recorded at
      `FOUND_NOT_FILED.md` 2026-09-20, still open.
    """
    cutoff = utcnow() - timedelta(days=int(months) * 30)
    sql = text(
        """
        SELECT c.id AS id, c.name AS name, c.email AS email, c.phone AS phone,
               MAX(j.created_at) AS last_job_date
          FROM customers c
          LEFT JOIN jobs j
                 ON j.customer_id = c.id
                AND j.deleted_at IS NULL
         WHERE c.company_id = :tenant_id
           AND c.deleted_at IS NULL
         GROUP BY c.id, c.name, c.email, c.phone
        HAVING MAX(j.created_at) IS NULL OR MAX(j.created_at) < :cutoff
         ORDER BY c.name
         LIMIT 5000
        """
    )
    try:
        # SAVEPOINT inside the try, never around it (rules 1 and 5). Be exact
        # about why, because the loose version of this ("the handlers below
        # never run") is wrong and was measured wrong here: with the savepoint
        # around the try, the handler DOES run and DOES return — and then
        # `__exit__` issues RELEASE SAVEPOINT on an already-aborted transaction,
        # which raises 25P02 out of the `with` anyway. Same 500, different line.
        # `.all()` materialises inside the block on purpose — no cursor outlives it.
        with contained_read(db):
            rows = db.execute(sql, {"tenant_id": tenant_id, "cutoff": cutoff}).mappings().all()
    except (OperationalError, ProgrammingError):  # degraded: tables missing, or a schema mismatch
        log.exception("winback_candidates_query_failed tenant=%s", tenant_id)
        return False, []
    except Exception:  # degraded: anything else, including a statement timeout
        log.exception("winback_candidates_unexpected_error tenant=%s", tenant_id)
        return False, []

    out: list[dict[str, Any]] = []
    for r in rows:
        last = r.get("last_job_date")
        out.append(
            {
                "id": str(r.get("id")),
                "name": r.get("name"),
                "email": r.get("email"),
                "phone": r.get("phone"),
                "last_job_date": last.isoformat() if hasattr(last, "isoformat") else last,
            }
        )
    return True, out


# ---------------------------------------------------------------------------
# Winback endpoints
# ---------------------------------------------------------------------------


@router.get("/api/winback/candidates", response_model=None)
def list_candidates(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    months: int = 6,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    months = max(1, min(int(months or 6), 60))
    return _query_candidates(db, tenant_id, months)


@router.get("/api/winback/campaigns", response_model=None)
def list_campaigns(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(WinbackCampaign)
        .where(
            WinbackCampaign.deleted_at.is_(None),
        )
        .order_by(WinbackCampaign.created_at.desc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize_campaign(c) for c in rows]


@router.post("/api/winback/campaigns", response_model=None, status_code=201)
def create_campaign(
    payload: WinbackCampaignIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    c = WinbackCampaign(
        company_id=tenant_id,
        name=payload.name.strip(),
        status="draft",
        channel=payload.channel,
        subject=payload.subject,
        body_template=payload.body_template,
        inactivity_months=payload.inactivity_months,
        created_by=_user_id(user),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="winback_campaign_created",
        entity_type="winback_campaign",
        entity_id=str(c.id),
        details={"name": c.name, "channel": c.channel},
        request=request,
    )
    return _serialize_campaign(c)


@router.get("/api/winback/stats", response_model=None)
def winback_stats(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    all_campaigns = db.execute(
        select(WinbackCampaign).where(
            WinbackCampaign.deleted_at.is_(None),
        )
    ).scalars().all()
    total_campaigns = len(all_campaigns)
    active = sum(1 for c in all_campaigns if c.status == "draft")
    cutoff_30 = utcnow() - timedelta(days=30)
    sent_last_30d = sum(
        1 for c in all_campaigns if c.sent_at is not None and c.sent_at >= cutoff_30
    )
    candidates_count = len(_query_candidates(db, tenant_id, 6))
    return {
        "total_campaigns": total_campaigns,
        "active": active,
        "sent_last_30d": sent_last_30d,
        "candidates_count": candidates_count,
    }


@router.post("/api/winback/campaigns/{campaign_id}/send", response_model=None)
def send_campaign(
    campaign_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    payload: SendCampaignIn | None = None,
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    c = _get_scoped_campaign(db, campaign_id, tenant_id)
    if c.status != "draft":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot send campaign in status '{c.status}'",
        )

    # Determine target customer IDs
    target_ids: list[str] = []
    if payload and payload.override_customer_ids:
        target_ids = list(payload.override_customer_ids)
    else:
        # 503 and not 409 because these are different answers to the operator:
        # "we could not find out" is retryable, "nobody qualifies" is not. Only
        # the checked variant can tell them apart — the bare `[]` cannot, which
        # is the whole reason it exists. Note the detail string below does NOT
        # reach the operator: useApi.js maps every status >= 500 to a generic
        # "Something went wrong / Please try again." That is accurate advice
        # here, and the win is the state, not the wording — the campaign stays a
        # retryable draft with no audit row claiming it was sent.
        ok, candidates = _query_candidates_checked(db, tenant_id, c.inactivity_months)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail="Could not read the win-back candidate list — campaign not sent. "
                       "It is still a draft; try again.",
            )
        target_ids = [cand["id"] for cand in candidates]

    enqueued = 0
    for cid in target_ids:
        try:
            customer_uuid = UUID(str(cid))
        except (ValueError, TypeError):
            log.exception("winback_send_invalid_customer_id cid=%s", cid)
            continue
        send = WinbackSend(
            company_id=tenant_id,
            campaign_id=c.id,
            customer_id=customer_uuid,
            channel=c.channel,
            status="queued",
        )
        db.add(send)
        enqueued += 1

    # The invariant lives HERE, at the write, not at any one read: a campaign must
    # never reach `sent` having queued nothing. GDXA-162's audit measured four
    # entrances to that state and only one of them is a failed read — the others
    # are a readable-but-empty candidate list and an override list the UUID loop
    # drops (malformed, or every id invalid). Guarding the read alone fixed one of
    # four. 409 rather than 503 because the detail below reaches the operator
    # (useApi.js surfaces a 4xx message and genericises >= 500).
    #
    # Scope, so this is not mistaken for more than it is. It blocks the literal
    # zero case and nothing else, so it does NOT make the audit row true:
    #   - `enqueued > 0` does not mean anyone was contacted. Nothing consumes
    #     `winback_sends` (no reader, no beat task — FOUND_NOT_FILED.md
    #     2026-09-20, still open). The transports exist; the drainer does not.
    #   - override ids are not checked for existence or tenant. There is no FK on
    #     `winback_sends.customer_id`, so a well-formed UUID matching no customer
    #     counts toward `enqueued` and the audit row reports it.
    # Both are named on GDXA-162 rather than fixed here.
    # `shorter`, not longer: `inactivity_months` is how long they must have been
    # quiet, so a LONGER window is stricter and matches fewer people. The first
    # draft of this string said "longer" and was measured backwards by the
    # GDXA-162 audit (candidates by window, 1/6/12/24/60 → 288/242/94/94/94 on
    # prod). It is the only sentence in this change an operator ever reads.
    if not enqueued:
        raise HTTPException(
            status_code=409,
            detail="Nobody matched, so nothing was queued and the campaign is "
                   "still a draft. Try a campaign with a shorter inactivity "
                   "window, which matches more customers.",
        )

    c.status = "sent"
    c.sent_at = utcnow()
    db.commit()
    db.refresh(c)

    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="winback_campaign_sent",
        entity_type="winback_campaign",
        entity_id=str(c.id),
        details={"enqueued": enqueued},
        request=request,
    )
    return {"campaign_id": str(c.id), "enqueued": enqueued}


# ---------------------------------------------------------------------------
# Follow-up endpoints
# ---------------------------------------------------------------------------


@router.get("/api/follow-ups", response_model=None)
def list_follow_ups(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = None,
    assigned_to: str | None = None,
    entity_type: str | None = None,
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(FollowUp).where(
        FollowUp.deleted_at.is_(None),
    )
    stmt = stmt.where(FollowUp.status == status) if status else stmt.where(FollowUp.status == "open")
    if assigned_to:
        stmt = stmt.where(FollowUp.assigned_to == assigned_to)
    if entity_type:
        stmt = stmt.where(FollowUp.entity_type == entity_type)
    stmt = stmt.order_by(FollowUp.due_date.asc())
    rows = db.execute(stmt).scalars().all()
    return [_serialize_followup(f) for f in rows]


@router.post("/api/follow-ups", response_model=None, status_code=201)
def create_follow_up(
    payload: FollowUpIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    f = FollowUp(
        company_id=tenant_id,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        assigned_to=payload.assigned_to,
        due_date=payload.due_date,
        note=payload.note,
        status="open",
        created_by=_user_id(user),
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="follow_up_created",
        entity_type="follow_up",
        entity_id=str(f.id),
        details={"entity_type": f.entity_type, "entity_id": f.entity_id},
        request=request,
    )
    return _serialize_followup(f)


@router.post("/api/follow-ups/{follow_up_id}/complete", response_model=None)
def complete_follow_up(
    follow_up_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    f = _get_scoped_followup(db, follow_up_id, tenant_id)
    f.status = "completed"
    f.completed_at = utcnow()
    db.commit()
    db.refresh(f)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="follow_up_completed",
        entity_type="follow_up",
        entity_id=str(f.id),
        request=request,
    )
    return _serialize_followup(f)


@router.post("/api/follow-ups/bulk-send", response_model=None)
def bulk_complete_follow_ups(
    payload: BulkFollowUpIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    now = utcnow()
    completed = 0
    for raw in payload.ids:
        try:
            fid = UUID(str(raw))
        except (ValueError, TypeError):
            log.exception("follow_up_bulk_invalid_id id=%s", raw)
            continue
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        row = db.execute(
            select(FollowUp).where(
                FollowUp.id == fid,
                FollowUp.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not row:
            continue
        row.status = "completed"
        row.completed_at = now
        completed += 1
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="follow_up_bulk_completed",
        entity_type="follow_up",
        entity_id="",
        details={"completed": completed, "requested": len(payload.ids)},
        request=request,
    )
    return {"completed": completed, "requested": len(payload.ids)}


@router.delete("/api/follow-ups/{follow_up_id}", response_model=None, status_code=204)
def delete_follow_up(
    follow_up_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    f = _get_scoped_followup(db, follow_up_id, tenant_id)
    f.deleted_at = utcnow()
    f.status = "cancelled"
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="follow_up_deleted",
        entity_type="follow_up",
        entity_id=str(follow_up_id),
        request=request,
    )
    return None
