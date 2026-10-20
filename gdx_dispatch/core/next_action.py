"""gdx_dispatch/core/next_action.py — Next-action queue for tenants and users.

Manages a prioritised queue of actionable tasks. Persisted actions live in the
tenant DB. Ephemeral auto-generated actions are computed from job/invoice state
and are not persisted.
"""
from __future__ import annotations

import contextlib
import logging
import uuid as _uuid_mod
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, log_audit_event_sync, utcnow
from gdx_dispatch.core.database import contained_read
from gdx_dispatch.models.tenant_models import Invoice, Job

logger = logging.getLogger(__name__)

_PRIORITY_KEY = {"high": 0, "medium": 1, "low": 2}

ACTION_TYPES = (
    "follow_up_estimate",
    "call_overdue_invoice",
    "schedule_maintenance",
    "request_review",
    "check_job_status",
)


# ---------------------------------------------------------------------------
# ORM model — stored in tenant DB
# ---------------------------------------------------------------------------

class NextAction(TenantBase):
    __tablename__ = "next_actions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="medium")
    action_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    estimated_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reference_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    snoozed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_uuid(value: Any) -> Any:
    """Convert a string to a UUID object, or return as-is if already a UUID."""
    if isinstance(value, _uuid_mod.UUID):
        return value
    try:
        return _uuid_mod.UUID(str(value))
    except (ValueError, AttributeError):
        logging.getLogger(__name__).exception("_to_uuid caught exception")
        return value


def _make_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _find(tenant_db: Session, tenant_id: str, action_id: str) -> NextAction | None:
    """The live persisted action with this id, or None."""
    return (
        tenant_db.query(NextAction)
        .filter(
            NextAction.id == _to_uuid(action_id),
            NextAction.tenant_id == tenant_id,
            NextAction.deleted_at.is_(None),
        )
        .first()
    )


def _audit(
    tenant_db: Session,
    tenant_id: str,
    verb: str,
    action: NextAction,
    details: dict,
    request: Any,
    user_id: str | None = None,
) -> None:
    """Flush an audit row into the caller's transaction, before its commit,
    so a change and its trail land together or not at all (invariant #1;
    GDXA-1808 found create, complete and snooze writing none). With no
    ``user_id`` the actor is resolved from ``request``."""
    log_audit_event_sync(
        tenant_db,
        tenant_id=tenant_id,
        user_id=user_id,
        action=f"next_action.{verb}",
        entity_type="next_action",
        entity_id=str(action.id),
        details=details,
        request=request,
    )


def _add_audited(
    tenant_db: Session, action: NextAction, details: dict, request: Any, user_id: str | None
) -> NextAction:
    """Insert ``action`` and flush its ``next_action.create`` audit row into
    the same transaction; the caller commits."""
    tenant_db.add(action)
    tenant_db.flush()
    _audit(tenant_db, action.tenant_id, "create", action, details, request, user_id=user_id)
    return action


# ---------------------------------------------------------------------------
# Background writers (Celery beat tasks)
# ---------------------------------------------------------------------------

# The fields a background writer may set. Identity (tenant, type, reference)
# is the lookup key and status is the helper's to manage, so neither can be
# smuggled through ``fields``.
_SYSTEM_FIELDS = frozenset(
    {"title", "description", "priority", "action_url", "estimated_value"}
)


def _system_actor(actor: str) -> str:
    # A bare "system" would be re-attributed from a request, and a person's id
    # here would claim a beat task's write for them. 64 is AuditLog.user_id's
    # width: Postgres refuses a longer one and rolls the whole tick back.
    if not actor.startswith("system:") or actor == "system:" or len(actor) > 64:
        raise ValueError(f"background actor must be 'system:<task>' (<=64 chars), got {actor!r}")
    return actor


def _live_system_actions(
    tenant_db: Session, tenant_id: str, action_type: str, reference_id: str
) -> list[NextAction]:
    """Live rows for this key, newest first. The tenant_id clause is kept on
    purpose although the scan calls it redundant: get_queue filters on it, so
    a row under a drifted tenant string is invisible to the office, and the
    three writers this replaces filter on it too."""
    return (
        tenant_db.query(NextAction)
        .filter(
            NextAction.tenant_id == tenant_id,
            NextAction.action_type == action_type,
            NextAction.reference_id == reference_id,
            NextAction.status != "completed",
            NextAction.deleted_at.is_(None),
        )
        .order_by(NextAction.created_at.desc())
        .all()
    )


