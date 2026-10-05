"""Swallowed reads on a caller-owned session, estimates-pricing side — GDXA-157.

Child of GDXA-86 (``core.database.contained_read``). Eighteen reads across this
domain sat on a session the reading helper did not own. Most were swallowed — read
inside a ``try``, degraded value returned from the ``except``; three deliberately
re-raise and are contained anyway, so that a swallowing frame ABOVE them cannot
inherit an aborted transaction. On SQLite that is honest. On Postgres the failed
statement aborts the whole transaction, so the caller believes it got an answer
AND its uncommitted estimate/invoice/job is already unsavable — the symptom
surfacing later on an unrelated line naming an unrelated table.

**Every test here is Postgres-only, and that is the point.** Pre-fix and post-fix
are byte-identical on SQLite: it does not poison a transaction on a failed
statement, so a green SQLite run is not evidence for a single assertion below.
With no reachable Postgres these SKIP green on a laptop and FAIL under CI (#440).

The caller's pending work is a scratch table (``_Row``), following
``tests/test_plugin_consent_contained_read.py``. A real ``Estimate`` row would say
"the customer's accepted estimate did not save" more directly and was tried first,
but it CANNOT be inserted on this fixture: ``tests/fixtures/structure.sql`` is
stale against the ORM — no ``estimates.jobsite_address`` column — so every ORM
insert of one dies on the template rather than on the defect. Refreshing that dump
(``tools/refresh_test_schema.sh``) is somebody's separate change; a scratch table
makes these tests independent of it either way, which is the better property.
Each read is made to fail the way production can — ``DROP TABLE`` /
``ALTER TABLE ... DROP COLUMN`` on ``pg_test_engine``, or a raw table that
genuinely does not exist — never by monkeypatching the session, which would
prove only that a mock was called.

Falsified twice, measured on PG 15 (see the commit's Verification Manifest):

- ``contained_read`` stubbed to a **no-op** → 15 of 20 redden. The five that do
  not are the five that should not: ``test_pg_a_bare_swallowed_rate_read_...``,
  which writes the pre-fix idiom inline and is the contrasting half;
  ``test_pg_permission_gate_still_loses_the_callers_row``, which pins a limit this
  fix deliberately leaves in place; and the three COST guards (the two
  ``..._opens_no_savepoint_per_row`` and ``..._for_an_eager_loaded_estimate``),
  which count savepoints — with none at all they trivially pass, and that is
  correct.
- ``contained_read`` stubbed to **``db.begin_nested()``** → 1 of 20 reddens:
  ``test_pg_tax_rate_read_failure_...``, on its ``len(db.new) == 1`` assertion.
  That single number is rule 3 — the ORM-level savepoint FLUSHES on entry, so it
  writes the caller's half-built estimate at the moment of a read that used to
  write nothing.

Those numbers count TESTS, not call sites, and an audit was right to say they read
as more coverage than they are: deleting five individual containments (the accept
tail's ``est.total``, the photos read, catalog's ``hydrate_settings_from_db``,
``_engine_sell``'s ``_customer_view``, ``_cached_settings``) left the suite green.
Three of those five now have a site-level guard below (``test_pg_site_coverage_*``),
each falsified individually — uncontain that one site and exactly that one test
reddens. The other two are covered by argument, not by a test, and this says which:

- ``_cached_settings`` — its read is shared by every pricing path, so a "break it"
  fixture reddens half this file for the wrong reason. Its cost guard
  (``..._opens_no_savepoint_per_row``) is what pins its placement.
- the accept tail's ``est.total`` — the read to break is the same column
  ``public_proposal_accept`` writes and commits three lines earlier, so breaking it
  kills the accept before the refresh runs. See
  ``test_pg_contained_read_contains_an_expired_attribute_refresh``, which proves
  the mechanism without claiming to cover the site.

Six earlier drafts of this file were wrong in ways only a falsification pass or an
adversarial audit caught, which is the best argument for running both. Two passed
the no-op pass while testing nothing (one supplied ``lines`` so the lazy load it
claimed to test never happened; one never dropped the table it claimed was
missing). One asserted a victim that does not exist (the public serializer's
callers all return immediately). One argued a rule-5 hazard away instead of
closing it. One set ``accepted_tier_id`` and so never entered the swallowing frame
it existed to test, while asserting on a class name SQLAlchemy never produces. One
added a per-row savepoint to a path that emits no SQL. Each is described at the
test that replaced it.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, String, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import declarative_base, sessionmaker

from gdx_dispatch.modules.proposals.totals import compute_estimate_totals

TENANT = "11111111-1111-4111-8111-111111111111"

_Base = declarative_base()


class _Row(_Base):
    """Stands in for the caller's half-built estimate at the choke point."""

    __tablename__ = "gdxa157_row"
    id = Column(Integer, primary_key=True)
    v = Column(String)


def _sessions(engine):
    # autoflush=False mirrors core.database.SessionLocal, which is what every
    # session reaching these helpers in the app actually is. contained_read's
    # rule 3 turns on this being true (with autoflush the read inside the
    # savepoint would flush the caller's row INTO it and lose it silently).
    _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _pending(db, key: int = 1) -> None:
    """The caller's work: added, NOT flushed, NOT committed."""
    db.add(_Row(id=key, v="the caller's pending work"))


def _committed(engine, key: int = 1) -> int:
    with engine.connect() as other:
        return other.execute(
            text("SELECT count(*) FROM gdxa157_row WHERE id = :k"), {"k": key}
        ).scalar()


