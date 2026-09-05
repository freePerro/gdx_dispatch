"""Tech self-service timesheet — the backend contract under the tech's own
weekly timesheet (2026-08-13, docs/design/tech-weekly-timesheet-plan.md).

PATCH/POST /api/timeclock/entries have allowed a tech to edit their OWN rows
since the module shipped — permission-gated and audit-logged, but with no
guardrails, because nothing in the UI ever called them as a tech. The weekly
timesheet gives that path a button, so the rules land first (D2, Doug
2026-08-13):

1. A tech must say WHY. The note is the tech's attestation on the row itself,
   not just a line in the audit log.
2. A tech may only touch the recent window (SELF_SERVICE_EDIT_WINDOW_DAYS).
   Older weeks are paid weeks; those corrections go through the office. The
   window binds BOTH the row's current clock-in and any new one being written,
   so a recent entry can't be back-dated into a paid week.
3. Dispatch/admin are exempt from both — the office corrects anything, any age
   (the /timesheets page must not regress).
4. `include_breaks=true` populates break_minutes on the SELF path, so the
   tech's week total nets lunches exactly like the office page. Opt-in: the
   bare self call (TimeclockView/MobileTimeclockView today-lists) is unchanged.
5. POST may omit technician_id and means "me" — same contract as clock-in/out.
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
from gdx_dispatch.models.tenant_models import TimeclockBreak, TimeclockEntry
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.timeclock import SELF_SERVICE_EDIT_WINDOW_DAYS, router

TENANT = "tenant-test"
ME = "user-tech-me"
_ROLE = {"value": "technician"}

# Comfortably outside the self-service window, whatever it's tuned to.
OLD_DAYS = SELF_SERVICE_EDIT_WINDOW_DAYS + 7


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
        """CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key))""",
    ):
        setup.execute(text(ddl))
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
        "user_id": ME,
        "sub": ME,
        "role": _ROLE["value"],
        "tenant_id": TENANT,
    }
    _ROLE["value"] = "technician"
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc, Session
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_entry(Session, *, entry_id: str, tech: str = ME, days_ago: float = 1,
                minutes: int | None = 480, closed: bool = True) -> str:
    clock_in = datetime.now(UTC) - timedelta(days=days_ago)
    clock_out = clock_in + timedelta(minutes=minutes or 480)
    db = Session()
    try:
        db.add(TimeclockEntry(
            id=entry_id,
            tenant_id=TENANT,
            technician_id=tech,
            clock_in_at=clock_in.isoformat(),
            clock_out_at=clock_out.isoformat() if closed else None,
            minutes=minutes if closed else None,
            notes=None,
            entry_type="clock",
            created_at=clock_in.isoformat(),
            updated_at=clock_in.isoformat(),
        ))
        db.commit()
    finally:
        db.close()
    return entry_id


def _seed_break(Session, *, break_id: str, user: str = ME, days_ago: float = 1,
                offset_minutes: int = 120, duration: int = 30) -> str:
    """A finished break `offset_minutes` into the shift seeded `days_ago` back —
    matched by user + time window, the way prod rows actually match (their
    time_entry_id is NULL on 10 of 10 real rows)."""
    started = datetime.now(UTC) - timedelta(days=days_ago) + timedelta(minutes=offset_minutes)
    db = Session()
    try:
        db.add(TimeclockBreak(
            id=break_id,
            tenant_id=TENANT,
            user_id=user,
            time_entry_id=None,
            type="lunch",
            notes=None,
            started_at=started.isoformat(),
            ended_at=(started + timedelta(minutes=duration)).isoformat(),
            duration_minutes=duration,
            created_at=started.isoformat(),
        ))
        db.commit()
    finally:
        db.close()
    return break_id


def _iso_days_ago(days: float, *, hours: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=days) + timedelta(hours=hours)).isoformat()


# ── include_breaks on the self path ─────────────────────────────────────────

def test_include_breaks_nets_the_self_rows(client):
    """The tech's week total must agree with the office page to the digit —
    without break_minutes it pays out every lunch."""
    tc, Session = client
    _seed_entry(Session, entry_id="shift", days_ago=1, minutes=480)
    _seed_break(Session, break_id="lunch", days_ago=1, offset_minutes=120, duration=30)

    r = tc.get("/api/timeclock/entries", params={"include_breaks": "true"})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [e["id"] for e in rows] == ["shift"]
    assert rows[0]["break_minutes"] == 30


def test_bare_self_call_is_unchanged(client):
    """Opt-in contract: the existing today-list callers send no flag and must
    keep getting exactly what they always got (break_minutes unpopulated)."""
    tc, Session = client
    _seed_entry(Session, entry_id="shift", days_ago=1, minutes=480)
    _seed_break(Session, break_id="lunch", days_ago=1, offset_minutes=120, duration=30)

    r = tc.get("/api/timeclock/entries")
    assert r.status_code == 200, r.text
    assert r.json()[0]["break_minutes"] is None


def test_include_breaks_does_not_open_the_crew(client):
    """include_breaks is about columns, not scope — it must not leak other
    people's rows into a tech's self read."""
    tc, Session = client
    _seed_entry(Session, entry_id="mine", tech=ME, days_ago=1)
    _seed_entry(Session, entry_id="theirs", tech="user-tech-b", days_ago=1)

    r = tc.get("/api/timeclock/entries", params={"include_breaks": "true"})
    assert r.status_code == 200, r.text
    assert [e["id"] for e in r.json()] == ["mine"]


# ── POST: technician_id optional, means "me" ────────────────────────────────

def test_post_without_technician_id_creates_for_self(client):
    tc, _ = client
    r = tc.post("/api/timeclock/entries", json={
        "clock_in_at": _iso_days_ago(1),
        "clock_out_at": _iso_days_ago(1, hours=8),
        "notes": "Forgot to clock in — worked the Hendricks install all day.",
    })
    assert r.status_code == 201, r.text
    assert r.json()["technician_id"] == ME
    assert r.json()["entry_type"] == "manual"


def test_post_for_someone_else_still_403s(client):
    """The optional field must not loosen _resolve_tech_id's rule."""
    tc, _ = client
    r = tc.post("/api/timeclock/entries", json={
        "technician_id": "user-tech-b",
        "clock_in_at": _iso_days_ago(1),
        "clock_out_at": _iso_days_ago(1, hours=8),
        "notes": "nope",
    })
    assert r.status_code == 403, r.text


