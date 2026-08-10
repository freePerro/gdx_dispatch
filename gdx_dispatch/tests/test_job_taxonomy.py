"""job_type taxonomy — plan §9. One vocabulary, one lane resolver, no drift.

Prod 2026-07-29 held four spellings of two work kinds because two dropdowns
and four backend writers each invented their own list. The vocabulary now
lives in core/job_taxonomy.py with a JS mirror; these tests pin:

1. The alias folding (every observed prod spelling + the latent code-only
   ones reach their canonical form; unknown text passes through unchanged).
2. The lane rules Doug decided: Service Call/Repair/Maintenance → service
   (hourly); Installation → install (flat); everything else — including the
   159 QB Import rows and NULL — → office (never auto-priced).
3. PY↔JS PARITY: the dropdown list in frontend/src/constants/jobTypes.js is
   byte-equal to JOB_TYPE_OPTIONS. Two lists drifting apart is the original
   bug; this is the brake.
4. The writers actually import the constants (service_calls, JobCreate
   default) and the service-call queue filters on the LANE, not a literal.
5. Migration 042 folds 'Service' → 'Service Call' and records the audit
   batch event.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gdx_dispatch.core.job_taxonomy import (
    INSTALLATION,
    JOB_TYPE_OPTIONS,
    MAINTENANCE,
    QB_IMPORT,
    REPAIR,
    SERVICE_CALL,
    SERVICE_LANE_TYPES,
    canonical_job_type,
    pricing_lane,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("raw", "canon"),
    [
        ("Service", SERVICE_CALL),
        ("service", SERVICE_CALL),
        ("Service Call", SERVICE_CALL),
        ("SERVICE  CALL", SERVICE_CALL),
        ("installation", INSTALLATION),
        ("Install", INSTALLATION),
        ("Installation", INSTALLATION),
        ("repair", REPAIR),
        ("Maintenance", MAINTENANCE),
        ("QB Import", QB_IMPORT),
        ("QuickBooks Import", QB_IMPORT),
    ],
)
def test_aliases_fold_to_canonical(raw: str, canon: str) -> None:
    assert canonical_job_type(raw) == canon


def test_unknown_text_passes_through_unchanged() -> None:
    """Never guess a work kind — unknowns keep their spelling and land in the
    office lane."""
    assert canonical_job_type("Garage Cleanout") == "Garage Cleanout"
    assert canonical_job_type(None) is None
    assert canonical_job_type("  ") is None


@pytest.mark.parametrize(
    ("jt", "lane"),
    [
        (SERVICE_CALL, "service"),
        ("Service", "service"),  # legacy spelling still lanes correctly
        (REPAIR, "service"),
        (MAINTENANCE, "service"),
        (INSTALLATION, "install"),
        ("install", "install"),
        (QB_IMPORT, "office"),
        ("New Construction", "office"),
        ("Inspection", "office"),
        ("Other", "office"),
        (None, "office"),
        ("mystery text", "office"),
    ],
)
def test_pricing_lane(jt: str | None, lane: str) -> None:
    assert pricing_lane(jt) == lane


def test_lifecycle_stage_values_are_not_job_types() -> None:
    """The standing category-error guard: lifecycle_stage's 'service_call'
    value must never be fed to the lane resolver as if it were a work kind —
    but if someone does, it must still not price as service... it WILL fold
    ('service call' alias). What we can pin: the resolver's docstring carries
    the rule, and the Job enum values aren't silently in the dropdown list."""
    assert "service_call" not in JOB_TYPE_OPTIONS
    assert "scheduled" not in JOB_TYPE_OPTIONS


def test_js_mirror_matches_python() -> None:
    """The two dropdowns diverging is the ORIGINAL bug. The JS mirror must
    list exactly JOB_TYPE_OPTIONS, in order, and the same service lane."""
    js = (REPO / "frontend/src/constants/jobTypes.js").read_text(encoding="utf-8")

    def _extract(name: str) -> list[str]:
        m = re.search(rf"{name}\s*=\s*\[(.*?)\]", js, re.S)
        assert m, f"{name} missing from jobTypes.js"
        return re.findall(r"'([^']+)'", m.group(1))

    assert _extract("JOB_TYPE_OPTIONS") == list(JOB_TYPE_OPTIONS)
    assert _extract("SERVICE_LANE_TYPES") == list(SERVICE_LANE_TYPES)


def test_writers_import_the_constants() -> None:
    sc = (REPO / "routers/service_calls.py").read_text(encoding="utf-8")
    assert "job_type=SERVICE_CALL" in sc, "service_calls writer stopped using the constant"
    assert 'Job.job_type.in_(SERVICE_LANE_TYPES)' in sc, (
        "the service-call queue no longer filters on the lane — legacy-"
        "spelled or Repair/Maintenance jobs go invisible again"
    )
    assert '== "Service Call"' not in sc, "a literal comparison crept back in"

    jobs = (REPO / "routers/jobs.py").read_text(encoding="utf-8")
    assert "default=SERVICE_CALL" in jobs, "JobCreate default diverged again"
    # Tight patterns: an invoice-line DESCRIPTION fallback also says
    # `or "Service"`, and that one is fine — only job_type fallbacks matter.
    assert 'job_type=payload.job_type or "Service"' not in jobs
    assert 'job_type=original.job_type or "Service"' not in jobs