def _public_estimate_row(db):
    """A serializer-shaped stand-in. Same reason as ``_estimate``: not an ORM row.

    ``_serialize_public_estimate`` projects explicitly rather than spreading the
    model (that is its whole security property), so every field it reads is named
    here — which means this stub going stale shows up as an AttributeError rather
    than as a silently narrower test.
    """
    return SimpleNamespace(
        id=uuid.uuid4(), estimate_number="EST-GDXA157", label=None,
        jobsite_address=None, description=None, status="sent", valid_until=None,
        sent_at=None, accepted_at=None, declined_at=None, declined_reason=None,
        accepted_tier_id=None, proposal_mode=False, hide_line_prices=None,
        total="1000.00", discount=None, tax_rate=None, customer_id=None,
        company_id=TENANT, lines=None,
    )


def _estimate(*, total="1000.00", tax_rate=None, customer_id=None, lines=None):
    """A stand-in estimate for the totals engine, which only READS attributes.

    Not an ORM row on purpose — see the module docstring on the stale template.
    ``lines=None`` is what reaches ``_load_lines``'s own query; a persistent
    ``Estimate`` short-circuits on its relationship before getting there.
    """
    return SimpleNamespace(
        id=uuid.uuid4(), total=total, discount=None, tax_rate=tax_rate,
        accepted_tier_id=None, customer_id=customer_id, lines=lines,
    )


def _break(engine, *statements: str) -> None:
    """Run DDL that makes a read fail, on its own connection.

    Call this with NO session holding an open transaction on the tables named.
    That ordering is not fussiness: a ``DROP TABLE`` blocks on the ACCESS SHARE
    lock a reader's transaction holds until it ends, so "read it once to prove
    the read works, then drop it" deadlocks and hangs the run rather than
    failing (measured while writing this file). Every test below that wants the
    happy-path proof takes it on a throwaway session and CLOSES that session
    first — see ``_prove_read_works``.
    """
    with engine.begin() as c:
        for s in statements:
            c.execute(text(s))


def _reconcile(engine, *tables) -> None:
    """Add any ORM column the PG template's copy of ``table`` is missing.

    ``tests/fixtures/structure.sql`` has drifted from the ORM (no
    ``estimates.jobsite_address``, no ``estimate_lines.category``), so a helper
    that reads one of these tables dies on the fixture rather than on the defect
    under test. Every added column is nullable and untyped-strictly — these tests
    only need the SELECT to parse, never the data to be right. Refreshing the dump
    with ``tools/refresh_test_schema.sh`` is the real fix and is somebody else's
    change; this keeps these guards independent of when it happens.
    """
    from sqlalchemy import inspect as _inspect

    insp = _inspect(engine)
    for table in tables:
        if not insp.has_table(table.name):
            # Absent entirely (proposal_tier_lines, the pricing_engine set) — the
            # ORM is truth, so build it from the ORM.
            table.create(engine, checkfirst=True)
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        missing = [c for c in table.columns if c.name not in have]
        if not missing:
            continue
        with engine.begin() as conn:
            for col in missing:
                conn.execute(text(
                    f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" '
                    f"{col.type.compile(engine.dialect)} NULL"
                ))


def _prove_read_works(engine, fn) -> None:
    """Run ``fn(db)`` on a throwaway session and close it.

    Without this the DROPs below prove nothing: a read of a table that was never
    there fails identically to a read of one that broke, so the test would pass
    against a fixture artifact. ``structure.sql`` genuinely lacks ``tax_config``
    (the tax module's models are not imported when it is generated), which makes
    that the live risk here rather than a theoretical one.
    """
    db = _sessions(engine)()
    try:
        fn(db)
    finally:
        db.rollback()
        db.close()


def _make_tax_config(engine, *, rate="0.07375", tax_labor=False) -> None:
    """Create and seed ``tax_config`` from the ORM.

    It is absent from ``tests/fixtures/structure.sql`` (the tax module's models
    are not imported when that dump is generated), so a test that merely read it
    would pass for the wrong reason — a fixture artifact rather than the failure
    under test. Creating it, proving the read WORKS, and only then dropping it is
    what makes the drop mean something.
    """
    from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption

    TaxConfig.__table__.create(engine, checkfirst=True)
    TaxExemption.__table__.create(engine, checkfirst=True)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO tax_config "
                "(id, name, default_rate, tax_labor, created_at, updated_at) "
                "VALUES (:i, 'Default', :r, :tl, now(), now())"
            ),
            {"i": str(uuid.uuid4()), "r": rate, "tl": tax_labor},
        )


def _make_pricing_config(engine) -> None:
    """Create and seed the margin-tier tables so ``hydrate_settings_from_db`` works.

    None of ``models/pricing_engine.py``'s tables are in ``structure.sql``, so
    without this the hydrate RAISES and ``_cached_settings`` never populates — which
    matters for the cost guard below: an uncached failure legitimately retries per
    row, so it cannot measure the warm path it exists to protect.
    """
    from gdx_dispatch.models.pricing_engine import (
        CustomerVolumeDiscountTier,
        MarginTier,
        PricingClassSettings,
        PricingSettings,
        PricingTierSet,
    )

    for t in (PricingSettings, PricingTierSet, MarginTier, PricingClassSettings,
              CustomerVolumeDiscountTier):
        t.__table__.create(engine, checkfirst=True)

    # Seeded through the ORM, not raw INSERTs: these tables carry NOT NULL columns
    # with Python-side defaults, so hand-written SQL just guesses at the schema
    # (it took two rounds to find `volume_discount_enabled`). The ORM already
    # knows.
    with sessionmaker(bind=engine)() as seed:
        ts = PricingTierSet(pricing_category="parts", pricing_class="retail", active=True)
        seed.add(PricingSettings())
        seed.add(ts)
        seed.flush()
        seed.add(MarginTier(tier_set_id=ts.id, cost_min=0, cost_max=100000,
                            margin_pct=40, sort_order=1))
        seed.commit()


