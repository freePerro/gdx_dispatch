"""Migration 117: open contractor/wholesale estimates with a NULL tax_rate get 0
(GDXA-756).

On both engines, against an ORM-built schema seeded through the ORM: exactly
the open trade rows change, closed ones are left and named, the audit event
chains and lists the ids, the totals engine then charges no tax on the row
(a real ``compute_estimate_totals`` call with a 7.38% default configured),
a rerun is a no-op, and the downgrade restores NULL only where the row still
holds the 0 it wrote.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import uuid
from decimal import Decimal

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/117_trade_estimate_tax_rate_zero.py"
REV = "117_trade_estimate_tax_rate_zero"
TENANT = "t-117"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m117", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if conn is not None:
        mod.op = Operations(MigrationContext.configure(conn))
    return mod


def test_it_chains_onto_116_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert f'revision = "{REV}"' in source
    assert 'down_revision = "116_retired_module_grants"' in source
    assert len(REV) <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "116_retired_module_grants"', p.read_text(), re.M)
    ]
    assert revising == [MIGRATION.name], revising


def test_no_unescaped_percent_signs_in_its_sql() -> None:
    """The SQL, not the file: the docstring quotes a rate and the log lines use
    logging's own placeholders, neither of which reaches a DB-API driver."""
    m = _load(None)
    for stmt in (m._CANDIDATES, m._ZERO, m._EVENTS, m._RESTORE):
        assert "%" not in stmt.text.replace("%%", ""), stmt.text


def _seed(s: Session) -> dict[str, uuid.UUID]:
    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.modules.proposals.models import Estimate
    from gdx_dispatch.modules.tax.models import TaxConfig

    s.add(TaxConfig(name="Default", default_rate=Decimal("0.0738")))
    cust = {}
    for cls in ("wholesale", "contractor", "retail", None):
        c = Customer(name=f"c-{cls}", company_id=TENANT, pricing_class=cls)
        s.add(c)
        cust[cls] = c
    s.flush()

    ids: dict[str, uuid.UUID] = {}

    def est(key, customer, status, rate=None, deleted=False):
        e = Estimate(
            customer_id=customer.id if customer is not None else None,
            estimate_number=f"E-{key}", public_token=f"tok-{key}", company_id=TENANT,
            status=status, tax_rate=rate, total=Decimal("1000.00"),
        )
        if deleted:
            from datetime import UTC, datetime
            e.deleted_at = datetime.now(UTC)
        s.add(e)
        s.flush()
        ids[key] = e.id

    est("wholesale_sent", cust["wholesale"], "sent")          # the ledger's row
    est("contractor_draft", cust["contractor"], "draft")
    est("wholesale_draft", cust["wholesale"], "draft")
    est("wholesale_accepted", cust["wholesale"], "accepted")  # left, named
    est("wholesale_explicit", cust["wholesale"], "sent", Decimal("0.0500"))
    est("retail_sent", cust["retail"], "sent")
    est("unclassified_sent", cust[None], "sent")
    est("wholesale_deleted", cust["wholesale"], "sent", deleted=True)
    est("no_customer", None, "draft")
    s.commit()
    return ids


def _rates(s: Session, ids) -> dict[str, Decimal | None]:
    from gdx_dispatch.modules.proposals.models import Estimate

    s.expire_all()
    return {k: s.get(Estimate, v).tax_rate for k, v in ids.items()}


def _events(s: Session):
    from gdx_dispatch.core.audit import AuditLog

    return s.execute(
        select(AuditLog).where(AuditLog.action == "estimate_tax_rate_backfill").order_by(AuditLog.created_at)
    ).scalars().all()


