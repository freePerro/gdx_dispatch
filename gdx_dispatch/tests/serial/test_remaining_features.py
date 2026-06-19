from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models as tenant_models  # noqa: F401
from gdx_dispatch.modules.proposals import models as proposal_models  # noqa: F401
from gdx_dispatch.routers import job_templates, recurring_jobs, referrals, reviews, search


@pytest.fixture()
def db_session() -> Session:
    tmp = tempfile.NamedTemporaryFile(suffix=".db")  # noqa: SIM115  # fixture holds handle for test lifetime
    engine = create_engine(f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False})
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
        tmp.close()


def _request(tenant_id: str = "tenant-1") -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": tenant_id}),
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _seed_customer(db: Session, *, name: str = "Customer A", email: str = "a@example.com") -> str:
    customer_id = uuid4().hex
    now = datetime.now(UTC).isoformat()
    db.execute(
        text(
            """
            INSERT INTO customers (id, name, name_hash, email, email_hash, phone, phone_hash, address, metadata, notes, source, company_id, created_at, deleted_at)
            VALUES (:id, :name, NULL, :email, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'tenant-test', :created_at, NULL)
            """
        ),
        {"id": customer_id, "name": name, "email": email, "created_at": now},
    )
    db.commit()
    return customer_id


def _seed_job(db: Session, *, customer_id: str, title: str = "Service Job") -> str:
    job_id = uuid4().hex
    now = datetime.now(UTC).isoformat()
    db.execute(
        text(
            """
            INSERT INTO jobs (id, customer_id, title, description, lifecycle_stage, dispatch_status, billing_status,
                              scheduled_at, completed_at, assigned_to, source, is_return_visit, parent_job_id, company_id, created_at, deleted_at)
            VALUES (:id, :customer_id, :title, NULL, 'scheduled', 'unassigned', 'unbilled',
                    NULL, NULL, NULL, NULL, 0, NULL, 'tenant-test', :created_at, NULL)
            """
        ),
        {"id": job_id, "customer_id": customer_id, "title": title, "created_at": now},
    )
    db.commit()
    return job_id


def _seed_invoice(db: Session, *, job_id: str, customer_id: str, number: str) -> str:
    invoice_id = uuid4().hex
    now = datetime.now(UTC).isoformat()
    db.execute(
        text(
            """
            INSERT INTO invoices (id, job_id, customer_id, invoice_number, billing_type, sequence_number, subtotal, tax_amount, total,
                                  balance_due, status, due_date, notes, locked, locked_at, sent_at, paid_at, public_token,
                                  company_id, created_at, deleted_at)
            VALUES (:id, :job_id, :customer_id, :invoice_number, 'standard', 1, 100, 0, 100,
                    100, 'draft', NULL, NULL, 0, NULL, NULL, NULL, :public_token,
                    'tenant-test', :created_at, NULL)
            """
        ),
        {
            "id": invoice_id,
            "job_id": job_id,
            "customer_id": customer_id,
            "invoice_number": number,
            "public_token": f"pub-{invoice_id[:8]}",
            "created_at": now,
        },
    )
    db.commit()
    return invoice_id


def _seed_estimate(db: Session, *, customer_id: str, number: str) -> str:
    estimate_id = uuid4().hex
    now = datetime.now(UTC).isoformat()
    db.execute(
        text(
            """
            INSERT INTO estimates (id, job_id, customer_id, estimate_number, label, notes, proposal_mode, total,
                                   status, sent_at, accepted_at, declined_at, declined_reason, accepted_tier_id,
                                   company_id, public_token, created_at, updated_at, deleted_at)
            VALUES (:id, NULL, :customer_id, :estimate_number, :label, NULL, 0, 0,
                    'draft', NULL, NULL, NULL, NULL, NULL,
                    'tenant-test', :public_token, :created_at, NULL, NULL)
            """
        ),
        {
            "id": estimate_id,
            "customer_id": customer_id,
            "estimate_number": number,
            "label": number,
            "public_token": f"tok-{estimate_id[:8]}",
            "created_at": now,
        },
    )
    db.commit()
    return estimate_id