# ---------------------------------------------------------------------------
# modules/proposals/totals.py — the money math
# ---------------------------------------------------------------------------


def test_pg_tax_rate_read_failure_cannot_cost_the_caller_its_estimate(pg_test_engine):
    """``compute_estimate_totals``'s rate lookup — the sharpest site in the domain.

    ``resolve_rate`` reads ``tax_exemptions`` and ``tax_config`` on the caller's
    session and re-raises, so the ``except Exception: rate = 0.0`` in
    ``compute_estimate_totals`` is the swallow. Bare, the swallow returned a
    plausible ``rate = 0`` while the caller's pending estimate had already become
    unsavable.

    Remove the ``contained_read`` in ``compute_estimate_totals`` and the final
    assertion drops to 0 rows.
    """
    _make_tax_config(pg_test_engine)
    cust = uuid.uuid4()

    # The read WORKS first — otherwise the drop below proves nothing. On its own
    # session, closed before the DROP, or the DROP deadlocks on its read lock.
    def _probe(probe):
        assert compute_estimate_totals(
            _estimate(customer_id=cust), probe
        )["tax_rate"] == pytest.approx(0.07375)

    _prove_read_works(pg_test_engine, _probe)
    _break(pg_test_engine, "DROP TABLE tax_exemption", "DROP TABLE tax_config")

    Session = _sessions(pg_test_engine)
    db = Session()
    _pending(db)
    totals = compute_estimate_totals(_estimate(customer_id=cust), db)
    assert totals["tax_rate"] == 0.0, "the documented degraded rate"
    assert totals["tax"] == 0.0

    assert len(db.new) == 1, "the totals engine flushed the caller's pending estimate"
    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1, (
        "a failed tax-rate read cost the caller its estimate"
    )


def test_pg_a_bare_swallowed_rate_read_loses_the_callers_estimate(pg_test_engine):
    """The contrasting half — the defect itself, asserted on purpose.

    Same scenario, but the swallow is written the way every site in this domain
    was written before GDXA-157: read, ``except``, degraded default, no savepoint.
    This is what makes the test above load-bearing rather than merely green; if
    this one ever stops reproducing, Postgres has changed its abort semantics and
    the whole class needs re-measuring.
    """
    _make_tax_config(pg_test_engine)
    _break(pg_test_engine, "DROP TABLE tax_config")

    Session = _sessions(pg_test_engine)
    db = Session()
    _pending(db)

    # The pre-GDXA-157 idiom, inline.
    try:
        db.execute(text("SELECT default_rate FROM tax_config LIMIT 1")).first()
        rate = 1.0
    except Exception:
        rate = 0.0
    assert rate == 0.0, "the helper believes it answered"

    with pytest.raises(Exception):
        db.commit()
    db.rollback()
    db.close()
    assert _committed(pg_test_engine) == 0, (
        "Postgres no longer aborts the transaction on a failed statement — "
        "re-measure this whole class"
    )


def test_pg_a_failed_rate_read_no_longer_takes_the_totals_block_with_it(pg_test_engine):
    """Containment changes a customer-visible NUMBER here, not just durability.

    The three reads in ``compute_estimate_totals`` run in order: rate,
    ``tax_labor``, then the estimate's lines. Bare, a failed rate read aborted
    the transaction, so the lazy ``estimate.lines`` load failed too — and THAT one
    is outside any ``try``, so ``compute_estimate_totals`` RAISED and the public
    proposal page's ``except`` dropped the whole ``totals`` block. The customer
    opened a proposal with no price on it.

    Contained, the rate read's failure stays its own: the lines still load, so the
    subtotal is right and only the tax is 0.

    This is the one test here that needs a real ORM ``Estimate`` — a stand-in's
    ``lines`` is a plain list, so ``_load_lines`` returns it without a query and
    the collateral damage under test never happens. An earlier version did exactly
    that and passed with ``contained_read`` stubbed out.
    """
    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine

    _reconcile(pg_test_engine, Estimate.__table__, EstimateLine.__table__)
    _make_tax_config(pg_test_engine)
    Session = _sessions(pg_test_engine)
    db = Session()

    # estimates.customer_id carries a real FK, and the rate read needs a customer
    # to check for an exemption, so this one is a genuine row.
    cust = uuid.uuid4()
    with pg_test_engine.begin() as c:
        c.execute(text(
            "INSERT INTO customers (id, name, company_id, created_at) "
            "VALUES (:i, 'GDXA157', :t, now())"
        ), {"i": str(cust), "t": TENANT})

    est = Estimate(
        id=uuid.uuid4(), estimate_number="EST-TOTALS", proposal_mode=False,
        total="1000.00", status="draft", company_id=TENANT,
        public_token=uuid.uuid4().hex, customer_id=cust,
    )
    db.add(est)
    db.add(EstimateLine(
        id=uuid.uuid4(), estimate_id=est.id, description="Labor",
        category="Labor", quantity=1, unit_price="400.00", line_total="400.00",
        company_id=TENANT,
    ))
    db.commit()
    db.expire_all()
    # commit() ended the transaction, so no read lock is held and the DROP below
    # cannot block on one.
    _break(pg_test_engine, "DROP TABLE tax_exemption", "DROP TABLE tax_config")

    totals = compute_estimate_totals(est, db)
    assert totals["subtotal"] == 1000.00
    assert totals["tax"] == 0.0, "the documented degraded rate"
    assert totals["labor_subtotal"] == 400.00, (
        "the lazy `lines` load was collateral damage from the rate read"
    )
    assert totals["total"] == 1000.00
    db.close()


