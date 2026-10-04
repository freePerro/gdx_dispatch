"""The jobs-dispatch swallowed reads must not cost the caller its work — GDXA-158.

Child of GDXA-86 (``core.database.contained_read``). Seven helpers in this
domain took the caller's session, read inside a ``try``, and returned a degraded
value from the ``except``. On SQLite that is honest. On Postgres a failed
statement aborts the whole transaction, so the degraded answer bought nothing
and the caller's next statement died with ``InFailedSqlTransaction`` (25P02).

**Every transaction-poisoning test here is Postgres-only, and that is the
point.** SQLite does not abort on a failed statement, so pre-fix and post-fix
are indistinguishable there. The PG arm skips (green) with no reachable
Postgres and fails under CI per #440 — so a green local matrix is not evidence
for this file. Run it with ``GDX_TEST_PG_HOST``/``GDX_TEST_PG_PORT`` set.

The reads fail the way production fails: a real aborted transaction on a real
engine, never a monkeypatched session. A monkeypatch proves the ``except`` runs;
only a real abort proves the containment.

Be exact about the mechanism, because "``DROP TABLE`` on the real engine" is what
this said first and it is only true for some of them:
``tests/fixtures/structure.sql`` carries no ``phone_com_calls`` and no
``outlook_accounts``, and its ``planner_tasks`` lacks ``contact_phone`` /
``phone_com_call_id`` / ``source``. So for those sites ``_drop()`` is a no-op and
the read fails on a table that was never created — fixture ABSENCE, not a drop.
The abort, and therefore the containment, is identical either way; the claim
about how it was produced is what needed correcting. It also means no PG guard in
this repo can drive an ORM path over ``planner_tasks``. The PG
``link_customer`` test therefore asserts the transaction shape; the endpoint
itself is driven on an ORM-built SQLite schema
(``test_link_customer_does_not_report_a_backfill_it_rolled_back``), which
proves the savepoint's rollback and the handler's reset but not the 25P02 half.

What each test asserts is that the CALLER'S COMMITTED WORK SURVIVES, not that
the session still answers ``SELECT 1``. Data loss is the consequence that
matters, and a session that answers ``SELECT 1`` after a rollback has already
lost it.

The contrasting pair at the top is the falsification harness: ``_bare`` re-runs
each helper's ORIGINAL body inline, so the "without the fix" column is measured
in the same test run rather than asserted from memory.

Measured: of the 16 tests here, 13 are Postgres-gated and 3 are
dialect-independent (``grep -c '^def test_pg_'`` against ``'^def test_'``).

**Two of the ten sites have no test that calls them**: the customer-name reads
in ``closeout_job`` and ``spawn_return_visit``. Reverting either leaves this
file green. ``test_pg_job_number_alloc_read_does_not_block_the_closeout`` pins
the shape, not the call site. Driving either endpoint on Postgres needs the
role, permission and job-access fixture on top of ``structure.sql``, which this
change does not build. That is a test gap, stated here rather than implied.

**What this file does NOT close, because the honest answer is uncomfortable.**
GDXA-158 wrapped ten sites — nine pure reads with ``contained_read`` and one
write-bearing block (``planner.py::link_customer``) with ``db.begin_nested()``.
Two neighbouring shapes are counted in the PR's sweep block and deliberately
left:

1. ``db.rollback()`` in the handler — the OTHER wrong remedy for this class, and
   the one ``contained_read`` rule 4 explicitly forbids, because a full rollback
   expires every object the caller is holding. **Seventeen** instances in the two
   files examined (``labor.py`` ×3, ``job_costing.py`` ×14) — counted by AST over
   ``db.rollback()`` inside an ``except`` handler. Checked, and NOT a live loss
   today: every caller is either a read path or runs after ``db.commit()``. So it
   is an accounting gap rather than a defect, which is exactly why it would have
   been easy to leave unmentioned. Domain-wide the figure is **68 across 20
   files** — ``jobs.py`` alone has 18, two of those being
   ``with contextlib.suppress(Exception): db.rollback()`` nested inside an
   ``except`` (the ``ready_for_billing_failed`` and
   ``return_visits_unscheduled_failed`` handlers), which the recipe DOES match
   because it walks every node under the handler. That larger number is NOT triaged here and
   should not be read as cleared.
2. ``routers/estimates.py::_holding_area_id_by_name`` was a verbatim twin of the
   ``jobs.py`` helper — same raw SQL, same swallow, same false docstring promise.
   #852 (GDXA-157) contained the estimates copy on main first, so once this branch
   was rebased the two were identical and ``duplicate_block_scan`` flagged three
   net-new groups. Both now re-export one function,
   ``core/holding_areas.py::holding_area_id_by_name``, so the tests below that
   import ``routers.jobs._holding_area_id_by_name`` exercise that single copy.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Column, Integer, String, create_engine, event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker

_Base = declarative_base()


class _CallerRow(_Base):
    """Stands in for the caller's half-built job/closeout/time-entry."""

    __tablename__ = "gdxa158_caller_row"
    id = Column(Integer, primary_key=True)
    v = Column(String)