def test_dropdowns_import_the_shared_list() -> None:
    for view in ("views/JobsView.vue", "views/CustomerDetailView.vue"):
        src = (REPO / "frontend/src" / view).read_text(encoding="utf-8")
        assert "JOB_TYPE_OPTIONS" in src, f"{view} stopped importing the shared list"
        assert not re.search(r"jobTypeOptions\s*=\s*\[\s*['\"]", src), (
            f"{view} re-inlined its own job-type list — that divergence is "
            "the original bug"
        )
    mob = (REPO / "frontend/src/components/MobileJobNewDialog.vue").read_text(encoding="utf-8")
    # The rendered control, not just the import (audit round 2: the earlier
    # pin passed with the whole <Select> deleted).
    assert 'data-testid="mjn-job-type"' in mob, (
        "the mobile dialog lost its job_type picker control (plan §14 gap 1)"
    )
    assert 'v-model="job.job_type"' in mob
    assert "DEFAULT_JOB_TYPE" in mob, (
        "the mobile dialog re-inlined the default spelling as a literal — "
        "the drift pattern this file exists to brake"
    )


def test_migration_042_backfills_and_audits() -> None:
    mig = (REPO / "migrations/versions/042_job_type_canonicalize.py").read_text(
        encoding="utf-8"
    )
    assert "UPDATE jobs SET job_type = 'Service Call' WHERE job_type = 'Service'" in mig
    assert "log_audit_event_sync" in mig, "the backfill lost its audit record"
    assert "job_ids" in mig, "the audit event must carry the affected rows"
    assert "QB Import" in mig, "the QB Import carve-out rationale disappeared"


def test_model_default_and_omitting_writers() -> None:
    """Audit round 2: the 042 backfill was a mop under a running leak — the
    MODEL default was still 'Service' and four constructors omitted job_type,
    minting the dead spelling on a schedule (template materializer, service
    triggers, proposal conversion, raw lead INSERTs). Pin the closures.

    Proposal conversion dropped out in migration 061: the standalone `proposals`
    table and its /api/proposals/{id}/convert-to-job endpoint (the only Job
    constructor in sub_resources.py) were retired, so there is no longer a
    conversion path there to pin. Good/better/best moved onto the estimate,
    which makes estimates.py's convert path the ONLY sold-work → job closure
    left — and it was never pinned, so the assertion moves there rather than
    disappearing with the endpoint it used to guard.
    """
    from gdx_dispatch.models.tenant_models import Job

    assert Job.__table__.c.job_type.default.arg == SERVICE_CALL, (
        "Job.job_type's model default reverted — every constructor that "
        "omits job_type mints a non-canonical spelling again"
    )

    jt = (REPO / "routers/job_templates.py").read_text(encoding="utf-8")
    assert "job_type=canonical_job_type(template.job_type)" in jt, (
        "the template materializer dropped the template's job_type again"
    )
    st = (REPO / "routers/service_triggers.py").read_text(encoding="utf-8")
    assert "job_type=MAINTENANCE" in st, (
        "maintenance-agreement auto-jobs lost their job_type"
    )
    # Sold work = Installation. This is the estimate→job convert path, which
    # inherited the rule when proposal conversion was retired (061). It writes
    # the literal rather than the INSTALLATION constant; assert on the value so
    # the guard pins the BEHAVIOR, and accept either spelling so switching to
    # the constant is not a false failure.
    est = (REPO / "routers/estimates.py").read_text(encoding="utf-8")
    assert ('job_type="Installation"' in est) or ("job_type=INSTALLATION" in est), (
        "estimate conversion lost its job_type (sold work = Installation)"
    )
    sr = (REPO / "routers/sub_resources.py").read_text(encoding="utf-8")
    assert "Job(" not in sr, (
        "sub_resources.py constructs a Job again — any new Job writer must set "
        "job_type explicitly or it mints the model default (Service Call)"
    )
    for path in ("api/public_router.py",):
        src = (REPO / path).read_text(encoding="utf-8")
        assert "'Service Call'" in src and "job_type" in src, (
            f"{path}'s raw lead INSERT no longer sets job_type — website-lead "
            "jobs land as NULL → office lane, invisible to the service queue"
        )

    jobs = (REPO / "routers/jobs.py").read_text(encoding="utf-8")
    assert 'updates["job_type"] = canonical_job_type(data["job_type"])' in jobs, (
        "the update path stopped canonicalizing — a stale SPA tab can write "
        "the dead spelling back after the backfill"
    )
