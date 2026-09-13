from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import (
    ensure_audit_table,
    log_audit_event_sync,
    resolve_audit_actor,
)
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.workflows.models import WorkflowRule, WorkflowRun
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api", tags=["workflows"], dependencies=[Depends(require_module("workflows")), Depends(get_current_user)])

def _validate_rule_shape(trigger_event: str | None, actions: list[dict] | None) -> None:
    """Reject rules that can never run (email overhaul: a rule on a trigger
    nothing emits, or with an unknown action, sits active with run_count 0
    forever — the UI-audit's dead-rule class). 422 at create/update, not a
    silent no-op at fire time."""
    from gdx_dispatch.modules.workflows.engine import (
        IMPLEMENTED_ACTIONS,
        SUPPORTED_ACTIONS,
        SUPPORTED_TRIGGERS,
    )

    if trigger_event is not None and trigger_event not in SUPPORTED_TRIGGERS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown trigger_event '{trigger_event}' — supported: {', '.join(SUPPORTED_TRIGGERS)}",
        )
    for action in actions or []:
        atype = str(action.get("action_type", ""))
        if atype not in SUPPORTED_ACTIONS:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown action_type '{atype}' — supported: {', '.join(SUPPORTED_ACTIONS)}",
            )
        if atype not in IMPLEMENTED_ACTIONS:
            # Listed in the contract, but nothing executes it yet — a rule
            # carrying it would run "not_implemented" on every fire.
            raise HTTPException(
                status_code=422,
                detail=(
                    f"action_type '{atype}' has no executor yet — "
                    f"implemented: {', '.join(IMPLEMENTED_ACTIONS)}"
                ),
            )


def _rule_snapshot(row: WorkflowRule) -> dict:
    """What the rule is, in the terms that decide whether email goes out."""
    return {
        "name": row.name,
        "trigger_event": row.trigger_event,
        "is_active": bool(row.is_active),
        "action_types": [str(a.get("action_type", "")) for a in (row.actions or [])],
        "condition_count": len(row.conditions or []),
    }


def _audit(db: Session, request: Request, action: str, rule_id: UUID, details: dict) -> None:
    """Who / what / when for a workflow-rule mutation (invariant #1, #558).

    ACTOR. These three handlers take no `user` parameter — authentication is a
    router-level dependency (see `router` above), so no principal is in the
    handler's scope at all. `routers/auth/core.py` stashes the decoded user on
    `request.state.user` precisely for this case; `resolve_audit_actor(None,
    request)` reads it. Do NOT reach for `user.get("sub")`: the login JWT dict
    is {"user_id", "tenant_id", "role"} with no "sub" key, and that chain
    writes "system" for a real person (#701).

    ATOMICITY. Called BEFORE `db.commit()`, so the audit row is flushed into
    the caller's transaction and the two land together. If the audit write
    raises, the request 500s with the mutation still unstaged — `get_db` closes
    the session without committing.

    WHY THIS SURFACE. `engine.IMPLEMENTED_ACTIONS == ("send_email",)` is the
    only action that executes, so every rule written here is a standing order
    to send customer-facing email. An unattributed one is an email nobody
    signed.
    """
    log_audit_event_sync(
        db,
        user_id=resolve_audit_actor(None, request),
        action=action,
        entity_type="workflow_rule",
        entity_id=str(rule_id),
        details=details,
        request=request,
    )


class WorkflowRuleIn(BaseModel): name: str; trigger_event: str; conditions: list[dict] = Field(default_factory=list); actions: list[dict] = Field(default_factory=list); is_active: bool = True  # noqa: E701,E702
class WorkflowRulePatch(BaseModel): name: str | None = None; trigger_event: str | None = None; conditions: list[dict] | None = None; actions: list[dict] | None = None; is_active: bool | None = None  # noqa: E701,E702

@router.get("/workflows", response_model=None)
def list_workflows(db: Session = Depends(get_db)) -> list[WorkflowRule]:
    return list(db.execute(select(WorkflowRule).where(WorkflowRule.is_active.is_(True)).order_by(WorkflowRule.created_at.desc())).scalars().all())

