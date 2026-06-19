"""Tests for the AI Health Score system (gdx_dispatch/core/health_score.py)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

# Ensure the repo root is on sys.path so gdx is importable without install
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from uuid import uuid4

from gdx_dispatch.core.health_score import (  # noqa: E402
    TenantHealthLog,
    TenantHealthScore,
    _score_to_grade,
    compute_health_score,
    get_retention_playbook,
)

# ---------------------------------------------------------------------------
# Grade assignment
# ---------------------------------------------------------------------------

def test_grade_A():
    assert _score_to_grade(85) == "A"
    assert _score_to_grade(80) == "A"


def test_grade_B():
    assert _score_to_grade(70) == "B"
    assert _score_to_grade(65) == "B"


def test_grade_C():
    assert _score_to_grade(55) == "C"
    assert _score_to_grade(50) == "C"


def test_grade_D():
    assert _score_to_grade(40) == "D"
    assert _score_to_grade(35) == "D"


def test_grade_F():
    assert _score_to_grade(25) == "F"
    assert _score_to_grade(0) == "F"
    assert _score_to_grade(34.9) == "F"


def test_grade_boundary_exact():
    """Boundary values must land in the right bucket."""
    assert _score_to_grade(79.9) == "B"
    assert _score_to_grade(64.9) == "C"
    assert _score_to_grade(49.9) == "D"


# ---------------------------------------------------------------------------
# Retention playbook
# ---------------------------------------------------------------------------

def _make_score(grade: str, score: float = 50.0) -> TenantHealthScore:
    return TenantHealthScore(
        tenant_id="test-tenant",
        score=score,
        grade=grade,
        signals={},
        computed_at=datetime.now(timezone.utc),
        playbook_triggered=None,
    )


def test_retention_playbook_urgent_outreach():
    assert get_retention_playbook(_make_score("F", 20)) == "urgent_outreach"


def test_retention_playbook_check_in_call():
    assert get_retention_playbook(_make_score("D", 40)) == "check_in_call"


def test_retention_playbook_feature_adoption_email():
    assert get_retention_playbook(_make_score("C", 55)) == "feature_adoption_email"


def test_retention_playbook_none_for_B():
    assert get_retention_playbook(_make_score("B", 70)) is None


def test_retention_playbook_none_for_A():
    assert get_retention_playbook(_make_score("A", 90)) is None


# ---------------------------------------------------------------------------
# compute_health_score — uses in-memory SQLite via conftest fixtures
# ---------------------------------------------------------------------------

def test_compute_health_score_empty_db(tenant_db, control_db):
    """Empty DB should return score 0 and grade F."""
    hs = compute_health_score("tenant-abc", tenant_db, control_db)
    assert isinstance(hs, TenantHealthScore)
    assert hs.score == 0.0
    assert hs.grade == "F"
    assert hs.playbook_triggered == "urgent_outreach"
    assert set(hs.signals.keys()) == {
        "jobs_last_30d",
        "invoices_sent_30d",
        "login_frequency_7d",
        "feature_adoption",
        "payment_velocity",
    }


def test_compute_health_score_with_jobs(tenant_db, control_db):
    """30 recent jobs should contribute 30 pts."""
    from gdx_dispatch.models.tenant_models import Customer, Job

    now = datetime.now(timezone.utc)
    cust = Customer(name="Acme", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    for i in range(30):
        tenant_db.add(
            Job(
                title=f"Job {i}",
                created_at=now - timedelta(days=i % 28),
                company_id="tenant-test",
            )
        )
    tenant_db.commit()

    hs = compute_health_score("tenant-abc", tenant_db, control_db)
    assert hs.signals["jobs_last_30d"] == pytest.approx(30.0)
    assert hs.score >= 30.0


def test_compute_health_score_with_paid_invoices(tenant_db, control_db):
    """Paid invoices in last 90d should boost payment_velocity signal."""
    from gdx_dispatch.models.tenant_models import Customer, Invoice, Job

    now = datetime.now(timezone.utc)
    cust = Customer(name="Acme", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    job = Job(title="Job A", created_at=now - timedelta(days=5), company_id="tenant-test")
    tenant_db.add(job)
    tenant_db.flush()

    # 5 paid out of 5 total = 100% velocity → 15 pts
    for i in range(5):
        tenant_db.add(
            Invoice(
                customer_id=uuid4(),
                job_id=job.id,
                invoice_number=f"INV-00{i}",
                status="paid",
                paid_at=now - timedelta(days=i),
                sent_at=now - timedelta(days=i + 1),
                public_token=f"tok-{i}",
                created_at=now - timedelta(days=i + 2),
                company_id="tenant-test",
            )
        )
    tenant_db.commit()

    hs = compute_health_score("tenant-abc", tenant_db, control_db)
    assert hs.signals["payment_velocity"] == pytest.approx(15.0)


def test_compute_health_score_feature_adoption(tenant_db, control_db):
    """Module grants in control_db should drive feature_adoption signal."""
    import uuid

    from gdx_dispatch.control.models import TenantModuleGrant

    tenant_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Grant 6 modules; score = 6 / NUM_MODULES * 15
    from gdx_dispatch.core.health_score import _NUM_MODULES
    for key in ["quickbooks", "inventory", "timeclock", "customer_portal", "equipment_tracking", "fleet"]:
        control_db.add(
            TenantModuleGrant(
                tenant_id=uuid.UUID(tenant_id),
                module_key=key,
                granted_at=now,
            )
        )
    control_db.commit()

    expected = round(6 / _NUM_MODULES * 15.0, 2)
    hs = compute_health_score(tenant_id, tenant_db, control_db)
    assert hs.signals["feature_adoption"] == pytest.approx(expected)


def test_health_score_signals_dict_has_all_keys(tenant_db, control_db):
    hs = compute_health_score("any-tenant", tenant_db, control_db)
    for key in ("jobs_last_30d", "invoices_sent_30d", "login_frequency_7d", "feature_adoption", "payment_velocity"):
        assert key in hs.signals, f"Missing signal: {key}"


# ---------------------------------------------------------------------------
# TenantHealthLog model
# ---------------------------------------------------------------------------

def test_tenant_health_log_model_fields():
    """TenantHealthLog must have the expected columns."""
    cols = {c.name for c in TenantHealthLog.__table__.columns}
    assert "id" in cols
    assert "tenant_id" in cols
    assert "score" in cols
    assert "grade" in cols
    assert "playbook" in cols
    assert "signals" in cols
    assert "computed_at" in cols
