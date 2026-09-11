"""The #700 guard: an audit write that no commit follows never lands.

``get_db()`` closes the session without committing, so an audit row flushed
after the last commit on its path is discarded. These tests pin the scan's
rules against synthetic modules, then run it over the real package — the
last test is the regression net for all 55 handlers fixed in #700.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from gdx_dispatch.tools import audit_after_commit_scan as scan_mod


def _scan(tmp_path: Path, source: str, name: str = "routers/mod.py") -> list[scan_mod.Finding]:
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(source))
    return scan_mod.scan(tmp_path)


def test_commit_then_audit_is_flagged(tmp_path):
    """The #700 shape itself: change committed, audit row flushed, nothing after."""
    found = _scan(tmp_path, """
        from gdx_dispatch.core.audit import log_audit_event_sync

        def create_area(db):
            db.add(object())
            db.commit()
            log_audit_event_sync(db, action="create", entity_type="holding_area")
            return {}
    """)
    assert [(f.function, f.kind) for f in found] == [("create_area", "after-commit")]


def test_audit_staged_before_the_commit_is_clean(tmp_path):
    assert _scan(tmp_path, """
        from gdx_dispatch.core.audit import log_audit_event_sync

        def create_area(db):
            db.add(object())
            log_audit_event_sync(db, action="create", entity_type="holding_area")
            db.commit()
            return {}
    """) == []


def test_audit_followed_by_its_own_commit_is_clean(tmp_path):
    """Where a helper committed the change, the row gets a commit of its own."""
    assert _scan(tmp_path, """
        from gdx_dispatch.core.audit import log_audit_event_sync

        def refresh(db, svc):
            svc.pull(db)
            db.commit()
            log_audit_event_sync(db, action="refresh", entity_type="cache")
            db.commit()
    """) == []


def test_early_return_branch_is_flagged_despite_a_later_commit(tmp_path):
    """The branch returns before the later commit can land its row."""
    found = _scan(tmp_path, """
        from gdx_dispatch.core.audit import log_audit_event_sync

        def update(db, fast):
            if fast:
                db.commit()
                log_audit_event_sync(db, action="update", entity_type="x")
                return {}
            db.commit()
            return {}
    """)
    assert [f.function for f in found] == ["update"]


def test_continue_does_not_end_the_path(tmp_path):
    """A `continue` goes back to the loop; the commit after the loop still runs."""
    assert _scan(tmp_path, """
        from gdx_dispatch.core.audit import ensure_audit_table, log_audit_event_sync

        def pull(db, rows):
            ensure_audit_table(db)
            db.commit()
            for row in rows:
                if row:
                    log_audit_event_sync(db, action="adopt", entity_type="customer")
                    continue
            db.commit()
    """) == []


def test_commit_inside_the_loop_does_not_land_the_last_row(tmp_path):
    found = _scan(tmp_path, """
        from gdx_dispatch.core.audit import log_audit_event_sync

        def apply_all(db, rows):
            for row in rows:
                db.commit()
                log_audit_event_sync(db, action="apply", entity_type="row")
    """)
    assert [f.function for f in found] == ["apply_all"]


def test_audit_wrapper_after_a_commit_is_flagged(tmp_path):
    """An audit write hidden in a helper that never commits is still a write."""
    found = _scan(tmp_path, """
        from gdx_dispatch.core.audit import log_audit_event_sync

        def _audit(db, action):
            log_audit_event_sync(db, action=action, entity_type="x")

        def handler(db):
            db.commit()
            _audit(db, "update")
    """)
    assert [f.function for f in found] == ["handler"]


def test_commit_inside_a_helper_counts_as_the_commit_before(tmp_path):
    """The change was committed by a helper; the row written after it is lost."""
    found = _scan(tmp_path, """
        from gdx_dispatch.core.audit import log_audit_event_sync

        def _save(db):
            db.commit()

        def handler(db):
            _save(db)
            log_audit_event_sync(db, action="update", entity_type="x")
    """)
    assert [f.function for f in found] == ["handler"]


