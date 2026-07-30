"""Canonicalize job_type: 'Service' → 'Service Call' — plan §9.

Two frontend dropdowns each carried their own job-type list and disagreed on
the service spelling, so prod accumulated `Service Call` (34) and `Service`
(12) for the same work kind. Every literal-comparing reader then missed one
of them: the service-call queue was blind to the 12 `Service` jobs for
months. The vocabulary now lives in core/job_taxonomy.py and both dropdowns
import one list; this migration folds the existing rows onto the canonical
spelling (decided by Doug 2026-07-29: "Service Call" — the majority spelling,
the dedicated reader's literal, and the business term).

`QB Import` rows are DELIBERATELY untouched: that value is import provenance
wearing a work-kind field, 159 rows of history recognize imports by it, and
the pricing lane already routes it to "office" (never auto-priced).

Backfills are office-visible data mutations, so this one writes a
hash-chained audit event through the same helper the app uses
(core/audit.log_audit_event_sync computes prev_hash correctly — a raw INSERT
here would break verify_audit_chain). The event carries the affected job ids;
at 12 rows that is the complete before/after record, not a sample.

Idempotent: a re-run finds zero rows and writes no event.

Revision ID: 042_job_type_canonicalize
Revises: 041_job_closeout_supersede
"""
from alembic import op

revision = "042_job_type_canonicalize"
down_revision = "041_job_closeout_supersede"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    rows = bind.exec_driver_sql(
        "SELECT CAST(id AS TEXT) AS id, company_id FROM jobs "
        "WHERE job_type = 'Service'"
    ).fetchall()

    if rows:
        bind.exec_driver_sql(
            "UPDATE jobs SET job_type = 'Service Call' WHERE job_type = 'Service'"
        )

    # The vocabulary also lives in DATA that exact-matches against
    # jobs.job_type (audit round 2): quote templates (ai_quote matches
    # QuoteTemplate.job_type == the job's) and job templates (the
    # materializer now carries template.job_type onto the job). Both tables
    # are empty on dev/prod today, so this is insurance, not surgery.
    for tbl in ("quote_templates", "job_templates"):
        bind.exec_driver_sql(
            f"UPDATE {tbl} SET job_type = 'Service Call' "
            "WHERE job_type IN ('Service', 'service')"
        )

    if not rows:
        return

    # Chained audit record — §13: a backfill that leaves no trace reads as
    # "nothing happened" when the office asks why a job changed.
    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import log_audit_event_sync

    session = Session(bind=bind)
    tenant_ids = sorted({r.company_id for r in rows if r.company_id})
    log_audit_event_sync(
        session,
        tenant_id=(tenant_ids[0] if len(tenant_ids) == 1 else None),
        user_id="migration:042_job_type_canonicalize",
        action="job_type_backfill",
        entity_type="jobs",
        entity_id="batch",
        details={
            "from": "Service",
            "to": "Service Call",
            "count": len(rows),
            "job_ids": [r.id for r in rows],
            "tenants": tenant_ids,
        },
    )
    session.flush()


def downgrade() -> None:
    # Deliberately a no-op: the canonical spelling is not information loss
    # (the audit event records exactly which rows changed), and rewriting
    # them back would resurrect the split-vocabulary bug.
    pass
