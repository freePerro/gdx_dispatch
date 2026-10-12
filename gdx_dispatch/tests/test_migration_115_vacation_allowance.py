"""Migration 115 and the vacation allowance it stores (GDXA-700, GDXA-576 part 1).

The migration, on both engines: existing rows read NULL (no shop default, no
override), up/down/up, rerunnable, a no-op on a ``create_all`` database. The
writes: the shop default saves through PATCH /api/settings and the per-person
override through PATCH /api/users/{id}, each read back from the database and
each leaving an audit row that names the acting user and both values.

The handlers run for real against an ORM-built SQLite schema; nothing is
mocked. As in test_users_certifications_683, ``update_user`` gets a
``uuid.UUID``: SQLite's ``Uuid`` bind cannot take the dashed path string.
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
from pydantic import ValidationError
from sqlalchemy import column, create_engine, insert, inspect, select, table, text, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/115_vacation_allowance.py"
TABLES = ("app_settings", "users")
COLUMN = "vacation_allowance_hours"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m115", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _has_column(conn) -> dict[str, bool]:
    insp = inspect(conn)
    return {t: COLUMN in {c["name"] for c in insp.get_columns(t)} for t in TABLES}


def _t(name: str):
    return table(name, column("id"), column("name"), column(COLUMN))


def _legacy_schema(conn) -> None:
    """The two tables as an existing database has them, one row each."""
    conn.exec_driver_sql("CREATE TABLE app_settings (id VARCHAR(36) PRIMARY KEY, name TEXT)")
    conn.exec_driver_sql("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, name TEXT)")
    for t in TABLES:
        conn.execute(insert(table(t, column("id"), column("name"))).values(id="a", name="old row"))


def _value(conn, name: str) -> object:
    tbl = _t(name)
    return conn.execute(select(tbl.c[COLUMN]).where(tbl.c.id == "a")).scalar_one()


def _values(conn) -> dict[str, object]:
    return {t: _value(conn, t) for t in TABLES}


def test_it_chains_onto_114_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "115_vacation_allowance"' in source
    assert 'down_revision = "114_row_version_tokens"' in source
    assert len("115_vacation_allowance") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "114_row_version_tokens"', p.read_text(), re.M)
    ]
    assert revising == ["115_vacation_allowance.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_sqlite_existing_rows_read_null_and_round_trip(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm115.db'}", future=True)
    with eng.begin() as c:
        _legacy_schema(c)
        m = _load(c)
        m.upgrade()
        assert _has_column(c) == dict.fromkeys(TABLES, True)
        assert _values(c) == dict.fromkeys(TABLES, None)
        m.upgrade()  # rerun is a no-op
        m.downgrade()
        assert _has_column(c) == dict.fromkeys(TABLES, False)
        assert c.exec_driver_sql("SELECT name FROM users WHERE id = 'a'").scalar_one() == "old row"
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _has_column(c) == dict.fromkeys(TABLES, True)
    eng.dispose()


def test_on_a_create_all_database_upgrade_is_a_no_op(tmp_path) -> None:
    from gdx_dispatch.models.tenant_models import Base

    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    Base.metadata.create_all(bind=eng)
    with eng.begin() as c:
        assert _has_column(c) == dict.fromkeys(TABLES, True), "create_all already builds the column"
        _load(c).upgrade()
    eng.dispose()


def test_postgres_existing_rows_read_null_and_round_trip(pg_test_db) -> None:
    eng = create_engine(pg_test_db, future=True)
    schema = "m115_test"
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
            assert _has_column(c) == dict.fromkeys(TABLES, True)
            assert _values(c) == dict.fromkeys(TABLES, None)
            for t in TABLES:
                info = c.execute(
                    text(
                        "SELECT data_type, numeric_precision, numeric_scale, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_schema = :s AND table_name = :t AND column_name = :c"
                    ),
                    {"s": schema, "t": t, "c": COLUMN},
                ).one()
                assert tuple(info) == ("numeric", 6, 2, "YES", None), (t, info)
            c.execute(update(_t("users")).values({COLUMN: Decimal("40.5")}))
            assert _values(c)["users"] == Decimal("40.50")
            m.upgrade()  # rerun is a no-op
            m.downgrade()
            c.commit()
            assert _has_column(c) == dict.fromkeys(TABLES, False)
            m.upgrade()
            c.commit()
            assert _has_column(c) == dict.fromkeys(TABLES, True)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()


# ── the writes: shop default and per-person override, each audited ───────────

TENANT = "tenant-700"


def _request(method: str = "PATCH") -> Request:
    req = Request({
        "type": "http", "method": method, "path": "/api/x", "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 0),
    })
    req.state.tenant = {"id": TENANT}
    return req


@pytest.fixture()
def db():
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


def _audits(db, action: str):
    from gdx_dispatch.core.audit import AuditLog

    return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def test_the_shop_default_starts_unset_saves_clears_and_is_audited(db) -> None:
    from gdx_dispatch.models.tenant_models import AppSettings
    from gdx_dispatch.routers.settings import SettingsPatchIn, get_settings, patch_settings

    admin = {"sub": "admin-700", "role": "admin"}
    assert get_settings(admin, db)["vacation_allowance_hours"] is None

    out = patch_settings(SettingsPatchIn(vacation_allowance_hours="80.5"), _request(), admin, db)
    assert out["vacation_allowance_hours"] == 80.5
    db.expire_all()
    assert db.execute(select(AppSettings.vacation_allowance_hours)).scalar_one() == Decimal("80.50")

    patch_settings(SettingsPatchIn(vacation_allowance_hours=None), _request(), admin, db)
    db.expire_all()
    assert db.execute(select(AppSettings.vacation_allowance_hours)).scalar_one() is None

    rows = _audits(db, "settings_updated")
    assert [r.user_id for r in rows] == ["admin-700", "admin-700"]
    assert rows[0].details["vacation_allowance_hours_previous"] is None
    assert rows[0].details["vacation_allowance_hours"] == 80.5
    assert rows[1].details["vacation_allowance_hours_previous"] == 80.5
    assert rows[1].details["vacation_allowance_hours"] is None


def test_a_settings_save_that_omits_it_leaves_it_alone(db) -> None:
    from gdx_dispatch.routers.settings import SettingsPatchIn, patch_settings

    admin = {"sub": "admin-700", "role": "admin"}
    patch_settings(SettingsPatchIn(vacation_allowance_hours=40), _request(), admin, db)
    out = patch_settings(SettingsPatchIn(time_off_counts_toward_overtime=True), _request(), admin, db)
    assert out["vacation_allowance_hours"] == 40.0
    assert "vacation_allowance_hours" not in _audits(db, "settings_updated")[-1].details


def test_resending_the_same_shop_default_records_no_previous_value(db) -> None:
    from gdx_dispatch.routers.settings import SettingsPatchIn, patch_settings

    admin = {"sub": "admin-700", "role": "admin"}
    patch_settings(SettingsPatchIn(vacation_allowance_hours=40), _request(), admin, db)
    patch_settings(SettingsPatchIn(vacation_allowance_hours="40.00"), _request(), admin, db)
    last = _audits(db, "settings_updated")[-1].details
    assert last["vacation_allowance_hours"] == 40.0
    assert "vacation_allowance_hours_previous" not in last


@pytest.mark.parametrize("bad", [-1, "8784.01", "40.125"])
def test_an_impossible_allowance_is_refused(bad) -> None:
    from gdx_dispatch.routers.settings import SettingsPatchIn
    from gdx_dispatch.routers.users import UserPatchIn

    for model in (SettingsPatchIn, UserPatchIn):
        with pytest.raises(ValidationError):
            model(vacation_allowance_hours=bad)


def test_a_non_admin_cannot_set_the_shop_default(db) -> None:
    from fastapi import HTTPException

    from gdx_dispatch.routers.settings import SettingsPatchIn, patch_settings

    with pytest.raises(HTTPException) as exc:
        patch_settings(SettingsPatchIn(vacation_allowance_hours=40), _request(), {"sub": "t", "role": "technician"}, db)
    assert exc.value.status_code == 403


def _make_user(db):
    from gdx_dispatch.models.tenant_models import User

    u = User(id=uuid.uuid4(), company_id=TENANT, email="tech700@example.com", full_name="Tech 700", role="technician")
    db.add(u)
    db.commit()
    return u


def test_the_override_starts_null_saves_clears_and_is_audited_with_both_values(db) -> None:
    from gdx_dispatch.models.tenant_models import User
    from gdx_dispatch.routers.users import UserPatchIn, _serialize, update_user

    u = _make_user(db)
    assert _serialize(u)["vacation_allowance_hours"] is None

    out = update_user(u.id, UserPatchIn(vacation_allowance_hours="120"), _request(), {"sub": "admin-700"}, db)
    assert out["vacation_allowance_hours"] == 120.0
    db.expire_all()
    assert db.execute(select(User.vacation_allowance_hours).where(User.id == u.id)).scalar_one() == Decimal("120.00")

    update_user(u.id, UserPatchIn(vacation_allowance_hours=None), _request(), {"sub": "admin-701"}, db)
    db.expire_all()
    assert db.get(User, u.id).vacation_allowance_hours is None
    assert _serialize(db.get(User, u.id))["vacation_allowance_hours"] is None

    rows = _audits(db, "user_updated")
    assert [r.user_id for r in rows] == ["admin-700", "admin-701"]
    assert rows[0].details["vacation_allowance_hours"] == {"from": None, "to": 120.0}
    assert rows[1].details["vacation_allowance_hours"] == {"from": 120.0, "to": None}


def test_a_profile_edit_that_omits_it_leaves_it_alone(db) -> None:
    from gdx_dispatch.models.tenant_models import User
    from gdx_dispatch.routers.users import UserPatchIn, update_user

    u = _make_user(db)
    update_user(u.id, UserPatchIn(vacation_allowance_hours=24), _request(), {"sub": "admin-700"}, db)
    update_user(u.id, UserPatchIn(phone="555-0100"), _request(), {"sub": "admin-700"}, db)
    db.expire_all()
    assert db.get(User, u.id).vacation_allowance_hours == Decimal("24.00")
    assert "vacation_allowance_hours" not in _audits(db, "user_updated")[-1].details

    # The dialog resends the unchanged value with every save: no from/to entry.
    update_user(u.id, UserPatchIn(phone="555-0101", vacation_allowance_hours="24"), _request(), {"sub": "admin-700"}, db)
    assert "vacation_allowance_hours" not in _audits(db, "user_updated")[-1].details
