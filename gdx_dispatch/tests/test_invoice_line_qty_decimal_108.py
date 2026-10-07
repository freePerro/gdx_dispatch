"""Migration 108: an invoice line's quantity takes two decimals
(the multi-day jobs plan, PR A: an invoice line holds hours).

The class this PR owns is *code that treats an invoice-line quantity as an
int*: it coerces it, refuses a fraction, or multiplies it in a way that breaks
once it is a ``Decimal``. The dangerous one is the last, because
``Decimal × float`` raises ``TypeError``: with only the schema changed, every
single-line add would have answered 500. Every write path here is driven with
2.5 × $33.33, whose float product (83.32499999999999) rounds the wrong way, so
a path still multiplying floats bills $83.32 instead of $83.33 and goes red.

Then the migration on both engines: rows keep their number, up and down and
up again, a no-op where the table is absent or already numeric, and a
downgrade that refuses rather than truncate a fraction.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.quantities import format_quantity
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, JobPartNeeded, Payment
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    InvoiceLinePatchIn,
    add_invoice_line,
    create_invoice,
    get_invoice,
    patch_invoice_line,
)

USER = {"user_id": "00000000-0000-0000-0000-000000000107", "tenant_id": "tenant-1", "role": "admin"}
MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/108_invoice_line_qty_decimal.py"


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for tbl in (Job, Estimate, EstimateLine, Invoice, InvoiceLine, Payment, JobPartNeeded):
        tbl.__table__.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


def _draft(db) -> UUID:
    job = Job(customer_id=uuid4(), title="Door repair", description="t", lifecycle_stage="completed",
              dispatch_status="done", billing_status="unbilled", company_id="tenant-1")
    db.add(job)
    db.commit()
    db.refresh(job)
    inv = create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id), _=USER, db=db)
    return UUID(inv["id"])


def _line(db, invoice_id: UUID) -> InvoiceLine:
    return db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id)).scalar_one()


# ── the request schema ───────────────────────────────────────────────────────

def _parse_both_ways(model, body):
    """FastAPI runs ``json.loads`` and then validates in Python mode, so a JSON
    number reaches the model as a float; JSON mode is checked too, so the two
    cannot drift apart."""
    return model.model_validate(json.loads(body)), model.model_validate_json(body)


@pytest.mark.parametrize("model", [InvoiceLineCreateIn, InvoiceLinePatchIn])
@pytest.mark.parametrize(("raw", "want"), [("2.5", "2.5"), ("0.25", "0.25"), ("3", "3")])
def test_a_two_decimal_quantity_is_accepted_from_json(model, raw, want):
    for parsed in _parse_both_ways(model, f'{{"description": "Labor", "quantity": {raw}}}'):
        assert parsed.quantity == Decimal(want)


@pytest.mark.parametrize("model", [InvoiceLineCreateIn, InvoiceLinePatchIn])
@pytest.mark.parametrize("raw", ["2.555", "0.30000000000000004", "0", "-1", "10000"])
def test_a_third_decimal_place_float_noise_zero_and_overflow_are_refused(model, raw):
    """A third decimal is a 422, never a silent round — rounding would bill an
    amount nobody typed. Float noise from a client lands here too."""
    body = f'{{"description": "Labor", "quantity": {raw}}}'
    with pytest.raises(ValidationError):
        model.model_validate(json.loads(body))
    with pytest.raises(ValidationError):
        model.model_validate_json(body)


# ── every write path, 2.5 × $33.33 ───────────────────────────────────────────

def test_add_invoice_line_bills_a_fractional_quantity_to_the_cent(db):
    inv_id = _draft(db)
    add_invoice_line(invoice_id=inv_id,
                     payload=InvoiceLineCreateIn(description="Labor", quantity=Decimal("2.5"), unit_price=33.33),
                     current_user=USER, db=db)
    line = _line(db, inv_id)
    assert line.quantity == Decimal("2.5")
    assert line.line_total == Decimal("83.33"), "ROUND_HALF_UP on the exact product, not the float"
    out = get_invoice(inv_id, _=USER, db=db)
    assert out["subtotal"] == 83.33
    assert out["lines"][0]["quantity"] == 2.5, "quantity is a JSON number, as unit_price is"


def test_patch_to_a_fractional_quantity_reprices_the_line(db):
    inv_id = _draft(db)
    add_invoice_line(invoice_id=inv_id,
                     payload=InvoiceLineCreateIn(description="Labor", quantity=1, unit_price=33.33),
                     current_user=USER, db=db)
    line = _line(db, inv_id)
    out = patch_invoice_line(inv_id, line.id, InvoiceLinePatchIn(quantity=Decimal("2.5")), USER, db)
    assert out["quantity"] == 2.5
    assert out["line_total"] == 83.33
    assert get_invoice(inv_id, _=USER, db=db)["subtotal"] == 83.33


def test_create_invoice_with_fractional_line_items(db):
    job = Job(customer_id=uuid4(), title="t", description="t", lifecycle_stage="completed",
              dispatch_status="done", billing_status="unbilled", company_id="tenant-1")
    db.add(job)
    db.commit()
    db.refresh(job)
    inv = create_invoice(payload=InvoiceCreateIn(
        job_id=job.id, customer_id=job.customer_id,
        line_items=[InvoiceLineCreateIn(description="Labor", quantity=Decimal("2.5"), unit_price=33.33),
                    InvoiceLineCreateIn(description="Spring", quantity=2, unit_price=75)],
    ), _=USER, db=db)
    lines = {ln.description: ln for ln in db.execute(select(InvoiceLine)).scalars()}
    assert lines["Labor"].line_total == Decimal("83.33")
    assert lines["Spring"].line_total == Decimal("150.00")
    assert get_invoice(UUID(inv["id"]), _=USER, db=db)["subtotal"] == 233.33


def test_create_invoice_discount_cap_reads_a_fractional_quantity(db):
    """The over-discount cap sums goods as Decimal × quantity; with an int()
    there it would have measured 2 × $100 and refused a $250 discount on $250
    of goods."""
    job = Job(customer_id=uuid4(), title="t", description="t", lifecycle_stage="completed",
              dispatch_status="done", billing_status="unbilled", company_id="tenant-1")
    db.add(job)
    db.commit()
    db.refresh(job)
    create_invoice(payload=InvoiceCreateIn(
        job_id=job.id, customer_id=job.customer_id, discount=250,
        line_items=[InvoiceLineCreateIn(description="Labor", quantity=Decimal("2.5"), unit_price=100)],
    ), _=USER, db=db)
    with pytest.raises(HTTPException) as exc:
        create_invoice(payload=InvoiceCreateIn(
            job_id=job.id, customer_id=job.customer_id, discount=251,
            line_items=[InvoiceLineCreateIn(description="Labor", quantity=Decimal("2.5"), unit_price=100)],
        ), _=USER, db=db)
    assert exc.value.status_code == 422


def _job_line_item(db, inv_id: UUID, quantity):
    from gdx_dispatch.routers.sub_resources import create_job_line_item

    request = SimpleNamespace(client=None, headers={}, url=SimpleNamespace(path="/x"), method="POST", state=SimpleNamespace())
    return create_job_line_item(str(uuid4()), request,
                                {"invoice_id": str(inv_id), "description": "Labor", "quantity": quantity, "unit_price": 33.33},
                                user=USER, db=db)


def test_the_raw_dict_line_item_endpoint_takes_two_decimals(db):
    inv_id = _draft(db)
    _job_line_item(db, inv_id, 2.5)
    line = _line(db, inv_id)
    assert line.quantity == Decimal("2.5")
    assert line.line_total == Decimal("83.33")


@pytest.mark.parametrize("bad", [2.555, 0, -1, 10000, True, "abc", None, float("inf"), float("nan"), 10 ** 400])
def test_the_raw_dict_line_item_endpoint_refuses_what_the_schema_refuses(db, bad):
    inv_id = _draft(db)
    with pytest.raises(HTTPException) as exc:
        _job_line_item(db, inv_id, bad)
    assert exc.value.status_code == 422
    assert db.execute(select(InvoiceLine)).first() is None


# ── what the customer reads ──────────────────────────────────────────────────

def test_format_quantity_prints_whole_numbers_bare_and_fractions_trimmed():
    assert format_quantity(Decimal("3.00")) == "3"
    assert format_quantity(Decimal("2.50")) == "2.5"
    assert format_quantity(Decimal("0.25")) == "0.25"
    assert format_quantity(3) == "3"
    assert format_quantity(2.5) == "2.5"
    assert format_quantity(Decimal("100")) == "100"
    assert format_quantity(None) == "1", "blank reads as 1, as recorded_quantity rules"
    assert format_quantity(0) == "0"


def test_the_pdf_and_the_email_print_the_formatted_quantity():
    from gdx_dispatch.core.email_layout import line_items_table
    from gdx_dispatch.core.pdf_generator import _JINJA_ENV

    html = line_items_table([{"description": "Labor", "quantity": Decimal("2.50"), "unit_price": 100, "line_total": 250},
                             {"description": "Doors", "quantity": Decimal("3.00"), "unit_price": 1, "line_total": 3}])
    assert ">2.5<" in html and ">3<" in html
    assert ">2.50<" not in html and ">3.00<" not in html, "a quantity cell never carries trailing zeros"

    tpl = _JINJA_ENV.from_string('{% import "_pdf_line_items.html" as li %}{{ li.line_row(line, false, false, false) }}')
    row = tpl.render(line={"description": "Labor", "quantity": Decimal("2.50"), "unit_price": 100, "line_total": 250})
    assert '<td class="num">2.5</td>' in row


# ── migration 108 ────────────────────────────────────────────────────────────

SEED = ("CREATE TABLE invoice_lines (id VARCHAR(36) PRIMARY KEY, invoice_id VARCHAR(36) NOT NULL, "
        "description TEXT NOT NULL, quantity INTEGER NOT NULL, unit_price NUMERIC(12, 2) NOT NULL, "
        "line_total NUMERIC(12, 2) NOT NULL)")
ROWS = ("INSERT INTO invoice_lines VALUES ('l1', 'i1', 'Spring', 3, 75.00, 225.00)",
        "INSERT INTO invoice_lines VALUES ('l2', 'i1', 'Doors', 256, 1.00, 256.00)")


def _load(conn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location("m108", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _qty_type(conn):
    return next(c["type"] for c in inspect(conn).get_columns("invoice_lines") if c["name"] == "quantity")


def _is_numeric(conn) -> bool:
    import sqlalchemy as sa

    return isinstance(_qty_type(conn), sa.Numeric)


def test_it_chains_onto_107_and_is_the_only_reviser():
    source = MIGRATION.read_text()
    assert 'revision = "108_invoice_line_qty_decimal"' in source
    assert 'down_revision = "107_audit_logs_guard"' in source
    assert len("108_invoice_line_qty_decimal") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [p.name for p in MIGRATION.parent.glob("*.py")
                if re.search(r'^down_revision = "107_audit_logs_guard"', p.read_text(), re.M)]
    assert revising == ["108_invoice_line_qty_decimal.py"], revising
    assert "%" not in source.replace("%%", "")


def test_the_orm_model_carries_the_type():
    col = InvoiceLine.__table__.columns["quantity"]
    assert (col.type.precision, col.type.scale) == (10, 2)
    assert col.nullable is False


def test_sqlite_upgrade_keeps_every_number_and_round_trips(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm108.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED)
        for sql in ROWS:
            c.exec_driver_sql(sql)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _is_numeric(c)
        assert [Decimal(str(q)) for (q,) in c.exec_driver_sql("SELECT quantity FROM invoice_lines ORDER BY id")] == [3, 256]
        assert [Decimal(str(t)) for (t,) in c.exec_driver_sql("SELECT line_total FROM invoice_lines ORDER BY id")] == [
            Decimal("225"), Decimal("256")]
        m.upgrade()  # rerun is a no-op
        c.exec_driver_sql("INSERT INTO invoice_lines VALUES ('l3', 'i1', 'Labor', 2.5, 100.00, 250.00)")
        assert Decimal(str(c.exec_driver_sql("SELECT quantity FROM invoice_lines WHERE id='l3'").scalar())) == Decimal("2.5")
    with eng.begin() as c, pytest.raises(RuntimeError, match="1 invoice line"):
        _load(c).downgrade()
    with eng.begin() as c:
        assert _is_numeric(c), "a refused downgrade changes nothing"
        assert c.exec_driver_sql("SELECT COUNT(*) FROM invoice_lines").scalar() == 3
        c.exec_driver_sql("DELETE FROM invoice_lines WHERE id='l3'")
        m = _load(c)
        m.downgrade()
        assert not _is_numeric(c)
        assert [q for (q,) in c.exec_driver_sql("SELECT quantity FROM invoice_lines ORDER BY id")] == [3, 256]
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _is_numeric(c)
    eng.dispose()


def test_sqlite_without_the_table_is_a_no_op(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()
        assert inspect(c).get_table_names() == []
    eng.dispose()


def test_on_a_create_all_database_upgrade_is_a_no_op(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    TenantBase.metadata.create_all(bind=eng)
    for tbl in (Job, Invoice, InvoiceLine):
        tbl.__table__.create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        assert _is_numeric(c), "create_all already builds the new type"
        _load(c).upgrade()
        assert _is_numeric(c)
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 108 needs a real Postgres; set DATABASE_URL or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m108_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            c.exec_driver_sql(SEED)
            for sql in ROWS:
                c.exec_driver_sql(sql)
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            row = c.exec_driver_sql(
                "SELECT data_type, numeric_precision, numeric_scale FROM information_schema.columns "
                "WHERE table_schema = 'm108_test' AND table_name = 'invoice_lines' AND column_name = 'quantity'"
            ).one()
            assert tuple(row) == ("numeric", 10, 2)
            assert [q for (q,) in c.exec_driver_sql("SELECT quantity FROM invoice_lines ORDER BY id")] == [
                Decimal("3.00"), Decimal("256.00")]
            m.upgrade()
            c.exec_driver_sql("INSERT INTO invoice_lines VALUES ('l3', 'i1', 'Labor', 2.5, 100.00, 250.00)")
            c.commit()
            with pytest.raises(RuntimeError, match="1 invoice line"):
                m.downgrade()
            c.rollback()
            c.exec_driver_sql(f"SET search_path TO {schema}")
            c.exec_driver_sql("DELETE FROM invoice_lines WHERE id='l3'")
            m.downgrade()
            c.commit()
            assert c.exec_driver_sql(
                "SELECT data_type FROM information_schema.columns WHERE table_schema = 'm108_test' "
                "AND table_name = 'invoice_lines' AND column_name = 'quantity'").scalar() == "integer"
            assert [q for (q,) in c.exec_driver_sql("SELECT quantity FROM invoice_lines ORDER BY id")] == [3, 256]
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
