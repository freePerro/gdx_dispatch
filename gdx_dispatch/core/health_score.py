"""gdx_dispatch/core/health_score.py — AI Health Score system.

Computes per-tenant engagement scores and triggers retention playbooks.
Stores results in the control-plane TenantHealthLog table.
"""
from __future__ import annotations

import contextlib
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import JSON, Column, DateTime, Float, String, and_, create_engine, func, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.types import Uuid

from gdx_dispatch.control.models import Base, Tenant, TenantModuleGrant
from gdx_dispatch.core.database import SessionLocal, get_db
from gdx_dispatch.core.modules import MODULE_KEYS, require_role
from gdx_dispatch.models.tenant_models import Invoice, Job

logger = logging.getLogger(__name__)

_NUM_MODULES = len(MODULE_KEYS)  # 12


# ---------------------------------------------------------------------------
# Dataclass — lightweight result carrier (NOT the ORM model)
# ---------------------------------------------------------------------------

@dataclass
class TenantHealthScore:
    tenant_id: str
    score: float
    grade: str
    signals: dict
    computed_at: datetime
    playbook_triggered: str | None


# ---------------------------------------------------------------------------
# ORM model — stored in the control-plane DB
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TenantHealthLog(Base):
    __tablename__ = "tenant_health_logs"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(String(100), index=True, nullable=False)
    score = Column(Float, nullable=False)
    grade = Column(String(2), nullable=False)
    playbook = Column(String(50), nullable=True)
    signals = Column(JSON, nullable=True)
    computed_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _score_to_grade(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    if score >= 35:
        return "D"
    return "F"


def get_retention_playbook(score: TenantHealthScore) -> str | None:
    """Return the retention playbook key for this score, or None if healthy."""
    if score.grade == "F":
        return "urgent_outreach"
    if score.grade == "D":
        return "check_in_call"
    if score.grade == "C":
        return "feature_adoption_email"
    return None  # B or A — healthy


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_health_score(
    tenant_id: str,
    tenant_db: Session,
    control_db: Session,
) -> TenantHealthScore:
    """Compute a weighted health score from five engagement signals."""
    now = datetime.now(timezone.utc)
    cutoff_30 = now - timedelta(days=30)
    cutoff_7 = now - timedelta(days=7)
    cutoff_90 = now - timedelta(days=90)

    # Signal 1 — jobs created in last 30 days (max 30 pts)
    jobs_weight = 0.0
    try:
        jobs_count = (
            tenant_db.query(Job)
            .filter(Job.created_at >= cutoff_30, Job.deleted_at.is_(None))
            .count()
        )
        jobs_weight = min(30.0, jobs_count / 30.0 * 30.0)
    except Exception:
        logging.getLogger(__name__).exception("compute_health_score caught exception")
        pass

    # Signal 2 — invoices sent in last 30 days (max 20 pts)
    invoices_weight = 0.0
    try:
        inv_count = (
            tenant_db.query(Invoice)
            .filter(Invoice.sent_at >= cutoff_30, Invoice.deleted_at.is_(None))
            .count()
        )
        invoices_weight = min(20.0, inv_count / 10.0 * 20.0)
    except Exception:
        logging.getLogger(__name__).exception("compute_health_score caught exception")
        pass

    # Signal 3 — login frequency last 7 days (max 20 pts); table may not exist
    login_weight = 0.0
    try:
        login_count = tenant_db.execute(
            text("SELECT COUNT(*) FROM login_events WHERE created_at >= :cutoff"),
            {"cutoff": cutoff_7},
        ).scalar() or 0
        login_weight = min(20.0, login_count / 5.0 * 20.0)
    except Exception:
        logging.getLogger(__name__).exception("compute_health_score caught exception")
        pass

    # Signal 4 — feature adoption: granted modules / total modules (max 15 pts)
    adoption_weight = 0.0
    try:
        import uuid as _uuid
        tid_uuid = _uuid.UUID(tenant_id) if not isinstance(tenant_id, _uuid.UUID) else tenant_id
        granted = (
            control_db.query(TenantModuleGrant)  # noqa: T1 — TenantModuleGrant is control-plane
            .filter(TenantModuleGrant.tenant_id == tid_uuid)
            .count()
        )
        adoption_weight = min(15.0, granted / _NUM_MODULES * 15.0)
    except Exception:
        logging.getLogger(__name__).exception("compute_health_score caught exception")
        pass

    # Signal 5 — payment velocity last 90 days (max 15 pts)
    payment_weight = 0.0
    try:
        paid_count = (
            tenant_db.query(Invoice)
            .filter(
                Invoice.paid_at >= cutoff_90,
                Invoice.deleted_at.is_(None),
            )
            .count()
        )
        total_count = (
            tenant_db.query(Invoice)
            .filter(
                Invoice.created_at >= cutoff_90,
                Invoice.deleted_at.is_(None),
                Invoice.status != "void",
            )
            .count()
        )
        if total_count > 0:
            payment_weight = min(15.0, paid_count / total_count * 15.0)
    except Exception:
        logging.getLogger(__name__).exception("compute_health_score caught exception")
        pass

    total_score = jobs_weight + invoices_weight + login_weight + adoption_weight + payment_weight
    grade = _score_to_grade(total_score)

    hs = TenantHealthScore(
        tenant_id=tenant_id,
        score=round(total_score, 2),
        grade=grade,
        signals={
            "jobs_last_30d": round(jobs_weight, 2),
            "invoices_sent_30d": round(invoices_weight, 2),
            "login_frequency_7d": round(login_weight, 2),
            "feature_adoption": round(adoption_weight, 2),
            "payment_velocity": round(payment_weight, 2),
        },
        computed_at=now,
        playbook_triggered=None,  # set after get_retention_playbook call
    )
    hs.playbook_triggered = get_retention_playbook(hs)
    return hs


# ---------------------------------------------------------------------------
# Batch job
# ---------------------------------------------------------------------------

def _connect_tenant_db() -> Session:
    return SessionLocal()


async def run_health_score_job(control_db: Session) -> list[dict]:
    """Compute health scores for every active tenant and persist to TenantHealthLog."""
    results: list[dict] = []
    tenants = control_db.query(Tenant).filter(Tenant.deleted_at.is_(None)).all()

    for tenant in tenants:
        tenant_db: Session | None = None
        try:
            tenant_db = _connect_tenant_db()
            hs = compute_health_score(str(tenant.id), tenant_db, control_db)
            playbook = get_retention_playbook(hs)

            log = TenantHealthLog(
                tenant_id=str(tenant.id),
                score=hs.score,
                grade=hs.grade,
                playbook=playbook,
                signals=hs.signals,
                computed_at=hs.computed_at,
            )
            control_db.add(log)
            control_db.commit()

            results.append(
                {
                    "tenant_id": str(tenant.id),
                    "score": hs.score,
                    "grade": hs.grade,
                    "playbook": playbook,
                }
            )
        except Exception as exc:
            logger.warning("Health score job skipped tenant %s: %s", tenant.id, exc)
            with contextlib.suppress(Exception):
                control_db.rollback()
        finally:
            if tenant_db is not None:
                with contextlib.suppress(Exception):
                    tenant_db.close()

    return results


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/admin/health-scores", tags=["health-scores"])

_admin_dep = Depends(require_role("admin", "owner"))


@router.get("/")
def list_health_scores(
    control_db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> list[dict]:
    """Return the latest health log entry per tenant."""
    try:
        latest_subq = (
            control_db.query(
                TenantHealthLog.tenant_id,
                func.max(TenantHealthLog.computed_at).label("max_ts"),
            )
            .group_by(TenantHealthLog.tenant_id)
            .subquery()
        )
        rows = (
            control_db.query(TenantHealthLog)
            .join(
                latest_subq,
                and_(
                    TenantHealthLog.tenant_id == latest_subq.c.tenant_id,
                    TenantHealthLog.computed_at == latest_subq.c.max_ts,
                ),
            )
            .all()
        )
        return [
            {
                "tenant_id": r.tenant_id,
                "score": r.score,
                "grade": r.grade,
                "playbook": r.playbook,
                "signals": r.signals,
                "computed_at": r.computed_at.isoformat() if r.computed_at else None,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("list_health_scores: table may not exist yet")
        with contextlib.suppress(Exception):
            control_db.rollback()
        return []


@router.get("/{tenant_id}")
def get_tenant_health_score(
    tenant_id: str,
    control_db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> dict:
    """Recompute and return a live health score for one tenant."""
    try:
        tid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid tenant_id format") from None

    tenant = control_db.query(Tenant).filter(Tenant.id == tid).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant_db: Session | None = None
    try:
        tenant_db = _connect_tenant_db()
        hs = compute_health_score(str(tenant.id), tenant_db, control_db)
        return {
            "tenant_id": hs.tenant_id,
            "score": hs.score,
            "grade": hs.grade,
            "playbook": hs.playbook_triggered,
            "signals": hs.signals,
            "computed_at": hs.computed_at.isoformat(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Score computation failed: {exc}") from exc
    finally:
        if tenant_db is not None:
            with contextlib.suppress(Exception):
                tenant_db.close()


@router.post("/run-job")
async def trigger_health_score_job(
    control_db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> dict:
    """Trigger the health score batch job synchronously for all active tenants."""
    results = await run_health_score_job(control_db)
    return {"processed": len(results), "results": results}