def _sessions(engine):
    # autoflush=False mirrors core.database.SessionLocal, which is what every
    # session reaching these helpers in the app actually is. With autoflush on,
    # contained_read's no_autoflush is doing the work instead and these tests
    # would be measuring a different guarantee (core.database, rule 3).
    _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _drop(engine, table: str) -> None:
    """Make the helper's read fail the way prod would — the table is gone."""
    with engine.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))


def _committed(engine, *, expect: int) -> None:
    with engine.connect() as other:
        got = other.execute(
            text("SELECT count(*) FROM gdxa158_caller_row")
        ).scalar()
    assert got == expect, (
        f"the caller's committed work: expected {expect} row(s), found {got}"
    )


# ---------------------------------------------------------------------------
# The falsification pair — what the fix is worth, measured in this run
# ---------------------------------------------------------------------------


def test_pg_a_bare_swallowed_read_loses_the_callers_row(pg_test_engine):
    """The defect itself, reproduced WITHOUT contained_read.

    This is the control. It runs ``_holding_area_id_by_name``'s original body
    inline — same query, same ``except Exception: return None`` — so the "before"
    column is a measurement, not a claim. If this test ever stops failing to
    commit, Postgres has stopped aborting transactions and this whole file is
    moot.
    """
    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "holding_areas")
    db = Session()
    db.add(_CallerRow(id=1, v="the caller's work"))

    # ── verbatim pre-fix body of routers/jobs.py::_holding_area_id_by_name ──
    try:
        row = db.execute(
            text("SELECT id FROM holding_areas WHERE name = :n LIMIT 1"),
            {"n": "Ready to Schedule"},
        ).first()
        out = str(row[0]) if row else None
    except Exception:
        out = None
    # ───────────────────────────────────────────────────────────────────────

    assert out is None, "the swallow still returns its degraded default"
    with pytest.raises(SQLAlchemyError):
        db.commit()  # 25P02 — the work is gone
    db.rollback()
    db.close()
    _committed(pg_test_engine, expect=0)


def test_pg_contained_read_keeps_the_callers_transaction_committable(pg_test_engine):
    """The same read, through the real helper. Degrades AND commits.

    Revert the ``with contained_read(db):`` in
    ``core/holding_areas.py::holding_area_id_by_name`` (re-exported by
    ``routers/jobs.py``) and this fails at
    ``db.commit()`` with ``InFailedSqlTransaction``.
    """
    from gdx_dispatch.routers.jobs import _holding_area_id_by_name

    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "holding_areas")
    db = Session()
    db.add(_CallerRow(id=1, v="the caller's work"))

    assert _holding_area_id_by_name(db, "Ready to Schedule") is None, (
        "a missing lane must still degrade to None"
    )

    db.commit()
    db.close()
    _committed(pg_test_engine, expect=1)


# ---------------------------------------------------------------------------
# routers/jobs.py
# ---------------------------------------------------------------------------


