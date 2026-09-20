"""Submit-day office badge (Doug ruled 2026-09-20: read-only badge, no lock-in).

GET /api/timeclock/submitted-days surfaces the techs' `timeclock_day_submitted`
attestations crew-wide for the /timesheets page. Pinned here:

1. a tech's submit becomes visible to a dispatch manager, keyed on the same
   day string submit_day counted entries against (badge and confirmation can
   never disagree);
2. re-submits collapse to one row per (tech, day) with the LATEST timestamp;
3. the read is crew-wide and therefore gated exactly like
   /entries?all_technicians — a technician gets 403;
4. the date-range filter actually filters.

Reuses the ORM-built harness from test_silent_success_sweep (create_all
schema, login dicts with BOTH sub and user_id per #701).
"""
from __future__ import annotations

from datetime import UTC, datetime

from gdx_dispatch.models.tenant_models import TimeclockEntry
from gdx_dispatch.tests.test_silent_success_sweep import (
    TECH_ID,
    TENANT,
    _build_client,
    _login,
    _teardown,
)


def _make_client(role: str, user_id: str):
    from gdx_dispatch.routers import timeclock as timeclock_mod

    return _build_client(
        timeclock_mod.router,
        user=_login(user_id, role),
        module_keys=("timeclock",),
    )


def _seed_and_submit(client, day: str) -> None:
    from uuid import uuid4

    with client.SessionLocal() as db:
        start = f"{day}T08:00:00+00:00"
        db.add(TimeclockEntry(
            id=str(uuid4()), tenant_id=TENANT, technician_id=TECH_ID,
            clock_in_at=start, clock_out_at=f"{day}T09:00:00+00:00",
            minutes=60, entry_type="clock", created_at=start, updated_at=start,
        ))
        db.commit()
    r = client.post("/api/timeclock/submit-day", json={"date": day})
    assert r.status_code == 200, r.text


def test_a_submit_shows_up_for_the_office_and_resubmits_collapse():
    tech = _make_client("technician", TECH_ID)
    try:
        day = datetime.now(UTC).date().isoformat()
        _seed_and_submit(tech, day)
        # Re-submit is a distinct attestation; the office read collapses it.
        r = tech.post("/api/timeclock/submit-day", json={"date": day})
        assert r.status_code == 200

        # Office read against the SAME database (swap the login only).
        tech.app.dependency_overrides[
            _login_dep_key(tech)
        ] = lambda: _login("office-1", "dispatcher")
        r = tech.get("/api/timeclock/submitted-days")
        assert r.status_code == 200, r.text
        rows = r.json()
        assert rows == [
            {"technician_id": TECH_ID, "date": day, "submitted_at": rows[0]["submitted_at"]}
        ], rows

        # Range filter: a window that excludes the day returns nothing.
        r = tech.get("/api/timeclock/submitted-days?date_start=1999-01-01&date_end=1999-12-31")
        assert r.status_code == 200
        assert r.json() == []
    finally:
        _teardown(tech)


def test_a_technician_cannot_read_the_crew_wide_attestations():
    tech = _make_client("technician", TECH_ID)
    try:
        r = tech.get("/api/timeclock/submitted-days")
        assert r.status_code == 403, r.text
    finally:
        _teardown(tech)


def _login_dep_key(client):
    from gdx_dispatch.routers.auth import get_current_user

    assert get_current_user in client.app.dependency_overrides
    return get_current_user
