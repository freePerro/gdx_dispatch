"""User labels on SQLite — GDXA-1793.

`routers/timeclock._tech_names`, the roster's active-staff read and
`tasks/payroll_timesheet._names` were raw Postgres SQL (`id::text`,
`= ANY(:ids)`). On SQLite every one raised, the `except` swallowed it, and the
suite saw an unnamed card, roster and timesheet as the documented degrade. So
no test could tell a working name lookup from a broken one.

These run on SQLite on purpose, each seeding a real `users` row and asserting
its name comes back, so they cannot pass on an empty result. The Postgres
containment half (a broken `users` table must not strand the caller) is pinned
in `test_swallowed_read_savepoint_people_time.py`.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.time_off import user_labels
from gdx_dispatch.models.tenant_models import TimeclockEntry, User

TENANT = "tenant-ul"


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db: Session, **kw) -> UUID:
    uid = uuid4()
    db.add(User(id=uid, company_id=TENANT, role="technician", **kw))
    db.commit()
    return uid


def _clock(db: Session, tech_id: str) -> None:
    db.add(TimeclockEntry(
        id=str(uuid4()), tenant_id=TENANT, technician_id=tech_id,
        clock_in_at="2026-10-02T13:00:00+00:00", clock_out_at="2026-10-02T21:00:00+00:00",
        minutes=480, entry_type="clock", created_at="x", updated_at="x",
    ))
    db.commit()


# ── the shared helper ────────────────────────────────────────────────────────


def test_user_labels_resolves_ids_and_keys_them_as_the_caller_spelled_them(db):
    """SQLite stores the Uuid dashless; a text comparison would never match."""
    a = _user(db, full_name="Ann Full", name="ann", email="a@x")
    b = _user(db, full_name="", name="", username="bobby", email="b@x")
    upper = str(b).upper()
    out = user_labels(db, TENANT, {str(a), upper, "not-a-uuid"})
    assert out == {str(a): "Ann Full", upper: "bobby"}


def test_user_labels_empty_ids_reads_nothing_but_none_reads_everyone(db):
    who = _user(db, email="only-email@x")
    assert user_labels(db, TENANT, set()) == {}
    assert user_labels(db, TENANT) == {str(who): "only-email@x"}


def test_user_labels_active_only_drops_departed_and_disabled_but_not_null(db):
    """NULL `active` means active (users._serialize); `= true` would hide them."""
    live = _user(db, full_name="Live", active=True)
    unset = _user(db, full_name="Unset")
    db.execute(text("UPDATE users SET active = NULL WHERE full_name = 'Unset'"))
    db.commit()
    _user(db, full_name="Disabled", active=False)
    _user(db, full_name="Departed", active=True, deleted_at=datetime.now(UTC))
    assert user_labels(db, TENANT, active_only=True) == {
        str(live): "Live",
        str(unset): "Unset",
    }


def test_user_labels_degrades_to_empty_when_users_is_unreadable(db, caplog):
    who = _user(db, full_name="Gone")
    db.execute(text("ALTER TABLE users RENAME TO users_gone"))
    db.commit()
    assert user_labels(db, TENANT, {str(who)}, log_event="probe_names_failed") == {}
    assert "probe_names_failed" in caplog.text


# ── the three callers ────────────────────────────────────────────────────────


def test_tech_names_names_a_departed_employee_on_sqlite(db):
    """The exception card and export name everyone who clocked in, including a
    soft-deleted user: their hours are still on the books."""
    from gdx_dispatch.routers.timeclock import _tech_names

    here = _user(db, full_name="Still Here")
    gone = _user(db, full_name="Left Us", deleted_at=datetime.now(UTC))
    assert _tech_names(db, TENANT, {str(here), str(gone)}) == {
        str(here): "Still Here",
        str(gone): "Left Us",
    }


def test_the_roster_names_clocked_and_active_staff_on_sqlite(db):
    from gdx_dispatch.routers.timeclock import timeclock_roster

    clocked = _user(db, full_name="Clocked Departed", deleted_at=datetime.now(UTC))
    idle = _user(db, full_name="Idle Active")
    _user(db, full_name="Disabled", active=False)
    _clock(db, str(clocked))

    items = timeclock_roster(
        request=SimpleNamespace(state=SimpleNamespace(tenant={"id": TENANT})),
        current_user={"sub": str(uuid4()), "role": "admin"},
        db=db,
    )
    by_id = {i.technician_id: i for i in items}
    assert set(by_id) == {str(clocked), str(idle)}
    assert by_id[str(clocked)].name == "Clocked Departed"
    assert by_id[str(clocked)].active is False
    assert by_id[str(clocked)].has_entries is True
    assert by_id[str(idle)].name == "Idle Active"
    assert by_id[str(idle)].active is True
    assert by_id[str(idle)].has_entries is False


def test_payroll_send_names_on_sqlite(db):
    from gdx_dispatch.tasks.payroll_timesheet import _names

    who = _user(db, full_name="Payroll Person")
    assert _names(db, TENANT) == {str(who): "Payroll Person"}