def test_pg_display_state_degrades_without_poisoning_the_list_endpoint(pg_test_engine):
    """``_display_state_for_jobs`` promises, in its own docstring, that "any
    failure degrades to an empty map so the jobs list never breaks over a
    display field". On Postgres that was false: every caller is a GET that keeps
    querying, and ``list_jobs`` calls ``resolve_job_sites(db, …)`` on the very
    next statement.

    So this asserts the thing the docstring actually promises — a FOLLOWING
    query still works — rather than only that the map came back empty. Revert
    the containment and the follow-up read raises 25P02.
    """
    from gdx_dispatch.routers.jobs import _display_state_for_jobs

    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "invoices")
    db = Session()
    db.add(_CallerRow(id=1, v="a list endpoint holds no pending work, but"))

    out = _display_state_for_jobs(db, [(uuid.uuid4(), "completed")])
    assert out == {}, "a failed enrichment must degrade to an empty map"

    # What list_jobs does on the next line, and the assertion that matters.
    # It is load-bearing against BOTH wrong implementations, which is why it is
    # written as an equality rather than a bare "does not raise":
    #   - contained_read removed  → this execute() raises 25P02
    #   - contained_read swapped for db.begin_nested() → the entry flush writes
    #     the caller's pending row, so this returns [1] instead of []
    # Measured: it is one of the 2 tests that redden under the begin_nested stub.
    assert db.execute(select(_CallerRow.id)).scalars().all() == []
    db.commit()
    db.close()
    _committed(pg_test_engine, expect=1)


def test_pg_resolve_technician_id_does_not_cost_the_closeout(pg_test_engine):
    """``closeout_job`` reaches ``_resolve_technician_id`` mid-write — its parts
    rows are already ``db.add()``ed (step 1, before step 2's labor trail). A
    bare swallow returned None, which downstream reads as "not a technician" and
    costs the labor row its rate; the real damage was the tech's whole attested
    closeout failing to commit.

    Attested hours are the only payable input in this repo, so losing them
    silently is the worst end of this class.
    """
    from gdx_dispatch.routers.jobs import _resolve_technician_id

    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "technicians")
    db = Session()
    db.add(_CallerRow(id=1, v="the closeout's staged parts rows"))

    assert _resolve_technician_id(db, str(uuid.uuid4())) is None

    db.commit()
    db.close()
    _committed(pg_test_engine, expect=1)


# ---------------------------------------------------------------------------
# routers/labor.py — a billed rate decided from a swallowed read
# ---------------------------------------------------------------------------


def test_pg_resolve_hourly_rate_degrades_and_still_commits(pg_test_engine):
    """The site with money on it. ``create_job_time_entry`` calls this inside
    the ``TimeEntry(...)`` constructor and ``update_time_entry`` one line after
    mutating ``row.tech_id``, so both reach it with work pending.

    Two assertions, and the second is the one the fix adds: the rate still falls
    back to the documented default, AND the entry can still be written.
    """
    from gdx_dispatch.routers.labor import DEFAULT_HOURLY_RATE, _resolve_hourly_rate

    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "technicians")
    db = Session()
    db.add(_CallerRow(id=1, v="the tech's time entry"))

    assert _resolve_hourly_rate(db, str(uuid.uuid4())) == DEFAULT_HOURLY_RATE

    db.commit()
    db.close()
    _committed(pg_test_engine, expect=1)


def test_pg_labor_rate_for_is_fixed_by_its_callee_not_by_its_own_savepoint(
    pg_test_engine,
):
    """``jobs.py::_labor_rate_for`` is on the census list and is deliberately
    NOT wrapped — core.database rule 5. It delegates to ``_resolve_hourly_rate``,
    which swallows its own failure, so a ``contained_read`` here would exit the
    block CLEAN and issue ``RELEASE SAVEPOINT`` on an already-aborted
    transaction: 25P02 raised out of the ``with`` itself, somewhere the caller
    has no handler, which is strictly worse than not wrapping.

    This test is the evidence for that "no change needed" verdict rather than an
    assertion that the line is fine: containing the callee fixes this caller
    too, which is why ``_labor_rate_for`` is counted as "no change needed"
    rather than among the ten fixed sites.
    """
    from gdx_dispatch.routers.jobs import _labor_rate_for
    from gdx_dispatch.routers.labor import DEFAULT_HOURLY_RATE

    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "technicians")
    db = Session()
    db.add(_CallerRow(id=1, v="the closeout's staged rows"))

    assert _labor_rate_for(db, str(uuid.uuid4())) == DEFAULT_HOURLY_RATE

    db.commit()
    db.close()
    _committed(pg_test_engine, expect=1)


# ---------------------------------------------------------------------------
# routers/planner.py
# ---------------------------------------------------------------------------


