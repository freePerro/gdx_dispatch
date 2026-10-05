"""MCP read tools run against a real Session, not only a mock — GDXA-209.

``schedule.lookup``, ``revenue.summary``, ``customers.lifetime_analysis`` and
``technicians.activity`` each passed a bare SQL string to ``db.execute``;
``invoices.aging_summary`` called ``db.select``, which a Session lacks.
SQLAlchemy 2 refuses that (``ArgumentError: Textual SQL expression ... should
be explicitly declared as text(...)``), so every call through the real MCP
mount failed; their unit tests stayed green because a ``MagicMock`` accepts
anything. A mock proves which arguments were passed, never what comes back, so
each test here builds the tables from the ORM on SQLite and goes through
``invoke_tool`` the way the mount does.

``schedule.lookup`` also listed cancelled jobs: cancelling sets
``lifecycle_stage`` and clears nothing, so a cancelled job keeps its
``scheduled_at`` and the assistant reported it as booked work.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import gdx_dispatch.core.mcp_tools.customers_lifetime  # noqa: F401
import gdx_dispatch.core.mcp_tools.invoices_aging  # noqa: F401
import gdx_dispatch.core.mcp_tools.revenue_summary  # noqa: F401
import gdx_dispatch.core.mcp_tools.schedule_lookup  # noqa: F401
import gdx_dispatch.core.mcp_tools.technicians_activity  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.models.tenant_models import AppSettings, Invoice, Job

COMPANY = "11111111-1111-4111-8111-111111111111"


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid.uuid4)
    capabilities: list[Any] = field(default_factory=lambda: [
        ("read", "job"), ("read", "schedule"), ("read", "invoice"),
        ("read", "customer"), ("read", "technician"),
    ])


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Job.__table__.create(engine)
    Invoice.__table__.create(engine)
    AppSettings.__table__.create(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _job(db, *, title: str, stage: str, at: datetime, tech: str = "tech-1", deleted: bool = False) -> None:
    # Core insert: column defaults apply, ORM flush listeners do not.
    db.execute(Job.__table__.insert().values(
        id=uuid.uuid4(), title=title, lifecycle_stage=stage, scheduled_at=at,
        assigned_to=tech, company_id=COMPANY, updated_at=at,
        deleted_at=at if deleted else None,
    ))


def _paid_invoice(db, *, customer_id: uuid.UUID, total: str, paid_at: datetime, on: date) -> None:
    db.execute(Invoice.__table__.insert().values(
        id=uuid.uuid4(), invoice_number=f"T-{uuid.uuid4().hex[:8]}", public_token=uuid.uuid4().hex,
        company_id=COMPANY,
        customer_id=customer_id, status="paid", total=total,
        paid_at=paid_at, invoice_date=on,
    ))


@pytest.mark.asyncio
async def test_schedule_lookup_runs_and_excludes_cancelled(db):
    day = datetime(2026, 10, 6, tzinfo=timezone.utc)
    _job(db, title="Booked", stage="scheduled", at=day + timedelta(hours=9))
    _job(db, title="Done", stage="completed", at=day + timedelta(hours=10))
    _job(db, title="Called off", stage="cancelled", at=day + timedelta(hours=11))
    _job(db, title="Deleted", stage="scheduled", at=day + timedelta(hours=12), deleted=True)
    _job(db, title="Next week", stage="scheduled", at=day + timedelta(days=8))
    db.flush()

    r = await invoke_tool(
        "schedule.lookup",
        {"start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat()},
        principal=_P(), db=db,
    )

    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    assert [(e["title"], e["lifecycle_stage"]) for e in r.result["schedule"]] == [
        ("Booked", "scheduled"), ("Done", "completed"),
    ]


@pytest.mark.asyncio
async def test_revenue_summary_runs(db):
    now = datetime.now(timezone.utc)
    _paid_invoice(db, customer_id=uuid.uuid4(), total="150.00", paid_at=now - timedelta(days=2), on=now.date())
    _paid_invoice(db, customer_id=uuid.uuid4(), total="99.00", paid_at=now - timedelta(days=60), on=now.date())
    db.flush()

    r = await invoke_tool("revenue.summary", {}, principal=_P(), db=db)

    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    assert r.result["revenue"]["total"] == 150.0
    assert r.result["revenue"]["invoice_count"] == 1


@pytest.mark.asyncio
async def test_customers_lifetime_runs_and_matches_dashed_uuid(db):
    cid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    _paid_invoice(db, customer_id=cid, total="100.00", paid_at=now, on=date(2025, 3, 1))
    _paid_invoice(db, customer_id=cid, total="250.00", paid_at=now, on=date(2026, 9, 1))
    _paid_invoice(db, customer_id=uuid.uuid4(), total="999.00", paid_at=now, on=date(2026, 9, 1))
    db.flush()

    r = await invoke_tool("customers.lifetime_analysis", {"customer_id": str(cid)}, principal=_P(), db=db)

    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    lt = r.result["lifetime"]
    assert (lt["total_paid"], lt["invoice_count"]) == (350.0, 2)
    assert (lt["first_invoice_date"], lt["last_invoice_date"]) == ("2025-03-01", "2026-09-01")


@pytest.mark.asyncio
async def test_customers_lifetime_rejects_non_uuid(db):
    r = await invoke_tool("customers.lifetime_analysis", {"customer_id": "not-a-uuid"}, principal=_P(), db=db)
    assert r.ok is False
    # The reason, not just the failure: the pre-GDXA-209 string query also
    # failed here, with ArgumentError, for every input.
    assert r.error_type == "execution_error"
    assert "not a UUID" in r.audit_row["error_detail"]


@pytest.mark.asyncio
async def test_technicians_activity_runs_with_default_window(db):
    now = datetime.now(timezone.utc)
    _job(db, title="a", stage="completed", at=now - timedelta(days=1), tech="tech-1")
    _job(db, title="b", stage="scheduled", at=now - timedelta(days=1), tech="tech-1")
    _job(db, title="c", stage="completed", at=now - timedelta(days=90), tech="tech-2")
    db.flush()

    r = await invoke_tool("technicians.activity", {}, principal=_P(), db=db)

    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    rows = {t["technician_id"]: t for t in r.result["technicians"]}
    assert set(rows) == {"tech-1"}
    assert (rows["tech-1"]["jobs_completed"], rows["tech-1"]["jobs_in_progress"]) == (1, 1)


def _open_invoice(db, *, status: str, balance: str, due: date | None, deleted: bool = False) -> None:
    db.execute(Invoice.__table__.insert().values(
        id=uuid.uuid4(), invoice_number=f"A-{uuid.uuid4().hex[:8]}", public_token=uuid.uuid4().hex,
        company_id=COMPANY, customer_id=uuid.uuid4(), status=status,
        total=balance, balance_due=balance, due_date=due,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    ))


@pytest.mark.asyncio
async def test_invoices_aging_runs_with_receivable_predicate(db):
    today = date.today()
    _open_invoice(db, status="sent", balance="100.00", due=today - timedelta(days=10))
    _open_invoice(db, status="sent", balance="200.00", due=today - timedelta(days=45))
    _open_invoice(db, status="sent", balance="400.00", due=today - timedelta(days=120))
    _open_invoice(db, status="draft", balance="1000.00", due=today - timedelta(days=45))
    _open_invoice(db, status="void", balance="2000.00", due=today - timedelta(days=45))
    _open_invoice(db, status="paid", balance="0.00", due=today - timedelta(days=45))
    _open_invoice(db, status="sent", balance="4000.00", due=today - timedelta(days=45), deleted=True)
    _open_invoice(db, status="sent", balance="8000.00", due=None)
    db.flush()

    r = await invoke_tool("invoices.aging_summary", {}, principal=_P(), db=db)

    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    got = {b["bucket"]: (b["count"], b["total_due"]) for b in r.result["summary"]}
    assert got == {"0-30": (1, 100.0), "31-60": (1, 200.0), "61-90": (0, 0.0), "90+": (1, 400.0)}
