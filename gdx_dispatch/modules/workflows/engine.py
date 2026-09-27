from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import contained_read
from gdx_dispatch.modules.workflows.models import WorkflowRule, WorkflowRun

# Only events the app ACTUALLY emits (audit round 2): advertising a trigger
# nothing fires means a rule sits active with run_count 0 forever. Re-add an
# event here the same commit that adds its emit_domain_event call.
SUPPORTED_TRIGGERS = ["job.created", "invoice.paid", "estimate.sent", "customer.created"]
SUPPORTED_ACTIONS = ["send_sms", "send_email", "create_followup_task", "emit_webhook", "update_job_field"]
# The target contract above is wider than what executes. Only these run;
# the rest would sit on an active rule reporting "not_implemented" forever,
# so the router refuses them at create/update (2026-08-31, same class as
# the retired automation sequences: an advertised action with no executor).
IMPLEMENTED_ACTIONS = ("send_email",)
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


def _automation_email_settings(db: Session) -> tuple[bool, str | None]:
    """(enabled, sender_user_id). Default OFF — the locked decision: rules
    configured while send_email was a no-op must not surprise-send."""
    try:
        from gdx_dispatch.models.tenant_models import AppSettings

        # Contained: `execute_rule` writes a WorkflowRun on this same session
        # once the action returns. On Postgres a swallowed read failure aborted
        # that transaction, so the rule's own run record — the only trace that
        # it fired at all — was lost with it (GDXA-86).
        with contained_read(db):
            row = db.query(AppSettings).first()
        if row is None:
            return False, None
        return (
            bool(getattr(row, "automation_emails_enabled", False)),
            getattr(row, "automation_sender_user_id", None) or None,
        )
    except Exception:
        logger.exception("automation_email_settings_read_failed")
        return False, None