def test_pg_tax_labor_flag_read_failure_cannot_cost_the_caller_its_estimate(pg_test_engine):
    """``_load_tax_labor_flag`` in isolation — the site the issue flagged first.

    Reached with ``estimate.tax_rate`` set explicitly, which skips the rate read
    above so this is the FIRST read to fail. Its degraded ``False`` means "do not
    tax labor", which is the Minnesota-correct answer, so the money is safe here;
    what was not safe was the caller's transaction.
    """
    _make_tax_config(pg_test_engine, tax_labor=True)

    def _probe(probe):
        assert compute_estimate_totals(
            _estimate(tax_rate="0.05"), probe
        )["tax_labor"] is True

    _prove_read_works(pg_test_engine, _probe)
    _break(pg_test_engine, "DROP TABLE tax_config")

    Session = _sessions(pg_test_engine)
    db = Session()
    _pending(db)
    assert compute_estimate_totals(_estimate(tax_rate="0.05"), db)["tax_labor"] is False

    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1, (
        "a failed tax_labor read cost the caller its estimate"
    )


def test_pg_load_lines_degraded_empty_still_taxes_labor_in_full(pg_test_engine):
    """Pins a money defect this fix deliberately does NOT repair.

    ``_load_lines``'s ``except Exception: return []`` means "no labor lines", so
    the labor exclusion is skipped and labor is taxed IN FULL — the one outcome a
    Minnesota garage-door contract must never produce. ``contained_read`` makes
    the caller's transaction survive; it does not make ``[]`` an honest answer,
    and nothing in this file should let a reader believe otherwise.

    Asserted as the CURRENT behaviour, on purpose. When someone decides this read
    should raise rather than degrade, this test is where that lands — invert it.

    (Reached with a stand-in whose ``lines`` is None: a persistent ``Estimate``
    has a real ``lines`` relationship, so the ORM path short-circuits before this
    read. That narrowness is itself worth recording — the site is defensive.)
    """
    _make_tax_config(pg_test_engine, tax_labor=False)
    Session = _sessions(pg_test_engine)
    db = Session()
    est = _estimate(tax_rate="0.10")
    _break(pg_test_engine, "DROP TABLE estimate_lines")

    totals = compute_estimate_totals(est, db)
    assert totals["labor_subtotal"] == 0.0
    assert totals["tax"] == 100.00, (
        "if this changed, the degraded-[] money decision was revisited — good, "
        "but update the docstring in _load_lines with it"
    )

    # The transaction half IS fixed: the session still works afterwards.
    _pending(db)
    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1


# ---------------------------------------------------------------------------
# modules/proposals/router.py — the public proposal page
# ---------------------------------------------------------------------------


def test_pg_public_proposal_company_read_failure_keeps_the_accept_committable(pg_test_engine):
    """``_serialize_public_estimate``'s ``AppSettings`` read.

    NO reachable victim today, and this test says so rather than implying one — an
    audit knocked down the first version's claim that the accept tail's
    ``db.refresh(est)`` runs after the serializer. It runs BEFORE (all three, in
    accept and decline) and all six call sites return immediately, so the poison dies with the
    request.

    What this asserts is therefore the helper's CONTRACT, not a live bug: the
    serializer is handed a session it does not own and must give it back usable.
    The ``_pending``/``commit`` pair below is a stand-in for a caller that has
    staged work, which no current route does — that is the point of the insurance.
    Delete the containment and it still reddens, which is what keeps it a guard.
    """
    from gdx_dispatch.models.tenant_models import Document
    from gdx_dispatch.modules.proposals.models import EstimateLine, ProposalTier
    from gdx_dispatch.modules.proposals.router import _serialize_public_estimate

    # The serializer reads these two unconditionally and `structure.sql` is stale
    # for both, so reconcile them against the ORM first — otherwise this test
    # fails on the fixture instead of on the AppSettings read it is about.
    # ADD COLUMN rather than drop-and-recreate on purpose: recreating pulls in
    # the FK chain (`labor_price_items`, also absent from the dump) and would
    # trade one fixture problem for another.
    _reconcile(pg_test_engine, EstimateLine.__table__, ProposalTier.__table__,
               Document.__table__)

    Session = _sessions(pg_test_engine)
    db = Session()
    est = _public_estimate_row(db)
    _break(pg_test_engine, "DROP TABLE app_settings")

    body = _serialize_public_estimate(est, db, None)
    assert body["company"] == {"name": "", "phone": ""}, "the documented degradation"

    # What the accept tail does immediately after this returns is
    # `db.refresh(est)`; with a stand-in estimate that is not a mapped instance,
    # so stand in for it with the same thing that matters — the session must
    # still be able to emit SQL and commit.
    assert db.execute(text("SELECT 1")).scalar() == 1, "the session came back dead"
    _pending(db)
    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1


