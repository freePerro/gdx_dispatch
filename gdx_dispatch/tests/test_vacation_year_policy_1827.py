"""Hire date and the company vacation-year policy (GDXA-1827, migration 131).

`users.hire_date` existed with no reader or writer; PATCH /api/users/{id} now
sets it and every user read returns it. `app_settings.vacation_year_policy`
is new: `calendar_year` (default) or `hire_anniversary`, the latter falling
back to the calendar year for anyone with no hire date (Doug, 2026-10-19).

The writes go through HTTP against ORM-built SQLite and are read back from the
database and from a later GET; each leaves an audit row naming the actor and
the prior value. Nothing is mocked. The migration runs on SQLite and Postgres.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import uuid
from datetime import date, timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import column, create_engine, insert, inspect, select, table, text

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.models.tenant_models import AppSettings, User
from gdx_dispatch.tests.test_silent_success_sweep import TENANT, _build_client, _login, _teardown

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/131_vacation_year_policy.py"
COLUMN = "vacation_year_policy"
ADMIN = str(uuid.uuid4())


# ── migration 131 ────────────────────────────────────────────────────────────

def _load(conn):
    spec = importlib.util.spec_from_file_location("m131", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _has_column(conn) -> bool:
    return COLUMN in {c["name"] for c in inspect(conn).get_columns("app_settings")}


def _legacy_schema(conn) -> None:
    conn.exec_driver_sql("CREATE TABLE app_settings (id VARCHAR(36) PRIMARY KEY, company_name TEXT)")
    conn.execute(insert(table("app_settings", column("id"), column("company_name"))).values(id="a", company_name="old"))


def _value(conn) -> object:
    return conn.exec_driver_sql("SELECT vacation_year_policy FROM app_settings WHERE id = 'a'").scalar_one()


def test_it_chains_onto_130_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "131_vacation_year_policy"' in source
    assert 'down_revision = "130_job_photo_twins_drop"' in source
    assert len("131_vacation_year_policy") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "130_job_photo_twins_drop"', p.read_text(), re.M)
    ]
    assert revising == ["131_vacation_year_policy.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_sqlite_existing_row_reads_calendar_year_and_round_trips(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm131.db'}", future=True)
    with eng.begin() as c:
        _legacy_schema(c)
        m = _load(c)
        m.upgrade()
        assert _has_column(c)
        assert _value(c) == "calendar_year"
        m.upgrade()  # rerun is a no-op
        m.downgrade()
        assert not _has_column(c)
        assert c.exec_driver_sql("SELECT company_name FROM app_settings WHERE id = 'a'").scalar_one() == "old"
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _value(c) == "calendar_year"
    eng.dispose()


def test_on_a_create_all_database_upgrade_is_a_no_op(tmp_path) -> None:
    from gdx_dispatch.models.tenant_models import Base

    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    Base.metadata.create_all(bind=eng)
    with eng.begin() as c:
        assert _has_column(c), "create_all already builds the column"
        _load(c).upgrade()
    eng.dispose()


def test_postgres_existing_row_reads_calendar_year_and_round_trips(pg_test_db) -> None:
    eng = create_engine(pg_test_db, future=True)
    schema = "m131_test"
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.exec_driver_sql(f"CREATE SCHEMA {schema}")
            c.exec_driver_sql(f"SET search_path TO {schema}")
            _legacy_schema(c)
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            assert _has_column(c)
            assert _value(c) == "calendar_year"
            info = c.execute(
                text(
                    "SELECT data_type, character_maximum_length, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'app_settings' AND column_name = :c"
                ),
                {"s": schema, "c": COLUMN},
            ).one()
            assert tuple(info) == ("character varying", 20, "NO", "'calendar_year'::character varying"), info
            m.upgrade()  # rerun is a no-op
            m.downgrade()
            c.commit()
            assert not _has_column(c)
            m.upgrade()
            c.commit()
            assert _value(c) == "calendar_year"
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()


# ── the policy: a company setting, audited ───────────────────────────────────

@pytest.fixture()
def settings_client():
    from gdx_dispatch.routers import settings as settings_mod

    c = _build_client(settings_mod.router, user=_login(ADMIN, "admin"))
    yield c
    _teardown(c)


def _audits(SessionLocal, action: str) -> list[AuditLog]:
    with SessionLocal() as db:
        return list(db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all())


def test_the_policy_defaults_to_calendar_year_saves_and_is_audited(settings_client) -> None:
    c = settings_client
    assert c.get("/api/settings").json()["vacation_year_policy"] == "calendar_year"

    r = c.patch("/api/settings", json={"vacation_year_policy": "hire_anniversary"})
    assert r.status_code == 200, r.text
    assert r.json()["vacation_year_policy"] == "hire_anniversary"
    with c.SessionLocal() as db:
        assert db.execute(select(AppSettings.vacation_year_policy)).scalar_one() == "hire_anniversary"
    assert c.get("/api/settings").json()["vacation_year_policy"] == "hire_anniversary"

    r = c.patch("/api/settings", json={"vacation_year_policy": "calendar_year"})
    assert r.status_code == 200, r.text
    with c.SessionLocal() as db:
        assert db.execute(select(AppSettings.vacation_year_policy)).scalar_one() == "calendar_year"

    rows = _audits(c.SessionLocal, "settings_updated")
    assert [r.user_id for r in rows] == [ADMIN, ADMIN]
    assert rows[0].details["vacation_year_policy"] == "hire_anniversary"
    assert rows[0].details["vacation_year_policy_previous"] == "calendar_year"
    assert rows[1].details["vacation_year_policy"] == "calendar_year"
    assert rows[1].details["vacation_year_policy_previous"] == "hire_anniversary"


def test_resending_the_same_policy_records_no_previous_and_omitting_it_keeps_it(settings_client) -> None:
    c = settings_client
    c.patch("/api/settings", json={"vacation_year_policy": "hire_anniversary"})
    c.patch("/api/settings", json={"vacation_year_policy": "hire_anniversary"})
    assert "vacation_year_policy_previous" not in _audits(c.SessionLocal, "settings_updated")[-1].details

    r = c.patch("/api/settings", json={"time_off_counts_toward_overtime": True})
    assert r.json()["vacation_year_policy"] == "hire_anniversary"
    assert "vacation_year_policy" not in _audits(c.SessionLocal, "settings_updated")[-1].details


@pytest.mark.parametrize("bad", [None, "", "anniversary", "fiscal_year", 1])
def test_a_policy_outside_the_two_choices_is_refused_and_nothing_is_written(settings_client, bad) -> None:
    c = settings_client
    r = c.patch("/api/settings", json={"vacation_year_policy": bad})
    assert r.status_code == 422, r.text
    assert c.get("/api/settings").json()["vacation_year_policy"] == "calendar_year"
    assert _audits(c.SessionLocal, "settings_updated") == []


def test_a_technician_cannot_change_the_policy(settings_client) -> None:
    from gdx_dispatch.routers import settings as settings_mod

    tech = _build_client(settings_mod.router, user=_login(str(uuid.uuid4()), "technician"))
    try:
        r = tech.patch("/api/settings", json={"vacation_year_policy": "hire_anniversary"})
        assert r.status_code == 403, r.text
    finally:
        _teardown(tech)


# ── the fallback rule ────────────────────────────────────────────────────────

ON = date(2026, 10, 19)


@pytest.mark.parametrize(
    ("policy", "hire", "on", "start"),
    [
        # Calendar year is the default, and what any policy it does not know reads as.
        ("calendar_year", date(2019, 4, 2), ON, date(2026, 1, 1)),
        (None, date(2019, 4, 2), ON, date(2026, 1, 1)),
        ("fiscal_year", date(2019, 4, 2), ON, date(2026, 1, 1)),
        # Anniversary: the latest anniversary on or before the day.
        ("hire_anniversary", date(2019, 4, 2), ON, date(2026, 4, 2)),
        ("hire_anniversary", date(2019, 11, 30), ON, date(2025, 11, 30)),
        ("hire_anniversary", date(2019, 10, 19), ON, date(2026, 10, 19)),
        # A 29 February hire counts from 28 February in a common year.
        ("hire_anniversary", date(2020, 2, 29), date(2026, 3, 1), date(2026, 2, 28)),
        ("hire_anniversary", date(2020, 2, 29), date(2028, 3, 1), date(2028, 2, 29)),
        # The fallback Doug ruled: no hire date means the calendar year...
        ("hire_anniversary", None, ON, date(2026, 1, 1)),
        # ...and so does a hire date that has not arrived yet.
        ("hire_anniversary", date(2026, 12, 1), ON, date(2026, 1, 1)),
    ],
)
def test_vacation_year_start(policy, hire, on, start) -> None:
    from gdx_dispatch.core.time_off import vacation_year_start

    assert vacation_year_start(policy, hire, on) == start


# ── hire date: written through the user PATCH, audited, read back ────────────

@pytest.fixture()
def users_client():
    from gdx_dispatch.routers import users as users_mod

    c = _build_client(users_mod.router, user=_login(ADMIN, "admin"), module_keys=("jobs",))

    # require_permission is not cached, so it cannot be overridden by identity;
    # hand it the resolved set an admin gets. RBAC has its own tests.
    @c.app.middleware("http")
    async def _grant(request, call_next):
        request.state.user_permissions = {"*"}
        return await call_next(request)

    with c.SessionLocal() as db:
        tech = User(id=uuid.uuid4(), company_id=TENANT, email="tech1827@example.com",
                    full_name="Tech 1827", role="technician")
        db.add(tech)
        db.commit()
        c.tech_id = str(tech.id)  # type: ignore[attr-defined]
    yield c
    _teardown(c)


def _hire_date(c) -> date | None:
    with c.SessionLocal() as db:
        return db.execute(select(User.hire_date).where(User.id == uuid.UUID(c.tech_id))).scalar_one()


def test_hire_date_starts_null_saves_clears_and_is_audited_with_both_values(users_client) -> None:
    c = users_client
    assert c.get(f"/api/users/{c.tech_id}").json()["hire_date"] is None

    r = c.patch(f"/api/users/{c.tech_id}", json={"hire_date": "2019-04-02"})
    assert r.status_code == 200, r.text
    assert r.json()["hire_date"] == "2019-04-02"
    assert _hire_date(c) == date(2019, 4, 2)
    assert c.get(f"/api/users/{c.tech_id}").json()["hire_date"] == "2019-04-02"
    assert next(u for u in c.get("/api/users").json() if u["id"] == c.tech_id)["hire_date"] == "2019-04-02"

    r = c.patch(f"/api/users/{c.tech_id}", json={"hire_date": None})
    assert r.status_code == 200, r.text
    assert _hire_date(c) is None

    rows = _audits(c.SessionLocal, "user_updated")
    assert [r.user_id for r in rows] == [ADMIN, ADMIN]
    assert rows[0].details["hire_date"] == {"from": None, "to": "2019-04-02"}
    assert rows[1].details["hire_date"] == {"from": "2019-04-02", "to": None}


def test_the_dialog_resending_the_same_hire_date_writes_no_change(users_client) -> None:
    c = users_client
    c.patch(f"/api/users/{c.tech_id}", json={"hire_date": "2019-04-02"})
    c.patch(f"/api/users/{c.tech_id}", json={"phone": "555-0100", "hire_date": "2019-04-02"})
    last = _audits(c.SessionLocal, "user_updated")[-1].details
    assert "hire_date" not in last and last["changed_fields"] == ["phone"]
    assert _hire_date(c) == date(2019, 4, 2)


@pytest.mark.parametrize(
    "bad", ["1899-12-31", (date.today() + timedelta(days=367)).isoformat(), "2019-02-30", "not a date"]
)
def test_an_impossible_hire_date_is_refused_and_nothing_is_written(users_client, bad) -> None:
    c = users_client
    r = c.patch(f"/api/users/{c.tech_id}", json={"hire_date": bad})
    assert r.status_code == 422, r.text
    assert _hire_date(c) is None
    assert _audits(c.SessionLocal, "user_updated") == []


def test_a_hire_date_up_to_a_year_ahead_is_accepted(users_client) -> None:
    c = users_client
    ahead = date.today() + timedelta(days=30)
    r = c.patch(f"/api/users/{c.tech_id}", json={"hire_date": ahead.isoformat()})
    assert r.status_code == 200, r.text
    assert _hire_date(c) == ahead