@router.post("/workflows", response_model=None)
def create_workflow(payload: WorkflowRuleIn, request: Request, db: Session = Depends(get_db)) -> WorkflowRule:
    # `ensure_audit_table` runs here, before anything is staged: its first call
    # for an engine COMMITS the guard DDL, and fired lazily from inside the
    # audit write it would harden the staged mutation before its audit row
    # exists. NOT `Depends(audit_ready_db)` — that resolves its own session via
    # `_get_db_dep`, which calls `get_db()` imperatively instead of declaring
    # `Depends(get_db)`, so it bypasses every `dependency_overrides[get_db]` in
    # the suite (routers/customers.py records two tests that went 404 that way;
    # during #558 it silently pointed two more at the real database).
    ensure_audit_table(db)
    _validate_rule_shape(payload.trigger_event, payload.actions)
    row = WorkflowRule(**payload.model_dump())
    db.add(row)
    db.flush()  # assign row.id so the audit row can name the entity
    _audit(db, request, "workflow_rule_created", row.id, _rule_snapshot(row))
    db.commit()
    db.refresh(row)
    return row

@router.put("/workflows/{rule_id}", response_model=None)
def update_workflow(rule_id: UUID, payload: WorkflowRulePatch, request: Request, db: Session = Depends(get_db)) -> WorkflowRule:
    # `ensure_audit_table` runs here, before anything is staged: its first call
    # for an engine COMMITS the guard DDL, and fired lazily from inside the
    # audit write it would harden the staged mutation before its audit row
    # exists. NOT `Depends(audit_ready_db)` — that resolves its own session via
    # `_get_db_dep`, which calls `get_db()` imperatively instead of declaring
    # `Depends(get_db)`, so it bypasses every `dependency_overrides[get_db]` in
    # the suite (routers/customers.py records two tests that went 404 that way;
    # during #558 it silently pointed two more at the real database).
    ensure_audit_table(db)
    row = db.execute(select(WorkflowRule).where(WorkflowRule.id == rule_id)).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Workflow rule not found")  # noqa: E701,E702
    updates = payload.model_dump(exclude_unset=True)
    _validate_rule_shape(updates.get("trigger_event"), updates.get("actions"))
    # The OLD value is the forensic half — "who pointed this rule at a new
    # subject line" is useless without what it said before.
    changed = {
        k: {"from": getattr(row, k), "to": v}
        for k, v in updates.items()
        if getattr(row, k) != v
    }
    for k, v in updates.items(): setattr(row, k, v)  # noqa: E701,E702
    _audit(db, request, "workflow_rule_updated", row.id, {"changed": changed, "name": row.name})
    db.commit()
    db.refresh(row)
    return row

@router.delete("/workflows/{rule_id}", response_model=None)
def delete_workflow(rule_id: UUID, request: Request, db: Session = Depends(get_db)) -> dict[str, bool]:
    # `ensure_audit_table` runs here, before anything is staged: its first call
    # for an engine COMMITS the guard DDL, and fired lazily from inside the
    # audit write it would harden the staged mutation before its audit row
    # exists. NOT `Depends(audit_ready_db)` — that resolves its own session via
    # `_get_db_dep`, which calls `get_db()` imperatively instead of declaring
    # `Depends(get_db)`, so it bypasses every `dependency_overrides[get_db]` in
    # the suite (routers/customers.py records two tests that went 404 that way;
    # during #558 it silently pointed two more at the real database).
    ensure_audit_table(db)
    row = db.execute(select(WorkflowRule).where(WorkflowRule.id == rule_id)).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Workflow rule not found")  # noqa: E701,E702
    was_active = bool(row.is_active)
    row.is_active = False
    # "deactivated", not "deleted": the row stays, `list_workflows` filters it
    # out. Calling it a delete in the trail would misdescribe what happened.
    _audit(
        db, request, "workflow_rule_deactivated", row.id,
        {**_rule_snapshot(row), "was_active": was_active, "soft_delete": True},
    )
    db.commit()
    return {"ok": True}

@router.get("/workflows/{rule_id}/runs", response_model=None)
def list_workflow_runs(rule_id: UUID, db: Session = Depends(get_db)) -> list[WorkflowRun]:
    return list(db.execute(select(WorkflowRun).where(WorkflowRun.rule_id == rule_id).order_by(WorkflowRun.triggered_at.desc())).scalars().all())
