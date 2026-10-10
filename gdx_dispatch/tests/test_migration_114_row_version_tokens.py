"""Migration 114 and the ``version`` token on customers, invoices, estimates.

The migration, on both engines: existing rows read 1, up/down/up, rerunnable,
a no-op on a ``create_all`` database. The model: a row starts at 1 and every
UPDATE the ORM compiles bumps it by one — a flush, a Core ``update()`` and a
``Query.update()`` alike — so a PATCH can compare the token its dialog loaded.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import column, create_engine, insert, inspect, select, table, text, update
from sqlalchemy.orm import Session

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/114_row_version_tokens.py"
TABLES = ("customers", "invoices", "estimates")


def _load(conn):
    spec = importlib.util.spec_from_file_location("m114", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _has_version(conn) -> dict[str, bool]:
    insp = inspect(conn)
    return {t: "version" in {c["name"] for c in insp.get_columns(t)} for t in TABLES}


def _t(name: str):
    return table(name, column("id"), column("name"), column("version"))


def _version(conn, t: str, row_id: str = "a") -> int:
    tbl = _t(t)
    return conn.execute(select(tbl.c.version).where(tbl.c.id == row_id)).scalar_one()


def _insert(conn, t: str, row_id: str, name: str) -> None:
    conn.execute(insert(_t(t)).values(id=row_id, name=name))


def _legacy_schema(conn) -> None:
    """The three tables as an existing database has them: no ``version``."""
    for t in TABLES:
        conn.exec_driver_sql(f"CREATE TABLE {t} (id VARCHAR(36) PRIMARY KEY, name TEXT)")
        _insert(conn, t, "a", "old row")


def test_it_chains_onto_113_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "114_row_version_tokens"' in source
    assert 'down_revision = "113_reseller_quotes"' in source
    assert len("114_row_version_tokens") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "113_reseller_quotes"', p.read_text(), re.M)
    ]
    assert revising == ["114_row_version_tokens.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_sqlite_existing_rows_read_1_and_round_trip(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm114.db'}", future=True)
    with eng.begin() as c:
        _legacy_schema(c)
        m = _load(c)
        m.upgrade()
        assert _has_version(c) == dict.fromkeys(TABLES, True)
        for t in TABLES:
            assert _version(c, t) == 1
            col = next(x for x in inspect(c).get_columns(t) if x["name"] == "version")
            assert col["nullable"] is False
            # A raw insert that names no version still gets one.
            _insert(c, t, "b", "new row")
            assert _version(c, t, "b") == 1
        m.upgrade()  # rerun is a no-op
        m.downgrade()
        assert _has_version(c) == dict.fromkeys(TABLES, False)
        assert c.exec_driver_sql("SELECT count(*) FROM customers").scalar_one() == 2
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _has_version(c) == dict.fromkeys(TABLES, True)
    eng.dispose()


def test_on_a_create_all_database_upgrade_is_a_no_op(tmp_path) -> None:
    from gdx_dispatch.models.tenant_models import Base
    from gdx_dispatch.modules.proposals import models  # noqa: F401

    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    Base.metadata.create_all(bind=eng)
    with eng.begin() as c:
        assert _has_version(c) == dict.fromkeys(TABLES, True), "create_all already builds the column"
        _load(c).upgrade()
    eng.dispose()


# ── the model: every UPDATE the ORM compiles bumps the token ─────────────────


@pytest.fixture()
def orm_session(tmp_path):
    from gdx_dispatch.models.tenant_models import Base
    from gdx_dispatch.modules.proposals import models  # noqa: F401

    eng = create_engine(f"sqlite:///{tmp_path / 'bump.db'}", future=True)
    Base.metadata.create_all(bind=eng)
    with Session(eng) as s:
        yield s
    eng.dispose()


def _rows(s: Session):
    from gdx_dispatch.models.tenant_models import Customer, Invoice
    from gdx_dispatch.modules.proposals.models import Estimate

    cust = Customer(name="Ada", company_id="t1")
    s.add(cust)
    s.flush()
    inv = Invoice(
        invoice_number="INV-1", public_token=uuid.uuid4().hex,
        customer_id=cust.id, company_id="t1",
    )
    est = Estimate(estimate_number="EST-1", public_token=uuid.uuid4().hex, company_id="t1")
    s.add_all([inv, est])
    s.commit()
    return cust, inv, est


def test_a_new_row_starts_at_1(orm_session) -> None:
    for row in _rows(orm_session):
        assert row.version == 1


def test_a_flush_bumps_it(orm_session) -> None:
    cust, inv, est = _rows(orm_session)
    cust.notes = "edited"
    inv.notes = "edited"
    est.notes = "edited"
    orm_session.commit()
    assert (cust.version, inv.version, est.version) == (2, 2, 2)
    cust.notes = "edited again"
    orm_session.commit()
    assert cust.version == 3


def test_a_flush_that_changes_nothing_does_not_bump_it(orm_session) -> None:
    cust, _, _ = _rows(orm_session)
    cust.notes = cust.notes  # no net change: the ORM emits no UPDATE
    orm_session.commit()
    assert cust.version == 1


def test_a_core_bulk_update_bumps_it(orm_session) -> None:
    from gdx_dispatch.models.tenant_models import Customer, Invoice
    from gdx_dispatch.modules.proposals.models import Estimate

    rows = _rows(orm_session)
    for model, row in zip((Customer, Invoice, Estimate), rows, strict=True):
        orm_session.execute(
            update(model).where(model.id == row.id).values(notes="bulk")
            .execution_options(synchronize_session=False)
        )
    orm_session.commit()
    for row in rows:
        orm_session.refresh(row)
        assert row.version == 2, type(row).__name__


def test_a_query_update_bumps_it(orm_session) -> None:
    from gdx_dispatch.models.tenant_models import Customer

    cust, _, _ = _rows(orm_session)
    orm_session.query(Customer).filter(Customer.id == cust.id).update(
        {"notes": "bulk"}, synchronize_session=False
    )
    orm_session.commit()
    orm_session.refresh(cust)
    assert cust.version == 2


def test_a_raw_text_update_does_not_bump_it(orm_session) -> None:
    """The documented limit: raw SQL must SET version = version + 1 itself."""
    from gdx_dispatch.models.tenant_models import Customer

    cust, _, _ = _rows(orm_session)
    orm_session.execute(
        update(Customer).where(Customer.id == cust.id).values(notes="x")
    )
    orm_session.commit()
    orm_session.refresh(cust)
    assert cust.version == 2
    orm_session.execute(text("UPDATE customers SET notes = 'raw'"))
    orm_session.commit()
    orm_session.refresh(cust)
    assert cust.version == 2


# ── Postgres ─────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 114 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


@_requires_pg
def test_postgres_existing_rows_read_1_and_round_trip() -> None:
    eng = create_engine(_URL, future=True)
    schema = "m114_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            _legacy_schema(c)
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            assert _has_version(c) == dict.fromkeys(TABLES, True)
            for t in TABLES:
                assert _version(c, t) == 1
                info = c.execute(
                    text(
                        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                        "WHERE table_schema = :s AND table_name = :t AND column_name = 'version'"
                    ),
                    {"s": schema, "t": t},
                ).one()
                assert info == ("integer", "NO", "1"), (t, info)
            m.upgrade()  # rerun is a no-op
            m.downgrade()
            c.commit()
            assert _has_version(c) == dict.fromkeys(TABLES, False)
            m.upgrade()
            c.commit()
            assert _has_version(c) == dict.fromkeys(TABLES, True)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()


def test_postgres_bulk_update_joined_to_another_table_bumps_it(pg_test_db) -> None:
    """On the Postgres fixture: the table-qualified ``customers.version + 1``
    compiles and bumps inside an UPDATE ... FROM, where an unqualified
    ``version`` could be ambiguous. Rows go in as raw SQL because the
    fixture's ``customers`` predates several ORM columns, so an ORM load of a
    Customer there fails for reasons unrelated to this token."""
    from gdx_dispatch.models.tenant_models import Customer, Invoice

    eng = create_engine(pg_test_db, future=True)
    cid, iid = uuid.uuid4(), uuid.uuid4()
    try:
        with eng.begin() as c:
            c.execute(
                text("INSERT INTO customers (id, name, company_id) VALUES (:id, 'Ada', 't1')"),
                {"id": cid},
            )
            c.execute(
                text(
                    "INSERT INTO invoices (id, invoice_number, billing_type, sequence_number, "
                    "subtotal, tax_amount, total, balance_due, status, locked, public_token, "
                    "customer_id, company_id, created_at) "
                    "VALUES (:id, 'INV-PG', 'standard', 1, 0, 0, 0, 0, 'draft', false, :tok, "
                    ":cid, 't1', now())"
                ),
                {"id": iid, "tok": uuid.uuid4().hex, "cid": cid},
            )
            assert c.execute(text("SELECT version FROM customers")).scalar_one() == 1
            c.execute(
                update(Customer)
                .where(Customer.id == Invoice.customer_id, Invoice.id == iid)
                .values(notes="joined")
            )
            c.execute(update(Invoice).where(Invoice.id == iid).values(notes="direct"))
            assert c.execute(text("SELECT version FROM customers")).scalar_one() == 2
            assert c.execute(text("SELECT version FROM invoices")).scalar_one() == 2
    finally:
        eng.dispose()