def test_pg_capture_customer_resolve_does_not_lose_the_note(pg_test_engine):
    """``_resolve_capture_customer``'s docstring: "Never raises — capture must
    not break if the phone_com module/tables are unavailable." On Postgres that
    promise was false whenever the read failed — ``create_task`` goes on to
    ``ensure_audit_table(db)`` and ``db.commit()``, which die with 25P02.

    This test drops the table to produce the abort, but do NOT read that as the
    production trigger: ``phone_com_calls`` is created unconditionally at boot and
    prod holds 878 rows. See the module docstring's trigger note.
    """
    from gdx_dispatch.routers.planner import _resolve_capture_customer

    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "phone_com_calls")
    db = Session()
    db.add(_CallerRow(id=1, v="the captured note"))

    resolved, norm = _resolve_capture_customer(
        db, call_id=str(uuid.uuid4()), phone="+16125551234"
    )
    assert resolved is None
    assert norm == "+16125551234", "normalization is pure and must still happen"

    db.commit()
    db.close()
    _committed(pg_test_engine, expect=1)


# ---------------------------------------------------------------------------
# tasks/planner_digest.py
# ---------------------------------------------------------------------------


def test_pg_digest_sender_lookup_does_not_poison_the_send(pg_test_engine):
    """The session is the digest task's own, but it is the CALLER's relative to
    this helper and the caller keeps using it — the return value goes straight
    into ``send_transactional_email(tenant_db=db, …)``, which writes the
    outbound-email log row. Falling back to SMTP was the harmless half.
    """
    from gdx_dispatch.tasks.planner_digest import _digest_sender_user_id

    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "outlook_accounts")
    db = Session()
    db.add(_CallerRow(id=1, v="the outbound email log row"))

    assert _digest_sender_user_id(db) is None

    db.commit()
    db.close()
    _committed(pg_test_engine, expect=1)


def test_pg_cold_lead_count_does_not_cost_the_whole_digest(pg_test_engine):
    """Worse blast radius than the sender lookup: this runs BEFORE the digest is
    rendered and sent, so on Postgres a failed read turned ``return 0`` into a
    digest that never went out at all.
    """
    from gdx_dispatch.tasks.planner_digest import _cold_lead_count

    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "phone_com_calls")
    db = Session()
    db.add(_CallerRow(id=1, v="the digest that still has to send"))

    assert _cold_lead_count(db) == 0

    db.commit()
    db.close()
    _committed(pg_test_engine, expect=1)


# ---------------------------------------------------------------------------
# Inline swallows — the half the AST recipe could not see
# ---------------------------------------------------------------------------
# The census enumerated NAMED HELPERS that return a degraded default. The class
# is wider: "a swallowed read on a session the caller keeps using" also lives
# inline inside endpoints, where there is no helper and no degraded return — just
# a `try` whose handler logs and falls through to a commit. Three such sites were
# found by the GDXA-158 audit, in the same two routers this change already
# edited, and all three were measured failing on PG 16.15 before being fixed.


def test_pg_job_number_alloc_read_does_not_block_the_closeout(pg_test_engine):
    """``closeout_job``'s return-visit numbering says in-code "the closeout is
    never blocked on numbering". The counter does run on its own session — but
    the CUSTOMER NAME it feeds in is read on the caller's, so a failed read there
    aborted the request's transaction and the ``db.add(child)`` after it died.
    The closeout was blocked on numbering, by the one read nobody filed under
    numbering.

    **This does not guard ``jobs.py``.** It imports ``contained_read`` itself
    and re-creates the two-session mix inline, so removing the wrap from
    ``closeout_job`` or ``spawn_return_visit`` leaves it green. It proves the
    mechanism those two sites rely on. The call sites are the test gap the
    module docstring names.
    """
    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "customers")
    db = Session()
    db.add(_CallerRow(id=1, v="the closeout's staged rows"))

    # Verbatim shape of jobs.py's guarded allocation: a read on the CALLER's
    # session inside a try that swallows, while the counter uses another session.
    from gdx_dispatch.core.database import contained_read as _cr

    assigned = None
    try:
        with _cr(db):
            cust = db.execute(
                text("SELECT name FROM customers WHERE id = :cid"), {"cid": "x"}
            ).first()
        assigned = cust[0] if cust else None
    except Exception:
        assigned = None

    assert assigned is None, "the numbering read still degrades to no name"
    db.commit()
    db.close()
    _committed(pg_test_engine, expect=1)


