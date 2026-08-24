"""M21 reporting-bundle guards (money audit 2026-08-04).

Three of the six M21 items are code-fixed here: estimate tax rounding now
matches the invoice convention (Decimal half-up — the customer accepts the
number they're billed), a schema-mismatch export 500s instead of producing
a short CSV indistinguishable from "no data", and soft-deleted invoices no
longer export as live.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase

TENANT = "tenant-m21"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


# ── estimate tax rounds like the invoice ───────────────────────────────────

def test_estimate_tax_rounds_half_up_like_invoices(db):
    """Taxable $36.25 at 10%: float round() gave $3.62 (banker's), the
    invoice's Decimal ROUND_HALF_UP gives $3.63. One number, both documents."""
    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
    from gdx_dispatch.modules.proposals.totals import compute_estimate_totals

    est = Estimate(id=uuid.uuid4(), estimate_number=f"EST-{uuid.uuid4().hex[:6]}",
                   status="draft", company_id=TENANT, public_token=uuid.uuid4().hex,
                   tax_rate=Decimal("0.10"), total=Decimal("36.25"))
    db.add(est)
    db.add(EstimateLine(id=uuid.uuid4(), estimate_id=est.id, description="Parts",
                        quantity=1, unit_price=Decimal("36.25"),
                        line_total=Decimal("36.25"), company_id=TENANT))
    db.commit()
    totals = compute_estimate_totals(est, db)
    assert totals["tax"] == 3.63, f"got {totals['tax']} — estimate disagrees with the invoice"


# ── broken exports look broken ─────────────────────────────────────────────

class _RaisingDb:
    def __init__(self, exc):
        self._exc = exc

    def execute(self, *a, **k):
        raise self._exc


def test_schema_mismatch_export_500s_not_short_csv():
    from gdx_dispatch.routers.exports import _safe_query

    with pytest.raises(HTTPException) as exc:
        _safe_query(_RaisingDb(ProgrammingError("q", {}, Exception("boom"))),
                    "SELECT 1", {}, "invoices")
    assert exc.value.status_code == 500
    assert "not an empty dataset" in str(exc.value.detail)


def test_missing_optional_table_still_degrades_to_empty_both_dialects():
    """SQLite says 'no such table'; Postgres raises ProgrammingError
    'relation ... does not exist'. BOTH are the optional-table contract."""
    from gdx_dispatch.routers.exports import _safe_query

    out = _safe_query(_RaisingDb(OperationalError("q", {}, Exception("no such table: vendor_bills"))),
                      "SELECT 1", {}, "vendor_bills")
    assert out == []
    out = _safe_query(_RaisingDb(ProgrammingError("q", {}, Exception('relation "vendor_bills" does not exist'))),
                      "SELECT 1", {}, "vendor_bills")
    assert out == []


def test_dropped_connection_no_longer_ships_a_short_csv():
    """psycopg2 OperationalError also covers dropped connections/timeouts —
    the likeliest REAL failure must 500, not masquerade as no data."""
    from gdx_dispatch.routers.exports import _safe_query

    with pytest.raises(HTTPException):
        _safe_query(_RaisingDb(OperationalError("q", {}, Exception("server closed the connection unexpectedly"))),
                    "SELECT 1", {}, "invoices")


# ── soft-deleted invoices stay out of exports ──────────────────────────────

def test_deleted_invoices_do_not_export_as_live(db):
    from datetime import UTC, datetime

    from gdx_dispatch.models.tenant_models import Invoice
    from gdx_dispatch.routers.exports import _fetch_invoices

    def _inv(deleted):
        return Invoice(
            id=uuid.uuid4(), invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
            billing_type="standard", sequence_number=1, subtotal=Decimal("100"),
            tax_amount=Decimal("0"), total=Decimal("100"), balance_due=Decimal("100"),
            status="sent", public_token=uuid.uuid4().hex, company_id=TENANT,
            customer_id=uuid.uuid4(),
            deleted_at=datetime.now(UTC) if deleted else None,
        )

    live, dead = _inv(False), _inv(True)
    db.add(live)
    db.add(dead)
    db.commit()
    header, rows = _fetch_invoices(db, tenant_id=TENANT)
    numbers = {r[1] for r in rows}
    assert live.invoice_number in numbers
    assert dead.invoice_number not in numbers, "a soft-deleted invoice exported as live"



def test_the_deleted_at_class_is_swept_not_one_instance():
    """Audit round 2: invoices was fixed while jobs/estimates/leads/techs
    kept exporting soft-deleted rows. Structural pin over every fetch whose
    SQL targets a soft-deleting table — biting: remove any one filter and
    this names it."""
    import inspect

    from gdx_dispatch.routers import exports as ex

    missing = []
    for name, fn in inspect.getmembers(ex, inspect.isfunction):
        if not name.startswith("_fetch_"):
            continue
        src = inspect.getsource(fn)
        if "company_id = :tenant_id" not in src:
            continue
        if "deleted_at" in src and "deleted_at IS NULL" not in src:
            # exports the column but never filters it — customers does this
            # deliberately (the CSV SHOWS deleted_at); only flag when the
            # column is absent from the SELECT yet unfiltered
            continue
        if "deleted_at" not in src:
            if name in ("_fetch_payments",):
                # payments has no deleted_at column — voided_at is its
                # lifecycle, and voided rows ARE part of a payments register.
                continue
            missing.append(name)
    assert missing == [], f"fetches with neither a deleted_at filter nor the column visible: {missing}"
