"""Migration 121: open service-lane trade estimates at tax_rate 0 with no
exemption record go back to NULL (GDXA-907).

On both engines, against an ORM-built schema seeded through the ORM, with 117
actually run first so its audit event is real: exactly the draft rows and the
sent row 117 zeroed change; a sent explicit 0, closed rows, a customer with an
exemption record and job-less / install-lane / New Construction estimates are
left and named; the totals engine then charges the 7.38% default (a real
``compute_estimate_totals`` call); the event chains; a rerun is a no-op; the
downgrade restores 0 only where the row is still NULL and open.
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

VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions"
MIGRATION = VERSIONS / "121_trade_estimate_tax_rate_null.py"
M117 = VERSIONS / "117_trade_estimate_tax_rate_zero.py"
REV = "121_trade_estimate_tax_rate_null"
TENANT = "t-121"


def _load(conn, path=MIGRATION):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if conn is not None:
        mod.op = Operations(MigrationContext.configure(conn))
    return mod


def test_it_chains_onto_120_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert f'revision = "{REV}"' in source
    assert 'down_revision = "120_completed_jobs_dispatch_done"' in source
    assert len(REV) <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in VERSIONS.glob("*.py")
        if re.search(r'^down_revision = "120_completed_jobs_dispatch_done"', p.read_text(), re.M)
    ]
    assert revising == [MIGRATION.name], revising


def test_no_unescaped_percent_signs_in_its_sql() -> None:
    m = _load(None)
    for stmt in (m._CANDIDATES, m._NULL, m._EVENTS, m._RESTORE):
        assert "%" not in stmt.text.replace("%%", ""), stmt.text


def _seed(s: Session) -> dict[str, uuid.UUID]:
    from gdx_dispatch.models.tenant_models import Customer, Job
    from gdx_dispatch.modules.proposals.models import Estimate
    from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption

    s.add(TaxConfig(name="Default", default_rate=Decimal("0.0738")))
    cust = {}
    for key, cls in (("wholesale", "wholesale"), ("contractor", "contractor"),
                     ("exempt", "contractor"), ("lapsed", "wholesale"), ("retail", "retail")):
        c = Customer(name=f"c-{key}", company_id=TENANT, pricing_class=cls)
        s.add(c)
        cust[key] = c
    s.flush()
    s.add(TaxExemption(customer_id=cust["exempt"].id, exempt=True, reason="resale cert"))
    # Any exemption row is the maintainer's to judge, even one switched off.
    s.add(TaxExemption(customer_id=cust["lapsed"].id, exempt=False, reason="cert withdrawn"))

    jobs = {}
    for jt in ("Service Call", "Installation", "New Construction"):
        j = Job(title=jt, job_type=jt, customer_id=cust["wholesale"].id, company_id=TENANT)
        s.add(j)
        jobs[jt] = j
    s.flush()

    ids: dict[str, uuid.UUID] = {}

    def est(key, customer, status, rate, job="Service Call", deleted=False):
        e = Estimate(
            customer_id=cust[customer].id, job_id=jobs[job].id if job else None,
            estimate_number=f"E-{key}", public_token=f"tok-{key}", company_id=TENANT,
            status=status, tax_rate=rate, total=Decimal("1000.00"),
        )
        if deleted:
            from datetime import UTC, datetime
            e.deleted_at = datetime.now(UTC)
        s.add(e)
        s.flush()
        ids[key] = e.id

    zero = Decimal("0")
    est("sent_by_117", "wholesale", "sent", None)       # 117 writes its 0
    est("draft_by_117", "contractor", "draft", None)    # 117 writes its 0
    est("draft_explicit", "wholesale", "draft", zero)
    est("sent_explicit", "wholesale", "sent", zero)     # left: a quote a person sent
    est("accepted", "wholesale", "accepted", zero)      # left: closed
    est("expired", "contractor", "expired", zero)       # left: closed
    est("exempt_draft", "exempt", "draft", zero)        # left: exemption record
    est("lapsed_draft", "lapsed", "draft", zero)        # left: exemption record, exempt=False
    # Taxed before 117 (the switched-off row is not an exemption); 117 zeroes it
    # without reading tax_exemption, so 121 must put it back.
    est("lapsed_sent_by_117", "lapsed", "sent", None)
    est("exempt_sent_by_117", "exempt", "sent", None)   # back to NULL, which resolves to 0
    est("jobless_draft", "wholesale", "draft", zero, job=None)
    est("install_draft", "wholesale", "draft", zero, job="Installation")
    est("newcon_sent", "wholesale", "sent", None, job="New Construction")  # 117 zeroes it; stays 0
    est("retail_draft", "retail", "draft", zero)
    est("deleted_draft", "wholesale", "draft", zero, deleted=True)
    est("draft_five", "wholesale", "draft", Decimal("0.0500"))
    s.commit()
    return ids


def _rates(s: Session, ids) -> dict[str, Decimal | None]:
    from gdx_dispatch.modules.proposals.models import Estimate

    s.expire_all()
    return {k: s.get(Estimate, v).tax_rate for k, v in ids.items()}


def _events(s: Session, action="estimate_tax_rate_null_backfill"):
    from gdx_dispatch.core.audit import AuditLog

    return s.execute(
        select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at)
    ).scalars().all()


def _exercise(engine) -> None:
    from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync, verify_audit_chain
    from gdx_dispatch.modules.proposals.models import Estimate
    from gdx_dispatch.modules.proposals.totals import compute_estimate_totals

    with Session(engine) as s:
        log_audit_event_sync(s, tenant_id=TENANT, user_id="u", action="seed", entity_type="x", details={})
        s.commit()
        ids = _seed(s)

    with engine.begin() as c:
        _load(c, M117).upgrade()

    with Session(engine) as s:
        mid = _rates(s, ids)
        assert mid["sent_by_117"] == 0 and mid["draft_by_117"] == 0 and mid["newcon_sent"] == 0
        # The defect, measured: a 0 on a service-lane estimate shows no tax.
        assert compute_estimate_totals(s.get(Estimate, ids["sent_by_117"]), s)["tax"] == 0
        v_before = {k: s.get(Estimate, ids[k]).version for k in ("sent_by_117", "draft_explicit")}

    with engine.begin() as c:
        _load(c).upgrade()

    changed = ("sent_by_117", "draft_by_117", "draft_explicit", "lapsed_sent_by_117", "exempt_sent_by_117")
    with Session(engine) as s:
        after = _rates(s, ids)
        for k in changed:
            assert after[k] is None, k
        for k in ("sent_explicit", "accepted", "expired", "exempt_draft", "lapsed_draft", "jobless_draft",
                  "install_draft", "newcon_sent", "retail_draft", "deleted_draft"):
            assert after[k] == 0, k
        assert after["draft_five"] == Decimal("0.05")
        for k, v in v_before.items():
            assert s.get(Estimate, ids[k]).version == v + 1, k
        assert s.get(Estimate, ids["sent_by_117"]).total == Decimal("1000.00")

        totals = compute_estimate_totals(s.get(Estimate, ids["sent_by_117"]), s)
        assert (totals["tax"], totals["total"]) == (pytest.approx(73.8), pytest.approx(1073.8))
        # A lapsed exemption is taxed again; an exempt customer's and an
        # installation's estimates still show no tax.
        assert compute_estimate_totals(s.get(Estimate, ids["lapsed_sent_by_117"]), s)["tax"] == pytest.approx(73.8)
        assert compute_estimate_totals(s.get(Estimate, ids["exempt_sent_by_117"]), s)["tax"] == 0
        assert compute_estimate_totals(s.get(Estimate, ids["install_draft"]), s)["tax"] == 0

        (event,) = _events(s)
        d = event.details
        assert event.user_id == "migration:121_trade_estimate_tax_rate_null"
        assert event.tenant_id == TENANT
        assert sorted(d["estimate_ids"]) == sorted(str(ids[k]) for k in changed)
        assert d["left_sent_explicit_zero"] == {str(ids["sent_explicit"]): "sent"}
        assert d["left_closed"] == {str(ids["accepted"]): "accepted", str(ids["expired"]): "expired"}
        assert sorted(d["left_exemption_record"]) == sorted(str(ids[k]) for k in ("exempt_draft", "lapsed_draft"))
        assert sorted(d["left_jobless_or_install"]) == sorted(
            str(ids[k]) for k in ("jobless_draft", "install_draft", "newcon_sent")
        )
        assert verify_audit_chain(s) is True

    # Rerun: nothing in scope is left, so no second event.
    with engine.begin() as c:
        _load(c).upgrade()
    with Session(engine) as s:
        assert len(_events(s)) == 1

        # Someone sets a rate on one changed row, and another is accepted at the default.
        s.get(Estimate, ids["draft_by_117"]).tax_rate = Decimal("0.0100")
        s.get(Estimate, ids["draft_explicit"]).status = "accepted"
        s.commit()
        count = s.scalar(select(func.count()).select_from(AuditLog))

    with engine.begin() as c:
        _load(c).downgrade()

    with Session(engine) as s:
        back = _rates(s, ids)
        assert back["sent_by_117"] == 0
        assert back["draft_by_117"] == Decimal("0.01"), "a rate set since is not the migration's to undo"
        assert back["draft_explicit"] is None, "accepted since: the downgrade leaves it"
        assert s.scalar(select(func.count()).select_from(AuditLog)) == count + 1
        (reverted,) = _events(s, "estimate_tax_rate_null_backfill_reverted")
        assert back["lapsed_sent_by_117"] == 0 and back["exempt_sent_by_117"] == 0
        assert reverted.details["restored"] == 3
        assert verify_audit_chain(s) is True


def test_sqlite(tmp_path) -> None:
    from gdx_dispatch.core.audit import TenantBase

    engine = create_engine(f"sqlite:///{tmp_path / 'm121.db'}", future=True)
    TenantBase.metadata.create_all(bind=engine)
    _exercise(engine)


def test_postgres(pg_test_db) -> None:
    """In a schema of its own built from the ORM, as 117's test does."""
    from gdx_dispatch.core.audit import TenantBase

    schema = "m121_test"
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
