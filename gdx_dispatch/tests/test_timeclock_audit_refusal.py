"""What every timeclock write does when `audit_logs` itself refuses the row.

GDXA-97, the deferred instances of GDXA-44/GDXA-58. Six sites in
`routers/timeclock.py` wrote their audit row AFTER committing the payroll
change, and none of them survived a refusal:

* Four answered **HTTP 500 with the write already committed** — clock-in,
  clock-out, `POST /entries`, `PATCH /entries/{id}`. Measured 2026-09-27 on the
  pre-fix code with the trigger below. That is the "and then the user did it
  twice" failure named in `audit_best_effort`'s own docstring, landing on
  payroll data: a dispatcher who is told the manual entry failed keys it again,
  and the period exports double-paid hours.
* `_auto_close_stale_shift` swallowed the refusal and promised in a comment
  that "audit failure must not block the close-out itself" — which it did not
  deliver. A refused flush deactivates the Session, so the caller's next
  `db.commit()` raised `PendingRollbackError` into `post_clock_in`'s
  `except SQLAlchemyError`: 500, the stale shift still open, and the technician
  unable to start their day (measured; `stale_closed=None`).
* `POST /pay-period/send` swallowed it and returned 200 `"sent": true` with the
  mail gone and **no audit row at all** — a payroll email that left the
  building with nothing recording it.

The refusal is a real `BEFORE INSERT ON audit_logs` trigger, not a patched
function, because the defect is what a failed *flush* does to the Session — the
same mechanism `tests/test_timesheet_export.py` and
`tests/test_audit_best_effort.py` use.

The database is a FILE, not `:memory:` with a StaticPool. Every assertion here
is about what SURVIVED A COMMIT, and one shared connection cannot tell a
committed row from a pending one — the reader sessions below open their own
connection, so "the row is there" means "the row is durable".

The six are NOT given the same helper, and the file is organised by that split:
the four punch/entry sites commit first, so they get `audit_best_effort`;
`_auto_close_stale_shift` is still staged when it audits, so it gets
`audit_or_rollback` and refuses atomically; the send commits its delivery row
and then audits best-effort.

Which arm is the falsifier for which claim, said plainly:

* The four status-code guards go red on the pre-fix code on SQLite. They are
  the defect.
* `test_a_refused_audit_rolls_the_auto_close_back_atomically` asserts a 500,
  which the pre-fix code ALSO returned — so what makes it red pre-fix is the
  detail string ("Clock-in failed", no mention of the audit) rather than the
  status. Said out loud because a guard whose headline assertion passes on the
  broken code is worth nobody's trust.
* `test_the_delivery_row_survives_a_refused_audit` does NOT go red on SQLite
  pre-fix: `_record_outbound` stages its row inside `begin_nested()`, and
  releasing an outermost SAVEPOINT commits on SQLite (see `audit_best_effort`'s
  docstring, note 3). On Postgres RELEASE never commits, so pre-fix that row was
  discarded at session close — which is why the PG arm at the bottom exists and
  is where that half is proven. Production is Postgres.
* The clean-path `..._is_audited` tests are new too, and they are not padding:
  `audit_best_effort` swallows, so a mistyped action or a dropped argument at
  any of these six sites would now be invisible. Nothing in the suite asserted
  that `timeclock_clock_in`, `timeclock_clock_out`, `timeclock_entry_created`
  or `timeclock_entry_updated` land at all before this file.
"""
from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import TenantBase, ensure_audit_table
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import AppSettings, OutboundEmail, TimeclockEntry
from gdx_dispatch.routers import timeclock as timeclock_module
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.timeclock import MAX_SHIFT_HOURS
from gdx_dispatch.routers.timeclock import router as timeclock_router

TENANT = "tenant-test"
TECH = "tech-1"
OFFICE = "office-1"
TZ = "America/Chicago"
RANGE = {"start": "2026-08-10", "end": "2026-08-23"}

#: The transport, NOT `send_transactional_email`. The seam has to sit BELOW
#: `_record_outbound` so the real `outbound_emails` row is really staged on the
#: handler's session — patching the whole send function is what made a first
#: probe report `outbound_emails=0` on the happy path.
SMTP_TARGET = "gdx_dispatch.core.transactional_email._try_smtp"

REFUSE_TRIGGER = (
    "CREATE TRIGGER audit_logs_refuse_insert BEFORE INSERT ON audit_logs "
    "BEGIN SELECT RAISE(ABORT, 'audit storage refuses this row'); END;"
)


