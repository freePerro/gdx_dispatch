"""Planner — tasks, delegation, plans, and internal messaging."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import (
    Message,
    MessageThread,
    MessageThreadMember,
    Plan,
    PlannerTask,
    PlanStep,
)
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/planner", tags=["planner"], dependencies=[Depends(require_module("jobs"))])


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Tasks ────────────────────────────────────────────────────────────────────

class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=5000)
    # 2026-04-13: default changed medium → low per Doug's direction — if the
    # user doesn't think about it, the task is low. High/urgent are choices.
    priority: str = Field(default="low", pattern="^(low|medium|high|urgent)$")
    due_date: str | None = None
    assigned_to: str | None = None
    job_id: str | None = None
    customer_id: str | None = None
    # ── Call-capture fields (2026-07-07) ──
    contact_phone: str | None = Field(default=None, max_length=40)
    phone_com_call_id: str | None = Field(default=None, max_length=80)
    source: str | None = Field(default=None, max_length=20)


class TaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(todo|in-progress|done)$")
    priority: str | None = None
    due_date: str | None = None
    assigned_to: str | None = None
    job_id: str | None = None
    customer_id: str | None = None


_SORT_OPTIONS = {"needs_action", "newest", "oldest", "priority", "due_date"}
_BUCKET_OPTIONS = {"active", "completed", "all"}


@router.get("/tasks")
def list_tasks(
    request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db),
    view: str = "mine",
    status: str = "",
    date: str = "",
    sort: str = "newest",
    bucket: str = "active",
) -> dict:
    uid = _uid(user)
    if sort not in _SORT_OPTIONS:
        sort = "newest"
    if bucket not in _BUCKET_OPTIONS:
        bucket = "active"
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    q = select(PlannerTask)

    if view == "mine":
        q = q.where(
            (PlannerTask.assigned_to == uid)
            | ((PlannerTask.assigned_to.is_(None)) & (PlannerTask.created_by == uid))
        )
    elif view == "delegated":
        q = q.where(
            PlannerTask.created_by == uid,
            PlannerTask.assigned_to.isnot(None),
            PlannerTask.assigned_to != uid,
        )

    # bucket gates done vs active; explicit `status` param wins when supplied
    # (kept for back-compat with any callers that already pass status=).
    if status:
        q = q.where(PlannerTask.status == status)
    elif bucket == "active":
        q = q.where(PlannerTask.status != "done")
    elif bucket == "completed":
        q = q.where(PlannerTask.status == "done")

    priority_case = case(
        (PlannerTask.priority == "urgent", 0),
        (PlannerTask.priority == "high", 1),
        (PlannerTask.priority == "medium", 2),
        else_=3,
    )
    if sort == "needs_action":
        # Overdue + due-today rise to the top (earliest due first), then
        # everything with a due date, then undated, oldest-created first within
        # a bucket. Quick-captures are due today, so they never scroll away.
        q = q.order_by(
            PlannerTask.due_date.asc().nullslast(),
            PlannerTask.created_at.asc().nullslast(),
        )
    elif sort == "oldest":
        q = q.order_by(PlannerTask.created_at.asc().nullslast())
    elif sort == "priority":
        q = q.order_by(priority_case, PlannerTask.created_at.desc().nullslast())
    elif sort == "due_date":
        q = q.order_by(PlannerTask.due_date.asc().nullslast(), PlannerTask.created_at.desc().nullslast())
    else:  # newest
        q = q.order_by(PlannerTask.created_at.desc().nullslast())
    rows = db.execute(q).scalars().all()
    return {"items": [
        {
            "id": str(t.id), "title": t.title, "description": t.description,
            "status": t.status, "priority": t.priority,
            "due_date": str(t.due_date) if t.due_date else None,
            "assigned_to": t.assigned_to, "created_by": t.created_by,
            "job_id": t.job_id, "customer_id": t.customer_id,
            "contact_phone": t.contact_phone, "phone_com_call_id": t.phone_com_call_id,
            "source": t.source,
            "created_at": str(t.created_at) if t.created_at else None,
            "completed_at": str(t.completed_at) if t.completed_at else None,
        }
        for t in rows
    ]}


@router.post("/tasks", status_code=201)
def create_task(body: TaskIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    tid, uid = _tid(request), _uid(user)
    due_dt = None
    if body.due_date:
        try:
            due_dt = datetime.fromisoformat(body.due_date.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            logging.getLogger(__name__).exception("create_task caught exception")
            due_dt = None
    # Server-guarantee the "captures never scroll away" invariant: a quick-capture
    # with no due date defaults to now, so the needs_action sort (which puts
    # undated tasks last) always surfaces it near the top. Don't rely on the
    # client to send today's date.
    if due_dt is None and body.source == "quick_capture":
        due_dt = _now()

    # Call-capture auto-match: fill customer_id from the linked call or the typed
    # number when the caller didn't already pick a customer. Never overrides an
    # explicit choice. Best-effort — a matcher failure must not block the note.
    contact_phone = body.contact_phone
    customer_id = body.customer_id
    if contact_phone or body.phone_com_call_id:
        resolved_id, norm_phone = _resolve_capture_customer(
            db, call_id=body.phone_com_call_id, phone=contact_phone,
        )
        if not customer_id and resolved_id:
            customer_id = resolved_id
        if norm_phone:
            contact_phone = norm_phone

    task = PlannerTask(
        id=str(uuid4()),
        company_id=tid,
        title=(body.title or "").replace("\x00", ""),
        description=(body.description or "").replace("\x00", ""),
        status="todo",
        priority=body.priority,
        due_date=due_dt,
        created_by=uid,
        assigned_to=body.assigned_to,
        job_id=body.job_id,
        customer_id=customer_id,
        contact_phone=(contact_phone or None),
        phone_com_call_id=(body.phone_com_call_id or None),
        source=(body.source or None),
        created_at=_now(),
    )
    db.add(task)
    db.commit()

    try:
        log_audit_event_sync(
            db=db, action="create_task", user_id=uid,
            entity_type="planner_task", entity_id=str(task.id),
            details={"title": task.title},
        )
    except Exception:
        log.exception("create_task_audit_failed")

    return {"id": str(task.id), "title": task.title, "priority": task.priority, "assigned_to": task.assigned_to}


# ── Call capture helpers & endpoints (2026-07-07) ─────────────────────────────

def _resolve_capture_customer(
    db: Session, *, call_id: str | None, phone: str | None
) -> tuple[str | None, str | None]:
    """Best-effort resolve a customer + normalized phone for a captured note.

    Returns (customer_id, normalized_e164). Either may be None. A linked call row
    wins (its customer_id was resolved at ingest); otherwise the typed number is
    matched via the phone_com resolver. Never raises — capture must not break if
    the phone_com module/tables are unavailable.
    """
    resolved_id: str | None = None
    norm_phone: str | None = None
    try:
        from gdx_dispatch.modules.phone_com.customer_resolver import (
            match_phone_to_customer,
            normalize_e164,
        )

        if phone:
            norm_phone = normalize_e164(phone) or phone

        if call_id:
            from gdx_dispatch.modules.phone_com.models import PhoneComCall

            call = db.query(PhoneComCall).filter(
                PhoneComCall.phone_com_call_id == call_id
            ).first()
            if call is not None and call.customer_id:
                resolved_id = str(call.customer_id)
            if call is not None and not norm_phone and call.from_number:
                norm_phone = normalize_e164(call.from_number) or call.from_number

        if not resolved_id and (norm_phone or phone):
            match = match_phone_to_customer(db, norm_phone or phone)
            if match is not None:
                resolved_id = str(match.id)
    except Exception:
        log.exception("capture_customer_resolve_failed call_id=%s", call_id)
    return resolved_id, norm_phone


@router.get("/recent-calls")
def recent_calls(
    request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db),
    limit: int = Query(default=5, ge=1, le=20),
) -> dict:
    """Last inbound calls for the capture sheet's quick-pick strip.

    Degrades to an empty list when the phone_com module/tables are absent — the
    UI hides the strip. Timing caveat lives in the design doc: Phone.com posts
    call records at completion with lag, so the just-ended call may not be here
    yet. The strip is a convenience; the typed-number field is the primary path.
    """
    try:
        from gdx_dispatch.models.tenant_models import Customer
        from gdx_dispatch.modules.phone_com.models import PhoneComCall

        rows = (
            db.query(PhoneComCall)
            .filter(PhoneComCall.direction == "in")
            .order_by(PhoneComCall.started_at.desc().nullslast())
            .limit(limit)
            .all()
        )
        cust_ids = [r.customer_id for r in rows if r.customer_id]
        names: dict[str, str] = {}
        if cust_ids:
            for c in db.query(Customer).filter(Customer.id.in_(cust_ids)).all():
                names[str(c.id)] = c.name
        return {"items": [
            {
                "call_id": r.phone_com_call_id,
                "from_number": r.from_number,
                "customer_id": str(r.customer_id) if r.customer_id else None,
                "customer_name": names.get(str(r.customer_id)) if r.customer_id else None,
                "started_at": str(r.started_at) if r.started_at else None,
                "status": r.status,
            }
            for r in rows
        ]}
    except Exception:
        log.exception("recent_calls_unavailable")
        return {"items": []}


@router.get("/match-phone")
def match_phone(
    request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db),
    phone: str = Query(..., min_length=1, max_length=40),
) -> dict:
    """Live customer lookup for the capture sheet's phone field.

    Returns {customer_id, name, normalized} or nulls. Never raises. Uses the
    resolver's Customer object directly rather than a PK re-fetch — Customer.id
    is a UUID column, so db.get() with a str id would fail the bind processor.
    """
    customer_id = None
    name = None
    norm_phone = None
    try:
        from gdx_dispatch.modules.phone_com.customer_resolver import (
            match_phone_to_customer,
            normalize_e164,
        )

        norm_phone = normalize_e164(phone) or phone
        match = match_phone_to_customer(db, phone)
        if match is not None:
            customer_id = str(match.id)
            name = match.name
    except Exception:
        log.exception("match_phone_failed")
    return {"customer_id": customer_id, "name": name, "normalized": norm_phone}


class LinkCustomerIn(BaseModel):
    customer_id: str = Field(min_length=1, max_length=36)


@router.post("/tasks/{task_id}/link-customer")
def link_customer(
    task_id: str, body: LinkCustomerIn, request: Request,
    user: dict = Depends(get_current_user), db: Session = Depends(get_db),
) -> dict:
    """Attach a customer to a captured task and backfill the originating call(s).

    Used by the task-detail "Create customer from this" flow: after the customer
    is created via the normal customer dialog, this stamps the task and — so the
    cold-leads queue shrinks — any phone_com_calls rows tied to the same call or
    number that are still unmatched. The call backfill is best-effort.
    """
    task = db.execute(
        select(PlannerTask).where(PlannerTask.id == task_id)
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.customer_id = body.customer_id

    backfilled = 0
    try:
        from uuid import UUID

        from gdx_dispatch.modules.phone_com.customer_resolver import normalize_e164
        from gdx_dispatch.modules.phone_com.models import PhoneComCall

        # PhoneComCall.customer_id is a UUID column — coerce the str id so the
        # bind processor doesn't choke. A non-UUID id just skips call backfill.
        call_customer_id = UUID(str(body.customer_id))

        if task.phone_com_call_id:
            call = db.query(PhoneComCall).filter(
                PhoneComCall.phone_com_call_id == task.phone_com_call_id
            ).first()
            if call is not None and not call.customer_id:
                call.customer_id = call_customer_id
                backfilled += 1
        if task.contact_phone:
            norm = normalize_e164(task.contact_phone) or task.contact_phone
            # phone_com_calls.from_number is stored RAW (Phone.com sends it
            # unnormalized — see modules/phone_com/upserts.py), so a SQL equality
            # against an E.164 string would silently match nothing. Pull the
            # unmatched inbound calls (bounded — this is the cold-lead set) and
            # compare normalized forms in Python.
            unmatched = db.query(PhoneComCall).filter(
                PhoneComCall.direction == "in",
                PhoneComCall.customer_id.is_(None),
                PhoneComCall.from_number.isnot(None),
            ).all()
            for call in unmatched:
                # Skip a call already linked in-memory by the phone_com_call_id
                # path above — with autoflush off, the SQL filter still saw it as
                # NULL and re-returned it, which would double-count backfilled.
                if call.customer_id is not None:
                    continue
                if (normalize_e164(call.from_number) or call.from_number) == norm:
                    call.customer_id = call_customer_id
                    backfilled += 1
    except Exception:
        log.exception("link_customer_call_backfill_failed task=%s", task_id)

    db.commit()
    return {"id": task_id, "customer_id": body.customer_id, "calls_backfilled": backfilled}


@router.patch("/tasks/{task_id}")
def update_task(task_id: str, body: TaskPatch, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    task = db.execute(
        select(PlannerTask).where(PlannerTask.id == task_id)
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    updated = []
    for field in ["title", "description", "status", "priority", "due_date",
                  "assigned_to", "job_id", "customer_id"]:
        val = getattr(body, field, None)
        if val is not None:
            setattr(task, field, val)
            updated.append(field)
    if not updated:
        raise HTTPException(status_code=400, detail="Nothing to update")
    if body.status == "done":
        task.completed_at = _now()
    db.commit()
    return {"id": task_id, "title": task.title, "updated": updated}


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    task = db.execute(
        select(PlannerTask).where(PlannerTask.id == task_id)
    ).scalar_one_or_none()
    if task:
        db.delete(task)
        db.commit()
    return {"deleted": True}


# ── Plans ────────────────────────────────────────────────────────────────────

class PlanIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=5000)
    is_template: bool = False
    steps: list[dict] = []


@router.get("/plans")
def list_plans(request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db),
              templates_only: bool = Query(default=False)) -> dict:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    q = select(Plan)
    if templates_only:
        q = q.where(Plan.is_template == True)  # noqa: E712
    q = q.order_by(Plan.created_at.desc())
    plans = db.execute(q).scalars().all()

    items = []
    for p in plans:
        total = db.execute(select(func.count()).where(PlanStep.plan_id == p.id)).scalar() or 0
        done = db.execute(select(func.count()).where(PlanStep.plan_id == p.id, PlanStep.status == "done")).scalar() or 0
        items.append({
            "id": str(p.id), "title": p.title, "description": p.description,
            "is_template": p.is_template, "created_by": p.created_by,
            "created_at": str(p.created_at) if p.created_at else None,
            "total_steps": total, "done_steps": done,
            "progress": round(done / max(total, 1) * 100),
        })
    return {"items": items}


@router.post("/plans", status_code=201)
def create_plan(body: PlanIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    tid, uid = _tid(request), _uid(user)
    plan = Plan(id=str(uuid4()), company_id=tid, title=body.title, description=body.description,
                is_template=body.is_template, created_by=uid, created_at=_now())
    db.add(plan)
    for i, step in enumerate(body.steps):
        db.add(PlanStep(id=str(uuid4()), plan_id=plan.id, title=step.get("title", ""),
                        assigned_to=step.get("assigned_to"), due_date=step.get("due_date"), sort_order=i))
    db.commit()
    return {"id": str(plan.id), "title": body.title, "steps": len(body.steps)}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    plan = db.execute(select(Plan).where(Plan.id == plan_id)).scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    steps = db.execute(select(PlanStep).where(PlanStep.plan_id == plan.id).order_by(PlanStep.sort_order)).scalars().all()
    total = len(steps)
    done = sum(1 for s in steps if s.status == "done")
    return {
        **{"id": str(plan.id), "title": plan.title, "description": plan.description,
           "is_template": plan.is_template, "created_by": plan.created_by,
           "created_at": str(plan.created_at) if plan.created_at else None},
        "steps": [{"id": str(s.id), "title": s.title, "assigned_to": s.assigned_to,
                   "status": s.status, "due_date": str(s.due_date) if s.due_date else None,
                   "sort_order": s.sort_order} for s in steps],
        "total_steps": total, "done_steps": done,
        "progress": round(done / max(total, 1) * 100),
    }


@router.patch("/plans/steps/{step_id}")
def update_step(step_id: str, status: str = Query(pattern="^(todo|in-progress|done)$"),
                request: Request = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    step = db.execute(select(PlanStep).where(PlanStep.id == step_id)).scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    step.status = status
    db.commit()
    return {"id": step_id, "status": status}


# ── Messaging ────────────────────────────────────────────────────────────────

class ThreadIn(BaseModel):
    name: str = Field(default="", max_length=200)
    type: str = Field(default="direct", pattern="^(direct|group)$")
    members: list[str] = []


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    job_id: str | None = None
    customer_id: str | None = None


@router.get("/threads")
def list_threads(request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    uid = _uid(user)
    # Get threads the user is a member of
    member_thread_ids = db.execute(
        select(MessageThreadMember.thread_id).where(MessageThreadMember.user_id == uid)
    ).scalars().all()

    if not member_thread_ids:
        return {"items": []}

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    threads = db.execute(
        select(MessageThread).where(
            MessageThread.id.in_(member_thread_ids),
        ).order_by(MessageThread.created_at.desc())
    ).scalars().all()

    items = []
    for t in threads:
        last_msg = db.execute(
            select(Message).where(Message.thread_id == t.id).order_by(Message.created_at.desc())
        ).scalars().first()
        items.append({
            "id": str(t.id), "type": t.type, "name": t.name,
            "created_at": str(t.created_at) if t.created_at else None,
            "last_message": last_msg.body if last_msg else None,
            "last_message_at": str(last_msg.created_at) if last_msg and last_msg.created_at else None,
        })
    return {"items": items}


@router.post("/threads", status_code=201)
def create_thread(body: ThreadIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    tid, uid = _tid(request), _uid(user)
    thread = MessageThread(id=str(uuid4()), company_id=tid, type=body.type, name=body.name or None, created_by=uid, created_at=_now())
    db.add(thread)
    all_members = list(set([uid] + body.members))
    for mid in all_members:
        db.add(MessageThreadMember(thread_id=thread.id, user_id=mid, joined_at=_now()))
    db.commit()
    return {"id": str(thread.id), "name": body.name, "members": all_members}


@router.get("/threads/{thread_id}/messages")
def get_messages(thread_id: str, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db),
                 limit: int = Query(default=50, ge=1, le=200)) -> dict:
    uid = _uid(user)
    rows = db.execute(
        select(Message).where(Message.thread_id == thread_id).order_by(Message.created_at.desc()).limit(limit)
    ).scalars().all()

    # Mark as read
    member = db.execute(
        select(MessageThreadMember).where(
            MessageThreadMember.thread_id == thread_id, MessageThreadMember.user_id == uid
        )
    ).scalar_one_or_none()
    if member:
        member.last_read_at = _now()
    else:
        db.add(MessageThreadMember(thread_id=thread_id, user_id=uid, joined_at=_now(), last_read_at=_now()))
    db.commit()

    return {"items": [
        {"id": str(m.id), "sender_id": m.sender_id, "body": m.body,
         "job_id": m.job_id, "customer_id": m.customer_id,
         "created_at": str(m.created_at) if m.created_at else None}
        for m in reversed(rows)
    ]}


@router.post("/threads/{thread_id}/messages", status_code=201)
def send_message(thread_id: str, body: MessageIn, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    uid = _uid(user)
    msg = Message(id=str(uuid4()), thread_id=thread_id, sender_id=uid, body=body.body,
                  job_id=body.job_id, customer_id=body.customer_id, created_at=_now())
    db.add(msg)
    # Update sender's read timestamp
    member = db.execute(
        select(MessageThreadMember).where(
            MessageThreadMember.thread_id == thread_id, MessageThreadMember.user_id == uid
        )
    ).scalar_one_or_none()
    if member:
        member.last_read_at = _now()
    db.commit()
    return {"id": str(msg.id), "body": body.body, "sender_id": uid}