# Recurring schedules (3+)
def test_recurring_create_and_list(db_session: Session):
    req = _request()
    user = {"id": "u1", "sub": "u1"}
    template = job_templates.create_job_template(
        payload=job_templates.JobTemplateCreateIn(
            title="Tune-up",
            job_type="maintenance",
            default_priority="normal",
            checklist=["inspect"],
            estimated_duration=90,
            default_parts=[],
        ),
        request=req,
        user=user,
        db=db_session,
    )

    created = recurring_jobs.create_recurring_schedule(
        payload=recurring_jobs.RecurringCreateIn(
            job_template_id=template["id"],
            frequency="weekly",
            customer_id=uuid4(),
            next_run=datetime.now(UTC) + timedelta(days=1),
        ),
        request=req,
        user=user,
        db=db_session,
    )
    listed = recurring_jobs.list_recurring_schedules(_=user, db=db_session)

    assert created["id"]
    assert any(item["id"] == created["id"] for item in listed["items"])


def test_recurring_patch_and_delete(db_session: Session):
    req = _request()
    user = {"id": "u1", "sub": "u1"}
    template = job_templates.create_job_template(
        payload=job_templates.JobTemplateCreateIn(
            title="Quarterly",
            job_type="maintenance",
            default_priority="normal",
            checklist=[],
            estimated_duration=60,
            default_parts=[],
        ),
        request=req,
        user=user,
        db=db_session,
    )
    created = recurring_jobs.create_recurring_schedule(
        payload=recurring_jobs.RecurringCreateIn(
            job_template_id=template["id"],
            frequency="weekly",
            customer_id=uuid4(),
            next_run=datetime.now(UTC) + timedelta(days=1),
        ),
        request=req,
        user=user,
        db=db_session,
    )

    patched = recurring_jobs.patch_recurring_schedule(
        schedule_id=created["id"],
        payload=recurring_jobs.RecurringPatchIn(frequency="biweekly", status="active"),
        request=req,
        user=user,
        db=db_session,
    )
    deleted = recurring_jobs.delete_recurring_schedule(created["id"], request=req, user=user, db=db_session)

    assert patched["frequency"] == "biweekly"
    assert deleted["ok"] is True