def test_pg_link_customer_keeps_the_link_when_the_backfill_cannot_run(pg_test_engine):
    """``planner.py::link_customer`` is the one site here that needed
    ``db.begin_nested()`` instead — it WRITES (mutates ``call.customer_id``), and
    core.database rule 2 requires the ORM's unit of work inside the savepoint so
    it knows what to un-stage.

    The bug: when the backfill read failed, the transaction aborted, the handler
    logged and fell through, and ``db.commit()`` on the next line raised 25P02.
    The user's customer link was LOST and the request 500'd. Measured on PG 16.15
    by the GDXA-158 audit: ``raised='InternalError' persisted_customer_id=None``.
    (The trigger is NOT "phone_com is off" — that table exists on every boot and
    prod is using it. See the module docstring.)

    The savepoint also required a second fix the audit caught: ``backfilled`` is a
    plain int and survives the rollback, so the handler now resets it to 0.
    Without that the 200 reported ``calls_backfilled: 1`` with zero rows written —
    a lying success the containment itself introduced.

    What this asserts is rule 3's early flush doing useful work: the caller's
    assignment happens BEFORE the block, so ``_take_snapshot`` writes it out
    before the savepoint exists — which is precisely why the link survives a
    backfill failure rather than rolling back with it.

    **What this test does not cover:** it pins the MECHANISM on Postgres by
    reproducing ``link_customer``'s shape inline, because ``structure.sql`` has
    neither ``phone_com_calls`` nor the capture columns on ``planner_tasks``.
    Removing the savepoint from ``link_customer`` is caught by the SQLite
    endpoint test below (the stamped call's link persists), but the 25P02 half,
    where the commit itself dies, is proven only here, inline.
    """
    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "phone_com_calls")
    db = Session()
    row = _CallerRow(id=1, v="the link the user asked for")
    db.add(row)

    # The shape: assign (the link), then a failing write-bearing block.
    try:
        with db.begin_nested():
            db.execute(text("SELECT customer_id FROM phone_com_calls LIMIT 1"))
    except Exception:
        pass  # link_customer's own handler logs and falls through

    db.commit()  # this raised 25P02 before the fix
    db.close()
    _committed(pg_test_engine, expect=1)


@pytest.fixture
def planner_client(tmp_path, monkeypatch):
    """The real ``link_customer`` endpoint over an ORM-built SQLite schema.

    ``structure.sql`` cannot carry this (no ``phone_com_calls``, no capture
    columns on ``planner_tasks``), so the schema comes from the ORM — the rule
    the ``create_all`` divergence already implies. SQLite cannot show the 25P02
    half, but the savepoint's ROLLBACK and what the handler reports after it are
    dialect-independent, and those are what this drives. The SAVEPOINT recipe is
    the same one ``sqlite_sessions`` below uses, for the same reason.
    """
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    import gdx_dispatch.models.tenant_models  # noqa: F401 — registers customers
    import gdx_dispatch.modules.phone_com.models  # noqa: F401 — phone_com_calls
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers import planner as planner_mod
    from gdx_dispatch.routers.auth import get_current_user

    engine = create_engine(f"sqlite:///{tmp_path}/p.db", future=True)

    @event.listens_for(engine, "connect")
    def _sqlite_no_implicit_begin(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_real_begin(conn):  # noqa: ANN001
        conn.exec_driver_sql("BEGIN")

    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    app = FastAPI()
    app.include_router(planner_mod.router)

    def _like_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()  # no commit — exactly what core.database.get_db does

    app.dependency_overrides[get_db] = _like_get_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "00000000-0000-0000-0000-000000000158",
        "tenant_id": "11111111-1111-1111-1111-111111111158",
        "role": "admin",
    }

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": "11111111-1111-1111-1111-111111111158"}
        return await call_next(request)

    yield TestClient(app), Session
    engine.dispose()


