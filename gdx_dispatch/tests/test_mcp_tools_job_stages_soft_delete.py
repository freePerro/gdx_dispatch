"""MCP tools against real SQLite rows: open stages, soft delete, stage preview.

GDXA-1881. jobs.list's "open" filter left out service_call, the default stage
of every new job, and it and three siblings returned soft-deleted rows
(invariant #2). jobs.update_status called db.get on a local empty class —
it raised on a real session and passed only against a mock — and reported
`updated` without writing. Every test here runs through invoke_tool on a real
session, because a mock db.get returns whatever it is told to.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import gdx_dispatch.core.mcp_tools.estimates_create_draft  # noqa: F401
import gdx_dispatch.core.mcp_tools.get_job_detail  # noqa: F401
import gdx_dispatch.core.mcp_tools.jobs_update_status  # noqa: F401
import gdx_dispatch.core.mcp_tools.list_invoices  # noqa: F401
import gdx_dispatch.core.mcp_tools.list_jobs  # noqa: F401
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job

TENANT = "00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@dataclass
class _P:
    tenant_id: str = TENANT
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=lambda: [
        ("read", "job"), ("read", "invoice"), ("read", "customer"),
        ("write", "estimate"), ("read", "estimate"),
    ])


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _customer(db, *, deleted=False) -> Customer:
    c = Customer(name="Pat Example", company_id=TENANT,
                 deleted_at=NOW if deleted else None)
    db.add(c)
    db.flush()
    return c


def _job(db, title, *, stage=None, deleted=False, customer=None, age_days=0) -> Job:
    j = Job(title=title, company_id=TENANT,
            customer_id=customer.id if customer else None,
            created_at=NOW - timedelta(days=age_days),
            deleted_at=NOW if deleted else None)
    if stage is not None:
        j.lifecycle_stage = stage
    db.add(j)
    db.flush()
    return j


async def _call(db, tool, payload):
    return await invoke_tool(tool, payload, principal=_P(), db=db)


# --- jobs.list ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_includes_a_new_job_at_its_default_stage(db):
    new = _job(db, "New service call")  # no stage given: the column default
    db.commit()
    db.refresh(new)
    assert new.lifecycle_stage == "service_call"
    r = await _call(db, "jobs.list", {"status": "open"})
    assert r.ok, r.error_body
    assert [j["title"] for j in r.result["jobs"]] == ["New service call"]
    assert r.result["jobs"][0]["lifecycle_stage"] == "service_call"


@pytest.mark.asyncio
async def test_open_is_every_stage_but_completed_and_cancelled(db):
    for i, stage in enumerate(("lead", "service_call", "estimate", "scheduled",
                               "in_progress", "completed", "cancelled")):
        _job(db, stage, stage=stage, age_days=i)
    db.commit()
    r = await _call(db, "jobs.list", {"status": "open"})
    assert r.ok, r.error_body
    assert sorted(j["title"] for j in r.result["jobs"]) == sorted(
        ("lead", "service_call", "estimate", "scheduled", "in_progress"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["open", "scheduled", "completed", "all"])
async def test_soft_deleted_jobs_never_listed(db, status):
    stage = "completed" if status == "completed" else "scheduled"
    _job(db, "live", stage=stage)
    _job(db, "deleted", stage=stage, deleted=True)
    db.commit()
    r = await _call(db, "jobs.list", {"status": status})
    assert r.ok, r.error_body
    assert [j["title"] for j in r.result["jobs"]] == ["live"]


@pytest.mark.asyncio
async def test_newest_first_and_truncation_drops_the_oldest(db):
    for i in range(52):
        _job(db, f"job {i:02d}", age_days=i)
    db.commit()
    r = await _call(db, "jobs.list", {"status": "all"})
    assert r.ok, r.error_body
    titles = [j["title"] for j in r.result["jobs"]]
    assert r.result["truncated"] is True
    assert titles == [f"job {i:02d}" for i in range(50)]


@pytest.mark.asyncio
async def test_customer_filter_matches_a_dashed_id_on_sqlite(db):
    c = _customer(db)
    _job(db, "theirs", customer=c)
    _job(db, "someone else's")
    db.commit()
    r = await _call(db, "jobs.list", {"customer_id": str(c.id)})
    assert r.ok, r.error_body
    assert [j["title"] for j in r.result["jobs"]] == ["theirs"]


# --- jobs.detail -------------------------------------------------------------

@pytest.mark.asyncio
async def test_detail_finds_a_live_job_and_not_a_deleted_one(db):
    live = _job(db, "live")
    gone = _job(db, "gone", deleted=True)
    db.commit()
    r = await _call(db, "jobs.detail", {"job_id": str(live.id)})
    assert r.ok and r.result["job"]["title"] == "live"
    r = await _call(db, "jobs.detail", {"job_id": str(gone.id)})
    assert r.ok and r.result["job"] is None


# --- invoices.list -----------------------------------------------------------

@pytest.mark.asyncio
async def test_soft_deleted_invoices_never_listed(db):
    c = _customer(db)
    for n, deleted in (("INV-1", False), ("INV-2", True)):
        db.add(Invoice(invoice_number=n, public_token=uuid4().hex,
                       customer_id=c.id, company_id=TENANT,
                       deleted_at=NOW if deleted else None))
    db.commit()
    r = await _call(db, "invoices.list", {})
    assert r.ok, r.error_body
    assert [i["invoice_number"] for i in r.result["invoices"]] == ["INV-1"]


# --- estimates.create_draft --------------------------------------------------

@pytest.mark.asyncio
async def test_draft_refuses_a_deleted_customer_or_job(db):
    gone = _customer(db, deleted=True)
    live = _customer(db)
    gone_job = _job(db, "gone", customer=live, deleted=True)
    db.commit()
    # Both the staged first call and the Apply must fail: a returned error
    # dict reads as success, and Apply would answer "Applied".
    for payload in ({"customer_id": str(gone.id)},
                    {"customer_id": str(live.id), "job_id": str(gone_job.id)}):
        r = await _call(db, "estimates.create_draft", payload)
        assert r.error_type == "approval_required"
        assert "result" not in r.error_body
        r = await invoke_tool("estimates.create_draft", payload, principal=_P(),
                              db=db, approval_ref="ok")
        assert r.ok is False and r.error_type == "execution_error"
        assert "not found" in r.error_body["detail"]
    from gdx_dispatch.modules.proposals.models import Estimate
    assert db.query(Estimate).count() == 0


# --- jobs.update_status ------------------------------------------------------

@pytest.mark.asyncio
async def test_update_status_previews_and_never_writes(db):
    j = _job(db, "J", stage="service_call")
    db.commit()
    r = await _call(db, "jobs.update_status",
                    {"job_id": str(j.id), "new_status": "scheduled"})
    assert r.ok, r.error_body
    assert r.result["applied"] is False
    assert r.result["allowed"] is True
    assert r.result["preview"] == {"job_id": str(j.id), "before": "service_call",
                                   "after": "scheduled"}
    assert "updated" not in r.result
    db.expire_all()
    assert db.get(Job, j.id).lifecycle_stage == "service_call"


@pytest.mark.asyncio
@pytest.mark.parametrize(("before", "after", "use"), [
    ("scheduled", "completed", "Close out"),
    ("scheduled", "cancelled", "Cancel job"),
    ("completed", "scheduled", "Re-open"),
])
async def test_update_status_reports_the_job_pages_refusal(db, before, after, use):
    j = _job(db, "J", stage=before)
    db.commit()
    r = await _call(db, "jobs.update_status",
                    {"job_id": str(j.id), "new_status": after})
    assert r.ok, r.error_body
    assert r.result["applied"] is False and r.result["allowed"] is False
    assert use in r.result["detail"]


@pytest.mark.asyncio
async def test_update_status_unknown_stage_and_deleted_job_are_errors(db):
    live = _job(db, "J")
    gone = _job(db, "gone", deleted=True)
    db.commit()
    r = await _call(db, "jobs.update_status",
                    {"job_id": str(live.id), "new_status": "done"})
    assert r.ok is False
    r = await _call(db, "jobs.update_status",
                    {"job_id": str(gone.id), "new_status": "scheduled"})
    assert r.ok is False and r.error_type == "execution_error"
    r = await _call(db, "jobs.update_status",
                    {"job_id": "not-a-uuid", "new_status": "scheduled"})
    assert r.ok is False and r.error_type == "execution_error"


def test_update_status_is_green_and_read_only():
    from gdx_dispatch.core.mcp_tools.jobs_update_status import DESCRIPTOR
    assert DESCRIPTOR.blast_radius == "green"
    assert not DESCRIPTOR.approval_required
    assert [tuple(c) for c in DESCRIPTOR.capabilities_required] == [("read", "job")]