def test_recurring_materialize_due_schedule_creates_job(db_session: Session):

    customer_id = _seed_customer(db_session)
    template_id = str(uuid4())
    now = datetime.now(UTC)

    db_session.execute(
        text(
            """
            INSERT INTO job_templates
                (id, title, job_type, default_priority, checklist, estimated_duration, default_parts, is_active, created_at, updated_at, deleted_at)
            VALUES
                (:id, 'Recurring Tune', 'maintenance', 'normal', '[]', 45, '[]', 1, :created_at, :updated_at, NULL)
            """
        ),
        {"id": template_id, "created_at": now.isoformat(), "updated_at": now.isoformat()},
    )
    db_session.execute(
        text(
            """
            INSERT INTO recurring_job_schedules
                (id, job_template_id, frequency, customer_id, next_run, last_run, status, created_at, updated_at, deleted_at)
            VALUES
                (:id, :job_template_id, 'weekly', :customer_id, :next_run, NULL, 'active', :created_at, :updated_at, NULL)
            """
        ),
        {
            "id": str(uuid4()),
            "job_template_id": template_id,
            "customer_id": customer_id,
            "next_run": (now - timedelta(minutes=1)).isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
    )
    db_session.commit()

    result = recurring_jobs.materialize_due_recurring_jobs(db_session, now=now, actor_id="system", tenant_id="t-1")
    count = db_session.execute(text("SELECT COUNT(*) AS c FROM jobs")).mappings().first()["c"]

    assert result["created_count"] == 1
    assert int(count) == 1


# Job templates (3+)
def test_job_template_create_and_get(db_session: Session):
    req = _request()
    user = {"id": "u1"}
    created = job_templates.create_job_template(
        payload=job_templates.JobTemplateCreateIn(
            title="Install",
            job_type="install",
            default_priority="high",
            checklist=["measure"],
            estimated_duration=120,
            default_parts=[{"sku": "P-1"}],
        ),
        request=req,
        user=user,
        db=db_session,
    )
    got = job_templates.get_job_template(created["id"], _=user, db=db_session)

    assert got["id"] == created["id"]
    assert got["title"] == "Install"


def test_job_template_patch_and_delete(db_session: Session):
    req = _request()
    user = {"id": "u1"}
    created = job_templates.create_job_template(
        payload=job_templates.JobTemplateCreateIn(
            title="Repair",
            job_type="repair",
            default_priority="normal",
            checklist=[],
            estimated_duration=60,
            default_parts=[],
        ),
        request=req,
        user=user,
        db=db_session,
    )

    patched = job_templates.patch_job_template(
        created["id"],
        payload=job_templates.JobTemplatePatchIn(default_priority="low"),
        request=req,
        user=user,
        db=db_session,
    )
    deleted = job_templates.delete_job_template(created["id"], request=req, user=user, db=db_session)
    listed = job_templates.list_job_templates(_=user, db=db_session)

    assert patched["default_priority"] == "low"
    assert deleted["ok"] is True
    assert all(item["id"] != created["id"] for item in listed["items"])


def test_job_template_apply_creates_real_job(db_session: Session):
    req = _request()
    user = {"id": "u1"}
    created = job_templates.create_job_template(
        payload=job_templates.JobTemplateCreateIn(
            title="Seasonal Service",
            job_type="maintenance",
            default_priority="normal",
            checklist=[],
            estimated_duration=75,
            default_parts=[],
        ),
        request=req,
        user=user,
        db=db_session,
    )

    applied = job_templates.apply_job_template(
        created["id"],
        payload=job_templates.TemplateApplyIn(customer_id=uuid4(), scheduled_at=datetime.now(UTC)),
        request=req,
        user=user,
        db=db_session,
    )

    assert applied["title"] == "Seasonal Service"


# Reviews (3+)
def test_reviews_request_for_job(db_session: Session):
    customer_id = _seed_customer(db_session)
    job_id = _seed_job(db_session, customer_id=customer_id)

    out = reviews.request_review(job_id=job_id, request=_request(), user={"id": "u1"}, db=db_session)
    assert out["status"] == "requested"


def test_reviews_submit_and_stats(db_session: Session):
    reviews.submit_review(
        payload=reviews.ReviewSubmitIn(job_id=str(uuid4()), customer_id=str(uuid4()), rating=5, text="Great"),
        request=_request(),
        user={"id": "u1"},
        db=db_session,
    )
    stats = reviews.review_stats(trend_days=30, _={"id": "u1"}, db=db_session)

    assert stats["count"] >= 1
    assert stats["average_rating"] >= 1


def test_reviews_list_returns_items(db_session: Session):
    reviews.submit_review(
        payload=reviews.ReviewSubmitIn(job_id=str(uuid4()), customer_id=str(uuid4()), rating=4, text="Solid"),
        request=_request(),
        user={"id": "u1"},
        db=db_session,
    )
    listed = reviews.list_reviews(_={"id": "u1"}, db=db_session)

    assert len(listed["items"]) >= 1


# Referrals (3+)
def test_referrals_create_and_list(db_session: Session):
    req = _request()
    user = {"id": "u1"}
    created = referrals.create_referral(
        payload=referrals.ReferralCreateIn(
            referrer_id="cust-1",
            referee_name="New Customer",
            referee_phone="5551112222",
            referee_email="new@example.com",
        ),
        request=req,
        user=user,
        db=db_session,
    )
    listed = referrals.list_referrals(_=user, db=db_session)

    assert any(row["id"] == created["id"] for row in listed["items"])


def test_referrals_patch_status_progression(db_session: Session):
    req = _request()
    user = {"id": "u1"}
    created = referrals.create_referral(
        payload=referrals.ReferralCreateIn(
            referrer_id="cust-2",
            referee_name="Lead",
            referee_phone="5559990000",
            referee_email="lead@example.com",
        ),
        request=req,
        user=user,
        db=db_session,
    )

    referrals.patch_referral(created["id"], payload=referrals.ReferralPatchIn(status="converted"), request=req, user=user, db=db_session)
    rewarded = referrals.patch_referral(created["id"], payload=referrals.ReferralPatchIn(status="rewarded"), request=req, user=user, db=db_session)

    assert rewarded["status"] == "rewarded"
    assert int(rewarded["reward_given"]) == 1


def test_referrals_stats(db_session: Session):
    req = _request()
    user = {"id": "u1"}
    a = referrals.create_referral(
        payload=referrals.ReferralCreateIn(
            referrer_id="cust-3",
            referee_name="A",
            referee_phone="1111111111",
            referee_email="a@example.com",
        ),
        request=req,
        user=user,
        db=db_session,
    )
    referrals.patch_referral(a["id"], payload=referrals.ReferralPatchIn(status="converted"), request=req, user=user, db=db_session)

    referrals.create_referral(
        payload=referrals.ReferralCreateIn(
            referrer_id="cust-4",
            referee_name="B",
            referee_phone="2222222222",
            referee_email="b@example.com",
        ),
        request=req,
        user=user,
        db=db_session,
    )

    stats = referrals.referral_stats(_=user, db=db_session)
    assert stats["total_referrals"] >= 2
    assert stats["conversion_rate"] > 0


# Global search (3+)
def test_search_grouped_results_max_five(db_session: Session):
    for idx in range(6):
        customer_id = _seed_customer(db_session, name=f"Alex {idx}", email=f"alex{idx}@example.com")
        job_id = _seed_job(db_session, customer_id=customer_id, title=f"Fix Door {idx}")
        _seed_invoice(db_session, job_id=job_id, customer_id=customer_id, number=f"INV-S-{idx}")
        _seed_estimate(db_session, customer_id=customer_id, number=f"EST-S-{idx}")

    out = search.global_search(q="s", db=db_session)
    assert len(out["jobs"]) <= 5
    assert len(out["customers"]) <= 5
    assert len(out["invoices"]) <= 5
    assert len(out["estimates"]) <= 5


def test_search_no_matches_returns_empty_groups(db_session: Session):
    out = search.global_search(q="zzzz-no-match", db=db_session)
    assert out == {"jobs": [], "customers": [], "invoices": [], "estimates": []}


def test_search_router_has_auth_gate():
    # Search is available to any authenticated tenant user (no single module
    # owns it), so the router gates on require_role for all signed-in roles
    # rather than require_module. This test used to assert `dependencies == []`
    # which encoded an insecure posture — the router was reachable without
    # any role check. Fixed as part of the CLAUDE.md Build Rule gate sweep.
    assert search.router.dependencies, "search router must have at least one dependency"


def test_new_routers_registered_in_app_source():
    app_py = Path("gdx_dispatch/app.py").read_text(encoding="utf-8")
    assert "app.include_router(search_router.router if hasattr(search_router, \"router\") else search_router)" in app_py
    assert "app.include_router(reviews_router.router if hasattr(reviews_router, \"router\") else reviews_router)" in app_py
    assert "app.include_router(referrals_router.router if hasattr(referrals_router, \"router\") else referrals_router)" in app_py
    assert "app.include_router(\n        recurring_jobs_router.router if hasattr(recurring_jobs_router, \"router\") else recurring_jobs_router\n    )" in app_py
    assert "app.include_router(\n        job_templates_router.router if hasattr(job_templates_router, \"router\") else job_templates_router\n    )" in app_py