def upsert_system_action(
    tenant_db: Session,
    *,
    tenant_id: str,
    actor: str,
    action_type: str,
    reference_id: str,
    fields: dict[str, Any],
    wake: bool = True,
) -> tuple[NextAction, str]:
    """Create or refresh a beat task's self-maintained action, audited.

    GDXA-1859: billing_followup, invoice_reminders_auto and outlook's sync
    health check each added or mutated their NextAction directly and wrote no
    audit row. The live (not completed, not deleted) row keyed by
    ``(tenant_id, action_type, reference_id)`` is updated from ``fields``, or
    a pending one is created. With ``wake`` an updated row returns to
    ``pending`` unless it is under a live snooze, which the office set on
    purpose. The audit row is flushed into the caller's transaction and
    nothing is committed: the caller's commit lands row and trail together.

    Returns ``(row, outcome)``, outcome one of ``"created"``, ``"updated"``
    or ``"unchanged"``. An unchanged refresh writes nothing, so a daily tick
    does not add a daily audit row for numbers that did not move.
    """
    actor = _system_actor(actor)
    unknown = set(fields) - _SYSTEM_FIELDS
    if unknown:
        raise ValueError(f"upsert_system_action: unsupported fields {sorted(unknown)}")
    if "estimated_value" in fields:
        # The column is Float NOT NULL; a Decimal sum would never compare equal
        # to the stored value and would audit an "update" on every tick, and
        # None fails here rather than at flush.
        fields = {**fields, "estimated_value": float(fields["estimated_value"])}

    live = _live_system_actions(tenant_db, tenant_id, action_type, reference_id)
    if len(live) > 1:
        logger.warning(
            "upsert_system_action: %d live %s/%s rows, refreshing the newest",
            len(live), action_type, reference_id,
        )
    existing = live[0] if live else None
    if existing is None:
        action = NextAction(
            tenant_id=tenant_id,
            user_id=None,
            action_type=action_type,
            reference_id=reference_id,
            status="pending",
            **fields,
        )
        details = {"action_type": action_type, "reference_id": reference_id, **fields}
        return _add_audited(tenant_db, action, details, None, actor), "created"

    changed: dict[str, dict[str, Any]] = {}
    for key, value in fields.items():
        before = getattr(existing, key)
        if before != value:
            changed[key] = {"from": before, "to": value}
            setattr(existing, key, value)
    if wake and existing.status != "pending":
        snoozed = (
            existing.status == "snoozed"
            and existing.snoozed_until is not None
            and _make_aware(existing.snoozed_until) > _utcnow()
        )
        if not snoozed:
            changed["status"] = {"from": existing.status, "to": "pending"}
            existing.status = "pending"
    if not changed:
        return existing, "unchanged"
    tenant_db.flush()
    _audit(
        tenant_db, tenant_id, "update", existing,
        {"action_type": action_type, "reference_id": reference_id, "changed": changed},
        None, user_id=actor,
    )
    return existing, "updated"


def resolve_system_action(
    tenant_db: Session,
    *,
    tenant_id: str,
    actor: str,
    action_type: str,
    reference_id: str,
) -> list[NextAction]:
    """Complete every live action for this key when its condition has
    cleared, each audited in the caller's transaction (no commit). Empty when
    there was none, in which case nothing is written."""
    actor = _system_actor(actor)
    live = _live_system_actions(tenant_db, tenant_id, action_type, reference_id)
    for existing in live:
        previous = existing.status
        existing.status = "completed"
        existing.completed_at = _utcnow()
        tenant_db.flush()
        _audit(
            tenant_db, tenant_id, "complete", existing,
            {"action_type": action_type, "reference_id": reference_id,
             "from": previous, "to": "completed"},
            None, user_id=actor,
        )
    return live


