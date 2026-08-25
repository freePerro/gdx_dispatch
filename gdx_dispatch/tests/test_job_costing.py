"""Tests for the job costing router (markup rules, price calc, cost breakdown)."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.job_costing import MarkupRule, router


def _make_client(tenant_id: str = "tenant-test") -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"
        ),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "sub": "user-1",
        "role": "admin",
        "tenant_id": tenant_id,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Markup rule CRUD
# ---------------------------------------------------------------------------


def test_create_markup_rule(client: TestClient):
    r = client.post(
        "/api/costing/markup-rules",
        json={"category": "parts", "markup_percent": 40, "minimum_margin_percent": 0},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["category"] == "parts"
    assert data["markup_percent"] == 40.0
    assert data["active"] is True
    assert data["company_id"] == "tenant-test"


def test_unique_category_per_tenant(client: TestClient):
    r1 = client.post(
        "/api/costing/markup-rules",
        json={"category": "labor", "markup_percent": 25},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/costing/markup-rules",
        json={"category": "labor", "markup_percent": 30},
    )
    assert r2.status_code == 409
    assert "labor" in r2.json()["detail"].lower()


def test_list_rules_tenant_scoped():
    a = _make_client(tenant_id="tenant-a")
    b = _make_client(tenant_id="tenant-b")
    try:
        a.post("/api/costing/markup-rules", json={"category": "parts", "markup_percent": 40})
        b.post("/api/costing/markup-rules", json={"category": "parts", "markup_percent": 55})

        list_a = a.get("/api/costing/markup-rules").json()
        list_b = b.get("/api/costing/markup-rules").json()
        assert len(list_a) == 1 and list_a[0]["markup_percent"] == 40.0
        assert len(list_b) == 1 and list_b[0]["markup_percent"] == 55.0
        assert list_a[0]["company_id"] == "tenant-a"
        assert list_b[0]["company_id"] == "tenant-b"
    finally:
        a.app.dependency_overrides.clear()
        b.app.dependency_overrides.clear()
        a._engine.dispose()  # type: ignore[attr-defined]
        b._engine.dispose()  # type: ignore[attr-defined]


def test_soft_delete_rule(client: TestClient):
    created = client.post(
        "/api/costing/markup-rules",
        json={"category": "equipment", "markup_percent": 20},
    ).json()
    r = client.delete(f"/api/costing/markup-rules/{created['id']}")
    assert r.status_code == 204

    listed = client.get("/api/costing/markup-rules").json()
    assert all(rule["id"] != created["id"] for rule in listed)

    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        row = db.execute(
            select(MarkupRule).where(MarkupRule.id == UUID(created["id"]))
        ).scalar_one()
        assert row.deleted_at is not None
        assert row.active is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Price calculator
# ---------------------------------------------------------------------------


def test_calculate_price_uses_rule(client: TestClient):
    client.post(
        "/api/costing/markup-rules",
        json={"category": "parts", "markup_percent": 40, "minimum_margin_percent": 0},
    )
    r = client.post(
        "/api/costing/calculate-price",
        json={"category": "parts", "cost": 100},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["markup_percent"] == 40.0
    assert data["suggested_price"] == 140.0
    assert data["rule_id"] is not None


def test_calculate_price_default_when_no_rule(client: TestClient):
    r = client.post(
        "/api/costing/calculate-price",
        json={"category": "unknown_category", "cost": 100},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # Default 35% markup => 135
    assert data["markup_percent"] == 35.0
    assert data["suggested_price"] == 135.0
    assert data["rule_id"] is None


def test_minimum_margin_floor(client: TestClient):
    # 20% markup is too low given 50% min margin; floor raises price to hit 50% margin.
    # cost=100, 50% min margin => price = 100 / (1 - 0.5) = 200
    client.post(
        "/api/costing/markup-rules",
        json={
            "category": "premium",
            "markup_percent": 20,
            "minimum_margin_percent": 50,
        },
    )
    r = client.post(
        "/api/costing/calculate-price",
        json={"category": "premium", "cost": 100},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["suggested_price"] == 200.0
    assert data["minimum_margin_percent"] == 50.0


# ---------------------------------------------------------------------------
# Cost breakdown for missing job
# ---------------------------------------------------------------------------


def test_get_costing_for_missing_job(client: TestClient):
    r = client.get(f"/api/costing/jobs/{uuid4()}")
    # Should return zeroed structure, NOT 500
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["labor"]["total"] == 0.0
    assert data["parts"]["total"] == 0.0
    assert data["total_cost"] == 0.0
    assert data["invoiced_amount"] == 0.0
    assert data["profit"] == 0.0
    assert data["margin_percent"] == 0.0


def test_parts_for_job_sums_via_parts_join():
    """Regression: `_parts_for_job` joins `parts` for the name and totals with
    `unit_cost_at_time`.

    Guards the prod bug where the raw query referenced job_parts columns that
    never existed (part_name/quantity/unit_cost/deleted_at) — the broad except
    swallowed the UndefinedColumn and every job's parts cost silently read $0.
    Also pins that costing uses the captured cost, not the part's current price.
    """
    from decimal import Decimal

    from gdx_dispatch.modules.inventory.models import JobPart, Part
    from gdx_dispatch.routers.job_costing import _parts_for_job

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    job_id = uuid4()
    part = Part(
        sku="SPR-1", name="Torsion Spring",
        unit_cost=Decimal("10.00"), unit_price=Decimal("25.00"),
    )
    db.add(part)
    db.flush()
    db.add(JobPart(job_id=job_id, part_id=part.id, qty_used=3, unit_cost_at_time=Decimal("12.50")))
    db.commit()

    result = _parts_for_job(db, job_id)
    # 3 × 12.50 = 37.50 — uses unit_cost_at_time, NOT part.unit_cost (10.00 → 30.00)
    assert result["total"] == 37.5
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["name"] == "Torsion Spring"
    assert item["qty"] == 3.0
    assert item["unit_cost"] == 12.5
    db.close()


# ---------------------------------------------------------------------------
# Patch / update
# ---------------------------------------------------------------------------


def test_patch_updates_markup_rule(client: TestClient):
    created = client.post(
        "/api/costing/markup-rules",
        json={"category": "parts", "markup_percent": 40},
    ).json()
    r = client.patch(
        f"/api/costing/markup-rules/{created['id']}",
        json={"markup_percent": 55, "minimum_margin_percent": 10},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["markup_percent"] == 55.0
    assert data["minimum_margin_percent"] == 10.0


def test_catalog_pricing_endpoint(client: TestClient):
    client.post(
        "/api/costing/markup-rules",
        json={"category": "parts", "markup_percent": 40},
    )
    client.post(
        "/api/costing/markup-rules",
        json={"category": "labor", "markup_percent": 25},
    )
    r = client.get("/api/costing/catalog-pricing")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    cats = {row["category"] for row in data}
    assert cats == {"parts", "labor"}


# ---------------------------------------------------------------------------
# Parts cost — attempt 3. Owner's rules, 2026-08-25:
#   only closeout/used carries cost · catalog estimates · the vendor's bill is
#   what counts · show the diff.
#
# Attempts 1 and 2 were pulled before merge. Both priced rows without first
# deciding which rows count, then tried to dedup by inference — attempt 1 on a
# foreign key confirm.py never sets to the tech's row, attempt 2 on lowercased
# part name, which core/part_pricing.py:30 forbids outright. This version does
# not infer anything: a part is billed IFF a bill line explicitly points at it,
# and estimates come from EXACT SKU only.
# ---------------------------------------------------------------------------

from gdx_dispatch.routers.job_costing import _parts_for_job  # noqa: E402


def _s(client):
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(bind=client._engine, autoflush=False, autocommit=False)()


def _used_part(db, job_id, name, qty=1, sku=None, status="used"):
    pid = str(uuid4())
    db.execute(
        text("INSERT INTO job_parts_needed (id, company_id, job_id, part_name, quantity, sku, status, source, created_at) "
             "VALUES (:id,'tenant-test',:j,:n,:q,:sku,:st,'closeout',datetime('now'))"),
        {"id": pid, "j": str(job_id), "n": name, "q": qty, "sku": sku, "st": status},
    )
    db.commit()
    return pid


def _bill(db, job_id, desc, qty=1, unit=10.0, links=None, kind="item",
          status="confirmed", deleted=False):
    """Write the bill through the ORM, exactly as confirm.py does.

    This used to raw-INSERT with `str(job_id)`. `vendor_invoice_lines.job_id` is
    a SQLAlchemy `Uuid`: SQLite stores it as 32 hex chars with NO dashes, so a
    dashed raw INSERT produced rows the application could never write and the
    reader could only find if IT also used the dashed form. Fixture and query
    agreed with each other and neither agreed with production — the tests passed
    while `actual` cost matched nothing. Going through the ORM is what makes
    them mean something.
    """
    from datetime import UTC, datetime
    from decimal import Decimal as D

    from gdx_dispatch.modules.vendor_invoices.models import VendorInvoice, VendorInvoiceLine

    inv = VendorInvoice(
        vendor_key="acme", vendor_name_raw="Acme", invoice_number=f"INV-{uuid4().hex[:8]}",
        subtotal=D("0"), tax=D("0"), shipping=D("0"), total=D("0"),
        status="confirmed", source="manual", extraction_method="manual",
        deleted_at=datetime(2026, 1, 1, tzinfo=UTC) if deleted else None,
    )
    db.add(inv)
    db.flush()
    db.add(
        VendorInvoiceLine(
            vendor_invoice_id=inv.id, line_no=0, kind=kind, description=desc,
            quantity=D(str(qty)), unit_cost=D(str(unit)), line_total=D(str(round(qty * unit, 2))),
            disposition="job", status=status, job_id=job_id, job_part_needed_id=links,
        )
    )
    db.commit()


def _cat(db, name, cost, sku, active=True):
    cid = str(uuid4())
    db.execute(
        text("INSERT INTO custom_catalogs (id,name,source_system,product_class,field_schema,"
             "pricing_strategy,pricing_config,active,created_at,updated_at) "
             "VALUES (:id,'Cat','manual','part','{}','fixed','{}',1,datetime('now'),datetime('now'))"),
        {"id": cid},
    )
    db.execute(
        text("INSERT INTO custom_catalog_items (id,catalog_id,sku,name,cost,price,product_class,"
             "attributes,active,created_at,updated_at) "
             "VALUES (:id,:c,:sku,:n,:cost,:p,'part','{}',:a,datetime('now'),datetime('now'))"),
        {"id": str(uuid4()), "c": cid, "sku": sku, "n": name, "cost": cost,
         "p": round(cost * 1.5, 2), "a": 1 if active else 0},
    )
    db.commit()


def test_a_request_costs_nothing(client):
    """68 of 73 prod rows are requests. A wish is not a spend."""
    db = _s(client)
    job = uuid4()
    _cat(db, "Torsion spring", 41.50, "TS-207")
    _used_part(db, job, "Torsion spring", 2, sku="TS-207", status="needed")
    out = _parts_for_job(db, job)
    assert out["total"] == 0.0 and out["items"] == [] and out["unknown_cost_count"] == 0
    db.close()


def test_a_used_part_is_estimated_by_exact_sku(client):
    db = _s(client)
    job = uuid4()
    _cat(db, "Torsion spring", 41.50, "TS-207")
    _used_part(db, job, "Torsion spring", 2, sku="TS-207")
    out = _parts_for_job(db, job)
    assert out["total"] == 83.00
    assert out["estimated_cost_total"] == 83.00 and out["actual_cost_total"] == 0.0
    assert out["items"][0]["is_estimate"] is True
    db.close()


def test_name_alone_never_prices_a_part(client):
    """AUDIT-R1: exact SKU only. A catalog row with the same NAME but a
    different SKU must not price this part — that is the ruling attempt 2
    broke."""
    db = _s(client)
    job = uuid4()
    _cat(db, "Torsion spring", 41.50, "SOMETHING-ELSE")
    _used_part(db, job, "Torsion spring", 2, sku="TS-207")
    out = _parts_for_job(db, job)
    assert out["unknown_cost_count"] == 1
    assert out["total"] == 0.0
    db.close()


def test_a_used_part_with_no_sku_is_unknown_not_free(client):
    db = _s(client)
    job = uuid4()
    _used_part(db, job, "Bespoke bracket", 1, sku=None)
    out = _parts_for_job(db, job)
    assert out["unknown_cost_count"] == 1 and out["total"] == 0.0
    assert out["items"][0]["cost_known"] is False
    db.close()


def test_a_linked_bill_replaces_the_estimate_and_is_not_double_counted(client):
    """THE BUG THAT PULLED BOTH EARLIER ATTEMPTS."""
    db = _s(client)
    job = uuid4()
    _cat(db, "Torsion spring", 41.50, "TS-207")
    pid = _used_part(db, job, "Torsion spring", 2, sku="TS-207")
    _bill(db, job, "SPRING TORS .250X2.0X32", qty=2, unit=48.75, links=pid)

    out = _parts_for_job(db, job)
    assert out["total"] == 97.50, "the bill, NOT 97.50 + 83.00"
    assert out["actual_cost_total"] == 97.50
    assert out["estimated_cost_total"] == 0.0
    assert len(out["items"]) == 1
    db.close()


def test_an_unlinked_bill_does_not_stack_an_estimate_on_top_of_itself(client):
    """THE DEFAULT PATH, and the one that matters — the picker is optional and
    every historical line has no link.

    A bill line on the job that is not linked to a part may BE one of the parts
    we just estimated. Summing both roughly doubles one physical part. An
    earlier revision did exactly that and shipped a test asserting it correct,
    replacing a known $0 with an unknown 2x — the worse error, because it looks
    like a number.

    So `total` carries only what is evidenced, the estimate is reported beside
    it marked ambiguous, and the job reads incomplete."""
    db = _s(client)
    job = uuid4()
    _cat(db, "Torsion spring", 41.50, "TS-207")
    _used_part(db, job, "Torsion spring", 2, sku="TS-207")
    _bill(db, job, "SPRING TORS .250X2.0X32", qty=2, unit=48.75, links=None)

    out = _parts_for_job(db, job)

    assert out["total"] == 97.50, "the bill only — NOT 97.50 + 83.00"
    assert out["actual_cost_total"] == 97.50
    assert out["estimated_cost_total"] == 83.00, "still reported, just not summed"
    assert out["estimates_ambiguous"] is True
    assert out["unlinked_bill_lines"] == 1
    assert all(i.get("ambiguous") for i in out["items"] if i.get("is_estimate"))
    assert out["catalog_variance"] == 0.0, "no link means no comparable pair"
    db.close()


def test_estimates_are_summed_normally_when_nothing_is_unattributed(client):
    """Counterfactual: with no unlinked bill there is nothing to collide with,
    so the estimate is real cost and DOES count."""
    db = _s(client)
    job = uuid4()
    _cat(db, "Torsion spring", 41.50, "TS-207")
    _used_part(db, job, "Torsion spring", 2, sku="TS-207")

    out = _parts_for_job(db, job)
    assert out["total"] == 83.00
    assert out["estimates_ambiguous"] is False
    assert not any(i.get("ambiguous") for i in out["items"])
    db.close()


def test_variance_is_actual_minus_catalog_on_the_billed_quantity(client):
    db = _s(client)
    job = uuid4()
    _cat(db, "Torsion spring", 41.50, "TS-207")
    pid = _used_part(db, job, "Torsion spring", 2, sku="TS-207")
    _bill(db, job, "spring", qty=2, unit=48.75, links=pid)
    assert _parts_for_job(db, job)["catalog_variance"] == 14.50
    db.close()


def test_variance_is_negative_when_the_catalog_overstates(client):
    db = _s(client)
    job = uuid4()
    _cat(db, "Roller set", 20.00, "RS-10")
    pid = _used_part(db, job, "Roller set", 1, sku="RS-10")
    _bill(db, job, "rollers", qty=1, unit=12.00, links=pid)
    assert _parts_for_job(db, job)["catalog_variance"] == -8.00
    db.close()


def test_a_part_billed_across_two_lines_accumulates(client):
    """Attempt 2 overwrote per-part state, so a split bill produced a nonsense
    variance under a UI telling the owner to reprice."""
    db = _s(client)
    job = uuid4()
    _cat(db, "Torsion spring", 41.50, "TS-207")
    pid = _used_part(db, job, "Torsion spring", 2, sku="TS-207")
    _bill(db, job, "spring 1 of 2", qty=1, unit=48.75, links=pid)
    _bill(db, job, "spring 2 of 2", qty=1, unit=48.75, links=pid)

    out = _parts_for_job(db, job)
    assert out["actual_cost_total"] == 97.50
    assert out["catalog_variance"] == 14.50, "2 billed @48.75 vs 2 catalog @41.50"
    db.close()


def test_freight_and_tax_are_not_parts_cost(client):
    db = _s(client)
    job = uuid4()
    _bill(db, job, "Freight", qty=1, unit=95.00, kind="freight")
    _bill(db, job, "Sales tax", qty=1, unit=31.20, kind="tax")
    out = _parts_for_job(db, job)
    assert out["total"] == 0.0 and out["items"] == []
    db.close()


def test_an_unconfirmed_line_does_not_charge_the_job(client):
    db = _s(client)
    job = uuid4()
    _bill(db, job, "spring", qty=2, unit=48.75, status="pending")
    assert _parts_for_job(db, job)["total"] == 0.0
    db.close()


def test_a_soft_deleted_bill_stops_charging(client):
    """Invariant #2 applies to reads."""
    db = _s(client)
    job = uuid4()
    _bill(db, job, "spring", qty=2, unit=48.75, deleted=True)
    assert _parts_for_job(db, job)["total"] == 0.0
    db.close()


