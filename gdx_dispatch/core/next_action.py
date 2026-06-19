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

from gdx_dispatch.core.audit import TenantBase, utcnow
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

        # Persisted actions from DB
        try:
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
    ) -> dict:
        """Mark a persisted action as completed."""
        try:
            action = (
                tenant_db.query(NextAction)
                .filter(
                    NextAction.id == _to_uuid(action_id),
                    NextAction.tenant_id == tenant_id,
                    NextAction.deleted_at.is_(None),
                )
                .first()
            )
            if action is None:
                return {"error": "Action not found", "id": action_id}
            action.status = "completed"
            action.completed_at = _utcnow()
            tenant_db.commit()
            return {"id": action_id, "status": "completed"}
        except Exception as exc:
            logger.warning(
                "complete_action failed for action %s: %s", action_id, exc
            )
            with contextlib.suppress(Exception):
                tenant_db.rollback()
            return {"error": str(exc), "id": action_id}

    def snooze_action(
        self,
        tenant_id: str,
        action_id: str,
        until_dt: datetime,
        tenant_db: Session,
    ) -> dict:
        """Defer a persisted action until a future datetime."""
        try:
            action = (
                tenant_db.query(NextAction)
                .filter(
                    NextAction.id == _to_uuid(action_id),
                    NextAction.tenant_id == tenant_id,
                    NextAction.deleted_at.is_(None),
                )
                .first()
            )
            if action is None:
                return {"error": "Action not found", "id": action_id}
            action.status = "snoozed"
            action.snoozed_until = until_dt
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
            return {"error": str(exc), "id": action_id}

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
    ) -> dict:
        """Create and persist a new next action."""
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
            tenant_db.add(action)
            tenant_db.commit()
            tenant_db.refresh(action)
            return _action_to_dict(action)
        except Exception as exc:
            logger.warning(
                "create_action failed for tenant %s: %s", tenant_id, exc
            )
            with contextlib.suppress(Exception):
                tenant_db.rollback()
            return {"error": str(exc)}

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
            stale_estimates = (
                tenant_db.query(Job)
                .filter(
                    Job.lifecycle_stage == "estimate",
                    Job.created_at < cutoff_72h,
                    Job.billing_status == "unbilled",
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
                    "action_url": f"/invoices/{inv.id}",
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
                        "action_url": f"/customers/{cid}/schedule",
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
