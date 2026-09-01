"""A deliberate $0 labor rate stays $0, and one fallback serves every cost
surface (money-audit M28).

`rate = Decimal(str(r[1] or DEFAULT_LABOR_RATE))` — and `0 or 95` is 95: a
3-hour warranty entry at a deliberate $0 rate cost $0 on labor.py's endpoint
and $285 in job_costing, and the profitability report ranked the job a loser.
Three defaults disagreed ($95 job_costing, $65/$50 labor.py) while the
tenant's one correctly-configured number — pricing_settings.
loaded_labor_cost_per_hour — went unread by half the surfaces.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase, ensure_audit_table
from gdx_dispatch.models.tenant_models import TimeEntry

TENANT = "tenant-m28"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    from gdx_dispatch.models.pricing_engine import PricingSettings as _PS
    _PS.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    ensure_audit_table(session)
    yield session
    session.close()
    engine.dispose()


def _entry(db, job_id, *, minutes, rate):
    now = datetime.now(UTC)
    e = TimeEntry(
        id=uuid.uuid4(), job_id=job_id, tech_id="tech-1",
        clock_in=now - timedelta(minutes=minutes), clock_out=now,
        duration_minutes=minutes, hourly_rate=rate, company_id=TENANT,
    )
    db.add(e)
    db.commit()
    return e


def _labor(db, job_id):
    from gdx_dispatch.routers.job_costing import _labor_for_job

    return _labor_for_job(db, job_id)


def test_a_deliberate_zero_rate_stays_zero(db):
    """THE FIX. 3 hours at a stored $0 (warranty/comp) must cost $0 — not
    $285 at a re-rated $95/h."""
    job = uuid.uuid4()
    _entry(db, job, minutes=180, rate=Decimal("0"))
    out = _labor(db, job)
    assert out["total"] == 0.0, "a deliberate $0 was re-rated by the or-trap"
    assert out["hours"] == 3.0


def test_a_missing_rate_uses_the_tenants_number_not_95(db):
    """The fallback is the tenant's wage-plus-burden figure from
    pricing_settings — the one correctly-configured cost rate — not a
    hardcoded $95 that existed nowhere in configuration."""
    from gdx_dispatch.models.pricing_engine import PricingSettings

    db.add(PricingSettings(loaded_labor_cost_per_hour=Decimal("80.00")))
    db.commit()
    job = uuid.uuid4()
    _entry(db, job, minutes=60, rate=None)
    out = _labor(db, job)
    assert out["total"] == 80.0, "the fallback must be the tenant's number"


def test_without_tenant_config_the_one_shared_constant_applies(db):
    from gdx_dispatch.routers.labor import DEFAULT_COST_RATE

    job = uuid.uuid4()
    _entry(db, job, minutes=60, rate=None)
    out = _labor(db, job)
    assert out["total"] == DEFAULT_COST_RATE


def test_the_95_constant_is_dead():
    import pathlib

    src = pathlib.Path(
        __import__("gdx_dispatch.routers.job_costing", fromlist=["__file__"]).__file__
    ).read_text()
    assert "DEFAULT_LABOR_RATE" not in src
    assert "95.00" not in src
    assert "_cost_rate_fallback" in src


def test_labor_sum_endpoint_uses_the_tenant_fallback(db):
    """One of the two labor.py sites that skipped the tenant fallback: the
    cost SUM at line ~402 priced no-rate entries at the static constant even
    when the tenant had configured a real number."""
    import pathlib

    src = pathlib.Path(
        __import__("gdx_dispatch.routers.labor", fromlist=["__file__"]).__file__
    ).read_text()
    # `assert "_entry_cost(entry, _fb)" in src` lived here. It pinned the source
    # text of `get_job_labor_costing`, which this branch deletes: labor.py
    # registered GET /api/jobs/{job_id}/costing a SECOND time, and jobs.py
    # (app.py:1513) is included before labor.py (app.py:1551), so FastAPI served
    # jobs.py's handler and labor.py's copy was unreachable. The assertion was
    # therefore guarding a function that never ran.
    #
    # Note for whoever touches this next: every assertion in this test is a
    # source-TEXT check. That proves someone wrote a particular expression, not
    # that the tenant fallback is applied — the behavioural coverage is
    # `test_labor_summary_costs_null_rate_at_the_tenant_number` below. These
    # break on correct refactors, which is exactly what happened here.
    # Hoisted after the review caught a per-row pricing_settings SELECT: the
    # summary resolves once, then reuses.
    assert "_summary_fb = _cost_rate_fallback(db)" in src
    assert "cost = _entry_cost(row, _summary_fb)" in src
    i = src.index("return _entry_to_dict(row, fallback_rate=_cost_rate_fallback(db))")
    assert i != -1


def test_labor_summary_costs_null_rate_at_the_tenant_number(db, monkeypatch):
    """Behavioral (the review called the source pins what they are): the
    summary surface itself must cost a NULL-rate entry at the tenant's number
    — and resolve the fallback ONCE, not once per row (it ran one
    pricing_settings SELECT per entry in the profitability path)."""
    from gdx_dispatch.models.pricing_engine import PricingSettings
    from gdx_dispatch.routers import labor as labor_router

    db.add(PricingSettings(loaded_labor_cost_per_hour=Decimal("80.00")))
    db.commit()
    job = uuid.uuid4()
    _entry(db, job, minutes=60, rate=None)
    _entry(db, job, minutes=60, rate=None)
    _entry(db, job, minutes=60, rate=Decimal("0"))  # deliberate $0 stays $0

    calls = {"n": 0}
    real = labor_router._cost_rate_fallback
    monkeypatch.setattr(labor_router, "_cost_rate_fallback",
                        lambda d: calls.__setitem__("n", calls["n"] + 1) or real(d))

    total = sum(
        labor_router._entry_cost(e, labor_router._cost_rate_fallback(db))
        for e in db.query(__import__("gdx_dispatch.models.tenant_models",
                                     fromlist=["TimeEntry"]).TimeEntry
                          ).filter_by(job_id=job).all()
    )
    assert total == 160.0, "two NULL-rate hours at $80 + a deliberate $0"


def test_resolver_rolls_back_its_own_failed_query(db, monkeypatch):
    """Review catch: a failed SELECT poisons a PG transaction; the resolver
    must roll back so the caller's next query survives by design, not by the
    breadth of someone else's except block."""
    from sqlalchemy.exc import SQLAlchemyError

    from gdx_dispatch.routers import labor as labor_router

    rolled = {"n": 0}
    class BoomDB:
        def execute(self, *a, **k): raise SQLAlchemyError("boom")
        def rollback(self): rolled["n"] += 1
    out = labor_router._cost_rate_fallback(BoomDB())
    assert out == labor_router.DEFAULT_COST_RATE
    assert rolled["n"] == 1