def test_link_customer_does_not_report_a_backfill_it_rolled_back(
    planner_client, monkeypatch
):
    """Containment must UN-COUNT what it un-does — driven through the endpoint.

    The bug the GDXA-158 audit found in this very change: ``backfilled`` and
    ``backfilled_calls`` are plain Python values, so they survive the savepoint
    rollback. Without the reset the 200 said ``calls_backfilled: 1`` and the
    audit row named a call whose link had just been rolled away — a lying
    success the containment itself introduced.

    The failure is placed AFTER the first call is stamped: the
    ``phone_com_call_id`` branch links the call and counts it, then the
    ``contact_phone`` branch's ``normalize_e164`` raises. So every one of these
    assertions has something to catch:

    - delete ``backfilled = 0`` → the response says 1
    - delete ``backfilled_calls = []`` → the audit row names the call
    - drop the savepoint → the call's ``customer_id`` persists
    - lose the link with the backfill → the task's ``customer_id`` is None
    """
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.models.tenant_models import PlannerTask
    from gdx_dispatch.modules.phone_com import customer_resolver
    from gdx_dispatch.modules.phone_com.models import PhoneComCall

    client, Session = planner_client
    cid = "33333333-3333-3333-3333-333333333158"
    with Session() as db:
        call = PhoneComCall(
            phone_com_call_id="pc-158", direction="in", from_number="6125551234"
        )
        task = PlannerTask(
            company_id="11111111-1111-1111-1111-111111111158",
            title="Captured call",
            created_by="00000000-0000-0000-0000-000000000158",
            phone_com_call_id="pc-158",
            contact_phone="6125551234",
        )
        db.add_all([call, task])
        db.commit()
        call_pk, task_id = call.id, task.id

    def _boom(*_a, **_k):
        raise RuntimeError("the backfill could not run")

    monkeypatch.setattr(customer_resolver, "normalize_e164", _boom)

    r = client.post(
        f"/api/planner/tasks/{task_id}/link-customer", json={"customer_id": cid}
    )

    assert r.status_code == 200, r.text
    assert r.json()["calls_backfilled"] == 0, (
        "reported a backfill the savepoint rolled back"
    )
    with Session() as db:
        assert db.get(PlannerTask, task_id).customer_id == cid, (
            "the link the user asked for was lost with the backfill"
        )
        assert db.get(PhoneComCall, call_pk).customer_id is None, (
            "the savepoint did not un-stage the call it had stamped"
        )
        audit = db.execute(
            select(AuditLog).where(AuditLog.action == "link_customer")
        ).scalar_one()
        assert audit.details["calls_backfilled"] == [], (
            "the audit row names a call link that never happened"
        )


# ---------------------------------------------------------------------------
# Containment must not cost the helpers their answers
# ---------------------------------------------------------------------------


def test_pg_the_helpers_still_answer_on_a_healthy_read(pg_test_engine):
    """A guard that only proves the degraded path would pass just as well if
    every helper were hard-wired to its default — and that would silently break
    job routing, closeout rates and the dispatch board with no transaction test
    noticing. So: with the real tables present, a real row comes back.

    Only ``_holding_area_id_by_name`` is exercised, because it is raw SQL over
    one column and needs no ORM fixture; the ORM sites' happy paths are already
    covered on SQLite in the default suite by test_jobs_display_state_serializer.py,
    test_labor.py, and serial/test_planner_capture.py
    (``test_capture_autolinks_customer_by_phone`` is the one for
    ``_resolve_capture_customer``).

    NOT covered anywhere else: ``tasks/planner_digest.py`` has no test file at
    all, so this file is the only guard on ``_digest_sender_user_id`` and
    ``_cold_lead_count`` — and it only exercises their DEGRADED paths. Their
    happy paths remain untested; called out rather than quietly implied.
    """
    from gdx_dispatch.routers.jobs import _holding_area_id_by_name

    Session = _sessions(pg_test_engine)
    lane = str(uuid.uuid4())
    with pg_test_engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS holding_areas CASCADE"))
        c.execute(text("CREATE TABLE holding_areas (id text PRIMARY KEY, name text)"))
        c.execute(
            text("INSERT INTO holding_areas (id, name) VALUES (:i, 'Ready to Schedule')"),
            {"i": lane},
        )
    db = Session()
    assert _holding_area_id_by_name(db, "Ready to Schedule") == lane
    assert _holding_area_id_by_name(db, "No Such Lane") is None
    db.close()


