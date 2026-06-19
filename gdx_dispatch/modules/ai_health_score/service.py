from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Invoice, Job
from gdx_dispatch.modules.ai_health_score.models import TenantHealthScore


def compute_health_score(tenant_id: str, db: Session) -> TenantHealthScore:
    """Compute a health score for the tenant based on recent activity.

    Scoring formula: min(100, (jobs_30d * 3) + (invoices_30d * 2))
    Playbook: score < 20 -> "re_engagement", score < 50 -> "activation", else None
    """
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=30)

    jobs_30d_stmt = select(func.count(Job.id)).where(
        Job.created_at >= cutoff,
        Job.deleted_at.is_(None),
    )
    jobs_30d: int = db.execute(jobs_30d_stmt).scalar_one()

    invoices_30d_stmt = select(func.count(Invoice.id)).where(
        Invoice.created_at >= cutoff,
        Invoice.deleted_at.is_(None),
    )
    invoices_30d: int = db.execute(invoices_30d_stmt).scalar_one()

    raw_score = (jobs_30d * 3) + (invoices_30d * 2)
    score = min(100.0, float(raw_score))

    if score < 20:
        playbook = "re_engagement"
    elif score < 50:
        playbook = "activation"
    else:
        playbook = None

    health_score = TenantHealthScore(
        tenant_id=tenant_id,
        score=score,
        factors={
            "jobs_30d": jobs_30d,
            "invoices_30d": invoices_30d,
        },
        playbook_triggered=playbook,
    )
    db.add(health_score)
    db.commit()
    db.refresh(health_score)
    return health_score


def trigger_retention_playbook(health_score: TenantHealthScore) -> str:
    """Return the playbook name to trigger, or 'none' if no playbook applies."""
    return health_score.playbook_triggered or "none"
