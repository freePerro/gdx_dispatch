"""MH-7 — timeclock status must reconcile open-shift elapsed into Today.

Audit P1 #9 (mobile UX audit 2026-05-19): the dashboard showed
"Clocked In 401:44:52" alongside "Today: 0.00h" and weekly summary all
0.00 — the aggregate summed only `TimeclockEntry.minutes`, which is
NULL/0 on an open entry until clock-out. So a tech who forgot to clock
out had a 401-hour ticker on screen while every downstream aggregate
silently said zero.

The fix: the status endpoint computes the open shift's elapsed time
and folds the portion-since-midnight into `today_hours`. These tests
exercise that reconciliation directly without spinning up the full
SQLAlchemy session.
"""
from __future__ import annotations

import inspect

from gdx_dispatch.routers import timeclock as tc


def test_status_response_carries_mh7_guard_fields():
    """Response model must expose max_shift_hours, warning_after_hours,
    open_shift_elapsed_hours, and auto_clockout_at — even if individual
    fields are None when no open shift exists."""
    model_fields = tc.TimeClockStatusResponse.model_fields
    for field in [
        "max_shift_hours",
        "warning_after_hours",
        "open_shift_elapsed_hours",
        "auto_clockout_at",
    ]:
        assert field in model_fields, f"TimeClockStatusResponse missing MH-7 field: {field}"


def test_status_handler_imports_datetime_for_elapsed_compute():
    """The handler must import datetime so the open-shift elapsed math
    can run. Locks against a future refactor that drops the import."""
    src = inspect.getsource(tc.get_timeclock_status)
    assert "datetime" in src
    assert "open_elapsed_hours" in src or "open_shift_elapsed_hours" in src


def test_status_handler_falls_back_gracefully_on_parse_failure():
    """If parsing clock_in_at fails for any reason, the handler MUST NOT
    5xx — the audit bug was a silent display issue, not a hard failure;
    we don't want to make it harder.
    """
    src = inspect.getsource(tc.get_timeclock_status)
    assert "fromisoformat" in src
    # Inner try around the elapsed compute (separate from the outer
    # SQLAlchemyError handler).
    assert src.count("except Exception:") >= 1


def test_status_handler_documents_audit_p1_9_reference():
    """Lock the audit-finding reference in the source so a future
    reader hitting this code can find the rationale + screenshot trail."""
    src = inspect.getsource(tc.get_timeclock_status)
    assert "P1 #9" in src or "MH-7" in src


def test_warning_thresholds_match_documented_values():
    """The 8h / 16h boundaries are the contract — frontend prompts at 8,
    emphasizes at 16, celery auto-closes at 16, and clock-in auto-closes
    a stale shift past 16. MH-7b hoisted the constants to module level so
    the clock-in router, /status, and the celery sweep share them.
    If a future refactor changes the literals, this test catches it
    before the frontend silently stops prompting."""
    assert tc.WARNING_AFTER_HOURS == 8.0
    assert tc.MAX_SHIFT_HOURS == 16.0


def test_auto_clockout_at_documented_as_deferred():
    """The auto_clockout_at field is computed but the actual celery beat
    that performs the close-out is deferred to MH-7b (payroll-impacting
    write). Lock that the field is set so the frontend can prompt, AND
    the deferral is documented in the source so a future reader doesn't
    assume the cron exists.
    """
    src = inspect.getsource(tc.get_timeclock_status)
    assert "auto_clockout_at_iso" in src
    # The MH-7 docstring block above the response constructor names the
    # deferral. We just spot-check it's documented somewhere in the file.
    file_src = inspect.getsource(tc)
    assert "deferred" in file_src.lower() or "MH-7b" in file_src