def _action_to_dict(action: NextAction) -> dict:
    return {
        "id": str(action.id),
        "tenant_id": action.tenant_id,
        "user_id": action.user_id,
        "action_type": action.action_type,
        "title": action.title,
        "description": action.description,
        "priority": action.priority,
        "action_url": action.action_url,
        "estimated_value": action.estimated_value,
        "reference_id": action.reference_id,
        "status": action.status,
        "snoozed_until": (
            action.snoozed_until.isoformat() if action.snoozed_until else None
        ),
        "completed_at": (
            action.completed_at.isoformat() if action.completed_at else None
        ),
        "created_at": (
            action.created_at.isoformat() if action.created_at else None
        ),
    }


# ---------------------------------------------------------------------------
# NextActionQueue
# ---------------------------------------------------------------------------

class NextActionQueue:
    """Manages prioritised next-action items per tenant/user."""

    def get_queue(
        self,
        tenant_id: str,
        user_id: str,
        tenant_db: Session,
    ) -> list[dict]:
        """Return the active next-action queue for a user.

        Merges persisted actions from the DB with ephemeral auto-generated
        actions derived from job/invoice state.
        """
        now = _utcnow()
        results: list[dict] = []

        # Persisted actions from DB.
        #
        # GDXA-159: contained because this frame keeps USING the session after
        # the swallow. `get_auto_actions` below runs three more reads on it, so
        # on Postgres one failure here aborted the transaction and took all of
        # them with it — the user got an empty queue and four log lines where
        # the ephemeral half would have worked perfectly. The degraded return
        # this `except` promises was only ever honest on SQLite.
        try:
            with contained_read(tenant_db):
                rows = (
                    tenant_db.query(NextAction)
                    .filter(
                        NextAction.tenant_id == tenant_id,
                        NextAction.deleted_at.is_(None),
                        NextAction.status != "completed",
                    )
                    .all()
                )
            for row in rows:
                # Exclude snoozed items that haven't woken yet
                if row.status == "snoozed" and row.snoozed_until:
                    if _make_aware(row.snoozed_until) > now:
                        continue
                # Include if assigned to this user or unassigned
                if row.user_id is None or row.user_id == user_id:
                    results.append(_action_to_dict(row))
        except Exception as exc:
            logger.warning(
                "get_queue DB query failed for tenant %s: %s", tenant_id, exc
            )

        # Ephemeral auto-generated actions
        try:
            auto = self.get_auto_actions(tenant_id, tenant_db)
            results.extend(auto)
        except Exception as exc:
            logger.warning(
                "get_auto_actions failed for tenant %s: %s", tenant_id, exc
            )

        # Deduplicate by (action_type, reference_id)
        seen: set[tuple[str, str | None]] = set()
        deduped: list[dict] = []
        for item in results:
            key = (item.get("action_type", ""), item.get("reference_id"))
            if key not in seen:
                seen.add(key)
                deduped.append(item)

        deduped.sort(key=lambda x: _PRIORITY_KEY.get(x.get("priority", "low"), 99))
        return deduped

    def complete_action(
        self,
        tenant_id: str,
        action_id: str,
        tenant_db: Session,
        request: Any = None,
    ) -> dict:
        """Mark a persisted action as completed, audited in the same commit."""
        try:
            action = _find(tenant_db, tenant_id, action_id)
            if action is None:
                return {"error": "Action not found", "id": action_id}
            previous = action.status
            action.status = "completed"
            action.completed_at = _utcnow()
            _audit(tenant_db, tenant_id, "complete", action, {"from": previous, "to": "completed"}, request)
            tenant_db.commit()
            return {"id": action_id, "status": "completed"}
        except Exception as exc:
            logger.warning(
                "complete_action failed for action %s: %s", action_id, exc
            )
            with contextlib.suppress(Exception):
                tenant_db.rollback()
            # Generic message; full exception is logged above. (CodeQL stack-trace-exposure)
            return {"error": "Failed to complete action", "id": action_id}

    def snooze_action(
        self,
        tenant_id: str,
        action_id: str,
        until_dt: datetime,
        tenant_db: Session,
        request: Any = None,
    ) -> dict:
        """Defer a persisted action until a future datetime, audited in the
        same commit."""
        try:
            action = _find(tenant_db, tenant_id, action_id)
            if action is None:
                return {"error": "Action not found", "id": action_id}
            previous = action.status
            action.status = "snoozed"
            action.snoozed_until = until_dt
            _audit(
                tenant_db, tenant_id, "snooze", action,
                {"from": previous, "to": "snoozed", "until": until_dt.isoformat()}, request,
            )
            tenant_db.commit()
            return {
                "id": action_id,
                "status": "snoozed",
                "snoozed_until": until_dt.isoformat(),
            }
        except Exception as exc:
            logger.warning(
                "snooze_action failed for action %s: %s", action_id, exc
            )
            with contextlib.suppress(Exception):
                tenant_db.rollback()
            # Generic message; full exception is logged above. (CodeQL stack-trace-exposure)
            return {"error": "Failed to snooze action", "id": action_id}

    def create_action(
        self,
        tenant_id: str,
        user_id: str | None,
        action_type: str,
        title: str,
        description: str | None,
        priority: str,
        action_url: str | None,
        estimated_value: float,
        reference_id: str | None,
        tenant_db: Session,
        request: Any = None,
    ) -> dict:
        """Create and persist a new next action, audited in the same commit."""
        try:
            action = NextAction(
                tenant_id=tenant_id,
                user_id=user_id,
                action_type=action_type,
                title=title,
                description=description,
                priority=priority,
                action_url=action_url,
                estimated_value=estimated_value,
                reference_id=reference_id,
                status="pending",
            )
            _add_audited(
                tenant_db, action,
                {
                    "action_type": action_type,
                    "title": title,
                    "priority": priority,
                    "action_url": action_url,
                    "reference_id": reference_id,
                },
                request, user_id,
            )
            tenant_db.commit()
            tenant_db.refresh(action)
            return _action_to_dict(action)
        except Exception as exc:
            logger.warning(
                "create_action failed for tenant %s: %s", tenant_id, exc
            )
            with contextlib.suppress(Exception):
                tenant_db.rollback()
            # Generic message; full exception is logged above. (CodeQL stack-trace-exposure)
            return {"error": "Failed to create action"}

    def get_auto_actions(
        self,
        tenant_id: str,
        tenant_db: Session,
    ) -> list[dict]:
        """Derive ephemeral action suggestions from job and invoice state.

        These are NOT persisted — they are recomputed each time and merged
        into the queue at read time.
        """
        actions: list[dict] = []
        now = _utcnow()
        cutoff_72h = now - timedelta(hours=72)
        cutoff_14d = now - timedelta(days=14)
        cutoff_180d = now - timedelta(days=180)

        # Rule: follow up on stale estimates
        try:
            # PR2-billing-capture: the billing_status clause here was safe to
            # delete. The column is a dead cache: no app code writes it past
            # "unbilled" (the demo seed still writes paid/invoiced fixtures),
            # and older prod rows hold a stale "invoiced" (newest 2026-07-18).
            # Every live non-"unbilled" row seen on prod and demo (2026-10-11)
            # is a `completed` job, and this query selects only `estimate` jobs.
            # Kept out rather than "fixed": estimate-stage jobs are pre-billing
            # by definition.
            with contained_read(tenant_db):
                stale_estimates = (
                    tenant_db.query(Job)
                    .filter(
                        Job.lifecycle_stage == "estimate",
                        Job.created_at < cutoff_72h,
                        Job.deleted_at.is_(None),
                    )
                    .all()
                )
            for job in stale_estimates:
                actions.append({
                    "id": f"auto:follow_up_estimate:{job.id}",
                    "tenant_id": tenant_id,
                    "user_id": job.assigned_to,
                    "action_type": "follow_up_estimate",
                    "title": f"Follow Up: {job.title}",
                    "description": (
                        "This estimate is over 72 hours old and hasn't been invoiced. "
                        "Follow up with the customer."
                    ),
                    "priority": "high",
                    "action_url": f"/jobs/{job.id}",
                    "estimated_value": 0.0,
                    "reference_id": str(job.id),
                    "status": "pending",
                    "snoozed_until": None,
                    "completed_at": None,
                    "created_at": now.isoformat(),
                })
        except Exception as exc:
            logger.warning(
                "auto follow_up_estimate rule failed for tenant %s: %s",
                tenant_id, exc,
            )

        # Rule: call on overdue sent invoices
        try:
            with contained_read(tenant_db):
                overdue_invoices = (
                    tenant_db.query(Invoice)
                    .filter(
                        Invoice.status == "sent",
                        Invoice.sent_at < cutoff_14d,
                        Invoice.deleted_at.is_(None),
                    )
                    .all()
                )
            for inv in overdue_invoices:
                actions.append({
                    "id": f"auto:call_overdue_invoice:{inv.id}",
                    "tenant_id": tenant_id,
                    "user_id": None,
                    "action_type": "call_overdue_invoice",
                    "title": f"Call on Overdue Invoice #{inv.invoice_number}",
                    "description": (
                        f"Invoice #{inv.invoice_number} was sent more than 14 days ago "
                        "and has not been paid. Call the customer."
                    ),
                    "priority": "high",
                    # The SPA has no /invoices/:id — `/invoices` is an exact
                    # redirect to /billing and does not catch the id form.
                    # InvoiceDetailView is /billing/:id.
                    "action_url": f"/billing/{inv.id}",
                    "estimated_value": float(inv.total) if inv.total else 0.0,
                    "reference_id": str(inv.id),
                    "status": "pending",
                    "snoozed_until": None,
                    "completed_at": None,
                    "created_at": now.isoformat(),
                })
        except Exception as exc:
            logger.warning(
                "auto call_overdue_invoice rule failed for tenant %s: %s",
                tenant_id, exc,
            )

        # Rule: schedule maintenance for return-visit customers
        try:
            # ONE savepoint for the whole rule, including the per-customer N+1
            # below. A per-iteration savepoint was tried and reverted under this
            # change's own audit: there is no `try` inside the loop, so the first
            # failed read re-raises straight out to this rule's `except` either
            # way — the loop cannot continue, so the finer granularity buys
            # nothing. It is not free, though. Measured on `GET /next-actions`
            # at 100 candidate rows: SAVEPOINT/RELEASE 103 each against 3, and
            # 250-289 ms against 56-66 ms — 4.1-4.5x, +190-220 ms. `return_jobs`
            # has no LIMIT, so that is the ordinary case, not the tail.
            with contained_read(tenant_db):
                return_jobs = (
                    tenant_db.query(Job)
                    .filter(
                        Job.lifecycle_stage == "completed",
                        Job.is_return_visit.is_(True),
                        Job.deleted_at.is_(None),
                        Job.customer_id.isnot(None),
                    )
                    .all()
                )
                seen_customers: set[str] = set()
                for job in return_jobs:
                    cid = str(job.customer_id)
                    if cid in seen_customers:
                        continue
                    # Check no recent job for this customer
                    recent = (
                        tenant_db.query(Job)
                        .filter(
                            Job.customer_id == job.customer_id,
                            Job.created_at >= cutoff_180d,
                            Job.deleted_at.is_(None),
                        )
                        .first()
                    )
                    if recent is None:
                        seen_customers.add(cid)
                        actions.append({
                            "id": f"auto:schedule_maintenance:{cid}",
                            "tenant_id": tenant_id,
                            "user_id": None,
                            "action_type": "schedule_maintenance",
                            "title": "Schedule Maintenance Follow-Up",
                            "description": (
                                "This return-visit customer has had no activity in 180 days. "
                                "Schedule a maintenance check-up."
                            ),
                            "priority": "medium",
                            # No customer-scoped scheduling route exists. JobsView
                            # opens its new-job form from ?new=1 and pre-fills the
                            # customer from ?customer_id= (JobsView.vue:1410).
                            "action_url": f"/jobs?new=1&customer_id={cid}",
                            "estimated_value": 150.0,
                            "reference_id": cid,
                            "status": "pending",
                            "snoozed_until": None,
                            "completed_at": None,
                            "created_at": now.isoformat(),
                        })
        except Exception as exc:
            logger.warning(
                "auto schedule_maintenance rule failed for tenant %s: %s",
                tenant_id, exc,
            )

        actions.sort(key=lambda x: _PRIORITY_KEY.get(x.get("priority", "low"), 99))
        return actions


# Module-level singleton
queue = NextActionQueue()