def test_pg_nesting_a_contained_read_around_a_self_swallowing_callee_is_legal(pg_test_engine):
    """The rule-5 coupling the proposals router leans on, proven not assumed.

    ``contained_read`` rule 5 says wrapping a callee that swallows its own
    failure is WORSE than not wrapping: the block exits CLEAN, so the
    ``RELEASE SAVEPOINT`` lands on an already-aborted transaction and raises
    25P02 out of the ``with`` itself. The public serializer wraps
    ``compute_estimate_totals``, which DOES swallow internally — legal only
    because every read it swallows is itself contained now, leaving the
    transaction healthy for the outer RELEASE.

    That is a real coupling between two files, so it gets a test rather than a
    comment. Un-contain ``modules/proposals/totals.py`` and this reddens with
    InFailedSqlTransaction — which is the whole reason the comment at that call
    site points here.
    """
    from gdx_dispatch.core.database import contained_read

    _make_tax_config(pg_test_engine)
    Session = _sessions(pg_test_engine)
    db = Session()
    _pending(db)
    est = _estimate(customer_id=uuid.uuid4())

    # The caller has emitted no SQL yet, so it holds no lock the DROP would wait on.
    _break(pg_test_engine, "DROP TABLE tax_exemption", "DROP TABLE tax_config")

    # The outer wrap exits clean, because the inner swallow returns a default.
    with contained_read(db):
        totals = compute_estimate_totals(est, db)
    assert totals["tax_rate"] == 0.0

    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1


# ---------------------------------------------------------------------------
# routers/catalog.py — a genuinely raw table, so no DROP needed
# ---------------------------------------------------------------------------


def test_pg_virtual_catalog_probe_cannot_cost_the_caller_its_row(pg_test_engine):
    """``_list_virtual_catalogs`` — two probes in a loop, so the first must not
    break the second, and neither must break the caller.

    The first version of this test was VACUOUS and it is worth saying why, because
    the mistake is the one this whole class is made of: it asserted
    ``_list_virtual_catalogs(db) == []`` without dropping anything, on the belief
    that ``chi_door_catalog`` is a raw plugin table absent from the fixture. It is
    in ``structure.sql`` (``models/tenant_models.py`` declares it on
    ``TenantBase``), so the probe SUCCEEDED and returned ``[]`` because the table
    was empty. Measured: the probe raised nothing and ``SELECT 1`` after it was
    fine. It passed with ``contained_read`` stubbed to a no-op — which is exactly
    what the falsification pass is for.

    Both tables are dropped now, and seeded first so the happy path is proven.
    """
    from gdx_dispatch.routers.catalog import _list_virtual_catalogs

    with pg_test_engine.begin() as c:
        c.execute(text(
            "INSERT INTO chi_door_catalog (id, sku, is_custom, is_active, imported_at) "
            "VALUES (:i, 'SKU-1', false, true, now())"
        ), {"i": str(uuid.uuid4())})

    def _probe(probe):
        found = _list_virtual_catalogs(probe)
        assert len(found) == 1, f"the probe must work before it is broken: {found}"

    _prove_read_works(pg_test_engine, _probe)
    _break(pg_test_engine, "DROP TABLE chi_door_catalog", "DROP TABLE chi_parts_catalog")

    Session = _sessions(pg_test_engine)
    db = Session()
    _pending(db)

    assert _list_virtual_catalogs(db) == [], "both probes degrade to no catalogs"

    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1, (
        "a missing virtual-catalog table cost the caller its row"
    )


# ---------------------------------------------------------------------------
# routers/estimates.py — the accept→job conversion
# ---------------------------------------------------------------------------


def test_pg_holding_area_lookup_failure_no_longer_sinks_the_conversion(pg_test_engine):
    """``_holding_area_id_by_name``, whose docstring promised exactly this.

    "Missing area is logged and the job is created without holding_area_id rather
    than failing the customer-facing accept" was false on Postgres: this runs as
    an argument to the ``Job(...)`` constructor, so the abort killed the
    ``db.add(new_job); db.flush()`` two lines later, the audit row recording the
    failure died on the same session, and ``db.refresh(est)`` raised out of the
    route. The assertion below is the promise, made true.
    """
    from gdx_dispatch.routers.estimates import _holding_area_id_by_name

    Session = _sessions(pg_test_engine)
    db = Session()
    _pending(db)
    _break(pg_test_engine, "DROP TABLE holding_areas")

    assert _holding_area_id_by_name(db, "Order Doors") is None

    # What _create_job_from_estimate does on the next two lines.
    db.flush()
    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1, (
        "a missing holding area cost the customer their converted job"
    )


# ---------------------------------------------------------------------------
# routers/change_orders.py — the same money resolver, a different surface
# ---------------------------------------------------------------------------


def test_pg_change_order_tax_read_failure_keeps_the_caller_committable(pg_test_engine):
    """``_serialize``'s ``resolve_rate`` call.

    No reachable victim today — the only call site passing ``db`` is a read-only
    GET — so this guards the helper's contract rather than a live path, and the
    comment at the site says which. It is the shared serializer for a money
    surface; the next caller to hand it a session with staged work inherits the
    class silently without this.
    """
    from gdx_dispatch.routers.change_orders import ChangeOrder, _serialize

    _make_tax_config(pg_test_engine)
    Session = _sessions(pg_test_engine)
    db = Session()
    _pending(db)
    co = ChangeOrder(id=uuid.uuid4(), customer_id=uuid.uuid4())
    _break(pg_test_engine, "DROP TABLE tax_exemption", "DROP TABLE tax_config")

    out = _serialize(co, lines=[], db=db)
    assert out["tax_rate"] == 0.0, "the documented LOUD-log degradation"

    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1


# ---------------------------------------------------------------------------
# core/part_pricing.py — the capture path's price resolver
# ---------------------------------------------------------------------------