# ── self-service guardrails: the note ───────────────────────────────────────

def test_tech_post_without_a_note_is_rejected(client):
    tc, _ = client
    r = tc.post("/api/timeclock/entries", json={
        "clock_in_at": _iso_days_ago(1),
        "clock_out_at": _iso_days_ago(1, hours=8),
    })
    assert r.status_code == 422, r.text
    assert "note" in r.json()["detail"]


def test_tech_patch_without_a_note_is_rejected(client):
    tc, Session = client
    _seed_entry(Session, entry_id="mine", days_ago=1)
    r = tc.patch("/api/timeclock/entries/mine", json={
        "clock_out_at": _iso_days_ago(1, hours=9),
    })
    assert r.status_code == 422, r.text
    assert "note" in r.json()["detail"]


def test_tech_patch_with_blank_note_is_rejected(client):
    """Whitespace is not an attestation."""
    tc, Session = client
    _seed_entry(Session, entry_id="mine", days_ago=1)
    r = tc.patch("/api/timeclock/entries/mine", json={
        "clock_out_at": _iso_days_ago(1, hours=9),
        "notes": "   ",
    })
    assert r.status_code == 422, r.text


def test_tech_patch_with_a_note_corrects_the_shift(client):
    tc, Session = client
    _seed_entry(Session, entry_id="mine", days_ago=1, minutes=480)
    r = tc.patch("/api/timeclock/entries/mine", json={
        "clock_out_at": _iso_days_ago(1, hours=9),
        "notes": "Forgot to clock out — left the Hendricks job at 5.",
    })
    assert r.status_code == 200, r.text
    assert r.json()["minutes"] == 9 * 60


def test_tech_closes_their_own_forgotten_open_shift(client):
    """The headline use case: yesterday's shift never got clocked out. The tech
    sets the real end time themselves instead of waiting on the office."""
    tc, Session = client
    _seed_entry(Session, entry_id="open", days_ago=1, closed=False)
    r = tc.patch("/api/timeclock/entries/open", json={
        "clock_out_at": _iso_days_ago(1, hours=8),
        "notes": "Never clocked out — day ended at 4.",
    })
    assert r.status_code == 200, r.text
    assert r.json()["clock_out_at"] is not None
    assert r.json()["minutes"] == 8 * 60


# ── self-service guardrails: the window ─────────────────────────────────────

def test_tech_cannot_touch_a_shift_older_than_the_window(client):
    tc, Session = client
    _seed_entry(Session, entry_id="paid", days_ago=OLD_DAYS)
    r = tc.patch("/api/timeclock/entries/paid", json={
        "clock_out_at": _iso_days_ago(OLD_DAYS, hours=10),
        "notes": "trying to stretch an old week",
    })
    assert r.status_code == 422, r.text
    assert "office" in r.json()["detail"]


def test_tech_cannot_backdate_a_recent_shift_into_a_paid_week(client):
    """The window binds the NEW clock-in too, or a tech could drag yesterday's
    row back into a week that's already been paid."""
    tc, Session = client
    _seed_entry(Session, entry_id="recent", days_ago=1)
    r = tc.patch("/api/timeclock/entries/recent", json={
        "clock_in_at": _iso_days_ago(OLD_DAYS),
        "clock_out_at": _iso_days_ago(OLD_DAYS, hours=8),
        "notes": "back-dating",
    })
    assert r.status_code == 422, r.text


