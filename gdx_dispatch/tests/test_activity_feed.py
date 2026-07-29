"""Tests for gdx_dispatch/core/activity_feed.py — the noise policy.

Why this exists, measured on the live tenant: **47 of the 50 most recent audit
rows are auth noise**. The dashboard asked for 50 rows and filtered them in the
browser, so only 3 could ever reach a 10-row card — and during a busy auth
window nothing survived and the widget silently fell through to an unrelated
jobs-list fallback. The policy has to run in SQL, before the LIMIT.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.activity_feed import (
    FEED_HIDDEN_ACTIONS,
    collapse_runs,
    feed_filter,
    wanted_fetch_size,
)
from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync
from gdx_dispatch.tests.conftest import make_fresh_db


@pytest.fixture()
def db():
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    yield session
    session.close()
    engine.dispose()


def _seed(db, action, entity_type="job", entity_id="j-1", user_id="u-1"):
    log_audit_event_sync(
        db,
        tenant_id="t-1",
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details={},
    )
    db.commit()


# ---------------------------------------------------------------------------
# feed_filter
# ---------------------------------------------------------------------------


def test_session_churn_is_excluded_in_sql(db):
    _seed(db, "token_refreshed", entity_type="auth", entity_id="s-1")
    _seed(db, "job_updated")
    rows = db.execute(select(AuditLog).where(feed_filter())).scalars().all()
    actions = {r.action for r in rows}
    assert "job_updated" in actions
    assert "token_refreshed" not in actions


def test_the_noise_does_not_consume_the_page_budget(db):
    """The actual production shape: 47 noise rows, 3 real ones, LIMIT 10.

    Filtering after the LIMIT — which is what the browser was doing — returns
    at most 3 rows. Filtering before it returns all 3 AND leaves room.
    """
    for i in range(47):
        _seed(db, "token_refreshed", entity_type="auth", entity_id=f"s-{i}")
    for i in range(3):
        _seed(db, "job_updated", entity_id=f"j-{i}")

    unfiltered = (
        db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(10))
        .scalars()
        .all()
    )
    filtered = (
        db.execute(
            select(AuditLog).where(feed_filter()).order_by(AuditLog.created_at.desc()).limit(10)
        )
        .scalars()
        .all()
    )
    # Sanity: without the filter the newest 10 are dominated by noise.
    assert sum(1 for r in unfiltered if r.action == "token_refreshed") > 0
    assert len(filtered) == 3
    assert {r.action for r in filtered} == {"job_updated"}


@pytest.mark.parametrize(
    "action",
    [
        # The account-takeover chain. An earlier version of this filter hid
        # entity_type='auth' wholesale, which hid all of these.
        "login_failed",
        "failed_login",
        "login_blocked",
        "password_reset_requested",
        "password_reset_success",
        "token_revoked",
        "user_sessions_revoked",
        "refresh_replay_detected",
        "refresh_denied_token_revoked",
        "refresh_denied_db_verify",
        # A SUCCESSFUL login is the one a small business can actually act on:
        # credential stuffing that works produces no failures at all.
        "login_success",
    ],
)
def test_security_relevant_auth_events_stay_visible(db, action):
    _seed(db, action, entity_type="auth", entity_id="s-1")
    rows = db.execute(select(AuditLog).where(feed_filter())).scalars().all()
    assert [r.action for r in rows] == [action], (
        f"{action} is security-relevant and must not be filtered out of the feed"
    )


def test_only_routine_churn_is_hidden(db):
    for action in ("token_refreshed", "logout", "login"):
        _seed(db, action, entity_type="auth", entity_id="s-1")
    assert not db.execute(select(AuditLog).where(feed_filter())).scalars().all()


def test_nothing_is_deleted_only_hidden(db):
    _seed(db, "token_refreshed", entity_type="auth", entity_id="s-1")
    assert db.execute(select(AuditLog)).scalars().all(), "row must still exist"
    assert not db.execute(select(AuditLog).where(feed_filter())).scalars().all()


def test_hidden_set_contains_only_measured_churn():
    """Every name here must be an action the app actually writes, and must be
    routine. An earlier version listed `auth_login`, `auth_logout` and
    `session_renewed` — none of which anything writes — while the real
    high-volume names went unhidden."""
    assert "token_refreshed" in FEED_HIDDEN_ACTIONS
    assert "logout" in FEED_HIDDEN_ACTIONS
    for security_action in (
        "login_success",
        "login_failed",
        "failed_login",
        "password_reset_success",
        "token_revoked",
        "user_sessions_revoked",
        "refresh_replay_detected",
    ):
        assert security_action not in FEED_HIDDEN_ACTIONS


def test_overfetch_keeps_a_collapsed_page_full():
    """collapse_runs runs after the query, so asking for exactly page_size
    would hand back a nearly empty page whenever one record was edited a lot —
    the starved-feed bug, reintroduced by its own fix."""
    assert wanted_fetch_size(20, feed=True) > 20
    assert wanted_fetch_size(20, feed=False) == 20


def test_a_long_run_still_leaves_a_usable_page():
    # 20 edits to one estimate line, then 9 distinct events. Over-fetching 5x
    # means the distinct events are still on the page after the run collapses.
    rows = [_row("patch_line", "l-1", f"2026-07-28T19:{i:02d}:00Z") for i in range(20, 0, -1)]
    rows += [_row("job_updated", f"j-{i}", f"2026-07-28T18:{i:02d}:00Z", entity_type="job") for i in range(9, 0, -1)]
    out = collapse_runs(rows)
    assert out[0]["occurrence_count"] == 20
    assert len(out) == 10, "one collapsed run plus nine distinct events"


# ---------------------------------------------------------------------------
# collapse_runs
# ---------------------------------------------------------------------------


def _row(action, entity_id, created_at, user_id="u-1", entity_type="estimate_line"):
    return {
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "user_id": user_id,
        "created_at": created_at,
    }


def test_consecutive_edits_to_the_same_record_collapse():
    """Editing a 12-line estimate emits 12 patch_line rows in seconds. Ten of
    them fill the card and say nothing."""
    rows = [_row("patch_line", "l-1", f"2026-07-28T18:0{i}:00Z") for i in range(5, 0, -1)]
    out = collapse_runs(rows)
    assert len(out) == 1
    assert out[0]["occurrence_count"] == 5
    # newest survives; the run's oldest timestamp is carried alongside
    assert out[0]["created_at"] == "2026-07-28T18:05:00Z"
    assert out[0]["first_occurred_at"] == "2026-07-28T18:01:00Z"


def test_a_single_event_reports_a_count_of_one():
    out = collapse_runs([_row("job_updated", "j-1", "2026-07-28T18:00:00Z")])
    assert out[0]["occurrence_count"] == 1


def test_different_actors_do_not_collapse_together():
    rows = [
        _row("patch_line", "l-1", "2026-07-28T18:02:00Z", user_id="u-1"),
        _row("patch_line", "l-1", "2026-07-28T18:01:00Z", user_id="u-2"),
    ]
    assert len(collapse_runs(rows)) == 2


def test_different_subjects_do_not_collapse_together():
    rows = [
        _row("patch_line", "l-1", "2026-07-28T18:02:00Z"),
        _row("patch_line", "l-2", "2026-07-28T18:01:00Z"),
    ]
    assert len(collapse_runs(rows)) == 2


def test_only_consecutive_runs_merge_so_chronology_survives():
    """An unrelated event between two edits keeps them apart. Merging
    non-adjacent rows would silently reorder history."""
    rows = [
        _row("patch_line", "l-1", "2026-07-28T18:03:00Z"),
        _row("job_updated", "j-1", "2026-07-28T18:02:00Z", entity_type="job"),
        _row("patch_line", "l-1", "2026-07-28T18:01:00Z"),
    ]
    out = collapse_runs(rows)
    assert [r["action"] for r in out] == ["patch_line", "job_updated", "patch_line"]
    assert all(r["occurrence_count"] == 1 for r in out)


def test_collapsing_an_empty_page_is_a_noop():
    assert collapse_runs([]) == []


def test_collapse_does_not_mutate_the_input_rows():
    original = _row("patch_line", "l-1", "2026-07-28T18:01:00Z")
    snapshot = dict(original)
    collapse_runs([original, dict(original)])
    assert original == snapshot


# ---------------------------------------------------------------------------
# Router level — filter + collapse + LIMIT together
# ---------------------------------------------------------------------------


def test_router_feed_survives_the_real_production_shape(db):
    """The whole pipeline at once, against the shape that broke prod.

    Neither the filter nor the collapse was previously tested through the
    router, so nothing covered their interaction with the LIMIT — which is
    exactly where the second starved-feed bug lived.
    """
    from gdx_dispatch.routers import audit as audit_router

    # 47 rows of session churn, one long edit run, and a few distinct events.
    for i in range(47):
        _seed(db, "token_refreshed", entity_type="auth", entity_id=f"s-{i}")
    for i in range(20):
        _seed(db, "patch_line", entity_type="estimate_line", entity_id="l-1")
    for i in range(4):
        _seed(db, "job_updated", entity_type="job", entity_id=f"j-{i}")

    result = audit_router._list_rows(db, page=1, page_size=10, feed=True)
    items = result["items"]
    actions = [i["action"] for i in items]

    assert "token_refreshed" not in actions, "churn must not reach the feed"
    # The 20-row edit run is one entry carrying its count, not 20 rows.
    patch_entries = [i for i in items if i["action"] == "patch_line"]
    assert len(patch_entries) == 1
    assert patch_entries[0]["occurrence_count"] == 20
    # …and the distinct events are still on the page rather than pushed off it.
    assert sum(1 for a in actions if a == "job_updated") == 4


def test_router_feed_defaults_off_so_the_compliance_view_is_unchanged(db):
    """A bare `if feed:` would be truthy when this is called directly, because
    the default is a fastapi Query object, not a bool."""
    from gdx_dispatch.routers import audit as audit_router

    _seed(db, "token_refreshed", entity_type="auth", entity_id="s-1")
    result = audit_router._list_rows(db, page=1, page_size=10)
    assert [i["action"] for i in result["items"]] == ["token_refreshed"]