def test_pg_part_price_resolve_failure_keeps_the_caller_committable(pg_test_engine):
    """``resolve_sell_price_with_source`` and, one frame down, ``_engine_sell``.

    ``_resolve_sell_price`` walks up to four lanes with a direct read each and
    catches nothing of its own, so all four failures land on this one
    ``except Exception: return None, None``. The caller is a capture path that is
    about to commit a priced ``job_parts_needed`` row — "the office prices it" is
    a fine answer, losing the row is not.

    ``job_parts_needed`` exists in the template and ``pricing_tier_sets`` does
    not, so the first lane's read is dropped to force lane 1 to fail rather than
    fall through.
    """
    from gdx_dispatch.core.part_pricing import resolve_sell_price_with_source

    Session = _sessions(pg_test_engine)
    db = Session()
    _pending(db)
    _break(pg_test_engine, "DROP TABLE job_parts_needed")

    assert resolve_sell_price_with_source(
        db, job_id=str(uuid.uuid4()), sku="GDXA157-SKU"
    ) == (None, None)

    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1


# ---------------------------------------------------------------------------
# routers/door_listings.py — the fail-closed permission gate
# ---------------------------------------------------------------------------


def test_pg_permission_gate_still_loses_the_callers_row(pg_test_engine):
    """Pins a KNOWN LIMIT, asserting the bad behaviour on purpose.

    ``_has_office`` is on the GDXA-157 census list and is deliberately NOT
    contained. ``_load_user_permissions`` (``core/modules.py``, platform-core's
    file) catches ``SQLAlchemyError`` on its role lookup and calls
    **``db.rollback()``** on the session it was handed — so it destroys the
    caller's pending work directly, and no savepoint at THIS frame can undo a
    rollback that already happened one frame down. Wrapping it would also be
    ``contained_read`` rule 5's mistake, because it swallows its own failure.

    Found by running this file's guards, not by reading the code: the attempt to
    contain it logged ``contained_read_staged_a_write before=(1, 0, 0)
    after=(0, 0, 0)`` and the caller's row still never committed. The fix is a
    savepoint in place of that session-wide rollback, in ``core/modules.py``.
    When someone makes it, this test is where it lands — invert the assertion.
    """
    from gdx_dispatch.routers.door_listings import _has_office

    Session = _sessions(pg_test_engine)
    db = Session()
    _pending(db)
    # state.current_user is the FIRST fallback `_resolve_request_user` tries, so
    # this stub never reaches the `request.app` branch (which it has no attribute
    # for, and which swallows its own failure anyway).
    request = SimpleNamespace(
        state=SimpleNamespace(
            tenant={"id": TENANT},
            user_permissions=None,
            current_user={"sub": str(uuid.uuid4()), "role": "tech"},
        ),
        headers={},
    )

    _break(pg_test_engine, "DROP TABLE user_role_assignments")

    assert _has_office(request, db, request.state.current_user) is False, (
        "the gate must fail closed"
    )

    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 0, (
        "core/modules.py no longer rolls the caller back — good; contain "
        "_has_office and invert this"
    )


def test_pg_the_deposit_block_can_be_wrapped_because_totals_is_fully_contained(pg_test_engine):
    """The rule-5 hole an audit found, closed and pinned.

    ``modules/deposits/service.py::deposit_ask_for`` has its own bare
    ``except Exception: return None`` around ``compute_estimate_totals``. That
    makes it a SWALLOWING frame between the public serializer's savepoint and the
    read — the shape ``contained_read`` rule 5 says is worse than no wrapping,
    because the block exits CLEAN and ``RELEASE SAVEPOINT`` on an aborted Postgres
    transaction is itself an error (measured on PG 15.17: SAVEPOINT and RELEASE
    both answer 25P02 after a failed statement).

    It is safe only because the statements ``compute_estimate_totals`` issues are
    contained, including the ones that deliberately do NOT swallow. Un-contain
    ``_load_lines``'s read and this reddens — measured both ways:
    contained → no exception, caller's row committed; uncontained → InternalError
    (25P02) and 0 rows.

    **``accepted_tier_id`` MUST stay None.** The first version of this test set it,
    which sends ``deposit_ask_for`` down its ``tier is not None`` branch — the one
    that never enters the ``try``. It passed without ever executing the swallowing
    frame it exists to test, and the audit that caught it also caught the second
    half: ``assert "InFailedSqlTransaction" not in type(exc).__name__`` can never
    fire, because SQLAlchemy wraps that psycopg2 error as ``InternalError``. The
    pgcode is the thing to assert on.
    """
    from gdx_dispatch.core.database import contained_read
    from gdx_dispatch.modules.deposits.service import deposit_ask_for
    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine, ProposalTier

    _reconcile(pg_test_engine, Estimate.__table__, EstimateLine.__table__,
               ProposalTier.__table__)
    Session = _sessions(pg_test_engine)
    db = Session()

    cust = uuid.uuid4()
    with pg_test_engine.begin() as c:
        c.execute(text(
            "INSERT INTO customers (id, name, company_id, created_at) "
            "VALUES (:i, 'GDXA157', :t, now())"
        ), {"i": str(cust), "t": TENANT})

    est = Estimate(
        id=uuid.uuid4(), estimate_number="EST-DEPOSIT", proposal_mode=False,
        total="1000.00", status="accepted", company_id=TENANT,
        public_token=uuid.uuid4().hex, customer_id=cust,
        accepted_tier_id=None,  # load-bearing — see the docstring
    )
    db.add(est)
    db.commit()
    db.expire_all()

    # Break the lines read, which `compute_estimate_totals` does not swallow, so
    # the abort reaches deposit_ask_for's bare except.
    _break(pg_test_engine, "DROP TABLE estimate_lines")

    _pending(db)
    # The nesting exactly as modules/proposals/router.py performs it.
    raised: Exception | None = None
    try:
        with contained_read(db):
            deposit_ask_for(est, db, TENANT)
    except Exception as exc:  # noqa: BLE001
        raised = exc

    pgcode = getattr(getattr(raised, "orig", None), "pgcode", None)
    assert pgcode != "25P02", (
        "RELEASE SAVEPOINT landed on an aborted transaction — rule 5. A statement "
        "inside compute_estimate_totals is no longer contained."
    )

    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1, (
        "the deposit lookup cost the caller its row"
    )


