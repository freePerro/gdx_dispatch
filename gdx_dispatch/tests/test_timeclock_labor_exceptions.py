"""Labor exceptions — the office's self-clearing review card (2026-07-17).

Doug: a tech is paid start-of-day to end-of-day, and "it should be the
dispatcher or office personel that get told about the discrepency."

Deliberately not a report and not a recommendation: `core/recommendations.py`
and next-actions have NO frontend renderer, so anything filed there is
invisible on arrival. This endpoint backs a card that only exists when
something is wrong, on a screen the office already opens — so it cannot nag on
a clean day and nobody has to remember to run it.

Pinned here:
1. A clean shop returns [] — the card must vanish, not nag.
2. A normal in-progress shift is NOT an exception (the anti-wallpaper rule).
3. A shift still open past the threshold IS one.
4. An auto-closed shift (minutes NULL) IS one, and reports 0 hours rather than
   the fabricated elapsed that put 1584h into prod.
5. A historical implausible shift (minutes > threshold) IS one.
6. Near-zero taps are NOT exceptions — 21 of 39 prod rows are accidental
   double-taps worth 0 minutes; surfacing them is the flood that turns the
   card into wallpaper (the recorded lesson at parts_needed.py:105).
7. Techs cannot read it — it exposes other people's time (same gate as the
   payroll aggregate).
8. Fixing the shift clears the exception — the fix IS the dismissal.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import TimeclockEntry
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.timeclock import IMPLAUSIBLE_SHIFT_MINUTES, router

TENANT = "tenant-test"
_ROLE = {"value": "dispatcher"}


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    for ddl in (
        """CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key))""",
    ):
        setup.execute(text(ddl))
    setup.execute(text(
        "INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at) "
        "VALUES ('g1', 'tenant-test', 'timeclock', datetime('now'), datetime('now'))"
    ))
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
        "VALUES ('g2', 'tenant-test', 'timeclock', datetime('now'), datetime('now'))"
    ))
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "office-1",
        "sub": "office-1",
        "role": _ROLE["value"],
        "tenant_id": TENANT,
    }
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc, Session
    app.dependency_overrides.clear()
    engine.dispose()


def _seed(Session, *, entry_id: str, hours_ago: float, minutes: int | None,
          closed: bool, notes: str | None = None) -> str:
    clock_in = datetime.now(UTC) - timedelta(hours=hours_ago)
    db = Session()
    try:
        db.add(TimeclockEntry(
            id=entry_id,
            tenant_id=TENANT,
            technician_id="user-michael",
            clock_in_at=clock_in.isoformat(),
            clock_out_at=datetime.now(UTC).isoformat() if closed else None,
            minutes=minutes,
            notes=notes,
            entry_type="clock",
            created_at=clock_in.isoformat(),
            updated_at=clock_in.isoformat(),
        ))
        db.commit()
    finally:
        db.close()
    return entry_id


def _get(tc) -> list[dict]:
    r = tc.get("/api/timeclock/exceptions")
    assert r.status_code == 200, r.text
    return r.json()


def test_clean_shop_returns_nothing(client):
    """The card renders v-if="rows.length" — empty means it does not exist."""
    tc, _ = client
    assert _get(tc) == []


def test_normal_in_progress_shift_is_not_an_exception(client):
    """A tech clocked in this morning is working, not an exception. Surfacing
    them is how the card becomes wallpaper."""
    tc, Session = client
    _seed(Session, entry_id="e-normal", hours_ago=6, minutes=None, closed=False)
    assert _get(tc) == []


def test_normal_completed_shift_is_not_an_exception(client):
    tc, Session = client
    _seed(Session, entry_id="e-done", hours_ago=8, minutes=480, closed=True)
    assert _get(tc) == []


def test_near_zero_tap_is_not_an_exception(client):
    """21 of 39 prod rows are accidental double-taps worth 0 minutes. They cost
    nothing and pay nothing — flooding the card with them defeats it."""
    tc, Session = client
    _seed(Session, entry_id="e-tap", hours_ago=0.01, minutes=0, closed=True)
    assert _get(tc) == []


def test_shift_still_open_past_threshold_is_surfaced(client):
    tc, Session = client
    _seed(Session, entry_id="e-open", hours_ago=30, minutes=None, closed=False)

    rows = _get(tc)
    assert len(rows) == 1
    assert rows[0]["kind"] == "open_shift"
    assert rows[0]["entry_id"] == "e-open"
    assert rows[0]["hours"] >= 30


def test_auto_closed_shift_reports_unknown_not_fabricated_hours(client):
    """The prod damage: MH-7b stamped elapsed as worked, producing a closed
    1584h shift. An auto-closed row now carries minutes=NULL and must surface
    as 'unknown', reporting 0 hours rather than the fiction."""
    tc, Session = client
    _seed(
        Session, entry_id="e-auto", hours_ago=1584, minutes=None, closed=True,
        notes="Auto-closed — end time unknown, needs office review",
    )

    rows = _get(tc)
    assert len(rows) == 1
    assert rows[0]["kind"] == "unknown_duration_shift"
    assert rows[0]["hours"] == 0.0, "fabricated elapsed leaked into the card"


def test_historical_implausible_shift_is_surfaced(client):
    """Rows written before the auto-close fix still carry fabricated minutes."""
    tc, Session = client
    _seed(Session, entry_id="e-1584", hours_ago=1584, minutes=95064, closed=True)

    rows = _get(tc)
    assert len(rows) == 1
    assert rows[0]["kind"] == "implausible_shift"
    assert rows[0]["hours"] > 1000


def test_threshold_boundary_is_not_surfaced(client):
    tc, Session = client
    _seed(
        Session, entry_id="e-edge", hours_ago=20,
        minutes=IMPLAUSIBLE_SHIFT_MINUTES, closed=True,
    )
    assert _get(tc) == []


def test_fixing_the_shift_clears_the_exception(client):
    """The fix IS the dismissal — no 'mark as read' to forget. The office sets
    the real end time and the row drops out on the next load."""
    tc, Session = client
    _seed(Session, entry_id="e-fix", hours_ago=200, minutes=12000, closed=True)
    assert len(_get(tc)) == 1

    db = Session()
    try:
        entry = db.get(TimeclockEntry, "e-fix")
        entry.minutes = 450  # office corrected it to a real 7.5h day
        db.commit()
    finally:
        db.close()

    assert _get(tc) == [], "corrected shift still nagging"


def test_deleted_shift_is_not_surfaced(client):
    tc, Session = client
    _seed(Session, entry_id="e-del", hours_ago=200, minutes=12000, closed=True)
    db = Session()
    try:
        entry = db.get(TimeclockEntry, "e-del")
        entry.deleted_at = datetime.now(UTC).isoformat()
        db.commit()
    finally:
        db.close()

    assert _get(tc) == []


def test_technician_cannot_read_other_peoples_time(client):
    """Same gate as the payroll aggregate — this exposes everyone's hours."""
    tc, Session = client
    _seed(Session, entry_id="e-open2", hours_ago=30, minutes=None, closed=False)
    _ROLE["value"] = "technician"
    try:
        r = tc.get("/api/timeclock/exceptions")
        assert r.status_code == 403
    finally:
        _ROLE["value"] = "dispatcher"


def test_worst_offenders_sort_first(client):
    tc, Session = client
    _seed(Session, entry_id="e-small", hours_ago=20, minutes=1200, closed=True)
    _seed(Session, entry_id="e-huge", hours_ago=1584, minutes=95064, closed=True)

    rows = _get(tc)
    assert [r["entry_id"] for r in rows] == ["e-huge", "e-small"]
