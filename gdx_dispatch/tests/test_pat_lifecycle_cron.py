"""Tests for gdx_dispatch.tools.pat_lifecycle_cron (SS-14 slice G)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from gdx_dispatch.models.platform_extensions import AccessToken, EventOutbox
from gdx_dispatch.tools.pat_lifecycle_cron import (
    UNUSED_DELETE_DAYS,
    UNUSED_FLAG_DAYS,
    sweep,
)
from gdx_dispatch.tests.factories.platform import (
    make_access_token,
    make_capability_set,
)


def _now():
    return datetime.now(timezone.utc)


def _events_by_name(db, name):
    return list(
        db.execute(select(EventOutbox).where(EventOutbox.event_name == name)).scalars()
    )


def _mk_pat(db, *, last_used_days_ago=None, created_days_ago=5, expires_in_days=60, revoked=False, prefix="gdx_pat_live_"):
    now = _now()
    capset = make_capability_set(db)
    kwargs = {
        "capability_set": capset,
        "prefix": prefix,
        "created_at": now - timedelta(days=created_days_ago),
        "expires_at": now + timedelta(days=expires_in_days),
    }
    if last_used_days_ago is not None:
        kwargs["last_used_at"] = now - timedelta(days=last_used_days_ago)
    if revoked:
        kwargs["revoked_at"] = now
    return make_access_token(db, **kwargs)


def test_sweep_flags_pat_unused_beyond_flag_threshold(control_db):
    pat = _mk_pat(
        control_db,
        last_used_days_ago=UNUSED_FLAG_DAYS + 5,
        created_days_ago=UNUSED_FLAG_DAYS + 10,
    )
    control_db.commit()

    result = sweep(control_db)
    assert result.flagged == 1
    assert result.revoked_unused == 0
    assert result.revoked_expired == 0

    events = _events_by_name(control_db, "gdx_dispatch.pat.flagged_unused.v1")
    assert len(events) == 1
    assert str(events[0].payload["pat_id"]) == str(pat.id)

    # Still not revoked.
    control_db.refresh(pat)
    assert pat.revoked_at is None


def test_sweep_skips_fresh_pat(control_db):
    pat = _mk_pat(control_db, last_used_days_ago=1, created_days_ago=1)
    control_db.commit()

    result = sweep(control_db)
    assert result.flagged == 0
    assert result.revoked_unused == 0
    control_db.refresh(pat)
    assert pat.revoked_at is None


def test_sweep_revokes_expired_pat(control_db):
    pat = _mk_pat(control_db, last_used_days_ago=1, expires_in_days=-1)
    control_db.commit()

    result = sweep(control_db)
    assert result.revoked_expired == 1
    control_db.refresh(pat)
    assert pat.revoked_at is not None
    events = _events_by_name(control_db, "gdx_dispatch.pat.auto_revoked.v1")
    assert any(e.payload.get("reason") == "expired" for e in events)


def test_sweep_revokes_pat_unused_beyond_delete_threshold(control_db):
    pat = _mk_pat(
        control_db,
        last_used_days_ago=UNUSED_DELETE_DAYS + 5,
        created_days_ago=UNUSED_DELETE_DAYS + 10,
        expires_in_days=2000,
    )
    control_db.commit()

    result = sweep(control_db)
    assert result.revoked_unused == 1
    control_db.refresh(pat)
    assert pat.revoked_at is not None
    events = _events_by_name(control_db, "gdx_dispatch.pat.auto_revoked.v1")
    assert any(e.payload.get("reason") == "unused_over_threshold" for e in events)


def test_sweep_does_not_double_flag(control_db):
    _mk_pat(
        control_db,
        last_used_days_ago=UNUSED_FLAG_DAYS + 5,
        created_days_ago=UNUSED_FLAG_DAYS + 10,
    )
    control_db.commit()

    r1 = sweep(control_db)
    r2 = sweep(control_db)
    assert r1.flagged == 1
    # Second pass: dedup window suppresses a second flag.
    assert r2.flagged == 0

    events = _events_by_name(control_db, "gdx_dispatch.pat.flagged_unused.v1")
    assert len(events) == 1


def test_sweep_ignores_already_revoked_pats(control_db):
    _mk_pat(
        control_db,
        last_used_days_ago=UNUSED_DELETE_DAYS + 5,
        created_days_ago=UNUSED_DELETE_DAYS + 10,
        revoked=True,
    )
    control_db.commit()

    result = sweep(control_db)
    # Revoked rows are filtered out of the candidate set entirely.
    assert result.scanned == 0
    assert result.revoked_unused == 0


def test_sweep_never_used_pat_uses_created_at(control_db):
    """A PAT that was never used but is old enough to flag is flagged."""
    _mk_pat(
        control_db,
        last_used_days_ago=None,
        created_days_ago=UNUSED_FLAG_DAYS + 10,
    )
    control_db.commit()

    result = sweep(control_db)
    assert result.flagged == 1


def test_sweep_counts_scanned_rows(control_db):
    _mk_pat(control_db, last_used_days_ago=1, created_days_ago=1)
    _mk_pat(control_db, last_used_days_ago=1, created_days_ago=1)
    _mk_pat(control_db, last_used_days_ago=1, created_days_ago=1)
    control_db.commit()

    result = sweep(control_db)
    assert result.scanned == 3
    assert result.flagged == 0