def _exercise(engine) -> None:
    from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync, verify_audit_chain
    from gdx_dispatch.modules.proposals.models import Estimate
    from gdx_dispatch.modules.proposals.totals import compute_estimate_totals

    with Session(engine) as s:
        # A pre-existing chain link, so the event must chain onto it.
        log_audit_event_sync(s, tenant_id=TENANT, user_id="u", action="seed", entity_type="x", details={})
        s.commit()
        ids = _seed(s)
        before = _rates(s, ids)
        assert before["wholesale_sent"] is None
        # The defect, measured: NULL resolves to the 7.38% default.
        assert compute_estimate_totals(s.get(Estimate, ids["wholesale_sent"]), s)["tax"] == pytest.approx(73.8)
        v_before = s.get(Estimate, ids["wholesale_sent"]).version

    with engine.begin() as c:
        _load(c).upgrade()

    with Session(engine) as s:
        after = _rates(s, ids)
        assert after["wholesale_sent"] == 0
        assert after["contractor_draft"] == 0
        assert after["wholesale_draft"] == 0
        for untouched in ("wholesale_accepted", "retail_sent", "unclassified_sent", "wholesale_deleted", "no_customer"):
            assert after[untouched] is None, untouched
        assert after["wholesale_explicit"] == Decimal("0.05")
        assert s.get(Estimate, ids["wholesale_sent"]).version == v_before + 1
        assert s.get(Estimate, ids["wholesale_sent"]).total == Decimal("1000.00")

        totals = compute_estimate_totals(s.get(Estimate, ids["wholesale_sent"]), s)
        assert (totals["tax"], totals["total"]) == (0.0, 1000.0)

        (event,) = _events(s)
        assert event.user_id == "migration:117_trade_estimate_tax_rate_zero"
        assert event.tenant_id == TENANT
        assert sorted(event.details["estimate_ids"]) == sorted(
            str(ids[k]) for k in ("wholesale_sent", "contractor_draft", "wholesale_draft")
        )
        assert event.details["left_as_is"] == {str(ids["wholesale_accepted"]): "accepted"}
        assert verify_audit_chain(s) is True

    # Rerun: nothing NULL is left in scope, so no second event.
    with engine.begin() as c:
        _load(c).upgrade()
    with Session(engine) as s:
        assert len(_events(s)) == 1

        # Someone sets a rate on one backfilled row after the release.
        e = s.get(Estimate, ids["contractor_draft"])
        e.tax_rate = Decimal("0.0100")
        # And the customer accepts another at the 0 it now shows.
        s.get(Estimate, ids["wholesale_sent"]).status = "accepted"
        s.commit()
        count = s.scalar(select(func.count()).select_from(AuditLog))

    with engine.begin() as c:
        _load(c).downgrade()

    with Session(engine) as s:
        back = _rates(s, ids)
        assert back["wholesale_draft"] is None
        assert back["wholesale_sent"] == 0, "accepted since: an invoice may be at 0, so the downgrade leaves it"
        assert back["contractor_draft"] == Decimal("0.01"), "a rate set since is not the migration's to undo"
        assert back["wholesale_explicit"] == Decimal("0.05")
        assert s.scalar(select(func.count()).select_from(AuditLog)) == count + 1
        reverted = s.execute(
            select(AuditLog).where(AuditLog.action == "estimate_tax_rate_backfill_reverted")
        ).scalar_one()
        assert reverted.details["restored"] == 1
        assert verify_audit_chain(s) is True


def test_sqlite(tmp_path) -> None:
    from gdx_dispatch.core.audit import TenantBase

    engine = create_engine(f"sqlite:///{tmp_path / 'm117.db'}", future=True)
    TenantBase.metadata.create_all(bind=engine)
    _exercise(engine)


def test_postgres(pg_test_db) -> None:
    """In a schema of its own built from the ORM: the template's structure.sql
    lags the ORM (no ``customers.local_edit_at`` as of 2026-10-12)."""
    from gdx_dispatch.core.audit import TenantBase

    schema = "m117_test"
    engine = create_engine(pg_test_db, future=True, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        TenantBase.metadata.create_all(engine)
        _exercise(engine)
    finally:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        engine.dispose()


def test_on_a_database_without_the_tables_both_directions_are_no_ops(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    with engine.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()