def _resolve_rule_customer(db: Session, context: dict):
    """The customer a rule's email addresses: context customer_id when the
    event payload carries one, else via the entity (invoice/estimate/job).

    Both reads are `contained_read`-wrapped for the same reason
    `_automation_email_settings` is, one frame further out: this function's own
    handlers catch only (ValueError, TypeError), so a DB failure propagates —
    into `execute_rule`'s `except Exception` at the `send_email` action, which
    turns it into `result = "send_failed"` and then writes the WorkflowRun on
    this same session. On Postgres that write died with the aborted transaction,
    so the rule's only trace was lost by the very handler that was recording the
    failure. Found by the GDXA-86 audit after `_automation_email_settings` alone
    had been contained — the pass had been run outward, never back through this
    file.

    Insurance, not a repair: prod cannot reach this function today
    (`_run_send_email_action` returns `skipped_disabled` above it, and
    `automation_emails_enabled` is false with 0 active rules — checked live
    2026-09-27)."""
    from gdx_dispatch.models.tenant_models import Customer

    customer_id = context.get("customer_id")
    if not customer_id:
        entity_type = str(context.get("entity_type") or "")
        entity_id = context.get("entity_id")
        if entity_id:
            try:
                with contained_read(db):
                    if entity_type == "invoice":
                        from gdx_dispatch.models.tenant_models import Invoice
                        row = db.get(Invoice, UUID(str(entity_id)))
                    elif entity_type == "estimate":
                        from gdx_dispatch.modules.proposals.models import Estimate
                        row = db.get(Estimate, UUID(str(entity_id)))
                    elif entity_type == "job":
                        from gdx_dispatch.models.tenant_models import Job
                        row = db.get(Job, UUID(str(entity_id)))
                    elif entity_type == "customer":
                        customer_id = entity_id
                        row = None
                    else:
                        row = None
                if row is not None:
                    customer_id = getattr(row, "customer_id", None)
            except (ValueError, TypeError):
                customer_id = None
    if not customer_id:
        return None
    try:
        with contained_read(db):
            return db.execute(
                select(Customer).where(
                    Customer.id == UUID(str(customer_id)),
                    Customer.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
    except (ValueError, TypeError):
        return None


def _run_send_email_action(rule: WorkflowRule, params: dict, context: dict, db: Session) -> str:
    """Actually send a workflow email. Returns a result string recorded on
    the WorkflowRun: 'sent' | 'skipped_disabled' | a skip_reason.

    Pre-overhaul this action (like every action) only logged
    {"result": "logged"} — automation email was advertised in the UI and
    sent nothing. Now it renders the rule's subject/body ({{placeholder}}
    over the event context + resolved customer) inside the branded shell and
    delivers via send_transactional_email, which also writes the
    outbound_emails audit row (initiator workflow_rule/<rule id>)."""
    enabled, sender_user_id = _automation_email_settings(db)
    if not enabled:
        return "skipped_disabled"

    from gdx_dispatch.core.email_layout import (
        email_branding,
        linkify,
        nl2br,
        render_email,
    )
    from gdx_dispatch.core.email_recipients import resolve_recipient
    from gdx_dispatch.core.transactional_email import send_transactional_email
    from gdx_dispatch.routers.estimates import _render_template

    # GDXA-86, and the honest state of this frame. The two reads in this module
    # (`_automation_email_settings` above, `_resolve_rule_customer` below) are
    # contained, so a failure in them no longer costs the WorkflowRun that
    # `execute_rule` writes afterwards. FOUR MORE ARE NOT, all on the caller's
    # session, all swallowed, all comms-email-phone's:
    #
    #   core/email_recipients.py:80   resolve_recipient()      — pure read
    #   core/email_layout.py:129      email_branding()         — pure read
    #   modules/outlook/token_refresh.py:163 get_user_tokens(tenant_db, …),
    #       reached on the FIRST send branch whenever tenant and user ids are
    #       present (transactional_email.py:454-455) via `_try_outlook_graph`
    #       -> `with_outlook_client`, and swallowed by `_try_outlook_graph`'s
    #       `except Exception -> (False, "outlook_send_failed")` at
    #       transactional_email.py:128
    #   core/transactional_email.py:325 _designated_sender_user_id(tenant_db),
    #       the flag-gated SMTP-fallback branch at :497
    #
    # That list is a CALL-GRAPH WALK, and it replaces one built from names: an
    # earlier draft of this comment listed `recently_sent` (core/
    # transactional_email.py:284) as being "inside the send below". It is not —
    # `send_transactional_email` never calls it; its callers are
    # routers/customer_statements.py:213, routers/estimates.py:2098 and
    # routers/invoices.py:2702. Naming it here while missing `get_user_tokens`,
    # which is the one read on this path that runs every time, is exactly the
    # mistake this file's own fix was about: the pass was run outward, not back
    # through the frame. Found by the GDXA-86 audit, round 3.
    #
    # Why none of the four is wrapped HERE: each swallows its own failure, and
    # `contained_read` rolls back only when the exception reaches its `__exit__`.
    # With the callee swallowing, the block exits clean and the CM issues RELEASE
    # SAVEPOINT on an already-aborted transaction, which raises 25P02 itself
    # while the caller's commit stays dead (measured, PG 15.17: 0 rows). See
    # `contained_read` rule 5. Do NOT read that as "unfixable from here", which
    # an earlier draft also claimed: `ROLLBACK TO SAVEPOINT` IS legal on an
    # aborted transaction, so an always-rollback variant of that helper would
    # contain the pure-read ones from this file (measured on PG 15.17 and 16.14,
    # 1 row survives). It is not built, and the reason is traffic, not physics:
    # checked live on prod 2026-09-27, `app_settings.automation_emails_enabled`
    # is false, there are 0 active workflow_rules and 0 workflow_runs ever — and
    # `_automation_email_settings` returns `skipped_disabled` above, so nothing
    # below this line executes on prod today. The two wraps in this module are
    # correctness insurance on a path that is switched off, not the repair of an
    # observed loss. Before this feature is turned on, fix those four in their
    # own files — `get_user_tokens` first, because it is the unconditional one.
    customer = _resolve_rule_customer(db, context)
    if customer is None:
        return "no_customer_for_entity"
    recipient = resolve_recipient(db, customer)
    if not recipient.ok:
        return "no_recipient_email"

    branding = email_branding(db)
    ctx = {str(k): str(v if v is not None else "") for k, v in context.items()}
    ctx.setdefault("company_name", branding["company_name"])
    ctx["customer_name"] = recipient.greeting_name
    subject = _render_template(str(params.get("subject") or ""), ctx).strip() \
        or f"A message from {branding['company_name']}"
    body_text = _render_template(str(params.get("body") or ""), ctx).strip()
    if not body_text:
        return "empty_body_template"
    accent = branding.get("accent") or "#2563eb"
    body_html = (
        '<p style="margin:0 0 12px;">'
        + linkify(nl2br(body_text), accent).replace(
            "<br><br>", '</p><p style="margin:0 0 12px;">'
        )
        + "</p>"
    )
    html = render_email(
        branding=branding, body_html=body_html, title=subject, preheader=subject,
    )
    sent, _provider, skip_reason = send_transactional_email(
        tenant_db=db,
        tenant_id=str(getattr(customer, "company_id", "") or context.get("company_id") or ""),
        user_id=sender_user_id,
        to_email=recipient.email,
        to_name=recipient.to_name,
        subject=subject,
        html_body=html,
        initiator_kind="workflow_rule",
        initiator_ref=str(rule.id),
        kind="automation",
        entity_type=str(context.get("entity_type") or "") or None,
        entity_id=str(context.get("entity_id") or "") or None,
        recipient_source=recipient.source,
        recipient_contact_id=recipient.contact_id,
    )
    return "sent" if sent else (skip_reason or "send_failed")


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
            if action_type == "send_email":
                try:
                    result = _run_send_email_action(rule, params or {}, context, db)
                except Exception:
                    logger.exception("workflow_send_email_failed rule=%s", rule.id)
                    result = "send_failed"
            else:
                # Honest label: these action types have no executor yet.
                # "logged" used to read like success for ALL actions.
                result = "not_implemented"
            actions_run.append({"action_type": action_type, "params": params, "result": result})
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