@contextmanager
def _timeclock_app(tmp_path, *, refuse_audit: bool):
    """The timeclock router on a throwaway on-disk SQLite database.

    Yields `(TestClient, SessionLocal)`. Reader sessions get their own
    connection, so they see committed state only.
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path}/tc.db", future=True)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    AppSettings.__table__.create(bind=engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = SessionLocal()
    setup.execute(text(
        """CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key))"""
    ))
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants"
        " (id, company_id, module_key, granted_at, created_at)"
        " VALUES ('g2', :t, 'timeclock', datetime('now'), datetime('now'))"
    ), {"t": TENANT})
    setup.add(AppSettings(
        company_name="Garage Door Xperts", address="", phone="", email="", logo="",
        timezone=TZ, enabled_modules=[], notification_preferences={}, integrations={},
        pay_period_cadence="biweekly", pay_period_anchor_start=date(2026, 8, 10),
        pay_period_pay_lag_days=5,
        payroll_recipient_emails="bookkeeper@example.com",
    ))
    setup.commit()
    setup.close()

    if refuse_audit:
        # `audit_logs` is created lazily by `ensure_audit_table`, so the table
        # has to exist before a trigger can be hung on it.
        with SessionLocal() as boot:
            ensure_audit_table(boot)
        with engine.begin() as conn:
            conn.execute(text(REFUSE_TRIGGER))

    def _override_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(timeclock_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": OFFICE, "sub": OFFICE, "role": "dispatcher", "tenant_id": TENANT,
    }
    try:
        yield TestClient(app, raise_server_exceptions=True), SessionLocal
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture()
def refusing(tmp_path) -> Generator[tuple[TestClient, object], None, None]:
    """A client whose every audit write is refused by the database itself."""
    with _timeclock_app(tmp_path, refuse_audit=True) as parts:
        yield parts


@pytest.fixture()
def clean(tmp_path) -> Generator[tuple[TestClient, object], None, None]:
    with _timeclock_app(tmp_path, refuse_audit=False) as parts:
        yield parts


# ---------------------------------------------------------------------------
# Reading committed state
# ---------------------------------------------------------------------------

def _entries(SessionLocal) -> list[tuple]:
    with SessionLocal() as s:
        return s.execute(text(
            "SELECT id, clock_out_at, minutes, notes FROM timeclock_entries_router"
            " ORDER BY created_at, id"
        )).fetchall()


def _audit_actions(SessionLocal) -> list[str]:
    with SessionLocal() as s:
        return [
            r[0] for r in s.execute(text(
                "SELECT action FROM audit_logs ORDER BY created_at, id"
            )).fetchall()
        ]


def _outbound(SessionLocal) -> list[tuple]:
    with SessionLocal() as s:
        return s.execute(text(
            "SELECT kind, to_email, status FROM outbound_emails"
        )).fetchall()


def _seed_shift(SessionLocal, *, entry_id, clock_in, clock_out=None, minutes=480,
                notes=None, tech=TECH):
    with SessionLocal() as s:
        s.add(TimeclockEntry(
            id=entry_id, tenant_id=TENANT, technician_id=tech,
            clock_in_at=clock_in, clock_out_at=clock_out, minutes=minutes,
            notes=notes, entry_type="clock", created_at=clock_in, updated_at=clock_in,
        ))
        s.commit()


def _manual_payload(**kw) -> dict:
    return {
        "technician_id": TECH,
        "clock_in_at": "2026-08-17T13:00:00+00:00",
        "clock_out_at": "2026-08-17T22:00:00+00:00",
        "notes": "keyed by the office",
        **kw,
    }


# ---------------------------------------------------------------------------
# The four that answered 500 with the payroll write already committed
# ---------------------------------------------------------------------------

def test_a_refused_audit_row_does_not_fail_a_clock_in(refusing, caplog):
    """201 with the shift on the clock, not 500 with the shift on the clock.

    A tech told "Clock-in failed" punches again, and the day carries two open
    shifts for one person.
    """
    tc, SessionLocal = refusing

    with caplog.at_level(logging.ERROR, logger="gdx_dispatch.core.audit"):
        r = tc.post("/api/timeclock/clock-in", json={"technician_id": TECH})

    assert r.status_code == 201, r.text
    rows = _entries(SessionLocal)
    assert len(rows) == 1, "the shift is durable either way — that was never the question"
    assert r.json()["id"] == rows[0][0], "the caller can reconcile what it created"
    assert _audit_actions(SessionLocal) == [], (
        "the trigger is the point; if the row landed this test proves nothing"
    )
    # The helper's contract: the ERROR is the only record that it happened.
    assert any("audit_best_effort_failed" in m for m in caplog.messages), caplog.messages


def test_a_refused_audit_row_does_not_fail_a_clock_out(refusing):
    """The hours are already committed when the audit runs, so 500 is a lie —
    and an out-punch that reports failure gets pressed twice."""
    tc, SessionLocal = refusing
    _seed_shift(SessionLocal, entry_id="e-open",
                clock_in=datetime.now(UTC).isoformat(), clock_out=None, minutes=None)

    r = tc.post("/api/timeclock/clock-out", json={"technician_id": TECH})

    assert r.status_code == 200, r.text
    (row_id, clock_out_at, minutes, _notes), = _entries(SessionLocal)
    assert row_id == "e-open"
    assert clock_out_at is not None, "the clock-out committed"
    assert minutes is not None and minutes >= 0
    assert r.json()["clock_out_at"] == clock_out_at
    assert _audit_actions(SessionLocal) == []


def test_a_refused_audit_row_does_not_fail_a_manual_entry(refusing):
    """The live one. 500 here meant the dispatcher re-keyed a row that had
    already saved, and `POST /entries` has no idempotency key to catch it — so
    the guard is that the office is never told to retry in the first place."""
    tc, SessionLocal = refusing

    r = tc.post("/api/timeclock/entries", json=_manual_payload())

    assert r.status_code == 201, r.text
    rows = _entries(SessionLocal)
    assert len(rows) == 1, "exactly one row, and no reason for the office to key a second"
    assert rows[0][2] == 540, "nine hours, as keyed"
    assert r.json()["id"] == rows[0][0]
    assert _audit_actions(SessionLocal) == []


def test_a_refused_audit_row_does_not_fail_a_time_entry_edit(refusing):
    """The second live one. The new stamps are committed before the audit runs,
    so a 500 invites the office to re-apply a correction that already landed."""
    tc, SessionLocal = refusing
    _seed_shift(SessionLocal, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
                clock_out="2026-08-17T22:00:00+00:00", minutes=540, notes="BEFORE")

    r = tc.patch("/api/timeclock/entries/e1", json={"notes": "AFTER"})

    assert r.status_code == 200, r.text
    (_id, _out, _minutes, notes), = _entries(SessionLocal)
    assert notes == "AFTER", "the edit committed"
    assert r.json()["notes"] == "AFTER"
    assert _audit_actions(SessionLocal) == []


# ---------------------------------------------------------------------------
# The auto-close — the one site that is NOT commit-then-audit
# ---------------------------------------------------------------------------

def test_a_refused_audit_rolls_the_auto_close_back_atomically(refusing):
    """The opposite answer from the four above, and deliberately so.

    `_auto_close_stale_shift` still has its close STAGED when the audit runs, so
    a refusal can be undone — and this is a system-initiated write to payroll
    data, which must not land unattributed. `audit_or_rollback` keeps the
    all-or-nothing shape the pre-fix code already had on its happy path (the
    audit row flushed into the caller's transaction, one commit) and fixes only
    the failure path.

    So: 500, and the stale shift UNTOUCHED. Not a regression — pre-fix this was
    also a 500, just an accidental one: the swallow hid the refusal, the Session
    was deactivated, and the caller's `db.commit()` raised
    `PendingRollbackError` into `except SQLAlchemyError` ("Clock-in failed").
    What changes is that the refusal is now deliberate, the Session is rolled
    back rather than handed on dead, and the detail says what happened.

    The cost, stated: while `audit_logs` refuses, a tech with a stale shift
    cannot clock in. `POST /entries` (best-effort) still works, so the office can
    key the day by hand.
    """
    tc, SessionLocal = refusing
    stale = (datetime.now(UTC) - timedelta(hours=MAX_SHIFT_HOURS + 5)).isoformat()
    _seed_shift(SessionLocal, entry_id="e-stale", clock_in=stale,
                clock_out=None, minutes=None)

    r = tc.post("/api/timeclock/clock-in", json={"technician_id": TECH})

    assert r.status_code == 500, r.text
    assert "audit" in r.json()["detail"], "the refusal says what actually failed"
    rows = {row[0]: row for row in _entries(SessionLocal)}
    assert len(rows) == 1, "no new shift was started on a failed close"
    _id, closed_at, minutes, notes = rows["e-stale"]
    assert closed_at is None, "the close was rolled back, not half-applied"
    assert timeclock_module.AUTO_CLOSE_NOTE not in (notes or "")
    assert _audit_actions(SessionLocal) == []


def test_the_auto_close_and_its_audit_row_commit_together(clean):
    """The atomicity itself, not just the failure path: one transaction carries
    the closed shift and its `timeclock_auto_close` row, so no crash window can
    leave a closed-but-unattributed shift.

    Also checks what the row SAYS: `unattended_minutes` is recorded as evidence
    — the office's bound when they set the real end time — and `minutes` stays
    null, because no duration was established.
    """
    tc, SessionLocal = clean
    stale = (datetime.now(UTC) - timedelta(hours=MAX_SHIFT_HOURS + 5)).isoformat()
    _seed_shift(SessionLocal, entry_id="e-stale", clock_in=stale,
                clock_out=None, minutes=None)

    assert tc.post("/api/timeclock/clock-in",
                   json={"technician_id": TECH}).status_code == 201

    rows = {row[0]: row for row in _entries(SessionLocal)}
    assert len(rows) == 2
    _id, closed_at, minutes, notes = rows["e-stale"]
    assert closed_at is not None
    assert minutes is None, "closed with an UNKNOWN duration — never an invented one"
    assert timeclock_module.AUTO_CLOSE_NOTE in (notes or "")

    # The office's own surface, and the audit row, both present.
    ex = tc.get("/api/timeclock/exceptions")
    assert ex.status_code == 200, ex.text
    kinds = {item["kind"]: item for item in ex.json()}
    assert kinds["unknown_duration_shift"]["entry_id"] == "e-stale"
    with SessionLocal() as s:
        tenant, user, details = s.execute(text(
            "SELECT tenant_id, user_id, details FROM audit_logs"
            " WHERE action = 'timeclock_auto_close'"
        )).one()
    # `audit_or_rollback` takes no tenant_id/user_id: both must survive via the
    # request and the actor dict, or the row is attributed to nobody.
    assert tenant == TENANT
    assert user == OFFICE
    assert '"minutes": null' in str(details).replace("'", '"')
    assert "unattended_minutes" in str(details)


# ---------------------------------------------------------------------------
# The send — a different helper, for a reason
# ---------------------------------------------------------------------------

def test_the_delivery_row_survives_a_refused_audit(refusing):
    """A payroll email that leaves the building has to leave a record.

    `send_transactional_email` stages its `outbound_emails` row on the caller's
    session, and pre-fix the swallowed audit refusal meant nothing committed:
    the mail went out, the audit row was refused, and the delivery row was
    discarded at session close — 200 `"sent": true` with nothing recording it.

    `audit_or_rollback` is the wrong answer here and this is why: it cannot
    un-send the mail, so its rollback would destroy the only record of a mail
    that really went out, and its 500 would tell the office to press Send again.

    SQLITE CANNOT FALSIFY THE DELIVERY-ROW HALF — see the module docstring. The
    PG arm below is where that half is proven.
    """
    tc, SessionLocal = refusing
    _seed_shift(SessionLocal, entry_id="e-ok", clock_in="2026-08-17T13:00:00+00:00",
                clock_out="2026-08-17T22:00:00+00:00", minutes=540)

    with patch(SMTP_TARGET, return_value=(True, None)) as smtp:
        r = tc.post("/api/timeclock/pay-period/send", json=RANGE)

    assert r.status_code == 200, r.text
    assert r.json()["sent"] is True
    assert smtp.call_count == 1, "the mail really went through the transport seam"
    assert _outbound(SessionLocal) == [
        ("payroll_timesheet", "bookkeeper@example.com", "sent")
    ], "the record that the bookkeeper was mailed"
    assert _audit_actions(SessionLocal) == []


def test_a_blocked_send_is_not_broken_by_a_refused_audit(refusing):
    """The hold still holds, and still says why, with the trail refused.

    A flagged period must not be mailed whatever the audit does — a 500 from the
    audit path here would read as "try again" on a period that is not ready.
    """
    tc, SessionLocal = refusing
    # An open shift inside the period: the hold `test_time_off_send_hold` covers.
    _seed_shift(SessionLocal, entry_id="e-open", clock_in="2026-08-17T13:00:00+00:00",
                clock_out=None, minutes=None)

    with patch(SMTP_TARGET, return_value=(True, None)) as smtp:
        r = tc.post("/api/timeclock/pay-period/send", json=RANGE)

    assert r.status_code == 409, r.text
    assert smtp.call_count == 0, "nothing may leave on a period that is not clean"
    assert r.json()["detail"]["flagged"], "the refusal still names the offending shifts"
    assert _outbound(SessionLocal) == []
    assert _audit_actions(SessionLocal) == []


def test_a_dead_session_from_the_send_still_records_that_mail_went_out(clean, caplog):
    """The `except SQLAlchemyError` around that `db.commit()`, earning its place.

    If anything inside the send swallows a failure, the handler is handed a
    deactivated Session. Committing it raises, and without the rollback the
    audit row would be refused too (`ensure_audit_table` raises on a poisoned
    session) — so the mail would leave with NOTHING recorded. 500 is not the
    answer either: the mail is gone and a 500 gets clicked again.
    """
    tc, SessionLocal = clean
    _seed_shift(SessionLocal, entry_id="e-ok", clock_in="2026-08-17T13:00:00+00:00",
                clock_out="2026-08-17T22:00:00+00:00", minutes=540)

    real_send = timeclock_module.send_period_timesheet

    def _send_then_poison(db, **kwargs):
        outcome = real_send(db, **kwargs)
        # A swallowed FAILED FLUSH, which is the mechanism that deactivates a
        # Session (`audit_best_effort`'s docstring, note 2) — a duplicate primary
        # key. A raw failing INSERT is not equivalent: the first attempt at this
        # test used `INSERT INTO audit_logs (id) VALUES (NULL)`, which SQLite
        # accepts, so nothing was poisoned and the test passed for no reason.
        try:
            db.add(TimeclockEntry(
                id="e-ok", tenant_id=TENANT, technician_id=TECH,
                clock_in_at="2026-08-17T13:00:00+00:00", clock_out_at=None,
                minutes=None, notes=None, entry_type="clock",
                created_at="2026-08-17T13:00:00+00:00",
                updated_at="2026-08-17T13:00:00+00:00",
            ))
            db.flush()
        except Exception:
            pass
        return outcome

    with patch(SMTP_TARGET, return_value=(True, None)), \
            patch.object(timeclock_module, "send_period_timesheet", _send_then_poison), \
            caplog.at_level(logging.ERROR, logger="gdx_dispatch.routers.timeclock"):
        r = tc.post("/api/timeclock/pay-period/send", json=RANGE)

    assert r.status_code == 200, r.text
    assert any("timesheet_send_delivery_row_lost" in m for m in caplog.messages), (
        caplog.messages
    )
    assert "timesheet_sent" in _audit_actions(SessionLocal), (
        "the session was restored, so the send is still in the audit trail"
    )


# ---------------------------------------------------------------------------
# The clean path — `audit_best_effort` swallows, so these are the only thing
# standing between a mistyped action and a silently unaudited payroll write.
# ---------------------------------------------------------------------------

def test_every_timeclock_write_is_audited_when_the_trail_works(clean):
    tc, SessionLocal = clean

    assert tc.post("/api/timeclock/clock-in",
                   json={"technician_id": TECH}).status_code == 201
    assert tc.post("/api/timeclock/clock-out",
                   json={"technician_id": TECH}).status_code == 200
    created = tc.post("/api/timeclock/entries", json=_manual_payload())
    assert created.status_code == 201, created.text
    assert tc.patch(f"/api/timeclock/entries/{created.json()['id']}",
                    json={"notes": "corrected"}).status_code == 200

    assert _audit_actions(SessionLocal) == [
        "timeclock_clock_in",
        "timeclock_clock_out",
        "timeclock_entry_created",
        "timeclock_entry_updated",
    ]


def test_the_send_is_audited_when_the_trail_works(clean):
    tc, SessionLocal = clean
    _seed_shift(SessionLocal, entry_id="e-ok", clock_in="2026-08-17T13:00:00+00:00",
                clock_out="2026-08-17T22:00:00+00:00", minutes=540)

    with patch(SMTP_TARGET, return_value=(True, None)):
        r = tc.post("/api/timeclock/pay-period/send", json=RANGE)

    assert r.status_code == 200, r.text
    assert _audit_actions(SessionLocal) == ["timesheet_sent"]


# ── the same thing on a real Postgres ────────────────────────────────────────
#
# SQLite and Postgres disagree about savepoints in exactly the way that decides
# the send: releasing an outermost SAVEPOINT commits on SQLite and never on PG.
# So the delivery row that SQLite reports as durable pre-fix was, on the dialect
# production runs, discarded. Skips without a reachable server, like every other
# PG arm in this suite — and FAILS under CI, which has one (#440).


def test_postgres_the_delivery_row_and_the_hours_survive_a_refused_audit(
    pg_test_engine, tmp_path
):
    """PG 15, real trigger, separate connection for every read.

    `fixtures/structure.sql` has drifted from the ORM, and two of its tables get
    rebuilt from the models here rather than worked around: `outbound_emails` is
    absent from the template entirely, and its `app_settings` predates
    `google_maps_api_key`, so an ORM insert raises `UndefinedColumn` (measured
    2026-09-27 — the same class of drift `test_audit_best_effort.py` notes for
    `users.shift_start`). The ORM is truth and the per-test database is a
    throwaway clone, so dropping and recreating those two is the honest fix.
    `audit_logs` and `timeclock_entries_router` are present and current.
    """
    AppSettings.__table__.drop(bind=pg_test_engine, checkfirst=True)
    AppSettings.__table__.create(bind=pg_test_engine)
    OutboundEmail.__table__.drop(bind=pg_test_engine, checkfirst=True)
    OutboundEmail.__table__.create(bind=pg_test_engine)
    SessionLocal = sessionmaker(bind=pg_test_engine, autoflush=False, autocommit=False)

    with pg_test_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO company_module_grants (id, company_id, module_key,"
            " granted_at, created_at) VALUES (gen_random_uuid(), :t, 'timeclock',"
            " now(), now()) ON CONFLICT DO NOTHING"
        ), {"t": TENANT})
    with SessionLocal() as s:
        s.add(AppSettings(
            company_name="Garage Door Xperts", address="", phone="", email="", logo="",
            timezone=TZ, enabled_modules=[], notification_preferences={},
            integrations={}, pay_period_cadence="biweekly",
            pay_period_anchor_start=date(2026, 8, 10), pay_period_pay_lag_days=5,
            payroll_recipient_emails="bookkeeper@example.com",
        ))
        s.add(TimeclockEntry(
            id="e-ok", tenant_id=TENANT, technician_id=TECH,
            clock_in_at="2026-08-17T13:00:00+00:00",
            clock_out_at="2026-08-17T22:00:00+00:00", minutes=540, notes=None,
            entry_type="clock", created_at="2026-08-17T13:00:00+00:00",
            updated_at="2026-08-17T13:00:00+00:00",
        ))
        s.commit()
        ensure_audit_table(s)

    with pg_test_engine.begin() as conn:
        conn.execute(text(
            "CREATE FUNCTION audit_logs_refuse() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'audit storage refuses this row'; END; "
            "$$ LANGUAGE plpgsql"
        ))
        conn.execute(text(
            "CREATE TRIGGER audit_logs_refuse_insert BEFORE INSERT ON audit_logs "
            "FOR EACH ROW EXECUTE FUNCTION audit_logs_refuse()"
        ))

    def _override_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(timeclock_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": OFFICE, "sub": OFFICE, "role": "dispatcher", "tenant_id": TENANT,
    }
    tc = TestClient(app, raise_server_exceptions=True)

    def committed(sql: str):
        with pg_test_engine.connect() as conn:
            return conn.execute(text(sql)).fetchall()

    # 1. the send: the delivery row is the half SQLite cannot falsify
    with patch(SMTP_TARGET, return_value=(True, None)) as smtp:
        r = tc.post("/api/timeclock/pay-period/send", json=RANGE)
    assert r.status_code == 200, r.text
    assert smtp.call_count == 1
    assert committed("SELECT kind, status FROM outbound_emails") == [
        ("payroll_timesheet", "sent")
    ], "pre-fix PG discarded this row with the refused audit — the mail left unrecorded"

    # 2. the manual entry: 201, not 500-with-the-row-saved
    created = tc.post("/api/timeclock/entries", json=_manual_payload())
    assert created.status_code == 201, created.text
    rows = committed(
        "SELECT id, minutes FROM timeclock_entries_router WHERE entry_type = 'manual'"
    )
    assert len(rows) == 1 and rows[0][1] == 540

    assert committed("SELECT action FROM audit_logs") == [], (
        "the trigger is the point; if any row landed this test proves nothing"
    )