def test_an_inactive_catalog_row_does_not_price(client):
    db = _s(client)
    job = uuid4()
    _cat(db, "Retired spring", 41.50, "RS-OLD", active=False)
    _used_part(db, job, "Retired spring", 1, sku="RS-OLD")
    assert _parts_for_job(db, job)["unknown_cost_count"] == 1
    db.close()


def test_a_zero_cost_catalog_row_is_unpriced_not_free(client):
    db = _s(client)
    job = uuid4()
    _cat(db, "Unpriced widget", 0.00, "UW-1")
    _used_part(db, job, "Unpriced widget", 2, sku="UW-1")
    assert _parts_for_job(db, job)["unknown_cost_count"] == 1
    db.close()


def test_the_endpoint_reports_the_split(client):
    db = _s(client)
    job = uuid4()
    _cat(db, "Torsion spring", 41.50, "TS-207")
    _used_part(db, job, "Torsion spring", 2, sku="TS-207")
    db.close()
    b = client.get(f"/api/costing/jobs/{job}").json()
    assert b["estimated_parts_cost"] == 83.00
    assert b["actual_parts_cost"] == 0.0
    # A cost made ENTIRELY of catalog guesses is not settled. An earlier
    # revision reported False here, so "complete" meant "we guessed everything
    # successfully" — the opposite of what a reader needs.
    assert b["cost_incomplete"] is True