def test_pg_totals_opens_no_savepoint_for_an_eager_loaded_estimate(pg_test_engine):
    """A cost guard for the SAVEPOINT this fix added to the lazy-`lines` path.

    `routers/portal.py::_portal_estimate_totals` serializes a LIST of estimates one per row, so an
    unconditional savepoint around `getattr(estimate, "lines")` would pay
    SAVEPOINT+RELEASE per row for an attribute that, once eager-loaded, emits no
    SQL at all. `_lines_would_query` gates it. Counted, not timed — a wall-clock
    threshold on a shared box is a flake.
    """
    from sqlalchemy import event
    from sqlalchemy.orm import selectinload

    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine, ProposalTier

    _reconcile(pg_test_engine, Estimate.__table__, EstimateLine.__table__,
               ProposalTier.__table__)
    _make_tax_config(pg_test_engine)
    Session = _sessions(pg_test_engine)
    db = Session()
    est = Estimate(
        id=uuid.uuid4(), estimate_number="EST-EAGER", proposal_mode=False,
        total="1000.00", status="draft", company_id=TENANT,
        public_token=uuid.uuid4().hex, tax_rate="0.05",
    )
    db.add(est)
    db.commit()
    db.close()

    db = Session()
    loaded = db.execute(
        select(Estimate).options(selectinload(Estimate.lines))
        .where(Estimate.estimate_number == "EST-EAGER")
    ).scalar_one()

    seen: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        seen.append(statement.strip().split()[0].upper())

    event.listen(pg_test_engine, "before_cursor_execute", _count)
    try:
        compute_estimate_totals(loaded, db)
    finally:
        event.remove(pg_test_engine, "before_cursor_execute", _count)
    db.close()

    savepoints = [s for s in seen if s in ("SAVEPOINT", "RELEASE")]
    assert len(savepoints) <= 2, (
        f"eager-loaded estimate still opened {len(savepoints)} savepoint statements "
        f"({seen}) — _lines_would_query is not gating the free path"
    )


def test_pg_the_catalog_pricer_opens_no_savepoint_per_row(pg_test_engine):
    """A cost guard, not a correctness one, and it is here because this fix
    introduced the regression it pins.

    ``_engine_pricer`` returns a closure called once per catalog ROW.
    ``_cached_settings`` is memoized on ``Session.info``, so after the first row it
    emits no SQL at all — and a ``contained_read`` at that call site therefore paid
    2 statements (SAVEPOINT + RELEASE) per row for no read. Measured by an audit at
    260 extra statements and 107 ms on a 130-row page of
    ``/api/catalogs/all-items``, which is unpaginated and is the endpoint the cache
    exists to keep fast.

    The savepoint lives inside ``_cached_settings``'s cache miss now, so it happens
    once. Counted rather than timed: a wall-clock threshold on a shared box is a
    flake, a statement count is not.
    """
    from sqlalchemy import event

    from gdx_dispatch.routers.catalog import _engine_pricer

    _make_pricing_config(pg_test_engine)
    Session = _sessions(pg_test_engine)
    db = Session()

    seen: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        seen.append(statement.strip().split()[0].upper())

    event.listen(pg_test_engine, "before_cursor_execute", _count)
    try:
        pricer = _engine_pricer(db, catalog=None, catalogs_by_id=None)
        for _ in range(30):
            pricer(cost=100, pricing_category="parts")
    finally:
        event.remove(pg_test_engine, "before_cursor_execute", _count)
    db.close()

    savepoints = [s for s in seen if s in ("SAVEPOINT", "RELEASE", "ROLLBACK")]
    assert len(savepoints) <= 3, (
        f"one savepoint for 30 rows, got {len(savepoints)}: {savepoints[:8]} — "
        "a contained_read moved back onto the per-row path"
    )


def test_pg_the_catalog_import_pricer_opens_no_savepoint_per_row(pg_test_engine):
    """The same cost guard for ``sell_price_for_row``, which the catalog import
    (``routers/catalog.py::_retail_for``) calls once per ROW with no job and no
    customer. ``_engine_sell`` contains ``_customer_view``'s read, but with neither
    id that helper returns the retail default without a query, and an audit
    measured the unguarded wrap at 130 SAVEPOINT + 130 RELEASE for 130 rows and
    zero SELECTs.
    """
    from sqlalchemy import event

    from gdx_dispatch.core.part_pricing import sell_price_for_row

    _make_pricing_config(pg_test_engine)
    Session = _sessions(pg_test_engine)
    db = Session()

    seen: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        seen.append(statement.strip().split()[0].upper())

    event.listen(pg_test_engine, "before_cursor_execute", _count)
    try:
        for _ in range(30):
            sell_price_for_row(db, price=None, cost=10, pricing_category="parts")
    finally:
        event.remove(pg_test_engine, "before_cursor_execute", _count)
    db.close()

    savepoints = [s for s in seen if s in ("SAVEPOINT", "RELEASE", "ROLLBACK")]
    assert len(savepoints) <= 3, (
        f"one savepoint for 30 rows, got {len(savepoints)}: {savepoints[:8]} — "
        "_engine_sell wraps _customer_view even when it has nothing to read"
    )


