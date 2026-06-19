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


class TaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(todo|in-progress|done)$")
    priority: str | None = None
    due_date: str | None = None
    assigned_to: str | None = None
    job_id: str | None = None
    customer_id: str | None = None


_SORT_OPTIONS = {"newest", "oldest", "priority", "due_date"}
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
    if sort == "oldest":
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
        customer_id=body.customer_id,
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