def test_profitability_carries_the_caveats(client):
    db = _s(client)
    job = uuid4()
    _used_part(db, job, "Bespoke bracket", 1, sku=None)
    db.close()
    for row in client.get("/api/costing/profitability?days=3650").json():
        assert "cost_incomplete" in row and "catalog_variance" in row
        assert "estimated_parts_cost" in row and "actual_parts_cost" in row


def test_a_partial_bill_leaves_the_remainder_costed_not_swallowed(client):
    """Used 4, billed 1. The other 3 units were genuinely consumed; treating the
    part as 'done' because one line mentions it undercounts the job — same
    direction of error (margin overstated) as the silent $0 this replaced."""
    db = _s(client)
    job = uuid4()
    _cat(db, "Torsion spring", 41.50, "TS-207")
    pid = _used_part(db, job, "Torsion spring", 4, sku="TS-207")
    _bill(db, job, "one spring", qty=1, unit=48.75, links=pid)

    out = _parts_for_job(db, job)

    assert out["actual_cost_total"] == 48.75, "the one unit that was billed"
    assert out["estimated_cost_total"] == 124.50, "3 remaining x 41.50 catalog"
    assert out["total"] == 173.25
    assert any("unbilled remainder" in i["name"] for i in out["items"])
    db.close()


def test_a_partial_bill_with_no_catalog_price_is_flagged_unknown(client):
    """Counterfactual: the remainder must not silently vanish when it cannot be
    priced either."""
    db = _s(client)
    job = uuid4()
    pid = _used_part(db, job, "Bespoke bracket", 4, sku="NO-SUCH-SKU")
    _bill(db, job, "one bracket", qty=1, unit=48.75, links=pid)

    out = _parts_for_job(db, job)
    assert out["actual_cost_total"] == 48.75
    assert out["unknown_cost_count"] == 1, "the 3 uncosted units are surfaced"
    db.close()


def test_a_sku_with_stray_whitespace_still_matches(client):
    """core/part_pricing.py resolves on lower(TRIM(sku)); costing must agree or
    a part prices at sell and reads $0 at cost — an inflated margin."""
    db = _s(client)
    job = uuid4()
    _cat(db, "Torsion spring", 41.50, "TS-207")
    _used_part(db, job, "Torsion spring", 2, sku="  ts-207 ")

    assert _parts_for_job(db, job)["estimated_cost_total"] == 83.00
    db.close()
