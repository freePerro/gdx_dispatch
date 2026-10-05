"""``contained_read`` at the people-time call sites — GDXA-164.

The class is ``core.database.contained_read``'s, and GDXA-86 proved it:
a helper takes a session it does not own, reads inside a ``try``, and returns a
degraded value from the ``except``. On Postgres the failed statement has aborted
the whole transaction, so the degraded answer is a lie twice over — the caller
believes it got an answer, and the caller's uncommitted work is already dead.

``tests/test_contained_read.py`` pins the helper's own behaviour and the kernel
call sites. This file pins the **people-time** ones: hours, pay, the timesheet
and the time-off record. It is a separate file on purpose, so the two sets never
collide.

**Every test that can prove anything lives in the PG arm.** SQLite does not
poison a transaction on a failed statement, so pre-fix and post-fix are
byte-identical there — a green SQLite run is not evidence for a single assertion
below. The arm skips (green) with no reachable Postgres and fails under CI
(#440), which is exactly the gap that let this class ship.

Two assertion shapes are used, and the second is the one that matters:

1. *The caller's committed work survives.* A row is ``add()``ed before the
   helper runs and the caller commits after it; the row must be there. This is
   the data-loss consequence, and it is what ``_caller_survives`` checks on
   every call.
2. *The next read still answers.* Where a helper has a second read after the
   first, or a caller has one, the test asserts that later read returns REAL
   DATA. This is strictly stronger than "the session still answers SELECT 1",
   because on an aborted transaction it cannot pass by luck.

Failures are injected by renaming a real table out from under one statement —
never by monkeypatching the session. A mock proves which arguments were passed,
never that the transaction survived.

``payroll_entries`` needs no rename because it is missing from this fixture
already — but be honest about WHY, because the first draft of this file got it
wrong. It is NOT "absent from the ``TenantBase``-scoped dump": ``PayrollEntry``
is declared on ``TenantBase``. It is missing because ``tests/fixtures/
structure.sql`` is 93 tables behind the ORM, and prod has the table (checked
2026-09-27, PG 16.13). So that injected failure is a FIXTURE ARTIFACT, not the
production failure mode the handler's own comment implies — the same trap
``contained_read``'s docstring had to correct once for ``tenant_settings``. It
still exercises the containment exactly as a real UndefinedTable would, which is
all these tests need it for.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Column, Integer, String, text
from sqlalchemy.orm import declarative_base, sessionmaker

_Base = declarative_base()

TENANT = "11111111-1111-4111-8111-111111111111"


class _Row(_Base):
    """Stands in for whatever the caller had staged when the helper ran."""

    __tablename__ = "gdxa164_row"
    id = Column(Integer, primary_key=True)
    v = Column(String)


def _sessionmaker(engine):
    # autoflush=False mirrors core.database.SessionLocal, which is what every
    # session that reaches these call sites actually is.
    _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _rename_away(engine, *tables: str) -> None:
    with engine.begin() as c:
        for t in tables:
            c.execute(text(f"ALTER TABLE {t} RENAME TO {t}_gdxa164_gone"))


def _caller_survives(pg_test_engine, tables, call):
    """Break ``tables``, run ``call`` on a session with pending work, and assert
    the helper degraded AND the caller could still commit.

    Returns whatever ``call`` returned, so a test can add the stronger
    "the next read still answers" assertion on top.
    """
    Session = _sessionmaker(pg_test_engine)
    if tables:
        _rename_away(pg_test_engine, *tables)

    db = Session()
    db.add(_Row(id=1, v="the caller's pending work"))
    result = call(db)
    db.commit()
    db.close()

    with pg_test_engine.connect() as other:
        surviving = other.execute(text("SELECT count(*) FROM gdxa164_row")).scalar()
    assert surviving == 1, (
        f"a failed read of {tables or 'an absent table'} cost the caller its row"
    )
    return result


def _seed_user(engine, user_id: str, name: str) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO users (id, company_id, email, name, full_name) "
                "VALUES (:id, :tid, :email, :name, :name)"
            ),
            {"id": user_id, "tid": TENANT, "email": f"{name}@example.test", "name": name},
        )


# ---------------------------------------------------------------------------
# modules/payroll/service.py — effective_labor_cost. Money.
# ---------------------------------------------------------------------------


def test_pg_labor_cost_falls_back_to_the_estimated_rate_after_the_true_read_fails(
    pg_test_engine,
):
    """The strongest assertion in this file, because it cannot pass by luck.

    ``effective_labor_cost`` reads ``payroll_entries`` for the true rate, and on
    failure falls back to ``technicians.hourly_rate``. ``payroll_entries`` is
    absent from the PG fixture (a stale-fixture artifact — see the module
    docstring; prod has the table), so read 1 raises a real ``UndefinedTable``.

    Without the savepoint, that abort makes read 2 fail too: source collapses to
    ``"none"`` and the entry is costed at 0.00. With it, read 2 answers and the
    estimated rate comes back. So this asserts a real number produced *after* a
    failed statement, which an aborted Postgres transaction physically cannot do.
    """
    from gdx_dispatch.modules.payroll import effective_labor_cost

    tech = str(uuid.uuid4())
    _seed_user(pg_test_engine, tech, "estimated-rate-tech")
    with pg_test_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO technicians (id, company_id, user_id, name, hourly_rate) "
                "VALUES (:id, :tid, :uid, 'Rate Tech', 40)"
            ),
            {"id": str(uuid.uuid4()), "tid": TENANT, "uid": tech},
        )

    lc = _caller_survives(
        pg_test_engine,
        [],  # payroll_entries is already absent — a genuine UndefinedTable
        lambda db: effective_labor_cost(db, tech_user_id=tech, hours=2, when=datetime.now(UTC)),
    )

    assert lc.source == "estimated", (
        "the technicians read after the failed payroll_entries read came back empty — "
        "the transaction was aborted, so the true-rate swallow ate the fallback too"
    )
    assert float(lc.estimated_cost) == pytest.approx(80.0)


def test_pg_labor_cost_with_both_rate_reads_broken_still_lets_the_caller_commit(
    pg_test_engine,
):
    """Both reads down: the documented degradation (source "none") is fine — the
    caller losing its work is not."""
    from gdx_dispatch.modules.payroll import effective_labor_cost

    lc = _caller_survives(
        pg_test_engine,
        ["technicians"],  # payroll_entries is absent too
        lambda db: effective_labor_cost(db, tech_user_id=str(uuid.uuid4()), hours=2),
    )
    assert lc.source == "none"


# ---------------------------------------------------------------------------
# core/timesheet_hours.py — the one hours authority
# ---------------------------------------------------------------------------


def _entry(tech_id: str):
    """An unpersisted TimeclockEntry, which is all these helpers read off it.

    Deliberately NOT added to the session: these tests are about what a failed
    BREAK read does, and staging a second row would muddy the one assertion
    ``_caller_survives`` makes.
    """
    from gdx_dispatch.models.tenant_models import TimeclockEntry

    now = datetime.now(UTC)
    return TimeclockEntry(
        id=str(uuid.uuid4()),
        tenant_id=TENANT,
        technician_id=tech_id,
        clock_in_at=(now - timedelta(hours=3)).isoformat(),
        clock_out_at=None,
    )


def test_pg_break_minutes_by_entry_cannot_cost_a_display_caller_its_session(pg_test_engine):
    """The lenient default, for screens only (status, today's hours): a failed
    read answers ``{}`` and the caller's session is still usable.

    ``build_timesheet`` does NOT take this path: ``{}`` there meant gross hours
    mailed to payroll (GDXA-197), so it passes ``strict=True`` — the test below.
    """
    from gdx_dispatch.core.timesheet_hours import break_minutes_by_entry

    tech = str(uuid.uuid4())
    out = _caller_survives(
        pg_test_engine,
        ["timeclock_breaks_router"],
        lambda db: break_minutes_by_entry(db, TENANT, [_entry(tech)]),
    )
    assert out == {}


def test_pg_a_strict_break_read_refuses_instead_of_answering_no_breaks(pg_test_engine):
    """What ``build_timesheet`` gets: the error, on a session that still
    answers and still commits. The send paths turn it into a refusal
    (``test_an_unreadable_break_table_blocks_the_send`` and its beat twin)."""
    from sqlalchemy.exc import SQLAlchemyError

    from gdx_dispatch.core.timesheet_hours import break_minutes_by_entry

    entry = _entry(str(uuid.uuid4()))
    seen = {}

    def _call(db):
        with pytest.raises(SQLAlchemyError):
            break_minutes_by_entry(db, TENANT, [entry], strict=True)
        seen["after"] = db.execute(text("SELECT 1")).scalar()

    _caller_survives(pg_test_engine, ["timeclock_breaks_router"], _call)
    assert seen["after"] == 1


def test_pg_open_break_in_shift_cannot_cost_the_techs_punch(pg_test_engine):
    from gdx_dispatch.core.timesheet_hours import open_break_in_shift

    tech = str(uuid.uuid4())
    entry = _entry(tech)
    out = _caller_survives(
        pg_test_engine,
        ["timeclock_breaks_router"],
        lambda db: open_break_in_shift(db, TENANT, entry),
    )
    assert out is None


def test_pg_break_minutes_started_on_cannot_cost_the_callers_work(pg_test_engine):
    from gdx_dispatch.core.timesheet_hours import break_minutes_started_on

    tech = str(uuid.uuid4())
    out = _caller_survives(
        pg_test_engine,
        ["timeclock_breaks_router"],
        lambda db: break_minutes_started_on(
            db, TENANT, tech, [_entry(tech)], date.today().isoformat()
        ),
    )
    assert out == 0


# ---------------------------------------------------------------------------
# routers/payroll.py — the payroll summary
# ---------------------------------------------------------------------------


def test_pg_a_failed_hours_read_does_not_zero_every_techs_commission_rate(pg_test_engine):
    """The hours read, then a rate read, on one session — and what that is worth.

    Break the hours read and, without containment, every rate lookup that
    follows fails too, while the log says only that one table was unreadable.

    Be careful about the money framing, because an earlier draft of this
    docstring asserted as live fact the very thing
    ``routers/payroll.py::_fetch_active_rate`` retracts: that the payroll
    summary would report "$0.00 commission for the whole crew". **It would
    not.** ``_build_summary_rows`` calls ``_fetch_tech_revenue`` between these
    two reads, and that raises ``RevenueBasisUnavailable`` unconditionally
    (``j.assigned_tech_id`` is on no database), so the real production outcome
    is a 503 and ``_fetch_active_rate`` never runs at all.

    This test therefore composes the two helpers directly rather than calling
    ``_build_summary_rows``, and it is a guard on the CONTAINMENT, not evidence
    of a live money defect. It is worth keeping for the day M27's revenue basis
    is fixed and this ordering goes live. The assertion is that the rate still
    resolves to a REAL row after the hours read failed.
    """
    from gdx_dispatch.routers.payroll import _fetch_active_rate, _fetch_tech_hours

    tech = str(uuid.uuid4())
    _seed_user(pg_test_engine, tech, "rate-holder")
    with pg_test_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO tech_commission_rates "
                "(id, company_id, tech_id, rate_type, rate_value, active, "
                " effective_from, created_at, updated_at) "
                "VALUES (:id, :tid, :tech, 'percent_revenue', 10, true, :eff, :now, :now)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": TENANT,
                "tech": tech,
                "eff": date.today(),
                "now": datetime.now(UTC),
            },
        )

    captured = {}

    def _call(db):
        captured["hours"] = _fetch_tech_hours(
            db, tenant_id=TENANT, start=date.today(), end=date.today()
        )
        rate = _fetch_active_rate(db, tenant_id=TENANT, tech_id=tech)
        # Read the value here, while the row is still attached — the caller's
        # commit inside `_caller_survives` expires it.
        captured["rate_value"] = None if rate is None else float(rate.rate_value)
        return captured

    _caller_survives(pg_test_engine, ["time_entries"], _call)

    assert captured["hours"] == {}  # the documented degrade
    assert captured["rate_value"] == 10.0, (
        "the commission rate read after the failed hours read came back empty — "
        "an aborted transaction silently zeroed a rate the caller did query. "
        "(Not a live payroll number today: see this test's docstring — the real "
        "summary 503s before reaching here.)"
    )


def test_pg_a_failed_rate_read_cannot_cost_the_caller_its_work(pg_test_engine):
    """``_fetch_active_rate``'s own containment, which the test above cannot
    reach — there the rate read succeeds, which is the whole point of it."""
    from gdx_dispatch.routers.payroll import _fetch_active_rate

    rate = _caller_survives(
        pg_test_engine,
        ["tech_commission_rates"],
        lambda db: _fetch_active_rate(db, tenant_id=TENANT, tech_id=str(uuid.uuid4())),
    )
    assert rate is None  # the documented degrade


def test_pg_fetch_tech_names_loop_failure_cannot_cost_the_caller(pg_test_engine):
    """The per-id loop. Without containment the first unreadable id aborts the
    transaction, every remaining id comes back unnamed, and `_build_summary_rows`
    goes on to read rates on a dead session."""
    from gdx_dispatch.routers.payroll import _fetch_tech_names

    names = _caller_survives(
        pg_test_engine,
        ["users"],
        lambda db: _fetch_tech_names(db, tenant_id=TENANT, tech_ids=[str(uuid.uuid4())]),
    )
    assert names == {}


def test_pg_the_dead_in_ids_probe_is_a_false_positive_not_a_missing_savepoint(
    pg_test_engine,
):
    """NOT a containment guard — it pins the verdict that there is nothing here
    to contain, so the next sweep does not "fix" it.

    ``_fetch_tech_names`` opens with a `SELECT ... WHERE id IN :ids` whose `:ids`
    is never bound. SQLAlchemy raises ``StatementError`` while processing
    parameters, client-side: measured on PG 16, the statement never reaches the
    server and the transaction is NOT aborted. So the swallow below it is
    harmless to the caller's transaction, unlike every other site in this file.

    This test passes with the savepoints removed — deliberately, and it is the
    only one here that does. What it asserts is that the per-id loop underneath
    still returns a real name despite that raise, which is the observable
    consequence of the failure being client-side.
    """
    from gdx_dispatch.routers.payroll import _fetch_tech_names

    good = str(uuid.uuid4())
    _seed_user(pg_test_engine, good, "named-tech")

    names = _caller_survives(
        pg_test_engine,
        [],
        lambda db: _fetch_tech_names(db, tenant_id=TENANT, tech_ids=[good]),
    )
    assert names.get(good) == "named-tech"


# ---------------------------------------------------------------------------
# routers/timeclock.py — the timesheet export and the roster
# ---------------------------------------------------------------------------


def test_pg_tech_names_cannot_strand_the_timesheet_export(pg_test_engine):
    """``_tech_names`` swallows its own failure and ``_export_context`` goes on
    to build the whole timesheet on the same session.

    That ordering is why the containment lives INSIDE ``_tech_names`` rather
    than around the call to it — see ``contained_read`` rule 5. This asserts the
    caller can still work afterwards, which is what rule 5 is protecting.
    """
    from gdx_dispatch.routers.timeclock import _tech_names

    out = _caller_survives(
        pg_test_engine,
        ["users"],
        lambda db: _tech_names(db, TENANT, {str(uuid.uuid4())}),
    )
    assert out == {}


def test_pg_the_roster_survives_an_unreadable_users_table(pg_test_engine):
    """The roster's OWN savepoint, driven through the real route function.

    `timeclock_roster` reads the clocked ids, then the active-staff labels
    (swallowed), then `_tech_names` — all on one session. Rename `users` and
    only the last two fail. Without the savepoint on the active-staff read the
    transaction is aborted from that point on, and `_caller_survives` catches it
    where it counts: the caller's committed row is gone.

    Called directly rather than over HTTP so the failure is the renamed table
    and nothing else; the role gate is satisfied with an admin dict.

    A clocked entry is SEEDED first, and that is load-bearing rather than
    scene-setting. The roster builds its rows from `clocked_ids`, so with no
    timeclock row the handler returns `[]`, `_tech_names` exits before issuing
    a query, and ``all(i.name is None for i in items)`` passes vacuously over
    an empty list — which is what an earlier version of this test did. Seeding
    one row is what makes the assertion able to fail.
    """
    from types import SimpleNamespace

    from gdx_dispatch.routers.timeclock import timeclock_roster

    tech_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    with pg_test_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO timeclock_entries_router "
                "(id, tenant_id, technician_id, clock_in_at, entry_type, "
                " created_at, updated_at) "
                "VALUES (:id, :tid, :tech, :in_at, 'work', :now, :now)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": TENANT,
                "tech": tech_id,
                "in_at": (now - timedelta(hours=2)).isoformat(),
                "now": now.isoformat(),
            },
        )

    items = _caller_survives(
        pg_test_engine,
        ["users"],
        lambda db: timeclock_roster(request=SimpleNamespace(
            state=SimpleNamespace(tenant={"id": TENANT})
        ), current_user={"sub": str(uuid.uuid4()), "role": "admin"}, db=db),
    )
    # Every label source is down, so the roster is unnamed — the degradation
    # this handler is willing to accept. Losing the caller's row is not.
    assert items, "nothing seeded — the name assertion below would be vacuous"
    assert {i.technician_id for i in items} == {tech_id}
    assert all(i.name is None for i in items)


# `_export_context`'s own id read has NO guard here, and that is a stated gap
# rather than an oversight. Two things block one:
#
#   1. It cannot be isolated by a schema injection. The id read and
#      `build_timesheet` two statements later read the SAME table, and
#      `build_timesheet` deliberately does not contain its own failure (it
#      refuses with 503 rather than emit a zero-hour payroll file). So anything
#      that breaks one breaks the other and the 503 is raised either way, with
#      or without the savepoint.
#   2. `_export_context` cannot be called against this fixture at all. It opens
#      with `db.query(AppSettings).first()`, and `tests/fixtures/structure.sql`
#      is 27 columns behind the `AppSettings` model — including every
#      `pay_period_*`, `payroll_autosend_*`, `holiday_calendar` and
#      `time_off_*` column. That read raises UndefinedColumn before any of this
#      change's code runs. (`users` is 3 columns behind too, which is why
#      `core.time_off.user_names`' `select(User)` cannot be used as a probe
#      below.) Measured 2026-09-27; reported, not worked around.
#
# What that containment buys is a per-statement failure — a 57014 timeout on
# the DISTINCT scan, proven contained in `test_contained_read.py`. The
# `_tech_names` half of the same function IS guarded, by the test above.


# ---------------------------------------------------------------------------
# tasks/payroll_timesheet.py — the beat that mails the closed period
# ---------------------------------------------------------------------------


def test_pg_audit_probe_failure_does_not_take_the_whole_send_run_with_it(pg_test_engine):
    """``_audit_says`` reads ``audit_logs`` on the task's own long-lived session.

    Its ``return True`` default ("claim it has been sent") is deliberate. What
    was not deliberate is that on Postgres the same abort killed ``_names``,
    ``build_timesheet`` and the ``_audit`` row that records what the run
    decided. This asserts ``_names`` still returns REAL names afterwards.
    """
    from gdx_dispatch.core.pay_periods import PayPeriod
    from gdx_dispatch.tasks.payroll_timesheet import _audit_says, _names

    who = str(uuid.uuid4())
    _seed_user(pg_test_engine, who, "payroll-person")
    period = PayPeriod(date(2026, 9, 1), date(2026, 9, 15))
    captured = {}

    def _call(db):
        captured["says"] = _audit_says(db, TENANT, "timesheet_sent", period)
        captured["names"] = _names(db, TENANT)
        return captured

    _caller_survives(pg_test_engine, ["audit_logs"], _call)

    assert captured["says"] is True  # the documented, deliberate default
    assert captured["names"].get(who) == "payroll-person", (
        "the name read after the failed audit probe came back empty — the run "
        "could not even have written down that it bailed"
    )


def test_pg_the_name_read_in_the_send_task_has_its_own_containment(pg_test_engine):
    """`_names` is the second savepoint in that task, and the test above cannot
    reach it — there `users` is intact and `_names` succeeds, which is the whole
    point of that assertion. Break `users` instead and the caller must survive."""
    from gdx_dispatch.tasks.payroll_timesheet import _names

    out = _caller_survives(
        pg_test_engine,
        ["users"],
        lambda db: _names(db, TENANT),
    )
    assert out == {}


# ---------------------------------------------------------------------------
# routers/performance.py — seven reads in a row, one session
# ---------------------------------------------------------------------------


def test_pg_one_unreadable_table_does_not_zero_every_other_performance_stat(
    pg_test_engine,
):
    """``_build_user_stats`` runs seven swallowed reads back to back.

    Break the first (``jobs``) and, without containment, the six after it fail
    too: the page shows a row of noughts for every user with nothing but
    ``log.debug`` to say why. This seeds a real ``planner_tasks`` row and asserts
    it is still counted after the ``jobs`` read failed — a number that an
    aborted Postgres transaction physically cannot produce.
    """
    from gdx_dispatch.routers.performance import REASON_READ_FAILED, _build_user_stats

    who = str(uuid.uuid4())
    _seed_user(pg_test_engine, who, "busy-person")
    with pg_test_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO planner_tasks "
                "(id, company_id, assigned_to, created_by, title, status, priority) "
                "VALUES (:id, :tid, :who, :who, 'done thing', 'done', 'normal')"
            ),
            {"id": str(uuid.uuid4()), "tid": TENANT, "who": who},
        )

    # No hours map: hours are not what this test is about (GDXA-174 reads them
    # through the hours authority, outside this function's swallowed reads).
    stats, unavailable = _caller_survives(
        pg_test_engine,
        ["jobs"],
        lambda db: _build_user_stats(db, TENANT, who, None, None),
    )

    # The read that really did fail: since GDXA-174 a failed read leaves the
    # stat None and names why, rather than reporting a plausible 0.
    assert stats["jobs_completed"] is None
    assert unavailable["jobs_completed"] == REASON_READ_FAILED
    assert stats["tasks_completed"] == 1, (
        "a later stat came back zero after the jobs read failed — one unreadable "
        "table silently zeroed the whole performance page"
    )


# ---------------------------------------------------------------------------
# routers/time_off.py + core/time_off.py — the sibling sweep's finds
# ---------------------------------------------------------------------------


def test_pg_a_missing_settings_row_does_not_kill_the_time_off_list(pg_test_engine):
    """Every ``_settings`` caller keeps reading afterwards — the request list,
    ``person_schedule``, the approve path."""
    from gdx_dispatch.routers.time_off import _settings

    who = str(uuid.uuid4())
    _seed_user(pg_test_engine, who, "time-off-person")
    captured = {}

    def _call(db):
        captured["settings"] = _settings(db)
        # A raw probe rather than `core.time_off.user_names`, deliberately:
        # that helper does `select(User)`, and the PG fixture's structure.sql
        # is behind the User model (no `shift_start`/`shift_end`/`workdays`
        # columns), so it raises here for a reason that has nothing to do with
        # this test. Recorded rather than worked around silently.
        captured["name"] = db.execute(
            text("SELECT name FROM users WHERE id = :id"), {"id": who}
        ).scalar()
        return captured

    # Renaming makes the failure an UndefinedTable. Without the rename this read
    # would fail anyway, with UndefinedColumn — structure.sql is 27 columns
    # behind the AppSettings model (see the note above the payroll_timesheet
    # section). The rename is used so the injected failure is the one the test
    # names, not a fixture artifact standing in for it.
    _caller_survives(pg_test_engine, ["app_settings"], _call)

    assert captured["settings"] is None  # the documented degrade
    assert captured["name"] == "time-off-person", (
        "the read after the failed settings read came back empty — the request "
        "list, person_schedule and the approve path all run on this session"
    )


def test_pg_user_names_no_longer_discards_the_callers_pending_work(pg_test_engine):
    """This one is about the FIX, not just the class.

    ``user_names`` used to clear the aborted transaction with ``db.rollback()``.
    That was safe for FIVE of its six callers, each of which has run its own
    ``db.commit()`` a few lines earlier (``routers/time_off.py`` 428/484/519/
    550/600; the commit is several lines up, not immediately above — a
    ``db.refresh`` sits between) — nothing pending, nothing to discard. The
    sixth,
    ``list_requests`` at :331, is a pure GET that never commits at all: it had
    just read ``rows``, and the rollback expired every one of them. So the old
    handler was already paying on the read path, it was one new caller away from
    silent data loss, and it expires every object the caller holds besides
    (``contained_read`` rule 4). Say five, not six — the call site's own comment
    in ``core/time_off.py`` says five, and a second copy of a fact is how the
    count in ``contained_read``'s docstring went stale twice.

    ``_caller_survives`` stages a row and commits after: on the rollback version
    that row is gone. This test therefore fails on the OLD code with data loss,
    not merely with a 25P02.
    """
    from gdx_dispatch.core.time_off import user_names

    out = _caller_survives(
        pg_test_engine,
        ["users"],
        lambda db: user_names(db, {str(uuid.uuid4())}),
    )
    assert out == {}


# ---------------------------------------------------------------------------
# strict=True — a contained read that feeds a WRITE guard must refuse, not
# degrade. Swallowing there turns a fail-closed guard into a fail-open one.
# ---------------------------------------------------------------------------


def test_pg_a_strict_open_break_read_refuses_instead_of_answering_none(pg_test_engine):
    """``start_break`` asks "is a break already open?" before writing one. A
    None from a failed read would let a second open break through, so the
    writer passes ``strict=True``: the error comes back, ``start_break``'s
    handler turns it into a 500 and writes nothing — and the session is still
    usable, because the failure was contained before it was re-raised."""
    from sqlalchemy.exc import SQLAlchemyError

    from gdx_dispatch.core.timesheet_hours import open_break_in_shift

    entry = _entry(str(uuid.uuid4()))
    seen = {}

    def _call(db):
        with pytest.raises(SQLAlchemyError):
            open_break_in_shift(db, TENANT, entry, strict=True)
        seen["after"] = db.execute(text("SELECT 1")).scalar()

    _caller_survives(pg_test_engine, ["timeclock_breaks_router"], _call)
    assert seen["after"] == 1


def test_pg_a_strict_settings_read_refuses_the_time_off_write(pg_test_engine):
    """The four time-off writers compute hours and accrual from the settings.
    Computing them from defaults because the read failed records a wrong
    number silently, so they pass ``strict=True`` and get a 503 instead —
    on a session that still commits."""
    from fastapi import HTTPException

    from gdx_dispatch.routers.time_off import _settings

    seen = {}

    def _call(db):
        with pytest.raises(HTTPException) as exc:
            _settings(db, strict=True)
        seen["status"] = exc.value.status_code
        seen["after"] = db.execute(text("SELECT 1")).scalar()

    _caller_survives(pg_test_engine, ["app_settings"], _call)
    assert seen["status"] == 503
    assert seen["after"] == 1