def test_pg_repeated_contained_reads_on_one_session_all_degrade(pg_test_engine):
    """``closeout_job`` hits two of these helpers on one session, and
    ``list_jobs`` calls ``_display_state_for_jobs`` once per request on a
    long-lived pooled connection. A savepoint that leaked would make the SECOND
    failure the fatal one, which a single-call test cannot see.
    """
    from gdx_dispatch.routers.jobs import (
        _holding_area_id_by_name,
        _resolve_technician_id,
    )

    Session = _sessions(pg_test_engine)
    _drop(pg_test_engine, "holding_areas")
    _drop(pg_test_engine, "technicians")
    db = Session()
    db.add(_CallerRow(id=1, v="survives N failed reads"))

    for _ in range(3):
        assert _holding_area_id_by_name(db, "Ready to Schedule") is None
        assert _resolve_technician_id(db, str(uuid.uuid4())) is None

    db.commit()
    db.close()
    _committed(pg_test_engine, expect=1)


# ---------------------------------------------------------------------------
# Both dialects: the savepoint must not flush or commit the caller's work
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_sessions(tmp_path):
    """A bare SQLite engine with no app tables — the helpers' reads all fail,
    which is the state this section wants.

    Copied from tests/test_plugin_consent_contained_read.py, including the
    SQLAlchemy SQLite-SAVEPOINT recipe: pysqlite's implicit BEGIN otherwise
    breaks nested-transaction rollback, so a SAVEPOINT release would wrongly
    persist and a containment test could pass because pysqlite is wrong rather
    than because the helper is right. Production is Postgres, which gets
    savepoints right natively.
    """
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)

    @event.listens_for(engine, "connect")
    def _sqlite_no_implicit_begin(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_real_begin(conn):  # noqa: ANN001
        conn.exec_driver_sql("BEGIN")

    yield _sessions(engine), engine
    engine.dispose()


def test_contained_read_does_not_flush_pending_work_at_these_sites(sqlite_sessions):
    """core.database rule 3, at this domain's sites rather than in the abstract.

    ``db.begin_nested()`` flushes on entry (``_take_snapshot``);
    ``contained_read`` opens its SAVEPOINT on the Connection under
    ``no_autoflush`` and writes nothing. Swap any of these to
    ``db.begin_nested()`` and ``len(db.new)`` drops to 0 here.

    Dialect-independent, so it runs in the default SQLite suite — one of the
    three tests in this file that are not Postgres-gated (the others are the
    ``link_customer`` endpoint test and the no-commit test below), and it guards the rule most likely to be "simplified" back to
    ``begin_nested()`` by someone who reads rule 2 and stops there.
    """
    from gdx_dispatch.routers.jobs import _holding_area_id_by_name
    from gdx_dispatch.routers.labor import _resolve_hourly_rate
    from gdx_dispatch.tasks.planner_digest import _digest_sender_user_id

    Session, _ = sqlite_sessions
    db = Session()
    db.add(_CallerRow(id=1, v="must stay pending"))

    _holding_area_id_by_name(db, "Ready to Schedule")
    _resolve_hourly_rate(db, str(uuid.uuid4()))
    _digest_sender_user_id(db)

    assert len(db.new) == 1, (
        "a contained read flushed the caller's pending work — see "
        "core.database.contained_read rule 3"
    )
    db.rollback()
    db.close()


def test_contained_read_does_not_commit_the_callers_pending_work(sqlite_sessions):
    """RELEASE SAVEPOINT must never become a commit.

    On SQLite, releasing a savepoint that is itself the outermost transaction
    boundary DOES commit (``core/audit.py`` documents why there is no
    ``commit=False`` variant for the same reason). A read helper that hardened
    the caller's half-built job would be far worse than the bug it fixes.

    The table EXISTS here, which is what gives this teeth: the read then
    SUCCEEDS, so the savepoint RELEASES rather than rolling back, and release is
    the only path on which a stray commit could happen. The caller flushes
    first — with nothing written, "another connection sees 0 rows" would be true
    either way and the assertion could not tell the two apart.
    """
    from gdx_dispatch.routers.jobs import _holding_area_id_by_name

    Session, engine = sqlite_sessions
    with engine.begin() as c:
        c.execute(text("CREATE TABLE holding_areas (id text PRIMARY KEY, name text)"))

    db = Session()
    db.add(_CallerRow(id=1, v="must not be committed by a read"))
    db.flush()

    assert _holding_area_id_by_name(db, "Ready to Schedule") is None

    assert db.execute(text("SELECT count(*) FROM gdxa158_caller_row")).scalar() == 1
    with engine.connect() as other:
        assert other.execute(
            text("SELECT count(*) FROM gdxa158_caller_row")
        ).scalar() == 0, "a contained read committed the caller's flushed row"
    db.rollback()
    db.close()
