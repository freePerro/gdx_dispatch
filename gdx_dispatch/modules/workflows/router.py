from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

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
    from gdx_dispatch.modules.workflows.engine import SUPPORTED_ACTIONS, SUPPORTED_TRIGGERS

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


class WorkflowRuleIn(BaseModel): name: str; trigger_event: str; conditions: list[dict] = Field(default_factory=list); actions: list[dict] = Field(default_factory=list); is_active: bool = True  # noqa: E701,E702
class WorkflowRulePatch(BaseModel): name: str | None = None; trigger_event: str | None = None; conditions: list[dict] | None = None; actions: list[dict] | None = None; is_active: bool | None = None  # noqa: E701,E702

@router.get("/workflows", response_model=None)
def list_workflows(db: Session = Depends(get_db)) -> list[WorkflowRule]:
    return list(db.execute(select(WorkflowRule).where(WorkflowRule.is_active.is_(True)).order_by(WorkflowRule.created_at.desc())).scalars().all())

@router.post("/workflows", response_model=None)
def create_workflow(payload: WorkflowRuleIn, db: Session = Depends(get_db)) -> WorkflowRule:
    _validate_rule_shape(payload.trigger_event, payload.actions)
    row = WorkflowRule(**payload.model_dump()); db.add(row); db.commit(); db.refresh(row); return row  # noqa: E701,E702

@router.put("/workflows/{rule_id}", response_model=None)
def update_workflow(rule_id: UUID, payload: WorkflowRulePatch, db: Session = Depends(get_db)) -> WorkflowRule:
    row = db.execute(select(WorkflowRule).where(WorkflowRule.id == rule_id)).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Workflow rule not found")  # noqa: E701,E702
    updates = payload.model_dump(exclude_unset=True)
    _validate_rule_shape(updates.get("trigger_event"), updates.get("actions"))
    for k, v in updates.items(): setattr(row, k, v)  # noqa: E701,E702
    db.commit(); db.refresh(row); return row  # noqa: E701,E702

@router.delete("/workflows/{rule_id}", response_model=None)
def delete_workflow(rule_id: UUID, db: Session = Depends(get_db)) -> dict[str, bool]:
    row = db.execute(select(WorkflowRule).where(WorkflowRule.id == rule_id)).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Workflow rule not found")  # noqa: E701,E702
    row.is_active = False; db.commit(); return {"ok": True}  # noqa: E701,E702

@router.get("/workflows/{rule_id}/runs", response_model=None)
def list_workflow_runs(rule_id: UUID, db: Session = Depends(get_db)) -> list[WorkflowRun]:
    return list(db.execute(select(WorkflowRun).where(WorkflowRun.rule_id == rule_id).order_by(WorkflowRun.triggered_at.desc())).scalars().all())