# ---------------------------------------------------------------------------
# Per-SITE coverage — the tests above are per-consequence, and an audit showed
# that leaves individual containments deletable with the suite still green.
# Each of these breaks ONE read and asserts the caller's row survives, so
# removing that one wrap reddens exactly one test.
# ---------------------------------------------------------------------------


def test_pg_contained_read_contains_an_expired_attribute_refresh(pg_test_engine):
    """`contained_read` around an ORM attribute refresh, which no census can see.

    **Read the name carefully: this is NOT site coverage for
    `modules/proposals/router.py`'s post-commit `est.total`.** It was written as
    that and falsified: uncontaining the router site leaves this GREEN, because the
    test performs the containment inline rather than driving the endpoint. Renamed
    rather than left with a name that implies coverage it does not have.

    Driving the real endpoint for this one is genuinely hard, and worth recording
    so the next person does not think it was skipped out of laziness: the read to
    break is `estimates.total`, and that is the same column
    `public_proposal_accept` WRITES and commits three lines earlier, so breaking it
    kills the accept before the refresh is ever reached. That site is therefore
    covered by argument and measurement (`expire_on_commit=True` makes the
    attribute a SELECT — measured) rather than by a test, and the header says so.

    What this DOES prove is the property the argument rests on: an expired
    attribute is a real statement, it does poison on failure, and `contained_read`
    contains it. Stub the helper to a no-op and this reddens.
    """
    from gdx_dispatch.core.database import contained_read
    from gdx_dispatch.modules.proposals.models import Estimate

    _reconcile(pg_test_engine, Estimate.__table__)
    Session = _sessions(pg_test_engine)
    db = Session()
    est = Estimate(
        id=uuid.uuid4(), estimate_number="EST-EXPIRED", proposal_mode=False,
        total="1000.00", status="accepted", company_id=TENANT,
        public_token=uuid.uuid4().hex,
    )
    db.add(est)
    db.commit()          # expires every attribute on `est`
    assert "total" in sa_inspect(est).unloaded, "not expired — the test is vacuous"

    _break(pg_test_engine, "ALTER TABLE estimates DROP COLUMN total")

    _pending(db)
    # The accept tail's shape: the refresh inside its own bare swallow.
    try:
        with contained_read(db):
            amount = float(est.total or 0)
    except Exception:
        amount = 0.0
    assert amount == 0.0, "the documented degraded amount"

    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1, (
        "a failed attribute refresh cost the caller its row — the office bell "
        "write that follows it in public_proposal_accept would have died too"
    )


def test_pg_site_coverage_public_proposal_photos(pg_test_engine):
    """`_estimate_public_photos`' Document read, on the customer-facing page."""
    from gdx_dispatch.models.tenant_models import Document
    from gdx_dispatch.modules.proposals.models import EstimateLine, ProposalTier
    from gdx_dispatch.modules.proposals.router import _serialize_public_estimate

    _reconcile(pg_test_engine, EstimateLine.__table__, ProposalTier.__table__,
               Document.__table__)
    Session = _sessions(pg_test_engine)
    db = Session()
    est = _public_estimate_row(db)

    _break(pg_test_engine, "DROP TABLE documents")

    _pending(db)
    body = _serialize_public_estimate(est, db, None)
    assert body["photos"] == [], "the documented degradation"

    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1


def test_pg_site_coverage_virtual_catalog_items_pricing_hydrate(pg_test_engine):
    """`_virtual_catalog_items`' `hydrate_settings_from_db`.

    Distinct from the pricer's cache miss: this one runs once per request on the
    CHI virtual-catalog items endpoint and degrades to `pricing_status="error"`,
    then goes on to read the catalog rows on the same session.
    """
    from gdx_dispatch.routers.catalog import VIRTUAL_CHI_PARTS_ID, _virtual_catalog_items

    _make_pricing_config(pg_test_engine)
    # margin_tiers FKs to pricing_tier_sets, so drop a COLUMN instead — same
    # UndefinedColumn class of failure, no dependency to fight.
    _break(pg_test_engine, "ALTER TABLE pricing_tier_sets DROP COLUMN pricing_class")

    Session = _sessions(pg_test_engine)
    db = Session()
    _pending(db)

    out = _virtual_catalog_items(VIRTUAL_CHI_PARTS_ID, None, 1, 10, db)
    assert out["pricing_status"] in ("error", "not_configured"), out["pricing_status"]

    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1


def test_pg_site_coverage_engine_sell_customer_view(pg_test_engine):
    """`_engine_sell`'s `_customer_view` — the job lookup behind a part's price.

    Its `except Exception` swallows a DB fault as well as a config one, and the
    caller is a closeout about to commit a priced `job_parts_needed` row.
    """
    from gdx_dispatch.core.part_pricing import _engine_sell

    _make_pricing_config(pg_test_engine)
    _break(pg_test_engine, "ALTER TABLE jobs DROP COLUMN customer_id")

    Session = _sessions(pg_test_engine)
    db = Session()
    _pending(db)

    # job_id set and customer_id None is what makes _customer_view read `jobs`.
    assert _engine_sell(db, cost=100, pricing_category="parts",
                        job_id=str(uuid.uuid4()), customer_id=None) is None

    db.commit()
    db.close()
    assert _committed(pg_test_engine) == 1
