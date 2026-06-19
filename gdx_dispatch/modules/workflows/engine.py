from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.workflows.models import WorkflowRule, WorkflowRun

SUPPORTED_TRIGGERS = ["job.stage_changed", "job.created", "invoice.created", "invoice.paid", "estimate.sent", "customer.created"]
SUPPORTED_ACTIONS = ["send_sms", "send_email", "create_followup_task", "emit_webhook", "update_job_field"]
logger = logging.getLogger(__name__)


def _check(op: str, left: object, right: object) -> bool:
    if op == "eq": return left == right  # noqa: E701,E702
    if op == "ne": return left != right  # noqa: E701,E702
    if op == "gt": return left is not None and right is not None and left > right  # noqa: E701,E702
    if op == "lt": return left is not None and right is not None and left < right  # noqa: E701,E702
    if op == "contains": return left is not None and str(right) in str(left)  # noqa: E701,E702
    if op == "in": return left in right if isinstance(right, (list, tuple, set)) else False  # noqa: E701,E702
    return False


def evaluate_conditions(rule: WorkflowRule, context: dict) -> bool:
    for cond in (rule.conditions or []):
        if not _check(str(cond.get("operator", "")), context.get(str(cond.get("field", ""))), cond.get("value")): return False  # noqa: E701,E702
    return True


async def execute_rule(rule_id: str, context: dict, db: Session):
    rule = db.execute(select(WorkflowRule).where(WorkflowRule.id == UUID(rule_id), WorkflowRule.is_active.is_(True))).scalar_one_or_none()
    if not rule: return  # noqa: E701,E702
    entity_type, entity_id, now = str(context.get("entity_type", "unknown")), str(context.get("entity_id", "unknown")), datetime.now(timezone.utc)
    if not evaluate_conditions(rule, context):
        db.add(WorkflowRun(rule_id=rule.id, entity_type=entity_type, entity_id=entity_id, triggered_at=now, status="skipped", actions_run=[])); db.commit(); return  # noqa: E701,E702
    actions_run, status, error = [], "success", None
    try:
        for action in (rule.actions or []):
            action_type = str(action.get("action_type", "")); params = action.get("params", {})  # noqa: E701,E702
            if action_type not in SUPPORTED_ACTIONS: raise ValueError(f"Unsupported action_type: {action_type}")  # noqa: E701,E702
            logger.info("workflow_action", extra={"rule_id": str(rule.id), "action_type": action_type, "params": params, "entity_id": entity_id})
            actions_run.append({"action_type": action_type, "params": params, "result": "logged"})
        rule.run_count, rule.last_run_at = (rule.run_count or 0) + 1, now
    except Exception as exc:
        logging.getLogger(__name__).exception("execute_rule caught exception")
        status, error = "failed", str(exc)
    db.add(WorkflowRun(rule_id=rule.id, entity_type=entity_type, entity_id=entity_id, triggered_at=now, status=status, actions_run=actions_run, error=error)); db.commit()  # noqa: E701,E702


async def fire_trigger(event_type: str, context: dict, tenant_id: str, db: Session):
    _ = tenant_id
    if event_type not in SUPPORTED_TRIGGERS: return  # noqa: E701,E702
    rules = db.execute(select(WorkflowRule).where(WorkflowRule.is_active.is_(True), WorkflowRule.trigger_event == event_type)).scalars().all()
    for rule in rules:
        try: await execute_rule(str(rule.id), context, db)  # noqa: E701,E702
        except Exception: logger.exception("workflow_rule_execution_failed", extra={"rule_id": str(rule.id), "event_type": event_type})  # noqa: E701,E702