def test_a_deep_callee_commit_does_not_count_as_landing_the_row(tmp_path):
    """routers/budgets.py: a helper three calls down commits only when it has to
    create a settings row. That must not read as landing the audit row."""
    _scan(tmp_path, """
        def get_or_create_settings(db):
            if db is None:
                db.commit()
    """, name="modules/forecast/service.py")
    found = _scan(tmp_path, """
        from gdx_dispatch.core.audit import log_audit_event_sync
        from gdx_dispatch.modules.forecast.service import get_or_create_settings

        def _revenue(db):
            return get_or_create_settings(db)

        def create_line(db):
            db.commit()
            log_audit_event_sync(db, action="create", entity_type="budget")
            return _revenue(db)
    """)
    assert [f.function for f in found] == ["create_line"]


def test_a_commit_in_one_branch_does_not_land_the_row(tmp_path):
    """routers/parts_needed.py: the only commit after the audit row ran for
    critical parts, so every ordinary part lost its row."""
    found = _scan(tmp_path, """
        from gdx_dispatch.core.audit import log_audit_event_sync

        def add_part(db, urgency):
            db.commit()
            log_audit_event_sync(db, action="create", entity_type="part_needed")
            try:
                if urgency == "critical":
                    db.commit()
            except Exception:
                pass
    """)
    assert [f.function for f in found] == ["add_part"]


def test_an_if_else_that_commits_on_both_sides_lands_the_row(tmp_path):
    assert _scan(tmp_path, """
        from gdx_dispatch.core.audit import log_audit_event_sync

        def handler(db, fast):
            db.commit()
            log_audit_event_sync(db, action="update", entity_type="x")
            if fast:
                db.commit()
            else:
                db.commit()
    """) == []


def test_a_callee_that_always_commits_lands_the_row(tmp_path):
    """Staged before a service whose body ends in a commit — the service's
    commit lands the change and the row together."""
    _scan(tmp_path, """
        def get_or_create(db):
            if db is None:
                db.commit()

        def update_settings(db, body):
            get_or_create(db)
            db.commit()
    """, name="modules/forecast/service.py")
    assert _scan(tmp_path, """
        from gdx_dispatch.core.audit import log_audit_event_sync
        from gdx_dispatch.modules.forecast import service

        def update(db, body):
            current = service.get_or_create(db)
            log_audit_event_sync(db, action="update", entity_type="settings")
            return service.update_settings(db, body)
    """) == []


def test_a_committing_helper_that_leaves_its_row_pending_is_a_writer(tmp_path):
    """core/payments._audit_money_event primes the guard (a commit) and then
    flushes its row for the CALLER to commit. Calling it after the caller's
    last commit loses the row, though the helper itself 'commits'."""
    found = _scan(tmp_path, """
        from gdx_dispatch.core.audit import ensure_audit_table, log_audit_event_sync

        def _audit_money_event(db):
            ensure_audit_table(db)
            log_audit_event_sync(db, action="overcharge", entity_type="invoice")

        def _audit_overcharge(db):
            _audit_money_event(db)

        def confirm(db):
            db.commit()
            _audit_overcharge(db)
    """)
    assert "confirm" in [f.function for f in found]


def test_an_audit_row_built_by_hand_after_a_commit_is_flagged(tmp_path):
    """modules/ledger builds its AuditLog rows directly — that is a write too."""
    found = _scan(tmp_path, """
        from gdx_dispatch.core.audit import AuditLog

        def _audit(db, action):
            db.add(AuditLog(action=action, entity_type="gl_settings"))

        def save_settings(db):
            db.commit()
            _audit(db, "update")
    """)
    assert [f.function for f in found] == ["save_settings"]


def test_route_handler_audit_with_no_commit_at_all_is_flagged(tmp_path):
    found = _scan(tmp_path, """
        from fastapi import APIRouter
        from gdx_dispatch.core.audit import log_audit_event_sync

        router = APIRouter()

        @router.post("/x")
        def handler(db):
            log_audit_event_sync(db, action="create", entity_type="x")
            return {}
    """)
    assert [(f.function, f.kind) for f in found] == [("handler", "no-commit")]


def test_repo_scan_is_clean():
    """Every audit write in the package has a commit after it, or a recorded
    reason in ALLOWED — and no ALLOWED entry has outlived what it excused."""
    findings = scan_mod.scan()
    assert scan_mod.unallowed(findings) == [], "\n".join(map(str, scan_mod.unallowed(findings)))
    assert scan_mod.stale_allowances(findings) == []