def test_tech_cannot_create_a_shift_in_a_paid_week(client):
    tc, _ = client
    r = tc.post("/api/timeclock/entries", json={
        "clock_in_at": _iso_days_ago(OLD_DAYS),
        "clock_out_at": _iso_days_ago(OLD_DAYS, hours=8),
        "notes": "inventing an old shift",
    })
    assert r.status_code == 422, r.text


# ── the window is two-sided: no future hours, no impossible days ────────────

def test_tech_cannot_attest_hours_that_have_not_happened(client):
    """Audit finding (2026-08-13): the window only looked backward. A shift
    ending three hours from now is hours nobody has worked yet — and the same
    check catches a fat-fingered year typed into the DatePicker."""
    tc, _ = client
    r = tc.post("/api/timeclock/entries", json={
        "clock_in_at": _iso_days_ago(0, hours=-2),   # two hours ago — fine
        "clock_out_at": _iso_days_ago(0, hours=3),   # three hours from now — no
        "notes": "pre-attesting the afternoon",
    })
    assert r.status_code == 422, r.text
    assert "future" in r.json()["detail"]


def test_tech_can_end_a_manual_shift_at_roughly_now(client):
    """The skew allowance: 'worked until just now' from a phone whose clock
    runs a few minutes hot must not bounce."""
    tc, _ = client
    r = tc.post("/api/timeclock/entries", json={
        "clock_in_at": _iso_days_ago(0, hours=-8),
        "clock_out_at": _iso_days_ago(0, hours=0.05),  # ~3 minutes ahead
        "notes": "Forgot to clock in this morning.",
    })
    assert r.status_code == 201, r.text


def test_tech_cannot_book_a_day_longer_than_the_longest_possible_day(client):
    """MAX_SHIFT_HOURS is the system's own declaration of the longest real
    day (clock-in guard + sweep both auto-close at it). A longer self-entry is
    almost always a wrong date, and it books pay either way."""
    tc, _ = client
    r = tc.post("/api/timeclock/entries", json={
        "clock_in_at": _iso_days_ago(2),
        "clock_out_at": _iso_days_ago(2, hours=20),
        "notes": "typo day",
    })
    assert r.status_code == 422, r.text
    assert "longer than" in r.json()["detail"]


def test_tech_patch_cannot_stretch_a_shift_past_the_bound(client):
    tc, Session = client
    _seed_entry(Session, entry_id="mine", days_ago=2, minutes=480)
    r = tc.patch("/api/timeclock/entries/mine", json={
        "clock_out_at": _iso_days_ago(2, hours=20),
        "notes": "stretching",
    })
    assert r.status_code == 422, r.text


def test_office_can_still_book_an_implausible_day(client):
    """The office stays exempt — correcting weird rows is their job, and their
    timesheet card flags anything implausible anyway."""
    tc, _ = client
    _ROLE["value"] = "dispatcher"
    r = tc.post("/api/timeclock/entries", json={
        "technician_id": "user-tech-b",
        "clock_in_at": _iso_days_ago(2),
        "clock_out_at": _iso_days_ago(2, hours=20),
    })
    assert r.status_code == 201, r.text


# ── the office is exempt from both rules ────────────────────────────────────

def test_office_corrects_an_old_shift_without_a_note(client):
    """/timesheets must not regress: dispatch corrects anything, any age, and
    the note stays a strong suggestion rather than a gate."""
    tc, Session = client
    _seed_entry(Session, entry_id="old", tech="user-tech-b", days_ago=OLD_DAYS, minutes=480)
    _ROLE["value"] = "dispatcher"
    r = tc.patch("/api/timeclock/entries/old", json={
        "clock_out_at": _iso_days_ago(OLD_DAYS, hours=7),
    })
    assert r.status_code == 200, r.text
    assert r.json()["minutes"] == 7 * 60


def test_office_creates_an_old_manual_entry(client):
    tc, _ = client
    _ROLE["value"] = "dispatcher"
    r = tc.post("/api/timeclock/entries", json={
        "technician_id": "user-tech-b",
        "clock_in_at": _iso_days_ago(OLD_DAYS),
        "clock_out_at": _iso_days_ago(OLD_DAYS, hours=8),
    })
    assert r.status_code == 201, r.text
    assert r.json()["technician_id"] == "user-tech-b"


# ── unchanged boundaries, pinned as regressions ─────────────────────────────

def test_tech_still_cannot_edit_another_persons_entry(client):
    tc, Session = client
    _seed_entry(Session, entry_id="theirs", tech="user-tech-b", days_ago=1)
    r = tc.patch("/api/timeclock/entries/theirs", json={
        "clock_out_at": _iso_days_ago(1, hours=9),
        "notes": "not mine",
    })
    assert r.status_code == 403, r.text
